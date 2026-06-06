"""
session_store.py — تخزين جلسة Quotex في SQLite (يبقى بعد إعادة تشغيل Railway)
"""
import sqlite3
import os
import json
import time
import logging

logger = logging.getLogger("QuotexBot")

DB_PATH = os.getenv("SESSION_DB_PATH", os.path.join(os.getcwd(), "session_store.db"))

FIXED_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Samsung Galaxy S23) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.6261.105 Mobile Safari/537.36"
)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_session(email: str, token: str, user_agent: str = FIXED_USER_AGENT, cookies: str = "") -> None:
    """يحفظ بيانات الجلسة في SQLite — يستبدل القديم تلقائياً."""
    payload = json.dumps({
        "token":      token,
        "user_agent": user_agent,
        "cookies":    cookies or f"token={token}; lang=en",
    })
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (key, value, updated_at) VALUES (?, ?, ?)",
                (email, payload, time.time()),
            )
        logger.info("✅ session_store: جلسة محفوظة في SQLite")
    except Exception as e:
        logger.error(f"session_store save error: {e}")


def load_session(email: str) -> dict:
    """يقرأ بيانات الجلسة من SQLite. يعيد {} إن لم توجد."""
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM sessions WHERE key = ?", (email,)
            ).fetchone()
        if row:
            data = json.loads(row[0])
            age  = time.time() - row[1]
            logger.info(f"✅ session_store: جلسة محفوظة عمرها {int(age//60)} دقيقة")
            return data
    except Exception as e:
        logger.error(f"session_store load error: {e}")
    return {}


def session_to_file(email: str, path: str) -> bool:
    """يكتب الجلسة من SQLite إلى session.json (الشكل الذي يتوقّعه pyquotex)."""
    data = load_session(email)
    if not data:
        return False
    try:
        payload = {
            email: {
                "token":      data["token"],
                "cookies":    data.get("cookies", f"token={data['token']}; lang=en"),
                "user_agent": data.get("user_agent", FIXED_USER_AGENT),
            }
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=4)
        logger.info("✅ session_store: session.json محدَّث من SQLite")
        return True
    except Exception as e:
        logger.error(f"session_store to_file error: {e}")
        return False


def get_token(email: str) -> str:
    """اختصار: يعيد التوكن المحفوظ أو سلسلة فارغة."""
    return load_session(email).get("token", "")
