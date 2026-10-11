"""Headless regression tests for CI. Run with unittest discover -s browser_tests -v."""
import csv
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from PIL import Image
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = dict(os.environ, DISASTER_DB_PATH=str(Path(cls.temp.name) / "browser.sqlite3"), DISASTER_AUTH_MODE="local")
        cls.logs = open(Path(cls.temp.name) / "server.log", "w+")
        cls.server = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless=true", "--server.address=127.0.0.1", "--server.port=8511"], cwd=ROOT, env=cls.env, stdout=cls.logs, stderr=subprocess.STDOUT)
        cls.wait_server(8511)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def wait_server(cls, port):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=1)
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Test server did not become ready.")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.terminate()
        cls.server.wait(timeout=15)
        cls.logs.close()
        cls.temp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.goto("http://127.0.0.1:8511")
        expect(self.page.get_by_role("tab", name="Assess", exact=True)).to_be_visible(timeout=20000)
        # Each test owns a new incident, rather than depending on another test's state.
        self.page.get_by_text("New incident", exact=True).click()
        self.page.get_by_label("Incident name", exact=True).fill(self.id().split(".")[-1])
        self.page.get_by_role("button", name="Create incident", exact=True).click()
        expect(self.page.get_by_label("Incident", exact=True)).to_have_value(re.compile("^" + re.escape(self.id().split(".")[-1]) + " ·"))
        expect(self.page.get_by_role("button", name="Load synthetic demo", exact=True)).to_be_visible()

    def load_demo(self):
        self.page.get_by_role("button", name="Load synthetic demo", exact=True).click()
        expect(self.page.get_by_role("heading", name="Review imagery", exact=True)).to_be_visible(timeout=20000)

    def test_saved_review_map_and_exports(self):
        self.load_demo()
        self.page.get_by_label("Review notes", exact=True).first.fill("Browser regression review")
        self.page.get_by_label("Review status", exact=True).first.click()
        self.page.get_by_role("option", name="reviewed", exact=True).click()
        self.page.get_by_role("button", name="Save review", exact=True).first.click()
        expect(self.page.get_by_text("Review saved. The original prediction is preserved.", exact=True)).to_be_visible()
        self.page.reload()
        expect(self.page.get_by_label("Review notes", exact=True).first).to_have_value("Browser regression review", timeout=20000)
        self.page.get_by_role("tab", name="Map", exact=True).click()
        expect(self.page.get_by_role("heading", name="Linked source image", exact=True)).to_be_visible()
        expect(self.page.get_by_label("Mapped observation", exact=True)).to_be_visible()
        frame = self.page.frame_locator("iframe")
        circles = frame.locator('path.leaflet-interactive[d*="a10"]')
        for _ in range(6):
            if circles.count():
                break
            clusters = frame.locator(".marker-cluster")
            expect(clusters.first).to_be_visible(timeout=5000)
            clusters.first.click()
            # Leaflet cluster animation is asynchronous; a bounded pause is confined to CI.
            self.page.wait_for_timeout(500)
        expect(circles.first).to_be_visible()
        circles.first.hover()
        tooltip = frame.locator(".leaflet-tooltip").inner_text()
        clicked_id = re.search(r"Observation ([a-f0-9]{24}-\d+)", tooltip)[1]
        from utils.storage import Store
        database = Store(self.env["DISASTER_DB_PATH"])
        incident = database.list_incidents("local")[0]
        saved = database.get_run(database.list_runs(incident["id"], "local")[0]["id"], "local")
        row = next(row for row in saved["batch"]["rows"] if row["observation_id"] == clicked_id)
        circles.first.click()
        expect(self.page.get_by_label("Mapped observation", exact=True)).to_have_value(f'{row["source_file"]} · {row["damage_class"]} · {clicked_id[-4:]}')
        self.page.get_by_role("tab", name="Export", exact=True).click()
        with self.page.expect_download() as result:
            self.page.get_by_role("button", name="Download priority CSV", exact=True).click()
        rows = list(csv.DictReader(Path(result.value.path()).read_text(encoding="utf-8-sig").splitlines()))
        reviewed = next(row for row in rows if row["review_notes"] == "Browser regression review")
        self.assertEqual(reviewed["review_status"], "reviewed")
        self.assertTrue(reviewed["predicted_class"])
        with self.page.expect_download() as result:
            self.page.get_by_role("button", name="Download GeoJSON", exact=True).click()
        self.assertEqual(len(json.loads(Path(result.value.path()).read_text())["features"]), len(rows))
        self.page.get_by_role("tab", name="History", exact=True).click()
        expect(self.page.get_by_role("heading", name="Assessment history", exact=True)).to_be_visible()
        self.page.get_by_role("button", name="Open saved assessment", exact=True).click()
        self.page.get_by_role("tab", name="Assess", exact=True).click()
        expect(self.page.get_by_label("Review notes", exact=True).first).to_have_value("Browser regression review")

    def test_real_upload_partial_failure_and_mobile_layout(self):
        buffer = io.BytesIO()
        Image.new("RGB", (320, 240), "#89786c").save(buffer, "PNG")
        input_element = self.page.locator('input[type="file"]').first
        input_element.set_input_files([
            {"name": "valid.png", "mimeType": "image/png", "buffer": buffer.getvalue()},
            {"name": "broken.png", "mimeType": "image/png", "buffer": b"not an image"},
        ])
        self.page.get_by_role("button", name="Run assessment", exact=True).click()
        expect(self.page.get_by_role("heading", name="Review imagery", exact=True)).to_be_visible(timeout=20000)
        expect(self.page.get_by_text("Failed: broken.png", exact=True)).to_be_visible()
        expect(self.page.get_by_role("button", name="Retry failed files", exact=True)).to_be_enabled()
        self.page.get_by_role("button", name="Retry failed files", exact=True).click()
        expect(self.page.get_by_text("Assessment partial", exact=True)).to_be_visible(timeout=20000)
        self.page.set_viewport_size({"width": 390, "height": 844})
        expect(self.page.get_by_role("tab", name="Assess", exact=True)).to_be_visible()
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        expect(self.page.get_by_test_id("stException")).to_have_count(0)

    def test_shared_mode_requires_sign_in(self):
        shared = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless=true", "--server.address=127.0.0.1", "--server.port=8512"], cwd=ROOT,
            env=dict(self.env, DISASTER_AUTH_MODE="oidc"), stdout=self.logs, stderr=subprocess.STDOUT)
        try:
            self.wait_server(8512)
            self.page.goto("http://127.0.0.1:8512")
            expect(self.page.get_by_role("heading", name="Sign in to Disaster Review", exact=True)).to_be_visible(timeout=20000)
            expect(self.page.get_by_role("tab", name="Assess", exact=True)).to_have_count(0)
        finally:
            shared.terminate()
            shared.wait(timeout=15)


if __name__ == "__main__":
    unittest.main()
