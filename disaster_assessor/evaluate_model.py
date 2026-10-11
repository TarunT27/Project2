"""Run: python evaluate_model.py --manifest evaluation/manifest.json --model models/damage.pt --output report.json"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageOps
from utils.detector import DamageDetector
from utils.evaluation import evaluate, validate_regions


def evaluate_model(manifest_path, model_path, threshold=0.5, device="cpu", match_iou=0.5):
    manifest_path, model_path = Path(manifest_path).resolve(), Path(model_path).resolve()
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("split") != "test" or not manifest.get("images"):
        raise ValueError("Manifest must declare a held-out test split with labeled images.")
    detector = DamageDetector(False, str(model_path), threshold, device)
    if detector.model.task == "classify":
        raise ValueError("Use detection/segmentation weights for bounding-box evaluation.")
    prepared, seen = [], set()
    dataset_hash = hashlib.sha256(manifest_bytes)
    for entry in manifest["images"]:
        path = (manifest_path.parent / entry["image"]).resolve()
        if not path.is_relative_to(manifest_path.parent) or path in seen:
            raise ValueError("Use unique relative image paths within the manifest directory.")
        seen.add(path)
        data = path.read_bytes()
        dataset_hash.update(hashlib.sha256(data).digest())
        with Image.open(path) as raw:
            if raw.width * raw.height > 30_000_000:
                raise ValueError("Evaluation image exceeds 30 megapixels.")
            image = ImageOps.exif_transpose(raw).convert("RGB")
        image.thumbnail((1600, 1600))
        if [entry.get("width"), entry.get("height")] != list(image.size):
            raise ValueError(f'{entry["image"]}: dimensions must match the oriented, resized inference image {image.size}.')
        annotations = entry["annotations"]
        validate_regions(annotations)
        if any(a["bbox"][2] > image.width or a["bbox"][3] > image.height for a in annotations):
            raise ValueError("An annotation extends beyond the prepared image.")
        prepared.append({"image": entry["image"], "annotations": annotations, "predictions": detector.predict(image)})
    report = evaluate(prepared, match_iou)
    report.update(generated_at_utc=datetime.now(timezone.utc).isoformat(), model=model_path.name,
                  model_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(), dataset_sha256=dataset_hash.hexdigest(),
                  threshold=threshold, declared_split="test", split_independence="Not independently verified")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--iou", type=float, default=0.5)
    args = parser.parse_args()
    try:
        report = evaluate_model(args.manifest, args.model, args.threshold, args.device, args.iou)
        Path(args.output).write_text(json.dumps(report, indent=2, allow_nan=False))
        print(f'Evaluated {report["image_count"]} test images. Report: {args.output}')
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Evaluation failed: {exc}\n")


if __name__ == "__main__":
    main()
