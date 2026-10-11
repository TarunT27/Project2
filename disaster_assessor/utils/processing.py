"""Streamlit-independent, bounded assessment pipeline with cooperative cancellation."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import warnings

from PIL import Image, ImageOps

from .detector import DamageDetector, annotate_image
from .geo_utils import extract_gps_from_exif, thumbnail_data_url, valid_coordinate
from .media import sample_video_frames
from .mock_data import sample_images

VIDEO_TYPES = {".mp4", ".mov", ".avi", ".mkv"}
MAX_FILES, MAX_BYTES, MAX_SAMPLES, MAX_OBSERVATIONS = 20, 200 * 1024 * 1024, 60, 2000


@dataclass(frozen=True)
class Settings:
    demo: bool = True
    threshold: float = 0.5
    model_path: str = ""
    device: str = "cpu"
    latitude: float = 37.7749
    longitude: float = -122.4194
    override: bool = False
    interval: int = 3
    max_frames: int = 8

    def validate(self):
        if not 0 <= self.threshold <= 1 or not valid_coordinate(self.latitude, self.longitude):
            raise ValueError("Invalid threshold or reference coordinates.")
        if not 1 <= self.interval <= 30 or not 1 <= self.max_frames <= 30:
            raise ValueError("Invalid video sampling settings.")
        if not self.demo and os.environ.get("DISASTER_AUTH_MODE") == "oidc":
            model_root = Path(os.environ.get("DISASTER_MODEL_DIR", "models")).resolve()
            if not Path(self.model_path).resolve().is_relative_to(model_root):
                raise ValueError("Shared-mode weights must be provisioned inside DISASTER_MODEL_DIR.")
        if not self.demo and not Path(self.model_path).is_file():
            raise ValueError("Select a trusted local damage-trained .pt model file.")

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Input:
    name: str
    data: bytes

    @property
    def identity(self):
        return hashlib.sha256(self.name.encode() + self.data).hexdigest()


def check_inputs(inputs, demo=False):
    if demo:
        return
    if not inputs or len(inputs) > MAX_FILES or sum(len(i.data) for i in inputs) > MAX_BYTES:
        raise ValueError("Upload 1–20 files, up to 200 MB total.")
    if len({i.identity for i in inputs}) != len(inputs):
        raise ValueError("Remove duplicate files before running.")


def process(inputs, settings, cancel, progress, built_in_demo=False, previous=None, detector_factory=None):
    settings.validate()
    check_inputs(inputs, built_in_demo)
    # Retrying adds a new historical run and retains successful observations/reviews.
    import copy
    batch = copy.deepcopy(previous) if previous else {"rows": [], "images": [], "processed": 0}
    batch.update(errors=[], failed_files=[], demo=settings.demo, threshold=settings.threshold,
                 model="synthetic-demo" if settings.demo else Path(settings.model_path).name,
                 settings=settings.to_dict())
    sources = list(sample_images()) if built_in_demo else inputs
    def cancelled(start):
        if not built_in_demo:
            recorded = {f["fingerprint"] for f in batch["failed_files"]}
            for pending in sources[start:]:
                if pending.identity not in recorded:
                    batch["failed_files"].append({"name": pending.name, "fingerprint": pending.identity, "reason": "Cancelled before this file completed"})
        return batch, "cancelled"
    if cancel.is_set():
        return cancelled(0)
    detector = (detector_factory(settings) if detector_factory else DamageDetector(settings.demo, settings.model_path, settings.threshold, settings.device))
    run_time = datetime.now(timezone.utc).isoformat()
    total = len(sources)
    processed_ids = {i["image_id"] for i in batch["images"]}
    for file_index, source in enumerate(sources):
        if cancel.is_set():
            return cancelled(file_index)
        name = source[0] if built_in_demo else source.name
        fingerprint = hashlib.sha256(name.encode() + source[1].tobytes()).hexdigest() if built_in_demo else source.identity
        frame_iterator = None
        new_images, new_rows = [], []
        try:
            progress(file_index / total, f"Reading {name}")
            if built_in_demo:
                frames = [(None, source[1])]
            elif Path(name).suffix.lower() in VIDEO_TYPES:
                frame_iterator = sample_video_frames(source.data, Path(name).suffix.lower(), settings.interval, settings.max_frames)
                frames = frame_iterator
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(source.data)) as loaded:
                        if loaded.width * loaded.height > 30_000_000:
                            raise ValueError("Image exceeds 30 megapixels")
                        loaded.load()
                        frames = [(None, loaded.copy())]
            for frame_seconds, original in frames:
                if cancel.is_set():
                    # Commit only complete files; the current file can be retried without duplicates.
                    return cancelled(file_index)
                if len(batch["images"]) + len(new_images) >= MAX_SAMPLES or len(batch["rows"]) + len(new_rows) >= MAX_OBSERVATIONS:
                    raise ValueError("Run limit reached (60 images/frames or 2,000 observations). Use a smaller batch.")
                image_id = hashlib.sha256(f"{fingerprint}:{frame_seconds}".encode()).hexdigest()[:24]
                if image_id in processed_ids:
                    continue
                location = extract_gps_from_exif(original, demo_fallback=settings.demo,
                    center=(settings.latitude, settings.longitude), seed=f"{name}:{frame_seconds}")
                if settings.override:
                    location.update(latitude=settings.latitude, longitude=settings.longitude,
                                    location_source="manual_reference", location_is_synthetic=False)
                oriented = ImageOps.exif_transpose(original).convert("RGB")
                oriented.thumbnail((1600, 1600))
                progress(file_index / total, f"Assessing {name}" + (f" · {frame_seconds:.1f}s" if frame_seconds is not None else ""))
                predictions = detector.predict(oriented)
                if cancel.is_set():
                    return cancelled(file_index)
                if len(batch["rows"]) + len(new_rows) + len(predictions) > MAX_OBSERVATIONS:
                    raise ValueError("Observation limit reached. Use a smaller batch.")
                annotated = annotate_image(oriented, predictions)
                preview = thumbnail_data_url(annotated)
                new_images.append({"image_id": image_id, "source_file": name, "source_fingerprint": fingerprint,
                    "frame_seconds": frame_seconds, "label": name + (f" · {frame_seconds:.1f}s" if frame_seconds is not None else ""),
                    "original": thumbnail_data_url(oriented, (1600, 1600)),
                    "annotated": thumbnail_data_url(annotated, (1600, 1600)),
                    "size": list(oriented.size), "detections": len(predictions), **location})
                for index, detection in enumerate(predictions):
                    new_rows.append({"observation_id": f"{image_id}-{index}", "image_id": image_id,
                        "source_file": name, "source_fingerprint": fingerprint, "frame_seconds": frame_seconds,
                        "damage_class": detection["label"], "predicted_class": detection["label"],
                        "confidence": detection["confidence"], "bbox": detection["bbox"], "scope": detection["scope"],
                        "is_demo": settings.demo, "model": batch["model"], "confidence_threshold": settings.threshold,
                        "review_status": "not_reviewed", "review_notes": "", "reviewed_by": None,
                        "reviewed_at_utc": None, "assessed_at_utc": run_time, "preview": preview, **location})
            batch["images"].extend(new_images)
            batch["rows"].extend(new_rows)
            processed_ids.update(i["image_id"] for i in new_images)
            batch["processed"] = len(batch["images"])
        except Exception as exc:
            batch["errors"].append(f"{name}: {exc}")
            batch["failed_files"].append({"name": name, "fingerprint": fingerprint, "reason": str(exc)})
        finally:
            if frame_iterator is not None:
                frame_iterator.close()
        progress((file_index + 1) / total, f"Completed {file_index + 1} of {total} files")
    return batch, ("partial" if batch["images"] else "failed") if batch["errors"] else "completed"


def retry_inputs(inputs, previous):
    failed = {f["fingerprint"] for f in previous.get("failed_files", [])}
    selected = [i for i in inputs if i.identity in failed]
    if not selected:
        raise ValueError("Re-upload the original failed files to retry. Successful files will be retained.")
    # A retry must not silently mark omitted failed files as resolved.
    if {i.identity for i in selected} != failed:
        raise ValueError("Upload all failed files for this retry; filenames and file contents must match.")
    return selected
