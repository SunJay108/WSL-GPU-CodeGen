import base64
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from string import Template
from typing import Optional

import gradio as gr
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

import db
from config import ADMIN_PASSWORD, ADMIN_USERNAME, COOKIE_SECURE, SESSION_HOURS

log = logging.getLogger("auth")

SESSION_COOKIE = "codegen_session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
PBKDF2_ITERATIONS = 310_000

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# =========================================
# Passwords
# =========================================

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# Verified against when the username does not exist, so timing does not reveal it.
DUMMY_HASH = hash_password(secrets.token_hex(16))


def username_error(username: str) -> Optional[str]:
    if not USERNAME_RE.match(username or ""):
        return "Username must be 3-32 characters: letters, digits, dot, dash or underscore."
    return None


def password_error(password: str) -> Optional[str]:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def authenticate(username: str, password: str):
    user = db.get_user_by_name(username)
    if user is None:
        verify_password(password, DUMMY_HASH)
        return None
    if not verify_password(password, user["password_hash"]) or not user["is_active"]:
        return None
    return user

# =========================================
# Sessions (random token in a cookie, SHA-256 of it in the database)
# =========================================

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    created = int(time.time())
    db.create_session(token_hash(token), user_id, created, created + SESSION_HOURS * 3600)
    db.touch_login(user_id)
    return token


def request_token(request) -> Optional[str]:
    cookies = getattr(request, "cookies", None) or {}
    return cookies.get(SESSION_COOKIE)


def user_from_request(request):
    """The logged-in, active user for a FastAPI or Gradio request, or None."""
    token = request_token(request)
    if not token:
        return None
    return db.session_user(token_hash(token), int(time.time()))


def require_user(request: gr.Request) -> dict:
    user = user_from_request(request)
    if user is None:
        raise gr.Error("Your session has expired. Please reload the page and sign in again.")
    return dict(user)


def require_admin(request: gr.Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise gr.Error("Administrator access required.")
    return user


def change_password(request: gr.Request, current: str, new: str) -> Optional[str]:
    """Returns an error message, or None on success. Other sessions are signed out."""
    user = require_user(request)
    if not verify_password(current or "", user["password_hash"]):
        return "Current password is incorrect."
    error = password_error(new)
    if error:
        return error
    db.update_user(user["id"], password_hash=hash_password(new))
    db.delete_user_sessions(user["id"], keep_token_hash=token_hash(request_token(request)))
    return None

# =========================================
# Gradio auth dependencies
# =========================================

def chat_dependency(request: Request) -> Optional[str]:
    user = user_from_request(request)
    return user["username"] if user else None


def admin_dependency(request: Request) -> Optional[str]:
    user = user_from_request(request)
    return user["username"] if user and user["role"] == "admin" else None

# =========================================
# First admin account
# =========================================

def ensure_admin():
    if db.count_users() > 0:
        return

    username = ADMIN_USERNAME if USERNAME_RE.match(ADMIN_USERNAME) else "admin"
    password = ADMIN_PASSWORD or secrets.token_urlsafe(12)
    user_id = db.create_user(username, hash_password(password), "admin")
    adopted = db.adopt_orphan_chats(user_id)

    log.warning("=" * 60)
    log.warning("Created administrator account '%s'.", username)
    if not ADMIN_PASSWORD:
        log.warning("Generated password: %s", password)
        log.warning("Sign in and change it from the user menu.")
    if adopted:
        log.warning("Assigned %d existing chat(s) to '%s'.", adopted, username)
    log.warning("=" * 60)

# =========================================
# Login throttling
# =========================================

class LoginThrottle:
    """At most MAX_FAILURES failed logins per (client, username) within WINDOW seconds."""

    MAX_FAILURES = 5
    WINDOW = 15 * 60

    def __init__(self):
        self.failures = defaultdict(deque)
        self.lock = threading.Lock()

    def _recent(self, key) -> deque:
        attempts = self.failures[key]
        while attempts and attempts[0] < time.time() - self.WINDOW:
            attempts.popleft()
        return attempts

    def blocked(self, key) -> bool:
        with self.lock:
            return len(self._recent(key)) >= self.MAX_FAILURES

    def fail(self, key):
        with self.lock:
            self._recent(key).append(time.time())

    def reset(self, key):
        with self.lock:
            self.failures.pop(key, None)


throttle = LoginThrottle()

# =========================================
# Routes & page gate
# =========================================

def render_login(error: str = "", username: str = "") -> str:
    with open(os.path.join(TEMPLATE_DIR, "login.html"), encoding="utf-8") as f:
        page = Template(f.read())
    error_block = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    return page.safe_substitute(error_block=error_block, username=html.escape(username))


def install_routes(app: FastAPI):
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if await run_in_threadpool(user_from_request, request):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(render_login())

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        username = str(form.get("username", "")).strip()[:64]
        password = str(form.get("password", ""))[:256]
        key = (request.client.host if request.client else "", username.lower())

        if throttle.blocked(key):
            return HTMLResponse(
                render_login("Too many failed attempts. Try again in 15 minutes.", username), status_code=429
            )

        user = await run_in_threadpool(authenticate, username, password)
        if user is None:
            throttle.fail(key)
            return HTMLResponse(render_login("Incorrect username or password.", username), status_code=401)

        throttle.reset(key)
        token = await run_in_threadpool(start_session, user["id"])
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, token, max_age=SESSION_HOURS * 3600,
            httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/",
        )
        return response

    @app.get("/logout")
    async def logout(request: Request):
        token = request_token(request)
        if token:
            await run_in_threadpool(db.delete_session, token_hash(token))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response


class PageGate:
    """
    ASGI middleware for browser page loads: anonymous visitors go to /login and
    non-admins are kept out of /admin. API calls are guarded by the Gradio auth
    dependencies and by the per-handler checks.
    """

    PAGES = ("/", "/admin", "/admin/")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("GET", "HEAD") and scope["path"] in self.PAGES:
            user = await run_in_threadpool(user_from_request, Request(scope))
            target = None
            if user is None:
                target = "/login"
            elif scope["path"].startswith("/admin") and user["role"] != "admin":
                target = "/"
            elif scope["path"] == "/admin":
                target = "/admin/"
            if target:
                await RedirectResponse(target, status_code=303)(scope, receive, send)
                return
        await self.app(scope, receive, send)
