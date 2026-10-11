"""Image files under DATA_DIR. Paths stored in the database are relative and never user-supplied."""

import io
from pathlib import Path
import shutil

from PIL import Image

from .config import get_config


def root():
    return get_config().data_dir


def resolve(relative):
    """Map a stored relative path to an absolute one, refusing anything outside the data dir."""
    base = root().resolve()
    path = (base / relative).resolve()
    if base not in path.parents:
        raise ValueError("Path escapes the data directory")
    return path


def save_jpeg(image, relative, quality=88, max_side=None):
    path = resolve(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = image.convert("RGB")
    if max_side:
        out = out.copy()
        out.thumbnail((max_side, max_side))
    out.save(path, "JPEG", quality=quality)
    return relative


def save_png(image, relative):
    path = resolve(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)
    return relative


def save_bytes(data, relative):
    path = resolve(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return relative


def read_bytes(relative):
    return resolve(relative).read_bytes()


def open_image(relative):
    with Image.open(io.BytesIO(read_bytes(relative))) as img:
        img.load()
        return img.copy()


def remove_tree(relative):
    path = resolve(relative)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def upload_dir(run_id):
    return f"uploads/run-{int(run_id)}"


def image_dir(incident_id, run_id):
    return f"images/incident-{int(incident_id)}/run-{int(run_id)}"


def disk_usage():
    total = 0
    for path in Path(root()).rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total
