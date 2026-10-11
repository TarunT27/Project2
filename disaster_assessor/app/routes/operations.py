"""Crews, rescue tasks, route planning and field reports (with offline-sync idempotency)."""

import json
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from utils.geo_utils import valid_coordinate

from .. import auth, db, storage
from ..common import (SEVERITY, TASK_SELECT, TASK_STATUSES, clean_hazards, get_or_404, task_out)
from ..config import get_config
from ..events import bus

router = APIRouter(prefix="/api", tags=["operations"])
FIELD_STATUSES = ("in_progress", "searched_clear", "needs_usar", "done")


# ---------- crews ----------

class CrewIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    contact: str = Field(default="", max_length=200)
    member_ids: list[int] = []


def _crew_out(conn, row):
    members = db.all_rows(conn, """SELECT u.id, u.name, u.username, u.role FROM crew_members m
                                  JOIN users u ON u.id = m.user_id WHERE m.crew_id = ? ORDER BY u.name""", (row["id"],))
    open_tasks = db.one(conn, "SELECT COUNT(*) AS n FROM tasks WHERE crew_id = ? AND status NOT IN ('done','cancelled')",
                        (row["id"],))["n"]
    return {"id": row["id"], "incident_id": row["incident_id"], "name": row["name"], "contact": row["contact"],
            "members": members, "open_tasks": open_tasks}


def _set_members(conn, crew_id, member_ids):
    conn.execute("DELETE FROM crew_members WHERE crew_id = ?", (crew_id,))
    for user_id in set(member_ids):
        if not db.one(conn, "SELECT id FROM users WHERE id = ?", (user_id,)):
            raise HTTPException(400, f"User {user_id} does not exist.")
        conn.execute("INSERT INTO crew_members (crew_id, user_id) VALUES (?, ?)", (crew_id, user_id))


