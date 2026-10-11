"""Two-stage inference: a building detector finds footprints, a classifier grades each crop.

Stage 1: Ultralytics *detect* model trained on a single "building" class (training/train.py building).
Stage 2: Ultralytics *classify* model over the four damage classes (training/train.py damage-cls).

`predict()` returns the same dicts as `DamageDetector.predict`, so the pipeline, map and
exports work unchanged. Torch / Ultralytics are imported only when real weights are loaded.
"""

from pathlib import Path

from .detector import canonical_label
from .mock_data import CLASSES


def crop_box(image, bbox, pad=0.2, square=True):
    """Crop a building with context, shared by dataset preparation and inference.

    The box is grown by ``pad`` x its size on every side; with ``square`` the shorter side
    is expanded to the longer one (aspect-preserving after a square resize). The result is
    clipped to the image, so edge crops may be non-square.
    """
    x1, y1, x2, y2 = (float(v) for v in bbox)
    w, h = max(1.0, x2 - x1), max(1.0, y2 - y1)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    if square:
        w = h = max(w, h)
    half_w, half_h = w * (0.5 + pad), h * (0.5 + pad)
    left = max(0, int(round(cx - half_w)))
    top = max(0, int(round(cy - half_h)))
    right = min(image.width, int(round(cx + half_w)))
    bottom = min(image.height, int(round(cy + half_h)))
    right, bottom = max(right, left + 1), max(bottom, top + 1)
    return image.crop((left, top, right, bottom))


def _names(model):
    names = getattr(model, "names", None) or {}
    return dict(names) if isinstance(names, dict) else dict(enumerate(names))


def _tolist(value):
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "tolist"):
        return value.tolist()
    return list(value)


def _scalar(value):
    if hasattr(value, "item"):
        return value.item()
    return value


def _load_yolo(path, role):
    if not path or not Path(path).is_file():
        raise ValueError(f"{role} model file not found: {path!r}. Train one with training/train.py.")
    try:
        from ultralytics import YOLO
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "Ultralytics is not installed. Run: pip install -r requirements.txt"
        ) from error
    return YOLO(str(path))


class TwoStageDetector:
    def __init__(
        self,
        building_model_path=None,
        damage_model_path=None,
        confidence_threshold=0.5,
        device="cpu",
        building_conf=0.25,
        crop_pad=0.2,
        batch=32,
        building_imgsz=1024,
        damage_imgsz=224,
        max_det=300,
        _building_model=None,
        _damage_model=None,
    ):
        if not 0 <= confidence_threshold <= 1:
            raise ValueError("Confidence threshold must be between 0 and 1")
        if not 0 <= building_conf <= 1:
            raise ValueError("Building confidence must be between 0 and 1")
        if batch < 1:
            raise ValueError("Batch size must be at least 1")
        self.use_mock = False
        self.confidence_threshold = confidence_threshold
        self.device = device
        self.building_conf = building_conf
        self.crop_pad = crop_pad
        self.batch = batch
        self.building_imgsz = building_imgsz
        self.damage_imgsz = damage_imgsz
        self.max_det = max_det
        self.building_model = _building_model or _load_yolo(building_model_path, "Building")
        self.damage_model = _damage_model or _load_yolo(damage_model_path, "Damage")
        self._building_ids = self._validate_building_model(self.building_model)
        self._damage_labels = self._validate_damage_model(self.damage_model)

    @staticmethod
    def _validate_building_model(model):
        task = getattr(model, "task", None)
        if task != "detect":
            raise ValueError(f"Building model must be a YOLO detect model (task={task!r}).")
        names = _names(model)
        if len(names) == 1:
            return set(names)
        ids = {i for i, name in names.items() if "building" in str(name).lower()}
        if not ids:
            raise ValueError(
                "Building model has several classes but none named 'building' "
                f"(found {list(names.values())[:10]}). Generic COCO weights cannot find buildings."
            )
        return ids

    @staticmethod
    def _validate_damage_model(model):
        task = getattr(model, "task", None)
        if task != "classify":
            raise ValueError(f"Damage model must be a YOLO classify model (task={task!r}).")
        names = _names(model)
        labels = {i: canonical_label(name) for i, name in names.items()}
        if len(labels) != len(CLASSES) or set(labels.values()) != set(CLASSES):
            raise ValueError(
                f"Damage model must contain exactly four classes: {', '.join(CLASSES)}. "
                f"Found: {list(names.values())}"
            )
        return labels

    def detect_buildings(self, rgb):
        """Return [(bbox_xyxy_float, building_confidence)] for one RGB PIL image."""
        result = self.building_model.predict(
            source=rgb,
            conf=self.building_conf,
            imgsz=self.building_imgsz,
            device=self.device,
            verbose=False,
            max_det=self.max_det,
        )[0]
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return []
        found = []
        for box, cls, score in zip(_tolist(boxes.xyxy), _tolist(boxes.cls), _tolist(boxes.conf)):
            if int(cls) in self._building_ids and float(score) >= self.building_conf:
                found.append((box, float(score)))
        return found

    def classify_crops(self, crops):
        """Return [(label, probability, {label: probability})] for a list of PIL crops."""
        out = []
        for start in range(0, len(crops), self.batch):
            results = self.damage_model.predict(
                source=crops[start : start + self.batch],
                imgsz=self.damage_imgsz,
                device=self.device,
                verbose=False,
            )
            for result in results:
                probs = result.probs
                top1 = int(_scalar(probs.top1))
                distribution = {}
                data = getattr(probs, "data", None)
                if data is not None:
                    for i, p in enumerate(_tolist(data)):
                        if i in self._damage_labels:
                            distribution[self._damage_labels[i]] = round(float(p), 4)
                out.append((self._damage_labels[top1], float(_scalar(probs.top1conf)), distribution))
        return out

    def predict(self, image):
        rgb = image.convert("RGB")
        buildings = self.detect_buildings(rgb)
        if not buildings:
            return []
        crops = [crop_box(rgb, box, self.crop_pad) for box, _ in buildings]
        detections = []
        for (box, building_score), (label, prob, distribution) in zip(
            buildings, self.classify_crops(crops)
        ):
            if prob < self.confidence_threshold:
                continue
            x1, y1, x2, y2 = (int(round(v)) for v in box)
            detections.append(
                {
                    "bbox": [
                        max(0, x1),
                        max(0, y1),
                        min(rgb.width - 1, x2),
                        min(rgb.height - 1, y2),
                    ],
                    "label": label,
                    "confidence": prob,
                    "building_confidence": building_score,
                    "class_probabilities": distribution,
                    "is_demo": False,
                    "scope": "detected_building",
                }
            )
        return detections
