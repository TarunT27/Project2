"""Runs (uploads + background assessment), images, observations and the review workflow."""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from utils.geo_utils import valid_coordinate
from utils.pipeline import IMAGE_TYPES, VIDEO_TYPES

from .. import auth, db, storage
from ..common import (CLASSES, OBS_SELECT, clean_hazards, get_or_404, image_out, observation_out, run_out)
from ..events import bus
from ..jobs import runner
from ..services import models, vision_review_status
from ..services.assess import safe_name

router = APIRouter(prefix="/api", tags=["assessment"])
MAX_FILES = 40
MAX_BYTES = 2 * 1024**3  # 2 GB per run (orthomosaics and drone video are large)
ACCEPTED = IMAGE_TYPES | VIDEO_TYPES | {".srt"}


@router.get("/models")
def list_models(user=Depends(auth.current_user)):
    return {"modes": models.MODES, "models": models.available_models(), "vision_review": vision_review_status()}


@router.get("/incidents/{incident_id}/runs")
def list_runs(incident_id: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        get_or_404(conn, "incidents", incident_id)
        rows = db.all_rows(conn, """SELECT r.*, u.name AS created_by_name,
            (SELECT COUNT(*) FROM observations o WHERE o.run_id = r.id) AS obs_count
            FROM runs r LEFT JOIN users u ON u.id = r.created_by WHERE r.incident_id = ? ORDER BY r.id DESC""",
                           (incident_id,))
    return [run_out(r) for r in rows]


@router.post("/incidents/{incident_id}/runs")
async def create_run(
    incident_id: int,
    files: list[UploadFile] = File(default=[]),
    mode: str = Form("demo"),
    threshold: float = Form(0.5),
    model: str = Form(""),
    building_model: str = Form(""),
    damage_model: str = Form(""),
    device: str = Form(""),
    interval: float = Form(3),
    max_frames: int = Form(8),
    manual_lat: float | None = Form(None),
    manual_lon: float | None = Form(None),
    vision_review: bool = Form(False),
    builtin_demo: bool = Form(False),
    user=Depends(auth.require("reviewer")),
):
    if mode not in models.MODES:
        raise HTTPException(400, f"Mode must be one of {', '.join(models.MODES)}.")
    if not 0 <= threshold <= 1:
        raise HTTPException(400, "Threshold must be between 0 and 1.")
    if not 1 <= interval <= 30 or not 1 <= max_frames <= 30:
        raise HTTPException(400, "Video sampling must be 1–30 s and 1–30 frames.")
    if device and device not in {"cpu", "mps", "cuda:0"}:
        raise HTTPException(400, "Device must be cpu, mps or cuda:0.")
    manual = None
    if manual_lat is not None or manual_lon is not None:
        if not valid_coordinate(manual_lat, manual_lon):
            raise HTTPException(400, "Manual reference coordinates are invalid.")
        manual = [manual_lat, manual_lon]
    if builtin_demo and mode != "demo":
        raise HTTPException(400, "The built-in demo scenes run in demo mode only.")
    files = [f for f in files if f.filename]
    if not files and not builtin_demo:
        raise HTTPException(400, "Add at least one image, video or GeoTIFF.")
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"Limit a run to {MAX_FILES} files.")
    settings = {"interval": interval, "max_frames": max_frames, "manual": manual, "vision_review": vision_review,
                "builtin_demo": builtin_demo, "model": model, "building_model": building_model,
                "damage_model": damage_model, "device": device}
    try:
        if mode == "single":
            models.model_path(model)
        if mode == "two_stage":
            models.model_path(building_model)
            models.model_path(damage_model)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with db.connect() as conn:
        get_or_404(conn, "incidents", incident_id)
        run_id = db.insert(conn, "runs", {
            "incident_id": incident_id, "mode": mode, "model": models.describe(mode, settings), "threshold": threshold,
            "settings_json": json.dumps(settings), "created_by": user["id"], "created_at": db.now(),
            "message": "Uploading", "status": "queued",
        })
    total = 0
    folder = storage.upload_dir(run_id)
    try:
        for index, upload in enumerate(files):
            name = safe_name(upload.filename)
            if Path(name).suffix.lower() not in ACCEPTED:
                raise HTTPException(400, f"{name}: unsupported file type.")
            data = await upload.read()
            total += len(data)
            if total > MAX_BYTES:
                raise HTTPException(413, "Limit a run to 2 GB of uploads.")
            storage.save_bytes(data, f"{folder}/{index:03d}__{name}")
    except HTTPException:
        storage.remove_tree(folder)
        with db.connect() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        raise
    with db.connect() as conn:
        db.update(conn, "runs", run_id, {"message": "Queued", "total_inputs": len(files) or 3})
        db.audit(conn, user["id"], "run.created", "run", run_id, mode=mode, files=len(files))
        row = db.one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
    bus.publish("run.queued", incident_id, run_id=run_id)
    runner.submit(run_id)
    return run_out(row)


