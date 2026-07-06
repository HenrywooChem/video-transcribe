"""
视频转录服务 - Web应用版
支持：文件上传转录 + URL下载转录 + API Key鉴权
"""
import os, json, time, uuid, asyncio, shutil
from pathlib import Path
import sys
import hashlib, base64, secrets, sqlite3
import jwt
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form, Query, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ============================================================
# 加载环境变量（兼容 .env 文件）
# ============================================================
_env_file = Path("/home/ubuntu/.hermes/.env")
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k not in os.environ:  # 不覆盖已有的环境变量
                os.environ[k] = v.strip('"').strip("'")

# 确保关键 Key 存在（优先使用百炼，其次是 DeepSeek）
_DASHSCOPE_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
_DASHSCOPE_WORKSPACE = os.environ.get("DASHSCOPE_WORKSPACE", "")
_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
_LLM_PROVIDER = "deepseek" if _DEEPSEEK_KEY else ("dashscope" if _DASHSCOPE_KEY else "none")

# 外部可访问的基础URL（用于 ASR API 下载音频文件）
_PUBLIC_BASE_URL = "https://ai4u.site"

# 微信小程序配置
_WX_APPID = os.environ.get("WX_APPID", "")
_WX_SECRET = os.environ.get("WX_SECRET", "")

# ============================================================
# 配置
# ============================================================
DATA_DIR = Path("/home/ubuntu/video-transcribe/data")
UPLOAD_DIR = DATA_DIR / "uploads"
RESULTS_DIR = DATA_DIR / "results"
TASKS_FILE = DATA_DIR / "tasks.json"
STATIC_DIR = Path(__file__).parent / "static"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
CLEANUP_HOURS = 24                  # 24小时后清理

for d in [UPLOAD_DIR, RESULTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="视频转录服务", version="1.0.0")

# 确保 .venv 的 site-packages 在 Python 路径中
_venv_site = Path(__file__).parent.parent / ".venv" / "lib" / "python3.11" / "site-packages"
if _venv_site.exists() and str(_venv_site) not in sys.path:
    sys.path.insert(0, str(_venv_site))

# 挂载静态文件（CSS/JS）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ============================================================
# API Key 鉴权（简单模式）
# ============================================================
API_KEYS_FILE = DATA_DIR / "api_keys.json"
API_KEYS = {}

def load_api_keys():
    global API_KEYS
    if API_KEYS_FILE.exists():
        API_KEYS = json.loads(API_KEYS_FILE.read_text())
    else:
        # 默认 Key
        API_KEYS = {
            "sk-demo": {"name": "demo", "created_at": time.time(), "used": 0}
        }
        save_api_keys()

def save_api_keys():
    API_KEYS_FILE.write_text(json.dumps(API_KEYS, ensure_ascii=False, indent=2))

load_api_keys()

# ============================================================
# 用户认证（JWT + SQLite）
# ============================================================
JWT_SECRET_FILE = DATA_DIR / "jwt_secret.txt"
if JWT_SECRET_FILE.exists():
    JWT_SECRET = JWT_SECRET_FILE.read_text().strip()
else:
    JWT_SECRET = hashlib.sha256(os.urandom(64)).hexdigest()
    JWT_SECRET_FILE.write_text(JWT_SECRET)

USERS_DB = DATA_DIR / "users.db"

