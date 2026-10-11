"""Background assessment job: uploads → detections → locations → database rows → live events.

Location priority for each detection:
  1. ortho_georef         — georeferenced GeoTIFF: the box centre's real map position
  2. telemetry_projection — drone XMP/SRT altitude + gimbal angles project the box onto the ground
  3. exif_camera / drone_telemetry — camera position only (not the building)
  4. manual_reference     — one shared point chosen by the operator
  5. synthetic_demo       — simulated, demo mode only
"""

import io
import json
import logging
from pathlib import Path
import re

from PIL import Image, ImageOps

from utils.detector import annotate_image
from utils.geo_utils import DEFAULT_CENTER, extract_gps_from_exif, valid_coordinate
from utils.media import sample_video_frames
from utils.mock_data import SEVERITY, sample_images
from utils.pipeline import IMAGE_TYPES, MAX_OBSERVATIONS, MAX_SAMPLES, VIDEO_TYPES

from .. import db, storage
from ..config import get_config
from ..events import bus
from . import models

log = logging.getLogger("dra.assess")
Image.MAX_IMAGE_PIXELS = 400_000_000  # orthomosaics are large; they are downsampled before inference
MAX_FRAME_PIXELS = 30_000_000
TIFF_TYPES = {".tif", ".tiff"}


class Cancelled(Exception):
    pass


def safe_name(name):
    name = Path(name or "upload").name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:120] or "upload"


# ---------- optional services (built as separate modules) ----------

def _telemetry():
    try:
        from . import telemetry
        return telemetry
    except ImportError:  # pragma: no cover
        return None


def read_image_telemetry(raw, focal_35mm=None):
    mod = _telemetry()
    if not mod or raw is None:
        return None
    try:
        return mod.read_dji_xmp(raw, focal_35mm=focal_35mm)
    except Exception:  # malformed XMP must never fail a run
        log.exception("XMP parse failed")
        return None


def project_detection(bbox, width, height, tele):
    mod = _telemetry()
    if not mod or not tele:
        return None
    try:
        hit = mod.project_bbox_center(bbox, width, height, tele)
    except Exception:
        log.exception("Projection failed")
        return None
    if hit and valid_coordinate(hit[0], hit[1]):
        return hit[0], hit[1]
    return None


def telemetry_camera(tele):
    """Camera lat/lon from a telemetry dict, whichever key spelling the parser used."""
    if not tele:
        return None
    for lat_key, lon_key in (("latitude", "longitude"), ("GpsLatitude", "GpsLongitude"), ("lat", "lon")):
        lat, lon = tele.get(lat_key), tele.get(lon_key)
        if lat is not None and lon is not None and valid_coordinate(lat, lon) and (lat, lon) != (0, 0):
            return float(lat), float(lon)
    return None


# ---------- job ----------

