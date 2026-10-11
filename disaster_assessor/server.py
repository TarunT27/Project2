"""Run the Rapid Disaster Assessment platform: python server.py  (then open http://127.0.0.1:8000).

First visit: create the administrator account. Configuration is via environment variables (see README).
Command-line tools: python -m app.cli --help
"""

import os

from app.main import app  # noqa: F401  (uvicorn server:app)

if __name__ == "__main__":
    import uvicorn

    # Localhost by default: imagery and EXIF can reveal sensitive locations. Put TLS in front before exposing it.
    uvicorn.run("app.main:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8000)),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))