def init_users_db():
    conn = sqlite3.connect(str(USERS_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at REAL,
            is_admin INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return salt + ':' + base64.b64encode(h).decode()

def verify_password(password: str, hash_str: str) -> bool:
    parts = hash_str.split(':', 1)
    if len(parts) != 2:
        return False
    salt, stored_hash = parts
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return base64.b64encode(h).decode() == stored_hash

def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.utcnow() + timedelta(days=30),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

init_users_db()

# ============================================================
# 用户额度系统（每月免费30分钟 + 每日签到+5分钟）
# ============================================================
MONTHLY_FREE_MINUTES = 300   # 每月免费30分钟
CHECKIN_BONUS_MINUTES = 5   # 每日签到加5分钟

def get_current_month() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m")

def get_today() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")

def init_quota_db():
    """初始化额度表和支付订单表"""
    conn = sqlite3.connect(str(USERS_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_quota (
            username TEXT PRIMARY KEY,
            month TEXT NOT NULL,
            used_seconds REAL DEFAULT 0,
            bonus_seconds REAL DEFAULT 0,
            last_checkin_date TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payment_orders (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            package_name TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            extra_seconds REAL NOT NULL,
            currency TEXT DEFAULT 'cny',
            status TEXT DEFAULT 'pending',
            stripe_session_id TEXT,
            created_at REAL,
            paid_at REAL
        )
    """)
    conn.commit()
    conn.close()

init_quota_db()

def get_user_quota(username: str) -> dict:
    """获取用户当月额度详情"""
    conn = sqlite3.connect(str(USERS_DB))
    month = get_current_month()
    row = conn.execute(
        "SELECT used_seconds, bonus_seconds, last_checkin_date FROM user_quota WHERE username=? AND month=?",
        (username, month)
    ).fetchone()
    conn.close()
    if not row:
        return {
            "month": month,
            "free_seconds": MONTHLY_FREE_MINUTES * 60,
            "bonus_seconds": 0,
            "used_seconds": 0,
            "available_seconds": MONTHLY_FREE_MINUTES * 60,
            "total_seconds": MONTHLY_FREE_MINUTES * 60,
            "last_checkin_date": None,
            "can_checkin": True,
        }
    used_secs = row[0] or 0
    bonus_secs = row[1] or 0
    last_checkin = row[2]
    free_secs = MONTHLY_FREE_MINUTES * 60
    total_available = free_secs + bonus_secs - used_secs
    today = get_today()
    can_checkin = (last_checkin != today)
    return {
        "month": month,
        "free_seconds": free_secs,
        "bonus_seconds": bonus_secs,
        "used_seconds": used_secs,
        "available_seconds": max(0, total_available),
        "total_seconds": free_secs + bonus_secs,
        "last_checkin_date": last_checkin,
        "can_checkin": can_checkin,
    }

def do_checkin(username: str) -> dict:
    """每日签到，奖励 5 分钟（使用 UPSERT 避免并发冲突）"""
    conn = sqlite3.connect(str(USERS_DB))
    conn.execute("PRAGMA busy_timeout=5000")
    month = get_current_month()
    today = get_today()
    row = conn.execute(
        "SELECT bonus_seconds, last_checkin_date FROM user_quota WHERE username=? AND month=?",
        (username, month)
    ).fetchone()
    if row:
        last_date = row[1]
        if last_date == today:
            conn.close()
            return {"success": False, "message": "今天已经签到过了"}
        new_bonus = (row[0] or 0) + CHECKIN_BONUS_MINUTES * 60
        conn.execute(
            "UPDATE user_quota SET bonus_seconds=?, last_checkin_date=? WHERE username=? AND month=?",
            (new_bonus, today, username, month)
        )
    else:
        new_bonus = CHECKIN_BONUS_MINUTES * 60
        # UPSERT: 如果用户已存在则 UPDATE，否则 INSERT
        # 注意：user_quota 的 PK 是 username，不含 month
        conn.execute(
            "INSERT INTO user_quota (username, month, bonus_seconds, last_checkin_date) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "bonus_seconds=bonus_seconds+?, last_checkin_date=?",
            (username, month, new_bonus, today, CHECKIN_BONUS_MINUTES * 60, today)
        )
    conn.commit()
    conn.close()
    return {"success": True, "message": f"签到成功！+{CHECKIN_BONUS_MINUTES}分钟额度", "bonus_seconds": new_bonus}

def deduct_quota(username: str, seconds: float):
    """扣除用户额度（转录完成时调用）"""
    conn = sqlite3.connect(str(USERS_DB))
    month = get_current_month()
    row = conn.execute(
        "SELECT used_seconds FROM user_quota WHERE username=? AND month=?",
        (username, month)
    ).fetchone()
    if row:
        new_used = (row[0] or 0) + seconds
        conn.execute(
            "UPDATE user_quota SET used_seconds=? WHERE username=? AND month=?",
            (new_used, username, month)
        )
    else:
        conn.execute(
            "INSERT INTO user_quota (username, month, used_seconds) VALUES (?, ?, ?)",
            (username, month, seconds)
        )
    conn.commit()
    conn.close()

def create_payment_order(username: str, package_name: str, amount_cents: int, extra_seconds: float) -> dict:
    """创建支付订单（优先虎皮椒微信支付，备选Stripe）"""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from xunhupay import create_order as xh_create, is_configured as xh_configured

    order_id = uuid.uuid4().hex[:12]
    conn = sqlite3.connect(str(USERS_DB))
    conn.execute(
        "INSERT INTO payment_orders (id, username, package_name, amount_cents, extra_seconds, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, username, package_name, amount_cents, extra_seconds, time.time())
    )
    conn.commit()
    conn.close()

    payment_url = None
    payment_qrcode = None
    payment_method = None

    # 1) 优先虎皮椒微信支付
    if xh_configured():
        try:
            amount_yuan = round(amount_cents / 100, 2)
            xh_resp = xh_create(
                trade_order_id=order_id,
                total_fee=amount_yuan,
                title=package_name,
                notify_url=f"{_PUBLIC_BASE_URL}/api/payment/xunhupay-notify",
                return_url=f"{_PUBLIC_BASE_URL}/?payment_success={order_id}",
            )
            if xh_resp.get("errcode") == 0:
                payment_url = xh_resp.get("url") or xh_resp.get("url_qrcode")
                payment_qrcode = xh_resp.get("url_qrcode")
                payment_method = "xunhupay"
                print(f"[Payment] 虎皮椒订单 {order_id} 已创建, 金额 {amount_yuan}元")
        except Exception as e:
            print(f"[Payment] 虎皮椒下单失败: {e}")

    # 2) 备选 Stripe
    if not payment_method:
        stripe_key = os.environ.get("STRIPE_API_KEY", "")
        if stripe_key:
            try:
                import stripe
                stripe.api_key = stripe_key
                session = stripe.checkout.Session.create(
                    payment_method_types=['card'],
                    line_items=[{
                        'price_data': {
                            'currency': 'cny',
                            'product_data': {'name': package_name},
                            'unit_amount': amount_cents,
                        },
                        'quantity': 1,
                    }],
                    mode='payment',
                    success_url=f"{_PUBLIC_BASE_URL}/?payment_success={order_id}",
                    cancel_url=f"{_PUBLIC_BASE_URL}/?payment_cancelled=1",
                    metadata={'order_id': order_id, 'username': username}
                )
                payment_url = session.url
                payment_method = "stripe"
                conn = sqlite3.connect(str(USERS_DB))
                conn.execute(
                    "UPDATE payment_orders SET stripe_session_id=? WHERE id=?",
                    (session.id, order_id)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"[Payment] Stripe error: {e}")

    return {
        "order_id": order_id,
        "amount_cents": amount_cents,
        "amount_yuan": round(amount_cents / 100, 2),
        "package_name": package_name,
        "extra_seconds": extra_seconds,
        "payment_url": payment_url,
        "payment_qrcode": payment_qrcode,
        "payment_method": payment_method,
        "status": "pending",
    }

def get_user_payment_history(username: str, limit: int = 10) -> list:
    """获取用户支付历史"""
    conn = sqlite3.connect(str(USERS_DB))
    rows = conn.execute(
        "SELECT id, package_name, amount_cents, extra_seconds, status, created_at, paid_at FROM payment_orders WHERE username=? ORDER BY created_at DESC LIMIT ?",
        (username, limit)
    ).fetchall()
    conn.close()
    return [{
        "order_id": r[0],
        "package_name": r[1],
        "amount_yuan": round(r[2] / 100, 2),
        "extra_minutes": round(r[3] / 60, 1),
        "status": r[4],
        "created_at": r[5],
        "paid_at": r[6],
    } for r in rows]

def confirm_payment_order(order_id: str):
    """确认订单已支付，为用户增加额度"""
    conn = sqlite3.connect(str(USERS_DB))
    row = conn.execute(
        "SELECT username, extra_seconds FROM payment_orders WHERE id=? AND status='pending'",
        (order_id,)
    ).fetchone()
    if not row:
        conn.close()
        return False
    username, extra_seconds = row
    # 加为 bonus (可跨月使用? 直接加到当月bonus)
    month = get_current_month()
    quota_row = conn.execute(
        "SELECT bonus_seconds FROM user_quota WHERE username=? AND month=?",
        (username, month)
    ).fetchone()
    if quota_row:
        new_bonus = (quota_row[0] or 0) + extra_seconds
        conn.execute(
            "UPDATE user_quota SET bonus_seconds=? WHERE username=? AND month=?",
            (new_bonus, username, month)
        )
    else:
        conn.execute(
            "INSERT INTO user_quota (username, month, bonus_seconds) VALUES (?, ?, ?)",
            (username, month, extra_seconds)
        )
    conn.execute(
        "UPDATE payment_orders SET status='paid', paid_at=? WHERE id=?",
        (time.time(), order_id)
    )
    conn.commit()
    conn.close()
    return True

# Stripe Webhook 签名密钥（可选）
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

async def get_current_user(request: Request):
    """从 Authorization header 获取当前登录用户"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "未登录，请先登录")
    username = verify_token(auth[7:])
    if not username:
        raise HTTPException(401, "登录已过期，请重新登录")
    return username

def verify_key(request: Request):
    """从请求头或查询参数获取 API Key"""
    key = request.headers.get("X-API-Key") or request.query_params.get("key")
    if key and key in API_KEYS:
        API_KEYS[key]["used"] += 1
        save_api_keys()
        return API_KEYS[key]["name"]
    return None

# ============================================================
# 任务管理
# ============================================================
tasks = {}

def load_tasks():
    global tasks
    if TASKS_FILE.exists():
        tasks = json.loads(TASKS_FILE.read_text())
    else:
        tasks = {}

def save_tasks():
    TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2))

load_tasks()

# ============================================================
# 双 ASR 容灾转录：阿里百炼（主）→ 腾讯云 ASR（备）
# ============================================================
# 腾讯云 ASR 密钥（从环境变量读取）
_TX_SECRET_ID = os.environ.get("TX_SECRET_ID", "")
_TX_SECRET_KEY = os.environ.get("TX_SECRET_KEY", "")

async def transcribe_audio(audio_path: Path) -> dict:
    """双 ASR 转录：主用阿里百炼，备用腾讯云"""
    import subprocess, json, time, asyncio, urllib.parse, base64
    import httpx
    from pathlib import Path

    # ===== 获取音频时长 =====
    def _get_duration() -> float:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_streams", str(audio_path)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            info = json.loads(r.stdout)
            for s in info.get("streams", []):
                dur = s.get("duration")
                if dur:
                    return float(dur)
        except Exception:
            pass
        return 0.0

    duration = _get_duration()
    if duration <= 0:
        return {"language": "zh", "duration": 0,
                "segments": [{"start": 0.0, "end": 0.0, "text": "(音频时长获取失败)"}]}

    dur_rounded = round(duration, 2)

    # ===== 尝试阿里百炼 =====
    result = await _try_aliyun_asr(audio_path, dur_rounded)
    if result is not None:
        return result

    # ===== 阿里失败，降级到腾讯云 =====
    print(f"[ASR] 阿里百炼降级，切换到腾讯云 ASR")
    result = await _try_tencent_asr(audio_path, dur_rounded)
    if result is not None:
        return result

    # ===== 都失败 =====
    return {"language": "zh", "duration": dur_rounded,
            "segments": [{"start": 0.0, "end": dur_rounded, "text": "(所有ASR均不可用)"}]}


async def _try_aliyun_asr(audio_path: Path, dur_rounded: float) -> dict | None:
    """尝试阿里百炼工作空间 fun-asr-flash"""
    import json, asyncio, urllib.parse
    import httpx

    _ASR_API_BASE = os.environ.get("DASHSCOPE_API_BASE", "https://dashscope.aliyuncs.com")
    _ASR_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
    _ASR_MODEL = os.environ.get("ASR_MODEL", "fun-asr-flash-2026-06-15")
    _PUBLIC_BASE_URL = "https://ai4u.site"

    if not _ASR_API_KEY:
        return None

    # 构建文件公开 URL
    filename = audio_path.name
    try:
        relative = audio_path.relative_to(Path("/home/ubuntu/video-transcribe/data/uploads"))
        temp_path = str(relative)
    except ValueError:
        temp_path = filename
    encoded_path = urllib.parse.quote(temp_path, safe='/')
    public_url = f"{_PUBLIC_BASE_URL}/temp_audio/{encoded_path}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_ASR_API_BASE}/api/v1/services/asr/transcriptions",
                headers={
                    "Authorization": f"Bearer {_ASR_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _ASR_MODEL,
                    "input": {"file_urls": [public_url]},
                },
            )
    except Exception as e:
        print(f"[阿里ASR] 网络错误: {e}")
        return None

    if resp.status_code != 200:
        try:
            err_body = resp.json()
            err_msg = err_body.get("message", "")
        except Exception:
            err_msg = resp.text[:200]
        # "url error" 是网络不可达的错误，需要降级
        if "url error" in err_msg:
            print(f"[阿里ASR] 网络不可达，降级")
            return None
        return {"language": "zh", "duration": dur_rounded,
                "segments": [{"start": 0.0, "end": dur_rounded,
                              "text": f"(转录失败: {err_msg[:200]})"}]}

    body = resp.json()
    task_id = body.get("output", {}).get("task_id", "")
    if not task_id:
        return {"language": "zh", "duration": dur_rounded,
                "segments": [{"start": 0.0, "end": dur_rounded,
                              "text": "(转录失败: 未获取到任务ID)"}]}

    # 轮询结果（最长 10 分钟）
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(120):
            await asyncio.sleep(5)
            try:
                poll = await client.get(
                    f"{_ASR_API_BASE}/api/v1/services/asr/transcriptions/{task_id}",
                    headers={"Authorization": f"Bearer {_ASR_API_KEY}"},
                )
                poll_body = poll.json()
                status = poll_body.get("output", {}).get("task_status", "")
                if status == "SUCCEEDED":
                    break
                elif status == "FAILED":
                    err_msg = poll_body.get("output", {}).get("message", "未知错误")
                    return {"language": "zh", "duration": dur_rounded,
                            "segments": [{"start": 0.0, "end": dur_rounded,
                                          "text": f"(转录失败: {err_msg[:200]})"}]}
            except Exception:
                continue
        else:
            return {"language": "zh", "duration": dur_rounded,
                    "segments": [{"start": 0.0, "end": dur_rounded,
                                  "text": "(转录超时)"}]}

    # 解析识别结果
    all_segments = []
    full_text = ""
    for r_item in poll_body.get("output", {}).get("results", []):
        trans_url = r_item.get("transcription_url", "")
        if trans_url:
            try:
                import requests as _requests
                seg_resp = _requests.get(trans_url, timeout=30)
                seg_data = seg_resp.json()
                for seg in seg_data.get("transcripts", []):
                    text = seg.get("text", "").strip()
                    if text:
                        all_segments.append({
                            "start": seg.get("begin_time", 0.0),
                            "end": seg.get("end_time", dur_rounded),
                            "text": text,
                        })
                        full_text += text
            except Exception:
                pass

    if not all_segments and full_text.strip():
        all_segments.append({
            "start": 0.0, "end": dur_rounded, "text": full_text.strip()
        })
    if not all_segments:
        all_segments = [{"start": 0.0, "end": dur_rounded, "text": "(无识别结果)"}]

    return {"language": "zh", "duration": dur_rounded, "segments": all_segments}


async def _try_tencent_asr(audio_path: Path, dur_rounded: float) -> dict | None:
    """备用：腾讯云 ASR 录音文件识别（直传音频）"""
    import base64, json, time as _time

    tx_id = os.environ.get("TX_SECRET_ID", "")
    tx_key = os.environ.get("TX_SECRET_KEY", "")
    if not tx_id or not tx_key:
        return None

    try:
        from tencentcloud.common import credential as tc_cred
        from tencentcloud.asr.v20190614 import asr_client, models
    except ImportError:
        print("[腾讯ASR] SDK 未安装")
        return None

    try:
        cred = tc_cred.Credential(tx_id, tx_key)
        client = asr_client.AsrClient(cred, "ap-guangzhou")

        # 读取音频文件
        audio_path_str = str(audio_path)
        raw_size = os.path.getsize(audio_path_str)

        # 小文件直传，大文件用 URL（腾讯云内网可访问）
        if raw_size <= 3_500_000:  # base64 后不超过 5MB
            with open(audio_path_str, "rb") as f:
                audio_data = base64.b64encode(f.read()).decode("utf-8")
            req = models.CreateRecTaskRequest()
            req.EngineModelType = "16k_zh"
            req.ChannelNum = 1
            req.ResTextFormat = 1
            req.SourceType = 1          # 直传
            req.Data = audio_data
            req.DataLen = raw_size      # 原始文件大小，不是 base64 长度
        else:
            # URL 模式：用公网 IP + HTTP（同腾讯云内网可达）
            import urllib.parse
            try:
                relative = audio_path.relative_to(Path("/home/ubuntu/video-transcribe/data/uploads"))
                temp_path = str(relative)
            except ValueError:
                temp_path = audio_path.name
            encoded_path = urllib.parse.quote(temp_path, safe='/')
            public_url = f"http://124.221.77.205:8000/temp_audio/{encoded_path}"
            req = models.CreateRecTaskRequest()
            req.EngineModelType = "16k_zh"
            req.ChannelNum = 1
            req.ResTextFormat = 1
            req.SourceType = 0          # URL 模式
            req.Url = public_url

        resp = client.CreateRecTask(req)
        task_id = resp.Data.TaskId
    except Exception as e:
        print(f"[腾讯ASR] 提交失败: {e}")
        return None

    # 轮询结果（最长 5 分钟）
    for i in range(150):
        await asyncio.sleep(2)
        try:
            req2 = models.DescribeTaskStatusRequest()
            req2.TaskId = task_id
            resp2 = client.DescribeTaskStatus(req2)
            status = resp2.Data.Status
            if status == 2:  # 成功
                result_str = resp2.Data.Result
                if not result_str:
                    return {"language": "zh", "duration": dur_rounded,
                            "segments": [{"start": 0.0, "end": dur_rounded,
                                          "text": "(无识别结果)"}]}
                # 解析结果
                all_segments = []
                full_text = ""
                for line in result_str.strip().split("\n"):
                    # 格式: [start,end] text
                    if line.startswith("["):
                        parts = line.split("]", 1)
                        if len(parts) == 2:
                            text = parts[1].strip()
                            if text:
                                all_segments.append({
                                    "start": 0.0, "end": dur_rounded, "text": text,
                                })
                                full_text += text
                    else:
                        text = line.strip()
                        if text:
                            all_segments.append({
                                "start": 0.0, "end": dur_rounded, "text": text,
                            })
                            full_text += text

                if not all_segments and full_text.strip():
                    all_segments.append({
                        "start": 0.0, "end": dur_rounded, "text": full_text.strip()
                    })
                if not all_segments:
                    all_segments = [{"start": 0.0, "end": dur_rounded, "text": "(无识别结果)"}]

                return {"language": "zh", "duration": dur_rounded, "segments": all_segments}

            elif status == 3:  # 失败
                print(f"[腾讯ASR] 失败: {resp2.Data.ErrorMsg}")
                return {"language": "zh", "duration": dur_rounded,
                        "segments": [{"start": 0.0, "end": dur_rounded,
                                      "text": f"(转录失败: {resp2.Data.ErrorMsg[:200]})"}]}
        except Exception as e:
            print(f"[腾讯ASR] 轮询错误: {e}")
            continue

    return {"language": "zh", "duration": dur_rounded,
            "segments": [{"start": 0.0, "end": dur_rounded, "text": "(转录超时)"}]}

async def llm_process(transcript: dict) -> dict:
    full_text = " ".join(s["text"] for s in transcript["segments"])
    from openai import OpenAI

    if _LLM_PROVIDER == "deepseek":
        client = OpenAI(
            api_key=_DEEPSEEK_KEY,
            base_url="https://api.deepseek.com/v1"
        )
        llm_model = "deepseek-chat"
    elif _DASHSCOPE_KEY:
        client = OpenAI(
            api_key=_DASHSCOPE_KEY,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        llm_model = "qwen-turbo"
    else:
        transcript["llm_error"] = "未配置 API Key（需要 DASHSCOPE_API_KEY 或 DEEPSEEK_API_KEY）"
        return transcript

    async def correct():
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=llm_model,
            messages=[
                {"role": "system", "content": "你是一个语音转写文字校对专家。请纠正以下ASR转写文本中的错误（专有名词、同音字等），保持原意和口语风格不变。只返回纠正后的文本。"},
                {"role": "user", "content": full_text}
            ],
            temperature=0.1, timeout=120)
        return resp.choices[0].message.content
    async def summarize():
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=llm_model,
            messages=[
                {"role": "system", "content": "请用中文总结以下视频/播客内容的要点，分点列出，简洁明了。"},
                {"role": "user", "content": full_text}
            ],
            temperature=0.3, timeout=120)
        return resp.choices[0].message.content
    transcript["corrected_text"], transcript["summary"] = await asyncio.gather(correct(), summarize())
    return transcript

async def download_audio(url: str, output_dir: Path) -> Path:
    """通用视频音频下载
    策略：
    1. B站 → Camofox 专用提取
    2. YouTube → yt-dlp 下载
    3. 其他 → 提示不支持
    """
    url_lower = url.lower()

    # B站走专用策略（Camofox 提取音视频流）
    if "bilibili.com" in url_lower or "b23.tv" in url_lower:
        return await download_bilibili_via_camofox(url, output_dir)

    # YouTube 走 yt-dlp
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        try:
            return await download_youtube_via_ytdlp(url, output_dir)
        except Exception as e:
            raise HTTPException(400, f"YouTube 视频下载失败: {str(e)[:200]}")

    # 其他网站不予支持
    raise HTTPException(400,
        "暂不支持该平台在线转录。\n"
        "目前仅支持 B站 和 YouTube 的视频链接。\n"
        "如需转录其他平台视频，请先下载到本地，再使用「上传文件」功能。"
    )

async def ensure_camofox_ready(max_wait: int = 90) -> None:
    """检查 Camofox 健康状态，浏览器未运行时触发启动"""
    import aiohttp
    health_url = "http://localhost:9377/health"

    for attempt in range(max_wait // 5):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("browserRunning") and data.get("browserConnected"):
                            return  # 一切正常
        except Exception:
            pass

        # 浏览器未运行 — 创建一个首页标签页触发 ensureBrowser()
        try:
            async with aiohttp.ClientSession() as session:
                await session.post("http://localhost:9377/tabs",
                    json={"userId": "healthcheck", "sessionKey": "ping", "url": "about:blank"},
                    timeout=aiohttp.ClientTimeout(total=15))
        except Exception:
            pass

        await asyncio.sleep(5)

    raise RuntimeError(f"Camofox 浏览器在 {max_wait}s 内未能就绪")

async def download_bilibili_via_camofox(url: str, output_dir: Path) -> Path:
    """通过 Camofox 浏览器下载 B站音频"""
    import aiohttp

    camofox_base = "http://localhost:9377"
    user_id = "transcriber"
    tab_id = None

    # 确保浏览器已就绪
    await ensure_camofox_ready()

    async def _camofox_post(path: str, data: dict = None) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{camofox_base}{path}",
                                    json=data or {},
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Camofox API {path} 返回 {resp.status}: {text[:200]}")
                return await resp.json()

    async def _camofox_get(path: str) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{camofox_base}{path}",
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Camofox API {path} 返回 {resp.status}: {text[:200]}")
                return await resp.json()

    try:
        # 1. 创建标签页（先打开B站首页建立 session）
        tab_data = await _camofox_post("/tabs", {
            "userId": user_id,
            "sessionKey": f"bili_{uuid.uuid4().hex[:8]}",
            "url": "https://www.bilibili.com/"
        })
        tab_id = tab_data.get("tabId")
        if not tab_id:
            raise RuntimeError("Camofox 创建标签页失败")

        # 2. 等首页加载
        await asyncio.sleep(3)

        # 3. 导航到视频页
        await _camofox_post(f"/tabs/{tab_id}/navigate", {
            "userId": user_id,
            "url": url
        })

        # 4. 等待视频页面完全加载
        await asyncio.sleep(5)

        # 4. 执行 JS 提取播放信息
        js_code = """
        (function() {
            try {
                var scripts = document.querySelectorAll('script');
                for (var i = 0; i < scripts.length; i++) {
                    var t = scripts[i].textContent || '';
                    if (t.indexOf('__playinfo__') > -1) {
                        var start = t.indexOf('{');
                        var end = t.lastIndexOf('}');
                        if (start > -1 && end > start) {
                            var json = JSON.parse(t.substring(start, end + 1));
                            var audio = json && json.data && json.data.dash && json.data.dash.audio;
                            if (audio && audio.length > 0) {
                                var best = audio[0];
                                return JSON.stringify({
                                    audioUrl: best.baseUrl || '',
                                    backupUrl: (best.backupUrl && best.backupUrl[0]) || '',
                                    duration: json.data.dash.duration || 0,
                                    title: document.title || ''
                                });
                            }
                        }
                    }
                }
                return 'no_playinfo';
            } catch(e) {
                return 'error:' + e.message;
            }
        })()
        """

        exec_result = await _camofox_post(f"/tabs/{tab_id}/evaluate", {
            "userId": user_id,
            "expression": js_code
        })

        result_text = exec_result.get("result", "")

        # 第一次不成功则尝试直接提取页面 HTML 中的 playinfo
        if result_text in ("no_playinfo", "") or result_text.startswith("error:"):
            await asyncio.sleep(3)
            exec_result = await _camofox_post(f"/tabs/{tab_id}/evaluate", {
                "userId": user_id,
                "expression": js_code
            })
            result_text = exec_result.get("result", "")

        if not result_text or result_text.startswith("error:") or result_text == "no_playinfo":
            raise RuntimeError(f"无法从B站页面提取音频信息: {result_text[:200]}")

        playinfo = json.loads(result_text)
        audio_url = playinfo.get("audioUrl") or playinfo.get("backupUrl")
        if not audio_url:
            raise RuntimeError("未找到音频流地址")

        title = playinfo.get("title", "bilibili_video")[:50]
        # 清理文件名中的非法字符
        import re
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
        output_path = output_dir / f"{safe_title}.m4a"

        # 6. 用 curl 下载音频流（带浏览器请求头）
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-L", "-o", str(output_path),
            "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "-H", "Referer: https://www.bilibili.com/",
            "--connect-timeout", "15",
            "--max-time", "120",
            audio_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            # 尝试备用 URL
            backup_url = playinfo.get("backupUrl")
            if backup_url and backup_url != audio_url:
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-s", "-L", "-o", str(output_path),
                    "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    "-H", "Referer: https://www.bilibili.com/",
                    "--connect-timeout", "15",
                    "--max-time", "120",
                    backup_url,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await proc.communicate()

            if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
                err_msg = stderr.decode()[:200] if stderr else "下载文件为空"
                raise RuntimeError(f"音频流下载失败: {err_msg}")

        file_size = output_path.stat().st_size
        if file_size < 1000:
            raise RuntimeError(f"音频文件过小 ({file_size} 字节)，可能下载不完整")

        # 转成 mp3（统一格式）
        mp3_path = output_dir / f"{safe_title}.mp3"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(output_path),
            "-vn", "-acodec", "libmp3lame", "-ab", "128k",
            str(mp3_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

        # 清理临时 m4a
        if output_path.exists():
            output_path.unlink()

        if mp3_path.exists() and mp3_path.stat().st_size > 1000:
            return mp3_path
        raise RuntimeError("音频转码失败")

    finally:
        # 7. 清理标签页
        if tab_id:
            try:
                async with aiohttp.ClientSession() as session:
                    await session.delete(f"{camofox_base}/tabs/{tab_id}?userId={user_id}",
                                         timeout=aiohttp.ClientTimeout(total=5))
            except Exception:
                pass


# ============================================================
# Camofox 通用视频提取（支持抖音/快手/小红书/微博等）
# ============================================================
async def download_via_camofox_generic(url: str, output_dir: Path) -> Path:
    """通用方案：用 Camofox 浏览器打开任意视频页面，提取视频源下载"""
    import aiohttp
    import re as _re

    camofox_base = "http://localhost:9377"
    user_id = "transcriber"
    tab_id = None

    async def _camofox_post(path: str, data: dict = None) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{camofox_base}{path}",
                                    json=data or {},
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Camofox API {path} 返回 {resp.status}: {text[:200]}")
                return await resp.json()

    try:
        # 1. 直接打开目标 URL（不先打开域名首页，以支持 xhslink/b23 等短链接）
        tab_data = await _camofox_post("/tabs", {
            "userId": user_id,
            "sessionKey": f"gen_{uuid.uuid4().hex[:8]}",
            "url": url
        })
        tab_id = tab_data.get("tabId")
        if not tab_id:
            raise RuntimeError("Camofox 创建标签页失败")

        # 2. 等待页面加载（动态页面需要更久）
        await asyncio.sleep(8)

        # 3. 检测是否为 blob URL，若是则尝试从 __INITIAL_STATE__ 提取真实地址
        detect_js = """
        (function() {
            var v = document.querySelector('video');
            var r = { hasVideo: false, isBlob: false, src: '', title: document.title || '' };

            if (v) {
                r.hasVideo = true;
                r.src = v.src || '';
                r.currentSrc = v.currentSrc || '';
                r.isBlob = (v.src && v.src.indexOf('blob:') === 0) || (v.currentSrc && v.currentSrc.indexOf('blob:') === 0);

                // 有 video 标签但 src 为空 → 找 source 子元素
                if (!r.src && !r.currentSrc) {
                    var sources = v.querySelectorAll('source');
                    if (sources.length > 0) {
                        r.src = sources[0].src || '';
                    }
                }
            }
            return JSON.stringify(r);
        })()
        """
        exec_result = await _camofox_post(f"/tabs/{tab_id}/evaluate", {
            "userId": user_id,
            "expression": detect_js
        })
        result_text = exec_result.get("result", "")
        if not result_text or result_text == "undefined":
            raise RuntimeError("无法从页面提取视频信息（页面可能未加载完成）")

        import json as _json
        page_info = _json.loads(result_text)
        title = page_info.get("title", "video")[:50]
        video_url = page_info.get("currentSrc") or page_info.get("src") or ""

        # 如果视频源是 blob，尝试从 __INITIAL_STATE__ 提取真实 URL
        if page_info.get("isBlob") or (video_url and video_url.startswith("blob:")):
            extract_real_js = r"""
            (function() {
                var r = {};

                // 策略1: 找 __INITIAL_STATE__ 原始文本并用正则提取 video URL
                var scripts = document.querySelectorAll('script');
                for (var i = 0; i < scripts.length; i++) {
                    var t = scripts[i].textContent || '';
                    if (t.indexOf('__INITIAL_STATE__') > -1) {
                        // 用正则从原始 JSON 文本中提取 masterUrl / url
                        var masterMatch = t.match(/"masterUrl"\s*:\s*"([^"]+)"/);
                        if (masterMatch && masterMatch[1]) {
                            r.realUrl = masterMatch[1].replace(/\\u0026/g, '&');
                            r.source = '__INITIAL_STATE__.regex_masterUrl';
                            return JSON.stringify(r);
                        }
                        var urlMatch = t.match(/"url"\s*:\s*"((https?:|)[^"]+\.(mp4|m3u8)[^"]*)"/);
                        if (urlMatch && urlMatch[1]) {
                            r.realUrl = urlMatch[1].replace(/\\u0026/g, '&');
                            r.source = '__INITIAL_STATE__.regex_url';
                            return JSON.stringify(r);
                        }
                    }
                }

                // 策略2: 在页面 HTML 中搜索视频 URL
                var html = document.documentElement.outerHTML || '';
                var anyUrl = html.match(/https?:\\/\\/[^\\s\\"'<>]+?\\.(mp4|m3u8|webm)(\\?[^\\s\\"'<>]*)?/i);
                if (anyUrl) {
                    r.realUrl = anyUrl[0];
                    r.source = 'html_regex';
                    return JSON.stringify(r);
                }

                r.realUrl = '';
                return JSON.stringify(r);
            })()
            """

            exec_result2 = await _camofox_post(f"/tabs/{tab_id}/evaluate", {
                "userId": user_id,
                "expression": extract_real_js
            })
            rt2 = exec_result2.get("result", "")
            if rt2 and rt2 != "undefined":
                real_info = _json.loads(rt2)
                if real_info.get("realUrl"):
                    video_url = real_info["realUrl"]
                    print(f"[Camofox] Extracted real URL from {real_info.get('source', 'unknown')}")

        # 如果依然没有视频 URL，回退到在 HTML 中搜索
        if not video_url:
            html_search_js = """
            (function() {
                var html = document.documentElement.outerHTML || '';
                var urls = [];
                var patterns = [
                    /https?:\\/\\/[^\\s\\"'<>]+?\\.(mp4|m3u8|webm|ts)(\\?[^\\s\\"'<>]*)?/gi,
                    /https?:\\/\\/[^\\s\\"'<>]+?video[^\\s\\"'<>]*?\\.(mp4|m3u8)/gi,
                    /https?:\\/\\/[-a-zA-Z0-9@:%._\\+~#=]{1,256}\\.(com|cn)[^\\s\\"'<>]*?\\.(mp4|m3u8)/gi
                ];
                for (var pi = 0; pi < patterns.length; pi++) {
                    var matches = html.match(patterns[pi]);
                    if (matches) {
                        for (var mi = 0; mi < matches.length; mi++) {
                            if (matches[mi].indexOf('blob:') !== 0 && matches[mi].indexOf('data:') !== 0) {
                                urls.push(matches[mi]);
                            }
                        }
                    }
                }
                return JSON.stringify(urls.slice(0, 5));
            })()
            """
            exec_result3 = await _camofox_post(f"/tabs/{tab_id}/evaluate", {
                "userId": user_id,
                "expression": html_search_js
            })
            rt3 = exec_result3.get("result", "")
            if rt3 and rt3 != "undefined":
                found_urls = _json.loads(rt3)
                if found_urls:
                    video_url = found_urls[0]

        if not video_url:
            raise RuntimeError(
                "未在页面中找到可下载的视频源。\n"
                f"页面标题: {page_info.get('title', '未知')}\n"
                f"视频元素: {'有' if page_info.get('hasVideo') else '无'}\n"
                "说明: 此网站的视频使用动态blob加密加载（小红书等平台），\n"
                "      无法直接提取视频地址下载。建议尝试B站、YouTube等\n"
                "      直接提供视频URL的平台。"
            )

        # 安全文件名
        safe_title = _re.sub(r'[\\/*?:"<>|]', "_", title) or "video"
        safe_title = safe_title[:80]

        # 5. 用 curl 下载
        output_path = output_dir / f"{safe_title}.mp4"

        # 判断是音频还是视频（音频链接不转码，视频需提取音频）
        is_audio = any(video_url.lower().endswith(ext) for ext in ['.m4a', '.mp3', '.aac', '.wav', '.ogg'])

        # 获取域名用于 Referer
        domain = url.split("/")[2] if "://" in url else url.split("/")[0]

        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-L", "-o", str(output_path),
            "-H", f"Referer: https://{domain}/",
            "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "--connect-timeout", "15",
            "--max-time", "180",
            video_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 1000:
            err_msg = stderr.decode()[:200] if stderr else "文件过小"
            # 清理可能存在的空文件
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"视频流下载失败: {err_msg}")

        file_size = output_path.stat().st_size

        if is_audio:
            # 已经是音频，确保 mp3 格式
            mp3_path = output_dir / f"{safe_title}.mp3"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(output_path),
                "-vn", "-acodec", "libmp3lame", "-ab", "128k",
                str(mp3_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            if output_path.exists():
                output_path.unlink()
            if mp3_path.exists() and mp3_path.stat().st_size > 1000:
                return mp3_path
            raise RuntimeError("音频转码失败")
        else:
            # 视频文件，提取音频轨道
            mp3_path = output_dir / f"{safe_title}.mp3"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(output_path),
                "-vn", "-acodec", "libmp3lame", "-ab", "128k",
                str(mp3_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            # 清理原始视频文件
            if output_path.exists():
                output_path.unlink()
            if mp3_path.exists() and mp3_path.stat().st_size > 1000:
                return mp3_path
            raise RuntimeError("视频转音频失败")

    finally:
        if tab_id:
            try:
                async with aiohttp.ClientSession() as session:
                    await session.delete(f"{camofox_base}/tabs/{tab_id}?userId={user_id}",
                                         timeout=aiohttp.ClientTimeout(total=5))
            except Exception:
                pass


async def download_youtube_via_ytdlp(url: str, output_dir: Path) -> Path:
    """用 yt-dlp 下载 YouTube 音频"""
    output_template = str(output_dir / "%(title)s.%(ext)s")
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "-o", output_template, "--no-playlist", "--print", "filename",
        url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err_msg = stderr.decode()[:500]
        raise HTTPException(400, f"下载失败: {err_msg}")
    return Path(stdout.decode().strip().split("\n")[-1])

def segments_to_srt(segments: list) -> str:
    def _fmt(sec):
        h, m, s = int(sec // 3600), int((sec % 3600) // 60), sec % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(f"{i}")
        lines.append(f"{_fmt(seg['start'])} --> {_fmt(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)

# ============================================================
# 后台任务
# ============================================================
bg_tasks_set = set()

async def run_file_task(task_id: str, audio_path: Path, use_llm: bool, username: str = None):
    try:
        tasks[task_id].update({"status": "transcribing", "progress": 30, "message": "🎙️ 正在语音转文字..."})
        save_tasks()
        transcript = await transcribe_audio(audio_path)
        result_file = RESULTS_DIR / f"{task_id}.json"
        result_file.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))

        if use_llm:
            tasks[task_id].update({"status": "processing", "progress": 70, "message": "🤖 正在AI纠错和总结..."})
            save_tasks()
            try:
                transcript = await llm_process(transcript)
                result_file.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
            except Exception as llm_err:
                transcript["llm_error"] = str(llm_err)
                result_file.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
                tasks[task_id].update({"message": f"转录完成，AI处理失败: {str(llm_err)[:100]}"})

        tasks[task_id].update({"status": "completed", "progress": 100, "message": "✅ 完成", "result": transcript})

        # 扣除用户额度
        if username and not tasks[task_id].get("api_key_name"):
            duration = transcript.get("duration", 0)
            if duration > 0:
                deduct_quota(username, duration)
                tasks[task_id]["quota_deducted"] = round(duration, 1)
    except Exception as e:
        tasks[task_id].update({"status": "failed", "message": str(e), "error": str(e)})
    finally:
        save_tasks()
        bg_tasks_set.discard(task_id)
        if audio_path.exists():
            audio_path.unlink()

async def run_url_task(task_id: str, url: str, use_llm: bool, username: str = None):
    download_dir = UPLOAD_DIR / task_id
    download_dir.mkdir(exist_ok=True)
    try:
        tasks[task_id].update({"status": "downloading", "progress": 15, "message": "📥 正在下载视频..."})
        save_tasks()
        audio_path = await download_audio(url, download_dir)

        tasks[task_id].update({"status": "transcribing", "progress": 40, "message": "🎙️ 正在语音转文字..."})
        save_tasks()
        transcript = await transcribe_audio(audio_path)

        result_file = RESULTS_DIR / f"{task_id}.json"
        result_file.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))

        if use_llm:
            tasks[task_id].update({"status": "processing", "progress": 75, "message": "🤖 正在AI纠错和总结..."})
            save_tasks()
            try:
                transcript = await llm_process(transcript)
                result_file.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
            except Exception as llm_err:
                transcript["llm_error"] = str(llm_err)
                result_file.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
                tasks[task_id].update({"message": f"转录完成，AI处理失败: {str(llm_err)[:100]}"})

        # 提取视频标题
        title = audio_path.stem if audio_path else url
        tasks[task_id].update({"status": "completed", "progress": 100, "message": "✅ 完成", "result": transcript, "title": title})

        # 扣除用户额度
        if username and not tasks[task_id].get("api_key_name"):
            duration = transcript.get("duration", 0)
            if duration > 0:
                deduct_quota(username, duration)
                tasks[task_id]["quota_deducted"] = round(duration, 1)
    except Exception as e:
        tasks[task_id].update({"status": "failed", "message": str(e), "error": str(e)})
    finally:
        save_tasks()
        bg_tasks_set.discard(task_id)
        if download_dir.exists():
            shutil.rmtree(download_dir, ignore_errors=True)

# ============================================================
# API 路由
# ============================================================

# ============ 用户认证 ============
@app.post("/api/auth/register")
async def register(data: dict):
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    if not username or len(username) < 2:
        raise HTTPException(400, "用户名至少2个字符")
    if len(password) < 4:
        raise HTTPException(400, "密码至少4个字符")
    conn = sqlite3.connect(str(USERS_DB))
    try:
        conn.execute("INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                     (username, hash_password(password), time.time()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(409, "用户名已存在")
    conn.close()
    token = create_token(username)
    return {"username": username, "token": token}

@app.post("/api/auth/login")
async def login(data: dict):
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    conn = sqlite3.connect(str(USERS_DB))
    row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row or not verify_password(password, row[0]):
        raise HTTPException(401, "用户名或密码错误")
    token = create_token(username)
    return {"username": username, "token": token}

# ============ 微信一键登录 ============
@app.post("/api/auth/wx-login")
async def wx_login(data: dict):
    """微信登录：code → jscode2session → openid → JWT"""
    import httpx

    code = (data.get("code") or "").strip()
    if not code or len(code) < 4:
        raise HTTPException(400, "无效的微信临时凭证")

    if not _WX_APPID or not _WX_SECRET:
        raise HTTPException(501, "微信登录功能未配置（请联系管理员设置 WX_APPID 和 WX_SECRET）")

    # 调微信 jscode2session 接口
    wx_url = "https://api.weixin.qq.com/sns/jscode2session"
    params = {
        "appid": _WX_APPID,
        "secret": _WX_SECRET,
        "js_code": code,
        "grant_type": "authorization_code",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(wx_url, params=params)
            wx_data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"调用微信接口失败: {str(e)[:100]}")

    openid = wx_data.get("openid")
    if not openid:
        errmsg = wx_data.get("errmsg", "未知错误")
        raise HTTPException(401, f"微信登录失败: {errmsg}")

    # session_key 敏感，仅日志记录且不返回前端
    session_key = wx_data.get("session_key", "")
    if session_key:
        print(f"[wx-login] 用户 {openid[:8]}... 登录成功")

    # 用 openid 作为唯一标识（前缀 wx_ 避免冲突）
    wx_username = f"wx_{openid}"

    # 查找或创建用户
    conn = sqlite3.connect(str(USERS_DB))
    row = conn.execute("SELECT username FROM users WHERE username = ?", (wx_username,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (wx_username, "wx_only", time.time())
        )
        conn.commit()
        print(f"[wx-login] 新用户注册: {wx_username[:16]}...")
    conn.close()

    # 生成 JWT token
    token = create_token(wx_username)

    return {"token": token, "openid": openid, "nickname": "微信用户", "avatar": ""}

@app.get("/api/auth/me")
async def me(username: str = Depends(get_current_user)):
    return {"username": username}

# ============================================================
# 额度 & 签到 API
# ============================================================
@app.get("/api/quota")
async def api_quota(username: str = Depends(get_current_user)):
    """获取用户当月额度信息"""
    quota = get_user_quota(username)
    quota["username"] = username
    quota["free_minutes"] = MONTHLY_FREE_MINUTES
    quota["checkin_bonus_minutes"] = CHECKIN_BONUS_MINUTES
    quota["available_minutes"] = round(quota["available_seconds"] / 60, 1)
    quota["used_minutes"] = round(quota["used_seconds"] / 60, 1)
    return quota

@app.post("/api/checkin")
async def api_checkin(username: str = Depends(get_current_user)):
    """每日签到"""
    try:
        return do_checkin(username)
    except Exception as e:
        raise HTTPException(500, detail=str(e)[:200])

@app.get("/api/quota/pricing")
async def api_pricing():
    """获取价格套餐"""
    return {
        "monthly_free_minutes": MONTHLY_FREE_MINUTES,
        "checkin_bonus_minutes": CHECKIN_BONUS_MINUTES,
        "packages": [
            {"name": "60分钟包", "minutes": 60, "price_yuan": 6, "price_cents": 600},
            {"name": "300分钟包", "minutes": 300, "price_yuan": 25, "price_cents": 2500},
            {"name": "无限月卡", "minutes": 99999, "price_yuan": 49, "price_cents": 4900},
        ],
        "currency": "CNY",
    }

# ============================================================
# 支付 API
# ============================================================
@app.post("/api/payment/create-order")
async def api_create_order(data: dict, username: str = Depends(get_current_user)):
    """创建支付订单"""
    package_minutes = int(data.get("minutes", 60))
    # 查找匹配的套餐
    pricing = [
        {"name": "60分钟包", "minutes": 60, "price_cents": 600},
        {"name": "300分钟包", "minutes": 300, "price_cents": 2500},
        {"name": "无限月卡", "minutes": 99999, "price_cents": 4900},
    ]
    pkg = None
    for p in pricing:
        if p["minutes"] == package_minutes:
            pkg = p
            break
    if not pkg:
        # 自定义套餐：1元=10分钟
        package_minutes = max(10, package_minutes)
        amount_cents = package_minutes * 10  # 1元=10分钟 → 1分钟=10分钱=10cents
        pkg = {"name": f"{package_minutes}分钟", "minutes": package_minutes, "price_cents": amount_cents}
    return create_payment_order(username, pkg["name"], pkg["price_cents"], pkg["minutes"] * 60.0)

@app.get("/api/payment/order/{order_id}")
async def api_order_status(order_id: str, username: str = Depends(get_current_user)):
    """查询订单状态"""
    conn = sqlite3.connect(str(USERS_DB))
    row = conn.execute(
        "SELECT id, package_name, amount_cents, extra_seconds, status, stripe_session_id, created_at, paid_at FROM payment_orders WHERE id=? AND username=?",
        (order_id, username)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "订单不存在")
    return {
        "order_id": row[0],
        "package_name": row[1],
        "amount_yuan": round(row[2] / 100, 2),
        "extra_minutes": round(row[3] / 60, 1),
        "status": row[4],
        "stripe_session_id": row[5],
        "created_at": row[6],
        "paid_at": row[7],
    }

@app.get("/api/payment/history")
async def api_payment_history(username: str = Depends(get_current_user)):
    """支付历史"""
    return get_user_payment_history(username)

@app.post("/api/payment/webhook")
async def api_stripe_webhook(request: Request):
    """Stripe Webhook: 支付成功回调"""
    import stripe
    stripe_key = os.environ.get("STRIPE_API_KEY", "")
    if not stripe_key:
        raise HTTPException(400, "未配置 Stripe")
    stripe.api_key = stripe_key
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            # 无签名验证（测试环境安全降级）
            event = json.loads(payload)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise HTTPException(400, f"Webhook 验证失败: {e}")

    if event.get("type") == "checkout.session.completed":
        session = event["data"]["object"]
        order_id = session.get("metadata", {}).get("order_id")
        if order_id:
            confirm_payment_order(order_id)
            print(f"[Payment] Order {order_id} confirmed via webhook")
    return {"status": "ok"}

@app.post("/api/payment/confirm/{order_id}")
async def api_manual_confirm(order_id: str, username: str = Depends(get_current_user)):
    """手动确认订单（仅管理员可用）"""
    conn = sqlite3.connect(str(USERS_DB))
    row = conn.execute(
        "SELECT is_admin FROM users WHERE username=?", (username,)
    ).fetchone()
    if not row or not row[0]:
        raise HTTPException(403, "仅管理员可手动确认订单，请使用在线支付")
    conn.close()
    if not confirm_payment_order(order_id):
        raise HTTPException(400, "订单不存在或已处理")
    return {"success": True, "message": f"管理员已确认订单"}

# ============================================================
# 虎皮椒微信支付回调
# ============================================================
@app.post("/api/payment/xunhupay-notify")
async def xunhupay_notify(request: Request):
    """虎皮椒支付异步通知"""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from xunhupay import verify_notify

    form_data = await request.form()
    data = dict(form_data)

    if not verify_notify(data):
        return "fail"

    trade_order_id = data.get("trade_order_id", "")
    status = data.get("status", "")
    if status == "OD" and trade_order_id:  # OD = 已支付
        confirm_payment_order(trade_order_id)
        print(f"[Payment] 虎皮椒回调确认: {trade_order_id}")

    return "success"  # 虎皮椒要求返回纯文本 success

# ============================================================
# 额度校验中间件（转录前检查）
# ============================================================
async def require_quota(username: str):
    """转录开始前检查用户是否有余量"""
    quota = get_user_quota(username)
    if quota["available_seconds"] <= 0:
        raise HTTPException(402,
            "本月免费额度已用尽，请签到获取额外额度，或购买套餐。"
        )
    return True

# 临时音频文件服务（供百炼 ASR 下载）
@app.get("/temp_audio/{path:path}")
async def temp_audio(path: str):
    from fastapi.responses import FileResponse
    # 防止路径穿越
    clean_path = path.replace("..", "").lstrip("/")
    file_path = UPLOAD_DIR / clean_path
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path), media_type="audio/wav")
    # 也查 results 目录
    file_path = RESULTS_DIR / clean_path
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path), media_type="audio/wav")
    raise HTTPException(404, "音频文件不存在或已过期")

