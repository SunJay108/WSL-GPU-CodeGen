"""
Accounts and sessions: PBKDF2 password hashes, a random session token in an
HttpOnly cookie (only its SHA-256 is stored), login throttling, and the
/api/auth/* endpoints used by the chat page.
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import audit
import db
from config import ADMIN_PASSWORD, ADMIN_USERNAME, ALLOW_SIGNUP, COOKIE_SECURE, SESSION_HOURS

log = logging.getLogger("auth")

SESSION_COOKIE = "codegen_session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LENGTH = 8
PBKDF2_ITERATIONS = 310_000

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


def public(user) -> dict:
    return {"id": user["id"], "username": user["username"], "role": user["role"]}

# =========================================
# Sessions
# =========================================

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    created = int(time.time())
    db.create_session(token_hash(token), user_id, created, created + SESSION_HOURS * 3600)
    db.touch_login(user_id)
    return token


def user_from_request(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return db.session_user(token_hash(token), int(time.time()))


def current_user(request: Request) -> dict:
    user = user_from_request(request)
    if user is None:
        raise HTTPException(401, "Please sign in.")
    return dict(user)


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Administrators only.")
    return user


def client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None

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
        log.warning("Sign in and change it from the sidebar.")
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
# /api/auth/*
# =========================================

class Credentials(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str


router = APIRouter()


def signed_in(user, request: Request) -> JSONResponse:
    token = start_session(user["id"])
    response = JSONResponse({"user": public(user)})
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_HOURS * 3600,
                        httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/")
    return response


@router.get("/api/auth/state")
def auth_state(request: Request):
    user = user_from_request(request)
    return {"user": public(user) if user else None, "allow_signup": ALLOW_SIGNUP}


@router.post("/api/auth/login")
def login(body: Credentials, request: Request):
    username = body.username.strip()[:64]
    key = (client_ip(request), username.lower())
    if throttle.blocked(key):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")

    user = authenticate(username, body.password[:256])
    if user is None:
        throttle.fail(key)
        audit.log("login_failed", username=username, ip=client_ip(request),
                  detail={"reason": "wrong username or password, or disabled account"})
        raise HTTPException(401, "Incorrect username or password.")

    throttle.reset(key)
    audit.log("login", user, ip=client_ip(request))
    return signed_in(user, request)


@router.post("/api/auth/signup")
def signup(body: Credentials, request: Request):
    if not ALLOW_SIGNUP:
        raise HTTPException(403, "Sign-up is disabled. Ask an administrator for an account.")
    username = body.username.strip()
    error = username_error(username) or password_error(body.password)
    if error is None and db.get_user_by_name(username):
        error = "That username is taken."
    if error:
        raise HTTPException(400, error)

    db.create_user(username, hash_password(body.password), "user")
    user = db.get_user_by_name(username)
    audit.log("signup", user, ip=client_ip(request), detail={"role": "user"})
    return signed_in(user, request)


@router.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    user = user_from_request(request)
    if token:
        db.delete_session(token_hash(token))
    if user:
        audit.log("logout", user, ip=client_ip(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/api/auth/password")
def change_password(body: PasswordChange, request: Request, user: dict = Depends(current_user)):
    if not verify_password(body.current, user["password_hash"]):
        raise HTTPException(400, "Current password is incorrect.")
    error = password_error(body.new)
    if error:
        raise HTTPException(400, error)
    db.update_user(user["id"], password_hash=hash_password(body.new))
    # Sign out every other session of this account.
    db.delete_user_sessions(user["id"], keep_token_hash=token_hash(request.cookies.get(SESSION_COOKIE, "")))
    audit.log("password_change", user, ip=client_ip(request))
    return {"ok": True}


def install(app: FastAPI):
    app.include_router(router)
