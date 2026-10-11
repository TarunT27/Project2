"""Evaluate damage models on a prepared validation set and write a JSON report.

  # two-stage (building detector + damage classifier) on the det4 val split
  python training/evaluate.py two-stage --data datasets/xbd_det4 \
      --building-model models/xbd_building.pt --damage-model models/xbd_damage_cls.pt

  # single 4-class detector
  python training/evaluate.py det4 --data datasets/xbd_det4 --model models/xbd_det4.pt

  # classifier only, on ground-truth crops
  python training/evaluate.py cls --data datasets/xbd_cls --damage-model models/xbd_damage_cls.pt

Detection modes match predicted boxes to ground-truth buildings at IoU >= 0.5 (class-agnostic,
greedy by score), then compare damage classes. Unmatched ground truth counts as missed (FN),
unmatched predictions as false positives. Reported: per-class precision/recall/F1, confusion
matrix, macro-F1 and the xView2-style harmonic mean of per-class F1 -- both end-to-end and
on matched buildings only (which isolates the damage classifier from localisation errors).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from training.metrics import (  # noqa: E402
    classification_confusion,
    empty_matrix,
    format_table,
    localization_summary,
    summarize,
    update_detection_confusion,
)
from utils.mock_data import CLASSES  # noqa: E402

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


# ---------------------------------------------------------------- pure evaluation


def evaluate_detection(pairs, classes=CLASSES, iou_threshold=0.5):
    """pairs: iterable of (gt, preds) per image with gt=[(box, cls)], preds=[(box, cls, score)]."""
    classes = list(classes)
    cm = empty_matrix(len(classes) + 1)
    y_true, y_pred, images = [], [], 0
    for gt, preds in pairs:
        images += 1
        for t, p in update_detection_confusion(cm, classes, gt, preds, iou_threshold):
            y_true.append(t)
            y_pred.append(p)
    return {
        "images": images,
        "iou_threshold": iou_threshold,
        "end_to_end": summarize(cm, classes),
        "matched_only": summarize(classification_confusion(y_true, y_pred, classes), classes),
        "localization": localization_summary(cm, classes),
    }


def evaluate_classification(y_true, y_pred, classes=CLASSES):
    classes = list(classes)
    return {"crops": len(y_true), "classification": summarize(classification_confusion(y_true, y_pred, classes), classes)}


def read_yolo_labels(label_path, width, height, names):
    """YOLO 'cls cx cy w h' (normalised) -> [(xyxy_box, class_name)]."""
    gt = []
    path = Path(label_path)
    if not path.is_file():
        return gt
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        cls, cx, cy, bw, bh = int(parts[0]), *(float(v) for v in parts[1:5])
        gt.append(
            (
                [(cx - bw / 2) * width, (cy - bh / 2) * height, (cx + bw / 2) * width, (cy + bh / 2) * height],
                names[cls],
            )
        )
    return gt


def dataset_root(data):
    path = Path(data).expanduser()
    return path.parent if path.is_file() else path


def dataset_classes(root):
    meta = Path(root) / "dataset.json"
    if meta.is_file():
        return json.loads(meta.read_text())["classes"]
    return list(CLASSES)


# ---------------------------------------------------------------- model runners (lazy torch)


def iter_detection_samples(root, split, predict, limit=None):
    from PIL import Image

    from utils.detector import canonical_label

    names = [canonical_label(n) for n in dataset_classes(root)]
    if sorted(n for n in names if n) != sorted(CLASSES):
        raise SystemExit(f"{root} is not a 4-class damage dataset (classes={names}). Use the det4 dataset.")
    images = sorted(p for p in (Path(root) / "images" / split).iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    for i, image_path in enumerate(images[:limit] if limit else images):
        with Image.open(image_path) as img:
            rgb = img.convert("RGB")
        gt = read_yolo_labels(Path(root) / "labels" / split / f"{image_path.stem}.txt", rgb.width, rgb.height, names)
        yield gt, predict(rgb)
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(images)} images", flush=True)


def two_stage_predictor(args):
    from utils.two_stage import TwoStageDetector

    detector = TwoStageDetector(
        args.building_model,
        args.damage_model,
        confidence_threshold=args.conf,
        device=args.device,
        building_conf=args.building_conf,
        building_imgsz=args.imgsz,
    )
    return lambda rgb: [(d["bbox"], d["label"], d["building_confidence"]) for d in detector.predict(rgb)]


def det4_predictor(args):
    from ultralytics import YOLO

    from utils.detector import canonical_label

    model = YOLO(str(args.model))
    if model.task != "detect":
        raise SystemExit(f"det4 needs a detect model, got {model.task}")

    def predict(rgb):
        result = model.predict(source=rgb, conf=args.conf, imgsz=args.imgsz, device=args.device, verbose=False, max_det=300)[0]
        boxes = result.boxes
        return [
            (box, canonical_label(result.names[int(cls)]), float(score))
            for box, cls, score in zip(boxes.xyxy.cpu().tolist(), boxes.cls.cpu().tolist(), boxes.conf.cpu().tolist())
        ]

    return predict


def run_classification(args):
    from PIL import Image
    from ultralytics import YOLO

    from utils.detector import canonical_label

    model = YOLO(str(args.damage_model))
    if model.task != "classify":
        raise SystemExit(f"cls needs a classify model, got {model.task}")
    root = dataset_root(args.data) / args.split
    y_true, y_pred = [], []
    for class_dir in sorted(d for d in root.iterdir() if d.is_dir()):
        truth = canonical_label(class_dir.name)
        files = sorted(class_dir.glob("*.jpg"))
        if args.limit:
            files = files[: args.limit]
        for start in range(0, len(files), args.batch):
            crops = [Image.open(f).convert("RGB") for f in files[start : start + args.batch]]
            for result in model.predict(source=crops, imgsz=224, device=args.device, verbose=False):
                y_true.append(truth)
                y_pred.append(canonical_label(result.names[int(result.probs.top1)]))
    return evaluate_classification(y_true, y_pred)


def run(args):
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        raise SystemExit("Ultralytics/PyTorch are not installed. Run: pip install -r requirements.txt")
    if args.command == "cls":
        report = run_classification(args)
        tables = [("classification", report["classification"])]
    else:
        predict = two_stage_predictor(args) if args.command == "two-stage" else det4_predictor(args)
        report = evaluate_detection(
            iter_detection_samples(dataset_root(args.data), args.split, predict, args.limit),
            iou_threshold=args.iou,
        )
        tables = [("end-to-end", report["end_to_end"]), ("matched buildings only", report["matched_only"])]
        loc = report["localization"]
        print(
            f"\nbuilding localisation @IoU{args.iou}: P={loc['precision']:.3f} R={loc['recall']:.3f} "
            f"F1={loc['f1']:.3f} (missed {loc['missed_gt']}, false positives {loc['false_positives']})"
        )
    for title, summary in tables:
        print(f"\n== {title} ==\n{format_table(summary)}")
    report.update(
        mode=args.command,
        data=str(Path(args.data).resolve()),
        split=args.split,
        models={k: str(v) for k, v in vars(args).items() if k.endswith("model") and v},
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )
    out = Path(args.out or ROOT / "runs" / "eval" / f"{args.command}_{datetime.now():%Y%m%d_%H%M%S}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nReport written to {out}")
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["two-stage", "det4", "cls"])
    parser.add_argument("--data", required=True, help="prepared dataset folder or its data.yaml")
    parser.add_argument("--split", default="val")
    parser.add_argument("--building-model")
    parser.add_argument("--damage-model")
    parser.add_argument("--model", help="det4 weights")
    parser.add_argument("--conf", type=float, default=0.0, help="damage-class / det4 confidence floor")
    parser.add_argument("--building-conf", type=float, default=0.25)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--imgsz", type=int, default=1024)
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--limit", type=int, help="evaluate only the first N images (per class for cls)")
    parser.add_argument("--out", help="JSON report path (default: runs/eval/<mode>_<time>.json)")
    args = parser.parse_args(argv)
    needed = {"two-stage": ["building_model", "damage_model"], "det4": ["model"], "cls": ["damage_model"]}[args.command]
    missing = [f"--{n.replace('_', '-')}" for n in needed if not getattr(args, n)]
    if missing:
        parser.error(f"{args.command} requires {' '.join(missing)}")
    return args


def main(argv=None):
    run(parse_args(argv))


if __name__ == "__main__":
    main()