@app.get("/")
async def root():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>视频转录服务</h1><p>前端页面未就绪，请等待部署完成。</p>")

@app.get("/api/status")
async def status():
    return {
        "service": "视频转录服务", "version": "1.0.0",
        "tasks_total": len(tasks),
        "tasks_pending": sum(1 for t in tasks.values() if t["status"] not in ("completed", "failed"))
    }

@app.post("/api/transcribe/upload")
async def transcribe_upload(
    request: Request,
    file: UploadFile = File(...),
    use_llm: bool = Form(True),
    user: str = Depends(get_current_user)
):
    """上传文件转录"""
    key_name = verify_key(request)
    if key_name is None and len(API_KEYS) > 1:
        raise HTTPException(401, "需要有效的 API Key (通过 X-API-Key 请求头或 ?key= 参数)")

    # 检查额度（仅对普通用户）
    if not key_name:
        await require_quota(user)

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, f"文件过大，最大支持 {MAX_FILE_SIZE // 1024 // 1024}MB")

    task_id = uuid.uuid4().hex[:12]
    ext = Path(file.filename).suffix or ".mp3"
    save_path = UPLOAD_DIR / f"{task_id}{ext}"
    save_path.write_bytes(content)

    tasks[task_id] = {
        "id": task_id, "url": f"upload://{file.filename}",
        "status": "queued", "progress": 0, "message": "⏳ 排队中",
        "created_at": time.time(), "result": None, "error": None,
        "mode": "upload", "use_llm": use_llm, "user": user, "api_key_name": key_name
    }
    save_tasks()

    t = asyncio.create_task(run_file_task(task_id, save_path, use_llm, user))
    bg_tasks_set.add(task_id)
    return {"task_id": task_id, "status": "queued", "filename": file.filename, "file_size": len(content)}

