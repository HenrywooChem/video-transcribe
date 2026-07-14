"""
AI Transcriber 后端主服务（优化版 v2.0）
====================================
功能: FunASR/Whisper 本地转录 + B站/YouTube 链接转录 + AI纠错总结
优化: URL结果缓存 · 并发控制 · 启动预加载 · 进度细化 · 合并AI请求
"""

import os, uuid, subprocess, json, re, hashlib, time, asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

import aiofiles
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
from openai import OpenAI

# ==================== 配置 ====================
BASE_DIR = Path(__file__).parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
MODEL_DIR = BASE_DIR / "models"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
MODEL_DIR.mkdir(exist_ok=True)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
FUN_ASR_MODEL = os.environ.get("FUN_ASR_MODEL", "iic/SenseVoiceSmall")

MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "2"))
CACHE_EXPIRE_HOURS = int(os.environ.get("CACHE_EXPIRE_HOURS", "24"))

# ffmpeg
FFMPEG_PATH = os.environ.get("FFMPEG_PATH")
if not FFMPEG_PATH:
    jianying = Path("C:/Users/IT-DEV/AppData/Local/JianyingPro/Apps/10.6.0.14057/ffmpeg.exe")
    FFMPEG_PATH = str(jianying) if jianying.exists() else "ffmpeg"

print(f"[Config] ffmpeg: {FFMPEG_PATH} | 最大并发: {MAX_CONCURRENT_TASKS} | 缓存有效期: {CACHE_EXPIRE_HOURS}h")

# ==================== 全局资源 ====================
ai_client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL) if OPENAI_API_KEY else None
asr_model = None
asr_backend = None
model_lock = asyncio.Lock()                     # ASR 模型并发锁
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)  # 并发任务数限制
tasks_db: dict = {}                              # 内存任务库
url_cache: dict = {}                             # URL → task_id 缓存

executor = ThreadPoolExecutor(max_workers=2)     # 阻塞操作（ASR）专用线程池


# ==================== ASR 模型（延迟加载） ====================
def get_asr_model():
    """获取本地 ASR 模型（懒加载：优先 Whisper → FunASR）"""
    global asr_model, asr_backend
    if asr_model is not None:
        return asr_model if asr_model != "error" else None
    try:
        import whisper
        print("[ASR] 加载 Whisper base...")
        asr_model = whisper.load_model("base")
        asr_backend = "whisper"
        print("[ASR] Whisper 就绪")
        return asr_model
    except Exception as e:
        print(f"[ASR] Whisper 失败: {e}")
    try:
        from funasr import AutoModel
        print(f"[ASR] 加载 FunASR: {FUN_ASR_MODEL}")
        asr_model = AutoModel(model=FUN_ASR_MODEL, device="cpu", disable_update=True)
        asr_backend = "funasr"
        print("[ASR] FunASR 就绪")
        return asr_model
    except Exception as e:
        print(f"[ASR] FunASR 失败: {e}")
        asr_model = "error"
    return None


# ==================== 转录函数 ====================
def transcribe_audio(audio_path: str) -> str:
    """
    转录音频（用线程池执行，不阻塞主线程）
    优先：OpenAI Whisper API → 本地 Whisper → 本地 FunASR
    """
    # 第一级：OpenAI Whisper API
    if OPENAI_API_KEY:
        try:
            print(f"[ASR] OpenAI API 转录: {audio_path}")
            with open(audio_path, "rb") as f:
                r = openai.OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)\
                    .audio.transcriptions.create(model="whisper-1", file=f, language="zh")
            return r.text
        except Exception as e:
            print(f"[ASR] API 失败: {e}")

    # 第二级：本地模型
    model = get_asr_model()
    if model is None:
        raise Exception("无可用 ASR 模型 && 未配置 API Key")

    if asr_backend == "whisper":
        result = model.transcribe(audio_path, language="zh")
        return result["text"]
    else:
        result = model.generate(input=audio_path)
        if isinstance(result, list) and len(result) > 0:
            text = result[0].get('text', '')
            return re.sub(r'<\|[^|]+\|>', '', text)
        return str(result)


