"""Deterministic synthetic data, exclusively for explicitly selected demo mode."""

import hashlib
import random
from PIL import Image, ImageDraw

CLASSES = ("No Damage", "Minor Damage", "Major Damage", "Destroyed")
COLORS = {
    "No Damage": "#16a34a",
    "Minor Damage": "#eab308",
    "Major Damage": "#f97316",
    "Destroyed": "#dc2626",
}
SEVERITY = {name: i for i, name in enumerate(CLASSES)}


def mock_predictions(image):
    """Plausible UI boxes, NOT image-based damage analysis or calibrated confidence."""
    rng = random.Random(hashlib.sha256(image.resize((32, 32)).tobytes()).digest())
    w, h = image.size
    detections = []
    for i in range(rng.randint(2, 5)):
        x, y = rng.uniform(0.04, 0.62), rng.uniform(0.08, 0.62)
        box = [
            int(x * w),
            int(y * h),
            min(w - 1, int((x + rng.uniform(0.15, 0.3)) * w)),
            min(h - 1, int((y + rng.uniform(0.16, 0.3)) * h)),
        ]
        detections.append(
            {
                "bbox": box,
                "label": CLASSES[i % 4],
                "confidence": round(rng.uniform(0.65, 0.97), 3),
                "is_demo": True,
            }
        )
    return detections


def sample_images():
    """Generated test scene illustrations; never represented as disaster photographs."""
    for i in range(3):
        image = Image.new("RGB", (960, 640), (210 - i * 12, 223 - i * 8, 221))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 280, 960, 330), fill="#73828b")
        draw.rectangle((455, 0, 495, 640), fill="#73828b")
        for x, y in [(80, 70), (540, 65), (95, 390), (565, 390)]:
            draw.rectangle(
                (x, y, x + 240, y + 150), fill="#d8c9ac", outline="#64748b", width=4
            )
            draw.polygon(
                [(x - 12, y), (x + 120, y - 48), (x + 252, y)],
                fill=["#778793", "#a08b79", "#8b939c"][i],
            )
        draw.text(
            (20, 605),
            f"SYNTHETIC TEST SCENE {i+1} - not a real incident",
            fill="#172b3a",
        )
        yield f"DEMO_scene_{i+1}.png", image