@app.post("/api/transcribe/url")
async def transcribe_url(
    request: Request,
    data: dict,
    user: str = Depends(get_current_user)
):
    """通过URL下载并转录（支持YouTube/B站）"""
    key_name = verify_key(request)
    if key_name is None and len(API_KEYS) > 1:
        raise HTTPException(401, "需要有效的 API Key")

    # 检查额度（仅对普通用户）
    if not key_name:
        await require_quota(user)

    url = data.get("url", "").strip()
    # 从包含标题的粘贴文本中提取实际 URL
    import re as _re
    url_match = _re.search(r'https?://[^\s]+', url)
    if url_match:
        url = url_match.group(0)
    use_llm = data.get("use_llm", True)
    if not url:
        raise HTTPException(400, "请提供视频URL")

    # 解析平台
    platform = "other"
    url_lower = url.lower()
    if "bilibili.com" in url_lower or "b23.tv" in url_lower:
        platform = "bilibili"
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        platform = "youtube"

    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {
        "id": task_id, "url": url, "platform": platform,
        "status": "queued", "progress": 0, "message": "⏳ 排队中",
        "created_at": time.time(), "result": None, "error": None,
        "mode": "url", "use_llm": use_llm, "user": user, "api_key_name": key_name
    }
    save_tasks()

    t = asyncio.create_task(run_url_task(task_id, url, use_llm, user))
    bg_tasks_set.add(task_id)
    return {"task_id": task_id, "status": "queued", "url": url, "platform": platform}

