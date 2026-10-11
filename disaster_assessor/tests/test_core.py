import io
import json
import tempfile
import os
import time
from pathlib import Path
import unittest
from unittest.mock import patch
import types

import cv2
import numpy as np
import pandas as pd
import piexif
from PIL import Image
from utils.detector import DamageDetector, annotate_image
from utils.geo_utils import extract_gps_from_exif, generate_damage_map, valid_coordinate
from utils.exports import export_csv, export_geojson, priority_list
from utils.media import sample_video_frames
from utils.mock_data import sample_images


class CoreTests(unittest.TestCase):
    def test_demo_deterministic_and_threshold(self):
        image = next(sample_images())[1]
        a = DamageDetector().predict(image)
        self.assertEqual(a, DamageDetector().predict(image))
        self.assertTrue(all(d["is_demo"] for d in a))
        self.assertEqual(DamageDetector(confidence_threshold=1).predict(image), [])
        self.assertEqual(annotate_image(image, a).size, image.size)

    def test_missing_gps_provenance(self):
        image = Image.new("RGB", (100, 100))
        first = extract_gps_from_exif(image, seed="one")
        self.assertEqual(first, extract_gps_from_exif(image, seed="one"))
        self.assertTrue(first["location_is_synthetic"])
        real = extract_gps_from_exif(image, demo_fallback=False)
        self.assertIsNone(real["latitude"])
        self.assertFalse(real["location_is_synthetic"])
        self.assertFalse(valid_coordinate(float("nan"), 0))
        self.assertFalse(valid_coordinate(95, 0))

    def test_real_exif_including_southern_western_hemisphere(self):
        image = Image.new("RGB", (100, 100))
        gps = {
            piexif.GPSIFD.GPSLatitudeRef: b"S",
            piexif.GPSIFD.GPSLatitude: ((33, 1), (30, 1), (0, 1)),
            piexif.GPSIFD.GPSLongitudeRef: b"W",
            piexif.GPSIFD.GPSLongitude: ((70, 1), (15, 1), (0, 1)),
        }
        exif = piexif.dump(
            {
                "GPS": gps,
                "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2026:10:11 12:30:00"},
            }
        )
        buf = io.BytesIO()
        image.save(buf, "JPEG", exif=exif)
        buf.seek(0)
        result = extract_gps_from_exif(Image.open(buf), demo_fallback=False)
        self.assertEqual(result["latitude"], -33.5)
        self.assertEqual(result["longitude"], -70.25)
        self.assertEqual(result["timestamp"], "2026:10:11 12:30:00")
        self.assertEqual(result["location_source"], "exif_camera")

    def test_ranking_geojson_and_formula_safety(self):
        df = pd.DataFrame(
            [
                {
                    "observation_id": "1",
                    "source_file": "=SUM(1,1)",
                    "damage_class": "Minor Damage",
                    "confidence": 0.99,
                    "latitude": 1.0,
                    "longitude": 2.0,
                    "is_demo": True,
                    "bbox": [1, 2, 3, 4],
                },
                {
                    "observation_id": "2",
                    "source_file": "x.jpg",
                    "damage_class": "Destroyed",
                    "confidence": 0.6,
                    "latitude": None,
                    "longitude": None,
                    "is_demo": True,
                    "bbox": [1, 2, 3, 4],
                },
            ]
        )
        self.assertEqual(priority_list(df).iloc[0]["observation_id"], "2")
        geo = json.loads(export_geojson(df))
        self.assertEqual(len(geo["features"]), 1)
        self.assertEqual(geo["features"][0]["geometry"]["coordinates"], [2.0, 1.0])
        self.assertTrue(geo["features"][0]["properties"]["is_demo"])
        self.assertIn("'=SUM", export_csv(df).decode("utf-8-sig"))
        self.assertEqual(json.loads(export_geojson(pd.DataFrame()))["features"], [])

    def test_map_sanitizes_filename_and_omits_missing_coordinates(self):
        rows = pd.DataFrame(
            [
                {
                    "damage_class": "Destroyed",
                    "confidence": 0.7,
                    "latitude": 1,
                    "longitude": 2,
                    "source_file": "<script>alert(1)</script>",
                    "is_demo": True,
                }
            ]
        )
        html = generate_damage_map(rows, show_tiles=False).get_root().render()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("SYNTHETIC DEMO", html)
        self.assertIn("#dc2626", html)
        self.assertNotIn("fitBounds", html)
        self.assertGreater(generate_damage_map(rows).options["zoom"], 10)

    def test_video_sampling(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.avi"
            writer = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 64)
            )
            self.assertTrue(writer.isOpened())
            for i in range(30):
                writer.write(np.full((64, 64, 3), i * 5, dtype=np.uint8))
            writer.release()
            frames = list(sample_video_frames(path.read_bytes(), ".avi", 1, 2))
            self.assertEqual([f[0] for f in frames], [0.0, 1.0])
            self.assertEqual(frames[0][1].size, (64, 64))

    def test_generic_weights_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".pt") as file:
            model = types.SimpleNamespace(names={0: "person", 1: "car"}, task="detect")
            with patch.dict(
                "sys.modules",
                {"ultralytics": types.SimpleNamespace(YOLO=lambda _: model)},
            ):
                with self.assertRaisesRegex(ValueError, "four damage classes"):
                    DamageDetector(False, file.name)

    def test_real_detector_maps_damage_boxes(self):
        class Tensor:
            def __init__(self, value):
                self.value = value

            def cpu(self):
                return self

            def tolist(self):
                return self.value

        names = {0: "no-damage", 1: "minor-damage", 2: "major-damage", 3: "destroyed"}
        boxes = types.SimpleNamespace(
            xyxy=Tensor([[1, 2, 40, 50]]), cls=Tensor([2]), conf=Tensor([0.85])
        )
        result = types.SimpleNamespace(boxes=boxes, probs=None, names=names)
        model = types.SimpleNamespace(
            names=names, task="detect", predict=lambda **_: [result]
        )
        with tempfile.NamedTemporaryFile(suffix=".pt") as file, patch.dict(
            "sys.modules", {"ultralytics": types.SimpleNamespace(YOLO=lambda _: model)}
        ):
            predictions = DamageDetector(False, file.name).predict(
                Image.new("RGB", (100, 100))
            )
            self.assertEqual(predictions[0]["label"], "Major Damage")
            self.assertFalse(predictions[0]["is_demo"])


class AppTests(unittest.TestCase):
    def test_demo_flow_reset_and_missing_model(self):
        from streamlit.testing.v1 import AppTest

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"DISASTER_DB_PATH": str(Path(temporary.name) / "test.sqlite3"), "DISASTER_AUTH_MODE": "local"})
        environment.start()
        self.addCleanup(environment.stop)
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30
        ).run()
        self.assertEqual(len(app.exception), 0)
        next(b for b in app.button if b.label == "Load synthetic demo").click().run()
        self.assertEqual(len(app.exception), 0)
        deadline = time.monotonic() + 10
        while not app.session_state.assessment["processed"] and time.monotonic() < deadline:
            time.sleep(0.05)
            app.run()
        self.assertGreater(len(app.session_state.assessment["rows"]), 0)
        self.assertEqual(app.session_state.assessment["processed"], 3)
        next(b for b in app.button if b.label == "Clear session").click().run()
        self.assertEqual(app.session_state.assessment["rows"], [])
        app.radio[0].set_value("Real · damage-trained YOLO").run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            next(b for b in app.button if b.label == "Load synthetic demo").disabled
        )


if __name__ == "__main__":
    unittest.main()
