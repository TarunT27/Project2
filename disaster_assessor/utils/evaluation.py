"""Inspect detection quality using one-to-one IoU matching, with no readiness claims."""
import math
from .mock_data import CLASSES


def iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area_a, area_b = (a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def validate_regions(regions):
    for item in regions:
        if item.get("label") not in CLASSES:
            raise ValueError("Every annotation and prediction must use a supported damage label.")
        box = item.get("bbox")
        if not isinstance(box, (list, tuple)) or len(box) != 4 or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in box):
            raise ValueError("Use finite pixel-space xyxy bounding boxes.")
        if min(box) < 0 or box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("Bounding boxes must have positive width and height.")
        if item.get("scope") == "whole_image_classification":
            raise ValueError("Whole-image classification is not localized building detection.")


def wilson(success, total):
    if not total:
        return None
    z, p = 1.96, success / total
    denominator = 1 + z * z / total
    middle = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [max(0, middle - radius), min(1, middle + radius)]


def evaluate(samples, match_iou=0.5):
    if not samples or not 0 < match_iou <= 1:
        raise ValueError("Supply at least one labeled image and a valid IoU threshold.")
    counts = {name: {"true_positive": 0, "false_positive": 0, "false_negative": 0, "support": 0} for name in CLASSES}
    confusion = {name: {p: 0 for p in (*CLASSES, "Missed")} for name in CLASSES}
    missed_damage = high_missed = damage_support = high_support = unmatched = false_alarms = 0
    details = []
    for sample in samples:
        truth, predictions = sample["annotations"], sample["predictions"]
        validate_regions(truth)
        validate_regions(predictions)
        candidates = sorted(((iou(t["bbox"], p["bbox"]), ti, pi) for ti, t in enumerate(truth) for pi, p in enumerate(predictions)), key=lambda x: (-x[0], x[1], x[2]))
        matches, used = {}, set()
        for overlap, ti, pi in candidates:
            if overlap >= match_iou and ti not in matches and pi not in used:
                matches[ti], used = pi, used | {pi}
        sample_missed = 0
        for ti, target in enumerate(truth):
            label = target["label"]
            counts[label]["support"] += 1
            pi = matches.get(ti)
            predicted = predictions[pi]["label"] if pi is not None else "Missed"
            confusion[label][predicted] += 1
            if predicted == label:
                counts[label]["true_positive"] += 1
            else:
                counts[label]["false_negative"] += 1
                if pi is not None:
                    counts[predicted]["false_positive"] += 1
            if pi is None:
                unmatched += 1
                sample_missed += 1
            if label != "No Damage":
                damage_support += 1
                missed_damage += predicted in {"Missed", "No Damage"}
            if label in {"Major Damage", "Destroyed"}:
                high_support += 1
                high_missed += predicted not in {"Major Damage", "Destroyed"}
        for pi, prediction in enumerate(predictions):
            if pi not in used:
                counts[prediction["label"]]["false_positive"] += 1
                false_alarms += 1
        details.append({"image": sample.get("image", ""), "labeled_regions": len(truth), "predicted_regions": len(predictions), "unmatched_regions": sample_missed})
    for count in counts.values():
        tp, fp, fn = (count[k] for k in ("true_positive", "false_positive", "false_negative"))
        count["precision"] = tp / (tp + fp) if tp + fp else None
        count["recall"] = tp / (tp + fn) if tp + fn else None
        count["recall_95pct_interval"] = wilson(tp, tp + fn)
    return {"schema_version": 1, "kind": "damage_detection_evaluation", "image_count": len(samples), "match_iou": match_iou,
            "per_class": counts, "confusion_matrix": confusion, "unmatched_regions": unmatched,
            "unmatched_predictions": false_alarms, "damaged_regions": damage_support, "missed_damage": missed_damage,
            "high_priority_regions": high_support, "missed_high_priority": high_missed,
            "high_priority_recall": (high_support - high_missed) / high_support if high_support else None,
            "high_priority_recall_95pct_interval": wilson(high_support - high_missed, high_support), "images": details,
            "limitations": "IoU matching at one confidence threshold; this is not mAP or emergency-readiness certification. Split independence, annotation quality and domain coverage require separate review."}


def validate_report(report):
    if not isinstance(report, dict) or report.get("kind") != "damage_detection_evaluation" or report.get("schema_version") != 1:
        raise ValueError("Use a report generated by evaluate_model.py.")
    for key in ("image_count", "missed_damage", "missed_high_priority"):
        if not isinstance(report.get(key), int) or isinstance(report[key], bool) or report[key] < 0:
            raise ValueError("Report counts must be nonnegative integers.")
    if report["image_count"] < 1:
        raise ValueError("Report needs at least one evaluated image.")
    if not isinstance(report.get("per_class"), dict) or not isinstance(report.get("confusion_matrix"), dict):
        raise ValueError("Report must contain class metrics and a confusion matrix.")
    for label in CLASSES:
        count = report.get("per_class", {}).get(label)
        matrix = report.get("confusion_matrix", {}).get(label)
        if not isinstance(count, dict) or not isinstance(matrix, dict):
            raise ValueError("Report must include every damage class.")
        for metric in ("precision", "recall"):
            value = count.get(metric)
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError("Precision and recall must be between zero and one.")
    if not isinstance(report.get("limitations"), str):
        raise ValueError("Report must describe its evaluation limits.")
    return report
