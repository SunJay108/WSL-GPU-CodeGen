"""Conversations and messages. Chats deleted by their owner are kept (hidden) for administrators."""

import json
import re
import uuid
from typing import List, Optional

from db import connect, epoch, now

DEFAULT_TITLE = "New chat"


def make_title(question: str) -> str:
    title = re.sub(r"\s+", " ", question).strip()
    return (title[:57] + "...") if len(title) > 60 else (title or DEFAULT_TITLE)


def message_dict(row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "data": json.loads(row["data"]) if row["data"] else None,
        "created_at": epoch(row["created_at"]),
    }


def conversation_dict(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"] or DEFAULT_TITLE,
        "user_id": row["user_id"],
        "created_at": epoch(row["created_at"]),
        "updated_at": epoch(row["updated_at"]),
        "deleted_at": epoch(row["deleted_at"]),
    }

# =========================================
# Own chats
# =========================================

def create_conversation(user_id: int) -> dict:
    chat_id = uuid.uuid4().hex
    stamp = now()
    with connect() as c:
        c.execute(
            "INSERT INTO chats (id, title, created_at, user_id, updated_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, DEFAULT_TITLE, stamp, user_id, stamp),
        )
    return {"id": chat_id, "title": DEFAULT_TITLE, "created_at": epoch(stamp), "updated_at": epoch(stamp)}


def list_conversations(user_id: int, q: Optional[str] = None) -> List[dict]:
    sql = "SELECT id, title, updated_at FROM chats WHERE user_id = ? AND deleted_at IS NULL"
    params: list = [user_id]
    if q:
        sql += """ AND (title LIKE ? OR EXISTS (
                    SELECT 1 FROM messages m WHERE m.chat_id = chats.id AND m.content LIKE ?))"""
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY updated_at DESC LIMIT 500"
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [{"id": r["id"], "title": r["title"] or DEFAULT_TITLE, "updated_at": epoch(r["updated_at"])} for r in rows]


def get_conversation(chat_id: str, user_id: Optional[int] = None) -> Optional[dict]:
    """With user_id: only that user's chat, and only if not deleted. Without: any chat (admins)."""
    with connect() as c:
        row = c.execute("""
            SELECT ch.*, u.username FROM chats ch LEFT JOIN users u ON u.id = ch.user_id WHERE ch.id = ?
        """, (chat_id,)).fetchone()
        if row is None or (user_id is not None and (row["user_id"] != user_id or row["deleted_at"])):
            return None
        messages = c.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
    return {**conversation_dict(row), "username": row["username"], "messages": [message_dict(m) for m in messages]}


def rename_conversation(chat_id: str, user_id: int, title: str):
    title = re.sub(r"\s+", " ", title).strip()[:200]
    with connect() as c:
        row = c.execute(
            "SELECT title FROM chats WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (chat_id, user_id)
        ).fetchone()
        if row is None or not title:
            return None
        c.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))
    return row["title"], title


def delete_conversation(chat_id: str, user_id: int) -> Optional[dict]:
    with connect() as c:
        row = c.execute(
            "SELECT title FROM chats WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (chat_id, user_id)
        ).fetchone()
        if row is None:
            return None
        c.execute("UPDATE chats SET deleted_at = ? WHERE id = ?", (now(), chat_id))
        questions = c.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND role = 'user'", (chat_id,)
        ).fetchone()[0]
    return {"title": row["title"], "questions": questions}


def delete_all(user_id: int) -> int:
    with connect() as c:
        return c.execute(
            "UPDATE chats SET deleted_at = ? WHERE user_id = ? AND deleted_at IS NULL", (now(), user_id)
        ).rowcount


def add_message(chat_id: str, role: str, content: str, data: Optional[dict] = None) -> dict:
    stamp = now()
    with connect() as c:
        cur = c.execute(
            "INSERT INTO messages (chat_id, role, content, created_at, data) VALUES (?, ?, ?, ?, ?)",
            (chat_id, role, content, stamp, json.dumps(data) if data else None),
        )
        c.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (stamp, chat_id))
        if role == "user":
            # The first question names the chat.
            c.execute("UPDATE chats SET title = ? WHERE id = ? AND title = ?",
                      (make_title(content), chat_id, DEFAULT_TITLE))
        row = c.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return message_dict(row)


def context_turns(chat_id: str) -> List[tuple]:
    """(question, answer) pairs the model may see: answered exchanges only, not blocked ones."""
    with connect() as c:
        rows = c.execute("SELECT * FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()

    turns, question = [], None
    for row in rows:
        if row["role"] == "user":
            question = row["content"]
        elif row["role"] == "assistant" and question is not None:
            data = json.loads(row["data"]) if row["data"] else {}
            if data.get("status", "answered") == "answered":
                turns.append((question, row["content"]))
            question = None
    return turns


def export_markdown(conv: dict) -> str:
    parts = [f"# {conv['title']}\n"]
    for m in conv["messages"]:
        parts.append(f"## {'You' if m['role'] == 'user' else 'CodeGen'}\n\n{m['content']}\n")
    return "\n".join(parts)

# =========================================
# Admin
# =========================================

def list_all_conversations(user_id: Optional[int], status: str = "all", q: Optional[str] = None) -> List[dict]:
    sql = """
        SELECT ch.*, u.username,
               (SELECT COUNT(*) FROM messages m WHERE m.chat_id = ch.id AND m.role = 'user') AS questions
        FROM chats ch LEFT JOIN users u ON u.id = ch.user_id WHERE 1 = 1
    """
    params: list = []
    if user_id:
        sql += " AND ch.user_id = ?"
        params.append(user_id)
    if status == "active":
        sql += " AND ch.deleted_at IS NULL"
    elif status == "deleted":
        sql += " AND ch.deleted_at IS NOT NULL"
    if q:
        sql += """ AND (ch.title LIKE ? OR EXISTS (
                    SELECT 1 FROM messages m WHERE m.chat_id = ch.id AND m.content LIKE ?))"""
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY ch.updated_at DESC LIMIT 500"
    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [{**conversation_dict(r), "username": r["username"], "questions": r["questions"]} for r in rows]


def purge_conversation(chat_id: str) -> Optional[dict]:
    """Delete a chat permanently (admin)."""
    with connect() as c:
        row = c.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            return None
        c.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        c.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    return {"title": row["title"]}
