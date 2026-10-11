import isolation  # noqa: F401  (hide real Claude credentials from tests)
import hashlib
import hmac
import io
import json
import random
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import shapefile
from PIL import Image

from app.services import notify, reports, routing, vision_review

KML = "{http://www.opengis.net/kml/2.2}"


def obs(i, cls="Destroyed", lat=37.0, lon=-122.0, **kw):
    base = {
        "id": f"o{i}", "incident_name": "Test", "source_file": f"img_{i}.png",
        "frame_seconds": None, "damage_class": cls, "confidence": 0.89,
        "latitude": lat, "longitude": lon, "location_source": "exif",
        "status": "confirmed", "reviewer": None, "review_note": None, "hazards": [],
        "assigned_crew": None, "task_status": None, "is_demo": False, "model": "m",
        "captured_at": None, "assessed_at": None,
    }
    base.update(kw)
    return base


class RoutingTests(unittest.TestCase):
    def test_haversine(self):
        self.assertAlmostEqual(routing.haversine_m(0, 0, 0, 1), 111195, delta=50)
        self.assertEqual(routing.haversine_m(10, 10, 10, 10), 0)

    def test_destroyed_before_nearer_minor(self):
        stops = [
            {"id": "minor", "latitude": 37.0001, "longitude": -122.0, "damage_class": "Minor Damage"},
            {"id": "dest", "latitude": 37.05, "longitude": -122.0, "damage_class": "Destroyed"},
            {"id": "major", "latitude": 37.02, "longitude": -122.0, "damage_class": "Major Damage"},
        ]
        plan = routing.plan_route((37.0, -122.0), stops)
        self.assertEqual(plan["order"], ["dest", "major", "minor"])
        self.assertEqual(plan["method"], "straight_line")
        self.assertEqual(plan["legs"][0]["from"], "start")
        self.assertEqual(len(plan["legs"]), 3)
        self.assertAlmostEqual(plan["total_distance_m"],
                               sum(l["distance_m"] for l in plan["legs"]), delta=1)
        # Without severity weighting, the nearest stop goes first.
        flat = routing.plan_route((37.0, -122.0), stops, severity_weight=False)
        self.assertEqual(flat["order"][0], "minor")

    def test_nearest_neighbour_within_tier(self):
        stops = [
            {"id": "far", "latitude": 37.03, "longitude": -122.0, "damage_class": "Destroyed"},
            {"id": "near", "latitude": 37.01, "longitude": -122.0, "damage_class": "Destroyed"},
            {"id": "mid", "latitude": 37.02, "longitude": -122.0, "damage_class": "Destroyed"},
        ]
        plan = routing.plan_route((37.0, -122.0), stops)
        self.assertEqual(plan["order"], ["near", "mid", "far"])

    def test_unrouted_stops(self):
        stops = [
            {"id": "ok", "latitude": 37.0, "longitude": -122.0, "damage_class": "Destroyed"},
            {"id": "none", "latitude": None, "longitude": None, "damage_class": "Destroyed"},
            {"id": "bad", "latitude": 95, "longitude": 0, "damage_class": "Destroyed"},
            {"id": "nan", "latitude": float("nan"), "longitude": 0, "damage_class": "Minor Damage"},
        ]
        plan = routing.plan_route(None, stops)
        self.assertEqual(plan["order"], ["ok"])
        self.assertEqual(sorted(plan["unrouted"]), ["bad", "nan", "none"])
        self.assertEqual(plan["legs"], [])
        capped = routing.plan_route(None, stops[:1] + [
            {"id": "minor", "latitude": 37.1, "longitude": -122.0, "damage_class": "Minor Damage"}
        ], max_stops=1)
        self.assertEqual(capped["order"], ["ok"])
        self.assertIn("minor", capped["unrouted"])

    def test_two_opt_does_not_increase_distance(self):
        rng = random.Random(7)
        for _ in range(10):
            stops = [
                {"id": i, "latitude": 37 + rng.uniform(0, 0.1),
                 "longitude": -122 + rng.uniform(0, 0.1), "damage_class": "Destroyed"}
                for i in range(25)
            ]
            nn = routing.plan_route((37.05, -122.05), stops, improve=False)
            opt = routing.plan_route((37.05, -122.05), stops, improve=True)
            self.assertLessEqual(opt["total_distance_m"], nn["total_distance_m"] + 0.5)
            self.assertEqual(sorted(opt["order"]), list(range(25)))

    def test_osrm_route_mocked(self):
        resp = MagicMock(status_code=200)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "code": "Ok",
            "routes": [{"distance": 1234.5, "duration": 300.0,
                        "geometry": {"type": "LineString",
                                     "coordinates": [[-122.0, 37.0], [-122.1, 37.1]]}}],
        }
        with patch.object(routing.httpx, "get", return_value=resp) as get:
            out = routing.osrm_route([(37.0, -122.0), (37.1, -122.1)], "http://osrm.local/")
        url = get.call_args.args[0]
        self.assertEqual(url, "http://osrm.local/route/v1/driving/-122.000000,37.000000;-122.100000,37.100000")
        self.assertEqual(get.call_args.kwargs["params"], {"overview": "full", "geometries": "geojson"})
        self.assertEqual(out["distance_m"], 1234.5)
        self.assertEqual(out["geometry"]["type"], "LineString")

    def test_osrm_failures_raise_runtime_error(self):
        with patch.object(routing.httpx, "get", side_effect=httpx.ConnectError("x")):
            with self.assertRaises(RuntimeError):
                routing.osrm_route([(37, -122), (37.1, -122)], "http://osrm.local")
        resp = MagicMock()
        resp.json.return_value = {"code": "NoRoute", "routes": []}
        with patch.object(routing.httpx, "get", return_value=resp):
            with self.assertRaises(RuntimeError):
                routing.osrm_route([(37, -122), (37.1, -122)], "http://osrm.local")
        with self.assertRaises(RuntimeError):
            routing.osrm_route([(37, -122)], "http://osrm.local")
        with self.assertRaises(RuntimeError):
            routing.osrm_route([(37, -122), (37.1, -122)], "http://osrm.local", profile="../x")