class Job:
    def __init__(self, run_id, cancelled=lambda: False):
        self.run_id = run_id
        self.cancelled = cancelled
        with db.connect() as conn:
            self.run = db.one(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))
            self.incident = db.one(conn, "SELECT * FROM incidents WHERE id = ?", (self.run["incident_id"],))
        self.settings = db.loads(self.run["settings_json"], {})
        self.demo = self.run["mode"] == "demo"
        self.threshold = self.run["threshold"]
        center = (self.incident["center_lat"], self.incident["center_lon"])
        self.center = center if valid_coordinate(*center) else DEFAULT_CENTER
        manual = self.settings.get("manual")
        self.manual = tuple(manual) if manual and valid_coordinate(*manual) else None
        self.errors = []
        self.samples = 0
        self.observation_count = 0
        self.video_tracks = {}  # source name -> list of (image_id, [obs ids], detections)

    # progress helpers
    def _set(self, **values):
        with db.connect() as conn:
            db.update(conn, "runs", self.run_id, values)

    def _progress(self, done, total, message):
        progress = round(done / total, 3) if total else 1.0
        self._set(progress=progress, processed=done, message=message)
        bus.publish("run.progress", self.incident["id"], run_id=self.run_id, progress=progress,
                    processed=done, total=total, message=message)

    def check(self):
        if self.cancelled():
            raise Cancelled()

    def run_job(self):
        self._set(status="running", started_at=db.now(), message="Loading model…")
        bus.publish("run.started", self.incident["id"], run_id=self.run_id)
        try:
            self.detector = models.build_detector(self.run["mode"], self.threshold, self.settings)
            sources = self._sources()
            srt = self._srt_tracks(sources)
            inputs = [s for s in sources if Path(s[0]).suffix.lower() != ".srt"]
            self._set(total_inputs=len(inputs))
            for index, (name, payload) in enumerate(inputs):
                self.check()
                try:
                    self._process_source(index, name, payload, srt)
                except Cancelled:
                    raise
                except Exception as exc:  # one bad file never sinks the batch
                    log.warning("Input %s failed: %s", name, exc)
                    self.errors.append(f"{name}: {exc}")
                self._progress(index + 1, len(inputs), f"Processed {index + 1}/{len(inputs)}")
            self.check()
            self._associate_video_tracks()
            self._match_footprints()
            self._vision_review()
            status, message = "done", f"{self.observation_count} observations"
            usage = getattr(getattr(self, "detector", None), "usage", None)
            if usage and usage.get("calls"):
                cost = self.detector.cost_usd()
                message += f" · Claude: {usage['calls']} calls, ~${cost:.2f}"
                if usage.get("refusals") or usage.get("errors"):
                    message += f" ({usage['refusals']} declined, {usage['errors']} failed)"
                self.settings["usage"] = dict(usage, cost_usd=cost)
                self._set(settings_json=json.dumps(self.settings))
        except Cancelled:
            status, message = "cancelled", "Cancelled"
        except Exception as exc:
            log.exception("Run %s failed", self.run_id)
            status, message = "failed", str(exc)
            self.errors.append(str(exc))
        self._set(status=status, message=message, finished_at=db.now(), errors_json=json.dumps(self.errors[-50:]),
                  progress=1.0 if status == "done" else self.run["progress"])
        if not get_config().keep_uploads:
            storage.remove_tree(storage.upload_dir(self.run_id))
        bus.publish("run.finished", self.incident["id"], run_id=self.run_id, status=status, message=message,
                    observations=self.observation_count, errors=self.errors[-10:])
        if status == "done":
            self._notify()
        return status

    def _sources(self):
        if self.settings.get("builtin_demo"):
            return [(name, image) for name, image in sample_images()]
        folder = storage.resolve(storage.upload_dir(self.run_id))
        if not folder.is_dir():
            return []
        return [(p.name.split("__", 1)[-1], p.read_bytes()) for p in sorted(folder.iterdir()) if p.is_file()]

    def _srt_tracks(self, sources):
        mod = _telemetry()
        tracks = {}
        for name, payload in sources:
            if Path(name).suffix.lower() == ".srt" and mod:
                try:
                    tracks[Path(name).stem.lower()] = mod.parse_dji_srt(payload.decode("utf-8", errors="replace"))
                except Exception as exc:
                    self.errors.append(f"{name}: telemetry could not be read ({exc})")
        return tracks

    def _process_source(self, index, name, payload, srt):
        suffix = Path(name).suffix.lower()
        if isinstance(payload, Image.Image):  # built-in demo scene
            self._process_frame(name, payload, kind="image")
            return
        if suffix in VIDEO_TYPES:
            track = srt.get(Path(name).stem.lower())
            mod = _telemetry()
            frames = sample_video_frames(payload, suffix, self.settings.get("interval", 3), self.settings.get("max_frames", 8))
            try:
                for seconds, frame in frames:
                    self.check()
                    tele = mod.telemetry_at(track, seconds) if (track and mod) else None
                    self._process_frame(name, frame, kind="video_frame", frame_seconds=seconds, telemetry=tele)
            finally:
                frames.close()
            return
        if suffix not in IMAGE_TYPES:
            raise ValueError("unsupported file type")
        if suffix in TIFF_TYPES:
            georef = self._georef(payload)
            if georef:
                self._process_ortho(name, payload, georef)
                return
        with Image.open(io.BytesIO(payload)) as loaded:
            loaded.load()
            if loaded.width * loaded.height > MAX_FRAME_PIXELS:
                raise ValueError("Image exceeds 30 megapixels (upload large orthomosaics as GeoTIFF)")
            frame = loaded.copy()
            try:
                focal = loaded.getexif().get_ifd(0x8769).get(41989)  # FocalLengthIn35mmFilm
            except Exception:
                focal = None
        tele = read_image_telemetry(payload, focal) if suffix in {".jpg", ".jpeg"} else None
        self._process_frame(name, frame, kind="image", telemetry=tele)

    def _georef(self, payload):
        try:
            from . import ortho
            return ortho.read_geotiff_georef(payload)
        except ImportError:  # pragma: no cover
            return None
        except Exception as exc:
            log.info("No usable georeference: %s", exc)
            return None

    # ---------- frames ----------

    def _limit(self):
        if self.samples >= MAX_SAMPLES or self.observation_count >= MAX_OBSERVATIONS * 5:
            raise ValueError("Run limit reached (60 images/frames). Split the upload into smaller runs.")

    def _base_location(self, original, name, frame_seconds, tele):
        location = extract_gps_from_exif(original, demo_fallback=False, center=self.center)
        camera = telemetry_camera(tele)
        if location["latitude"] is None and camera:
            location.update(latitude=camera[0], longitude=camera[1], location_source="drone_telemetry")
        if self.manual:
            location.update(latitude=self.manual[0], longitude=self.manual[1], location_source="manual_reference",
                            location_is_synthetic=False)
        elif location["latitude"] is None and self.demo:
            location = extract_gps_from_exif(original, demo_fallback=True, center=self.center,
                                             seed=f"{name}:{frame_seconds}")
        return location

    def _process_frame(self, name, original, kind, frame_seconds=None, telemetry=None):
        self._limit()
        location = self._base_location(original, name, frame_seconds, telemetry)
        oriented = ImageOps.exif_transpose(original).convert("RGB")
        oriented.thumbnail((1600, 1600))
        predictions = self.detector.predict(oriented)
        self.samples += 1
        detections = []
        for det in predictions:
            lat, lon, source = location["latitude"], location["longitude"], location["location_source"]
            if not self.manual:
                hit = project_detection(det["bbox"], oriented.width, oriented.height, telemetry)
                if hit:
                    lat, lon, source = hit[0], hit[1], "telemetry_projection"
            detections.append((det, lat, lon, source))
        image_id, obs_ids = self._save(name, oriented, kind, frame_seconds, location, telemetry, None, detections)
        if kind == "video_frame":
            self.video_tracks.setdefault(name, []).append(
                (image_id, obs_ids, [dict(d[0], latitude=d[1], longitude=d[2]) for d in detections]))

    def _process_ortho(self, name, payload, georef):
        from . import ortho

        self._limit()
        with Image.open(io.BytesIO(payload)) as full:
            full_rgb = full.convert("RGB")
            small, scale = ortho.downsample_for_inference(full_rgb, 1600)
            if getattr(self.detector, "supports_tiling", False):
                # Tiled detectors see the mosaic at higher resolution; boxes come back in that space.
                big, big_scale = ortho.downsample_for_inference(full_rgb, self.detector.max_input_side)
                factor = big_scale / scale
                predictions = []
                for det in self.detector.predict(big, tiled=True):
                    predictions.append(dict(det, bbox=[int(round(v * factor)) for v in det["bbox"]]))
            else:
                predictions = None
        if predictions is None:
            predictions = self.detector.predict(small)
        self.samples += 1
        detections = []
        for det in predictions:
            x1, y1, x2, y2 = det["bbox"]
            lat, lon = ortho.pixel_to_lonlat(georef, (x1 + x2) / 2 * scale, (y1 + y2) / 2 * scale)
            detections.append((det, lat, lon, "ortho_georef"))
        clat, clon = ortho.pixel_to_lonlat(georef, georef["width"] / 2, georef["height"] / 2)
        location = {"latitude": clat, "longitude": clon, "location_source": "ortho_georef", "timestamp": None}
        georef_store = dict(georef, bounds=ortho.bounds_latlon(georef))
        self._save(name, small, "ortho", None, location, None, georef_store, detections)
        # Keep a map overlay of the post-event mosaic.
        rel = f"overlays/incident-{self.incident['id']}/run-{self.run_id}-{safe_name(Path(name).stem)}.png"
        overlay = small.copy()
        storage.save_png(overlay, rel)
        with db.connect() as conn:
            db.insert(conn, "overlays", {"incident_id": self.incident["id"], "kind": "post", "name": name, "path": rel,
                                         "bounds_json": json.dumps(georef_store["bounds"]), "created_at": db.now()})

    def _save(self, name, image, kind, frame_seconds, location, telemetry, georef, detections):
        folder = storage.image_dir(self.incident["id"], self.run_id)
        with db.connect() as conn:
            image_id = db.insert(conn, "images", {
                "run_id": self.run_id, "incident_id": self.incident["id"], "kind": kind,
                "label": name + (f" · {frame_seconds:.1f}s" if frame_seconds is not None else ""),
                "source_file": name, "frame_seconds": frame_seconds, "width": image.width, "height": image.height,
                "path": "", "preview_path": "", "latitude": location["latitude"], "longitude": location["longitude"],
                "location_source": location["location_source"], "captured_at": location.get("timestamp"),
                "telemetry_json": json.dumps(telemetry) if telemetry else None,
                "georef_json": json.dumps(georef, default=list) if georef else None, "created_at": db.now(),
            })
            path = storage.save_jpeg(image, f"{folder}/{image_id}.jpg")
            preview = storage.save_jpeg(annotate_image(image, [d[0] for d in detections]), f"{folder}/{image_id}_preview.jpg",
                                        quality=72, max_side=360)
            db.update(conn, "images", image_id, {"path": path, "preview_path": preview})
            obs_ids = []
            for det, lat, lon, source in detections:
                if self.observation_count >= MAX_OBSERVATIONS * 5:
                    break
                obs_ids.append(db.insert(conn, "observations", {
                    "run_id": self.run_id, "incident_id": self.incident["id"], "image_id": image_id,
                    "bbox_json": json.dumps(det["bbox"]), "model_class": det["label"], "final_class": det["label"],
                    "confidence": float(det["confidence"]), "scope": det.get("scope", "detected_region"),
                    "latitude": lat if valid_coordinate(lat, lon) else None,
                    "longitude": lon if valid_coordinate(lat, lon) else None,
                    "location_source": source if valid_coordinate(lat, lon) else "missing",
                    "is_demo": 1 if det.get("is_demo") else 0, "model": self.run["model"], "threshold": self.threshold,
                    "vision_json": json.dumps({
                        "damage_class": det["label"], "confidence": det["confidence"], "rationale": det["rationale"],
                        "hazards": det.get("hazards", []), "model": det.get("model"), "source": "detector",
                    }) if det.get("rationale") else None,
                    "created_at": db.now(),
                }))
                self.observation_count += 1
        bus.publish("image.added", self.incident["id"], run_id=self.run_id, image_id=image_id, observations=len(obs_ids))
        return image_id, obs_ids

    # ---------- post-processing ----------

    def _associate_video_tracks(self):
        try:
            from . import tracking
        except ImportError:  # pragma: no cover
            return
        with db.connect() as conn:
            for source_index, (name, frames) in enumerate(self.video_tracks.items()):
                ids = tracking.associate_tracks([f[2] for f in frames])
                for (_, obs_ids, _), track_ids in zip(frames, ids):
                    for obs_id, track in zip(obs_ids, track_ids):
                        conn.execute("UPDATE observations SET track_id = ? WHERE id = ?",
                                     (self.run_id * 100000 + source_index * 1000 + int(track), obs_id))

    def _match_footprints(self):
        from .buildings import incident_footprints, match_and_store

        footprints = incident_footprints(self.incident["id"])
        if footprints:
            match_and_store(self.incident["id"], footprints, run_id=self.run_id)

    def _vision_review(self):
        if not self.settings.get("vision_review") or self.run["mode"] == "claude":
            return  # Claude-mode detections already carry Claude's reasoning
        try:
            from . import vision_review
        except ImportError:  # pragma: no cover
            return
        if not vision_review.is_configured():
            self.errors.append("Second-opinion review skipped: ANTHROPIC_API_KEY is not set.")
            return
        classes = ("Major Damage", "Destroyed") if not self.settings.get("vision_all") else tuple(SEVERITY)
        with db.connect() as conn:
            rows = db.all_rows(conn, """SELECT o.id, o.bbox_json, o.final_class, i.path FROM observations o
                JOIN images i ON i.id = o.image_id WHERE o.run_id = ? AND o.final_class IN (%s) LIMIT 200"""
                               % ",".join("?" * len(classes)), (self.run_id, *classes))
        for count, row in enumerate(rows, 1):
            self.check()
            image = storage.open_image(row["path"])
            result = vision_review.review_crop(image, db.loads(row["bbox_json"]), {"model_class": row["final_class"]})
            with db.connect() as conn:
                db.update(conn, "observations", row["id"], {"vision_json": json.dumps(result)})
            bus.publish("run.progress", self.incident["id"], run_id=self.run_id, progress=1.0,
                        message=f"Second opinion {count}/{len(rows)}")

    def _notify(self):
        cfg = get_config()
        if not (cfg.slack_webhook or cfg.webhook_url):
            return
        try:
            from . import notify
        except ImportError:  # pragma: no cover
            return
        with db.connect() as conn:
            rows = db.all_rows(conn, """SELECT o.*, i.source_file FROM observations o JOIN images i ON i.id = o.image_id
                WHERE o.run_id = ? AND o.final_class = 'Destroyed' ORDER BY o.confidence DESC""", (self.run_id,))
        if not rows:
            return
        lines = [notify.format_alert(dict(r, id=r["id"]), self.incident["name"], cfg.public_base_url or None)
                 for r in rows[:5]]
        header = f"{len(rows)} Destroyed observation(s) in run #{self.run_id} — review required."
        text = "\n".join([header, *lines] + ([f"…and {len(rows) - 5} more"] if len(rows) > 5 else []))
        if cfg.slack_webhook:
            notify.send_slack(cfg.slack_webhook, text)
        if cfg.webhook_url:
            notify.send_webhook(cfg.webhook_url, {"event": "run.destroyed_detected", "incident": self.incident["name"],
                                                  "run_id": self.run_id, "count": len(rows), "text": text},
                                secret=cfg.webhook_secret or None)
