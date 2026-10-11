# Rapid Disaster Assessment & Rescue Planner

A Python/Streamlit MVP for image and sampled-video damage observations, approximate geospatial mapping, and priority-review exports.

```sh
cd disaster_assessor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Start with **Load synthetic demo**. For a smaller installation without model dependencies, use `requirements-demo.txt`.

See [the application README](disaster_assessor/README.md) for model requirements, geolocation limits, and verification. No damage-trained checkpoint is bundled; real mode requires compatible, trusted weights.
