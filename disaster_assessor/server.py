"""Mission-console web app. Run: python server.py  (then open http://127.0.0.1:8000).

The browser UI lives in web/. All assessment logic is shared with the Streamlit app
through utils/pipeline.py. Processing happens on this machine; no cloud inference API.
"""

from collections import OrderedDict
from functools import lru_cache
import os
from pathlib import Path
import secrets
import threading

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import pandas as pd

from utils.detector import DamageDetector
from utils.exports import export_csv, export_geojson
from utils.geo_utils import DEFAULT_CENTER, valid_coordinate
from utils.mock_data import CLASSES, COLORS, SEVERITY
from utils.pipeline import (
    IMAGE_TYPES,
    MAX_FILES,
    MAX_UPLOAD_BYTES,
    VIDEO_TYPES,
    Settings,
    run_assessment,
)

WEB = Path(__file__).with_name("web")
MAX_RUNS = 10

app = FastAPI(title="Rapid Disaster Assessment", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=WEB), name="static")

# Bounded in-memory run store: incident imagery never touches disk.
_runs = OrderedDict()
_runs_lock = threading.Lock()


def _store(batch):
    run_id = secrets.token_urlsafe(9)
    with _runs_lock:
        _runs[run_id] = batch
        while len(_runs) > MAX_RUNS:
            _runs.popitem(last=False)
    return run_id


def _get(run_id):
    with _runs_lock:
        if run_id not in _runs:
            raise HTTPException(404, "Run not found. It may have expired; run the assessment again.")
        return _runs[run_id]


@lru_cache(maxsize=2)
def _real_detector(path, modified_ns, threshold, device):
    """Cache only model resources, keyed so a replaced weights file reloads."""
    return DamageDetector(False, path, threshold, device)


def _summary(batch):
    rows = batch["rows"]
    by_class = {name: sum(r["damage_class"] == name for r in rows) for name in CLASSES}
    high = by_class["Major Damage"] + by_class["Destroyed"]
    return {
        "total": len(rows),
        "high": high,
        "high_pct": round(high / len(rows) * 100, 1) if rows else 0,
        "mapped": sum(valid_coordinate(r.get("latitude"), r.get("longitude")) for r in rows),
        "by_class": by_class,
        "severity_index": round(sum(SEVERITY[r["damage_class"]] for r in rows) / len(rows), 2)
        if rows
        else 0,
        "processed": batch["processed"],
    }


def _response(run_id, batch):
    rows = [{k: v for k, v in r.items() if k != "preview"} for r in batch["rows"]]
    return {
        "run_id": run_id,
        "demo": batch["demo"],
        "model": batch["model"],
        "threshold": batch["threshold"],
        "assessed_at_utc": batch["assessed_at_utc"],
        "images": batch["images"],
        "rows": rows,
        "errors": batch["errors"],
        "summary": _summary(batch),
    }


def _settings(mode, confidence, model_path, lat, lon, override, interval, max_frames):
    if mode not in {"demo", "real"}:
        raise HTTPException(400, "Mode must be demo or real.")
    if not 0 <= confidence <= 1:
        raise HTTPException(400, "Confidence threshold must be between 0 and 1.")
    if not valid_coordinate(lat, lon):
        raise HTTPException(400, "Reference coordinates are out of range.")
    if not 1 <= interval <= 30 or not 1 <= max_frames <= 30:
        raise HTTPException(400, "Video sampling must be 1–30 s and 1–30 frames.")
    use_mock = mode == "demo"
    return Settings(
        use_mock=use_mock,
        confidence=confidence,
        model_name="synthetic-demo" if use_mock else Path(model_path).name,
        center=(lat, lon),
        override=override,
        interval=interval,
        max_frames=max_frames,
    )


def _detector(settings, model_path, device):
    if settings.use_mock:
        return DamageDetector(True, confidence_threshold=settings.confidence)
    if device not in {"cpu", "mps", "cuda:0"}:
        raise HTTPException(400, "Device must be cpu, mps, or cuda:0.")
    path = Path(model_path).expanduser()
    if not model_path or not path.is_file():
        raise HTTPException(
            400,
            "Select a local damage-trained .pt file. Generic yolov8n.pt COCO weights cannot classify building damage.",
        )
    try:
        return _real_detector(str(path.resolve()), path.stat().st_mtime_ns, settings.confidence, device)
    except ModuleNotFoundError:
        raise HTTPException(400, "Real mode needs the full install: pip install -r requirements.txt")
    except Exception as exc:
        raise HTTPException(400, f"Could not load the model: {exc}")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/config")
def config():
    return {
        "classes": CLASSES,
        "colors": COLORS,
        "default_center": DEFAULT_CENTER,
        "accept": sorted(IMAGE_TYPES | VIDEO_TYPES),
        "max_files": MAX_FILES,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
    }


@app.post("/api/demo")
async def demo(
    confidence: float = Form(0.5),
    lat: float = Form(DEFAULT_CENTER[0]),
    lon: float = Form(DEFAULT_CENTER[1]),
    override: bool = Form(False),
):
    settings = _settings("demo", confidence, "", lat, lon, override, 3, 8)
    batch = await run_in_threadpool(
        run_assessment, _detector(settings, "", "cpu"), [], settings, True
    )
    return _response(_store(batch), batch)


@app.post("/api/assess")
async def assess(
    files: list[UploadFile] = File(...),
    mode: str = Form("demo"),
    confidence: float = Form(0.5),
    model_path: str = Form(""),
    device: str = Form("cpu"),
    lat: float = Form(DEFAULT_CENTER[0]),
    lon: float = Form(DEFAULT_CENTER[1]),
    override: bool = Form(False),
    interval: float = Form(3),
    max_frames: int = Form(8),
):
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"Limit the batch to {MAX_FILES} files.")
    settings = _settings(mode, confidence, model_path, lat, lon, override, interval, max_frames)
    detector = _detector(settings, model_path, device)
    sources, total = [], 0
    for upload in files:
        name = Path(upload.filename or "upload").name
        if Path(name).suffix.lower() not in IMAGE_TYPES | VIDEO_TYPES:
            raise HTTPException(400, f"{name}: unsupported file type.")
        data = await upload.read()
        total += len(data)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Limit the batch to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB total.")
        sources.append((name, data))
    batch = await run_in_threadpool(run_assessment, detector, sources, settings)
    return _response(_store(batch), batch)


def _export_name(batch, suffix):
    return ("DEMO_" if batch["demo"] else "UNVERIFIED_") + suffix


def _export_frame(batch, min_conf):
    """Rows matching the console's live confidence filter; never below the run threshold."""
    threshold = max(batch["threshold"], min(max(min_conf, 0.0), 1.0))
    rows = [dict(r, confidence_threshold=threshold) for r in batch["rows"] if r["confidence"] >= threshold - 1e-9]
    return pd.DataFrame(rows)


@app.get("/api/runs/{run_id}/export.csv")
def download_csv(run_id: str, min_conf: float = 0.0):
    batch = _get(run_id)
    name = _export_name(batch, "damage_review_priority.csv")
    return Response(
        export_csv(_export_frame(batch, min_conf)),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/runs/{run_id}/export.geojson")
def download_geojson(run_id: str, min_conf: float = 0.0):
    batch = _get(run_id)
    name = _export_name(batch, "damage_observations.geojson")
    return Response(
        export_geojson(_export_frame(batch, min_conf)),
        media_type="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.exception_handler(HTTPException)
async def http_error(_, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn

    # Localhost by default: uploaded imagery and EXIF can be sensitive, and there is no auth.
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8000)))
