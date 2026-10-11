# ⚡ Rapid Disaster Assessment & Rescue Planner

A runnable, single-page Streamlit MVP for visual damage review, approximate geospatial situational awareness, and severity-ranked exports. Five tabs: **Assess**, **Map**, **Export**, **History**, and **Model quality**. Incidents, assessments, image previews, reviews, coordinates, audit history, and evaluation reports are saved in SQLite.

## 1. Install and run

Python 3.11 or 3.12 recommended; verified on Python 3.12 / macOS ARM64.

```sh
cd disaster_assessor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Windows activation: `.venv\Scripts\activate`.

**Fastest demo-only setup:** replace the installation command with `pip install -r requirements-demo.txt`. This skips Torch, Torchvision, Ultralytics, and GeoPandas; the complete demo, video sampling, maps, and exports still work. The full requirements file includes all requested dependencies. `requirements-tested.txt` captures the complete resolved versions tested locally; platform-specific wheels, especially GPU Torch builds, may differ. For CUDA installations follow the official PyTorch platform-specific installer.

Open the printed localhost URL, then click **Load synthetic demo**. No API key, account, model download, or external image service is required for this flow.

## 2. Source files

1. `requirements.txt` + `requirements-demo.txt`: full and minimal installations.
2. `utils/geo_utils.py`: EXIF DMS parsing, timestamp, explicit provenance, demo fallback, Folium map/popups.
3. `utils/detector.py`: `DamageDetector(use_mock=True).predict(image)`; real YOLO integration and PIL box rendering.
4. `utils/mock_data.py`: deterministic synthetic detections and generated test scenes.
5. `app.py` + `styles.css`: incident selection, reviews, mapping, exports, history, model quality, and responsive UI.
6. `utils/media.py`: bounded OpenCV frame sampling; temporary files deleted after decoding.
7. `utils/exports.py`: severity ranking, CSV formula escaping, GeoJSON `[longitude, latitude]` points.
8. `utils/storage.py`, `utils/auth.py`, `utils/jobs.py`, `utils/processing.py`: persistence, permissions, OIDC gating, and bounded background processing.
9. `utils/review_ui.py`: source-linked review forms and per-image location edits.
10. `evaluate_model.py` + `utils/evaluation.py`: held-out detection evaluation and reproducible quality reports.
11. `tests/`, `browser_tests/`, `.github/workflows/ci.yml`: unit, Streamlit, and browser regression checks.
12. `Dockerfile`, `compose.yaml`, `manage_workspace.py`: shared-host packaging and consistent database backups.

## 3. Use the workspace

- Select an incident, or create one with a name, date, and location. Each account sees owned/shared incidents; configured administrators can see all incidents.
- Upload JPG/JPEG, PNG, WebP, TIFF, or MP4/MOV/AVI/MKV. Up to 20 files / 200 MB combined. The demo needs no model download.
- Run an assessment. Processing happens in a bounded worker pool (two active jobs, one per account), with progress and cancellation. Cancellation waits for the current model call to finish; only complete files are retained. Closing the browser does not cancel an in-progress job. A server restart marks incomplete runs as interrupted.
- Failed or unfinished files can be retried from the same uploader, or re-uploaded after reopening the app. Filenames and contents must match. A retry creates a new run and preserves successful observations/reviews without duplicating them. A permanently corrupt file will continue to fail until replaced in a new assessment.
- Select an image/frame and an observation. Correct its damage label, add notes, and mark it `reviewed` or `needs_follow_up`. Original predictions remain in `predicted_class`; current labels are in `damage_class`. Every edit records the actor, timestamp, before/after values, and an audit entry. Concurrent stale edits are rejected with a reload action.
- Edit an individual image/frame's capture location. Its observations share that reference point, while original coordinates and their source remain in provenance. Other images are unaffected.
- Filter the map by severity, source, and review status. Expand clusters and select a marker to open its corresponding source image and review controls. Marker color reflects the current label. Popup previews retain the original model annotation snapshot.
- Download severity-ranked CSV or GeoJSON. Both include original predictions, review notes/status/actor/time, and location/model provenance. CSV retains unmapped rows; GeoJSON excludes them. Map filters do not change exports.
- Reopen any run in History. Clear session clears the current view; it does not delete saved records or stop a background job.

Counts are **observations**, not unique buildings or confirmed rescues needed. Overlapping photos/video may include the same structure. The app does not infer occupancy, road accessibility, safe entry, navigation routes, or emergency readiness.

## 4. Real model mode

The app supports **trusted local Ultralytics YOLOv8/YOLO11-compatible damage-trained `.pt` weights**, using the installed Ultralytics API. Put weights in a separate file, such as `models/damage_best.pt`, and enter that path in the sidebar. Do not paste binary weights into `detector.py`.

```python
from PIL import Image
from utils.detector import DamageDetector

