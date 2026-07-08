"""
FunASR llama.cpp 本地推理包装器（VAD 分段 + 逐段 ASR，带准确时间戳）
"""
import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path

# 部署路径
FUNASR_BIN = Path("/opt/funasr/bin/llama-funasr-sensevoice")
VAD_BIN = Path("/opt/funasr/bin/llama-funasr-vad")
MODEL_PATH = Path("/opt/funasr/models/sensevoice-small-q8.gguf")
VAD_MODEL_PATH = Path("/opt/funasr/models/fsmn-vad.gguf")

# 并发锁
_lock = asyncio.Lock()

# 模型就绪标志
_ready = False


async def _ensure_model() -> bool:
    global _ready
    if _ready:
        return True
    missing = []
    for p, name in [(FUNASR_BIN, "sensevoice"), (VAD_BIN, "vad"),
                    (MODEL_PATH, "模型"), (VAD_MODEL_PATH, "VAD模型")]:
        if not p.exists():
            missing.append(f"{name} ({p})")
    if missing:
        print(f"[FunASR] 缺少: {', '.join(missing)}")
        return False
    if not (FUNASR_BIN.stat().st_mode & 0o111):
        print(f"[FunASR] 二进制无执行权限")
        return False
    _ready = True
    return True


async def transcribe(audio_path: Path, timeout: int = 600) -> dict | None:
    """
    VAD 分段 → 逐段 ASR → 合并带时间戳的结果。
    每个 segment 都有准确的 start/end 时间。
    """
    if not await _ensure_model():
        return None

    # 获取音频总时长
    duration = _get_duration(audio_path)
    if duration <= 0:
        return None

    # 第1步：VAD 分段
    segments = await _vad_segment(audio_path)
    if not segments:
        # VAD 没分出段，整段处理
        segments = [(0.0, duration)]

    # 第2步：逐段 ASR
    all_segments = []
    full_text_parts = []
    for seg_start, seg_end in segments:
        text = await _transcribe_segment(audio_path, seg_start, seg_end, timeout)
        if text:
            all_segments.append({
                "start": round(seg_start, 2),
                "end": round(seg_end, 2),
                "text": text,
            })
            full_text_parts.append(text)

    if not all_segments:
        # 全部失败，兜底：整段跑一次
        text = await _transcribe_segment(audio_path, 0.0, duration, timeout)
        if not text:
            return None
        all_segments = [{"start": 0.0, "end": round(duration, 2), "text": text}]

    full_text = " ".join(full_text_parts)
    return {
        "language": _detect_lang(full_text),
        "duration": round(duration, 2),
        "segments": all_segments,
    }


async def _vad_segment(audio_path: Path) -> list[tuple[float, float]]:
    """用 FSMN-VAD 检测语音段，返回 [(start_sec, end_sec), ...]"""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(VAD_BIN),
            "-m", str(VAD_MODEL_PATH),
            "-a", str(audio_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return []

        # 解析 VAD 输出: "start_ms end_ms" 每行
        segments = []
        for line in stdout.decode().strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("[vad]"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    start_ms = int(parts[0])
                    end_ms = int(parts[1])
                    segments.append((start_ms / 1000.0, end_ms / 1000.0))
                except (ValueError, IndexError):
                    continue

        # 合并间隔小于 0.3 秒的相邻段
        if segments:
            merged = [segments[0]]
            for s, e in segments[1:]:
                if s - merged[-1][1] < 0.3:
                    merged[-1] = (merged[-1][0], e)
                else:
                    merged.append((s, e))
            segments = merged

        return segments
    except (asyncio.TimeoutError, Exception):
        return []


async def _transcribe_segment(
    audio_path: Path, start_sec: float, end_sec: float, global_timeout: int
) -> str | None:
    """
    提取音频的一段 [start, end]，送 sensevoice 识别。
    用 ffmpeg 切割到临时 wav 文件。
    """
    duration = end_sec - start_sec
    if duration <= 0.1:
        return None

    # 创建临时 wav
    tmpdir = tempfile.mkdtemp(prefix="funasr_")
    tmp_wav = Path(tmpdir) / "segment.wav"
    try:
        # ffmpeg 截取
        ffmpeg_proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-t", str(duration),
            "-i", str(audio_path),
            "-ar", "16000",
            "-ac", "1",
            "-sample_fmt", "s16",
            str(tmp_wav),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(ffmpeg_proc.wait(), timeout=30)
        if not tmp_wav.exists() or tmp_wav.stat().st_size == 0:
            return None

        # SenseVoice 推理
        sense_proc = await asyncio.create_subprocess_exec(
            str(FUNASR_BIN),
            "-m", str(MODEL_PATH),
            "-a", str(tmp_wav),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, _ = await asyncio.wait_for(
                sense_proc.communicate(), timeout=min(global_timeout, 120)
            )
        except asyncio.TimeoutError:
            sense_proc.kill()
            return None

        if sense_proc.returncode != 0:
            return None

        text = stdout.decode().strip()
        text = re.sub(r'<\|[^>]+\|>', '', text).strip()
        return text if text else None

    finally:
        # 清理临时文件
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _get_duration(audio_path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(audio_path)],
            capture_output=True, text=True, timeout=15,
        )
        info = json.loads(r.stdout)
        for s in info.get("streams", []):
            dur = s.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return 0.0


def _detect_lang(text: str) -> str:
    if not text:
        return "zh"
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    total = len(text.strip())
    if total == 0:
        return "zh"
    return "zh" if (cjk / total) > 0.1 else "en"
