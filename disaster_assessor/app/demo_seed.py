"""Build the judge-demo incident from bundled NOAA Hurricane Ian imagery.

    python -m app.cli seed-demo            # Claude vision (needs ANTHROPIC_API_KEY)
    python -m app.cli seed-demo --if-empty # skip when any incident exists (used by start-demo.sh)

Runs real detection on three Fort Myers Beach / San Carlos Island mosaics, imports OpenStreetMap
building footprints for the same area, creates two crews and turns high-priority buildings into
tasks. Matlacha is left out on purpose so it can be uploaded live during the demo.
Nothing is marked as reviewed: reviewing is part of the demo.
"""

import json
from pathlib import Path
import shutil

from . import db, storage
from .config import ROOT
from .services import buildings, models

DEMO_DIR = ROOT / "demo" / "noaa-ian"
PRESEED = ("fort-myers-beach-north.tif", "fort-myers-beach-estero.tif", "san-carlos-island.tif")
INCIDENT = {
    "name": "Hurricane Ian – Fort Myers Beach, FL",
    "description": ("Category 4 landfall 28 Sep 2022. Post-storm aerial imagery flown by NOAA's National Geodetic "
                    "Survey on 30 Sep 2022 (collection 20220930d). Lee County barrier islands."),
    "center_lat": 26.4560, "center_lon": -81.9490,
}
CREWS = (("Engine 21 – Fort Myers Beach Fire", "Radio TAC-3"), ("FL-TF2 USAR (Miami-Dade)", "Radio TAC-5"))


def _progress(message):
    print(f"  · {message}", flush=True)


def seed(detector="claude", if_empty=False, threshold=0.4):
    with db.connect() as conn:
        if if_empty and db.one(conn, "SELECT COUNT(*) AS n FROM incidents")["n"]:
            print("Incidents already exist; skipping demo seed.")
            return None
    if detector == "claude":
        from .services.claude_detector import is_configured
        if not is_configured():
            from .services.claude_detector import NOT_CONFIGURED
            raise SystemExit(NOT_CONFIGURED + " Or pass --detector demo for synthetic boxes.")
    missing = [n for n in PRESEED if not (DEMO_DIR / n).is_file()]
    if missing:
        raise SystemExit(f"Missing demo imagery: {', '.join(missing)} in {DEMO_DIR}")

    with db.connect() as conn:
        incident_id = db.insert(conn, "incidents", {**INCIDENT, "created_at": db.now()})
        db.audit(conn, None, "demo.seeded", "incident", incident_id, detector=detector)
    print(f"Created incident #{incident_id}: {INCIDENT['name']}")

    settings = {"interval": 3, "max_frames": 8, "manual": None, "vision_review": False, "builtin_demo": False}
    with db.connect() as conn:
        run_id = db.insert(conn, "runs", {
            "incident_id": incident_id, "mode": detector, "model": models.describe(detector, settings),
            "threshold": threshold, "settings_json": json.dumps(settings), "created_at": db.now(),
            "status": "queued", "message": "Queued", "total_inputs": len(PRESEED),
        })
    folder = storage.resolve(storage.upload_dir(run_id))
    folder.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(PRESEED):
        shutil.copyfile(DEMO_DIR / name, folder / f"{index:03d}__{name}")

    from .services.assess import Job

    print(f"Assessing {len(PRESEED)} NOAA mosaics with {models.describe(detector, settings)} (about a minute)…")
    status = Job(run_id).run_job()
    with db.connect() as conn:
        run = db.one(conn, "SELECT status, message, errors_json FROM runs WHERE id = ?", (run_id,))
    _progress(f"run #{run_id}: {run['status']} — {run['message']}")
    for err in db.loads(run["errors_json"], []):
        _progress(f"warning: {err}")
    if status != "done":
        raise SystemExit("Assessment failed; see the warnings above.")

    _import_footprints(incident_id)

    with db.connect() as conn:
        crew_ids = [db.insert(conn, "crews", {"incident_id": incident_id, "name": name, "contact": contact,
                                              "created_at": db.now()}) for name, contact in CREWS]
        high = db.all_rows(conn, """SELECT o.*, i.source_file FROM observations o JOIN images i ON i.id = o.image_id
            WHERE o.incident_id = ? AND o.final_class IN ('Destroyed','Major Damage')
            ORDER BY CASE o.final_class WHEN 'Destroyed' THEN 0 ELSE 1 END, o.confidence DESC""", (incident_id,))
        for index, obs in enumerate(high):
            crew = crew_ids[0] if index < 4 else crew_ids[1] if index < 7 else None
            db.insert(conn, "tasks", {
                "incident_id": incident_id, "observation_id": obs["id"],
                "title": f"{obs['final_class']} · {Path(obs['source_file']).stem.replace('-', ' ').title()}",
                "damage_class": obs["final_class"],
                "priority": (3 if obs["final_class"] == "Destroyed" else 2) * 100 + round(obs["confidence"] * 99),
                "status": "assigned" if crew else "open", "crew_id": crew,
                "latitude": obs["latitude"], "longitude": obs["longitude"], "hazards_json": obs["hazards_json"],
                "created_at": db.now(), "updated_at": db.now(),
            })
    _progress(f"{len(CREWS)} crews, {len(high)} tasks for Major/Destroyed buildings ({min(len(high), 7)} assigned)")
    print(f"Done. Open http://127.0.0.1:8000/#/i/{incident_id}/console")
    return incident_id


def _import_footprints(incident_id):
    from .config import get_config
    from .services import footprints as fpmod

    with db.connect() as conn:
        boxes = [db.loads(r["bounds_json"]) for r in db.all_rows(
            conn, "SELECT bounds_json FROM overlays WHERE incident_id = ?", (incident_id,))]
    import time

    total, failed = 0, 0
    for (south, west), (north, east) in boxes:
        for attempt in range(3):  # the public Overpass server rate-limits bursts
            try:
                fps = fpmod.fetch_osm_buildings(south, west, north, east,
                                                cache_dir=str(get_config().data_dir / "cache" / "overpass"))
                total += buildings.store_footprints(incident_id, fps, "osm")
                break
            except Exception:  # OSM is a nice-to-have for the demo, never fatal
                if attempt == 2:
                    failed += 1
                else:
                    time.sleep(5 * (attempt + 1))
        time.sleep(1)
    if failed:
        _progress(f"OpenStreetMap footprints unavailable for {failed} area(s); import them later from the map")
    matched = buildings.match_and_store(incident_id)
    _progress(f"{total} OpenStreetMap building footprints; {matched} observations matched to a building")
