"""Greedy cross-frame association of detections from sampled video frames."""

from __future__ import annotations

from utils.geo_utils import valid_coordinate
from utils.mock_data import SEVERITY

from .footprints import haversine_m

GEO_VETO_FACTOR = 4  # coords farther apart than this x geo_threshold_m never link


def iou(a, b) -> float:
    """Intersection-over-union of two [x1, y1, x2, y2] boxes (0 for invalid boxes)."""
    try:
        ax1, ay1, ax2, ay2 = (float(v) for v in a)
        bx1, by1, bx2, by2 = (float(v) for v in b)
    except (TypeError, ValueError):
        return 0.0
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    union = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1) + max(0.0, bx2 - bx1) * max(0.0, by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def _coords(det):
    lat, lon = det.get("latitude"), det.get("longitude")
    return (float(lat), float(lon)) if valid_coordinate(lat, lon) else None


def _score(prev, cur, iou_threshold, geo_threshold_m):
    overlap = iou(prev.get("bbox"), cur.get("bbox"))
    score = overlap if overlap >= iou_threshold else 0.0
    p, c = _coords(prev), _coords(cur)
    if p and c and geo_threshold_m > 0:
        d = haversine_m(*p, *c)
        if d > GEO_VETO_FACTOR * geo_threshold_m:
            return 0.0  # overlapping boxes but clearly different places
        if d <= geo_threshold_m:
            score = max(score, 1.0 - d / geo_threshold_m, 1e-6)
    return score


def associate_tracks(frames: list[list[dict]], iou_threshold=0.3, geo_threshold_m=15) -> list[list[int]]:
    """Track ids shaped like ``frames``; links to the previous frame by IoU or geo distance.

    Pairs are taken greedily by score (IoU, or 1 - distance/threshold). Detections whose
    coordinates are > 4x geo_threshold_m apart are never linked, even if boxes overlap.
    """
    next_id, previous, out = 0, [], []
    for detections in frames:
        detections = detections or []
        pairs = sorted(
            (
                (-s, i, j)
                for i, prev in enumerate(previous)
                for j, cur in enumerate(detections)
                if (s := _score(prev[1], cur, iou_threshold, geo_threshold_m)) > 0
            )
        )
        ids: list[int | None] = [None] * len(detections)
        used = set()
        for _, i, j in pairs:
            if i not in used and ids[j] is None:
                used.add(i)
                ids[j] = previous[i][0]
        for j, track in enumerate(ids):
            if track is None:
                ids[j], next_id = next_id, next_id + 1
        out.append(ids)
        previous = list(zip(ids, detections))
    return out


def summarize_tracks(frames: list[list[dict]], track_ids: list[list[int]]) -> list[dict]:
    """One row per track: most severe class, its max confidence, mean location, members."""
    tracks: dict[int, dict] = {}
    for f, (detections, ids) in enumerate(zip(frames, track_ids)):
        for d, (det, tid) in enumerate(zip(detections or [], ids)):
            row = tracks.setdefault(
                tid,
                {"track_id": tid, "damage_class": None, "confidence": None, "count": 0,
                 "detections": [], "_coords": []},
            )
            row["count"] += 1
            row["detections"].append((f, d))
            if (c := _coords(det)) is not None:
                row["_coords"].append(c)
            cls = det.get("damage_class", det.get("label"))
            conf = det.get("confidence")
            conf = float(conf) if conf is not None else None
            if row["count"] == 1 or SEVERITY.get(cls, -1) > SEVERITY.get(row["damage_class"], -1):
                row["damage_class"], row["confidence"] = cls, conf
            elif cls == row["damage_class"] and conf is not None:
                row["confidence"] = conf if row["confidence"] is None else max(row["confidence"], conf)
    result = []
    for tid in sorted(tracks):
        row = tracks[tid]
        coords = row.pop("_coords")
        row["latitude"] = sum(c[0] for c in coords) / len(coords) if coords else None
        row["longitude"] = sum(c[1] for c in coords) / len(coords) if coords else None
        result.append(row)
    return result
