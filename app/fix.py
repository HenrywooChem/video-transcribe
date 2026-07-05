"""
Fix SQLite issues and ASR endpoint for transcription service.

1. SQLite: enable WAL mode, fix UNIQUE constraint race in do_checkin()
2. ASR: use workspace native API at /api/v1/services/asr/transcriptions
"""
import sqlite3, os, sys, json, asyncio, subprocess, urllib.parse, base64
from pathlib import Path

# --- Fix 1: Enable WAL mode ---
_USERS_DB = Path(__file__).parent / "data" / "users.db"
_UPLOAD_DIR = Path(__file__).parent / "data" / "uploads"

def enable_wal():
    conn = sqlite3.connect(str(_USERS_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.close()
    print(f"[OK] WAL mode enabled on {_USERS_DB}")

# --- Fix 2: Fix do_checkin() to avoid UNIQUE constraint race ---
# Instead of INSERT ... VALUES, use INSERT OR REPLACE or ON CONFLICT DO UPDATE

_NEW_CHECKIN_CODE = '''
def do_checkin(username: str) -> dict:
    """每日签到，奖励 5 分钟（修复并发 INSERT 冲突）"""
    conn = sqlite3.connect(str(USERS_DB))
    conn.execute("PRAGMA busy_timeout=5000")
    month = get_current_month()
    today = get_today()
    # 先查询
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
        # 使用 INSERT OR IGNORE + UPDATE 避免并发冲突
        conn.execute(
            "INSERT OR IGNORE INTO user_quota (username, month, bonus_seconds, last_checkin_date) VALUES (?, ?, ?, ?)",
            (username, month, new_bonus, today)
        )
        # 检查是否真的插入了（如果已存在则忽略）
        row2 = conn.execute(
            "SELECT bonus_seconds FROM user_quota WHERE username=? AND month=?",
            (username, month)
        ).fetchone()
        if row2 and row2[0] != new_bonus:
            # 没插入成功，说明有竞争，更新
            conn.execute(
                "UPDATE user_quota SET bonus_seconds=bonus_seconds+?, last_checkin_date=? WHERE username=? AND month=?",
                (CHECKIN_BONUS_MINUTES * 60, today, username, month)
            )
    conn.commit()
    conn.close()
    return {"success": True, "message": f"签到成功！+{CHECKIN_BONUS_MINUTES}分钟额度", "bonus_seconds": new_bonus}
'''

print(f"[TODO] Apply checkin fix to main.py")
print(f"[TODO] Test ASR with workspace API at /api/v1/services/asr/transcriptions")