@router.get("/incidents/{incident_id}/crews")
def list_crews(incident_id: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        rows = db.all_rows(conn, "SELECT * FROM crews WHERE incident_id = ? ORDER BY name", (incident_id,))
        return [_crew_out(conn, r) for r in rows]


@router.post("/incidents/{incident_id}/crews")
def create_crew(incident_id: int, body: CrewIn, user=Depends(auth.require("coordinator"))):
    with db.connect() as conn:
        get_or_404(conn, "incidents", incident_id)
        crew_id = db.insert(conn, "crews", {"incident_id": incident_id, "name": body.name.strip(),
                                            "contact": body.contact.strip(), "created_at": db.now()})
        _set_members(conn, crew_id, body.member_ids)
        db.audit(conn, user["id"], "crew.created", "crew", crew_id, name=body.name)
        out = _crew_out(conn, get_or_404(conn, "crews", crew_id))
    bus.publish("crews.updated", incident_id)
    return out


@router.patch("/crews/{crew_id}")
def update_crew(crew_id: int, body: CrewIn, user=Depends(auth.require("coordinator"))):
    with db.connect() as conn:
        row = get_or_404(conn, "crews", crew_id)
        db.update(conn, "crews", crew_id, {"name": body.name.strip(), "contact": body.contact.strip()})
        _set_members(conn, crew_id, body.member_ids)
        out = _crew_out(conn, get_or_404(conn, "crews", crew_id))
    bus.publish("crews.updated", row["incident_id"])
    return out


@router.delete("/crews/{crew_id}")
def delete_crew(crew_id: int, user=Depends(auth.require("coordinator"))):
    with db.connect() as conn:
        row = get_or_404(conn, "crews", crew_id)
        conn.execute("UPDATE tasks SET status = 'open' WHERE crew_id = ? AND status = 'assigned'", (crew_id,))
        conn.execute("DELETE FROM crews WHERE id = ?", (crew_id,))
        db.audit(conn, user["id"], "crew.deleted", "crew", crew_id, name=row["name"])
    bus.publish("crews.updated", row["incident_id"])
    return {"ok": True}


# ---------- tasks ----------

class TaskCreate(BaseModel):
    observation_ids: list[int] = []
    high_priority: bool = False
    title: str | None = Field(default=None, max_length=200)
    latitude: float | None = None
    longitude: float | None = None
    damage_class: str | None = None
    crew_id: int | None = None


class TaskPatch(BaseModel):
    status: str | None = None
    crew_id: int | None = None
    notes: str | None = Field(default=None, max_length=4000)
    priority: int | None = None
    hazards: list[str] | None = None


def _user_crew_ids(conn, user_id):
    return {r["crew_id"] for r in db.all_rows(conn, "SELECT crew_id FROM crew_members WHERE user_id = ?", (user_id,))}


def _task(conn, task_id):
    row = db.one(conn, TASK_SELECT + " WHERE t.id = ?", (task_id,))
    if not row:
        raise HTTPException(404, "Task not found.")
    return row


@router.get("/incidents/{incident_id}/tasks")
def list_tasks(incident_id: int, status: str | None = None, crew_id: int | None = None,
               user=Depends(auth.current_user)):
    sql, params = TASK_SELECT + " WHERE t.incident_id = ?", [incident_id]
    if status:
        sql += " AND t.status = ?"
        params.append(status)
    if crew_id:
        sql += " AND t.crew_id = ?"
        params.append(crew_id)
    with db.connect() as conn:
        rows = db.all_rows(conn, sql + " ORDER BY t.priority DESC, t.id", params)
    return [task_out(r) for r in rows]


@router.post("/incidents/{incident_id}/tasks")
def create_tasks(incident_id: int, body: TaskCreate, user=Depends(auth.require("coordinator"))):
    created = []
    with db.connect() as conn:
        get_or_404(conn, "incidents", incident_id)
        if body.crew_id is not None:
            crew = get_or_404(conn, "crews", body.crew_id)
            if crew["incident_id"] != incident_id:
                raise HTTPException(400, "That crew belongs to another incident.")
        ids = list(body.observation_ids)
        if body.high_priority:
            ids += [r["id"] for r in db.all_rows(conn, """SELECT id FROM observations WHERE incident_id = ?
                AND final_class IN ('Destroyed','Major Damage') AND status != 'rejected'
                AND id NOT IN (SELECT observation_id FROM tasks WHERE observation_id IS NOT NULL)""", (incident_id,))]
        status = "assigned" if body.crew_id else "open"
        for obs_id in dict.fromkeys(ids):
            obs = db.one(conn, "SELECT o.*, i.source_file FROM observations o JOIN images i ON i.id = o.image_id "
                               "WHERE o.id = ? AND o.incident_id = ?", (obs_id, incident_id))
            if not obs or obs["status"] == "rejected":
                continue
            if db.one(conn, "SELECT id FROM tasks WHERE observation_id = ? AND status NOT IN ('done','cancelled')",
                      (obs_id,)):
                continue
            created.append(db.insert(conn, "tasks", {
                "incident_id": incident_id, "observation_id": obs_id,
                "title": f"{obs['final_class']} · {obs['source_file']}", "damage_class": obs["final_class"],
                "priority": SEVERITY[obs["final_class"]] * 100 + round(obs["confidence"] * 99),
                "status": status, "crew_id": body.crew_id, "latitude": obs["latitude"], "longitude": obs["longitude"],
                "hazards_json": obs["hazards_json"], "created_by": user["id"], "created_at": db.now(),
                "updated_at": db.now(),
            }))
        if body.title:
            if body.latitude is not None and not valid_coordinate(body.latitude, body.longitude):
                raise HTTPException(400, "Invalid task coordinates.")
            created.append(db.insert(conn, "tasks", {
                "incident_id": incident_id, "title": body.title.strip(), "damage_class": body.damage_class,
                "priority": SEVERITY.get(body.damage_class, 0) * 100, "status": status, "crew_id": body.crew_id,
                "latitude": body.latitude, "longitude": body.longitude, "created_by": user["id"],
                "created_at": db.now(), "updated_at": db.now(),
            }))
        db.audit(conn, user["id"], "tasks.created", "incident", incident_id, count=len(created))
        rows = [task_out(_task(conn, t)) for t in created]
    bus.publish("tasks.updated", incident_id, created=len(rows))
    return rows


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, body: TaskPatch, user=Depends(auth.require("field"))):
    values = body.model_dump(exclude_unset=True)
    with db.connect() as conn:
        row = _task(conn, task_id)
        if not auth.has_role(user, "coordinator"):
            # Field crews may only move their own tasks through field statuses and add notes.
            if row["crew_id"] not in _user_crew_ids(conn, user["id"]):
                raise HTTPException(403, "This task is not assigned to your crew.")
            if set(values) - {"status", "notes", "hazards"}:
                raise HTTPException(403, "Only a coordinator can reassign or reprioritise tasks.")
            if "status" in values and values["status"] not in FIELD_STATUSES:
                raise HTTPException(403, "Field crews can set in_progress, searched_clear, needs_usar or done.")
        if "status" in values and values["status"] not in TASK_STATUSES:
            raise HTTPException(400, f"Status must be one of {', '.join(TASK_STATUSES)}.")
        if "crew_id" in values and values["crew_id"] is not None:
            crew = get_or_404(conn, "crews", values["crew_id"])
            if crew["incident_id"] != row["incident_id"]:
                raise HTTPException(400, "That crew belongs to another incident.")
            if row["status"] == "open" and "status" not in values:
                values["status"] = "assigned"
        if "hazards" in values:
            values["hazards_json"] = json.dumps(clean_hazards(values.pop("hazards")))
        values["updated_at"] = db.now()
        db.update(conn, "tasks", task_id, values)
        db.audit(conn, user["id"], "task.updated", "task", task_id, **{k: v for k, v in values.items() if k != "notes"})
        out = task_out(_task(conn, task_id))
    bus.publish("task.updated", row["incident_id"], task=out, by=user["name"])
    return out


