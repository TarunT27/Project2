"""Shared lookups and JSON shapes for the API."""

from fastapi import HTTPException

from . import db

CLASSES = ("No Damage", "Minor Damage", "Major Damage", "Destroyed")
REVIEW_STATUSES = ("not_reviewed", "confirmed", "corrected", "rejected", "added")
TASK_STATUSES = ("open", "assigned", "in_progress", "searched_clear", "needs_usar", "done", "cancelled")
HAZARDS = ("fire", "flooding", "debris_blocking_access", "gas_or_utility_damage", "possible_entrapment",
           "unstable_structure", "hazmat", "downed_power_lines")
SEVERITY = {c: i for i, c in enumerate(CLASSES)}


def get_or_404(conn, table, row_id, label=None):
    row = db.one(conn, f"SELECT * FROM {table} WHERE id = ?", (row_id,))
    if not row:
        raise HTTPException(404, f"{label or table.rstrip('s').capitalize()} not found.")
    return row


def clean_hazards(values):
    values = values or []
    if not isinstance(values, list) or any(v not in HAZARDS for v in values):
        raise HTTPException(400, f"Hazards must be from: {', '.join(HAZARDS)}.")
    return sorted(set(values))


def observation_out(row):
    return {
        "id": row["id"], "run_id": row["run_id"], "incident_id": row["incident_id"], "image_id": row["image_id"],
        "bbox": db.loads(row["bbox_json"], []), "model_class": row["model_class"], "damage_class": row["final_class"],
        "confidence": row["confidence"], "scope": row["scope"], "latitude": row["latitude"],
        "longitude": row["longitude"], "location_source": row["location_source"], "is_demo": bool(row["is_demo"]),
        "model": row["model"], "threshold": row["threshold"], "status": row["status"],
        "reviewer_id": row["reviewer_id"], "reviewer": row.get("reviewer_name"), "reviewed_at": row["reviewed_at"],
        "review_note": row["review_note"], "hazards": db.loads(row["hazards_json"], []),
        "footprint_id": row["footprint_id"], "track_id": row["track_id"], "vision": db.loads(row["vision_json"]),
        "source_file": row.get("source_file"), "frame_seconds": row.get("frame_seconds"),
        "captured_at": row.get("captured_at"), "task_id": row.get("task_id"), "task_status": row.get("task_status"),
        "created_at": row["created_at"],
    }


OBS_SELECT = """SELECT o.*, i.source_file, i.frame_seconds, i.captured_at, u.name AS reviewer_name,
    (SELECT t.id FROM tasks t WHERE t.observation_id = o.id ORDER BY t.id DESC LIMIT 1) AS task_id,
    (SELECT t.status FROM tasks t WHERE t.observation_id = o.id ORDER BY t.id DESC LIMIT 1) AS task_status
    FROM observations o JOIN images i ON i.id = o.image_id LEFT JOIN users u ON u.id = o.reviewer_id"""


def image_out(row):
    return {
        "id": row["id"], "run_id": row["run_id"], "incident_id": row["incident_id"], "task_id": row["task_id"],
        "kind": row["kind"], "label": row["label"], "source_file": row["source_file"],
        "frame_seconds": row["frame_seconds"], "width": row["width"], "height": row["height"],
        "latitude": row["latitude"], "longitude": row["longitude"], "location_source": row["location_source"],
        "captured_at": row["captured_at"], "telemetry": db.loads(row["telemetry_json"]),
        "has_georef": bool(row["georef_json"]), "observations": row.get("obs_count"),
        "unreviewed": row.get("unreviewed"), "created_at": row["created_at"],
    }


def run_out(row):
    return {
        "id": row["id"], "incident_id": row["incident_id"], "status": row["status"], "mode": row["mode"],
        "model": row["model"], "threshold": row["threshold"], "settings": db.loads(row["settings_json"], {}),
        "progress": row["progress"], "message": row["message"], "total_inputs": row["total_inputs"],
        "processed": row["processed"], "errors": db.loads(row["errors_json"], []),
        "created_by": row.get("created_by_name") or row["created_by"], "created_at": row["created_at"],
        "started_at": row["started_at"], "finished_at": row["finished_at"],
        "observations": row.get("obs_count"),
    }


def task_out(row):
    return {
        "id": row["id"], "incident_id": row["incident_id"], "observation_id": row["observation_id"],
        "title": row["title"], "damage_class": row["damage_class"], "priority": row["priority"],
        "status": row["status"], "crew_id": row["crew_id"], "crew": row.get("crew_name"),
        "latitude": row["latitude"], "longitude": row["longitude"], "notes": row["notes"],
        "hazards": db.loads(row["hazards_json"], []), "image_id": row.get("image_id"),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "last_report": row.get("last_report"),
    }


TASK_SELECT = """SELECT t.*, c.name AS crew_name, o.image_id,
    (SELECT r.reported_at FROM field_reports r WHERE r.task_id = t.id ORDER BY r.id DESC LIMIT 1) AS last_report
    FROM tasks t LEFT JOIN crews c ON c.id = t.crew_id LEFT JOIN observations o ON o.id = t.observation_id"""
