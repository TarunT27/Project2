"""Training-pipeline tests that run without torch / ultralytics."""

import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from training import metrics
from training.evaluate import evaluate_detection, read_yolo_labels
from training.prepare_xbd import (
    event_name,
    parse_wkt_points,
    prepare,
    split_events,
    wkt_bbox,
    yolo_label_line,
)
from training.train import build_train_kwargs, parse_args, write_metadata
from utils.mock_data import CLASSES
from utils.two_stage import TwoStageDetector, crop_box


def polygon(x1, y1, x2, y2):
    return f"POLYGON (({x1} {y1}, {x2} {y1}, {x2} {y2}, {x1} {y2}, {x1} {y1}))"


def write_xbd_image(split_dir, stem, buildings, size=(256, 256)):
    (split_dir / "images").mkdir(parents=True, exist_ok=True)
    (split_dir / "labels").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (40, 90, 40)).save(split_dir / "images" / f"{stem}.png")
    features = [
        {
            "properties": {"feature_type": "building", "subtype": subtype, "uid": f"{stem}-{i}"},
            "wkt": polygon(*box),
        }
        for i, (box, subtype) in enumerate(buildings)
    ]
    label = {
        "features": {"lng_lat": [], "xy": features},
        "metadata": {"width": size[0], "height": size[1], "img_name": f"{stem}.png"},
    }
    (split_dir / "labels" / f"{stem}.json").write_text(json.dumps(label))


class WktAndLabelTests(unittest.TestCase):
    def test_polygon_bbox(self):
        wkt = "POLYGON ((10.5 20, 30 22.25, 28 40.75, 9 35, 10.5 20))"
        self.assertEqual(len(parse_wkt_points(wkt)), 5)
        self.assertEqual(wkt_bbox(wkt), [9.0, 20.0, 30.0, 40.75])
        self.assertEqual(wkt_bbox("MULTIPOLYGON (((1 2, 3 4, 5 1, 1 2)), ((7 8, 9 9, 8 7, 7 8)))"), [1, 1, 9, 9])
        with self.assertRaises(ValueError):
            wkt_bbox("POINT (1 2)")
        with self.assertRaises(ValueError):
            wkt_bbox("POLYGON EMPTY")

    def test_yolo_normalization_and_clipping(self):
        self.assertEqual(yolo_label_line(0, [100, 200, 300, 600], 1000, 1000), "0 0.200000 0.400000 0.200000 0.400000")
        # Clipped to image bounds: x -50..50 -> 0..50
        self.assertEqual(yolo_label_line(3, [-50, 0, 50, 100], 100, 100), "3 0.250000 0.500000 0.500000 1.000000")
        self.assertIsNone(yolo_label_line(0, [200, 200, 300, 300], 100, 100))
        gt = read_yolo_labels_from_text("2 0.5 0.5 0.2 0.4\n", 100, 200)
        self.assertEqual(gt, [([40.0, 60.0, 60.0, 140.0], "Major Damage")])

    def test_event_name(self):
        self.assertEqual(event_name("hurricane-harvey_00000123_post_disaster"), "hurricane-harvey")
        self.assertEqual(event_name("socal-fire_00001400_post_disaster.png"), "socal-fire")


def read_yolo_labels_from_text(text, width, height):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.txt"
        path.write_text(text)
        return read_yolo_labels(path, width, height, list(CLASSES))


class SplitTests(unittest.TestCase):
    COUNTS = {f"event-{i}": 10 + i * 3 for i in range(12)}

    def test_deterministic_and_order_independent(self):
        a = split_events(self.COUNTS, 0.15, seed=1)
        b = split_events(dict(reversed(list(self.COUNTS.items()))), 0.15, seed=1)
        self.assertEqual(a, b)

    def test_no_leakage_and_reasonable_fraction(self):
        for seed in range(5):
            train, val = split_events(self.COUNTS, 0.15, seed=seed)
            self.assertFalse(train & val)
            self.assertEqual(train | val, set(self.COUNTS))
            self.assertTrue(train and val)
            fraction = sum(self.COUNTS[e] for e in val) / sum(self.COUNTS.values())
            self.assertLess(fraction, 0.3)

    def test_edge_cases(self):
        self.assertEqual(split_events({"a": 5}, 0.15), ({"a"}, set()))
        self.assertEqual(split_events({"a": 5, "b": 5}, 0.0), ({"a", "b"}, set()))
        self.assertEqual(split_events({"a": 1, "b": 1, "c": 1}, 0.15, val_events=["b"]), ({"a", "c"}, {"b"}))
        train, val = split_events({"huge": 1000, "small": 10}, 0.5)
        self.assertEqual(len(val), 1)
        self.assertEqual(len(train), 1)


class PrepareXbdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root, self.out = base / "xbd", base / "out"
        tier1 = self.root / "tier1"
        write_xbd_image(tier1, "flood-a_00000000_post_disaster", [
            ((10, 10, 60, 60), "no-damage"),
            ((100, 100, 150, 140), "destroyed"),
            ((200, 200, 205, 205), "minor-damage"),  # 5px: too small for crops
        ])
        write_xbd_image(tier1, "flood-a_00000001_post_disaster", [
            ((20, 20, 80, 70), "major-damage"),
            ((120, 30, 170, 90), "un-classified"),
        ])
        # Pre-disaster labels must be ignored.
        write_xbd_image(tier1, "flood-a_00000001_pre_disaster", [((0, 0, 50, 50), None)])
        write_xbd_image(self.root / "tier3", "fire-b_00000000_post_disaster", [
            ((30, 30, 90, 90), "minor-damage"),
            ((150, 150, 230, 220), "minor-damage"),
            ((10, 150, 70, 210), "destroyed"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def run_prepare(self, **kwargs):
        return prepare(self.root, self.out, modes=("building", "cls", "det4"), verbose=False, **kwargs)

    def split_for(self, summary, event):
        return "val" if event in summary["val_events"] else "train"

    def test_all_modes(self):
        summary = self.run_prepare()
        self.assertEqual(summary["images"], 3)
        self.assertEqual(set(summary["train_events"]) | set(summary["val_events"]), {"flood-a", "fire-b"})
        self.assertEqual(len(summary["val_events"]), 1)
        flood, fire = self.split_for(summary, "flood-a"), self.split_for(summary, "fire-b")
        self.assertNotEqual(flood, fire)

        # building: all 8 post-disaster footprints, un-classified included
        building = self.out / "xbd_building"
        counts = summary["datasets"]["building"]["counts"]
        self.assertEqual(counts[flood]["building"], 5)
        self.assertEqual(counts[fire]["building"], 3)
        label = (building / "labels" / flood / "flood-a_00000000_post_disaster.txt").read_text().split("\n")
        self.assertEqual(label[0], yolo_label_line(0, [10, 10, 60, 60], 256, 256))
        image = building / "images" / flood / "flood-a_00000000_post_disaster.png"
        self.assertTrue(image.is_symlink() and image.exists())
        self.assertFalse(list(building.rglob("*pre_disaster*")))
        yaml = (building / "data.yaml").read_text()
        self.assertIn('0: "building"', yaml)
        self.assertIn("nc: 1", yaml)

        # det4: un-classified dropped, class ids follow CLASSES order
        det4 = self.out / "xbd_det4"
        counts = summary["datasets"]["det4"]["counts"]
        self.assertEqual(counts[flood], {"No Damage": 1, "Destroyed": 1, "Minor Damage": 1, "Major Damage": 1,
                                         "skipped_unclassified": 1, "images": 2})
        self.assertEqual(counts[fire]["Minor Damage"], 2)
        first_ids = [line.split()[0] for line in (det4 / "labels" / flood / "flood-a_00000000_post_disaster.txt").read_text().split()[::5]]
        self.assertEqual(first_ids, ["0", "3", "1"])
        self.assertIn('3: "Destroyed"', (det4 / "data.yaml").read_text())

        # cls: four folders per split, 224px crops, small and un-classified skipped
        cls_root = self.out / "xbd_cls"
        for split in ("train", "val"):
            self.assertEqual(sorted(p.name for p in (cls_root / split).iterdir()), sorted(CLASSES))
        counts = summary["datasets"]["cls"]["counts"]
        self.assertEqual(counts[flood].get("No Damage"), 1)
        self.assertEqual(counts[flood].get("skipped_small"), 1)
        self.assertEqual(counts[flood].get("skipped_unclassified"), 1)
        self.assertEqual(counts[fire].get("Minor Damage"), 2)
        crops = list(cls_root.rglob("*.jpg"))
        self.assertEqual(len(crops), 6)
        with Image.open(crops[0]) as crop:
            self.assertEqual(crop.size, (224, 224))
        self.assertEqual(len((cls_root / "crops.csv").read_text().strip().splitlines()), 7)

    def test_balance_caps_train_only(self):
        summary = self.run_prepare(val_events=["flood-a"], balance=1)
        counts = summary["datasets"]["cls"]["counts"]
        self.assertEqual(counts["train"]["Minor Damage"], 1)
        self.assertEqual(counts["train"]["capped_by_balance"], 1)
        self.assertEqual(counts["val"]["No Damage"], 1)

    def test_deterministic(self):
        first = self.run_prepare()
        second = self.run_prepare()
        self.assertEqual(first["val_events"], second["val_events"])
        self.assertEqual(first["datasets"]["cls"]["counts"], second["datasets"]["cls"]["counts"])


class MetricsTests(unittest.TestCase):
    CLASSES = ["A", "B"]

    def test_hand_computed_detection_example(self):
        cm = metrics.empty_matrix(3)
        gt = [([0, 0, 10, 10], "A"), ([20, 20, 30, 30], "A"), ([40, 40, 50, 50], "B"), ([60, 60, 70, 70], "B")]
        preds = [
            ([0, 0, 10, 11], "A", 0.9),     # matches gt0, correct
            ([20, 20, 30, 30], "B", 0.8),   # matches gt1, wrong class
            ([40, 41, 50, 50], "B", 0.7),   # matches gt2, correct
            ([90, 90, 99, 99], "B", 0.6),   # false positive
        ]                                    # gt3 missed
        matched = metrics.update_detection_confusion(cm, self.CLASSES, gt, preds)
        self.assertEqual(sorted(matched), [("A", "A"), ("A", "B"), ("B", "B")])
        self.assertEqual(cm, [[1, 1, 0], [0, 1, 1], [0, 1, 0]])
        per = metrics.per_class_prf(cm, self.CLASSES)
        # A: TP1 FP0 FN1 -> P1 R.5 F1 2/3 ; B: TP1 FP2 FN1 -> P1/3 R1/2 F1 0.4
        self.assertAlmostEqual(per["A"]["precision"], 1.0)
        self.assertAlmostEqual(per["A"]["recall"], 0.5)
        self.assertAlmostEqual(per["A"]["f1"], 2 / 3)
        self.assertAlmostEqual(per["B"]["precision"], 1 / 3)
        self.assertAlmostEqual(per["B"]["recall"], 0.5)
        self.assertAlmostEqual(per["B"]["f1"], 0.4)
        summary = metrics.summarize(cm, self.CLASSES)
        self.assertAlmostEqual(summary["macro_f1"], (2 / 3 + 0.4) / 2)
        self.assertAlmostEqual(summary["harmonic_mean_f1"], 2 / (1.5 + 2.5), places=5)
        self.assertEqual(summary["labels"], ["A", "B", "Background"])
        loc = metrics.localization_summary(cm, self.CLASSES)
        self.assertEqual((loc["matched"], loc["missed_gt"], loc["false_positives"]), (3, 1, 1))
        self.assertIn("harmonic F1", metrics.format_table(summary))

    def test_matching_is_greedy_by_score_and_one_to_one(self):
        pairs, missed, extra = metrics.match_detections(
            [[0, 0, 10, 10]], [[0, 0, 10, 10], [0, 0, 10, 9]], [0.2, 0.9]
        )
        self.assertEqual((pairs, missed, extra), ([(0, 1)], [], [0]))
        self.assertAlmostEqual(metrics.iou([0, 0, 10, 10], [5, 0, 15, 10]), 1 / 3)
        self.assertEqual(metrics.iou([0, 0, 1, 1], [2, 2, 3, 3]), 0.0)

    def test_classification_confusion_and_harmonic_zero(self):
        cm = metrics.classification_confusion(["A", "A", "B"], ["A", "B", "B"], self.CLASSES)
        self.assertEqual(cm, [[1, 1], [0, 1]])
        self.assertAlmostEqual(metrics.accuracy(cm, self.CLASSES), 2 / 3)
        self.assertLess(metrics.harmonic_mean_f1([1.0, 0.0]), 1e-5)
        self.assertEqual(metrics.harmonic_mean_f1([]), 0.0)
        self.assertAlmostEqual(metrics.macro_f1([1.0, 0.0]), 0.5)

    def test_evaluate_detection_report(self):
        report = evaluate_detection(
            [([([0, 0, 10, 10], "Destroyed")], [([0, 0, 10, 10], "Destroyed", 0.9)]), ([], [])],
        )
        self.assertEqual(report["images"], 2)
        self.assertAlmostEqual(report["end_to_end"]["per_class"]["Destroyed"]["f1"], 1.0)
        self.assertEqual(report["matched_only"]["confusion_matrix"][3][3], 1)


# ---------------------------------------------------------------- fake Ultralytics objects


class FakeTensor:
    def __init__(self, values):
        self.values = values

    def cpu(self):
        return self

    def tolist(self):
        return list(self.values)

    def item(self):
        return self.values


class FakeBoxes:
    def __init__(self, rows):
        self.xyxy = FakeTensor([r[0] for r in rows])
        self.conf = FakeTensor([r[1] for r in rows])
        self.cls = FakeTensor([r[2] for r in rows])


class FakeDetectModel:
    task = "detect"

    def __init__(self, rows, names=None):
        self.rows, self.names = rows, names or {0: "building"}
        self.calls = []

    def predict(self, source, conf, **kwargs):
        self.calls.append(dict(kwargs, conf=conf))
        return [type("R", (), {"boxes": FakeBoxes([r for r in self.rows if r[1] >= conf])})()]


# Fake classifier: decides the class from the crop's dominant colour.
COLOR_CLASS = {(0, 200, 0): ("No Damage", 0.9), (250, 200, 0): ("Minor Damage", 0.4), (200, 0, 0): ("Destroyed", 0.8)}


class FakeProbs:
    def __init__(self, index, prob):
        self.top1 = index
        self.top1conf = FakeTensor(prob)
        data = [(1 - prob) / 3] * 4
        data[index] = prob
        self.data = FakeTensor(data)


class FakeClassifyModel:
    task = "classify"

    def __init__(self, names=None):
        # Ultralytics sorts class folders alphabetically.
        self.names = names or {0: "Destroyed", 1: "Major Damage", 2: "Minor Damage", 3: "No Damage"}
        self.batches = []

    def predict(self, source, **kwargs):
        self.batches.append(len(source))
        index_of = {v: k for k, v in self.names.items()}
        out = []
        for crop in source:
            color = crop.resize((1, 1), Image.NEAREST).getpixel((0, 0))
            color = min(COLOR_CLASS, key=lambda c: sum(abs(a - b) for a, b in zip(c, color)))
            label, prob = COLOR_CLASS[color]
            out.append(type("R", (), {"probs": FakeProbs(index_of[label], prob)})())
        return out


class TwoStageTests(unittest.TestCase):
    def scene(self):
        image = Image.new("RGB", (400, 300), (128, 128, 128))
        image.paste((0, 200, 0), (20, 20, 120, 120))
        image.paste((250, 200, 0), (150, 20, 250, 120))
        image.paste((200, 0, 0), (280, 150, 380, 280))
        return image

    def detector(self, **kwargs):
        rows = [
            ([30, 30, 110, 110], 0.95, 0),
            ([160, 30, 240, 110], 0.85, 0),
            ([290, 160, 370, 270], 0.6, 0),
            ([0, 0, 5, 5], 0.1, 0),  # below building_conf
        ]
        self.building, self.damage = FakeDetectModel(rows), FakeClassifyModel()
        return TwoStageDetector(_building_model=self.building, _damage_model=self.damage, **kwargs)

    def test_labels_confidence_and_filtering(self):
        detections = self.detector(confidence_threshold=0.5, batch=2).predict(self.scene())
        self.assertEqual([d["label"] for d in detections], ["No Damage", "Destroyed"])
        self.assertEqual(detections[0]["bbox"], [30, 30, 110, 110])
        self.assertAlmostEqual(detections[0]["confidence"], 0.9)
        self.assertAlmostEqual(detections[1]["building_confidence"], 0.6)
        self.assertAlmostEqual(detections[1]["class_probabilities"]["Destroyed"], 0.8)
        for d in detections:
            self.assertFalse(d["is_demo"])
            self.assertEqual(d["scope"], "detected_building")
            self.assertTrue(all(isinstance(v, int) for v in d["bbox"]))
        self.assertEqual(self.damage.batches, [2, 1])
        self.assertEqual(self.building.calls[0]["conf"], 0.25)

    def test_low_threshold_keeps_minor(self):
        labels = [d["label"] for d in self.detector(confidence_threshold=0.3).predict(self.scene())]
        self.assertEqual(labels, ["No Damage", "Minor Damage", "Destroyed"])

    def test_no_buildings(self):
        detector = TwoStageDetector(_building_model=FakeDetectModel([]), _damage_model=FakeClassifyModel())
        self.assertEqual(detector.predict(self.scene()), [])

    def test_rejects_wrong_models(self):
        with self.assertRaises(ValueError):
            TwoStageDetector(_building_model=FakeDetectModel([]),
                             _damage_model=FakeClassifyModel({0: "cat", 1: "dog", 2: "Destroyed", 3: "No Damage"}))
        with self.assertRaises(ValueError):
            TwoStageDetector(_building_model=FakeDetectModel([]),
                             _damage_model=FakeClassifyModel({0: "No Damage", 1: "Destroyed"}))
        wrong_task = FakeDetectModel([])
        with self.assertRaises(ValueError):
            TwoStageDetector(_building_model=wrong_task, _damage_model=wrong_task)
        with self.assertRaises(ValueError):  # COCO-style detector without a building class
            TwoStageDetector(_building_model=FakeDetectModel([], {0: "person", 1: "car"}),
                             _damage_model=FakeClassifyModel())
        with self.assertRaises(ValueError):
            TwoStageDetector(building_model_path="/nonexistent/building.pt", damage_model_path="/nonexistent/d.pt")

    def test_multiclass_building_model_filters_to_building_class(self):
        building = FakeDetectModel([([30, 30, 110, 110], 0.9, 1), ([160, 30, 240, 110], 0.9, 0)],
                                   {0: "person", 1: "building"})
        detector = TwoStageDetector(_building_model=building, _damage_model=FakeClassifyModel(), confidence_threshold=0)
        self.assertEqual([d["label"] for d in detector.predict(self.scene())], ["No Damage"])

    def test_crop_box_pads_and_clips(self):
        image = Image.new("RGB", (100, 100))
        self.assertEqual(crop_box(image, [40, 40, 60, 60], pad=0.2).size, (28, 28))
        self.assertEqual(crop_box(image, [40, 45, 60, 55], pad=0.0).size, (20, 20))
        self.assertEqual(crop_box(image, [0, 0, 20, 20], pad=0.5).size, (30, 30))


class TrainCliTests(unittest.TestCase):
    def test_kwargs_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "xbd_cls"
            data.mkdir()
            (data / "data.yaml").write_text("x")
            args = parse_args(["damage-cls", "--data", str(data / "data.yaml"), "--device", "cpu"])
            kwargs = build_train_kwargs(args)
            self.assertEqual(kwargs["data"], str(data.resolve()))
            self.assertEqual((kwargs["imgsz"], kwargs["name"], kwargs["device"]), (224, "xbd_damage_cls", "cpu"))
            meta = write_metadata(Path(tmp) / "m.json", name="m", task="classify", classes=list(CLASSES),
                                  data=str(data), epochs=1, imgsz=224, base_model="yolo11n-cls.pt")
            self.assertEqual(json.loads((Path(tmp) / "m.json").read_text())["classes"], meta["classes"])


if __name__ == "__main__":
    unittest.main()
