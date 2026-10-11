"""Convert xBD / xView2 post-disaster labels into Ultralytics datasets.

Modes (any combination, each written to its own sub-folder of --out):
  building  YOLO detect, one class "building"            -> <out>/xbd_building/data.yaml
  cls       YOLO classify crops, 4 damage-class folders  -> <out>/xbd_cls/{train,val}/<class>/*.jpg
  det4      YOLO detect, the 4 damage classes            -> <out>/xbd_det4/data.yaml

The train/val split is by disaster *event* (e.g. "hurricane-harvey"), never by image, so
neighbouring tiles of the same disaster cannot leak between train and validation.

Example:
  python training/prepare_xbd.py --xbd-root ~/data/xBD --splits tier1 tier3 \
      --out datasets --mode building cls --balance 20000
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from utils.mock_data import CLASSES  # noqa: E402
from utils.two_stage import crop_box  # noqa: E402

SUBTYPE_TO_CLASS = {
    "no-damage": "No Damage",
    "minor-damage": "Minor Damage",
    "major-damage": "Major Damage",
    "destroyed": "Destroyed",
}
IMAGE_SUFFIXES = (".png", ".tif", ".tiff", ".jpg", ".jpeg")
MODES = ("building", "cls", "det4")
_NUMBER = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_PAIR = re.compile(rf"({_NUMBER})\s+({_NUMBER})")


# ---------------------------------------------------------------- pure helpers


def parse_wkt_points(wkt):
    """All (x, y) vertices of a WKT POLYGON / MULTIPOLYGON (holes included)."""
    text = str(wkt).strip()
    if not text.upper().startswith(("POLYGON", "MULTIPOLYGON")):
        raise ValueError(f"Unsupported WKT geometry: {text[:30]!r}")
    points = [(float(x), float(y)) for x, y in _PAIR.findall(text)]
    if not points:
        raise ValueError("WKT polygon has no coordinates")
    return points


def wkt_bbox(wkt):
    """[x1, y1, x2, y2] bounding box of a WKT polygon in its own (pixel) coordinates."""
    points = parse_wkt_points(wkt)
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def clip_bbox(bbox, width, height):
    x1, y1, x2, y2 = bbox
    return [
        min(max(x1, 0.0), width),
        min(max(y1, 0.0), height),
        min(max(x2, 0.0), width),
        min(max(y2, 0.0), height),
    ]


def yolo_label_line(class_id, bbox, width, height, min_pixels=1.0):
    """'cls cx cy w h' normalised to [0, 1]; None if the clipped box is degenerate."""
    x1, y1, x2, y2 = clip_bbox(bbox, width, height)
    bw, bh = x2 - x1, y2 - y1
    if bw < min_pixels or bh < min_pixels:
        return None
    return (
        f"{int(class_id)} {(x1 + bw / 2) / width:.6f} {(y1 + bh / 2) / height:.6f} "
        f"{bw / width:.6f} {bh / height:.6f}"
    )


def event_name(stem):
    """'hurricane-harvey_00000123_post_disaster' -> 'hurricane-harvey'."""
    base = re.sub(r"_(post|pre)_disaster$", "", Path(stem).stem if "." in stem else stem)
    return base.rsplit("_", 1)[0] if "_" in base else base


def _event_rank(event, seed):
    return hashlib.sha256(f"{seed}:{event}".encode()).hexdigest()


def split_events(event_counts, val_fraction=0.15, seed=0, val_events=None):
    """Deterministically assign whole events to train / val.

    Events are visited in a seeded hash order (independent of input order) and added to
    val while the val image count is below ``val_fraction`` of all images, skipping an
    event that would overshoot the target by more than 50%. Returns (train, val) sets.
    """
    events = sorted(event_counts)
    if val_events:
        val = {e for e in val_events if e in event_counts}
        return set(events) - val, val
    if val_fraction <= 0 or len(events) < 2:
        return set(events), set()
    total = sum(event_counts.values())
    target = val_fraction * total
    val, val_count = set(), 0
    for event in sorted(events, key=lambda e: _event_rank(e, seed)):
        if val_count >= target or len(val) == len(events) - 1:
            break
        n = event_counts[event]
        if val_count + n <= target * 1.5:
            val.add(event)
            val_count += n
    if not val:
        val.add(min(events, key=lambda e: (abs(event_counts[e] - target), _event_rank(e, seed))))
    return set(events) - val, val


# ---------------------------------------------------------------- reading xBD


@dataclass
class Sample:
    stem: str
    event: str
    image_path: Path
    label_path: Path
    width: int
    height: int
    buildings: list = field(default_factory=list)  # [(bbox, class_name_or_None)]


def find_split_dirs(xbd_root, splits=None):
    root = Path(xbd_root).expanduser()
    if splits:
        dirs = [root / s for s in splits]
    elif (root / "labels").is_dir():
        dirs = [root]
    else:
        dirs = sorted(d for d in root.iterdir() if (d / "labels").is_dir() and (d / "images").is_dir())
    missing = [str(d) for d in dirs if not (d / "labels").is_dir()]
    if missing or not dirs:
        raise FileNotFoundError(f"No xBD <split>/images + <split>/labels folders found: {missing or root}")
    return dirs


def _find_image(images_dir, stem):
    for suffix in IMAGE_SUFFIXES:
        candidate = images_dir / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def load_samples(xbd_root, splits=None, limit=None):
    """Read every *_post_disaster.json label (pre-disaster files are ignored)."""
    samples, skipped = [], Counter()
    for split_dir in find_split_dirs(xbd_root, splits):
        for label_path in sorted((split_dir / "labels").glob("*_post_disaster.json")):
            stem = label_path.stem
            image_path = _find_image(split_dir / "images", stem)
            if image_path is None:
                skipped["missing_image"] += 1
                continue
            data = json.loads(label_path.read_text())
            with Image.open(image_path) as img:
                width, height = img.size
            buildings = []
            for feature in data.get("features", {}).get("xy", []):
                props = feature.get("properties", {})
                if props.get("feature_type", "building") != "building":
                    continue
                try:
                    bbox = wkt_bbox(feature["wkt"])
                except (KeyError, ValueError):
                    skipped["bad_polygon"] += 1
                    continue
                buildings.append((bbox, SUBTYPE_TO_CLASS.get(props.get("subtype"))))
            samples.append(
                Sample(stem, event_name(stem), image_path, label_path, width, height, buildings)
            )
            if limit and len(samples) >= limit:
                return samples, skipped
    return samples, skipped


# ---------------------------------------------------------------- writers


def _yaml_quote(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_data_yaml(path, dataset_root, names, train="images/train", val="images/val", extra=None):
    lines = [
        "# Generated by training/prepare_xbd.py",
        f"path: {_yaml_quote(Path(dataset_root).resolve())}",
        f"train: {train}",
        f"val: {val}",
        f"nc: {len(names)}",
        "names:",
    ] + [f"  {i}: {_yaml_quote(n)}" for i, n in enumerate(names)]
    for key, value in (extra or {}).items():
        lines.append(f"# {key}: {value}")
    Path(path).write_text("\n".join(lines) + "\n")


def _place_image(src, dst, copy):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    if copy:
        shutil.copy2(src, dst)
    else:
        os.symlink(Path(src).resolve(), dst)


def write_detection_dataset(samples, split_of, out_dir, mode, copy=False, skip_unclassified=False):
    """YOLO detect layout: images/{split}/*.png + labels/{split}/*.txt + data.yaml."""
    out_dir = Path(out_dir)
    names = ["building"] if mode == "building" else list(CLASSES)
    class_index = {name: i for i, name in enumerate(CLASSES)}
    counts = defaultdict(Counter)
    for sample in samples:
        split = split_of[sample.event]
        lines = []
        for bbox, cls in sample.buildings:
            if mode == "building":
                if cls is None and skip_unclassified:
                    counts[split]["skipped_unclassified"] += 1
                    continue
                class_id, counted = 0, "building"
            else:
                if cls is None:
                    counts[split]["skipped_unclassified"] += 1
                    continue
                class_id, counted = class_index[cls], cls
            line = yolo_label_line(class_id, bbox, sample.width, sample.height)
            if line is None:
                counts[split]["skipped_degenerate"] += 1
                continue
            lines.append(line)
            counts[split][counted] += 1
        image_dst = out_dir / "images" / split / f"{sample.stem}{sample.image_path.suffix}"
        _place_image(sample.image_path, image_dst, copy)
        label_dst = out_dir / "labels" / split / f"{sample.stem}.txt"
        label_dst.parent.mkdir(parents=True, exist_ok=True)
        label_dst.write_text("\n".join(lines) + ("\n" if lines else ""))
        counts[split]["images"] += 1
    for split in ("train", "val"):
        (out_dir / "images" / split).mkdir(parents=True, exist_ok=True)
        (out_dir / "labels" / split).mkdir(parents=True, exist_ok=True)
    write_data_yaml(out_dir / "data.yaml", out_dir, names, extra={"mode": mode})
    return names, counts


def write_classification_dataset(
    samples,
    split_of,
    out_dir,
    crop_pad=0.2,
    min_size=16,
    crop_size=224,
    balance=None,
    seed=0,
):
    """YOLO classify layout: {train,val}/<class name>/*.jpg (+ crops.csv manifest)."""
    out_dir = Path(out_dir)
    counts = defaultdict(Counter)
    records = defaultdict(list)  # (split, cls) -> [(sample_index, building_index, bbox)]
    for si, sample in enumerate(samples):
        split = split_of[sample.event]
        for bi, (bbox, cls) in enumerate(sample.buildings):
            if cls is None:
                counts[split]["skipped_unclassified"] += 1
                continue
            x1, y1, x2, y2 = clip_bbox(bbox, sample.width, sample.height)
            if min(x2 - x1, y2 - y1) < min_size:
                counts[split]["skipped_small"] += 1
                continue
            records[(split, cls)].append((si, bi, [x1, y1, x2, y2]))
    keep = []
    for (split, cls), items in sorted(records.items()):
        if balance and split == "train" and len(items) > balance:
            counts[split]["capped_by_balance"] += len(items) - balance
            items = sorted(random.Random(f"{seed}:{cls}").sample(items, balance))
        keep.extend((si, bi, bbox, split, cls) for si, bi, bbox in items)
    for split in ("train", "val"):
        for cls in CLASSES:
            (out_dir / split / cls).mkdir(parents=True, exist_ok=True)
    keep.sort(key=lambda r: (r[0], r[1]))
    manifest_rows = []
    current_index, current_image = None, None
    for si, bi, bbox, split, cls in keep:
        sample = samples[si]
        if si != current_index:
            if current_image is not None:
                current_image.close()
            current_image = Image.open(sample.image_path).convert("RGB")
            current_index = si
        crop = crop_box(current_image, bbox, crop_pad).resize((crop_size, crop_size), Image.BILINEAR)
        rel = Path(split) / cls / f"{sample.stem}_{bi:04d}.jpg"
        crop.save(out_dir / rel, quality=92)
        counts[split][cls] += 1
        manifest_rows.append([rel.as_posix(), sample.stem, sample.event, split, cls] + [round(v, 1) for v in bbox])
    if current_image is not None:
        current_image.close()
    with open(out_dir / "crops.csv", "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["crop", "source", "event", "split", "class", "x1", "y1", "x2", "y2"])
        writer.writerows(manifest_rows)
    # Ultralytics classify accepts the folder itself; data.yaml documents the layout.
    write_data_yaml(out_dir / "data.yaml", out_dir, list(CLASSES), train="train", val="val", extra={"mode": "cls"})
    return list(CLASSES), counts


# ---------------------------------------------------------------- orchestration


def prepare(
    xbd_root,
    out_dir,
    modes=("building", "cls"),
    splits=None,
    val_fraction=0.15,
    seed=0,
    val_events=None,
    copy_images=False,
    skip_unclassified=False,
    crop_pad=0.2,
    min_size=16,
    crop_size=224,
    balance=None,
    limit=None,
    verbose=True,
):
    samples, skipped = load_samples(xbd_root, splits, limit)
    if not samples:
        raise FileNotFoundError(f"No *_post_disaster.json labels with images under {xbd_root}")
    event_counts = Counter(s.event for s in samples)
    train_events, val_events_set = split_events(event_counts, val_fraction, seed, val_events)
    split_of = {e: "train" for e in train_events} | {e: "val" for e in val_events_set}
    out_dir = Path(out_dir)
    summary = {
        "xbd_root": str(Path(xbd_root).expanduser().resolve()),
        "images": len(samples),
        "skipped": dict(skipped),
        "train_events": sorted(train_events),
        "val_events": sorted(val_events_set),
        "datasets": {},
    }
    for mode in modes:
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode!r}; choose from {MODES}")
        target = out_dir / f"xbd_{mode}"
        if mode == "cls":
            names, counts = write_classification_dataset(
                samples, split_of, target, crop_pad, min_size, crop_size, balance, seed
            )
        else:
            names, counts = write_detection_dataset(
                samples, split_of, target, mode, copy_images, skip_unclassified
            )
        info = {
            "path": str(target.resolve()),
            "data": str((target / "data.yaml").resolve()),
            "classes": names,
            "counts": {split: dict(counts[split]) for split in ("train", "val")},
        }
        (target / "dataset.json").write_text(
            json.dumps(dict(info, train_events=summary["train_events"], val_events=summary["val_events"]), indent=2)
        )
        summary["datasets"][mode] = info
        if verbose:
            print_counts(mode, names, counts)
    if verbose:
        print(f"\nimages: {len(samples)}  events train={len(train_events)} val={len(val_events_set)}")
        print(f"val events: {', '.join(sorted(val_events_set)) or '-'}")
        if skipped:
            print(f"skipped: {dict(skipped)}")
    return summary


def print_counts(mode, names, counts):
    keys = list(names) + sorted(
        {k for split in counts.values() for k in split} - set(names)
    )
    print(f"\n[{mode}]")
    print(f"{'':<24}{'train':>10}{'val':>10}")
    for key in keys:
        print(f"{key:<24}{counts['train'].get(key, 0):>10}{counts['val'].get(key, 0):>10}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--xbd-root", required=True, help="xBD root (contains tier1/, tier3/, ... or images/+labels/)")
    parser.add_argument("--splits", nargs="*", help="sub-folders to read, e.g. tier1 tier3 (default: all found)")
    parser.add_argument("--out", default="datasets", help="output folder (default: datasets)")
    parser.add_argument("--mode", nargs="+", default=["building", "cls"], choices=MODES)
    parser.add_argument("--val-fraction", type=float, default=0.15)
    parser.add_argument("--val-events", help="comma-separated events forced into val (overrides --val-fraction)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--copy", action="store_true", help="copy images instead of symlinking them")
    parser.add_argument("--skip-unclassified", action="store_true", help="building mode: drop 'un-classified' footprints")
    parser.add_argument("--crop-pad", type=float, default=0.2)
    parser.add_argument("--min-size", type=int, default=16, help="cls: skip buildings smaller than this (px)")
    parser.add_argument("--crop-size", type=int, default=224)
    parser.add_argument("--balance", type=int, help="cls: cap train crops per class at N")
    parser.add_argument("--limit", type=int, help="only read the first N labelled images (smoke tests)")
    args = parser.parse_args(argv)
    prepare(
        args.xbd_root,
        args.out,
        modes=args.mode,
        splits=args.splits,
        val_fraction=args.val_fraction,
        seed=args.seed,
        val_events=[e.strip() for e in args.val_events.split(",")] if args.val_events else None,
        copy_images=args.copy,
        skip_unclassified=args.skip_unclassified,
        crop_pad=args.crop_pad,
        min_size=args.min_size,
        crop_size=args.crop_size,
        balance=args.balance,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
