"""Expiring read-only share links: map, summary and exports for partner agencies without accounts."""

from datetime import datetime, timedelta, timezone
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import auth, db
from ..common import OBS_SELECT, get_or_404, observation_out
from ..config import get_config
from ..services import buildings
from . import geo
from .assessment import image_file
from .exports import build_export
from .incidents import summary

router = APIRouter(prefix="/api", tags=["share"])


class ShareIn(BaseModel):
    label: str = Field(default="", max_length=100)
    hours: int = Field(default=72, ge=1, le=24 * 30)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def link_for_token(token):
    with db.connect() as conn:
        row = db.one(conn, "SELECT * FROM share_links WHERE token_hash = ?", (_hash(token),))
    if not row or row["revoked"] or row["expires_at"] < db.now():
        raise HTTPException(404, "This share link is invalid or has expired.")
    return row


def incident_for_token(token):
    return link_for_token(token)["incident_id"]


@router.post("/incidents/{incident_id}/share")
def create_share(incident_id: int, body: ShareIn, user=Depends(auth.require("coordinator"))):
    token = secrets.token_urlsafe(24)
    expires = (datetime.now(timezone.utc) + timedelta(hours=body.hours)).isoformat(timespec="seconds")
    with db.connect() as conn:
        get_or_404(conn, "incidents", incident_id)
        db.insert(conn, "share_links", {"token_hash": _hash(token), "incident_id": incident_id, "label": body.label,
                                        "created_by": user["id"], "created_at": db.now(), "expires_at": expires})
        db.audit(conn, user["id"], "share.created", "incident", incident_id, label=body.label, expires=expires)
    base = get_config().public_base_url
    return {"token": token, "path": f"/share/{token}", "url": f"{base}/share/{token}" if base else None,
            "expires_at": expires}


@router.get("/incidents/{incident_id}/share")
def list_shares(incident_id: int, user=Depends(auth.require("coordinator"))):
    with db.connect() as conn:
        rows = db.all_rows(conn, """SELECT s.*, u.name AS creator FROM share_links s LEFT JOIN users u ON u.id = s.created_by
                                   WHERE s.incident_id = ? ORDER BY s.created_at DESC""", (incident_id,))
    return [{"id": r["token_hash"][:16], "label": r["label"], "creator": r["creator"], "created_at": r["created_at"],
             "expires_at": r["expires_at"], "active": not r["revoked"] and r["expires_at"] >= db.now()} for r in rows]


@router.delete("/share/{link_id}")
def revoke_share(link_id: str, user=Depends(auth.require("coordinator"))):
    if len(link_id) != 16:
        raise HTTPException(400, "Unknown link.")
    with db.connect() as conn:
        cur = conn.execute("UPDATE share_links SET revoked = 1 WHERE substr(token_hash, 1, 16) = ?", (link_id,))
        if not cur.rowcount:
            raise HTTPException(404, "Unknown link.")
        db.audit(conn, user["id"], "share.revoked", detail_id=link_id)
    return {"ok": True}


# ---------- public, token-scoped ----------

@router.get("/public/{token}")
def public_incident(token: str):
    link = link_for_token(token)
    incident_id = link["incident_id"]
    with db.connect() as conn:
        incident = get_or_404(conn, "incidents", incident_id)
        info = summary(conn, incident_id)
        overlays = db.all_rows(conn, "SELECT id, kind, name, bounds_json FROM overlays WHERE incident_id = ?",
                               (incident_id,))
    info.pop("tasks", None)
    return {"incident": {k: incident[k] for k in ("id", "name", "description", "center_lat", "center_lon", "status")},
            "summary": info, "overlays": [{"id": o["id"], "kind": o["kind"], "name": o["name"],
                                           "bounds": db.loads(o["bounds_json"])} for o in overlays],
            "config": {"tile_url": get_config().tile_url, "tile_attribution": get_config().tile_attribution},
            "generated_at": db.now(), "expires_at": link["expires_at"], "label": link["label"]}


@router.get("/public/{token}/observations")
def public_observations(token: str):
    incident_id = incident_for_token(token)
    with db.connect() as conn:
        rows = db.all_rows(conn, OBS_SELECT + " WHERE o.incident_id = ? AND o.status != 'rejected'", (incident_id,))
    out = []
    for r in rows:
        o = observation_out(r)
        for key in ("reviewer_id", "reviewer", "review_note", "task_id", "task_status", "vision"):
            o.pop(key, None)
        out.append(o)
    return out


@router.get("/public/{token}/buildings")
def public_buildings(token: str):
    return buildings.building_rollup(incident_for_token(token))


@router.get("/public/{token}/footprints")
def public_footprints(token: str):
    return geo.footprints_geojson(incident_for_token(token))


@router.get("/public/{token}/images/{image_id}")
def public_image(token: str, image_id: int, variant: str = "preview"):
    incident_id = incident_for_token(token)
    row, response = image_file(image_id, variant)
    if row["incident_id"] != incident_id or row["kind"] == "field_photo":
        raise HTTPException(404, "Image not found.")
    return response


@router.get("/public/{token}/overlays/{overlay_id}")
def public_overlay(token: str, overlay_id: int):
    incident_id = incident_for_token(token)
    row, response = geo.overlay_file(overlay_id)
    if row["incident_id"] != incident_id:
        raise HTTPException(404, "Overlay not found.")
    return response


@router.get("/public/{token}/export/{fmt}")
def public_export(token: str, fmt: str):
    if fmt == "pdf":
        raise HTTPException(404, "Reports are not available on shared links.")
    return build_export(incident_for_token(token), fmt, public=True)
