# Rapid Disaster Assessment & Rescue Planner

A self-hosted platform for community emergency response. It takes drone footage, photos and orthomosaics, detects and maps building damage, and supports human review. It turns high-priority buildings into crew tasks with an offline field app, and exports situation reports or shares a read-only view with partner agencies.

![Console](disaster_assessor/design/console-desktop.png)

```sh
cd disaster_assessor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-demo.txt
python server.py            # open http://127.0.0.1:8000 and create the admin account
```

Or run `docker compose up -d` from `disaster_assessor/`.

Full documentation covering workflow, roles, locations, models, configuration and security is in [disaster_assessor/README.md](disaster_assessor/README.md). Model training is in [disaster_assessor/training/README.md](disaster_assessor/training/README.md).

No damage-trained model is bundled. Demo mode uses synthetic detections, and real use requires trusted, validated weights.
