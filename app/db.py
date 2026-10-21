import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import List, Optional

from config import DATA_DIR

DB_FILE = os.path.join(DATA_DIR, "chat_history.db")
SCHEMA_VERSION = 3

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
    updated_at TEXT,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT,
    role TEXT,
    content TEXT,
    created_at TEXT,
    data TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    conversation_id TEXT,
    detail TEXT,
    ip TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_chats_user ON chats (user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (created_at);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log (user_id, id);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def epoch(stamp: Optional[str]) -> Optional[int]:
    """ISO timestamp from the database -> Unix seconds for the browser."""
    return int(datetime.fromisoformat(stamp).timestamp()) if stamp else None


@contextmanager
def connect():
    """One connection per operation: safe to use from any worker thread."""
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

def add_column(c, table: str, column: str, kind: str) -> bool:
    if column in {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}:
        return False
    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    return True


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    existed = os.path.exists(DB_FILE) and os.path.getsize(DB_FILE) > 0

    with connect() as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]

    if existed and version < SCHEMA_VERSION:
        shutil.copy2(DB_FILE, f"{DB_FILE}.v{version}.bak")

    with connect() as c:
        c.executescript(SCHEMA)

        # v0 (single-user app) -> v2: chats belong to users.
        add_column(c, "chats", "user_id", "INTEGER")
        if add_column(c, "chats", "updated_at", "TEXT"):
            c.execute("""
                UPDATE chats SET updated_at = COALESCE(
                    (SELECT MAX(created_at) FROM messages m WHERE m.chat_id = chats.id), created_at)
            """)

        # v2 -> v3: soft-deleted chats, message metadata, activity log.
        add_column(c, "chats", "deleted_at", "TEXT")
        add_column(c, "messages", "data", "TEXT")
        if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'moderation_log'").fetchone():
            c.execute("""
                INSERT INTO audit_log (created_at, user_id, username, action, conversation_id, detail)
                SELECT l.created_at, l.user_id, u.username, 'ask', l.chat_id,
                       json_object('question', l.content, 'status', 'blocked', 'stage', l.stage,
                                   'checker', l.checker, 'verdict', l.verdict, 'reason', l.detail)
                FROM moderation_log l LEFT JOIN users u ON u.id = l.user_id
                ORDER BY l.id
            """)
            c.execute("DROP TABLE moderation_log")

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
            SELECT u.*,
                (SELECT COUNT(*) FROM chats ch WHERE ch.user_id = u.id AND ch.deleted_at IS NULL) AS chats,
                (SELECT COUNT(*) FROM chats ch WHERE ch.user_id = u.id AND ch.deleted_at IS NOT NULL) AS deleted_chats,
                (SELECT COUNT(*) FROM messages m JOIN chats ch ON ch.id = m.chat_id
                    WHERE ch.user_id = u.id AND m.role = 'user') AS questions,
                (SELECT MAX(created_at) FROM audit_log a WHERE a.user_id = u.id) AS last_activity
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
    """Remove the account and all of its chats. Activity log entries keep the username."""
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