@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    t = tasks.get(task_id)
    resp = None

    if t:
        resp = {
            "task_id": t.get("id", task_id),
            "status": t["status"],
            "progress": t["progress"],
            "message": t.get("message", ""),
            "error": t.get("error"),
            "mode": t.get("mode", "upload"),
            "title": t.get("title"),
            "result_url": f"/view/{task_id}" if t["status"] == "completed" else None
        }
        # 优先从内存取 result
        if t.get("result"):
            resp["result"] = t["result"]
        else:
            # 内存无 result → 尝试从文件恢复
            rfile = RESULTS_DIR / f"{task_id}.json"
            if rfile.exists():
                try:
                    resp["result"] = json.loads(rfile.read_text())
                    resp["status"] = "completed"
                    resp["progress"] = 100
                    resp["message"] = "✅ 完成"
                except Exception:
                    pass
    else:
        # 任务不在内存中 → 尝试从文件恢复
        rfile = RESULTS_DIR / f"{task_id}.json"
        if rfile.exists():
            try:
                result = json.loads(rfile.read_text())
                resp = {
                    "task_id": task_id,
                    "status": "completed",
                    "progress": 100,
                    "message": "✅ 完成",
                    "result_url": f"/view/{task_id}",
                    "result": result
                }
            except Exception:
                pass

    if not resp:
        raise HTTPException(404, "任务不存在")

    return resp

