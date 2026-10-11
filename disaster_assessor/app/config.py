"""Runtime configuration from environment variables (all optional)."""

from dataclasses import dataclass, field
from functools import lru_cache
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def load_dotenv(path=None):
    """Read KEY=VALUE lines from disaster_assessor/.env into the environment (real env vars win).

    Called by server.py and the CLI only, never on import, so tests can't pick up a real API key.
    """
    path = Path(path or ROOT / ".env")
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        # Skip blanks: an empty ANTHROPIC_API_KEY would shadow an `ant auth login` profile in the SDK.
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    get_config.cache_clear()
    return loaded


def _bool(name, default=False):
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    data_dir: Path
    models_dir: Path
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    session_hours: int = 12
    secure_cookies: bool = False
    public_base_url: str = ""
    # Map tiles. Bulk-prefetching OpenStreetMap tiles violates its usage policy; point
    # this at your own tile server (or MBTiles proxy) for offline field use.
    tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tile_attribution: str = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    osrm_url: str = ""
    slack_webhook: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    vision_review_default: bool = False
    retention_days: int = 0
    keep_uploads: bool = False
    device: str = "cpu"
    building_model: str = ""
    damage_model: str = ""
    allowed_origins: tuple = field(default_factory=tuple)
    # Single-user local mode: no sign-in, everyone reaching the server is the built-in local admin.
    # Only honoured on a loopback bind; never use it behind a reverse proxy.
    no_login: bool = False

    @property
    def db_path(self):
        return self.data_dir / "dra.sqlite3"


@lru_cache(maxsize=1)
def get_config():
    data_dir = Path(os.environ.get("DRA_DATA_DIR", ROOT / "data")).expanduser().resolve()
    models_dir = Path(os.environ.get("DRA_MODELS_DIR", ROOT / "models")).expanduser().resolve()
    for folder in (data_dir, models_dir):
        folder.mkdir(parents=True, exist_ok=True)
    return Config(
        data_dir=data_dir,
        models_dir=models_dir,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 8000)),
        workers=max(1, int(os.environ.get("DRA_WORKERS", 1))),
        session_hours=int(os.environ.get("DRA_SESSION_HOURS", 12)),
        secure_cookies=_bool("DRA_SECURE_COOKIES"),
        public_base_url=os.environ.get("DRA_PUBLIC_URL", "").rstrip("/"),
        tile_url=os.environ.get("DRA_TILE_URL", Config.tile_url),
        tile_attribution=os.environ.get("DRA_TILE_ATTRIBUTION", Config.tile_attribution),
        osrm_url=os.environ.get("DRA_OSRM_URL", "").rstrip("/"),
        slack_webhook=os.environ.get("DRA_SLACK_WEBHOOK", ""),
        webhook_url=os.environ.get("DRA_WEBHOOK_URL", ""),
        webhook_secret=os.environ.get("DRA_WEBHOOK_SECRET", ""),
        vision_review_default=_bool("DRA_VISION_REVIEW"),
        retention_days=int(os.environ.get("DRA_RETENTION_DAYS", 0)),
        keep_uploads=_bool("DRA_KEEP_UPLOADS"),
        device=os.environ.get("DRA_DEVICE", "cpu"),
        building_model=os.environ.get("DRA_BUILDING_MODEL", ""),
        damage_model=os.environ.get("DRA_DAMAGE_MODEL", ""),
        allowed_origins=tuple(o for o in os.environ.get("DRA_ALLOWED_ORIGINS", "").split(",") if o),
        no_login=_bool("DRA_NO_LOGIN"),
    )
