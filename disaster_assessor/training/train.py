"""Train the damage models with Ultralytics and publish them into models/.

  python training/train.py building   --data datasets/xbd_building/data.yaml --epochs 50
  python training/train.py damage-cls --data datasets/xbd_cls --epochs 30
  python training/train.py det4       --data datasets/xbd_det4/data.yaml --epochs 80

After training, runs/<project>/<name>/weights/best.pt is copied to models/<name>.pt and
models/<name>.json records task, classes, data, epochs and final metrics.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRESETS = {
    "building": {"task": "detect", "model": "yolo11n.pt", "imgsz": 1024, "epochs": 50, "batch": 8, "name": "xbd_building"},
    "damage-cls": {"task": "classify", "model": "yolo11n-cls.pt", "imgsz": 224, "epochs": 30, "batch": 64, "name": "xbd_damage_cls"},
    "det4": {"task": "detect", "model": "yolo11n.pt", "imgsz": 1024, "epochs": 80, "batch": 8, "name": "xbd_det4"},
}


def require_ultralytics():
    try:
        from ultralytics import YOLO
    except ImportError as error:
        raise SystemExit(
            "Ultralytics/PyTorch are not installed. Run: pip install -r requirements.txt"
        ) from error
    return YOLO


def resolve_device(requested=None):
    """Explicit --device wins; otherwise CUDA GPU 0, Apple 'mps', then 'cpu'."""
    if requested:
        return requested
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "0"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def resolve_data(command, data):
    """Classification training wants the dataset folder, not a yaml file."""
    path = Path(data).expanduser()
    if not path.exists():
        raise SystemExit(f"Dataset not found: {path}. Run training/prepare_xbd.py first.")
    if command == "damage-cls" and path.is_file():
        path = path.parent
    return str(path.resolve())


def build_train_kwargs(args):
    preset = PRESETS[args.command]
    return {
        "data": resolve_data(args.command, args.data),
        "epochs": args.epochs or preset["epochs"],
        "imgsz": args.imgsz or preset["imgsz"],
        "batch": args.batch or preset["batch"],
        "device": resolve_device(args.device),
        "project": str(Path(args.project).resolve()),
        "name": args.name or preset["name"],
        "patience": args.patience,
        "workers": args.workers,
        "seed": args.seed,
        "exist_ok": args.exist_ok,
        "plots": True,
    }


def model_classes(model):
    names = getattr(model, "names", {}) or {}
    return [names[k] for k in sorted(names)] if isinstance(names, dict) else list(names)


def write_metadata(path, *, name, task, classes, data, epochs, imgsz, base_model, metrics=None, run_dir=None):
    metadata = {
        "name": name,
        "task": task,
        "classes": classes,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
        "epochs": epochs,
        "imgsz": imgsz,
        "base_model": base_model,
        "metrics": metrics or {},
        "run_dir": str(run_dir) if run_dir else None,
        "note": "Trained on xBD satellite imagery; validate on local imagery before operational use.",
    }
    Path(path).write_text(json.dumps(metadata, indent=2, default=float))
    return metadata


def publish(best_path, models_dir, name):
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / f"{name}.pt"
    shutil.copy2(best_path, target)
    return target


def train(args):
    YOLO = require_ultralytics()
    preset = PRESETS[args.command]
    kwargs = build_train_kwargs(args)
    base_model = args.model or preset["model"]
    print(f"[{args.command}] {base_model} -> {kwargs['name']} on {kwargs['data']} (device={kwargs['device']})")
    model = YOLO(base_model)
    results = model.train(**kwargs)
    trainer = model.trainer
    run_dir = Path(trainer.save_dir)
    best = Path(getattr(trainer, "best", "") or run_dir / "weights" / "best.pt")
    if not best.is_file():
        best = run_dir / "weights" / "last.pt"
    if not best.is_file():
        raise SystemExit(f"No weights found in {run_dir / 'weights'}")
    trained = YOLO(str(best))
    if trained.task != preset["task"]:
        raise SystemExit(f"Expected a {preset['task']} model, got {trained.task}")
    metrics = {}
    try:
        metrics = {k: float(v) for k, v in (getattr(results, "results_dict", None) or {}).items()}
    except (TypeError, ValueError):
        pass
    target = publish(best, args.models_dir, kwargs["name"])
    write_metadata(
        target.with_suffix(".json"),
        name=kwargs["name"],
        task=trained.task,
        classes=model_classes(trained),
        data=kwargs["data"],
        epochs=kwargs["epochs"],
        imgsz=kwargs["imgsz"],
        base_model=base_model,
        metrics=metrics,
        run_dir=run_dir,
    )
    print(f"Saved {target} and {target.with_suffix('.json')}")
    return target


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(PRESETS))
    parser.add_argument("--data", required=True, help="data.yaml (detect) or dataset folder / its data.yaml (classify)")
    parser.add_argument("--model", help="starting weights (default: yolo11n.pt / yolo11n-cls.pt)")
    parser.add_argument("--epochs", type=int)
    parser.add_argument("--imgsz", type=int)
    parser.add_argument("--batch", type=int, help="batch size (-1 = Ultralytics auto-batch on CUDA)")
    parser.add_argument("--device", help="'0', '0,1', 'mps' or 'cpu' (default: auto)")
    parser.add_argument("--project", default=str(ROOT / "runs" / "train"))
    parser.add_argument("--name", help="run name and models/<name>.pt filename")
    parser.add_argument("--models-dir", default=str(ROOT / "models"))
    parser.add_argument("--patience", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--exist-ok", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    train(parse_args(argv))


if __name__ == "__main__":
    main()
