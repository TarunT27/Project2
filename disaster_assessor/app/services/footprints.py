"""Building footprints (GeoJSON / OpenStreetMap Overpass), point matching and dedupe."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import httpx

from utils.geo_utils import valid_coordinate
from utils.mock_data import SEVERITY

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "disaster-assessor/1.0 (post-disaster building damage assessment; footprint lookup)"
MAX_BBOX_DEG = 0.05
GRID_THRESHOLD = 500
GRID_CELL_DEG = 0.001
EARTH_RADIUS_M = 6371008.8  # mean radius for haversine


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _ring_centroid(ring):
    """Area-weighted centroid (lon, lat) and absolute area of a ring."""
    ox, oy = ring[0]  # shift to a local origin to avoid float cancellation
    local = [(x - ox, y - oy) for x, y in ring]
    area = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(local, local[1:] + local[:1]):
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-20:
        return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)), 0.0
    return (ox + cx / (3 * area), oy + cy / (3 * area)), abs(area) / 2


def _footprint(fid, rings, properties):
    rings = [r for r in rings if len(r) >= 3]
    if not rings:
        return None
    points = [p for r in rings for p in r]
    centroids = [_ring_centroid(r) for r in rings]
    total = sum(a for _, a in centroids)
    if total > 0:
        lon = sum(c[0] * a for c, a in centroids) / total
        lat = sum(c[1] * a for c, a in centroids) / total
    else:
        lon, lat = centroids[0][0]
    return {
        "id": fid,
        "polygon": rings,
        "bbox": (
            min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points),
        ),
        "centroid": (lat, lon),
        "properties": properties or {},
    }


def _ring(coords):
    ring = [(float(c[0]), float(c[1])) for c in coords if len(c) >= 2]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if not all(valid_coordinate(lat, lon) for lon, lat in ring):
        raise ValueError("Footprint coordinates must be [lon, lat] in WGS84")
    return ring


def load_footprints_geojson(data: bytes | str | dict) -> list[dict]:
    """Parse a GeoJSON FeatureCollection of Polygon/MultiPolygon buildings (holes ignored)."""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8-sig")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid GeoJSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("GeoJSON must be an object")
    if data.get("type") == "Feature":
        features = [data]
    elif data.get("type") == "FeatureCollection":
        features = data.get("features") or []
    else:
        raise ValueError("GeoJSON must be a FeatureCollection or Feature")
    footprints = []
    for index, feature in enumerate(features):
        geometry = (feature or {}).get("geometry") or {}
        coords = geometry.get("coordinates") or []
        try:
            if geometry.get("type") == "Polygon":
                rings = [_ring(coords[0])] if coords else []
            elif geometry.get("type") == "MultiPolygon":
                rings = [_ring(poly[0]) for poly in coords if poly]
            else:
                continue
        except (TypeError, IndexError) as exc:
            raise ValueError(f"Malformed geometry in feature {index}: {exc}") from exc
        props = feature.get("properties") or {}
        fid = feature.get("id") or props.get("id") or props.get("@id") or f"fp-{index}"
        footprint = _footprint(str(fid), rings, props)
        if footprint:
            footprints.append(footprint)
    return footprints


def _overpass_footprints(payload: dict) -> list[dict]:
    footprints = []
    for element in payload.get("elements") or []:
        kind, tags = element.get("type"), element.get("tags") or {}
        if kind == "way":
            geoms = [element.get("geometry") or []]
        elif kind == "relation":  # closed outer member ways only
            geoms = [
                m.get("geometry") or [] for m in element.get("members") or []
                if m.get("role") == "outer" and m.get("geometry")
                and m["geometry"][0] == m["geometry"][-1]
            ]
        else:
            continue
        try:
            rings = [_ring([(p["lon"], p["lat"]) for p in g]) for g in geoms if g]
        except (KeyError, TypeError, ValueError):
            continue
        footprint = _footprint(f"osm-{kind}-{element.get('id')}", rings, tags)
        if footprint:
            footprints.append(footprint)
    return footprints


def fetch_osm_buildings(south, west, north, east, timeout=25, cache_dir=None) -> list[dict]:
    """Fetch OSM building footprints for a small bbox (<= 0.05 deg per side) via Overpass."""
    try:
        south, west, north, east = (float(v) for v in (south, west, north, east))
    except (TypeError, ValueError) as exc:
        raise ValueError("Bounding box must be numeric") from exc
    if not (valid_coordinate(south, west) and valid_coordinate(north, east)):
        raise ValueError("Bounding box is outside WGS84 range")
    if south >= north or west >= east:
        raise ValueError("Bounding box must satisfy south < north and west < east")
    if north - south > MAX_BBOX_DEG or east - west > MAX_BBOX_DEG:
        raise ValueError(f"Bounding box larger than {MAX_BBOX_DEG} deg; split it into tiles")
    south, west = math.floor(south * 1e4) / 1e4, math.floor(west * 1e4) / 1e4
    north, east = math.ceil(north * 1e4) / 1e4, math.ceil(east * 1e4) / 1e4
    cache_path = None
    if cache_dir:
        cache_path = Path(cache_dir) / f"overpass_{south:.4f}_{west:.4f}_{north:.4f}_{east:.4f}.json"
        if cache_path.is_file():
            try:
                return _overpass_footprints(json.loads(cache_path.read_text("utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
    box = f"{south},{west},{north},{east}"
    query = (
        f"[out:json][timeout:{int(timeout)}];"
        f'(way["building"]({box});relation["building"]["type"="multipolygon"]({box}););'
        "out geom;"
    )
    try:
        response = httpx.post(
            OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=timeout
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"Overpass request failed: {exc}") from exc
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), "utf-8")
        os.replace(tmp, cache_path)
    return _overpass_footprints(payload)


def point_in_polygon(lat: float, lon: float, ring) -> bool:
    """Ray-casting test; ``ring`` is a list of (lon, lat) vertices."""
    inside = False
    n = len(ring)
    for i in range(n):
        (x0, y0), (x1, y1) = ring[i], ring[(i + 1) % n]
        if (y0 > lat) != (y1 > lat) and lon < x0 + (lat - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _contains(footprint, lat, lon):
    minx, miny, maxx, maxy = footprint["bbox"]
    return minx <= lon <= maxx and miny <= lat <= maxy and any(
        point_in_polygon(lat, lon, r) for r in footprint["polygon"]
    )


class FootprintIndex:
    """Uniform lat/lon grid over footprint bboxes for fast candidate lookup."""

    def __init__(self, footprints: list[dict], cell_deg: float = GRID_CELL_DEG):
        self.footprints, self.cell = footprints, cell_deg
        self.grid: dict[tuple[int, int], list[int]] = {}
        for i, fp in enumerate(footprints):
            minx, miny, maxx, maxy = fp["bbox"]
            for gx in range(self._k(minx), self._k(maxx) + 1):
                for gy in range(self._k(miny), self._k(maxy) + 1):
                    self.grid.setdefault((gx, gy), []).append(i)

    def _k(self, value):
        return math.floor(value / self.cell)

    def candidates(self, lat, lon, radius_m=0.0) -> list[dict]:
        dlat = radius_m / 111320.0
        dlon = radius_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-6))
        found = set()
        for gx in range(self._k(lon - dlon), self._k(lon + dlon) + 1):
            for gy in range(self._k(lat - dlat), self._k(lat + dlat) + 1):
                found.update(self.grid.get((gx, gy), ()))
        return [self.footprints[i] for i in sorted(found)]


def match_point(lat, lon, footprints, max_distance_m=25) -> dict | None:
    """Footprint containing the point, else nearest centroid within max_distance_m.

    ``footprints`` may be a list or a prebuilt FootprintIndex.
    """
    if not valid_coordinate(lat, lon) or not footprints:
        return None
    lat, lon = float(lat), float(lon)
    if isinstance(footprints, list) and len(footprints) > GRID_THRESHOLD:
        footprints = FootprintIndex(footprints)
    if isinstance(footprints, FootprintIndex):
        candidates = footprints.candidates(lat, lon, max_distance_m)
    else:
        candidates = footprints
    for fp in candidates:
        if _contains(fp, lat, lon):
            return fp
    best, best_d = None, float(max_distance_m)
    for fp in candidates:
        d = haversine_m(lat, lon, *fp["centroid"])
        if d <= best_d:
            best, best_d = fp, d
    return best


def assign_footprints(observations: list[dict], footprints, max_distance_m=25) -> list[dict]:
    """Copies of observations with ``footprint_id`` set (None when unmatched)."""
    index = footprints
    if isinstance(footprints, list) and len(footprints) > GRID_THRESHOLD:
        index = FootprintIndex(footprints)
    out = []
    for obs in observations:
        fp = match_point(obs.get("latitude"), obs.get("longitude"), index, max_distance_m)
        out.append({**obs, "footprint_id": fp["id"] if fp else None})
    return out


def _obs_id(obs, index):
    return obs.get("id", obs.get("observation_id", index))


def dedupe_observations(observations: list[dict], footprints, max_distance_m=25) -> list[dict]:
    """Group observations per building; severity is the worst class seen."""
    by_id = {fp["id"]: fp for fp in (footprints.footprints if isinstance(footprints, FootprintIndex) else footprints or [])}
    groups: dict = {}
    for index, obs in enumerate(assign_footprints(observations, footprints or [], max_distance_m)):
        fid = obs["footprint_id"]
        key = fid if fid is not None else ("unmatched", index)
        if key not in groups:
            fp = by_id.get(fid)
            location = (
                fp["centroid"] if fp
                else (float(obs["latitude"]), float(obs["longitude"]))
                if valid_coordinate(obs.get("latitude"), obs.get("longitude")) else None
            )
            groups[key] = {
                "footprint_id": fid,
                "centroid": location,
                "polygon": fp["polygon"] if fp else None,
                "observation_ids": [],
                "damage_class": None,
                "confidence": None,
                "count": 0,
            }
        group = groups[key]
        group["observation_ids"].append(_obs_id(obs, index))
        group["count"] += 1
        cls, conf = obs.get("damage_class"), obs.get("confidence")
        conf = float(conf) if conf is not None else None
        current = SEVERITY.get(group["damage_class"], -1)
        if group["count"] == 1 or SEVERITY.get(cls, -1) > current:
            group["damage_class"], group["confidence"] = cls, conf
        elif cls == group["damage_class"] and conf is not None:
            group["confidence"] = conf if group["confidence"] is None else max(group["confidence"], conf)
    return list(groups.values())
