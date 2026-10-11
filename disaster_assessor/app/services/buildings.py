"""Footprints stored per incident; observation → building matching and building-level rollups."""

import json

from .. import db
from ..events import bus

SEVERITY = {"No Damage": 0, "Minor Damage": 1, "Major Damage": 2, "Destroyed": 3}


def _row_to_footprint(row):
    return {
        "id": row["id"], "external_id": row["external_id"], "source": row["source"],
        "polygon": db.loads(row["polygon_json"], []),
        "bbox": (row["min_lon"], row["min_lat"], row["max_lon"], row["max_lat"]),
        "centroid": (row["centroid_lat"], row["centroid_lon"]),
        "properties": db.loads(row["properties_json"], {}),
    }


def incident_footprints(incident_id):
    with db.connect() as conn:
        rows = db.all_rows(conn, "SELECT * FROM footprints WHERE incident_id = ?", (incident_id,))
    return [_row_to_footprint(r) for r in rows]


def store_footprints(incident_id, footprints, source):
    """Insert parsed footprints (dicts from services.footprints); returns the number added."""
    added = 0
    with db.connect() as conn:
        for fp in footprints:
            minlon, minlat, maxlon, maxlat = fp["bbox"]
            cur = conn.execute(
                """INSERT OR IGNORE INTO footprints (incident_id, external_id, source, polygon_json, centroid_lat,
                   centroid_lon, min_lon, min_lat, max_lon, max_lat, properties_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (incident_id, str(fp["id"]), source, json.dumps(fp["polygon"]), fp["centroid"][0], fp["centroid"][1],
                 minlon, minlat, maxlon, maxlat, json.dumps(fp.get("properties") or {})))
            added += cur.rowcount
    return added


def match_and_store(incident_id, footprints=None, run_id=None, max_distance_m=25):
    """Assign footprint_id to observations with coordinates. Returns the number matched."""
    from . import footprints as fpmod

    footprints = footprints if footprints is not None else incident_footprints(incident_id)
    if not footprints:
        return 0
    sql = "SELECT id, latitude, longitude FROM observations WHERE incident_id = ? AND latitude IS NOT NULL"
    params = [incident_id]
    if run_id is not None:
        sql += " AND run_id = ?"
        params.append(run_id)
    matched = 0
    with db.connect() as conn:
        for row in db.all_rows(conn, sql, params):
            hit = fpmod.match_point(row["latitude"], row["longitude"], footprints, max_distance_m=max_distance_m)
            conn.execute("UPDATE observations SET footprint_id = ? WHERE id = ?", (hit["id"] if hit else None, row["id"]))
            matched += bool(hit)
    bus.publish("buildings.updated", incident_id, matched=matched)
    return matched


def building_rollup(incident_id, include_rejected=False):
    """One entry per matched footprint: worst reviewed-or-model class, counts and review state."""
    with db.connect() as conn:
        rows = db.all_rows(conn, f"""SELECT o.id, o.footprint_id, o.final_class, o.confidence, o.status
            FROM observations o WHERE o.incident_id = ? AND o.footprint_id IS NOT NULL
            {'' if include_rejected else "AND o.status != 'rejected'"}""", (incident_id,))
        fps = {r["id"]: r for r in db.all_rows(conn, "SELECT * FROM footprints WHERE incident_id = ?", (incident_id,))}
    groups = {}
    for row in rows:
        group = groups.setdefault(row["footprint_id"], {"observation_ids": [], "classes": [], "statuses": []})
        group["observation_ids"].append(row["id"])
        group["classes"].append((SEVERITY[row["final_class"]], row["confidence"], row["final_class"]))
        group["statuses"].append(row["status"])
    out = []
    for fid, group in groups.items():
        fp = fps.get(fid)
        if not fp:
            continue
        worst = max(group["classes"])
        reviewed = sum(s != "not_reviewed" for s in group["statuses"])
        out.append({
            "footprint_id": fid, "external_id": fp["external_id"], "polygon": db.loads(fp["polygon_json"], []),
            "centroid": [fp["centroid_lat"], fp["centroid_lon"]], "damage_class": worst[2], "confidence": worst[1],
            "count": len(group["observation_ids"]), "reviewed": reviewed, "observation_ids": group["observation_ids"],
        })
    out.sort(key=lambda b: (-SEVERITY[b["damage_class"]], -b["confidence"]))
    return out