@router.get("/incidents/{incident_id}/route")
def plan_route(incident_id: int, crew_id: int | None = None, start_lat: float | None = None,
               start_lon: float | None = None, road: bool = False, user=Depends(auth.current_user)):
    from ..services import routing

    with db.connect() as conn:
        sql = TASK_SELECT + " WHERE t.incident_id = ? AND t.status NOT IN ('done','cancelled','searched_clear')"
        params = [incident_id]
        if crew_id:
            sql += " AND t.crew_id = ?"
            params.append(crew_id)
        tasks = db.all_rows(conn, sql, params)
    stops = [{"id": t["id"], "latitude": t["latitude"], "longitude": t["longitude"],
              "damage_class": t["damage_class"] or "No Damage"} for t in tasks]
    start = (start_lat, start_lon) if valid_coordinate(start_lat, start_lon) else None
    plan = routing.plan_route(start, stops)
    by_id = {t["id"]: t for t in tasks}
    plan["stops"] = [task_out(by_id[i]) for i in plan["order"]]
    cfg = get_config()
    if road and cfg.osrm_url and plan["order"]:
        points = ([start] if start else []) + [(by_id[i]["latitude"], by_id[i]["longitude"]) for i in plan["order"]]
        try:
            plan["road"] = routing.osrm_route(points[:100], cfg.osrm_url)
        except RuntimeError as exc:
            plan["road_error"] = str(exc)
    return plan


# ---------- field ----------

@router.get("/field/tasks")
def my_tasks(user=Depends(auth.current_user)):
    with db.connect() as conn:
        crews = _user_crew_ids(conn, user["id"])
        if auth.has_role(user, "coordinator") and not crews:
            rows = db.all_rows(conn, TASK_SELECT + """ JOIN incidents inc ON inc.id = t.incident_id
                WHERE inc.status = 'active' AND t.status NOT IN ('done','cancelled') ORDER BY t.priority DESC""")
        elif crews:
            marks = ",".join("?" * len(crews))
            rows = db.all_rows(conn, TASK_SELECT + f""" WHERE t.crew_id IN ({marks})
                AND t.status NOT IN ('cancelled') ORDER BY t.status = 'done', t.priority DESC""", tuple(crews))
        else:
            rows = []
        incidents = {r["id"]: r["name"] for r in db.all_rows(conn, "SELECT id, name FROM incidents")}
    out = [dict(task_out(r), incident=incidents.get(r["incident_id"])) for r in rows]
    return {"tasks": out, "crews": sorted(crews), "server_time": db.now()}


