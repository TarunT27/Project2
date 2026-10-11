"""Field-visit ordering for damage observations.

LIMITATIONS (read before use):
* ``plan_route`` orders stops using straight-line (great-circle) distance only.
  It knows nothing about roads, bridges, debris, flooding, closures, or one-way
  streets. A "short" leg may be impassable. Treat the order as a review/visit
  suggestion, never as a safe or drivable route.
* Severity ordering prioritises visits; it does not confirm occupancy, safe
  access, or that a structure is actually damaged as classified.
* ``osrm_route`` is only used when an operator explicitly configures an OSRM
  server URL. Even then, OSRM uses its own map data, which will not reflect
  post-disaster blockages unless that data was updated.
"""

from __future__ import annotations

import math
import re

import httpx

from utils.mock_data import SEVERITY

EARTH_RADIUS_M = 6_371_008.8
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def haversine_m(a_lat, a_lon, b_lat, b_lon) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    p1, p2 = math.radians(float(a_lat)), math.radians(float(b_lat))
    dp = p2 - p1
    dl = math.radians(float(b_lon) - float(a_lon))
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def valid_point(lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
    )


def _dist(a, b) -> float:
    return haversine_m(a[0], a[1], b[0], b[1])


def _nearest_neighbour(current, stops):
    remaining = list(stops)
    ordered = []
    while remaining:
        if current is None:
            idx = 0
        else:
            # Ties broken by original (stable) position for determinism.
            idx = min(range(len(remaining)), key=lambda k: _dist(current, remaining[k]["_pt"]))
        nxt = remaining.pop(idx)
        ordered.append(nxt)
        current = nxt["_pt"]
    return ordered


def _two_opt(anchor, seq, max_passes=50):
    """Open-path 2-opt with an optional fixed anchor before seq[0].

    Only accepts strictly improving reversals, so the tier path length never
    increases.
    """
    pts = ([anchor] if anchor is not None else []) + [s["_pt"] for s in seq]
    off = 1 if anchor is not None else 0
    items = ([None] if anchor is not None else []) + list(seq)
    n = len(pts)
    if n - off < 2:
        return list(seq)

    def d(i, j):
        if i < 0 or j >= n:
            return 0.0
        return _dist(pts[i], pts[j])

    for _ in range(max_passes):
        improved = False
        for i in range(off, n - 1):
            for j in range(i + 1, n):
                before = d(i - 1, i) + d(j, j + 1)
                after = d(i - 1, j) + d(i, j + 1)
                if after + 1e-9 < before:
                    pts[i : j + 1] = pts[i : j + 1][::-1]
                    items[i : j + 1] = items[i : j + 1][::-1]
                    improved = True
        if not improved:
            break
    return items[off:]


def plan_route(start, stops, severity_weight=True, max_stops=200, improve=True) -> dict:
    """Order stops: worst severity tier first, nearest-neighbour + 2-opt within each tier.

    Straight-line only; ignores blocked roads (see module docstring).
    Stops lacking valid coordinates, or beyond ``max_stops`` (lowest priority
    dropped first), are listed in ``unrouted`` with reasons in ``skipped``.
    """
    unrouted, skipped, valid = [], [], []
    for stop in stops or []:
        if valid_point(stop.get("latitude"), stop.get("longitude")):
            valid.append(
                dict(stop, _pt=(float(stop["latitude"]), float(stop["longitude"])))
            )
        else:
            unrouted.append(stop.get("id"))
            skipped.append({"id": stop.get("id"), "reason": "missing or invalid coordinates"})

    def sev(stop):
        return SEVERITY.get(stop.get("damage_class"), -1) if severity_weight else 0

    if max_stops is not None and len(valid) > max_stops:
        ranked = sorted(valid, key=lambda s: -sev(s))  # stable
        for stop in ranked[max_stops:]:
            unrouted.append(stop.get("id"))
            skipped.append({"id": stop.get("id"), "reason": "max_stops exceeded"})
        keep = {id(s) for s in ranked[:max_stops]}
        valid = [s for s in valid if id(s) in keep]

    current = None
    if start is not None and valid_point(start[0], start[1]):
        current = (float(start[0]), float(start[1]))

    ordered = []
    for tier in sorted({sev(s) for s in valid}, reverse=True):
        tier_stops = [s for s in valid if sev(s) == tier]
        seq = _nearest_neighbour(current, tier_stops)
        if improve:
            seq = _two_opt(current, seq)
        ordered.extend(seq)
        current = seq[-1]["_pt"]

    legs, total = [], 0.0
    prev_label, prev_pt = "start", None
    if start is not None and valid_point(start[0], start[1]):
        prev_pt = (float(start[0]), float(start[1]))
    for stop in ordered:
        if prev_pt is not None:
            dist = _dist(prev_pt, stop["_pt"])
            legs.append({"from": prev_label, "to": stop.get("id"), "distance_m": round(dist, 1)})
            total += dist
        prev_label, prev_pt = stop.get("id"), stop["_pt"]

    return {
        "order": [s.get("id") for s in ordered],
        "legs": legs,
        "total_distance_m": round(total, 1),
        "method": "straight_line",
        "unrouted": unrouted,
        "skipped": skipped,
        "note": "Straight-line ordering; ignores roads, closures and debris. Verify access on the ground.",
    }


def osrm_route(points, base_url: str, profile="driving", timeout=20) -> dict:
    """Query a configured OSRM server for a road route through ``points`` [(lat, lon)].

    Returns {"distance_m", "duration_s", "geometry"} (GeoJSON LineString).
    Raises RuntimeError on any failure. Only call when OSRM is explicitly configured.
    """
    if not base_url or not str(base_url).lower().startswith(("http://", "https://")):
        raise RuntimeError("OSRM base_url must be an http(s) URL")
    if not _PROFILE_RE.match(str(profile)):
        raise RuntimeError("Invalid OSRM profile")
    pts = list(points or [])
    if len(pts) < 2:
        raise RuntimeError("OSRM route needs at least two points")
    for lat, lon in pts:
        if not valid_point(lat, lon):
            raise RuntimeError("OSRM route received an invalid coordinate")
    coords = ";".join(f"{float(lon):.6f},{float(lat):.6f}" for lat, lon in pts)
    url = f"{str(base_url).rstrip('/')}/route/v1/{profile}/{coords}"
    try:
        resp = httpx.get(
            url, params={"overview": "full", "geometries": "geojson"}, timeout=timeout
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # network, HTTP status, or JSON errors
        raise RuntimeError(f"OSRM request failed: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("code") != "Ok" or not data.get("routes"):
        code = data.get("code") if isinstance(data, dict) else None
        raise RuntimeError(f"OSRM returned no route (code={code!r})")
    route = data["routes"][0]
    geometry = route.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        raise RuntimeError("OSRM response missing GeoJSON LineString geometry")
    try:
        return {
            "distance_m": float(route["distance"]),
            "duration_s": float(route["duration"]),
            "geometry": geometry,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("OSRM response missing distance/duration") from exc
