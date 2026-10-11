"""SQLite persistence. One short-lived connection per unit of work; WAL for concurrent readers."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import threading

from .config import get_config

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','coordinator','reviewer','field')),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  center_lat REAL, center_lon REAL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','cancelled')),
  mode TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  threshold REAL NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}',
  progress REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  total_inputs INTEGER NOT NULL DEFAULT 0,
  processed INTEGER NOT NULL DEFAULT 0,
  errors_json TEXT NOT NULL DEFAULT '[]',
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT
);

CREATE TABLE IF NOT EXISTS images (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  kind TEXT NOT NULL CHECK (kind IN ('image','video_frame','ortho','field_photo')),
  label TEXT NOT NULL,
  source_file TEXT NOT NULL,
  frame_seconds REAL,
  width INTEGER, height INTEGER,
  path TEXT NOT NULL,
  preview_path TEXT NOT NULL,
  latitude REAL, longitude REAL,
  location_source TEXT NOT NULL DEFAULT 'missing',
  captured_at TEXT,
  telemetry_json TEXT,
  georef_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  bbox_json TEXT NOT NULL,
  model_class TEXT NOT NULL,
  final_class TEXT NOT NULL,
  confidence REAL NOT NULL,
  scope TEXT NOT NULL DEFAULT 'detected_region',
  latitude REAL, longitude REAL,
  location_source TEXT NOT NULL DEFAULT 'missing',
  is_demo INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL DEFAULT '',
  threshold REAL,
  status TEXT NOT NULL DEFAULT 'not_reviewed'
    CHECK (status IN ('not_reviewed','confirmed','corrected','rejected','added')),
  reviewer_id INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  review_note TEXT NOT NULL DEFAULT '',
  hazards_json TEXT NOT NULL DEFAULT '[]',
  footprint_id INTEGER,
  track_id INTEGER,
  vision_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_incident ON observations(incident_id, status);
CREATE INDEX IF NOT EXISTS idx_obs_image ON observations(image_id);

CREATE TABLE IF NOT EXISTS review_events (
  id INTEGER PRIMARY KEY,
  observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id),
  action TEXT NOT NULL,
  before_json TEXT, after_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crews (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  contact TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crew_members (
  crew_id INTEGER NOT NULL REFERENCES crews(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (crew_id, user_id)
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  damage_class TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','assigned','in_progress','searched_clear','needs_usar','done','cancelled')),
  crew_id INTEGER REFERENCES crews(id) ON DELETE SET NULL,
  latitude REAL, longitude REAL,
  notes TEXT NOT NULL DEFAULT '',
  hazards_json TEXT NOT NULL DEFAULT '[]',
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS field_reports (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id),
  client_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  hazards_json TEXT NOT NULL DEFAULT '[]',
  latitude REAL, longitude REAL,
  reported_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS footprints (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  source TEXT NOT NULL,
  polygon_json TEXT NOT NULL,
  centroid_lat REAL NOT NULL, centroid_lon REAL NOT NULL,
  min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
  properties_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE (incident_id, external_id)
);

CREATE TABLE IF NOT EXISTS overlays (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('pre','post')),
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  bounds_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS share_links (
  token_hash TEXT PRIMARY KEY,
  incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  label TEXT NOT NULL DEFAULT '',
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  action TEXT NOT NULL,
  target_type TEXT, target_id INTEGER,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
"""

_init_lock = threading.Lock()
_initialized = set()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dict_factory(cursor, row):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def init_db(path=None):
    path = str(path or get_config().db_path)
    with _init_lock:
        if path in _initialized:
            return
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        finally:
            conn.close()
        _initialized.add(path)


@contextmanager
def connect(path=None):
    """Yield a connection inside a transaction: commit on success, roll back on error."""
    path = str(path or get_config().db_path)
    init_db(path)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def all_rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def insert(conn, table, values):
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())).lastrowid


def update(conn, table, row_id, values, key="id"):
    if not values:
        return
    sets = ", ".join(f"{col} = ?" for col in values)
    conn.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*values.values(), row_id))


def audit(conn, user_id, action, target_type=None, target_id=None, **detail):
    insert(conn, "audit_log", {
        "user_id": user_id, "action": action, "target_type": target_type, "target_id": target_id,
        "detail_json": json.dumps(detail, default=str), "created_at": now(),
    })


def loads(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
