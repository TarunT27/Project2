import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
import pandas as pd

from utils.auth import verified_identity, require_identity
from utils.evaluation import evaluate, iou
from utils.exports import export_csv, export_geojson
from utils.jobs import JobManager
from utils.processing import Input, Settings, process, retry_inputs
from utils.storage import Store, ConflictError


def png(name="image.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), "#89786c").save(buffer, "PNG")
    return Input(name, buffer.getvalue())


def demo_batch():
    return process([], Settings(), threading.Event(), lambda *_: None, built_in_demo=True)[0]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Store(Path(self.temp.name) / "workspace.sqlite3")
        for user in ("owner@example.com", "viewer@example.com", "editor@example.com", "outsider@example.com"):
            self.db.register_user(user)
        self.user = "owner@example.com"
        self.incident = self.db.create_incident(self.user, "Test incident", "2026-10-11", "Test region")
        self.identity = self.db.begin_run(self.incident, self.user, Settings().to_dict())
        self.db.finish_run(self.identity, demo_batch(), "completed")

    def test_persistence_and_incident_isolation(self):
        reopened = Store(self.db.path)
        self.assertEqual(reopened.list_incidents(self.user)[0]["name"], "Test incident")
        self.assertEqual(len(reopened.get_run(self.identity, self.user)["batch"]["images"]), 3)
        self.assertEqual(reopened.list_incidents("outsider@example.com"), [])
        with self.assertRaises(PermissionError):
            reopened.get_run(self.identity, "outsider@example.com")
        with self.assertRaises(PermissionError):
            reopened.audit_log(self.incident, "outsider@example.com")

    def test_viewer_read_only_editor_access_and_revocation(self):
        self.db.grant(self.incident, self.user, "viewer@example.com", "viewer")
        self.db.grant(self.incident, self.user, "editor@example.com", "editor")
        run = self.db.get_run(self.identity, "viewer@example.com")
        row = run["batch"]["rows"][0]
        with self.assertRaises(PermissionError):
            self.db.review(self.identity, "viewer@example.com", run["version"], row["observation_id"], "Destroyed", "reviewed", "bad")
        with self.assertRaises(PermissionError):
            self.db.begin_run(self.incident, "viewer@example.com", {})
        with self.assertRaises(PermissionError):
            self.db.grant(self.incident, "editor@example.com", "outsider@example.com", "viewer")
        self.db.review(self.identity, "editor@example.com", run["version"], row["observation_id"], "Destroyed", "reviewed", "checked")
        self.db.revoke(self.incident, self.user, "editor@example.com")
        with self.assertRaises(PermissionError):
            self.db.get_run(self.identity, "editor@example.com")

    def test_review_original_prediction_audit_conflict_and_exports(self):
        run = self.db.get_run(self.identity, self.user)
        original = run["batch"]["rows"][0]
        self.db.review(self.identity, self.user, run["version"], original["observation_id"], "Destroyed", "reviewed", "=SUM(1,1)")
        with self.assertRaises(ConflictError):
            self.db.review(self.identity, self.user, run["version"], original["observation_id"], "Minor Damage", "not_reviewed", "lost update")
        updated = self.db.get_run(self.identity, self.user)
        row = updated["batch"]["rows"][0]
        self.assertEqual(row["damage_class"], "Destroyed")
        self.assertEqual(row["predicted_class"], original["damage_class"])
        self.assertEqual(row["reviewed_by"], self.user)
        self.assertEqual(len([r for r in self.db.audit_log(self.incident, self.user) if r["action"] == "review_observation"]), 1)
        frame = pd.DataFrame(updated["batch"]["rows"])
        self.assertIn("'=SUM", export_csv(frame).decode("utf-8-sig"))
        props = next(f["properties"] for f in json.loads(export_geojson(frame))["features"] if f["properties"]["observation_id"] == row["observation_id"])
        self.assertEqual(props["predicted_class"], original["damage_class"])
        self.assertEqual(props["review_notes"], "=SUM(1,1)")

    def test_image_coordinates_only_affect_selected_frame_and_preserve_source(self):
        run = self.db.get_run(self.identity, self.user)
        image = run["batch"]["images"][0]
        self.db.locate_image(self.identity, self.user, run["version"], image["image_id"], -30, 120)
        updated = self.db.get_run(self.identity, self.user)["batch"]
        for row in updated["rows"]:
            if row["image_id"] == image["image_id"]:
                self.assertEqual((row["latitude"], row["longitude"]), (-30, 120))
                self.assertEqual(row["original_location_source"], "synthetic_demo")
                self.assertEqual(row["original_latitude"], image["latitude"])
            else:
                self.assertNotEqual(row["latitude"], -30)
        with self.assertRaises(ValueError):
            self.db.locate_image(self.identity, self.user, 3, image["image_id"], 100, 0)

    def test_saved_evaluation_permissions_and_deduplication(self):
        report = evaluate([{"annotations": [], "predictions": []}])
        first = self.db.save_evaluation(self.incident, self.user, report)
        self.assertEqual(first, self.db.save_evaluation(self.incident, self.user, report))
        self.assertEqual(len(self.db.evaluations(self.incident, self.user)), 1)
        self.db.grant(self.incident, self.user, "viewer@example.com", "viewer")
        self.assertEqual(len(self.db.evaluations(self.incident, "viewer@example.com")), 1)
        with self.assertRaises(PermissionError):
            self.db.save_evaluation(self.incident, "viewer@example.com", report)
        with self.assertRaises(ValueError):
            self.db.save_evaluation(self.incident, self.user, dict(report, image_count=-1))

    def test_interrupted_run_recovery(self):
        unfinished = self.db.begin_run(self.incident, self.user, {})
        self.db.recover_interrupted()
        self.assertEqual(self.db.get_run(unfinished, self.user)["status"], "interrupted")
        self.assertEqual(self.db.get_run(self.identity, self.user)["status"], "completed")

    def test_retry_parent_cannot_cross_incidents(self):
        other = self.db.create_incident(self.user, "Other", "2026-10-12")
        with self.assertRaises(ValueError):
            self.db.begin_run(other, self.user, {}, self.identity)


class ProcessingTests(unittest.TestCase):
    def test_failed_file_does_not_remove_successful_file(self):
        batch, status = process([png(), Input("broken.png", b"bad")], Settings(), threading.Event(), lambda *_: None)
        self.assertEqual(status, "partial")
        self.assertEqual(batch["processed"], 1)
        self.assertEqual(batch["failed_files"][0]["name"], "broken.png")
        self.assertEqual(len({r["observation_id"] for r in batch["rows"]}), len(batch["rows"]))

    def test_retry_is_historical_and_deduplicates_successful_sources(self):
        video = Input("video.mp4", b"transient-codec-test")
        with patch("utils.processing.sample_video_frames", side_effect=ValueError("transient decode error")):
            batch, _ = process([png(), video], Settings(), threading.Event(), lambda *_: None)
        old_rows = list(batch["rows"])
        retry = retry_inputs([png(), video], batch)
        self.assertEqual([i.name for i in retry], ["video.mp4"])
        with patch("utils.processing.sample_video_frames", return_value=(frame for frame in [(0.0, Image.new("RGB", (320, 240)))])):
            updated, status = process(retry, Settings(), threading.Event(), lambda *_: None, previous=batch)
        self.assertEqual(status, "completed")
        self.assertEqual(updated["processed"], 2)
        self.assertEqual(updated["rows"][:len(old_rows)], old_rows)
        self.assertEqual(batch["processed"], 1)
        with self.assertRaises(ValueError):
            retry_inputs([png("different.png")], batch)

    def test_cancellation_keeps_only_complete_files(self):
        cancel = threading.Event()
        def progress(value, _):
            if value >= 0.5:
                cancel.set()
        batch, status = process([png(), png("second.png")], Settings(), cancel, progress)
        self.assertEqual(status, "cancelled")
        self.assertEqual(batch["processed"], 1)
        self.assertEqual(batch["failed_files"][0]["name"], "second.png")

    def test_limits_and_duplicate_inputs(self):
        with self.assertRaises(ValueError):
            process([png()] * 21, Settings(), threading.Event(), lambda *_: None)
        with self.assertRaises(ValueError):
            process([png(), png()], Settings(), threading.Event(), lambda *_: None)
        with self.assertRaises(ValueError):
            Settings(threshold=2).validate()

    def test_model_call_cancellation_discards_partial_file(self):
        cancel = threading.Event()
        class Detector:
            def predict(self, image):
                cancel.set()
                return []
        batch, status = process([png()], Settings(), cancel, lambda *_: None, detector_factory=lambda _: Detector())
        self.assertEqual(status, "cancelled")
        self.assertEqual(batch["images"], [])


class JobTests(unittest.TestCase):
    def test_bounded_job_and_owner_only_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "jobs.sqlite3")
            store.register_user("one")
            store.register_user("two")
            incident = store.create_incident("one", "Jobs", "2026-10-11")
            manager = JobManager(store, workers=1)
            gate = threading.Event()
            self.addCleanup(gate.set)
            def delayed(*args, **kwargs):
                gate.wait(timeout=5)
                return process(*args, **kwargs)
            with patch("utils.jobs.process", side_effect=delayed):
                identity = manager.start(incident, "one", Settings(), [], demo=True)
                with self.assertRaises(ValueError):
                    manager.start(incident, "one", Settings(), [], demo=True)
                self.assertIsNone(manager.snapshot(identity, "two"))
                with self.assertRaises(PermissionError):
                    manager.cancel(identity, "two")
                manager.cancel(identity, "one")
                gate.set()
                manager.shutdown()
            result = manager.snapshot(identity, "one")
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(store.get_run(result["run_id"], "one")["status"], "cancelled")


