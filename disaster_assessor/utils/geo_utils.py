"""EXIF capture locations, explicitly labeled demo fallbacks, and Folium mapping."""

import base64
import hashlib
import html
import io
import math
import random

import folium
from folium.plugins import MarkerCluster
from PIL import ExifTags
import piexif

from .mock_data import COLORS

DEFAULT_CENTER = (37.7749, -122.4194)


def _decimal(values, ref):
    def number(value):
        return (
            float(value[0]) / float(value[1])
            if isinstance(value, (tuple, list))
            else float(value)
        )

    degrees, minutes, seconds = [number(v) for v in values]
    if degrees < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("Invalid GPS DMS values")
    ref = ref.decode() if isinstance(ref, bytes) else str(ref)
    if ref.upper() not in {"N", "S", "E", "W"}:
        raise ValueError("Invalid GPS hemisphere")
    return (degrees + minutes / 60 + seconds / 3600) * (
        -1 if ref.upper() in {"S", "W"} else 1
    )


def valid_coordinate(lat, lon):
    try:
        return (
            math.isfinite(float(lat))
            and math.isfinite(float(lon))
            and -90 <= float(lat) <= 90
            and -180 <= float(lon) <= 180
        )
    except (TypeError, ValueError):
        return False


def extract_gps_from_exif(image, *, demo_fallback=True, center=DEFAULT_CENTER, seed=""):
    """Return lat/lon/timestamp AND provenance. Real mode must set demo_fallback=False.

    EXIF coordinates represent camera capture location, not a georeferenced building.
    A deterministic random offset is supplied only for explicitly labeled demos.
    """
    timestamp, gps = None, {}
    try:
        exif = image.getexif()
        timestamp = exif.get(306)
        try:
            timestamp = exif.get_ifd(ExifTags.IFD.Exif).get(36867, timestamp)
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        if not gps and image.info.get("exif"):
            data = piexif.load(image.info["exif"])
            gps = data.get("GPS", {})
            timestamp = data.get("Exif", {}).get(
                piexif.ExifIFD.DateTimeOriginal, timestamp
            )
        if isinstance(timestamp, bytes):
            timestamp = timestamp.decode("utf-8", errors="replace")
        lat, lon = _decimal(gps[2], gps[1]), _decimal(gps[4], gps[3])
        if not valid_coordinate(lat, lon):
            raise ValueError("GPS is out of range")
        return {
            "latitude": lat,
            "longitude": lon,
            "timestamp": str(timestamp) if timestamp else None,
            "location_source": "exif_camera",
            "location_is_synthetic": False,
        }
    except (
        KeyError,
        ValueError,
        TypeError,
        AttributeError,
        ZeroDivisionError,
        OSError,
    ):
        if demo_fallback:
            if not valid_coordinate(*center):
                raise ValueError("Invalid demo center")
            rng = random.Random(hashlib.sha256(str(seed).encode()).digest())
            lat = max(-90, min(90, center[0] + rng.uniform(-0.008, 0.008)))
            lon = max(-180, min(180, center[1] + rng.uniform(-0.008, 0.008)))
            return {
                "latitude": lat,
                "longitude": lon,
                "timestamp": str(timestamp) if timestamp else None,
                "location_source": "synthetic_demo",
                "location_is_synthetic": True,
            }
        return {
            "latitude": None,
            "longitude": None,
            "timestamp": str(timestamp) if timestamp else None,
            "location_source": "missing",
            "location_is_synthetic": False,
        }


def thumbnail_data_url(image, size=(280, 180)):
    thumb = image.copy().convert("RGB")
    thumb.thumbnail(size)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def generate_damage_map(df_results, center=DEFAULT_CENTER, *, show_tiles=True):
    """One clustered marker per observation; overlapping detections remain inspectable."""
    rows = [
        r
        for r in df_results.to_dict("records")
        if valid_coordinate(r.get("latitude"), r.get("longitude"))
    ]
    position = (
        [sum(float(r[k]) for r in rows) / len(rows) for k in ("latitude", "longitude")]
        if rows
        else list(center)
    )
    # fit_bounds runs while Streamlit's inactive tabs have zero width, which can
    # incorrectly select world zoom. Choose the initial zoom before rendering.
    span = (
        max(
            (
                max(float(r[k]) for r in rows) - min(float(r[k]) for r in rows)
                for k in ("latitude", "longitude")
            ),
            default=0,
        )
        if rows
        else 0
    )
    zoom = max(2, min(16, int(math.log2(360 / max(span, 0.002))) - 2))
    result = folium.Map(
        location=position,
        zoom_start=zoom,
        tiles="OpenStreetMap" if show_tiles else None,
        control_scale=True,
    )
    cluster = MarkerCluster(name="Damage observations").add_to(result)
    for row in rows:
        label = str(row["damage_class"])
        preview = row.get("preview", "")
        # Only locally generated image data is accepted; never interpolate arbitrary URLs.
        img = (
            f'<img src="{preview}" width="240" alt="Observation preview">'
            if isinstance(preview, str)
            and preview.startswith("data:image/jpeg;base64,")
            and all(c.isalnum() or c in "+/=" for c in preview.split(",", 1)[1])
            else ""
        )
        confidence = float(row["confidence"])
        tag = "SYNTHETIC DEMO" if row.get("is_demo") else "UNVERIFIED MODEL OUTPUT"
        content = f'<div style="width:260px;font-family:sans-serif"><b>{html.escape(str(row.get("source_file", "Image")))}</b><p>{tag}</p>{img}<h4>{html.escape(label)}</h4><p>Model score: {confidence:.1%}<br>Location: {html.escape(str(row.get("location_source", "unknown")))}<br>Capture time: {html.escape(str(row.get("timestamp") or "unknown"))}</p><small>Observation point, not a verified building location.</small></div>'
        folium.CircleMarker(
            location=[row["latitude"], row["longitude"]],
            radius=10,
            color=COLORS[label],
            fill=True,
            fill_color=COLORS[label],
            fill_opacity=0.9,
            tooltip=f"{label} · {confidence:.0%} · {tag} · Observation {html.escape(str(row.get("observation_id", "unknown")))}",
            popup=folium.Popup(content, max_width=300),
        ).add_to(cluster)
    folium.LayerControl().add_to(result)
    return result
