# Rapid Disaster Assessment & Rescue Planner

Upload drone footage or damage photos, get color-coded damage observations on a map, and export a severity-ranked review list (CSV / GeoJSON).

![Mission console](disaster_assessor/design/console-desktop.png)

```sh
cd disaster_assessor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-demo.txt
python server.py            # open http://127.0.0.1:8000
```

Click **Load synthetic demo**, or drop images/video onto the viewport. Use Python 3.11+.

- **Mission console** (`server.py` + `web/`): a dark, drone-style dashboard with a viewport and HUD, a telemetry card, a severity dial, a rescue map and exports.
- **Classic Streamlit app** (`streamlit run app.py`): the original three-tab interface. Both front ends share the same pipeline.

For real YOLO inference, run `pip install -r requirements.txt` and supply trusted, damage-trained weights. See [the application README](disaster_assessor/README.md) for model requirements, geolocation limits, and verification. No damage-trained checkpoint is bundled.
