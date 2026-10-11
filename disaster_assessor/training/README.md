# Training real damage models (xBD)

The app runs in synthetic demo mode until you give it trained weights. This folder
turns the xBD / xView2 dataset into Ultralytics datasets, trains the models, evaluates
them, and publishes them to `models/`.

**Recommended path: two stages.**

1. **Building detector.** A YOLO detect model with one class, `building`.
2. **Damage classifier.** A YOLO classify model that grades each padded building crop as
   `No Damage`, `Minor Damage`, `Major Damage` or `Destroyed`.

The alternative is **det4**: a single YOLO detector that outputs the four damage classes
directly. It plugs into the existing `DamageDetector` (one `.pt` file). It is simpler,
but it is usually weaker on the rare Minor and Major classes.

## 1. Get xBD

- Register at <https://xview2.org> and download the challenge data: `tier1`, `tier3`,
  `hold` and `test`. Each split has `images/*_{pre,post}_disaster.png` (1024×1024) and
  `labels/*.json` (building polygons in pixel WKT, plus a damage `subtype` on post images).
- **License:** xBD is released under terms aimed at research and non-commercial use
  (CC BY-NC-SA at the time of writing). Read the current terms on xview2.org before any
  commercial or operational use, including use of models trained on it.

## 2. Prepare datasets

```bash
pip install -r requirements.txt            # ultralytics + torch (training only)
python training/prepare_xbd.py --xbd-root ~/data/xBD --splits tier1 tier3 \
    --out datasets --mode building cls --balance 20000
# optional single-model path:
python training/prepare_xbd.py --xbd-root ~/data/xBD --splits tier1 tier3 --out datasets --mode det4
```

- The script reads post-disaster labels only. Each polygon becomes its bounding box.
- The train/val split is **by disaster event** (`hurricane-harvey`, `socal-fire`, …).
  The split is deterministic (`--seed`), and `--val-fraction` (default 0.15) sets the
  share of images that go to val. Tiles from one disaster never land on both sides of
  the split. To choose the val events yourself, pass `--val-events a,b`.
- Outputs:
  - `datasets/xbd_building/`: YOLO detect format, `data.yaml`, images are symlinked
    (use `--copy` to copy them instead). `--skip-unclassified` drops `un-classified`
    footprints.
  - `datasets/xbd_cls/{train,val}/<class>/*.jpg`: each crop is the bounding box plus
    20% padding, squared and resized to 224 px. Buildings under 16 px are skipped.
    `--balance N` caps the train crops at N per class (xBD is about 75% No Damage);
    val keeps the real class distribution. `crops.csv` maps every crop back to its
    source image.
  - `datasets/xbd_det4/`: YOLO detect format with the four damage classes.
    `un-classified` footprints are dropped.
- Each output folder also gets a `dataset.json` with per-class counts, which are
  printed at the end of the run. `--limit 50` runs a quick smoke test.

## 3. Train

```bash
python training/train.py building   --data datasets/xbd_building/data.yaml --epochs 50
python training/train.py damage-cls --data datasets/xbd_cls --epochs 30
python training/train.py det4       --data datasets/xbd_det4/data.yaml --epochs 80   # alternative
```

| Model | Default start weights | Default `--imgsz` |
|---|---|---|
| building, det4 | `yolo11n.pt` | 1024 |
| damage-cls | `yolo11n-cls.pt` | 224 |

Other flags: `--model yolo11s.pt` (bigger start weights), `--batch`, `--device`,
`--project`, `--name`. When training finishes, `best.pt` is copied to
`models/<name>.pt` (`xbd_building`, `xbd_damage_cls` or `xbd_det4`) and
`models/<name>.json` records the task, classes, data, epochs and final metrics.

**Hardware.** `--device` is auto-detected: CUDA `0`, then Apple `mps`, then `cpu`.
Set it explicitly with, for example, `--device 0,1` or `--device mps`.

**Time.** These are rough orders of magnitude, so plan for variance:
- **Building detector on tier1+tier3 at 1024 px:** about 2–6 h for 50 epochs on one
  modern NVIDIA GPU, roughly 3–5× that on Apple MPS, and impractical on CPU.
- **Damage classifier:** from under an hour to a few hours on a GPU.

If you hit out-of-memory errors, lower `--batch`. On CUDA, `--batch -1` lets
Ultralytics pick a batch size automatically.

## 4. Evaluate

```bash
python training/evaluate.py two-stage --data datasets/xbd_det4 \
    --building-model models/xbd_building.pt --damage-model models/xbd_damage_cls.pt
python training/evaluate.py det4 --data datasets/xbd_det4 --model models/xbd_det4.pt
python training/evaluate.py cls  --data datasets/xbd_cls --damage-model models/xbd_damage_cls.pt
```

Detection modes match predicted boxes to ground-truth buildings. The match ignores
class and requires IoU ≥ 0.5. They report:

- per-class precision, recall and F1, macro-F1, and the xView2-style **harmonic mean**
  of per-class F1, both end-to-end (missed buildings count as FN, extra boxes as FP)
  and on matched buildings only;
- building localisation precision and recall;
- a confusion matrix with a `Background` row and column.

The JSON report is written to `runs/eval/`. All the metric math lives in
`training/metrics.py`.

These numbers are box-level. The official xView2 score is pixel-level on polygons, so
the two are related but not directly comparable.

## 5. Use the models in the app

Copy the weights into `models/`, then point the app at them with environment variables
or select them in the app:

```bash
export DRA_BUILDING_MODEL=models/xbd_building.pt
export DRA_DAMAGE_MODEL=models/xbd_damage_cls.pt
```

The app builds `utils.two_stage.TwoStageDetector` from these two files. Its output has
the same format as `DamageDetector`, plus `building_confidence` and
`class_probabilities`. A det4 `.pt` file loads through the existing single-model
`DamageDetector` path instead.

## Caveats (read before relying on results)

- **Domain shift.** xBD is nadir (straight-down) satellite imagery at roughly 0.3–0.8 m
  per pixel. Models trained on it **will not transfer well** to oblique drone photos,
  street-level photos or video frames, because the scale, viewing angle, sensor and
  damage cues are all different. For those inputs, label a few hundred to a few
  thousand local images in the same four classes and fine-tune from these weights
  with `--model models/xbd_damage_cls.pt`.
- **Pre-disaster imagery.** The post-disaster image alone carries most of the damage
  signal, but the xView2 winners also used the pre-disaster image. This pipeline does
  not.
- **Decision support only.** Treat model output as a triage aid. A qualified
  assessor must confirm every finding before it drives an operational decision.