@app.get("/api/tasks")
async def list_tasks(limit: int = 30, username: str = Depends(get_current_user)):
    """获取当前用户的转录历史"""
    user_tasks = [t for t in tasks.values() if t.get("user") == username]
    sorted_tasks = sorted(user_tasks, key=lambda t: t.get("created_at", 0), reverse=True)[:limit]
    return [{
        "task_id": t.get("id", "?"), "status": t["status"],
        "url": t.get("url", "?"), "progress": t["progress"],
        "message": t.get("message", ""),
        "created_at": t.get("created_at", 0),
        "mode": t.get("mode", "upload"),
        "title": t.get("title")
    } for t in sorted_tasks]

@app.get("/api/export/{task_id}")
async def export_task(task_id: str, fmt: str = "txt"):
    rfile = RESULTS_DIR / f"{task_id}.json"
    if not rfile.exists():
        raise HTTPException(404, "结果不存在或未完成")
    result = json.loads(rfile.read_text())

    if fmt == "json":
        return JSONResponse(result)
    elif fmt == "srt":
        srt_content = segments_to_srt(result.get("segments", []))
        content_type = "text/plain; charset=utf-8"
    else:  # txt
        lines = []
        for s in result.get("segments", []):
            m, s_sec = divmod(int(s["start"]), 60)
            lines.append(f"[{m:02d}:{s_sec:02d}] {s['text']}")
        if result.get("corrected_text"):
            lines.append(f"\n{'='*40}\nAI 纠错\n{'='*40}\n{result['corrected_text']}")
        if result.get("summary"):
            lines.append(f"\n{'='*40}\n内容总结\n{'='*40}\n{result['summary']}")
        srt_content = "\n".join(lines)

    return HTMLResponse(
        f"<pre style='font-family:monospace;white-space:pre-wrap;max-width:900px;margin:20px auto;background:#f8f8f8;padding:20px;border-radius:8px;'>{srt_content}</pre>",
        headers={"Content-Type": "text/plain; charset=utf-8"}
    )

