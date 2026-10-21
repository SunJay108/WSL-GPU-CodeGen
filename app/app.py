"""
CodeGen web app (port 7860): chat with history, user accounts and an admin
panel; answers come from the local coder model and pass the Qwen3Guard filter.

Pages
    GET  /                          chat UI (shows the sign-in screen when needed)
    GET  /admin                     admin panel (administrators only)

Accounts (auth.py)
    GET  /api/auth/state            signed-in user, whether sign-up is open
    POST /api/auth/login | signup | logout | password

Monitoring (no sign-in)
    GET  /api/health                model server and guard reachable?

Chats (own chats only)
    GET    /api/status                      model / filter state
    GET    /api/conversations?q=            history list (optionally searched)
    POST   /api/conversations               new conversation
    GET    /api/conversations/{id}          messages (+ state of an answer in progress)
    PATCH  /api/conversations/{id}          rename
    DELETE /api/conversations/{id}          delete (hidden from the user, kept for admins)
    DELETE /api/conversations               delete all own chats
    GET    /api/conversations/{id}/export   Markdown download
    POST   /api/conversations/{id}/ask      ask; progress is streamed as Server-Sent Events
    POST   /api/conversations/{id}/stop     stop the answer in progress

Admin
    GET    /api/admin/stats | activity | users | conversations | conversations/{id}
    POST   /api/admin/users                 add a user
    PATCH  /api/admin/users/{id}            disable/enable, change role, reset password
    DELETE /api/admin/users/{id}            delete a user and their chats
    DELETE /api/admin/conversations/{id}    delete a chat permanently
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

import requests
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

import audit
import auth
import db
import filter
import history
from answer import answer_question
from auth import admin_user, client_ip, current_user
from config import GUARD_URL, LLAMA_URL, MAX_PROMPT_CHARS, MODEL_LABEL

log = logging.getLogger("app")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def warm_up():
    """Prime the classifier slot so the first real question is not slow."""
    for _ in range(60):
        try:
            filter.topic_check("prompt", filter.PROMPT_TOPIC_SYSTEM, filter.PROMPT_TOPIC_EXAMPLES,
                               filter.topic_input("How do I reverse a list in Python?"), filter.PROMPT_LABELS)
            log.info("Topic classifier warmed up")
            return
        except Exception:
            time.sleep(5)
    log.warning("Topic classifier warm-up gave up; the first question may be slow")


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=warm_up, name="warm-up", daemon=True).start()
    yield


db.init_db()
auth.ensure_admin()

app = FastAPI(title="CodeGen", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
auth.install(app)

# Answers in progress: conversation id -> {"user_id", "stage", "text", "partial", "stop"}.
# An answer keeps running (and is saved) even if the browser disconnects.
_pending = {}
_pending_lock = threading.Lock()

# =========================================
# Request models / helpers
# =========================================

class AskBody(BaseModel):
    question: str


class RenameBody(BaseModel):
    title: str


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "user"


class UserUpdate(BaseModel):
    disabled: Optional[bool] = None
    role: Optional[str] = None
    password: Optional[str] = None


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def own_conversation(conversation_id: str, user: dict) -> dict:
    conv = history.get_conversation(conversation_id, user_id=user["id"])
    if conv is None:
        raise HTTPException(404, "Conversation not found.")
    return conv


def busy(user_id: int = None, conversation_id: str = None) -> bool:
    with _pending_lock:
        if conversation_id is not None:
            return conversation_id in _pending
        return any(p["user_id"] == user_id for p in _pending.values())


def reachable(url: str) -> bool:
    try:
        return requests.get(url, timeout=2).ok
    except requests.RequestException:
        return False


def services() -> dict:
    return {"llm": reachable(f"{LLAMA_URL}/health"), "guard": reachable(f"{GUARD_URL}/health")}

# =========================================
# Pages
# =========================================

@app.get("/", include_in_schema=False)
def index_page():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    state = services()
    ready = all(state.values())
    return JSONResponse({"status": "ok" if ready else "degraded", **state}, status_code=200 if ready else 503)

# =========================================
# Chats
# =========================================

@app.get("/api/status")
def status(user: dict = Depends(current_user)):
    return {
        "model": MODEL_LABEL,
        "services": services(),
        "max_question_chars": MAX_PROMPT_CHARS,
        "busy": busy(user_id=user["id"]),
        "user": auth.public(user),
    }


@app.get("/api/conversations")
def list_conversations(q: str = "", user: dict = Depends(current_user)):
    return history.list_conversations(user["id"], q.strip() or None)


@app.post("/api/conversations")
def create_conversation(user: dict = Depends(current_user)):
    return history.create_conversation(user["id"])


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user: dict = Depends(current_user)):
    conv = own_conversation(conversation_id, user)
    with _pending_lock:
        pending = _pending.get(conversation_id)
        conv["pending"] = (
            {k: v for k, v in pending.items() if k not in ("user_id", "stop")} if pending else None
        )
    return conv


@app.patch("/api/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, body: RenameBody, request: Request,
                        user: dict = Depends(current_user)):
    renamed = history.rename_conversation(conversation_id, user["id"], body.title)
    if renamed is None:
        raise HTTPException(404, "Conversation not found.")
    audit.log("rename_chat", user, conversation_id, {"old": renamed[0], "new": renamed[1]}, ip=client_ip(request))
    return {"ok": True}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request, user: dict = Depends(current_user)):
    if busy(conversation_id=conversation_id):
        raise HTTPException(409, "This conversation is still being answered.")
    deleted = history.delete_conversation(conversation_id, user["id"])
    if deleted is None:
        raise HTTPException(404, "Conversation not found.")
    audit.log("delete_chat", user, conversation_id, deleted, ip=client_ip(request))
    return {"ok": True}


@app.delete("/api/conversations")
def delete_all_conversations(request: Request, user: dict = Depends(current_user)):
    if busy(user_id=user["id"]):
        raise HTTPException(409, "A question is still being answered; try again when it has finished.")
    count = history.delete_all(user["id"])
    audit.log("clear_history", user, detail={"chats": count}, ip=client_ip(request))
    return {"deleted": count}


@app.get("/api/conversations/{conversation_id}/export")
def export_conversation(conversation_id: str, request: Request, user: dict = Depends(current_user)):
    conv = own_conversation(conversation_id, user)
    audit.log("export_chat", user, conversation_id, {"title": conv["title"]}, ip=client_ip(request))
    filename = (re.sub(r"[^\w\- ]+", "", conv["title"]).strip() or "conversation")[:60] + ".md"
    return Response(
        history.export_markdown(conv),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/api/conversations/{conversation_id}/stop")
def stop_answer(conversation_id: str, user: dict = Depends(current_user)):
    with _pending_lock:
        pending = _pending.get(conversation_id)
        if pending and pending["user_id"] == user["id"]:
            pending["stop"] = True
    return {"ok": True}


@app.post("/api/conversations/{conversation_id}/ask")
async def ask(conversation_id: str, body: AskBody, request: Request, user: dict = Depends(current_user)):
    # async: a waiting answer stream holds no worker thread. Database calls and
    # the answer itself run in threads so they never block the event loop.
    question = filter.sanitize(body.question)
    if not question:
        raise HTTPException(400, "Please enter a question.")
    if len(question) > MAX_PROMPT_CHARS:
        raise HTTPException(400, f"The question is too long (maximum {MAX_PROMPT_CHARS:,} characters).")
    await run_in_threadpool(own_conversation, conversation_id, user)

    with _pending_lock:
        if conversation_id in _pending:
            raise HTTPException(409, "This conversation is still being answered.")
        _pending[conversation_id] = {"user_id": user["id"], "stage": "queued", "text": "Starting…",
                                     "partial": "", "stop": False}

    turns = await run_in_threadpool(history.context_turns, conversation_id)
    user_message = await run_in_threadpool(history.add_message, conversation_id, "user", question)
    loop = asyncio.get_running_loop()
    events = asyncio.Queue()
    ip = client_ip(request)

    def put(item):
        loop.call_soon_threadsafe(events.put_nowait, item)

    def progress(event, **data):
        with _pending_lock:
            state = _pending.get(conversation_id)
            if state is not None:
                if event == "stage":
                    state.update(stage=data["stage"], text=data["text"])
                elif event == "delta":
                    state["partial"] = data["text"]
        put((event, data))

    def should_stop() -> bool:
        with _pending_lock:
            return bool(_pending.get(conversation_id, {}).get("stop"))

    def work():
        try:
            result = answer_question(question, turns, progress, should_stop)
            data = {k: v for k, v in result.items() if k not in ("markdown", "partial")}
            message = history.add_message(conversation_id, "assistant", result["markdown"], data)
            guard = result.get("guard") or {}
            audit.log("ask", user, conversation_id, {
                "question": question[:2000],
                "status": result["status"],
                "stage": guard.get("stage"),
                "checker": guard.get("checker"),
                "verdict": guard.get("verdict"),
                "reason": guard.get("reason"),
                "answer": (result.get("partial") or "")[:2000] or None,
                "stopped": result.get("stopped") or None,
                "seconds": result.get("seconds"),
            }, ip=ip)
            put(("done", {"message": message}))
        except Exception as exc:
            log.exception("Answer failed")
            text = f"⚠️ Something went wrong while answering: {exc}"
            message = history.add_message(conversation_id, "assistant", text, {"status": "error", "message": text})
            audit.log("ask", user, conversation_id,
                      {"question": question[:2000], "status": "error", "reason": str(exc)[:300]}, ip=ip)
            put(("error", {"message": message}))
        finally:
            with _pending_lock:
                _pending.pop(conversation_id, None)

    threading.Thread(target=work, name=f"answer-{conversation_id[:8]}", daemon=True).start()

    async def stream():
        yield sse("user", {"message": user_message})
        while True:
            try:
                event, data = await asyncio.wait_for(events.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield sse(event, data)
            if event in ("done", "error"):
                break

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# =========================================
# Admin
# =========================================

def admin_view(u) -> dict:
    return {
        "id": u["id"], "username": u["username"], "role": u["role"], "disabled": not u["is_active"],
        "created_at": db.epoch(u["created_at"]), "last_login_at": db.epoch(u["last_login_at"]),
        "last_activity": db.epoch(u["last_activity"]),
        "chats": u["chats"], "deleted_chats": u["deleted_chats"], "questions": u["questions"],
    }


def keeps_an_admin(target, role: str = None, disabled: bool = None) -> bool:
    """False if the change would leave no active administrator."""
    if target["role"] != "admin" or not target["is_active"]:
        return True
    losing = (role is not None and role != "admin") or bool(disabled)
    return not losing or db.count_active_admins() > 1


@app.get("/api/admin/stats")
def admin_stats(admin: dict = Depends(admin_user)):
    return audit.stats()


@app.get("/api/admin/activity")
def admin_activity(user_id: int = 0, action: str = "", q: str = "", before: int = 0, limit: int = 100,
                   admin: dict = Depends(admin_user)):
    return {
        "items": audit.query(user_id or None, action or None, q.strip() or None, before or None, limit),
        "actions": audit.ACTIONS,
    }


@app.get("/api/admin/users")
def admin_users(admin: dict = Depends(admin_user)):
    return [admin_view(u) for u in db.list_users()]


@app.post("/api/admin/users")
def admin_create_user(body: NewUser, request: Request, admin: dict = Depends(admin_user)):
    username = body.username.strip()
    error = auth.username_error(username) or auth.password_error(body.password)
    if error is None and body.role not in ("user", "admin"):
        error = "Role must be user or admin."
    if error is None and db.get_user_by_name(username):
        error = f"User '{username}' already exists."
    if error:
        raise HTTPException(400, error)
    db.create_user(username, auth.hash_password(body.password), body.role)
    audit.log("admin_create_user", admin, detail={"target": username, "role": body.role}, ip=client_ip(request))
    return {"ok": True}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserUpdate, request: Request, admin: dict = Depends(admin_user)):
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(404, "User not found.")
    ip = client_ip(request)

    if body.disabled is not None and body.disabled == bool(target["is_active"]):
        if user_id == admin["id"]:
            raise HTTPException(409, "You cannot disable your own account.")
        if not keeps_an_admin(target, disabled=body.disabled):
            raise HTTPException(409, "This is the last active administrator.")
        db.update_user(user_id, is_active=not body.disabled)
        if body.disabled:
            db.delete_user_sessions(user_id)
        audit.log("admin_disable_user" if body.disabled else "admin_enable_user", admin,
                  detail={"target": target["username"]}, ip=ip)

    if body.role and body.role != target["role"]:
        if body.role not in ("user", "admin"):
            raise HTTPException(400, "Role must be user or admin.")
        if user_id == admin["id"]:
            raise HTTPException(409, "You cannot change your own role.")
        if not keeps_an_admin(target, role=body.role):
            raise HTTPException(409, "This is the last active administrator.")
        db.update_user(user_id, role=body.role)
        audit.log("admin_set_role", admin,
                  detail={"target": target["username"], "role": body.role, "previous": target["role"]}, ip=ip)

    if body.password:
        error = auth.password_error(body.password)
        if error:
            raise HTTPException(400, error)
        db.update_user(user_id, password_hash=auth.hash_password(body.password))
        db.delete_user_sessions(user_id)
        audit.log("admin_reset_password", admin, detail={"target": target["username"]}, ip=ip)

    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request, admin: dict = Depends(admin_user)):
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(404, "User not found.")
    if user_id == admin["id"]:
        raise HTTPException(409, "You cannot delete your own account.")
    if not keeps_an_admin(target, disabled=True):
        raise HTTPException(409, "This is the last active administrator.")
    if busy(user_id=user_id):
        raise HTTPException(409, "This user has a question being answered; try again shortly.")
    db.delete_user(user_id)
    audit.log("admin_delete_user", admin, detail={"target": target["username"]}, ip=client_ip(request))
    return {"ok": True}


@app.get("/api/admin/conversations")
def admin_conversations(user_id: int = 0, status: str = "all", q: str = "", admin: dict = Depends(admin_user)):
    return history.list_all_conversations(user_id or None, status, q.strip() or None)


@app.get("/api/admin/conversations/{conversation_id}")
def admin_conversation(conversation_id: str, admin: dict = Depends(admin_user)):
    conv = history.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(404, "Conversation not found.")
    return conv


@app.delete("/api/admin/conversations/{conversation_id}")
def admin_delete_conversation(conversation_id: str, request: Request, admin: dict = Depends(admin_user)):
    if busy(conversation_id=conversation_id):
        raise HTTPException(409, "This conversation is still being answered.")
    deleted = history.purge_conversation(conversation_id)
    if deleted is None:
        raise HTTPException(404, "Conversation not found.")
    audit.log("admin_delete_chat", admin, None, {"title": deleted["title"]}, ip=client_ip(request))
    return {"ok": True}

# =========================================
# Launch
# =========================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="warning")
