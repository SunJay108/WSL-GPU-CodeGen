import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import List, Optional

from config import DATA_DIR

DB_FILE = os.path.join(DATA_DIR, "chat_history.db")
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT,
    user_id INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT,
    role TEXT,
    content TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS moderation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    chat_id TEXT,
    stage TEXT NOT NULL,
    checker TEXT NOT NULL,
    verdict TEXT NOT NULL,
    detail TEXT,
    content TEXT,
    created_at TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_chats_user ON chats (user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_moderation_created ON moderation_log (created_at);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    """One connection per operation: safe to use from Gradio's worker threads."""
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()

# =========================================
# Schema & migration
# =========================================

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    existed = os.path.exists(DB_FILE) and os.path.getsize(DB_FILE) > 0

    with connect() as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]

    if existed and version < SCHEMA_VERSION:
        # The original app (schema v0) had no users; keep a copy before migrating.
        shutil.copy2(DB_FILE, f"{DB_FILE}.v{version}.bak")

    with connect() as c:
        c.executescript(SCHEMA)
        chat_columns = {row["name"] for row in c.execute("PRAGMA table_info(chats)")}
        if "user_id" not in chat_columns:
            c.execute("ALTER TABLE chats ADD COLUMN user_id INTEGER")
        if "updated_at" not in chat_columns:
            c.execute("ALTER TABLE chats ADD COLUMN updated_at TEXT")
            c.execute("""
                UPDATE chats SET updated_at = COALESCE(
                    (SELECT MAX(created_at) FROM messages m WHERE m.chat_id = chats.id), created_at)
            """)
        c.executescript(INDEXES)
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def adopt_orphan_chats(user_id: int) -> int:
    """Assign chats created before accounts existed to the given user."""
    with connect() as c:
        return c.execute("UPDATE chats SET user_id = ? WHERE user_id IS NULL", (user_id,)).rowcount

# =========================================
# Users
# =========================================

def count_users() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username: str, password_hash: str, role: str = "user") -> int:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, role, now()),
        )
        return cur.lastrowid


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(username: str) -> Optional[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_users() -> List[sqlite3.Row]:
    with connect() as c:
        return c.execute("""
            SELECT u.*, (SELECT COUNT(*) FROM chats ch WHERE ch.user_id = u.id) AS chat_count
            FROM users u ORDER BY u.username COLLATE NOCASE
        """).fetchall()


def count_active_admins() -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()[0]


def update_user(user_id: int, *, role: str = None, is_active: bool = None, password_hash: str = None):
    fields, values = [], []
    if role is not None:
        fields.append("role = ?")
        values.append(role)
    if is_active is not None:
        fields.append("is_active = ?")
        values.append(1 if is_active else 0)
    if password_hash is not None:
        fields.append("password_hash = ?")
        values.append(password_hash)
    if not fields:
        return
    with connect() as c:
        c.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", (*values, user_id))


def touch_login(user_id: int):
    with connect() as c:
        c.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now(), user_id))


def delete_user(user_id: int):
    with connect() as c:
        c.execute("DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE user_id = ?)", (user_id,))
        c.execute("DELETE FROM chats WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))

# =========================================
# Sessions
# =========================================

def create_session(token_hash: str, user_id: int, created_at: int, expires_at: int):
    with connect() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (created_at,))
        c.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, user_id, created_at, expires_at),
        )


def session_user(token_hash: str, at: int) -> Optional[sqlite3.Row]:
    """The active user behind a session that has not expired, or None."""
    with connect() as c:
        return c.execute("""
            SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
        """, (token_hash, at)).fetchone()


def delete_session(token_hash: str):
    with connect() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_user_sessions(user_id: int, keep_token_hash: str = None):
    with connect() as c:
        c.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash IS NOT ?",
            (user_id, keep_token_hash),
        )

# =========================================
# Chats & messages
# =========================================