class IdentityTests(unittest.TestCase):
    def test_verified_allowlisted_identity_and_incident_invitation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DISASTER_ADMIN_EMAILS": "admin@example.com", "DISASTER_ALLOWED_EMAILS": ""}, clear=False):
            db = Store(Path(tmp) / "identity.sqlite3")
            claims = {"sub": "abc", "email": "ADMIN@example.com", "email_verified": True}
            self.assertEqual(verified_identity(claims, db), "admin@example.com")
            incident = db.create_incident("admin@example.com", "Auth", "2026-10-11")
            other = {"sub": "other", "email": "guest@example.com", "email_verified": True}
            with self.assertRaises(PermissionError):
                verified_identity(other, db)
            db.grant(incident, "admin@example.com", "guest@example.com", "viewer")
            self.assertEqual(verified_identity(other, db), "guest@example.com")
            with self.assertRaises(PermissionError):
                verified_identity(dict(other, email_verified=False), db)
            db.revoke(incident, "admin@example.com", "guest@example.com")
            with self.assertRaises(PermissionError):
                verified_identity(other, db)

    def test_local_mode_rejects_public_bind(self):
        class Stopped(Exception):
            pass
        class UI:
            def get_option(self, name):
                return "0.0.0.0"
            def error(self, text):
                self.message = text
            def stop(self):
                raise Stopped()
        with patch.dict(os.environ, {"DISASTER_AUTH_MODE": "local"}), self.assertRaises(Stopped):
            require_identity(UI(), None)


