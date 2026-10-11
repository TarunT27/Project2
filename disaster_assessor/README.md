# ⚡ Rapid Disaster Assessment & Rescue Planner

A runnable, single-page Streamlit MVP for visual damage review, approximate geospatial situational awareness, and severity-ranked exports. Three tabs: **Assess** (single/batch images and video), **Map** (interactive observations), and **Export** (CSV/GeoJSON).

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

## 2. Source files in requested order

1. `requirements.txt` + `requirements-demo.txt`: full and minimal installations.
2. `utils/geo_utils.py`: EXIF DMS parsing, timestamp, explicit provenance, demo fallback, Folium map/popups.
3. `utils/detector.py`: `DamageDetector(use_mock=True).predict(image)`; real YOLO integration and PIL box rendering.
4. `utils/mock_data.py`: deterministic synthetic detections and generated test scenes.
5. `app.py`: three-tab UI, upload/run controls, video handling, original/annotated views, class percentages, map and exports.
6. `utils/media.py`: bounded OpenCV frame sampling; temporary files deleted after decoding.
7. `utils/exports.py`: severity ranking, CSV formula escaping, GeoJSON `[longitude, latitude]` points.
8. `tests/test_core.py`: unit/integration tests including Streamlit AppTest.

## 3. Use the dashboard

- Upload JPG/JPEG, PNG, WebP, TIFF, or MP4/MOV/AVI/MKV. Up to 20 files / 200 MB combined. Video decoding depends on installed codecs; H.264 MP4 is a practical input choice.
- Select synthetic demo or real damage-trained YOLO in Settings. Expand Detection, Location, or Video settings to configure the next run.
- Run assessment. Original and annotated images appear side by side; choose another image/frame from the selector. The class distribution includes retained detections only, with all four classes shown.
- Explore clustered map observations: green = No Damage, yellow = Minor, orange = Major, red = Destroyed. Each popup carries preview, class, score, location source, and time. Multiple boxes from a single image share a capture location.
- Download CSV or GeoJSON. Rows sort Destroyed → Major → Minor → No Damage, then score, with stable tie ordering. CSV retains missing-coordinate observations; GeoJSON excludes them. All rows retain source/demo/coordinate/model/threshold/review-status metadata.
- A new run replaces the previous results. Changing controls does not silently recalculate old results. Clear session removes results; remove uploader files or close the tab to release uploaded inputs.

The MVP counts **building observations**, not unique structures. Video frames and overlapping photos may include the same building. It does not infer occupancy, confirmed rescues needed, travel access, or safe road routes. Exports are a review-priority list, not navigation instructions. No heatmap is claimed: output uses explicit bounding boxes, class counts, and map markers.

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
- Manual overrides are shared reference coordinates for the current batch; they are labeled `manual_reference`. For distinct locations, run location-specific batches or retain valid per-image EXIF. Precise per-building georeferencing, drone telemetry import, photogrammetry, and tracking are out of scope.
- EXIF identifies the **camera position**, not the detected building. It is an approximate observation location. A bounding box alone cannot locate a building on Earth.

Frames are sampled at the configured interval, limited to 30 per video and 60 total images/frames per run, with at most 2,000 observations. Images are oriented using EXIF then resized to at most 1600 × 1600 for inference. Bounding boxes refer to these inference-image pixels. Capture GPS is parsed before orientation transforms.

## 6. Privacy and runtime

Processing occurs on the machine hosting Streamlit. On localhost, that is your laptop; hosted use uploads data to the hosting server. No cloud image inference API is called. Uploaded imagery/results remain in session memory; temporary video files are deleted after decoding, including normal decode errors. Images and EXIF can contain sensitive incident/location information: protect a hosted deployment with appropriate authentication and network access. The MVP includes no authentication and should not be exposed as a public incident-data service without adding it.

OpenStreetMap tiles and Folium JavaScript/CSS are fetched externally. The map requests the viewed geographic region but does not send source image files to the tile provider. Disabling the basemap stops tile requests; it does not make Folium's externally hosted JS/CSS fully offline. Streamlit usage statistics are disabled in the local config. The synthetic demo is clearly marked in UI, popups, filenames, and each exported record.

## 7. Tests and verification

```sh
python -m unittest discover -s tests -v
python -m compileall -q app.py utils tests
pip check
```

Nine tests cover deterministic mock mode and thresholds, EXIF south/west parsing and timestamps, missing-GPS provenance, coordinate validation, export rank/order/GeoJSON and spreadsheet escaping, popup HTML escaping, bounded video sampling, rejection of generic YOLO class names, real-adapter mapping with a mocked model, and the Streamlit demo/reset/real-mode UI flow.

Completed locally: 9/9 tests; Python compilation; dependency compatibility check. Full requested dependencies installed. Browser verification: synthetic assessment creates original/annotated previews and severity percentages, map tab displays mapped observations, export tab exposes CSV/GeoJSON downloads. A damage-trained checkpoint was **not supplied or validated**; real model output quality and GPU performance remain untested. No FPS/latency or emergency readiness claims are made.

## 8. Deployment

For a trusted local network, deliberately choose a bind address and restrict access using your network controls. For Streamlit Community Cloud or another hosted service, use `disaster_assessor/app.py` as the entry point and install its requirements; configure access controls before uploading nonpublic incident data. Model files must be provisioned on that server. GPU availability depends on the host. GitHub Pages cannot run Streamlit/Python.

API references: [Ultralytics prediction](https://docs.ultralytics.com/modes/predict/), [Streamlit file uploader](https://docs.streamlit.io/develop/api-reference/widgets/st.file_uploader), [Pillow EXIF tags](https://pillow.readthedocs.io/en/stable/reference/ExifTags.html).