# ============================================================
# 结果查看页面（美化版）
# ============================================================
@app.get("/view/{task_id}")
async def view_result(task_id: str):
    t = tasks.get(task_id)
    result = None
    rfile = RESULTS_DIR / f"{task_id}.json"
    if rfile.exists():
        result = json.loads(rfile.read_text())

    if not t and not result:
        return HTMLResponse("<h1>任务不存在</h1>", status_code=404)

    status = t["status"] if t else "completed"

    if status in ("queued", "downloading", "transcribing", "processing"):
        msg_map = {
            "queued": "⏳ 排队中",
            "downloading": "📥 正在下载...",
            "transcribing": "🎙️ 正在转录...",
            "processing": "🤖 AI 处理中..."
        }
        body = f"""
        <div class="status-card">
            <h2>{msg_map.get(status, status)}</h2>
            <div class="progress-bar"><div class="progress-fill" style="width:{t.get('progress', 0)}%"></div></div>
            <p class="status-msg">{t.get('message', '')}</p>
        </div>
        <script>setTimeout(()=>location.reload(),3000)</script>
        """
    elif status == "failed":
        body = f"""
        <div class="status-card error">
            <h2>❌ 转录失败</h2>
            <p class="error-msg">{t.get('error', '未知错误')}</p>
        </div>
        """
    elif status == "completed" and result:
        segs = result.get("segments", [])
        seg_html = ""
        for s in segs[:100]:
            m, sec = divmod(int(s["start"]), 60)
            seg_html += f'<div class="segment"><span class="ts">{m:02d}:{sec:02d}</span><span class="txt">{s["text"]}</span></div>'
        if len(segs) > 100:
            seg_html += f'<p class="more">… 共 {len(segs)} 段，查看完整版请下载 TXT/SRT</p>'

        lang = result.get("language", "zh")
        duration = result.get("duration", 0)
        dm, ds = divmod(int(duration), 60)
        duration_str = f"{dm}:{ds:02d}"

        corrected = result.get("corrected_text", "")
        summary = result.get("summary", "")

        body = f"""
        <div class="result-header">
            <h2>✅ 转录完成</h2>
            <div class="meta">
                <span class="badge">🌐 {lang}</span>
                <span class="badge">⏱ {duration_str}</span>
                <span class="badge">📄 {len(segs)} 段</span>
            </div>
        </div>

        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('transcript')">📝 逐段文字</button>
            <button class="tab-btn" onclick="switchTab('corrected')">🔧 纠错版</button>
            <button class="tab-btn" onclick="switchTab('summary')">📋 AI 总结</button>
        </div>

        <div id="tab-transcript" class="tab-content active">
            <div class="segments-list">{seg_html}</div>
        </div>

        <div id="tab-corrected" class="tab-content">
            <div class="corrected-text">
                <pre>{corrected}</pre>
            </div>
        </div>

        <div id="tab-summary" class="tab-content">
            <div class="summary-text">
                <pre>{summary}</pre>
            </div>
        </div>

        <div class="export-bar">
            <h3>📥 导出</h3>
            <a href="/api/export/{task_id}?fmt=txt" class="btn">📄 TXT</a>
            <a href="/api/export/{task_id}?fmt=json" class="btn">📊 JSON</a>
            <a href="/api/export/{task_id}?fmt=srt" class="btn">🎬 SRT 字幕</a>
        </div>
        """
    else:
        body = f"<h2>状态: {status}</h2>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>转录结果 - 视频转录服务</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f0f0f;color:#e0e0e0;min-height:100vh;}}