detector = DamageDetector(
    use_mock=False,
    model_path="models/damage_best.pt",
    confidence_threshold=0.5,
    device="cpu",  # or mps / cuda:0 when supported
)
boxes = detector.predict(Image.open("incident.jpg"))
```

Exactly four class names are required: `No Damage`, `Minor Damage`, `Major Damage`, `Destroyed`. Hyphens/underscores/case are normalized; `intact` / `undamaged`, `minor`, and `major` are supported aliases. Unsupported taxonomies fail clearly instead of guessing class-ID mappings. Detection/segmentation weights produce boxes; classification weights produce a **whole-image** box tagged `whole_image_classification`, which must not be read as a localized building.

**Standard `yolov8n.pt` and COCO YOLO11 weights do not classify building damage.** They are deliberately rejected instead of assigning arbitrary damage labels to generic objects. No suitable damage-trained checkpoint was supplied, so this delivery does not claim real-world damage accuracy. There is no misleading ImageNet ResNet fallback.

Model name compatibility is not scientific validation. Before operational use, validate trained weights against local building types, pre/post-event imagery, viewpoints, sensor resolution, lighting, and damage definitions. Aerial and street-level domains differ. Some xView2 methods require paired pre/post images and footprints; they cannot simply be substituted into this single-image YOLO interface. Review dataset licenses and model provenance. Load only trusted checkpoints: PyTorch weight files may execute code when deserialized by their loader.

## 5. Geospatial behavior

`extract_gps_from_exif(image, demo_fallback=True, center=(37.7749, -122.4194), seed="...")` returns a dictionary with latitude, longitude, capture timestamp, location source, and synthetic-location flag.

- Valid EXIF uses signed degrees/minutes/seconds and preserves the raw capture timestamp (timezone unknown unless provided by the source).
- Demo mode without EXIF generates a deterministic random offset near the reference point, marked `synthetic_demo` and `location_is_synthetic=True`.
- Real mode explicitly uses `demo_fallback=False`: missing GPS remains null/unmapped until a manual reference is enabled.
- Run-level manual overrides are labeled `manual_reference`. Per-image/frame edits are labeled `manual_image_reference` and retain original coordinates/source. Precise building georeferencing, drone telemetry import, photogrammetry, and tracking are out of scope.
- EXIF identifies the **camera position**, not the detected building. It is an approximate observation location. A bounding box alone cannot locate a building on Earth.

Frames are sampled at the configured interval, limited to 30 per video and 60 total images/frames per run, with at most 2,000 observations. Images are oriented using EXIF then resized to at most 1600 × 1600 for inference. Bounding boxes refer to these inference-image pixels. Capture GPS is parsed before orientation transforms.

## 6. Persistence, privacy, and backups

The default database is `data/workspace.sqlite3`, or `DISASTER_DB_PATH`. Raw uploaded files stay in session/worker memory and temporary video files are deleted after decoding. Bounded inference-image previews (up to 1600 × 1600), detection records, coordinates, reviews, and audit/evaluation history are persisted. These previews are not full-resolution source archives. Store original evidence separately if needed. Changing controls affects future runs; saved runs are not recalculated.

The database, incident imagery, local checkpoints, real evaluation datasets, and actual OIDC secrets are excluded from Git. Processing happens on the machine hosting Streamlit. OpenStreetMap tiles request the viewed region externally; Folium JS/CSS is also externally hosted. Disabling tiles is not a fully offline mode. Demo status remains in the UI, popups, and exports.

Use an encrypted host volume and restrict filesystem access for nonpublic incident data. Clear session does not erase persisted records. Revocation prevents future workspace operations; previously downloaded files cannot be recalled. The database has no automatic retention/purge policy. The first release supports one app process and one persistent SQLite volume, rather than horizontally scaled replicas.

Create a consistent backup while the app is running:

```sh
python manage_workspace.py --output backups/workspace-2026-10-11.sqlite3
```

Existing backup files are never overwritten. To restore, stop the app and replace its database with a backup, removing stale WAL/SHM sidecars from the stopped instance. Protect backups as incident data.

## 7. Tests

```sh
python -m unittest discover -s tests -v
python -m compileall -q app.py utils tests browser_tests evaluate_model.py manage_workspace.py
pip check
```

Tests cover persistence/reopening, incident isolation, viewer/editor restrictions, revocation, immutable predictions, audit trails, optimistic concurrency, per-image locations, failure isolation, retry deduplication, cancellation, bounded jobs, evaluation matching/provenance, and the original detector/media/export checks. Streamlit tests exercise incident creation, reviews, reopening, and stale-edit rejection.

Browser tests cover file uploads, partial failure/retry, saved reviews after refresh, source-linked maps, CSV/GeoJSON downloads, history, mobile overflow, and the shared-mode sign-in gate. GitHub Actions runs both suites. To run browser tests locally:

```sh
pip install -r requirements-test.txt
python -m playwright install chromium
python -m unittest discover -s browser_tests -v
```

The real checkpoint and identity-provider integration require separate verification with the actual deployment credentials/data. Unit tests inject a detector adapter for evaluation logic; they do not establish real model accuracy.

## 8. Shared hosting and access control

Local mode is the default and requires `server.address` to be `127.0.0.1`, `localhost`, or `::1`; the checked-in config binds to loopback. A public bind with local mode stops before rendering incident data. Shared mode uses Streamlit's OIDC login and requires a verified email claim plus a stable subject identifier. Existing expired token claims are rejected. Identities must be explicitly allowed, configured administrators, or invited to an incident.

1. Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml`. Configure an OIDC provider, a random cookie secret, and a redirect URI matching the final HTTPS host's `/oauth2callback`. The provider must return `email`, `email_verified=true`, and `sub`.
2. Set `DISASTER_AUTH_MODE=oidc`, `DISASTER_ADMIN_EMAILS` to a comma-separated administrator email list, and optionally `DISASTER_ALLOWED_EMAILS` for users who may create their own incidents.
3. Administrators/owners grant incident access by sign-in email. **Viewer** can read/export; **editor** can assess, review, and locate; **owner** can manage members. Server-side persistence methods check permissions for every operation, including after revocation. Periodic job updates also recheck identity/permissions.
4. Provision trusted model files under `DISASTER_MODEL_DIR` (default `models`). Shared-mode model paths outside that directory are rejected. Do not expose weights via static-file serving.
5. Place the app behind an HTTPS reverse proxy / controlled network. OIDC configuration is not bundled with real credentials and no external deployment has been created.