class EvaluationTests(unittest.TestCase):
    def test_confusion_missed_damage_false_alarms_and_recall(self):
        box = [0, 0, 10, 10]
        report = evaluate([{"image": "one", "annotations": [{"label": "Destroyed", "bbox": box}, {"label": "Minor Damage", "bbox": [20, 20, 30, 30]}],
                            "predictions": [{"label": "No Damage", "bbox": box}, {"label": "Major Damage", "bbox": [50, 50, 60, 60]}]}])
        self.assertEqual(report["missed_damage"], 2)
        self.assertEqual(report["missed_high_priority"], 1)
        self.assertEqual(report["unmatched_predictions"], 1)
        self.assertEqual(report["confusion_matrix"]["Destroyed"]["No Damage"], 1)
        self.assertEqual(report["per_class"]["Destroyed"]["recall"], 0)
        self.assertIsNone(report["per_class"]["Major Damage"]["recall"])

    def test_one_to_one_matches_and_whole_image_rejection(self):
        region = {"label": "Major Damage", "bbox": [1, 1, 20, 20]}
        report = evaluate([{"annotations": [region], "predictions": [region, region]}])
        self.assertEqual(report["per_class"]["Major Damage"]["true_positive"], 1)
        self.assertEqual(report["per_class"]["Major Damage"]["false_positive"], 1)
        self.assertEqual(iou(region["bbox"], region["bbox"]), 1)
        with self.assertRaises(ValueError):
            evaluate([{"annotations": [region], "predictions": [dict(region, scope="whole_image_classification")]}])
        with self.assertRaises(ValueError):
            evaluate([{"annotations": [dict(region, bbox=[0, 0, float("nan"), 1])], "predictions": []}])