class ReportTests(unittest.TestCase):
    def sample(self):
        return [
            obs(1, "Destroyed", hazards=["fire"], assigned_crew="Crew A"),
            obs(2, "Minor Damage", lat=37.01, confidence=0.7, status="not_reviewed"),
            obs(3, "Major Damage", lat=None, lon=None),
            obs(4, "No Damage", lat=37.02, source_file="<script>alert(1)</script>.png",
                is_demo=True),
        ]

    def test_kml_valid_escaped_colored(self):
        data = reports.export_kml(self.sample())
        root = ET.fromstring(data)  # valid XML
        self.assertEqual(root.tag, KML + "kml")
        self.assertNotIn(b"<script", data)
        placemarks = root.iter(KML + "Placemark")
        placemarks = list(placemarks)
        self.assertEqual(len(placemarks), 3)  # one has no coordinates
        names = [p.find(KML + "name").text for p in placemarks]
        self.assertEqual(names[0], "Destroyed (89%)")
        colors = {s.get("id"): s.find(f"{KML}IconStyle/{KML}color").text
                  for s in root.iter(KML + "Style")}
        self.assertEqual(colors["dmg-destroyed"], "ff2626dc")   # #dc2626
        self.assertEqual(colors["dmg-no-damage"], "ff4aa316")   # #16a34a
        self.assertEqual(colors["dmg-minor-damage"], "ff08b3ea")  # #eab308
        script_pm = [p for p in placemarks
                     if "script" in (p.find(KML + "description").text or "")][0]
        desc = script_pm.find(KML + "description").text
        self.assertNotIn("<script", desc)  # HTML-escaped inside the description
        ext = {d.get("name"): d.find(KML + "value").text
               for d in script_pm.iter(KML + "Data")}
        self.assertEqual(ext["source_file"], "<script>alert(1)</script>.png")
        self.assertEqual(ext["id"], "o4")
        self.assertEqual(ext["status"], "confirmed")
        coords = placemarks[0].find(f"{KML}Point/{KML}coordinates").text
        self.assertTrue(coords.startswith("-122.0000000,37.0000000"))

    def test_shapefile_zip_roundtrip(self):
        items = self.sample() + [obs(5, "Destroyed", lat=37.03, hazards=["fire"] * 100,
                                     source_file="ünicode_地震.png")]
        data = reports.export_shapefile_zip(items)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            self.assertEqual(len(names), 4)
            self.assertEqual(sorted(Path(n).suffix for n in names),
                             [".dbf", ".prj", ".shp", ".shx"])
            self.assertIn("WGS_1984", zf.read([n for n in names if n.endswith(".prj")][0]).decode())
            with tempfile.TemporaryDirectory() as tmp:
                zf.extractall(tmp)
                stem = Path(tmp) / Path(names[0]).stem
                with shapefile.Reader(str(stem)) as reader:
                    self.assertEqual(reader.shapeType, shapefile.POINT)
                    self.assertEqual(len(reader), 4)
                    fields = [f[0] for f in reader.fields[1:]]
                    self.assertEqual(fields, ["OBS_ID", "CLASS", "SCORE", "STATUS",
                                              "SOURCE", "LOCSRC", "HAZARDS", "CREW"])
                    self.assertTrue(all(len(f) <= 10 for f in fields))
                    first = reader.shapeRecord(0)
                    self.assertEqual(first.record["CLASS"], "Destroyed")
                    self.assertAlmostEqual(first.shape.points[0][0], -122.0)
                    self.assertAlmostEqual(first.shape.points[0][1], 37.0)
                    self.assertLessEqual(len(reader.record(1)["HAZARDS"].encode()), 200)

    def test_pdf_with_unicode(self):
        items = self.sample() + [obs(6, "Major Damage", source_file="地震_\U0001F525.png",
                                     assigned_crew="Équipe — α",
                                     hazards=["unstable_structure"])]
        pdf = reports.situation_report_pdf(
            {"name": "Hurricane “Z” — 東京", "description": "Test ☃"},
            items, [{"status": "open"}, {"status": "done"}, {"task_status": "open"}],
            generated_by="tester ✓", generated_at="2026-10-11T00:00Z")
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)
        # Many rows (multi-page) and empty inputs also render.
        many = [obs(i, "Destroyed", hazards=["fire", "flooding"]) for i in range(80)]
        self.assertTrue(reports.situation_report_pdf({}, many, [], "a", "b").startswith(b"%PDF"))
        self.assertTrue(reports.situation_report_pdf({}, [], [], "a", "b").startswith(b"%PDF"))

    def test_latin1_sanitizer(self):
        self.assertEqual(reports.latin1("a—b 地"), "a-b ?")