@router.get("/runs/{run_id}")
def get_run(run_id: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        row = db.one(conn, """SELECT r.*, (SELECT COUNT(*) FROM observations o WHERE o.run_id = r.id) AS obs_count
                              FROM runs r WHERE r.id = ?""", (run_id,))
    if not row:
        raise HTTPException(404, "Run not found.")
    return run_out(row)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, user=Depends(auth.require("reviewer"))):
    with db.connect() as conn:
        row = get_or_404(conn, "runs", run_id)
        if row["status"] not in ("queued", "running"):
            raise HTTPException(409, "Only queued or running assessments can be cancelled.")
        if row["status"] == "queued":
            db.update(conn, "runs", run_id, {"status": "cancelled", "message": "Cancelled", "finished_at": db.now()})
    runner.cancel(run_id)
    return {"ok": True}


# ---------- images ----------

@router.get("/incidents/{incident_id}/images")
def list_images(incident_id: int, run_id: int | None = None, kind: str | None = None,
                user=Depends(auth.current_user)):
    sql = """SELECT i.*, (SELECT COUNT(*) FROM observations o WHERE o.image_id = i.id) AS obs_count,
             (SELECT COUNT(*) FROM observations o WHERE o.image_id = i.id AND o.status = 'not_reviewed') AS unreviewed
             FROM images i WHERE i.incident_id = ?"""
    params = [incident_id]
    if run_id is not None:
        sql += " AND i.run_id = ?"
        params.append(run_id)
    if kind:
        sql += " AND i.kind = ?"
        params.append(kind)
    with db.connect() as conn:
        rows = db.all_rows(conn, sql + " ORDER BY i.id", params)
    return [image_out(r) for r in rows]


def image_file(image_id, variant):
    with db.connect() as conn:
        row = db.one(conn, "SELECT * FROM images WHERE id = ?", (image_id,))
    if not row:
        raise HTTPException(404, "Image not found.")
    rel = row["preview_path"] if variant == "preview" else row["path"]
    try:
        path = storage.resolve(rel)
    except ValueError:
        raise HTTPException(404, "Image not found.")
    if not path.is_file():
        raise HTTPException(404, "Image file is missing.")
    return row, FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/images/{image_id}/file")
def get_image_file(image_id: int, variant: str = "full", user=Depends(auth.current_user)):
    return image_file(image_id, variant)[1]


# ---------- observations & review ----------

class ReviewIn(BaseModel):
    status: str | None = None
    damage_class: str | None = None
    bbox: list[int] | None = Field(default=None, min_length=4, max_length=4)
    review_note: str | None = Field(default=None, max_length=2000)
    hazards: list[str] | None = None


class BulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=2000)
    action: str


class ManualIn(BaseModel):
    bbox: list[int] = Field(min_length=4, max_length=4)
    damage_class: str
    review_note: str = Field(default="", max_length=2000)
    hazards: list[str] = []


