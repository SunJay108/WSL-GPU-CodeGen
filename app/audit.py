"""Activity log: sign-ins, questions (including blocked ones), chat and admin actions."""

import json
from datetime import datetime
from typing import List, Optional

from db import connect, epoch, now

ACTIONS = {
    "ask": "Question",
    "login": "Sign in",
    "login_failed": "Failed sign-in",
    "logout": "Sign out",
    "signup": "Sign-up",
    "password_change": "Password change",
    "rename_chat": "Rename chat",
    "delete_chat": "Delete chat",
    "clear_history": "Delete all chats",
    "export_chat": "Export chat",
    "admin_create_user": "Admin: add user",
    "admin_set_role": "Admin: change role",
    "admin_disable_user": "Admin: disable user",
    "admin_enable_user": "Admin: enable user",
    "admin_reset_password": "Admin: reset password",
    "admin_delete_user": "Admin: delete user",
    "admin_delete_chat": "Admin: delete chat",
}


def log(action: str, user=None, conversation_id: str = None, detail: dict = None,
        ip: str = None, username: str = None):
    with connect() as c:
        c.execute(
            """INSERT INTO audit_log (created_at, user_id, username, action, conversation_id, detail, ip)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now(), user["id"] if user else None, user["username"] if user else username,
             action, conversation_id, json.dumps(detail or {}, ensure_ascii=False), ip),
        )


def query(user_id: Optional[int], action: Optional[str], q: Optional[str],
          before: Optional[int], limit: int = 100) -> List[dict]:
    sql = """
        SELECT a.*, ch.deleted_at AS conversation_deleted_at, ch.title AS conversation_title
        FROM audit_log a LEFT JOIN chats ch ON ch.id = a.conversation_id WHERE 1 = 1
    """
    params: list = []
    if user_id:
        sql += " AND a.user_id = ?"
        params.append(user_id)
    if action:
        sql += " AND a.action = ?"
        params.append(action)
    if q:
        sql += " AND (a.username LIKE ? OR a.detail LIKE ? OR ch.title LIKE ?)"
        params += [f"%{q}%"] * 3
    if before:
        sql += " AND a.id < ?"
        params.append(before)
    sql += " ORDER BY a.id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))

    with connect() as c:
        rows = c.execute(sql, params).fetchall()
    return [{
        "id": r["id"],
        "created_at": epoch(r["created_at"]),
        "user_id": r["user_id"],
        "username": r["username"],
        "action": r["action"],
        "action_label": ACTIONS.get(r["action"], r["action"]),
        "conversation_id": r["conversation_id"] if r["conversation_title"] is not None else None,
        "conversation_deleted_at": epoch(r["conversation_deleted_at"]),
        "detail": json.loads(r["detail"] or "{}"),
        "ip": r["ip"],
    } for r in rows]


def stats() -> dict:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    with connect() as c:
        one = lambda sql, *p: c.execute(sql, p).fetchone()[0]
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "active_today": one("SELECT COUNT(DISTINCT user_id) FROM audit_log WHERE created_at >= ? "
                                "AND user_id IS NOT NULL", today),
            "chats": one("SELECT COUNT(*) FROM chats WHERE deleted_at IS NULL"),
            "deleted_chats": one("SELECT COUNT(*) FROM chats WHERE deleted_at IS NOT NULL"),
            "questions": one("SELECT COUNT(*) FROM messages WHERE role = 'user'"),
            "questions_today": one("SELECT COUNT(*) FROM messages WHERE role = 'user' AND created_at >= ?", today),
            "blocked": one("SELECT COUNT(*) FROM audit_log WHERE action = 'ask' "
                           "AND json_extract(detail, '$.status') = 'blocked'"),
            "failed_logins_today": one("SELECT COUNT(*) FROM audit_log WHERE action = 'login_failed' "
                                       "AND created_at >= ?", today),
        }
