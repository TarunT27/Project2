"""Detector selection. Weights load only from MODELS_DIR: PyTorch checkpoints can execute code."""

from functools import lru_cache
import json
from pathlib import Path

from utils.detector import DamageDetector

from ..config import get_config

MODES = ("demo", "single", "two_stage")


def available_models():
    folder = get_config().models_dir
    out = []
    for path in sorted(folder.glob("*.pt")):
        meta = {}
        sidecar = path.with_suffix(".json")
        if sidecar.is_file():
            try:
                meta = json.loads(sidecar.read_text())
            except ValueError:
                meta = {}
        out.append({"name": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "task": meta.get("task"),
                    "classes": meta.get("classes"), "metrics": meta.get("metrics"), "trained_at": meta.get("trained_at")})
    return out


def model_path(name):
    """Resolve a bare model filename inside MODELS_DIR; anything else is refused."""
    if not name or Path(name).name != name or not name.endswith(".pt"):
        raise ValueError("Choose a .pt model file from the models folder.")
    path = (get_config().models_dir / name).resolve()
    if not path.is_file():
        raise ValueError(f"Model {name} was not found in the models folder.")
    return path


def describe(mode, settings):
    if mode == "demo":
        return "synthetic-demo"
    if mode == "single":
        return settings.get("model") or ""
    return f"{settings.get('building_model')} + {settings.get('damage_model')}"


def build_detector(mode, threshold, settings, device=None):
    device = device or settings.get("device") or get_config().device
    if mode == "demo":
        return DamageDetector(True, confidence_threshold=threshold)
    if mode == "single":
        path = model_path(settings.get("model"))
        return _cached_single(str(path), path.stat().st_mtime_ns, threshold, device)
    if mode == "two_stage":
        building = model_path(settings.get("building_model") or get_config().building_model)
        damage = model_path(settings.get("damage_model") or get_config().damage_model)
        return _cached_two_stage(str(building), building.stat().st_mtime_ns, str(damage), damage.stat().st_mtime_ns,
                                 threshold, device)
    raise ValueError(f"Unknown inference mode {mode!r}.")


def _trained_imgsz(path, default=640):
    """Run inference at the resolution the model was trained at (recorded by training/train.py)."""
    sidecar = Path(path).with_suffix(".json")
    try:
        return int(json.loads(sidecar.read_text()).get("imgsz") or default)
    except (OSError, ValueError, TypeError, AttributeError):
        return default


@lru_cache(maxsize=2)
def _cached_single(path, _mtime, threshold, device):
    return DamageDetector(False, path, threshold, device, imgsz=_trained_imgsz(path))


@lru_cache(maxsize=2)
def _cached_two_stage(building, _bm, damage, _dm, threshold, device):
    from utils.two_stage import TwoStageDetector

    return TwoStageDetector(building, damage, confidence_threshold=threshold, device=device)