def create_chat(user_id: int, title: str) -> str:
    chat_id = str(uuid.uuid4())
    stamp = now()
    with connect() as c:
        c.execute(
            "INSERT INTO chats (id, title, created_at, user_id, updated_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, title, stamp, user_id, stamp),
        )
    return chat_id


def get_chat(chat_id: str) -> Optional[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()


def list_chats(user_id: int) -> List[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT id, title FROM chats WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()


def chat_turns(chat_id: str) -> List[tuple]:
    """Saved (question, answer) pairs, oldest first."""
    with connect() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id ASC", (chat_id,)
        ).fetchall()

    turns, question = [], None
    for row in rows:
        if row["role"] == "user":
            question = row["content"]
        elif row["role"] == "assistant" and question is not None:
            turns.append((question, row["content"]))
            question = None
    return turns


def add_exchange(chat_id: str, question: str, answer: str):
    stamp = now()
    with connect() as c:
        c.executemany(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [(chat_id, "user", question, stamp), (chat_id, "assistant", answer, stamp)],
        )
        c.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (stamp, chat_id))


def delete_chat(chat_id: str):
    with connect() as c:
        c.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        c.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


def delete_user_chats(user_id: int) -> int:
    with connect() as c:
        c.execute("DELETE FROM messages WHERE chat_id IN (SELECT id FROM chats WHERE user_id = ?)", (user_id,))
        return c.execute("DELETE FROM chats WHERE user_id = ?", (user_id,)).rowcount


def admin_list_chats(user_id: Optional[int] = None, search: str = "", limit: int = 500) -> List[sqlite3.Row]:
    sql = """
        SELECT ch.id, ch.title, ch.updated_at, ch.user_id,
               COALESCE(u.username, '(deleted)') AS username,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_id = ch.id) AS message_count
        FROM chats ch LEFT JOIN users u ON u.id = ch.user_id
        WHERE 1 = 1
    """
    params: list = []
    if user_id is not None:
        sql += " AND ch.user_id = ?"
        params.append(user_id)
    if search:
        sql += """ AND (ch.title LIKE ? OR EXISTS (
                    SELECT 1 FROM messages m WHERE m.chat_id = ch.id AND m.content LIKE ?))"""
        params += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY ch.updated_at DESC LIMIT ?"
    params.append(limit)
    with connect() as c:
        return c.execute(sql, params).fetchall()

# =========================================
# Moderation log
# =========================================

def log_moderation(user_id, chat_id, stage, checker, verdict, detail, content):
    with connect() as c:
        c.execute(
            """INSERT INTO moderation_log
               (user_id, chat_id, stage, checker, verdict, detail, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, chat_id, stage, checker, verdict, detail, (content or "")[:4000], now()),
        )


def list_moderation(user_id: Optional[int] = None, limit: int = 500) -> List[sqlite3.Row]:
    sql = """
        SELECT l.*, COALESCE(u.username, '(deleted)') AS username
        FROM moderation_log l LEFT JOIN users u ON u.id = l.user_id
    """
    params: list = []
    if user_id is not None:
        sql += " WHERE l.user_id = ?"
        params.append(user_id)
    sql += " ORDER BY l.id DESC LIMIT ?"
    params.append(limit)
    with connect() as c:
        return c.execute(sql, params).fetchall()


def clear_moderation() -> int:
    with connect() as c:
        return c.execute("DELETE FROM moderation_log").rowcount

# =========================================
# Stats
# =========================================

def stats() -> dict:
    since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    with connect() as c:
        one = lambda sql, *p: c.execute(sql, p).fetchone()[0]
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "active_users": one("SELECT COUNT(*) FROM users WHERE is_active = 1"),
            "chats": one("SELECT COUNT(*) FROM chats"),
            "messages": one("SELECT COUNT(*) FROM messages"),
            "blocked_24h": one("SELECT COUNT(*) FROM moderation_log WHERE created_at >= ?", since),
        }