async def ai_correct_and_summarize(text: str) -> dict:
    """合并纠错+总结合一次 AI 请求（减少 API 调用次数）"""
    if ai_client is None:
        return {"corrected": text, "summary": "（未配置 AI 服务）"}
    try:
        prompt = f"""你是一个专业的文字处理助手。请对下面的转录文本做两件事：

【任务一·纠错】修正错别字、标点、语病，保持原意。
【任务二·摘要】生成摘要：① 内容概述（2-3句）② 关键要点（3-5条）

请用以下格式返回：

---纠错结果---
（修正后的全文）

---摘要---
（摘要内容）

转录文本：
{text}"""
        resp = await asyncio.to_thread(
            ai_client.chat.completions.create,
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=4096,
        )
        content = resp.choices[0].message.content.strip()
        # 解析分段
        corrected, summary = text, ""
        if "---纠错结果---" in content:
            parts = content.split("---纠错结果---", 1)
            remaining = parts[1]
            if "---摘要---" in remaining:
                corrected = remaining.split("---摘要---", 1)[0].strip()
                summary = remaining.split("---摘要---", 1)[1].strip()
            else:
                corrected = remaining.strip()
        else:
            corrected = content
        return {"corrected": corrected, "summary": summary or "（AI 未生成摘要）"}
    except Exception as e:
        print(f"[AI] 处理失败: {e}")
        return {"corrected": text, "summary": f"（AI 处理失败: {e}）"}


# ==================== URL 缓存 ====================
def url_cache_key(url: str) -> str:
    return hashlib.md5(url.strip().encode()).hexdigest()

def get_cached_task_id(url: str) -> Optional[str]:
    key = url_cache_key(url)
    entry = url_cache.get(key)
    if not entry:
        return None
    tid, ts = entry["task_id"], entry["cached_at"]
    if (time.time() - ts) > CACHE_EXPIRE_HOURS * 3600:
        del url_cache[key]
        return None
    # 检查原任务是否已完成
    task = tasks_db.get(tid)
    if task and task.get("status") == "completed":
        return tid
    return None

def set_url_cache(url: str, task_id: str):
    url_cache[url_cache_key(url)] = {"task_id": task_id, "cached_at": time.time()}


# ==================== 后台任务 ====================
async def process_task(task_id: str, source: str, source_type: str):
    """带并发控制的后台任务"""
    async with task_semaphore:  # ← 并发上限
        try:
            t = tasks_db[task_id]
            t["status"] = "downloading"; t["progress"] = 5
            if source_type == "file":
                audio_path = source
            else:
                audio_path = await asyncio.to_thread(download_video, source, task_id)
            t["progress"] = 20

            t["status"] = "transcribing"; t["progress"] = 30

            # ASR 转录（在线程池执行避免阻塞事件循环）
            text = await asyncio.get_event_loop().run_in_executor(
                executor, transcribe_audio, audio_path
            )
            t["result_text"] = text; t["progress"] = 60

            # AI 纠错+总结（合并一次请求）
            t["status"] = "correcting"; t["progress"] = 70
            ai_result = await ai_correct_and_summarize(text)
            t["corrected_text"] = ai_result["corrected"]
            t["summary"] = ai_result["summary"]
            t["progress"] = 85

            # 保存文件
            output_file = OUTPUT_DIR / f"{task_id}.txt"
            async with aiofiles.open(output_file, "w", encoding="utf-8") as f:
                await f.write(f"# 转录结果\n\n## 原始转录\n\n{text}\n\n"
                              f"## AI纠错后\n\n{ai_result['corrected']}\n\n"
                              f"## AI摘要\n\n{ai_result['summary']}\n\n")
            t["status"] = "completed"
            t["progress"] = 100
            t["completed_at"] = datetime.now().isoformat()
        except Exception as e:
            tasks_db[task_id]["status"] = "error"
            tasks_db[task_id]["error_msg"] = str(e)
            print(f"[Task {task_id}] 错误: {e}")


def download_video(url: str, task_id: str) -> str:
    """下载视频/音频"""
    out = UPLOAD_DIR / task_id
    out.mkdir(exist_ok=True)
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(out / '%(id)s.%(ext)s'),
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'wav'}],
        'quiet': True, 'no_warnings': True,
        'ffmpeg_location': os.path.dirname(FFMPEG_PATH) if FFMPEG_PATH != 'ffmpeg' else None,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    for f in sorted(out.iterdir()):
        if f.suffix in ['.wav', '.mp3', '.m4a', '.aac', '.ogg']:
            return str(f)
    raise Exception("下载后未找到音频文件")


# ==================== 应用 ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时预加载 ASR 模型"""
    print("[Lifespan] 后台预加载 ASR 模型...")
    loop = asyncio.get_event_loop()
    loop.run_in_executor(executor, get_asr_model)
    yield
    print("[Lifespan] 服务关闭")

app = FastAPI(title="AI Transcriber", version="2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")


# ==================== API 路由 ====================

@app.get("/")
def root():
    return {"service": "AI Transcriber", "version": "2.0", "status": "running"}


