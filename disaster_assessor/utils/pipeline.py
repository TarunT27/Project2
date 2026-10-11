"""Shared assessment pipeline used by both the web console and the Streamlit app."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import warnings

from PIL import Image, ImageOps

from .detector import annotate_image
from .geo_utils import DEFAULT_CENTER, extract_gps_from_exif, thumbnail_data_url
from .media import sample_video_frames
from .mock_data import sample_images

Image.MAX_IMAGE_PIXELS = 30_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
VIDEO_TYPES = {".mp4", ".mov", ".avi", ".mkv"}
MAX_FILES = 20
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_SAMPLES = 60
MAX_OBSERVATIONS = 2000


@dataclass
class Settings:
    use_mock: bool = True
    confidence: float = 0.5
    model_name: str = "synthetic-demo"
    center: tuple = DEFAULT_CENTER
    override: bool = False
    interval: float = 3
    max_frames: int = 8


def empty_batch(use_mock=True):
    return {"rows": [], "images": [], "errors": [], "processed": 0, "demo": use_mock}


def run_assessment(detector, sources, settings, built_in_demo=False, progress=None):
    """Assess (name, bytes) sources, or the generated demo scenes, as one complete batch.

    `progress(done, total)` is called after each input. Per-file failures are collected
    in `errors` so one bad upload never discards the rest of the run.
    """
    batch = dict(
        empty_batch(settings.use_mock),
        threshold=settings.confidence,
        model=settings.model_name,
        assessed_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    sources = list(sample_images()) if built_in_demo else list(sources)
    total_samples = 0
    for file_index, (name, payload) in enumerate(sources):
        frame_iterator = None
        try:
            suffix = Path(name).suffix.lower()
            if built_in_demo:
                frames = [(None, payload)]
            elif suffix in VIDEO_TYPES:
                frame_iterator = sample_video_frames(
                    payload, suffix, settings.interval, settings.max_frames
                )
                frames = frame_iterator
            else:
                with Image.open(io.BytesIO(payload)) as loaded:
                    loaded.load()
                    if loaded.width * loaded.height > 30_000_000:
                        raise ValueError("Image exceeds 30 megapixels")
                    frames = [(None, loaded.copy())]
            for frame_seconds, original in frames:
                if total_samples >= MAX_SAMPLES or len(batch["rows"]) >= MAX_OBSERVATIONS:
                    raise ValueError(
                        "Run limit reached (60 images/frames or 2,000 observations). Upload a smaller batch."
                    )
                location = extract_gps_from_exif(
                    original,
                    demo_fallback=settings.use_mock,
                    center=settings.center,
                    seed=f"{name}:{frame_seconds}",
                )
                if settings.override:
                    location.update(
                        latitude=settings.center[0],
                        longitude=settings.center[1],
                        location_source="manual_reference",
                        location_is_synthetic=False,
                    )
                oriented = ImageOps.exif_transpose(original).convert("RGB")
                oriented.thumbnail((1600, 1600))
                predictions = detector.predict(oriented)
                total_samples += 1
                annotated = annotate_image(oriented, predictions)
                preview = thumbnail_data_url(annotated)
                image_id = hashlib.sha256(
                    (name + str(frame_seconds)).encode() + oriented.tobytes()
                ).hexdigest()[:16]
                batch["images"].append(
                    {
                        "label": name
                        + (f" · {frame_seconds:.1f}s" if frame_seconds is not None else ""),
                        "kind": "video" if frame_seconds is not None else "image",
                        "width": oriented.width,
                        "height": oriented.height,
                        "original": thumbnail_data_url(oriented, (1200, 900)),
                        "annotated": thumbnail_data_url(annotated, (1200, 900)),
                        "preview": preview,
                        "detections": len(predictions),
                        "classes": [d["label"] for d in predictions],
                        "location_source": location["location_source"],
                        "latitude": location["latitude"],
                        "longitude": location["longitude"],
                    }
                )
                batch["processed"] += 1
                for index, detection in enumerate(predictions):
                    if len(batch["rows"]) >= MAX_OBSERVATIONS:
                        break
                    batch["rows"].append(
                        {
                            "observation_id": f"{file_index}-{image_id}-{index}",
                            "source_file": name,
                            "frame_seconds": frame_seconds,
                            "damage_class": detection["label"],
                            "confidence": detection["confidence"],
                            "bbox": detection["bbox"],
                            "scope": detection["scope"],
                            "is_demo": settings.use_mock,
                            "model": settings.model_name,
                            "confidence_threshold": settings.confidence,
                            "review_status": "not_reviewed",
                            "assessed_at_utc": batch["assessed_at_utc"],
                            "preview": preview,
                            "image_index": len(batch["images"]) - 1,
                            **location,
                        }
                    )
        except Exception as exc:
            batch["errors"].append(f"{name}: {exc}")
        finally:
            if frame_iterator is not None:
                frame_iterator.close()
        if progress:
            progress(file_index + 1, len(sources))
    return batch