.container{{max-width:900px;margin:0 auto;padding:24px 16px;}}
h2{{font-size:1.3rem;margin-bottom:12px;}}
.status-card{{background:#1a1a2e;border-radius:12px;padding:40px;text-align:center;margin-top:40px;}}
.status-card.error{{background:#2e1a1a;}}
.progress-bar{{height:8px;background:#333;border-radius:4px;overflow:hidden;margin:20px 0;}}
.progress-fill{{height:100%;background:#6c63ff;border-radius:4px;transition:width 0.5s;}}
.status-msg{{color:#aaa;}}
.error-msg{{color:#ff6b6b;}}
.result-header{{margin-bottom:20px;}}
.meta{{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;}}
.badge{{background:#1a1a2e;padding:4px 12px;border-radius:20px;font-size:.85rem;color:#ccc;}}
.tabs{{display:flex;gap:4px;margin-bottom:16px;flex-wrap:wrap;}}
.tab-btn{{background:#1a1a2e;color:#aaa;border:none;padding:10px 18px;border-radius:8px;cursor:pointer;font-size:.9rem;transition:all .2s;}}
.tab-btn.active{{background:#6c63ff;color:#fff;}}
.tab-btn:hover{{background:#333;}}
.tab-content{{display:none;}}
.tab-content.active{{display:block;}}
.segments-list{{max-height:600px;overflow-y:auto;}}
.segment{{display:flex;gap:10px;padding:8px 12px;border-radius:6px;margin-bottom:4px;background:#1a1a2e;}}
.segment:hover{{background:#222;}}
.ts{{color:#6c63ff;font-family:monospace;font-size:.85rem;white-space:nowrap;padding-top:1px;}}
.txt{{color:#d0d0d0;line-height:1.6;}}
.corrected-text pre,.summary-text pre{{background:#1a1a2e;padding:20px;border-radius:8px;line-height:1.8;white-space:pre-wrap;font-family:inherit;font-size:.95rem;}}
.export-bar{{background:#1a1a2e;border-radius:12px;padding:20px;margin-top:20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;}}
.export-bar h3{{margin:0;margin-right:8px;font-size:1rem;}}
.btn{{display:inline-block;background:#6c63ff;color:#fff;padding:8px 20px;border-radius:6px;text-decoration:none;font-size:.9rem;transition:background .2s;}}
.btn:hover{{background:#5a52e0;}}
.more{{color:#888;text-align:center;padding:12px;font-style:italic;}}
</style>
<script>
function switchTab(name){{
    document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    document.getElementById('tab-'+name).classList.add('active');
    document.querySelector(`.tab-btn[onclick="switchTab('${{name}}')"]`).classList.add('active');
}}
</script>
</head><body><div class="container">{body}</div></body></html>""")

# ============================================================
# 后台清理（定期删除旧文件）
# ============================================================
async def cleanup_orphaned():
    """启动时清理孤儿文件（下载目录/上传文件/结果文件）"""
    now = time.time()
    active_task_ids = set(tasks.keys())

    # 清理 UPLOAD_DIR 中的孤儿子目录（URL下载残留）
    for item in UPLOAD_DIR.iterdir():
        if item.is_dir() and item.name not in active_task_ids:
            shutil.rmtree(item, ignore_errors=True)
        elif item.is_file() and item.stem not in active_task_ids:
            item.unlink(missing_ok=True)

    # 清理 RESULTS_DIR 中超过 24h 的孤儿文件
    for item in RESULTS_DIR.iterdir():
        if item.is_file() and item.stem not in active_task_ids:
            if item.stat().st_mtime < now - CLEANUP_HOURS * 3600:
                item.unlink(missing_ok=True)

async def cleanup_loop():
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        for task_id, t in list(tasks.items()):
            if t.get("created_at", 0) < now - CLEANUP_HOURS * 3600:
                del tasks[task_id]
                rfile = RESULTS_DIR / f"{task_id}.json"
                if rfile.exists():
                    rfile.unlink()
                # 同时清理 UPLOAD_DIR 中对应的子目录（URL下载）
                udir = UPLOAD_DIR / task_id
                if udir.exists() and udir.is_dir():
                    shutil.rmtree(udir, ignore_errors=True)
                # 清理 UPLOAD_DIR 中对应的文件（上传文件）
                for ext in ('.mp3', '.mp4', '.wav', '.m4a', '.aac', '.ogg', '.flac', '.wma', '.webm'):
                    ufile = UPLOAD_DIR / f"{task_id}{ext}"
                    if ufile.exists():
                        ufile.unlink(missing_ok=True)
        save_tasks()

@app.on_event("startup")
async def startup():
    asyncio.create_task(cleanup_orphaned())
    asyncio.create_task(cleanup_loop())

# ============================================================
# 管理 API
# ============================================================
@app.get("/admin/keys")
async def list_keys(key: str = Query(...)):
    if key != os.environ.get("ADMIN_KEY", "admin-secret"):
        raise HTTPException(403, "无权限")
    return [{"key": k, **v} for k, v in API_KEYS.items()]

@app.post("/admin/keys")
async def add_key(key: str = Query(...), name: str = Query(...)):
    if key != os.environ.get("ADMIN_KEY", "admin-secret"):
        raise HTTPException(403, "无权限")
    new_key = uuid.uuid4().hex[:16]
    API_KEYS[new_key] = {"name": name, "created_at": time.time(), "used": 0}
    save_api_keys()
    return {"key": new_key, "name": name}

# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9000)
