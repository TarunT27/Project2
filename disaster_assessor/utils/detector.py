"""Mock-first detection; real inference requires trusted, damage-trained YOLO weights."""

from pathlib import Path
from PIL import ImageDraw, ImageFont
from .mock_data import CLASSES, COLORS, mock_predictions


def canonical_label(name):
    normalized = str(name).strip().lower().replace("_", " ").replace("-", " ")
    aliases = {
        "no damage": "No Damage",
        "intact": "No Damage",
        "undamaged": "No Damage",
        "minor damage": "Minor Damage",
        "minor": "Minor Damage",
        "major damage": "Major Damage",
        "major": "Major Damage",
        "destroyed": "Destroyed",
    }
    return aliases.get(normalized)


class DamageDetector:
    def __init__(
        self, use_mock=True, model_path=None, confidence_threshold=0.5, device="cpu"
    ):
        if not 0 <= confidence_threshold <= 1:
            raise ValueError("Confidence threshold must be between 0 and 1")
        self.use_mock, self.confidence_threshold, self.device = (
            use_mock,
            confidence_threshold,
            device,
        )
        self.model = None
        if not use_mock:
            if not model_path or not Path(model_path).is_file():
                raise ValueError(
                    "Select a local damage-trained .pt file. Generic yolov8n.pt COCO weights cannot classify building damage."
                )
            # Lazy import keeps the entire demo usable without Torch / model downloads.
            from ultralytics import YOLO

            self.model = YOLO(str(model_path))
            names = (
                list(self.model.names.values())
                if isinstance(self.model.names, dict)
                else list(self.model.names)
            )
            labels = [canonical_label(name) for name in names]
            if set(labels) != set(CLASSES) or len(labels) != len(CLASSES):
                raise ValueError(
                    f"Weights must contain exactly four damage classes: {', '.join(CLASSES)}. Found: {names}"
                )
            if self.model.task not in {"detect", "segment", "classify"}:
                raise ValueError(
                    "Use YOLO detection, segmentation, or classification weights."
                )

    def predict(self, image):
        """Return pixel-space xyxy boxes, severity labels, scores, and demo provenance.

        Classification models label the entire image; they do not locate buildings.
        """
        rgb = image.convert("RGB")
        if self.use_mock:
            detections = mock_predictions(rgb)
        else:
            result = self.model.predict(
                source=rgb,
                conf=self.confidence_threshold,
                imgsz=640,
                device=self.device,
                verbose=False,
                max_det=100,
            )[0]
            detections = []
            if result.probs is not None:
                index = result.probs.top1
                detections.append(
                    {
                        "bbox": [0, 0, rgb.width - 1, rgb.height - 1],
                        "label": canonical_label(result.names[index]),
                        "confidence": float(result.probs.top1conf.item()),
                        "is_demo": False,
                        "scope": "whole_image_classification",
                    }
                )
            elif result.boxes is not None:
                for box, cls, score in zip(
                    result.boxes.xyxy.cpu().tolist(),
                    result.boxes.cls.cpu().tolist(),
                    result.boxes.conf.cpu().tolist(),
                ):
                    detections.append(
                        {
                            "bbox": [int(v) for v in box],
                            "label": canonical_label(result.names[int(cls)]),
                            "confidence": float(score),
                            "is_demo": False,
                            "scope": "detected_region",
                        }
                    )
        return [
            dict(d, scope=d.get("scope", "synthetic_region"))
            for d in detections
            if d["confidence"] >= self.confidence_threshold
        ]


def annotate_image(image, detections):
    result = image.convert("RGB").copy()
    draw = ImageDraw.Draw(result)
    font = ImageFont.load_default(size=max(14, min(28, image.width // 45)))
    for detection in detections:
        box = detection["bbox"]
        color = COLORS[detection["label"]]
        draw.rectangle(box, outline=color, width=max(3, image.width // 300))
        text = f'{detection["label"]} {detection["confidence"]:.0%}'
        if detection["is_demo"]:
            text = "DEMO " + text
        x, y = max(0, box[0]), max(0, box[1] - 24)
        bounds = draw.textbbox((x, y), text, font=font)
        draw.rectangle(bounds, fill="#101827")
        draw.text((x, y), text, fill="white", font=font)
    return result
