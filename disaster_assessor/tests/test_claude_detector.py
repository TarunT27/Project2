"""Claude vision detector: tiling, merging, validation, refusals, failures and cost tracking (fake client)."""

import base64
import io
import json
import types
import unittest

from PIL import Image

from app.services import claude_detector as cd


def response(buildings, stop_reason="end_turn", input_tokens=1000, output_tokens=200):
    text = json.dumps({"scene": "nadir aerial", "buildings": buildings})
    return types.SimpleNamespace(
        stop_reason=stop_reason, model="claude-opus-5-5",
        content=[types.SimpleNamespace(type="text", text=text)],
        usage=types.SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeClient:
    """Returns one building at the centre of whatever image it receives; records requests."""

    def __init__(self, handler=None):
        self.requests = []
        self.handler = handler
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self.create))
        self.messages = types.SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        block = kwargs["messages"][0]["content"][0]
        with Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))) as img:
            w, h = img.size
        if self.handler:
            return self.handler(w, h, kwargs)
        return response([{"box": [w // 2 - 20, h // 2 - 20, w // 2 + 20, h // 2 + 20], "damage_class": "Destroyed",
                          "confidence": 0.9, "rationale": "Only a slab remains", "hazards": ["debris_blocking_access"]}])


class ClaudeDetectorTests(unittest.TestCase):
    def test_tile_grid_covers_image_with_overlap(self):
        self.assertEqual(cd.tile_grid(1000, 800), [(0, 0, 1000, 800)])
        tiles = cd.tile_grid(3000, 2000)
        self.assertGreater(len(tiles), 1)
        covered = Image.new("1", (3000, 2000), 0)
        for x, y, w, h in tiles:
            self.assertLessEqual(w, cd.TILE)
            covered.paste(1, (x, y, x + w, y + h))
        self.assertEqual(covered.getbbox(), (0, 0, 3000, 2000))
        self.assertTrue(all(covered.getpixel((px, py)) for px in range(0, 3000, 97) for py in range(0, 2000, 97)))

    def test_merge_keeps_most_confident_and_drops_contained(self):
        dets = [{"bbox": [0, 0, 100, 100], "confidence": 0.6}, {"bbox": [5, 5, 100, 100], "confidence": 0.9},
                {"bbox": [20, 20, 60, 60], "confidence": 0.5}, {"bbox": [300, 300, 340, 340], "confidence": 0.7}]
        kept = cd.merge_boxes(dets)
        self.assertEqual([d["confidence"] for d in kept], [0.9, 0.7])

    def test_single_image_request_shape_and_output(self):
        fake = FakeClient()
        det = cd.ClaudeDetector(0.5, client=fake)
        out = det.predict(Image.new("RGB", (800, 600), "gray"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["label"], "Destroyed")
        self.assertEqual(out[0]["bbox"], [380, 280, 420, 320])
        self.assertFalse(out[0]["is_demo"])
        self.assertEqual(out[0]["scope"], "claude_vision")
        req = fake.requests[0]
        self.assertEqual(req["model"], "claude-opus-5-5")
        self.assertEqual(req["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(req["fallbacks"], "default")
        self.assertNotIn("temperature", req)
        self.assertIn("800 x 600", req["messages"][0]["content"][1]["text"])
        self.assertEqual(det.usage["calls"], 1)
        self.assertAlmostEqual(det.cost_usd(), 1000 / 1e6 * 4 + 200 / 1e6 * 20)

    def test_tiled_prediction_maps_boxes_back(self):
        fake = FakeClient()
        out = cd.ClaudeDetector(0.5, client=fake).predict(Image.new("RGB", (3000, 2000)), tiled=True)
        self.assertEqual(len(fake.requests), len(cd.tile_grid(3000, 2000)))
        self.assertEqual(len(out), len(fake.requests))  # one distinct centre per tile
        for d in out:
            self.assertTrue(0 <= d["bbox"][0] < d["bbox"][2] <= 3000 and 0 <= d["bbox"][1] < d["bbox"][3] <= 2000)

    def test_validation_clamps_and_filters(self):
        def handler(w, h, _):
            return response([
                {"box": [-50, -50, 100, 100], "damage_class": "Minor Damage", "confidence": 0.8, "rationale": "x", "hazards": []},
                {"box": [10, 10, 12, 12], "damage_class": "Destroyed", "confidence": 0.9, "rationale": "tiny", "hazards": []},
                {"box": [200, 200, 300], "damage_class": "Destroyed", "confidence": 0.9, "rationale": "bad", "hazards": []},
                {"box": [200, 200, 300, 300], "damage_class": "No Damage", "confidence": 0.3, "rationale": "low", "hazards": []},
                {"box": [400, 100, 500, 200], "damage_class": "Major Damage", "confidence": 7, "rationale": "y",
                 "hazards": ["fire", "aliens"]},
            ])
        out = cd.ClaudeDetector(0.5, client=FakeClient(handler)).predict(Image.new("RGB", (640, 480)))
        self.assertEqual([d["label"] for d in out], ["Minor Damage", "Major Damage"])
        self.assertEqual(out[0]["bbox"], [0, 0, 100, 100])
        self.assertEqual(out[1]["confidence"], 1.0)
        self.assertEqual(out[1]["hazards"], ["fire"])

    def test_refusal_returns_nothing_and_is_counted(self):
        det = cd.ClaudeDetector(0.5, client=FakeClient(lambda w, h, _: response([], stop_reason="refusal")))
        self.assertEqual(det.predict(Image.new("RGB", (300, 300))), [])
        self.assertEqual(det.usage["refusals"], 1)

    def test_all_tiles_failing_raises_readable_error(self):
        def boom(w, h, _):
            raise ConnectionError("network down")
        det = cd.ClaudeDetector(0.5, client=FakeClient(boom))
        with self.assertRaisesRegex(RuntimeError, "Claude detection failed"):
            det.predict(Image.new("RGB", (300, 300)))

    def test_requires_key_without_client(self):
        import os
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(cd.ClaudeUnavailable):
                cd.ClaudeDetector(0.5)
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved
