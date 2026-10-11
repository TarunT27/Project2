"""Pure-Python damage-assessment metrics (no torch / numpy needed, fully unit-testable).

Conventions
-----------
* ``classes`` is an ordered list of K damage class names.
* Detection confusion matrices are (K+1) x (K+1): rows = ground truth, columns =
  prediction, and index K is "background":
    - ``cm[g][K]``  = ground-truth building of class g that no prediction matched (missed)
    - ``cm[K][p]``  = prediction of class p that matched no ground truth (false positive)
    - ``cm[K][K]``  is always 0.
* Classification-only matrices (matched pairs, or crop classification) are K x K.
"""

from __future__ import annotations

BACKGROUND = "Background"


def iou(a, b):
    """Intersection-over-union of two [x1, y1, x2, y2] boxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def match_detections(gt_boxes, pred_boxes, pred_scores=None, iou_threshold=0.5):
    """Greedy one-to-one matching, highest-scoring prediction first.

    Each prediction is matched to the unmatched ground-truth box with the highest IoU,
    provided IoU >= ``iou_threshold``. Matching ignores class labels on purpose: the
    question is "was this building found?", class agreement is scored afterwards.

    Returns (pairs, unmatched_gt, unmatched_pred) where pairs is a list of
    (gt_index, pred_index).
    """
    if pred_scores is None:
        pred_scores = [1.0] * len(pred_boxes)
    order = sorted(range(len(pred_boxes)), key=lambda i: (-pred_scores[i], i))
    taken = set()
    pairs = []
    for p in order:
        best, best_iou = None, iou_threshold
        for g, gbox in enumerate(gt_boxes):
            if g in taken:
                continue
            value = iou(gbox, pred_boxes[p])
            if value >= best_iou:
                best, best_iou = g, value
        if best is not None:
            taken.add(best)
            pairs.append((best, p))
    matched_pred = {p for _, p in pairs}
    unmatched_gt = [g for g in range(len(gt_boxes)) if g not in taken]
    unmatched_pred = [p for p in range(len(pred_boxes)) if p not in matched_pred]
    return sorted(pairs), unmatched_gt, unmatched_pred


def empty_matrix(size):
    return [[0] * size for _ in range(size)]


def update_detection_confusion(cm, classes, gt, preds, iou_threshold=0.5):
    """Accumulate one image into a (K+1)x(K+1) detection confusion matrix.

    gt:    list of (box, class_name)
    preds: list of (box, class_name, score)
    Returns the list of matched (gt_class, pred_class) pairs for this image.
    """
    k = len(classes)
    index = {name: i for i, name in enumerate(classes)}
    pairs, missed, extra = match_detections(
        [b for b, _ in gt],
        [b for b, _, _ in preds],
        [s for _, _, s in preds],
        iou_threshold,
    )
    matched = []
    for g, p in pairs:
        cm[index[gt[g][1]]][index[preds[p][1]]] += 1
        matched.append((gt[g][1], preds[p][1]))
    for g in missed:
        cm[index[gt[g][1]]][k] += 1
    for p in extra:
        cm[k][index[preds[p][1]]] += 1
    return matched


def classification_confusion(y_true, y_pred, classes):
    """K x K confusion matrix from parallel label lists (rows = truth)."""
    index = {name: i for i, name in enumerate(classes)}
    cm = empty_matrix(len(classes))
    for t, p in zip(y_true, y_pred):
        cm[index[t]][index[p]] += 1
    return cm


def _safe_div(a, b):
    return a / b if b else 0.0


def per_class_prf(cm, classes):
    """Precision / recall / F1 / support for each of the first K rows/cols of ``cm``.

    Works for both K x K and (K+1) x (K+1) matrices. In the detection case the
    background column counts as a miss (FN) and the background row as an FP, because
    rows/columns are summed over the whole matrix.
    """
    k = len(classes)
    size = len(cm)
    out = {}
    for c in range(k):
        tp = cm[c][c]
        predicted = sum(cm[r][c] for r in range(size))
        actual = sum(cm[c][q] for q in range(size))
        precision = _safe_div(tp, predicted)
        recall = _safe_div(tp, actual)
        f1 = _safe_div(2 * precision * recall, precision + recall)
        out[classes[c]] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": actual,
            "tp": tp,
            "fp": predicted - tp,
            "fn": actual - tp,
        }
    return out


def macro_f1(f1_scores):
    f1_scores = list(f1_scores)
    return sum(f1_scores) / len(f1_scores) if f1_scores else 0.0


def harmonic_mean_f1(f1_scores, eps=1e-6):
    """xView2-style damage score: harmonic mean of per-class F1.

    Mirrors the official scorer (n / sum(1 / (f1 + eps))), so a single class with
    F1 = 0 drives the score to ~0 -- rare classes cannot be ignored.
    """
    f1_scores = list(f1_scores)
    if not f1_scores:
        return 0.0
    return len(f1_scores) / sum(1.0 / (f + eps) for f in f1_scores)


def accuracy(cm, classes):
    k = len(classes)
    total = sum(cm[r][c] for r in range(k) for c in range(k))
    return _safe_div(sum(cm[i][i] for i in range(k)), total)


def summarize(cm, classes):
    """Report dict for a K x K or (K+1) x (K+1) confusion matrix."""
    per_class = per_class_prf(cm, classes)
    f1s = [per_class[c]["f1"] for c in classes]
    labels = list(classes) + ([BACKGROUND] if len(cm) == len(classes) + 1 else [])
    return {
        "labels": labels,
        "confusion_matrix": cm,
        "per_class": per_class,
        "macro_f1": macro_f1(f1s),
        "harmonic_mean_f1": harmonic_mean_f1(f1s),
        "accuracy_on_matched": accuracy(cm, classes),
    }


def localization_summary(cm, classes):
    """Building-level detection P/R/F1 at the IoU threshold, ignoring damage class."""
    k = len(classes)
    matched = sum(cm[r][c] for r in range(k) for c in range(k))
    missed = sum(cm[r][k] for r in range(k))
    extra = sum(cm[k][c] for c in range(k))
    precision = _safe_div(matched, matched + extra)
    recall = _safe_div(matched, matched + missed)
    return {
        "matched": matched,
        "missed_gt": missed,
        "false_positives": extra,
        "precision": precision,
        "recall": recall,
        "f1": _safe_div(2 * precision * recall, precision + recall),
    }


def format_table(summary):
    """Plain-text per-class table plus the confusion matrix."""
    lines = [f"{'class':<14}{'prec':>8}{'recall':>8}{'f1':>8}{'support':>9}"]
    for name, row in summary["per_class"].items():
        lines.append(
            f"{name:<14}{row['precision']:>8.3f}{row['recall']:>8.3f}{row['f1']:>8.3f}{row['support']:>9d}"
        )
    lines.append(f"{'macro F1':<14}{summary['macro_f1']:>24.3f}")
    lines.append(f"{'harmonic F1':<14}{summary['harmonic_mean_f1']:>24.3f}")
    labels = summary["labels"]
    short = [label.replace(" Damage", "")[:10] for label in labels]
    lines.append("")
    lines.append("confusion (rows = truth, cols = prediction)")
    lines.append(" " * 12 + "".join(f"{s:>11}" for s in short))
    for s, row in zip(short, summary["confusion_matrix"]):
        lines.append(f"{s:<12}" + "".join(f"{v:>11d}" for v in row))
    return "\n".join(lines)
