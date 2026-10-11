"""DJI drone telemetry (XMP, SRT) and flat-earth pixel-to-ground projection.

Projection assumes flat terrain at the takeoff elevation (``rel_alt`` below the
camera), a distortion-free pinhole camera and zero gimbal roll. Results are
estimates; accuracy degrades quickly at shallow pitch angles and over hills.
"""

from __future__ import annotations

import math
import re

from utils.geo_utils import valid_coordinate

EARTH_RADIUS_M = 6378137.0  # WGS84 equatorial radius
DEFAULT_HFOV_DEG = 73.7  # DJI Mavic-class wide camera (24 mm equivalent)
MAX_GROUND_DISTANCE_M = 5000.0

# XMP field name -> output key (DJI also ships the "GpsLongtitude" typo).
_XMP_FIELDS = {
    "GpsLatitude": "latitude",
    "Latitude": "latitude",
    "GpsLongitude": "longitude",
    "GpsLongtitude": "longitude",
    "Longitude": "longitude",
    "AbsoluteAltitude": "abs_alt",
    "RelativeAltitude": "rel_alt",
    "GimbalRollDegree": "gimbal_roll",
    "GimbalYawDegree": "gimbal_yaw",
    "GimbalPitchDegree": "gimbal_pitch",
    "FlightYawDegree": "flight_yaw",
}
_NUM = r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
_XMP_ATTR = re.compile(r'(?:drone-dji|drone):(\w+)\s*=\s*["\']\s*' + _NUM + r'\s*["\']')
_XMP_ELEM = re.compile(r"<(?:drone-dji|drone):(\w+)>\s*" + _NUM + r"\s*</(?:drone-dji|drone):\1>")
_XMP_FOCAL = re.compile(r'exif:FocalLengthIn35mmFilm\s*(?:=\s*["\']|>)\s*' + _NUM)


def read_dji_xmp(jpeg_bytes: bytes, focal_35mm: float | None = None) -> dict | None:
    """Extract DJI flight/gimbal fields from the XMP packet in raw JPEG bytes."""
    if not isinstance(jpeg_bytes, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(jpeg_bytes)
    start = raw.find(b"<x:xmpmeta")
    end = raw.find(b"</x:xmpmeta>", start + 1) if start >= 0 else -1
    if start < 0 or end < 0:
        return None
    text = raw[start:end].decode("utf-8", errors="ignore")
    out: dict = {}
    for pattern in (_XMP_ATTR, _XMP_ELEM):
        for name, value in pattern.findall(text):
            key = _XMP_FIELDS.get(name)
            if key and key not in out:
                number = float(value)
                if math.isfinite(number):
                    out[key] = number
    if not out:
        return None
    if "latitude" in out or "longitude" in out:
        if not valid_coordinate(out.get("latitude"), out.get("longitude")):
            out.pop("latitude", None), out.pop("longitude", None)
    match = _XMP_FOCAL.search(text)
    focal = focal_35mm if focal_35mm is not None else (float(match.group(1)) if match else None)
    if focal is not None and float(focal) > 0:
        out["focal_35mm"] = float(focal)
    return out


_SRT_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)
_SRT_KV = re.compile(r"([A-Za-z_]+)\s*:\s*" + _NUM)
_SRT_KEYS = {
    "latitude": "latitude",
    "longitude": "longitude",
    "longtitude": "longitude",
    "rel_alt": "rel_alt",
    "abs_alt": "abs_alt",
    "gb_pitch": "gimbal_pitch",
    "gb_yaw": "yaw",
    "gb_roll": "gimbal_roll",
    "barometer": "rel_alt",
}
_SRT_GPS = re.compile(r"GPS\s*\(\s*" + _NUM + r"\s*,\s*" + _NUM + r"(?:\s*,\s*" + _NUM + r")?\s*\)")
_SRT_HEIGHT = re.compile(r"(?<![A-Za-z])H\s+" + _NUM + r"\s*m")