@router.post("/tasks/{task_id}/reports")
async def field_report(
    task_id: int,
    status: str = Form(...),
    client_id: str = Form(""),
    notes: str = Form(""),
    hazards: str = Form("[]"),
    latitude: float | None = Form(None),
    longitude: float | None = Form(None),
    reported_at: str = Form(""),
    photos: list[UploadFile] = File(default=[]),
    user=Depends(auth.require("field")),
):
    """Idempotent: a retried offline submission with the same client_id is acknowledged, not duplicated."""
    if status not in FIELD_STATUSES:
        raise HTTPException(400, f"Status must be one of {', '.join(FIELD_STATUSES)}.")
    try:
        hazard_list = clean_hazards(json.loads(hazards or "[]"))
    except ValueError:
        raise HTTPException(400, "Hazards must be a JSON list.")
    client_id = (client_id or str(uuid.uuid4()))[:64]
    if latitude is not None and not valid_coordinate(latitude, longitude):
        latitude = longitude = None
    photos = [p for p in photos if p.filename][:6]
    with db.connect() as conn:
        row = _task(conn, task_id)
        if not auth.has_role(user, "coordinator") and row["crew_id"] not in _user_crew_ids(conn, user["id"]):
            raise HTTPException(403, "This task is not assigned to your crew.")
        existing = db.one(conn, "SELECT id FROM field_reports WHERE client_id = ?", (client_id,))
        if existing:
            return {"ok": True, "duplicate": True, "task": task_out(row)}
        report_id = db.insert(conn, "field_reports", {
            "task_id": task_id, "user_id": user["id"], "client_id": client_id, "status": status,
            "notes": notes[:4000], "hazards_json": json.dumps(hazard_list), "latitude": latitude,
            "longitude": longitude, "reported_at": reported_at[:40] or db.now(), "created_at": db.now(),
        })
        merged = sorted(set(db.loads(row["hazards_json"], [])) | set(hazard_list))
        note_line = f"[{db.now()[:16]} {user['name']}] {status.replace('_', ' ')}" + (f": {notes.strip()}" if notes.strip() else "")
        db.update(conn, "tasks", task_id, {"status": status, "hazards_json": json.dumps(merged),
                                           "notes": (row["notes"] + "\n" + note_line).strip(), "updated_at": db.now()})
    for index, photo in enumerate(photos):
        await _save_field_photo(row, report_id, index, photo, latitude, longitude)
    with db.connect() as conn:
        out = task_out(_task(conn, task_id))
    bus.publish("task.updated", row["incident_id"], task=out, by=user["name"], field_report=True)
    return {"ok": True, "duplicate": False, "task": out}


async def _save_field_photo(task, report_id, index, upload, latitude, longitude):
    import io
    from PIL import Image, ImageOps

    data = await upload.read()
    if len(data) > 25 * 1024 * 1024:
        return
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            image = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        return
    image.thumbnail((1600, 1600))
    folder = f"images/incident-{task['incident_id']}/field"
    with db.connect() as conn:
        image_id = db.insert(conn, "images", {
            "incident_id": task["incident_id"], "task_id": task["id"], "kind": "field_photo",
            "label": f"Field photo · task {task['id']}", "source_file": f"report-{report_id}-{index}.jpg",
            "width": image.width, "height": image.height, "path": "", "preview_path": "",
            "latitude": latitude, "longitude": longitude,
            "location_source": "device_gps" if latitude is not None else "missing", "created_at": db.now(),
        })
        path = storage.save_jpeg(image, f"{folder}/{image_id}.jpg")
        preview = storage.save_jpeg(image, f"{folder}/{image_id}_preview.jpg", quality=72, max_side=360)
        db.update(conn, "images", image_id, {"path": path, "preview_path": preview})


@router.get("/tasks/{task_id}/reports")
def task_reports(task_id: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        _task(conn, task_id)
        reports = db.all_rows(conn, """SELECT r.*, u.name AS user FROM field_reports r LEFT JOIN users u ON u.id = r.user_id
                                      WHERE r.task_id = ? ORDER BY r.id DESC""", (task_id,))
        photos = db.all_rows(conn, "SELECT id, created_at, latitude, longitude FROM images WHERE task_id = ? ORDER BY id",
                             (task_id,))
    return {"reports": [dict(r, hazards=db.loads(r["hazards_json"], [])) for r in reports], "photos": photos}