def _query_observations(conn, incident_id, status=None, cls=None, min_conf=0.0, run_id=None, image_id=None,
                        include_rejected=True, limit=5000):
    sql = OBS_SELECT + " WHERE o.incident_id = ? AND o.confidence >= ?"
    params = [incident_id, min_conf]
    for column, value in (("o.status", status), ("o.final_class", cls), ("o.run_id", run_id), ("o.image_id", image_id)):
        if value not in (None, ""):
            sql += f" AND {column} = ?"
            params.append(value)
    if not include_rejected:
        sql += " AND o.status != 'rejected'"
    sql += """ ORDER BY CASE o.final_class WHEN 'Destroyed' THEN 0 WHEN 'Major Damage' THEN 1
               WHEN 'Minor Damage' THEN 2 ELSE 3 END, o.confidence DESC, o.id LIMIT ?"""
    params.append(limit)
    return db.all_rows(conn, sql, params)


@router.get("/incidents/{incident_id}/observations")
def list_observations(incident_id: int, status: str | None = None, cls: str | None = None, min_conf: float = 0,
                      run_id: int | None = None, image_id: int | None = None, include_rejected: bool = True,
                      user=Depends(auth.current_user)):
    with db.connect() as conn:
        get_or_404(conn, "incidents", incident_id)
        rows = _query_observations(conn, incident_id, status, cls, min_conf, run_id, image_id, include_rejected)
    return [observation_out(r) for r in rows]