class NotifyTests(unittest.TestCase):
    def test_webhook_signature(self):
        resp = MagicMock(status_code=200)
        with patch.object(notify.httpx, "post", return_value=resp) as post:
            ok = notify.send_webhook("https://hooks.example/x", {"b": 1, "a": "z"}, secret="s3cret")
        self.assertTrue(ok)
        body = post.call_args.kwargs["content"]
        headers = post.call_args.kwargs["headers"]
        expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-DRA-Signature"], expected)
        self.assertEqual(json.loads(body), {"a": "z", "b": 1})

    def test_no_signature_without_secret(self):
        with patch.object(notify.httpx, "post", return_value=MagicMock(status_code=204)) as post:
            self.assertTrue(notify.send_webhook("https://hooks.example/x", {"a": 1}))
        self.assertNotIn("X-DRA-Signature", post.call_args.kwargs["headers"])

    def test_returns_false_on_errors(self):
        with patch.object(notify.httpx, "post", side_effect=httpx.ConnectError("down")):
            with self.assertLogs("dra.notify", level="WARNING") as logs:
                self.assertFalse(notify.send_slack("https://hooks.slack.com/services/SECRET", "hi"))
                self.assertFalse(notify.send_webhook("https://hooks.example/x", {"a": 1}, "k"))
        self.assertFalse(any("SECRET" in line for line in logs.output))
        with patch.object(notify.httpx, "post", side_effect=RuntimeError("boom")):
            self.assertFalse(notify.send_slack("https://hooks.slack.com/x", "hi"))
        with patch.object(notify.httpx, "post", return_value=MagicMock(status_code=500)):
            self.assertFalse(notify.send_slack("https://hooks.slack.com/x", "hi", blocks=[{}]))
        self.assertFalse(notify.send_slack("not a url", "hi"))

    def test_slack_payload(self):
        with patch.object(notify.httpx, "post", return_value=MagicMock(status_code=200)) as post:
            self.assertTrue(notify.send_slack("https://hooks.slack.com/x", "hello", blocks=[{"type": "divider"}]))
        payload = json.loads(post.call_args.kwargs["content"])
        self.assertEqual(payload, {"text": "hello", "blocks": [{"type": "divider"}]})

    def test_format_alert(self):
        o = obs(1, "Destroyed", lat=37.779012, lon=-122.426851, source_file="DEMO_scene_1.png",
                is_demo=True, status="not_reviewed")
        msg = notify.format_alert(o, "Hurricane X", base_url="https://dra.example/")
        self.assertIn("DESTROYED (89%) at 37.77901, -122.42685 — DEMO_scene_1.png", msg)
        self.assertIn("incident: Hurricane X", msg)
        self.assertIn("DEMO", msg)
        self.assertIn("UNVERIFIED", msg)
        self.assertIn("https://dra.example/?observation=o1", msg)
        self.assertNotIn("\n", msg)
        msg2 = notify.format_alert(obs(2, lat=None, source_file="<!channel>\nx"), "I")
        self.assertIn("location unknown", msg2)
        self.assertNotIn("<!channel>", msg2)
        self.assertNotIn("\n", msg2)


