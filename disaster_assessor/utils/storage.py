"""SQLite persistence with incident permissions and optimistic review updates."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from .geo_utils import valid_coordinate
from .mock_data import CLASSES

STATUSES = ("not_reviewed", "reviewed", "needs_follow_up")


def now():
    return datetime.now(timezone.utc).isoformat()


def empty_batch():
    return {"rows": [], "images": [], "errors": [], "failed_files": [], "processed": 0, "demo": True}


class ConflictError(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, display_name TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
                    event_date TEXT NOT NULL, location TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    incident_id TEXT REFERENCES incidents(id), user_id TEXT REFERENCES users(id),
                    role TEXT NOT NULL CHECK(role IN ('viewer', 'editor')),
                    PRIMARY KEY (incident_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, incident_id TEXT REFERENCES incidents(id), actor TEXT REFERENCES users(id),
                    created_at TEXT NOT NULL, status TEXT NOT NULL, settings TEXT NOT NULL,
                    batch TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, parent_id TEXT
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT REFERENCES incidents(id),
                    run_id TEXT, actor TEXT NOT NULL, at TEXT NOT NULL, action TEXT NOT NULL,
                    target TEXT NOT NULL, before_json TEXT, after_json TEXT
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY, incident_id TEXT REFERENCES incidents(id), actor TEXT NOT NULL,
                    created_at TEXT NOT NULL, report TEXT NOT NULL, digest TEXT NOT NULL,
                    UNIQUE(incident_id, digest)
                );
                CREATE INDEX IF NOT EXISTS runs_incident ON runs(incident_id, created_at);
                CREATE INDEX IF NOT EXISTS audit_incident ON audit(incident_id, id);
            """)
        Path(self.path).chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def register_user(self, user, display_name=None, admin=False):
        with self.connect() as db:
            db.execute("INSERT INTO users VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, is_admin=excluded.is_admin",
                       (user, display_name or user, int(admin)))

    def invited(self, user):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM members WHERE user_id=? LIMIT 1", (user,)).fetchone())

    def _role(self, db, incident, user):
        account = db.execute("SELECT is_admin FROM users WHERE id=?", (user,)).fetchone()
        info = db.execute("SELECT owner FROM incidents WHERE id=?", (incident,)).fetchone()
        if not account or not info:
            raise PermissionError("Incident unavailable or access denied.")
        if account["is_admin"] or info["owner"] == user:
            return "owner"
        member = db.execute("SELECT role FROM members WHERE incident_id=? AND user_id=?", (incident, user)).fetchone()
        if not member:
            raise PermissionError("Incident unavailable or access denied.")
        return member["role"]

    def _require(self, db, incident, user, edit=False, owner=False):
        role = self._role(db, incident, user)
        if (edit and role == "viewer") or (owner and role != "owner"):
            raise PermissionError("You do not have permission to change this incident.")
        return role

    def role(self, incident, user):
        with self.connect() as db:
            return self._role(db, incident, user)

    def _audit(self, db, incident, run, actor, action, target, before=None, after=None):
        db.execute("INSERT INTO audit(incident_id,run_id,actor,at,action,target,before_json,after_json) VALUES(?,?,?,?,?,?,?,?)",
                   (incident, run, actor, now(), action, target, json.dumps(before), json.dumps(after)))

    def create_incident(self, user, name, event_date, location=""):
        name, location = name.strip(), location.strip()
        if not 1 <= len(name) <= 120 or len(location) > 500:
            raise ValueError("Use an incident name of 1–120 characters and a location of at most 500 characters.")
        # Explicit ISO date validation keeps history filtering predictable.
        from datetime import date
        date.fromisoformat(str(event_date))
        identity = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?)", (identity, user, name, str(event_date), location, now()))
            self._audit(db, identity, None, user, "create_incident", identity, after={"name": name, "date": str(event_date), "location": location})
        return identity

    def list_incidents(self, user):
        with self.connect() as db:
            account = db.execute("SELECT is_admin FROM users WHERE id=?", (user,)).fetchone()
            if not account:
                return []
            rows = db.execute("SELECT DISTINCT i.* FROM incidents i LEFT JOIN members m ON m.incident_id=i.id WHERE i.owner=? OR m.user_id=? OR ?=1 ORDER BY i.created_at DESC",
                              (user, user, account["is_admin"]))
            return [dict(r) for r in rows]

    def members(self, incident, user):
        with self.connect() as db:
            self._require(db, incident, user, owner=True)
            return [dict(r) for r in db.execute("SELECT user_id,role FROM members WHERE incident_id=?", (incident,))]

    def grant(self, incident, actor, user, role):
        user = user.strip().lower()
        if role not in {"viewer", "editor"} or "@" not in user or len(user) > 254:
            raise ValueError("Enter a valid sign-in email and role.")
        with self.connect() as db:
            self._require(db, incident, actor, owner=True)
            if db.execute("SELECT owner FROM incidents WHERE id=?", (incident,)).fetchone()[0] == user:
                raise ValueError("The owner already has full access.")
            db.execute("INSERT OR IGNORE INTO users VALUES(?,?,0)", (user, user))
            previous = db.execute("SELECT role FROM members WHERE incident_id=? AND user_id=?", (incident, user)).fetchone()
            db.execute("INSERT INTO members VALUES(?,?,?) ON CONFLICT(incident_id,user_id) DO UPDATE SET role=excluded.role", (incident, user, role))
            self._audit(db, incident, None, actor, "grant_access", user, before=dict(previous) if previous else None, after={"role": role})

    def revoke(self, incident, actor, user):
        with self.connect() as db:
            self._require(db, incident, actor, owner=True)
            previous = db.execute("SELECT role FROM members WHERE incident_id=? AND user_id=?", (incident, user)).fetchone()
            db.execute("DELETE FROM members WHERE incident_id=? AND user_id=?", (incident, user))
            self._audit(db, incident, None, actor, "revoke_access", user, before=dict(previous) if previous else None)

    def begin_run(self, incident, user, settings, parent_id=None):
        identity = uuid.uuid4().hex
        with self.connect() as db:
            self._require(db, incident, user, edit=True)
            if parent_id:
                parent = db.execute("SELECT incident_id FROM runs WHERE id=?", (parent_id,)).fetchone()
                if not parent or parent[0] != incident:
                    raise ValueError("Retry must belong to the same incident.")
            db.execute("INSERT INTO runs(id,incident_id,actor,created_at,status,settings,batch,parent_id) VALUES(?,?,?,?,?,?,?,?)",
                       (identity, incident, user, now(), "running", json.dumps(settings), json.dumps(empty_batch()), parent_id))
            self._audit(db, incident, identity, user, "start_run", identity, after=settings)
        return identity

    def finish_run(self, identity, batch, status):
        if status not in {"completed", "partial", "failed", "cancelled"}:
            raise ValueError("Invalid run status")
        with self.connect() as db:
            info = db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
            if not info or info["status"] != "running":
                raise ConflictError("Run is no longer active.")
            # Internal worker writes are limited to an already authorized run. Read/edit access
            # is rechecked on every user-facing operation, including after membership revocation.
            db.execute("UPDATE runs SET batch=?,status=?,version=version+1 WHERE id=?", (json.dumps(batch, allow_nan=False), status, identity))
            self._audit(db, info["incident_id"], identity, info["actor"], "finish_run", identity, after={"status": status, "processed": batch["processed"]})

    def recover_interrupted(self):
        with self.connect() as db:
            db.execute("UPDATE runs SET status='interrupted',version=version+1 WHERE status='running'")

    def list_runs(self, incident, user):
        with self.connect() as db:
            self._require(db, incident, user)
            return [dict(r) for r in db.execute("SELECT id,created_at,status,actor,parent_id FROM runs WHERE incident_id=? ORDER BY created_at DESC", (incident,))]

    def get_run(self, identity, user):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
            if not row:
                raise PermissionError("Assessment unavailable.")
            self._require(db, row["incident_id"], user)
            result = dict(row)
            result["batch"], result["settings"] = json.loads(row["batch"]), json.loads(row["settings"])
            return result

    def _edit_batch(self, identity, user, expected_version, action, target, update):
        with self.connect() as db:
            # The write lock covers read, version check, audit and commit.
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
            if not run:
                raise PermissionError("Assessment unavailable.")
            self._require(db, run["incident_id"], user, edit=True)
            if run["status"] == "running" or run["version"] != expected_version:
                raise ConflictError("This assessment changed. Reload the latest version before saving.")
            batch = json.loads(run["batch"])
            before, after = update(batch)
            db.execute("UPDATE runs SET batch=?,version=version+1 WHERE id=?", (json.dumps(batch, allow_nan=False), identity))
            self._audit(db, run["incident_id"], identity, user, action, target, before, after)

    def review(self, identity, user, expected_version, observation, label, status, notes):
        if label not in CLASSES or status not in STATUSES or len(notes) > 4000:
            raise ValueError("Invalid review label, status, or notes.")
        def update(batch):
            row = next((r for r in batch["rows"] if r["observation_id"] == observation), None)
            if row is None:
                raise ValueError("Observation not found.")
            fields = ("damage_class", "review_status", "review_notes", "reviewed_by", "reviewed_at_utc")
            before = {k: row.get(k) for k in fields}
            row.setdefault("predicted_class", row["damage_class"])
            row.update(damage_class=label, review_status=status, review_notes=notes,
                       reviewed_by=user, reviewed_at_utc=now())
            return before, {k: row.get(k) for k in fields}
        self._edit_batch(identity, user, expected_version, "review_observation", observation, update)

    def locate_image(self, identity, user, expected_version, image_id, latitude, longitude):
        if not valid_coordinate(latitude, longitude):
            raise ValueError("Coordinates are out of range.")
        def update(batch):
            image = next((i for i in batch["images"] if i["image_id"] == image_id), None)
            if image is None:
                raise ValueError("Source image not found.")
            rows = [r for r in batch["rows"] if r["image_id"] == image_id]
            before = [{k: r.get(k) for k in ("observation_id", "latitude", "longitude", "location_source")} for r in rows]
            for item in [image] + rows:
                item.setdefault("original_latitude", item.get("latitude"))
                item.setdefault("original_longitude", item.get("longitude"))
                item.setdefault("original_location_source", item.get("location_source"))
                item.update(latitude=float(latitude), longitude=float(longitude), location_source="manual_image_reference", location_is_synthetic=False)
            return before, {"latitude": latitude, "longitude": longitude, "location_source": "manual_image_reference"}
        self._edit_batch(identity, user, expected_version, "locate_image", image_id, update)

    def audit_log(self, incident, user, limit=1000):
        with self.connect() as db:
            self._require(db, incident, user)
            if limit is not None and (not isinstance(limit, int) or limit < 1):
                raise ValueError("Invalid audit limit.")
            sql = "SELECT * FROM audit WHERE incident_id=? ORDER BY id DESC"
            params = (incident,)
            if limit is not None:
                sql += " LIMIT ?"
                params += (limit,)
            return [dict(r) for r in db.execute(sql, params)]


    def save_evaluation(self, incident, user, report):
        import hashlib
        from .evaluation import validate_report
        validate_report(report)
        encoded = json.dumps(report, sort_keys=True, allow_nan=False)
        if len(encoded.encode()) > 5 * 1024 * 1024:
            raise ValueError("Use a report below 5 MB.")
        digest, identity = hashlib.sha256(encoded.encode()).hexdigest(), uuid.uuid4().hex
        with self.connect() as db:
            self._require(db, incident, user, edit=True)
            existing = db.execute("SELECT id FROM evaluations WHERE incident_id=? AND digest=?", (incident, digest)).fetchone()
            if existing:
                return existing[0]
            db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?)", (identity, incident, user, now(), encoded, digest))
            self._audit(db, incident, None, user, "save_evaluation", identity, after={"digest": digest, "model": report.get("model"), "images": report["image_count"]})
        return identity

    def evaluations(self, incident, user):
        with self.connect() as db:
            self._require(db, incident, user)
            return [dict(r, report=json.loads(r["report"])) for r in db.execute("SELECT * FROM evaluations WHERE incident_id=? ORDER BY created_at DESC", (incident,))]
