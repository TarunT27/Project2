"""Building footprints (OSM or uploaded), building-level rollups, and pre/post imagery overlays."""

import io
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel

from .. import auth, db, storage
from ..common import get_or_404
from ..config import get_config
from ..events import bus
from ..services import buildings

router = APIRouter(prefix="/api", tags=["geo"])


class OsmRequest(BaseModel):
    south: float | None = None
    west: float | None = None
    north: float | None = None
    east: float | None = None


def footprints_geojson(incident_id, limit=20000):
    with db.connect() as conn:
        rows = db.all_rows(conn, "SELECT * FROM footprints WHERE incident_id = ? LIMIT ?", (incident_id, limit))
    features = []
    for r in rows:
        rings = db.loads(r["polygon_json"], [])
        features.append({"type": "Feature", "id": r["id"],
                         "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring] for ring in rings]},
                         "properties": {"id": r["id"], "source": r["source"], "external_id": r["external_id"]}})
    return {"type": "FeatureCollection", "features": features}


@router.get("/incidents/{incident_id}/footprints")
def get_footprints(incident_id: int, user=Depends(auth.current_user)):
    return footprints_geojson(incident_id)


def _observation_extent(incident_id, pad=0.002):
    with db.connect() as conn:
        box = db.one(conn, """SELECT MIN(latitude) s, MIN(longitude) w, MAX(latitude) n, MAX(longitude) e
                              FROM observations WHERE incident_id = ? AND latitude IS NOT NULL""", (incident_id,))
    if box["s"] is None:
        raise HTTPException(400, "No mapped observations yet. Pass a bounding box instead.")
    return box["s"] - pad, box["w"] - pad, box["n"] + pad, box["e"] + pad


@router.post("/incidents/{incident_id}/footprints/osm")
def import_osm(incident_id: int, body: OsmRequest, user=Depends(auth.require("coordinator"))):
    from ..services import footprints as fpmod

    with db.connect() as conn:
        get_or_404(conn, "incidents", incident_id)
    if None in (body.south, body.west, body.north, body.east):
        south, west, north, east = _observation_extent(incident_id)
    else:
        south, west, north, east = body.south, body.west, body.north, body.east
    try:
        fps = fpmod.fetch_osm_buildings(south, west, north, east,
                                        cache_dir=str(get_config().data_dir / "cache" / "overpass"))
    except ValueError as exc:
        raise HTTPException(400, f"{exc} Zoom into a smaller area (max ~5 km across).")
    except RuntimeError as exc:
        raise HTTPException(502, f"OpenStreetMap request failed: {exc}")
    added = buildings.store_footprints(incident_id, fps, "osm")
    matched = buildings.match_and_store(incident_id)
    with db.connect() as conn:
        db.audit(conn, user["id"], "footprints.osm_imported", "incident", incident_id, added=added)
    return {"fetched": len(fps), "added": added, "matched_observations": matched}


@router.post("/incidents/{incident_id}/footprints/upload")
async def upload_footprints(incident_id: int, file: UploadFile = File(...), user=Depends(auth.require("coordinator"))):
    from ..services import footprints as fpmod

    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Footprint files are limited to 200 MB.")
    try:
        fps = fpmod.load_footprints_geojson(data)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, f"Could not read GeoJSON footprints: {exc}")
    added = buildings.store_footprints(incident_id, fps, "upload")
    matched = buildings.match_and_store(incident_id)
    with db.connect() as conn:
        db.audit(conn, user["id"], "footprints.uploaded", "incident", incident_id, added=added)
    return {"loaded": len(fps), "added": added, "matched_observations": matched}


@router.delete("/incidents/{incident_id}/footprints")
def clear_footprints(incident_id: int, user=Depends(auth.require("coordinator"))):
    with db.connect() as conn:
        conn.execute("UPDATE observations SET footprint_id = NULL WHERE incident_id = ?", (incident_id,))
        conn.execute("DELETE FROM footprints WHERE incident_id = ?", (incident_id,))
    bus.publish("buildings.updated", incident_id)
    return {"ok": True}


@router.get("/incidents/{incident_id}/buildings")
def get_buildings(incident_id: int, user=Depends(auth.current_user)):
    return buildings.building_rollup(incident_id)


# ---------- overlays ----------

def overlay_out(row):
    return {"id": row["id"], "incident_id": row["incident_id"], "kind": row["kind"], "name": row["name"],
            "bounds": db.loads(row["bounds_json"]), "created_at": row["created_at"]}


@router.get("/incidents/{incident_id}/overlays")
def list_overlays(incident_id: int, user=Depends(auth.current_user)):
    with db.connect() as conn:
        rows = db.all_rows(conn, "SELECT * FROM overlays WHERE incident_id = ? ORDER BY id", (incident_id,))
    return [overlay_out(r) for r in rows]


@router.post("/incidents/{incident_id}/overlays")
async def upload_overlay(incident_id: int, kind: str = Form(...), file: UploadFile = File(...),
                         user=Depends(auth.require("coordinator"))):
    """Pre- or post-event GeoTIFF for the before/after comparison."""
    from ..services import ortho

    if kind not in ("pre", "post"):
        raise HTTPException(400, "Kind must be pre or post.")
    data = await file.read()
    if len(data) > 1024**3:
        raise HTTPException(413, "Overlays are limited to 1 GB.")
    georef = ortho.read_geotiff_georef(data)
    if not georef:
        raise HTTPException(400, "This file has no usable GeoTIFF georeference.")
    Image.MAX_IMAGE_PIXELS = 400_000_000
    with Image.open(io.BytesIO(data)) as img:
        small, _ = ortho.downsample_for_inference(img.convert("RGB"), 4096)
    with db.connect() as conn:
        get_or_404(conn, "incidents", incident_id)
        overlay_id = db.insert(conn, "overlays", {"incident_id": incident_id, "kind": kind, "name": file.filename or kind,
                                                  "path": "", "bounds_json": json.dumps(ortho.bounds_latlon(georef)),
                                                  "created_at": db.now()})
        rel = storage.save_png(small, f"overlays/incident-{incident_id}/overlay-{overlay_id}.png")
        db.update(conn, "overlays", overlay_id, {"path": rel})
        out = overlay_out(db.one(conn, "SELECT * FROM overlays WHERE id = ?", (overlay_id,)))
    bus.publish("overlays.updated", incident_id)
    return out


def overlay_file(overlay_id):
    with db.connect() as conn:
        row = db.one(conn, "SELECT * FROM overlays WHERE id = ?", (overlay_id,))
    if not row:
        raise HTTPException(404, "Overlay not found.")
    return row, FileResponse(storage.resolve(row["path"]), media_type="image/png",
                             headers={"Cache-Control": "private, max-age=86400"})


@router.get("/overlays/{overlay_id}/file")
def get_overlay_file(overlay_id: int, user=Depends(auth.current_user)):
    return overlay_file(overlay_id)[1]


@router.delete("/overlays/{overlay_id}")
def delete_overlay(overlay_id: int, user=Depends(auth.require("coordinator"))):
    with db.connect() as conn:
        row = get_or_404(conn, "overlays", overlay_id)
        conn.execute("DELETE FROM overlays WHERE id = ?", (overlay_id,))
    try:
        storage.resolve(row["path"]).unlink(missing_ok=True)
    except ValueError:
        pass
    bus.publish("overlays.updated", row["incident_id"])
    return {"ok": True}
