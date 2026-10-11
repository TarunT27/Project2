import csv
import io
import json
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from server import app


def png_bytes(size=(320, 240)):
    buf = io.BytesIO()
    Image.new("RGB", size, (120, 140, 150)).save(buf, "PNG")
    return buf.getvalue()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_index_and_config(self):
        self.assertIn("Rapid Disaster Assessment", self.client.get("/").text)
        config = self.client.get("/api/config").json()
        self.assertEqual(config["classes"], ["No Damage", "Minor Damage", "Major Damage", "Destroyed"])
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)

    def test_demo_run_and_exports(self):
        body = self.client.post("/api/demo", data={"confidence": "0.5"}).json()
        self.assertTrue(body["demo"])
        self.assertEqual(body["summary"]["processed"], 3)
        self.assertEqual(body["summary"]["total"], len(body["rows"]))
        self.assertEqual(body["summary"]["mapped"], len(body["rows"]))
        self.assertTrue(all("preview" not in r for r in body["rows"]))
        self.assertTrue(all(img["preview"].startswith("data:image/jpeg") for img in body["images"]))

        response = self.client.get(f"/api/runs/{body['run_id']}/export.csv")
        self.assertIn("DEMO_damage_review_priority.csv", response.headers["content-disposition"])
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), len(body["rows"]))
        self.assertEqual(rows[0]["priority_rank"], "1")
        self.assertEqual(rows[0]["damage_class"], "Destroyed")

        geo = json.loads(self.client.get(f"/api/runs/{body['run_id']}/export.geojson").content)
        self.assertEqual(len(geo["features"]), len(body["rows"]))

        # The console's live filter narrows exports and records the effective threshold.
        strict = list(csv.DictReader(io.StringIO(
            self.client.get(f"/api/runs/{body['run_id']}/export.csv?min_conf=0.9").content.decode("utf-8-sig")
        )))
        self.assertTrue(all(float(r["confidence"]) >= 0.9 for r in strict))
        self.assertTrue(all(r["confidence_threshold"] == "0.9" for r in strict))
        self.assertLess(len(strict), len(rows))

    def test_upload_image(self):
        response = self.client.post(
            "/api/assess",
            data={"mode": "demo", "confidence": "0.5"},
            files=[("files", ("roof.png", png_bytes(), "image/png"))],
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["errors"], [])
        self.assertEqual(body["images"][0]["width"], 320)
        self.assertEqual(body["images"][0]["location_source"], "synthetic_demo")

    def test_bad_upload_reported_without_failing_batch(self):
        body = self.client.post(
            "/api/assess",
            data={"mode": "demo"},
            files=[("files", ("bad.jpg", b"not an image", "image/jpeg")), ("files", ("ok.png", png_bytes(), "image/png"))],
        ).json()
        self.assertEqual(len(body["errors"]), 1)
        self.assertEqual(body["summary"]["processed"], 1)

    def test_rejections(self):
        missing = self.client.post(
            "/api/assess",
            data={"mode": "real", "model_path": "does/not/exist.pt"},
            files=[("files", ("a.png", png_bytes(), "image/png"))],
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("damage-trained", missing.json()["error"])
        too_many = self.client.post(
            "/api/assess", files=[("files", (f"{i}.png", b"x", "image/png")) for i in range(21)]
        )
        self.assertEqual(too_many.status_code, 413)
        wrong_type = self.client.post("/api/assess", files=[("files", ("a.exe", b"x", "application/octet-stream"))])
        self.assertEqual(wrong_type.status_code, 400)
        self.assertEqual(self.client.get("/api/runs/nope/export.csv").status_code, 404)
        self.assertEqual(self.client.post("/api/demo", data={"lat": "120"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
