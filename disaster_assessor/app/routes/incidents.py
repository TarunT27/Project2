from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.geo_utils import valid_coordinate

from .. import auth, db, storage
from ..common import CLASSES, REVIEW_STATUSES, TASK_STATUSES, get_or_404
from ..events import bus

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


class IncidentIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    center_lat: float | None = None
    center_lon: float | None = None


class IncidentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    center_lat: float | None = None
    center_lon: float | None = None
    status: str | None = None


def _check_center(lat, lon):
    if (lat is None) != (lon is None) or (lat is not None and not valid_coordinate(lat, lon)):
        raise HTTPException(400, "Provide a valid latitude and longitude pair, or neither.")


def summary(conn, incident_id):
    by_class = {c: 0 for c in CLASSES}
    by_status = {s: 0 for s in REVIEW_STATUSES}
    for row in db.all_rows(conn, """SELECT final_class, status, COUNT(*) AS n FROM observations
                                    WHERE incident_id = ? GROUP BY final_class, status""", (incident_id,)):
        by_status[row["status"]] += row["n"]
        if row["status"] != "rejected":
            by_class[row["final_class"]] += row["n"]
    tasks = {s: 0 for s in TASK_STATUSES}
    for row in db.all_rows(conn, "SELECT status, COUNT(*) AS n FROM tasks WHERE incident_id = ? GROUP BY status",
                           (incident_id,)):
        tasks[row["status"]] = row["n"]
    timeline = db.all_rows(conn, """SELECT r.id AS run_id, r.created_at, r.status, r.mode,
        SUM(o.final_class = 'Destroyed' AND o.status != 'rejected') AS destroyed,
        SUM(o.final_class = 'Major Damage' AND o.status != 'rejected') AS major,
        SUM(o.final_class = 'Minor Damage' AND o.status != 'rejected') AS minor,
        SUM(o.final_class = 'No Damage' AND o.status != 'rejected') AS intact
        FROM runs r LEFT JOIN observations o ON o.run_id = r.id WHERE r.incident_id = ?
        GROUP BY r.id ORDER BY r.id""", (incident_id,))
    counts = db.one(conn, """SELECT
        (SELECT COUNT(*) FROM observations WHERE incident_id = :i) AS observations,
        (SELECT COUNT(*) FROM observations WHERE incident_id = :i AND latitude IS NOT NULL AND status != 'rejected') AS mapped,
        (SELECT COUNT(*) FROM images WHERE incident_id = :i AND kind != 'field_photo') AS images,
        (SELECT COUNT(*) FROM runs WHERE incident_id = :i) AS runs,
        (SELECT COUNT(*) FROM footprints WHERE incident_id = :i) AS footprints,
        (SELECT COUNT(DISTINCT footprint_id) FROM observations WHERE incident_id = :i AND footprint_id IS NOT NULL
            AND status != 'rejected') AS buildings,
        (SELECT COUNT(*) FROM observations WHERE incident_id = :i AND is_demo = 1) AS demo""", {"i": incident_id})
    active = sum(v for k, v in by_class.items())
    reviewed = sum(v for k, v in by_status.items() if k != "not_reviewed")
    return {
        **counts, "by_class": by_class, "by_status": by_status, "tasks": tasks, "timeline": timeline,
        "high_priority": by_class["Major Damage"] + by_class["Destroyed"],
        "reviewed_pct": round(reviewed / counts["observations"] * 100, 1) if counts["observations"] else 0,
        "active_observations": active,
    }


def incident_out(row, conn=None):
    out = {k: row[k] for k in ("id", "name", "description", "center_lat", "center_lon", "status", "created_at")}
    if conn is not None:
        out["summary"] = summary(conn, row["id"])
    return out


@router.get("")
def list_incidents(user=Depends(auth.current_user)):
    with db.connect() as conn:
        rows = db.all_rows(conn, "SELECT * FROM incidents ORDER BY status = 'closed', id DESC")
        return [incident_out(r, conn) for r in rows]


@router.post("")
def create_incident(body: IncidentIn, user=Depends(auth.require("coordinator"))):
    _check_center(body.center_lat, body.center_lon)
    with db.connect() as conn:
        incident_id = db.insert(conn, "incidents", {**body.model_dump(), "created_by": user["id"], "created_at": db.now()})
        db.audit(conn, user["id"], "incident.created", "incident", incident_id, name=body.name)
        row = get_or_404(conn, "incidents", incident_id)
        return incident_out(row, conn)


@router.get("/{incident_id}")
def get_incident(incident_id: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        return incident_out(get_or_404(conn, "incidents", incident_id), conn)


@router.patch("/{incident_id}")
def patch_incident(incident_id: int, body: IncidentPatch, user=Depends(auth.require("coordinator"))):
    values = body.model_dump(exclude_unset=True)
    if "status" in values and values["status"] not in ("active", "closed"):
        raise HTTPException(400, "Status must be active or closed.")
    with db.connect() as conn:
        row = get_or_404(conn, "incidents", incident_id)
        _check_center(values.get("center_lat", row["center_lat"]), values.get("center_lon", row["center_lon"]))
        db.update(conn, "incidents", incident_id, values)
        db.audit(conn, user["id"], "incident.updated", "incident", incident_id, **values)
        out = incident_out(get_or_404(conn, "incidents", incident_id), conn)
    bus.publish("incident.updated", incident_id)
    return out


@router.delete("/{incident_id}")
def delete_incident(incident_id: int, user=Depends(auth.require("admin"))):
    with db.connect() as conn:
        row = get_or_404(conn, "incidents", incident_id)
        conn.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
        db.audit(conn, user["id"], "incident.deleted", "incident", incident_id, name=row["name"])
    storage.remove_tree(f"images/incident-{incident_id}")
    storage.remove_tree(f"overlays/incident-{incident_id}")
    return {"ok": True}


@router.get("/{incident_id}/summary")
def get_summary(incident_id: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        get_or_404(conn, "incidents", incident_id)
        return summary(conn, incident_id)