@app.post("/api/transcribe/file")
@app.post("/api/transcribe/upload")
async def upload_and_transcribe(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """上传文件转录"""
    task_id = str(uuid.uuid4())
    file_path = UPLOAD_DIR / task_id / file.filename
    file_path.parent.mkdir(exist_ok=True)
    async with aiofiles.open(file_path, 'wb') as f:
        await f.write(await file.read())

    tasks_db[task_id] = {
        "task_id": task_id, "id": task_id,
        "source": str(file_path), "source_type": "file",
        "status": "pending", "progress": 0,
        "result_text": None, "corrected_text": None,
        "summary": None, "error_msg": None,
        "message": "等待处理",
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
    }
    background_tasks.add_task(process_task, task_id, str(file_path), "file")
    return {"task_id": task_id, "message": "任务已创建"}


@app.post("/api/transcribe/url")
async def transcribe_from_url(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
):
    """从 URL 转录（含缓存检查）"""
    # URL 格式校验
    if not any(x in url for x in ["bilibili.com", "b23.tv", "youtube.com", "youtu.be"]):
        raise HTTPException(400, "仅支持 B站 / YouTube")

    source_type = "bilibili" if ("bilibili" in url or "b23.tv" in url) else "youtube"

    # ← 缓存命中检测
    cached = get_cached_task_id(url)
    if cached:
        print(f"[Cache] 命中: {url[:50]} -> {cached}")
        return {"task_id": cached, "message": "已有处理结果", "cached": True}

    task_id = str(uuid.uuid4())
    tasks_db[task_id] = {
        "task_id": task_id, "id": task_id,
        "source": url, "source_type": source_type,
        "status": "pending", "progress": 0,
        "result_text": None, "corrected_text": None,
        "summary": None, "error_msg": None,
        "message": "等待处理",
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
    }
    set_url_cache(url, task_id)
    background_tasks.add_task(process_task, task_id, url, source_type)
    return {"task_id": task_id, "message": "任务已创建"}


@app.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    """查询任务状态（兼容 id / task_id 字段）"""
    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(404, detail="任务不存在")
    # 统一返回字段，小程序和网站都能用
    return {
        "task_id": task.get("task_id") or task.get("id"),
        "id": task.get("id") or task.get("task_id"),
        "status": task["status"],
        "progress": task.get("progress", 0),
        "message": task.get("message") or task.get("status", ""),
        "error": task.get("error_msg"),
        "title": task.get("title", ""),
        "url": task.get("source") if task.get("source_type") != "file" else None,
        "result_url": f"/view/{task_id}" if task["status"] == "completed" else None,
        "created_at": task.get("created_at"),
        "completed_at": task.get("completed_at"),
        "result": {
            "corrected_text": task.get("corrected_text"),
            "segments": task.get("segments", []),
            "summary": task.get("summary"),
            "language": task.get("language", "zh"),
            "duration": task.get("duration", 0),
        } if task["status"] == "completed" else None,
    }


@app.get("/api/task/{task_id}/result")
async def get_task_result_file(task_id: str):
    """获取结果 .txt 文件"""
    f = OUTPUT_DIR / f"{task_id}.txt"
    if not f.exists():
        raise HTTPException(404, detail="结果文件不存在")
    return FileResponse(f, media_type="text/plain", filename=f"transcript_{task_id}.txt")


@app.get("/api/tasks")
async def list_tasks(limit: int = Query(50, ge=1, le=200)):
    """任务列表（支持 limit 分页）"""
    all_tasks = list(tasks_db.values())
    all_tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    return {"tasks": all_tasks[:limit]}


@app.get("/api/export/{task_id}")
async def export_task(task_id: str, fmt: str = Query("txt")):
    """导出结果（小程序 export 接口）"""
    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(404, detail="任务不存在")
    if task["status"] != "completed":
        raise HTTPException(400, detail="任务未完成")

    text = task.get("corrected_text") or task.get("result_text") or ""
    if fmt == "txt":
        return PlainTextResponse(text, media_type="text/plain")
    elif fmt == "json":
        return task
    else:
        return PlainTextResponse(text, media_type="text/plain")


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "asr_model": asr_backend or "loading...",
        "ai_service": "configured" if ai_client else "not_configured",
        "pending_tasks": sum(1 for t in tasks_db.values() if t["status"] not in ("completed", "error")),
        "cached_urls": len(url_cache),
        "total_tasks": len(tasks_db),
    }


@app.get("/api/view/{task_id}")
async def view_result(task_id: str):
    """查看结果（网页版 result_url 指向）"""
    task = tasks_db.get(task_id)
    if not task:
        raise HTTPException(404, detail="任务不存在")
    text = task.get("corrected_text") or task.get("result_text") or ""
    return PlainTextResponse(text, media_type="text/plain;charset=utf-8")


# ==================== 启动 ====================
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8001"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"🚀 AI Transcriber v2.0 @ http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
