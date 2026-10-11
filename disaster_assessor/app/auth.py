"""Password hashing (scrypt), cookie/bearer sessions, and role checks."""

import base64
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
import threading
import time

from fastapi import Depends, HTTPException, Request

from . import db
from .config import get_config

ROLES = ("field", "reviewer", "coordinator", "admin")
COOKIE = "dra_session"
_N, _R, _P = 2**14, 8, 1


def hash_password(password):
    if len(password) < 10:
        raise ValueError("Passwords need at least 10 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    enc = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_N}${_R}${_P}${enc(salt)}${enc(digest)}"


def verify_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=get_config().session_hours)
    db.insert(conn, "sessions", {
        "token_hash": _token_hash(token), "user_id": user_id,
        "created_at": db.now(), "expires_at": expires.isoformat(timespec="seconds"),
    })
    return token, expires


def end_session(conn, token):
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def public_user(user):
    return {k: user[k] for k in ("id", "username", "name", "role", "active")} if user else None


def user_for_token(token):
    if not token:
        return None
    with db.connect() as conn:
        row = db.one(
            conn,
            "SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
            (_token_hash(token),),
        )
        if not row or not row["active"] or row["expires_at"] < db.now():
            return None
        return row


def request_token(request: Request):
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(COOKIE)


def current_user(request: Request):
    user = user_for_token(request_token(request))
    if not user:
        raise HTTPException(401, "Sign in to continue.")
    return user


def require(min_role):
    """Dependency: user must have at least `min_role` (field < reviewer < coordinator < admin)."""
    level = ROLES.index(min_role)

    def check(user=Depends(current_user)):
        if ROLES.index(user["role"]) < level:
            raise HTTPException(403, f"This action needs the {min_role} role or higher.")
        return user

    return check


def has_role(user, min_role):
    return ROLES.index(user["role"]) >= ROLES.index(min_role)


class LoginThrottle:
    """In-memory brute-force guard: 8 failures per key per 10 minutes."""

    def __init__(self, limit=8, window=600):
        self.limit, self.window = limit, window
        self.failures = defaultdict(list)
        self.lock = threading.Lock()

    def blocked(self, key):
        with self.lock:
            cutoff = time.monotonic() - self.window
            self.failures[key] = [t for t in self.failures[key] if t > cutoff]
            return len(self.failures[key]) >= self.limit

    def fail(self, key):
        with self.lock:
            self.failures[key].append(time.monotonic())

    def reset(self, key):
        with self.lock:
            self.failures.pop(key, None)


throttle = LoginThrottle()