def _seconds(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_dji_srt(text: str) -> list[dict]:
    """Parse DJI .SRT telemetry (bracketed ``[key: value]`` and legacy ``GPS(lon,lat,alt)``)."""
    if not isinstance(text, str):
        raise ValueError("SRT text must be a string")
    entries = []
    for block in re.split(r"\r?\n\s*\r?\n", text.replace("﻿", "")):
        timing = _SRT_TIME.search(block)
        if not timing:
            continue
        body = re.sub(r"<[^>]+>", " ", block[timing.end():])
        entry: dict = {
            "start_s": _seconds(*timing.groups()[:4]),
            "end_s": _seconds(*timing.groups()[4:]),
        }
        legacy_alt = None
        gps = _SRT_GPS.search(body)
        if gps:  # legacy order is (longitude, latitude[, altitude])
            entry["longitude"], entry["latitude"] = float(gps.group(1)), float(gps.group(2))
            legacy_alt = float(gps.group(3)) if gps.group(3) else None
            body = body[: gps.start()] + body[gps.end():]
        body = re.sub(r"HOME\s*\([^)]*\)", " ", body)
        for name, value in _SRT_KV.findall(body):
            key = _SRT_KEYS.get(name.lower())
            if key and key not in entry:
                entry[key] = float(value)
        if "rel_alt" not in entry:
            height = _SRT_HEIGHT.search(body)
            altitude = re.search(r"\baltitude\s*:\s*" + _NUM, body, re.I)
            if height:
                entry["rel_alt"] = float(height.group(1))
            elif altitude:
                entry["rel_alt"] = float(altitude.group(1))
            elif legacy_alt is not None:
                entry["rel_alt"] = legacy_alt
        if "latitude" in entry or "longitude" in entry:
            if not valid_coordinate(entry.get("latitude"), entry.get("longitude")):
                entry.pop("latitude", None), entry.pop("longitude", None)
        if len(entry) > 2:
            entries.append(entry)
    return entries


def telemetry_at(entries: list[dict], seconds: float) -> dict | None:
    """Entry whose [start_s, end_s) covers ``seconds``, else the nearest one."""
    if not entries:
        return None
    for entry in entries:
        if entry["start_s"] <= seconds < entry["end_s"]:
            return entry
    return min(
        entries,
        key=lambda e: min(abs(seconds - e["start_s"]), abs(seconds - e["end_s"])),
    )


def hfov_from_35mm(focal_35mm: float) -> float:
    """Horizontal field of view (degrees) from a 35 mm-equivalent focal length."""
    if not focal_35mm or focal_35mm <= 0:
        raise ValueError("focal_35mm must be positive")
    return math.degrees(2 * math.atan(36.0 / (2 * focal_35mm)))


def offset_to_latlon(lat: float, lon: float, east_m: float, north_m: float) -> tuple[float, float]:
    """Local tangent-plane offset (meters) to lat/lon."""
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlon = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat + dlat, (lon + dlon + 180) % 360 - 180


def project_pixel_to_ground(
    px, py, img_w, img_h, cam_lat, cam_lon, rel_alt_m, gimbal_pitch_deg, heading_deg,
    hfov_deg=DEFAULT_HFOV_DEG,
) -> tuple[float, float] | None:
    """Intersect the ray through pixel (px, py) with flat ground ``rel_alt_m`` below the camera.

    Pitch: DJI convention, -90 = nadir, 0 = horizon. Heading: degrees clockwise from
    true north (gimbal yaw). Returns None when the ray misses the ground or lands > 5 km.
    """
    try:
        values = [float(v) for v in (px, py, img_w, img_h, rel_alt_m, gimbal_pitch_deg, heading_deg, hfov_deg)]
    except (TypeError, ValueError):
        return None
    px, py, img_w, img_h, alt, pitch, heading, hfov = values
    if (
        not all(math.isfinite(v) for v in values)
        or not valid_coordinate(cam_lat, cam_lon)
        or img_w <= 0 or img_h <= 0 or alt <= 0 or not 0 < hfov < 180
    ):
        return None
    p, h = math.radians(pitch), math.radians(heading)
    forward = (math.sin(h) * math.cos(p), math.cos(h) * math.cos(p), math.sin(p))
    right = (math.cos(h), -math.sin(h), 0.0)
    up = (  # right x forward
        right[1] * forward[2] - right[2] * forward[1],
        right[2] * forward[0] - right[0] * forward[2],
        right[0] * forward[1] - right[1] * forward[0],
    )
    focal_px = (img_w / 2) / math.tan(math.radians(hfov) / 2)
    dx, dy = (px - img_w / 2) / focal_px, (py - img_h / 2) / focal_px
    ray = [forward[i] + dx * right[i] - dy * up[i] for i in range(3)]
    if ray[2] >= -1e-9:
        return None
    t = alt / -ray[2]
    east, north = t * ray[0], t * ray[1]
    if math.hypot(east, north) > MAX_GROUND_DISTANCE_M:
        return None
    return offset_to_latlon(float(cam_lat), float(cam_lon), east, north)


def _first(telemetry, *keys):
    for key in keys:
        if telemetry.get(key) is not None:
            return telemetry[key]
    return None


def project_bbox_center(bbox, img_w, img_h, telemetry: dict, hfov_deg=None):
    """Project a bbox center using DJI telemetry; returns (lat, lon, "telemetry_projection") or None.

    Needs latitude/longitude, rel_alt, gimbal_pitch and a heading (gimbal_yaw, yaw or
    flight_yaw). Pass the same image size the bbox was measured in.
    """
    if not telemetry or not bbox or len(bbox) != 4:
        return None
    pitch = _first(telemetry, "gimbal_pitch")
    heading = _first(telemetry, "gimbal_yaw", "yaw", "flight_yaw")
    alt = _first(telemetry, "rel_alt")
    if pitch is None or heading is None or alt is None:
        return None
    if hfov_deg is None:
        focal = telemetry.get("focal_35mm")
        hfov_deg = hfov_from_35mm(focal) if focal and focal > 0 else DEFAULT_HFOV_DEG
    try:
        cx, cy = (float(bbox[0]) + float(bbox[2])) / 2, (float(bbox[1]) + float(bbox[3])) / 2
    except (TypeError, ValueError):
        return None
    hit = project_pixel_to_ground(
        cx, cy, img_w, img_h, telemetry.get("latitude"), telemetry.get("longitude"),
        alt, pitch, heading, hfov_deg,
    )
    return (hit[0], hit[1], "telemetry_projection") if hit else None