class AppWorkspaceTests(unittest.TestCase):
    def test_unconfigured_oidc_is_a_sign_in_gate_not_an_exception(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DISASTER_DB_PATH": str(Path(tmp) / "auth.sqlite3"), "DISASTER_AUTH_MODE": "oidc"}):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30).run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any(title.value == "Sign in to Disaster Review" for title in app.title))
            self.assertEqual(len(app.tabs), 0)

    def test_create_review_reload_and_concurrent_edit(self):
        from streamlit.testing.v1 import AppTest
        path = str(Path(__file__).resolve().parents[1] / "app.py")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DISASTER_DB_PATH": str(Path(tmp) / "app.sqlite3"), "DISASTER_AUTH_MODE": "local"}):
            app = AppTest.from_file(path, default_timeout=30).run()
            next(w for w in app.text_input if w.label == "Incident name").set_value("Saved test incident")
            next(w for w in app.text_input if w.label == "Incident location").set_value("Test region")
            next(b for b in app.button if b.label == "Create incident").click().run()
            self.assertEqual(len(app.exception), 0)
            db = Store(Path(tmp) / "app.sqlite3")
            incident = db.list_incidents("local")[0]
            self.assertEqual(incident["name"], "Saved test incident")
            self.assertEqual(app.session_state.selected_incident, incident["id"])
            next(b for b in app.button if b.label == "Load synthetic demo").click().run()
            deadline = time.monotonic() + 10
            while not app.session_state.assessment["processed"] and time.monotonic() < deadline:
                time.sleep(0.05)
                app.run()
            self.assertEqual(len(app.exception), 0)
            next(w for w in app.text_area if w.label == "Review notes").set_value("Persist this review")
            next(w for w in app.selectbox if w.label == "Review status").set_value("reviewed")
            next(b for b in app.button if b.label == "Save review").click().run()
            self.assertEqual(len(app.exception), 0)
            active_id = app.session_state.active_run
            saved = db.get_run(active_id, "local")
            self.assertEqual(saved["batch"]["rows"][0]["review_notes"], "Persist this review")
            reopened = AppTest.from_file(path, default_timeout=30).run()
            self.assertEqual(reopened.session_state.active_run, active_id)
            self.assertEqual(next(w for w in reopened.text_area if w.label == "Review notes").value, "Persist this review")
            row = saved["batch"]["rows"][0]
            db.review(active_id, "local", saved["version"], row["observation_id"], "Destroyed", "needs_follow_up", "Other reviewer saved first")
            next(w for w in reopened.text_area if w.label == "Review notes").set_value("Stale edit")
            next(b for b in reopened.button if b.label == "Save review").click().run()
            self.assertEqual(len(reopened.exception), 0)
            self.assertTrue(any("changed" in error.value for error in reopened.error))
            self.assertEqual(db.get_run(active_id, "local")["batch"]["rows"][0]["review_notes"], "Other reviewer saved first")


class EvaluationCliTests(unittest.TestCase):
    def test_real_adapter_evaluation_provenance_with_injected_detector(self):
        from evaluate_model import evaluate_model
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Image.new("RGB", (100, 80)).save(root / "test.png")
            (root / "model.pt").write_bytes(b"unit-test-checkpoint-placeholder")
            manifest = {"split": "test", "images": [{"image": "test.png", "width": 100, "height": 80, "annotations": [{"label": "Destroyed", "bbox": [10, 10, 50, 60]}]}]}
            (root / "manifest.json").write_text(json.dumps(manifest))
            adapter = SimpleNamespace(model=SimpleNamespace(task="detect"), predict=lambda image: [{"label": "Destroyed", "bbox": [10, 10, 50, 60], "scope": "detected_region"}])
            with patch("evaluate_model.DamageDetector", return_value=adapter):
                report = evaluate_model(root / "manifest.json", root / "model.pt")
                self.assertEqual(report["per_class"]["Destroyed"]["recall"], 1)
                self.assertEqual(len(report["model_sha256"]), 64)
                self.assertEqual(len(report["dataset_sha256"]), 64)
                manifest["images"][0]["width"] = 500
                (root / "manifest.json").write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    evaluate_model(root / "manifest.json", root / "model.pt")


if __name__ == "__main__":
    unittest.main()
