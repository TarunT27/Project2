"""CSV, GeoJSON (observations or buildings), KML, Shapefile and PDF situation reports."""

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
import pandas as pd

from utils.exports import EXPORT_COLUMNS, export_csv, export_geojson

from .. import auth, db
from ..common import OBS_SELECT, TASK_SELECT, get_or_404, task_out
from ..services import buildings

router = APIRouter(prefix="/api", tags=["exports"])

COLUMNS = EXPORT_COLUMNS[:-1] + ["model_class", "reviewer", "reviewed_at", "review_note", "hazards", "footprint_id",
                                 "track_id", "task_status", "assigned_crew", "bbox"]
FORMATS = {
    "csv": ("text/csv", "csv"), "geojson": ("application/geo+json", "geojson"),
    "buildings": ("application/geo+json", "buildings.geojson"), "kml": ("application/vnd.google-earth.kml+xml", "kml"),
    "shp": ("application/zip", "shp.zip"), "pdf": ("application/pdf", "pdf"),
}


def export_rows(conn, incident_id, reviewed_only=False, min_conf=0.0, include_rejected=False):
    sql = OBS_SELECT.replace("FROM observations o", """, c.name AS crew_name FROM observations o""") \
        .replace("LEFT JOIN users u ON u.id = o.reviewer_id", """LEFT JOIN users u ON u.id = o.reviewer_id
        LEFT JOIN tasks tk ON tk.id = (SELECT t2.id FROM tasks t2 WHERE t2.observation_id = o.id ORDER BY t2.id DESC LIMIT 1)
        LEFT JOIN crews c ON c.id = tk.crew_id""")
    sql += " WHERE o.incident_id = ? AND o.confidence >= ?"
    params = [incident_id, min_conf]
    if reviewed_only:
        sql += " AND o.status IN ('confirmed','corrected','added')"
    elif not include_rejected:
        sql += " AND o.status != 'rejected'"
    rows = db.all_rows(conn, sql, params)
    out = []
    for r in rows:
        out.append({
            "observation_id": r["id"], "id": r["id"], "source_file": r["source_file"], "frame_seconds": r["frame_seconds"],
            "damage_class": r["final_class"], "model_class": r["model_class"], "confidence": r["confidence"],
            "latitude": r["latitude"], "longitude": r["longitude"], "location_source": r["location_source"],
            "location_is_synthetic": r["location_source"] == "synthetic_demo", "timestamp": r["captured_at"],
            "captured_at": r["captured_at"], "assessed_at_utc": r["created_at"], "assessed_at": r["created_at"],
            "is_demo": bool(r["is_demo"]), "scope": r["scope"], "model": r["model"],
            "confidence_threshold": r["threshold"], "review_status": r["status"], "status": r["status"],
            "reviewer": r["reviewer_name"], "reviewed_at": r["reviewed_at"], "review_note": r["review_note"],
            "hazards": db.loads(r["hazards_json"], []), "footprint_id": r["footprint_id"], "track_id": r["track_id"],
            "task_status": r["task_status"], "assigned_crew": r["crew_name"], "bbox": db.loads(r["bbox_json"]),
        })
    return out


def _filename(incident, rows, ext):
    slug = re.sub(r"[^A-Za-z0-9]+", "_", incident["name"]).strip("_")[:40] or "incident"
    demo = any(r["is_demo"] for r in rows)
    reviewed = rows and all(r["status"] in ("confirmed", "corrected", "added") for r in rows)
    prefix = "DEMO_" if demo else ("" if reviewed else "UNVERIFIED_")
    return f"{prefix}{slug}_damage.{ext}"


def build_export(incident_id, fmt, reviewed_only=False, min_conf=0.0, generated_by="", public=False):
    if fmt not in FORMATS:
        raise HTTPException(400, f"Format must be one of {', '.join(FORMATS)}.")
    with db.connect() as conn:
        incident = get_or_404(conn, "incidents", incident_id)
        rows = export_rows(conn, incident_id, reviewed_only, min_conf)
        tasks = [] if public else [task_out(t) for t in db.all_rows(conn, TASK_SELECT + " WHERE t.incident_id = ?",
                                                                     (incident_id,))]
    if public:  # shared links never include crew assignments or reviewer identities
        for row in rows:
            row.update(reviewer=None, assigned_crew=None, review_note="")
    media, ext = FORMATS[fmt]
    for row in rows:
        row["incident_name"] = incident["name"]
    if fmt == "csv":
        frame = pd.DataFrame([dict(r, hazards=";".join(r["hazards"])) for r in rows])
        body = export_csv(frame, COLUMNS)
    elif fmt == "geojson":
        frame = pd.DataFrame([dict(r, hazards=";".join(r["hazards"])) for r in rows])
        body = export_geojson(frame, COLUMNS)
    elif fmt == "buildings":
        features = [{"type": "Feature", "id": b["footprint_id"],
                     "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring] for ring in b["polygon"]]},
                     "properties": {k: b[k] for k in ("footprint_id", "external_id", "damage_class", "confidence",
                                                      "count", "reviewed")}}
                    for b in buildings.building_rollup(incident_id)]
        body = json.dumps({"type": "FeatureCollection", "features": features}).encode()
    elif fmt == "kml":
        from ..services.reports import export_kml
        body = export_kml(rows)
    elif fmt == "shp":
        from ..services.reports import export_shapefile_zip
        body = export_shapefile_zip(rows)
    else:
        from ..services.reports import situation_report_pdf
        body = situation_report_pdf(dict(incident), rows, tasks, generated_by or "shared link", db.now())
    name = _filename(incident, rows, ext)
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/incidents/{incident_id}/export/{fmt}")
def export(incident_id: int, fmt: str, reviewed_only: bool = False, min_conf: float = 0.0,
           user=Depends(auth.current_user)):
    response = build_export(incident_id, fmt, reviewed_only, min_conf, user["name"])
    with db.connect() as conn:
        db.audit(conn, user["id"], "export.downloaded", "incident", incident_id, format=fmt, reviewed_only=reviewed_only)
    return response
