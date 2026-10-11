from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import auth, db
from ..config import get_config

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=2, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class Setup(Credentials):
    name: str = Field(default="", max_length=100)


class PasswordChange(BaseModel):
    current: str
    new: str = Field(min_length=10, max_length=200)


def _set_cookie(response, token, expires):
    response.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=get_config().secure_cookies,
                        expires=expires, path="/")


def client_config():
    from ..services import vision_review_status

    cfg = get_config()
    return {
        "no_login": cfg.no_login,
        "tile_url": cfg.tile_url, "tile_attribution": cfg.tile_attribution,
        "integrations": {
            "osrm": bool(cfg.osrm_url), "slack": bool(cfg.slack_webhook), "webhook": bool(cfg.webhook_url),
            "vision_review": vision_review_status(),
        },
    }


@router.get("/status")
def status(request: Request):
    with db.connect() as conn:
        has_users = db.one(conn, "SELECT COUNT(*) AS n FROM users")["n"] > 0
    if get_config().no_login:
        return {"setup_required": False, "user": auth.public_user(auth.local_user()), "config": client_config()}
    user = auth.user_for_token(auth.request_token(request))
    return {"setup_required": not has_users, "user": auth.public_user(user), "config": client_config()}


@router.post("/setup")
def setup(body: Setup, response: Response):
    """Create the first administrator. Only possible while no user exists."""
    try:
        password_hash = auth.hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with db.connect() as conn:
        if db.one(conn, "SELECT COUNT(*) AS n FROM users")["n"]:
            raise HTTPException(409, "Setup is already complete. Sign in instead.")
        user_id = db.insert(conn, "users", {"username": body.username.strip(), "name": body.name.strip() or body.username,
                                            "password_hash": password_hash, "role": "admin", "created_at": db.now()})
        token, expires = auth.create_session(conn, user_id)
        db.audit(conn, user_id, "setup.admin_created", "user", user_id)
        user = db.one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
    _set_cookie(response, token, expires)
    return {"user": auth.public_user(user)}


@router.post("/login")
def login(body: Credentials, request: Request, response: Response):
    key = f"{body.username.lower()}|{request.client.host if request.client else ''}"
    if auth.throttle.blocked(key):
        raise HTTPException(429, "Too many failed attempts. Wait 10 minutes and try again.")
    with db.connect() as conn:
        user = db.one(conn, "SELECT * FROM users WHERE username = ?", (body.username.strip(),))
        if not user or not user["active"] or not auth.verify_password(body.password, user["password_hash"]):
            auth.throttle.fail(key)
            raise HTTPException(401, "Incorrect username or password.")
        auth.throttle.reset(key)
        token, expires = auth.create_session(conn, user["id"])
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
        db.audit(conn, user["id"], "auth.login", "user", user["id"])
    _set_cookie(response, token, expires)
    return {"user": auth.public_user(user)}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = auth.request_token(request)
    if token:
        with db.connect() as conn:
            auth.end_session(conn, token)
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.post("/password")
def change_password(body: PasswordChange, user=Depends(auth.current_user)):
    if not auth.verify_password(body.current, user["password_hash"]):
        raise HTTPException(400, "Current password is incorrect.")
    try:
        new_hash = auth.hash_password(body.new)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with db.connect() as conn:
        db.update(conn, "users", user["id"], {"password_hash": new_hash})
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        db.audit(conn, user["id"], "auth.password_changed", "user", user["id"])
    return {"ok": True, "message": "Password changed. Sign in again."}