For a container deployment, create the configured secrets file and a `models` directory, then:

```sh
export DISASTER_ADMIN_EMAILS="your-admin@example.com"
docker compose up --build -d
```

Compose publishes to `127.0.0.1:8501` for your HTTPS reverse proxy, runs the app as a non-root user, mounts secrets/models read-only, and persists SQLite in a named volume. It defaults to OIDC and cannot open incident data without authentication. Set `INSTALL_REAL_MODEL=true` before building if you need Torch/Ultralytics inference; the default image supports the complete synthetic workflow. This repository includes no TLS domain, identity-provider credentials, damage-trained checkpoint, or production deployment.

Official guidance: [Streamlit OIDC](https://docs.streamlit.io/develop/concepts/connections/authentication), [background UI fragments](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment), [Playwright CI](https://playwright.dev/python/docs/ci).

## 9. Model evaluation

Copy `evaluation/manifest.example.json` to `evaluation/manifest.json` and replace its sample with your labeled, held-out test images. Relative image paths must stay inside the manifest directory and be unique. Annotations use the four supported damage classes and xyxy pixel boxes in the **EXIF-oriented image resized to fit 1600 × 1600**; manifest width/height must match that prepared image. Detection/segmentation weights are required for this evaluator; whole-image classification is rejected as localization.

```sh
python evaluate_model.py --manifest evaluation/manifest.json \
  --model models/damage_best.pt --threshold 0.5 --iou 0.5 \
  --output evaluation/report.json
```

The report includes per-class precision/recall and recall intervals, label confusion, unmatched regions/predictions, missed damaged/high-priority regions, model/dataset SHA-256 fingerprints, confidence/IoU thresholds, and an evaluation timestamp. Import it in Model quality and save it to the incident.

Matching is one-to-one greedy IoU matching at a fixed confidence threshold. It is not Ultralytics mAP/AP evaluation; [Ultralytics validation](https://docs.ultralytics.com/modes/val/) can complement it for a compatible dataset. `split=test` is a declaration, not proof of separation from training data. Review annotation quality, domain coverage, leakage, class imbalance, and false negatives separately. No suitable checkpoint or labeled dataset was supplied, so **real-world accuracy has not been measured**.