@router.get("/observations/{obs_id}")
def get_observation(obs_id: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        row = db.one(conn, OBS_SELECT + " WHERE o.id = ?", (obs_id,))
        if not row:
            raise HTTPException(404, "Observation not found.")
        history = db.all_rows(conn, """SELECT e.action, e.before_json, e.after_json, e.created_at, u.name AS user
                                      FROM review_events e LEFT JOIN users u ON u.id = e.user_id
                                      WHERE e.observation_id = ? ORDER BY e.id""", (obs_id,))
    out = observation_out(row)
    out["history"] = [{"action": h["action"], "before": db.loads(h["before_json"]), "after": db.loads(h["after_json"]),
                       "user": h["user"], "at": h["created_at"]} for h in history]
    return out


def _review_snapshot(row):
    return {"status": row["status"], "damage_class": row["final_class"], "bbox": db.loads(row["bbox_json"]),
            "review_note": row["review_note"], "hazards": db.loads(row["hazards_json"], [])}


def apply_review(conn, row, body: ReviewIn, user):
    values = {}
    if body.damage_class is not None:
        if body.damage_class not in CLASSES:
            raise HTTPException(400, f"Class must be one of {', '.join(CLASSES)}.")
        values["final_class"] = body.damage_class
    if body.bbox is not None:
        x1, y1, x2, y2 = body.bbox
        if x2 <= x1 or y2 <= y1 or min(body.bbox) < 0:
            raise HTTPException(400, "Box must have positive width and height.")
        values["bbox_json"] = json.dumps(body.bbox)
    if body.review_note is not None:
        values["review_note"] = body.review_note.strip()
    if body.hazards is not None:
        values["hazards_json"] = json.dumps(clean_hazards(body.hazards))
    status = body.status
    if status is None and ("final_class" in values or "bbox_json" in values):
        status = "corrected" if row["status"] != "added" else "added"
    if status is not None:
        if status not in ("not_reviewed", "confirmed", "corrected", "rejected"):
            raise HTTPException(400, "Status must be not_reviewed, confirmed, corrected or rejected.")
        if status == "confirmed" and values.get("final_class", row["final_class"]) != row["model_class"]:
            status = "corrected"
        if row["status"] == "added" and status in ("confirmed", "corrected"):
            status = "added"
        values["status"] = status
        if status == "not_reviewed":
            values.update(reviewer_id=None, reviewed_at=None, final_class=row["model_class"])
        else:
            values.update(reviewer_id=user["id"], reviewed_at=db.now())
    if not values:
        raise HTTPException(400, "Nothing to change.")
    before = _review_snapshot(row)
    db.update(conn, "observations", row["id"], values)
    after_row = db.one(conn, "SELECT * FROM observations WHERE id = ?", (row["id"],))
    db.insert(conn, "review_events", {"observation_id": row["id"], "user_id": user["id"],
                                      "action": values.get("status", "edit"), "before_json": json.dumps(before),
                                      "after_json": json.dumps(_review_snapshot(after_row)), "created_at": db.now()})
    return after_row


@router.patch("/observations/{obs_id}")
def review_observation(obs_id: int, body: ReviewIn, user=Depends(auth.require("reviewer"))):
    with db.connect() as conn:
        row = get_or_404(conn, "observations", obs_id, "Observation")
        apply_review(conn, row, body, user)
        out = observation_out(db.one(conn, OBS_SELECT + " WHERE o.id = ?", (obs_id,)))
    bus.publish("observation.updated", out["incident_id"], observation=out, by=user["name"])
    return out


@router.post("/observations/bulk")
def bulk_review(body: BulkIn, user=Depends(auth.require("reviewer"))):
    actions = {"confirm": "confirmed", "reject": "rejected", "reset": "not_reviewed"}
    if body.action not in actions:
        raise HTTPException(400, "Action must be confirm, reject or reset.")
    incidents, changed = set(), 0
    with db.connect() as conn:
        for obs_id in body.ids:
            row = db.one(conn, "SELECT * FROM observations WHERE id = ?", (obs_id,))
            if not row or (body.action == "confirm" and row["status"] in ("corrected", "added")):
                continue
            apply_review(conn, row, ReviewIn(status=actions[body.action]), user)
            incidents.add(row["incident_id"])
            changed += 1
    for incident_id in incidents:
        bus.publish("observations.bulk", incident_id, action=body.action, count=changed, by=user["name"])
    return {"changed": changed}


@router.post("/images/{image_id}/observations")
def add_observation(image_id: int, body: ManualIn, user=Depends(auth.require("reviewer"))):
    """A reviewer draws a building the model missed."""
    if body.damage_class not in CLASSES:
        raise HTTPException(400, f"Class must be one of {', '.join(CLASSES)}.")
    x1, y1, x2, y2 = body.bbox
    with db.connect() as conn:
        image = get_or_404(conn, "images", image_id, "Image")
        if x2 <= x1 or y2 <= y1 or x1 < 0 or y1 < 0 or x2 > image["width"] or y2 > image["height"]:
            raise HTTPException(400, "Box must lie inside the image.")
        lat, lon, source = image["latitude"], image["longitude"], image["location_source"]
        georef = db.loads(image["georef_json"])
        if georef:
            from ..services import ortho
            scale = georef["width"] / image["width"]
            lat, lon = ortho.pixel_to_lonlat(georef, (x1 + x2) / 2 * scale, (y1 + y2) / 2 * scale)
            source = "ortho_georef"
        else:
            from ..services.assess import project_detection
            hit = project_detection(body.bbox, image["width"], image["height"], db.loads(image["telemetry_json"]))
            if hit:
                lat, lon, source = hit[0], hit[1], "telemetry_projection"
        obs_id = db.insert(conn, "observations", {
            "run_id": image["run_id"], "incident_id": image["incident_id"], "image_id": image_id,
            "bbox_json": json.dumps(body.bbox), "model_class": body.damage_class, "final_class": body.damage_class,
            "confidence": 1.0, "scope": "reviewer_added", "latitude": lat, "longitude": lon,
            "location_source": source if lat is not None else "missing", "is_demo": 0, "model": "reviewer",
            "status": "added", "reviewer_id": user["id"], "reviewed_at": db.now(),
            "review_note": body.review_note.strip(), "hazards_json": json.dumps(clean_hazards(body.hazards)),
            "created_at": db.now(),
        })
        db.insert(conn, "review_events", {"observation_id": obs_id, "user_id": user["id"], "action": "added",
                                          "after_json": json.dumps({"damage_class": body.damage_class, "bbox": body.bbox}),
                                          "created_at": db.now()})
        out = observation_out(db.one(conn, OBS_SELECT + " WHERE o.id = ?", (obs_id,)))
    bus.publish("observation.updated", out["incident_id"], observation=out, by=user["name"])
    return out
