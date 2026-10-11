"""User management, audit log, backups and data retention (admin only)."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import auth, db, storage
from ..config import get_config
from ..events import bus
from ..services import models, vision_review_status

router = APIRouter(prefix="/api", tags=["admin"])


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9._@-]+$")
    name: str = Field(default="", max_length=100)
    role: str
    password: str = Field(min_length=10, max_length=200)


class UserPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    role: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)


class RetentionIn(BaseModel):
    days: int = Field(ge=1, le=3650)
    dry_run: bool = True


@router.get("/users")
def list_users(user=Depends(auth.require("coordinator"))):
    """Coordinators need the roster to build crews; only admins can change it."""
    with db.connect() as conn:
        rows = db.all_rows(conn, "SELECT * FROM users ORDER BY active DESC, name")
    return [auth.public_user(r) for r in rows]


@router.post("/admin/users")
def create_user(body: UserIn, user=Depends(auth.require("admin"))):
    if body.role not in auth.ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(auth.ROLES)}.")
    with db.connect() as conn:
        if db.one(conn, "SELECT id FROM users WHERE username = ?", (body.username,)):
            raise HTTPException(409, "That username is taken.")
        user_id = db.insert(conn, "users", {"username": body.username, "name": body.name or body.username,
                                            "password_hash": auth.hash_password(body.password), "role": body.role,
                                            "created_at": db.now()})
        db.audit(conn, user["id"], "user.created", "user", user_id, username=body.username, role=body.role)
        return auth.public_user(db.one(conn, "SELECT * FROM users WHERE id = ?", (user_id,)))


@router.patch("/admin/users/{user_id}")
def update_user(user_id: int, body: UserPatch, user=Depends(auth.require("admin"))):
    values = body.model_dump(exclude_unset=True)
    if "role" in values and values["role"] not in auth.ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(auth.ROLES)}.")
    with db.connect() as conn:
        target = db.one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
        if not target:
            raise HTTPException(404, "User not found.")
        demoting = values.get("role", "admin") != "admin" or values.get("active") is False
        if target["role"] == "admin" and demoting:
            admins = db.one(conn, "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1")["n"]
            if admins <= 1:
                raise HTTPException(400, "Keep at least one active administrator.")
        if "password" in values:
            values["password_hash"] = auth.hash_password(values.pop("password"))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        if values.get("active") is False:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        if "active" in values:
            values["active"] = int(values["active"])
        db.update(conn, "users", user_id, values)
        db.audit(conn, user["id"], "user.updated", "user", user_id,
                 **{k: v for k, v in values.items() if k != "password_hash"})
        return auth.public_user(db.one(conn, "SELECT * FROM users WHERE id = ?", (user_id,)))


@router.get("/admin/audit")
def audit_log(limit: int = 200, user=Depends(auth.require("admin"))):
    with db.connect() as conn:
        rows = db.all_rows(conn, """SELECT a.*, u.name AS user FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
                                   ORDER BY a.id DESC LIMIT ?""", (min(max(limit, 1), 2000),))
    return [dict(r, detail=db.loads(r.pop("detail_json"), {})) for r in rows]


@router.get("/admin/system")
def system(user=Depends(auth.require("admin"))):
    cfg = get_config()
    with db.connect() as conn:
        counts = db.one(conn, """SELECT (SELECT COUNT(*) FROM users) users, (SELECT COUNT(*) FROM incidents) incidents,
            (SELECT COUNT(*) FROM observations) observations, (SELECT COUNT(*) FROM images) images""")
    return {
        "data_dir": str(cfg.data_dir), "models_dir": str(cfg.models_dir), "disk_bytes": storage.disk_usage(),
        "counts": counts, "models": models.available_models(), "retention_days": cfg.retention_days,
        "integrations": {"osrm": bool(cfg.osrm_url), "slack": bool(cfg.slack_webhook), "webhook": bool(cfg.webhook_url),
                         "vision_review": vision_review_status(), "public_url": cfg.public_base_url or None},
        "backups": list_backup_files(),
    }


def _backup_dir():
    folder = get_config().data_dir / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def list_backup_files():
    return [{"name": p.name, "size": p.stat().st_size,
             "created_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
            for p in sorted(_backup_dir().glob("dra-backup-*.zip"), reverse=True)]


def make_backup(include_images=True):
    """Consistent SQLite snapshot (online backup API) plus stored imagery, zipped."""
    cfg = get_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = _backup_dir()
    snapshot = folder / f"snapshot-{stamp}.sqlite3"
    src = sqlite3.connect(cfg.db_path)
    dst = sqlite3.connect(snapshot)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    target = folder / f"dra-backup-{stamp}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(snapshot, "dra.sqlite3")
        if include_images:
            for sub in ("images", "overlays"):
                base = cfg.data_dir / sub
                for path in base.rglob("*") if base.exists() else []:
                    if path.is_file():
                        zf.write(path, str(path.relative_to(cfg.data_dir)))
    snapshot.unlink(missing_ok=True)
    return target


@router.post("/admin/backups")
def create_backup(include_images: bool = True, user=Depends(auth.require("admin"))):
    target = make_backup(include_images)
    with db.connect() as conn:
        db.audit(conn, user["id"], "backup.created", file=target.name)
    return {"name": target.name, "size": target.stat().st_size}


@router.get("/admin/backups/{name}")
def download_backup(name: str, user=Depends(auth.require("admin"))):
    if Path(name).name != name or not name.startswith("dra-backup-") or not name.endswith(".zip"):
        raise HTTPException(404, "Backup not found.")
    path = _backup_dir() / name
    if not path.is_file():
        raise HTTPException(404, "Backup not found.")
    return FileResponse(path, media_type="application/zip", filename=name)


def apply_retention(days, dry_run=True):
    """Delete CLOSED incidents (and their files) whose last activity is older than `days`."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        rows = db.all_rows(conn, """SELECT i.id, i.name FROM incidents i WHERE i.status = 'closed' AND
            COALESCE((SELECT MAX(created_at) FROM runs WHERE incident_id = i.id), i.created_at) < ?""", (cutoff,))
        if not dry_run:
            for row in rows:
                conn.execute("DELETE FROM incidents WHERE id = ?", (row["id"],))
    if not dry_run:
        for row in rows:
            storage.remove_tree(f"images/incident-{row['id']}")
            storage.remove_tree(f"overlays/incident-{row['id']}")
    return rows


@router.post("/admin/retention")
def retention(body: RetentionIn, user=Depends(auth.require("admin"))):
    rows = apply_retention(body.days, body.dry_run)
    if not body.dry_run:
        with db.connect() as conn:
            db.audit(conn, user["id"], "retention.applied", days=body.days, deleted=[r["id"] for r in rows])
    return {"dry_run": body.dry_run, "incidents": rows}


# ---------- live events ----------

@router.get("/events")
async def events(request: Request, incident_id: int | None = None, user=Depends(auth.current_user)):
    sub = bus.subscribe(incident_id)
    queue = sub[0]

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {message}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            bus.unsubscribe(sub)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
