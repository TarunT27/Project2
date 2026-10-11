"""End-to-end API tests for the platform: auth, runs, review, tasks, field sync, geo, exports, sharing, admin."""

import isolation  # noqa: F401  (must run before anything reads Claude credentials)

import csv
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile

from PIL import Image

TMP = tempfile.mkdtemp(prefix="dra-test-")


def setUpModule():
    os.environ["DRA_DATA_DIR"] = TMP
    os.environ["DRA_MODELS_DIR"] = os.path.join(TMP, "models")
    from app.config import get_config
    get_config.cache_clear()


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


ADMIN_PW = "admin-test-password"
PW = "member-test-password"


def jpeg_with_dji_xmp(lat=29.95, lon=-90.07, alt=60.0, pitch=-90.0, yaw=0.0):
    img = Image.new("RGB", (640, 480), (120, 130, 125))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           f'<rdf:Description xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" drone-dji:GpsLatitude="{lat}" '
           f'drone-dji:GpsLongitude="{lon}" drone-dji:RelativeAltitude="+{alt}" drone-dji:GimbalPitchDegree="{pitch}" '
           f'drone-dji:GimbalYawDegree="{yaw}" drone-dji:FlightYawDegree="{yaw}"/></rdf:RDF></x:xmpmeta>')
    data = buf.getvalue()
    return data[:2] + b"\xff\xe1" + (len(xmp) + 31).to_bytes(2, "big") + b"http://ns.adobe.com/xap/1.0/\x00" + xmp.encode() + data[2:]


def geotiff_4326(west=-90.08, north=29.96, px=0.00001, size=(800, 600)):
    img = Image.new("RGB", size, (150, 150, 140))
    buf = io.BytesIO()
    img.save(buf, "TIFF", tiffinfo={
        33550: (px, px, 0.0),
        33922: (0.0, 0.0, 0.0, west, north, 0.0),
        34735: (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326),
    })
    return buf.getvalue()


class PlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app

        cls.ctx = TestClient(app)
        cls.client = cls.ctx.__enter__()
        r = cls.client.post("/api/auth/setup", json={"username": "admin", "name": "Ada Admin", "password": ADMIN_PW})
        assert r.status_code == 200, r.text
        cls.admin_cookie = r.cookies.get("dra_session")

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    # helpers
    def login(self, username, password):
        from fastapi.testclient import TestClient
        from app.main import app

        c = TestClient(app)
        r = c.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def wait_run(self, run_id, client=None, timeout=60):
        client = client or self.client
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in ("done", "failed", "cancelled"):
                return run
            time.sleep(0.2)
        self.fail("run did not finish")

    def new_incident(self, name="Test incident", lat=37.7749, lon=-122.4194):
        r = self.client.post("/api/incidents", json={"name": name, "center_lat": lat, "center_lon": lon})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def demo_run(self, incident_id):
        r = self.client.post(f"/api/incidents/{incident_id}/runs", data={"mode": "demo", "builtin_demo": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        run = self.wait_run(r.json()["id"])
        self.assertEqual(run["status"], "done", run)
        return run

    # tests
    def test_auth_guards(self):
        from fastapi.testclient import TestClient
        from app.main import app

        anon = TestClient(app)
        self.assertTrue(anon.get("/api/auth/status").json()["user"] is None)
        self.assertEqual(anon.get("/api/incidents").status_code, 401)
        self.assertEqual(anon.post("/api/auth/setup", json={"username": "xx", "password": "y" * 12}).status_code, 409)
        self.assertEqual(anon.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code, 401)
        # cross-site writes are refused even with a valid session
        r = self.client.post("/api/incidents", json={"name": "csrf"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        page = self.client.get("/")
        self.assertIn("Content-Security-Policy", page.headers)

    def test_demo_run_review_and_history(self):
        inc = self.new_incident()
        run = self.demo_run(inc["id"])
        self.assertGreater(run["observations"], 0)
        obs = self.client.get(f"/api/incidents/{inc['id']}/observations").json()
        self.assertEqual(len(obs), run["observations"])
        self.assertTrue(all(o["is_demo"] and o["location_source"] == "synthetic_demo" for o in obs))
        first = obs[0]
        self.assertEqual(self.client.get(f"/api/images/{first['image_id']}/file?variant=preview").status_code, 200)

        r = self.client.patch(f"/api/observations/{first['id']}",
                              json={"damage_class": "Minor Damage", "review_note": "Roof intact", "hazards": ["fire"]})
        self.assertEqual(r.json()["status"], "corrected")
        self.assertEqual(r.json()["model_class"], first["model_class"])
        detail = self.client.get(f"/api/observations/{first['id']}").json()
        self.assertEqual(detail["history"][0]["after"]["damage_class"], "Minor Damage")
        self.assertEqual(self.client.patch(f"/api/observations/{first['id']}", json={"hazards": ["aliens"]}).status_code, 400)

        r = self.client.post("/api/observations/bulk", json={"ids": [o["id"] for o in obs], "action": "confirm"})
        self.assertEqual(r.json()["changed"], len(obs) - 1)  # corrected one is kept
        summary = self.client.get(f"/api/incidents/{inc['id']}/summary").json()
        self.assertEqual(summary["by_status"]["not_reviewed"], 0)
        self.assertEqual(summary["reviewed_pct"], 100.0)

        added = self.client.post(f"/api/images/{first['image_id']}/observations",
                                 json={"bbox": [10, 10, 60, 60], "damage_class": "Destroyed"})
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(added.json()["status"], "added")
        bad = self.client.post(f"/api/images/{first['image_id']}/observations",
                               json={"bbox": [10, 10, 99999, 60], "damage_class": "Destroyed"})
        self.assertEqual(bad.status_code, 400)

    def test_roles_crews_tasks_field_sync_and_route(self):
        inc = self.new_incident("Ops incident")
        self.demo_run(inc["id"])
        for username, role in (("rita", "reviewer"), ("finn", "field")):
            r = self.client.post("/api/admin/users", json={"username": username, "name": username.title(), "role": role,
                                                           "password": PW})
            self.assertEqual(r.status_code, 200, r.text)
        users = {u["username"]: u for u in self.client.get("/api/users").json()}
        crew = self.client.post(f"/api/incidents/{inc['id']}/crews",
                                json={"name": "Engine 7", "member_ids": [users["finn"]["id"]]}).json()
        tasks = self.client.post(f"/api/incidents/{inc['id']}/tasks", json={"high_priority": True, "crew_id": crew["id"]}).json()
        self.assertTrue(tasks and all(t["status"] == "assigned" and t["damage_class"] in ("Destroyed", "Major Damage")
                                      for t in tasks))
        again = self.client.post(f"/api/incidents/{inc['id']}/tasks", json={"high_priority": True}).json()
        self.assertEqual(again, [])  # no duplicate tasks for the same observation

        field = self.login("finn", PW)
        self.assertEqual(field.post("/api/incidents", json={"name": "nope"}).status_code, 403)
        mine = field.get("/api/field/tasks").json()["tasks"]
        self.assertEqual({t["id"] for t in mine}, {t["id"] for t in tasks})
        task = mine[0]
        photo = io.BytesIO()
        Image.new("RGB", (64, 48), "red").save(photo, "JPEG")
        payload = {"status": "needs_usar", "client_id": "offline-123", "notes": "Voices heard",
                   "hazards": json.dumps(["possible_entrapment"]), "latitude": "37.77", "longitude": "-122.41"}
        r = field.post(f"/api/tasks/{task['id']}/reports", data=payload,
                       files=[("photos", ("p.jpg", photo.getvalue(), "image/jpeg"))])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["duplicate"])
        self.assertEqual(r.json()["task"]["status"], "needs_usar")
        self.assertIn("possible_entrapment", r.json()["task"]["hazards"])
        self.assertTrue(field.post(f"/api/tasks/{task['id']}/reports", data=payload).json()["duplicate"])
        reports = self.client.get(f"/api/tasks/{task['id']}/reports").json()
        self.assertEqual(len(reports["reports"]), 1)
        self.assertEqual(len(reports["photos"]), 1)
        self.assertEqual(field.patch(f"/api/tasks/{task['id']}", json={"crew_id": None}).status_code, 403)
        self.assertEqual(field.patch(f"/api/tasks/{task['id']}", json={"status": "cancelled"}).status_code, 403)

        reviewer = self.login("rita", PW)
        self.assertEqual(reviewer.post(f"/api/incidents/{inc['id']}/tasks", json={"high_priority": True}).status_code, 403)

        plan = self.client.get(f"/api/incidents/{inc['id']}/route?crew_id={crew['id']}&start_lat=37.77&start_lon=-122.42").json()
        self.assertEqual(plan["method"], "straight_line")
        self.assertEqual(len(plan["stops"]), len(tasks))

    def test_exports_and_share_links(self):
        inc = self.new_incident("Export incident")
        run = self.demo_run(inc["id"])
        for fmt, magic in (("csv", None), ("geojson", b"{"), ("kml", b"<?xml"), ("shp", b"PK"), ("pdf", b"%PDF"),
                           ("buildings", b"{")):
            r = self.client.get(f"/api/incidents/{inc['id']}/export/{fmt}")
            self.assertEqual(r.status_code, 200, (fmt, r.text[:200]))
            self.assertIn("DEMO_", r.headers["content-disposition"])
            if magic:
                self.assertTrue(r.content.startswith(magic), fmt)
        rows = list(csv.DictReader(io.StringIO(
            self.client.get(f"/api/incidents/{inc['id']}/export/csv").content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), run["observations"])
        self.assertIn("review_status", rows[0])
        self.assertEqual(rows[0]["priority_rank"], "1")
        reviewed = self.client.get(f"/api/incidents/{inc['id']}/export/csv?reviewed_only=true").content.decode("utf-8-sig")
        self.assertEqual(len(list(csv.DictReader(io.StringIO(reviewed)))), 0)
        with zipfile.ZipFile(io.BytesIO(self.client.get(f"/api/incidents/{inc['id']}/export/shp").content)) as zf:
            self.assertEqual(len(zf.namelist()), 4)

        link = self.client.post(f"/api/incidents/{inc['id']}/share", json={"label": "County EOC", "hours": 2}).json()
        from fastapi.testclient import TestClient
        from app.main import app
        anon = TestClient(app)
        self.assertEqual(anon.get(link["path"]).status_code, 200)
        public = anon.get(f"/api/public/{link['token']}").json()
        self.assertEqual(public["incident"]["name"], "Export incident")
        self.assertNotIn("tasks", public["summary"])
        obs = anon.get(f"/api/public/{link['token']}/observations").json()
        self.assertNotIn("reviewer", obs[0])
        self.assertEqual(anon.get(f"/api/public/{link['token']}/images/{obs[0]['image_id']}").status_code, 200)
        self.assertEqual(anon.get(f"/api/public/{link['token']}/export/csv").status_code, 200)
        self.assertEqual(anon.get(f"/api/public/{link['token']}/export/pdf").status_code, 404)
        shares = self.client.get(f"/api/incidents/{inc['id']}/share").json()
        self.client.delete(f"/api/share/{shares[0]['id']}")
        self.assertEqual(anon.get(f"/api/public/{link['token']}").status_code, 404)

    def test_footprints_and_buildings(self):
        inc = self.new_incident("Footprint incident")
        self.demo_run(inc["id"])
        obs = self.client.get(f"/api/incidents/{inc['id']}/observations").json()
        lat, lon = obs[0]["latitude"], obs[0]["longitude"]
        d = 0.0002
        ring = [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]
        geojson = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": "b1", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}]}
        r = self.client.post(f"/api/incidents/{inc['id']}/footprints/upload",
                             files={"file": ("fp.geojson", json.dumps(geojson).encode(), "application/geo+json")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json()["matched_observations"], 1)
        bld = self.client.get(f"/api/incidents/{inc['id']}/buildings").json()
        self.assertEqual(len(bld), 1)
        self.assertGreaterEqual(bld[0]["count"], 1)
        self.assertEqual(len(self.client.get(f"/api/incidents/{inc['id']}/footprints").json()["features"]), 1)

    def test_drone_telemetry_and_geotiff_runs(self):
        inc = self.new_incident("Geo incident", 29.95, -90.07)
        files = [("files", ("DJI_0001.JPG", jpeg_with_dji_xmp(), "image/jpeg")),
                 ("files", ("ortho.tif", geotiff_4326(), "image/tiff"))]
        r = self.client.post(f"/api/incidents/{inc['id']}/runs", data={"mode": "demo"}, files=files)
        self.assertEqual(r.status_code, 200, r.text)
        run = self.wait_run(r.json()["id"])
        self.assertEqual(run["status"], "done", run)
        self.assertEqual(run["errors"], [])
        images = {i["source_file"]: i for i in self.client.get(f"/api/incidents/{inc['id']}/images").json()}
        self.assertEqual(images["DJI_0001.JPG"]["location_source"], "drone_telemetry")
        self.assertEqual(images["ortho.tif"]["kind"], "ortho")
        obs = self.client.get(f"/api/incidents/{inc['id']}/observations").json()
        sources = {o["location_source"] for o in obs}
        self.assertIn("telemetry_projection", sources)
        self.assertIn("ortho_georef", sources)
        for o in obs:
            if o["location_source"] == "ortho_georef":
                self.assertTrue(29.954 < o["latitude"] < 29.96 and -90.08 < o["longitude"] < -90.072, o)
            if o["location_source"] == "telemetry_projection":  # nadir shot at 60 m: within ~60 m of the camera
                self.assertLess(abs(o["latitude"] - 29.95), 0.001)
        overlays = self.client.get(f"/api/incidents/{inc['id']}/overlays").json()
        self.assertEqual(len(overlays), 1)
        self.assertEqual(self.client.get(f"/api/overlays/{overlays[0]['id']}/file").status_code, 200)

    def test_claude_mode_run_records_reasoning_and_cost(self):
        from unittest.mock import patch
        from app.services import claude_detector, models
        from tests.test_claude_detector import FakeClient

        inc = self.new_incident("Claude incident", 29.95, -90.07)
        url = f"/api/incidents/{inc['id']}/runs"
        tif = ("files", ("ian.tif", geotiff_4326(size=(2600, 1800)), "image/tiff"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            r = self.client.post(url, data={"mode": "claude"}, files=[tif])
            self.assertEqual(r.status_code, 400)
            self.assertIn("ANTHROPIC_API_KEY", r.json()["error"])
            os.environ["ANTHROPIC_API_KEY"] = "test-not-a-real-key"
            fake = FakeClient()
            real_build = models.build_detector
            with patch.object(models, "build_detector", lambda mode, *a, **k: claude_detector.ClaudeDetector(
                    0.5, client=fake) if mode == "claude" else real_build(mode, *a, **k)):
                r = self.client.post(url, data={"mode": "claude"}, files=[tif])
                self.assertEqual(r.status_code, 200, r.text)
                run = self.wait_run(r.json()["id"])
        self.assertEqual(run["status"], "done", run)
        self.assertIn("Claude:", run["message"])
        self.assertIn("usage", run["settings"])
        self.assertGreater(len(fake.requests), 1)  # tiled
        obs = self.client.get(f"/api/incidents/{inc['id']}/observations").json()
        self.assertTrue(obs)
        for o in obs:
            self.assertFalse(o["is_demo"])
            self.assertEqual(o["location_source"], "ortho_georef")
            self.assertEqual(o["vision"]["source"], "detector")
            self.assertEqual(o["vision"]["rationale"], "Only a slab remains")
            self.assertTrue(o["model"].startswith("Claude vision"))

    def test_run_validation_and_model_safety(self):
        inc = self.new_incident("Validation incident")
        url = f"/api/incidents/{inc['id']}/runs"
        self.assertEqual(self.client.post(url, data={"mode": "demo"}).status_code, 400)
        self.assertEqual(self.client.post(url, data={"mode": "single", "model": "../../etc/passwd"},
                                          files=[("files", ("a.png", b"x", "image/png"))]).status_code, 400)
        self.assertEqual(self.client.post(url, data={"mode": "demo"},
                                          files=[("files", ("a.exe", b"x", "application/octet-stream"))]).status_code, 400)
        r = self.client.post(url, data={"mode": "demo"}, files=[("files", ("bad.jpg", b"not an image", "image/jpeg"))])
        run = self.wait_run(r.json()["id"])
        self.assertEqual(run["status"], "done")
        self.assertEqual(len(run["errors"]), 1)

    def test_admin_backup_retention_and_audit(self):
        r = self.client.post("/api/admin/backups?include_images=false")
        self.assertEqual(r.status_code, 200, r.text)
        name = r.json()["name"]
        with zipfile.ZipFile(io.BytesIO(self.client.get(f"/api/admin/backups/{name}").content)) as zf:
            self.assertIn("dra.sqlite3", zf.namelist())
        self.assertEqual(self.client.get("/api/admin/backups/..%2Fdra.sqlite3").status_code, 404)
        inc = self.new_incident("Old closed incident")
        self.client.patch(f"/api/incidents/{inc['id']}", json={"status": "closed"})
        dry = self.client.post("/api/admin/retention", json={"days": 1, "dry_run": True}).json()
        self.assertTrue(dry["dry_run"])
        audit = self.client.get("/api/admin/audit").json()
        self.assertTrue(any(a["action"] == "backup.created" for a in audit))
        system = self.client.get("/api/admin/system").json()
        self.assertIn("integrations", system)
        users = self.client.get("/api/users").json()
        admin_id = next(u["id"] for u in users if u["username"] == "admin")
        self.assertEqual(self.client.patch(f"/api/admin/users/{admin_id}", json={"role": "field"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