class FakeMessages:
    def __init__(self, text=None, exc=None, stop_reason="end_turn"):
        self.text, self.exc, self.stop_reason, self.calls = text, exc, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="thinking", thinking=""),
                     types.SimpleNamespace(type="text", text=self.text)],
            stop_reason=self.stop_reason, model=kwargs["model"])


def fake_client(**kw):
    messages = FakeMessages(**kw)
    return types.SimpleNamespace(messages=messages, beta=types.SimpleNamespace(messages=messages)), messages


class VisionReviewTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (3000, 2000), "gray")

    def test_fenced_json_parsed(self):
        text = ('```json\n{"damage_class": "Major Damage", "confidence": 1.7, '
                '"rationale": "Roof partly collapsed", "hazards": ["fire", "aliens", "fire"]}\n```')
        client, messages = fake_client(text=text)
        with patch.dict("os.environ", {"DRA_VISION_MODEL": ""}):
            out = vision_review.review_crop(self.image, [100, 100, 2900, 1900],
                                            {"incident_name": "X", "damage_class": "Destroyed"},
                                            client=client)
        self.assertEqual(out["damage_class"], "Major Damage")
        self.assertEqual(out["confidence"], 1.0)
        self.assertEqual(out["hazards"], ["fire"])
        self.assertFalse(out["error"])
        call = messages.calls[0]
        self.assertEqual(call["model"], vision_review.DEFAULT_MODEL)
        blocks = call["messages"][0]["content"]
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/jpeg")
        import base64
        crop = Image.open(io.BytesIO(base64.b64decode(blocks[0]["source"]["data"])))
        self.assertLessEqual(max(crop.size), 1024)
        # Prior model label withheld from context to keep the opinion independent.
        self.assertNotIn("Destroyed\"", blocks[1]["text"])

    def test_invalid_class_becomes_uncertain(self):
        client, _ = fake_client(text='{"damage_class": "Kinda bad", "confidence": 0.9, '
                                     '"rationale": "x", "hazards": []}')
        out = vision_review.review_crop(self.image, [0, 0, 50, 50], client=client)
        self.assertEqual(out["damage_class"], "Uncertain")
        self.assertEqual(out["confidence"], 0.0)

    def test_client_exception_returns_error(self):
        client, _ = fake_client(exc=RuntimeError("network down sk-ant-secret"))
        out = vision_review.review_crop(self.image, [0, 0, 50, 50], client=client)
        self.assertTrue(out["error"])
        self.assertEqual(out["damage_class"], "Uncertain")
        self.assertEqual(out["hazards"], [])
        self.assertNotIn("sk-ant", out["rationale"])

    def test_garbage_and_refusal_and_bad_bbox(self):
        client, _ = fake_client(text="I cannot tell.")
        self.assertTrue(vision_review.review_crop(self.image, [0, 0, 50, 50], client=client)["error"])
        client, _ = fake_client(text="", stop_reason="refusal")
        self.assertTrue(vision_review.review_crop(self.image, [0, 0, 50, 50], client=client)["error"])
        client, messages = fake_client(text="{}")
        out = vision_review.review_crop(self.image, [5000, 5000, 6000, 6000], client=client)
        self.assertTrue(out["error"])
        self.assertEqual(messages.calls, [])

    def test_is_configured_and_model_env(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "DRA_VISION_MODEL": "claude-haiku-5-5"}):
            self.assertFalse(vision_review.is_configured())
            self.assertEqual(vision_review.model_id(), "claude-haiku-5-5")
            client, messages = fake_client(text='{"damage_class": "No Damage", "confidence": 0.5}')
            out = vision_review.review_crop(self.image, [0, 0, 50, 50], client=client)
            self.assertEqual(out["damage_class"], "No Damage")
            self.assertNotIn("fallbacks", messages.calls[0])
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x"}):
            self.assertTrue(vision_review.is_configured())


if __name__ == "__main__":
    unittest.main()
