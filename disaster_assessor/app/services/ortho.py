"""GeoTIFF orthomosaic georeferencing via Pillow tags and pyproj (no GDAL)."""

from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, TiffImagePlugin
from pyproj import Transformer

MODEL_PIXEL_SCALE = 33550
MODEL_TIEPOINT = 33922
MODEL_TRANSFORMATION = 34264
GEOKEY_DIRECTORY = 34735
GT_MODEL_TYPE, GT_RASTER_TYPE = 1024, 1025
GEOGRAPHIC_TYPE, PROJECTED_CS_TYPE = 2048, 3072
USER_DEFINED = 32767


def _geokeys(directory) -> dict[int, int]:
    """Inline SHORT GeoKeys from a GeoKeyDirectoryTag value."""
    values = [int(v) for v in directory or ()]
    if len(values) < 4:
        return {}
    keys = {}
    for i in range(min(values[3], (len(values) - 4) // 4)):
        key_id, location, _count, value = values[4 + 4 * i: 8 + 4 * i]
        if location == 0:
            keys[key_id] = value
    return keys


def read_geotiff_georef(tiff_bytes: bytes) -> dict | None:
    """Return {"epsg", "transform": (a,b,c,d,e,f), "width", "height"} or None if not a GeoTIFF.

    ``transform`` maps continuous pixel coords (0,0 = top-left corner) to CRS x/y:
    x = a*px + b*py + c, y = d*px + e*py + f. ``epsg`` is None for user-defined CRSs.
    """
    try:
        # Construct directly so huge orthos skip Pillow's decompression-bomb check;
        # only the header/tags are read here, never pixel data.
        image = TiffImagePlugin.TiffImageFile(io.BytesIO(bytes(tiff_bytes)))
    except Exception:
        return None
    tags = image.tag_v2
    matrix = tags.get(MODEL_TRANSFORMATION)
    scale, tiepoint = tags.get(MODEL_PIXEL_SCALE), tags.get(MODEL_TIEPOINT)
    if matrix is not None and len(matrix) >= 8:
        m = [float(v) for v in matrix]
        a, b, c, d, e, f = m[0], m[1], m[3], m[4], m[5], m[7]
    elif scale is not None and tiepoint is not None and len(scale) >= 2 and len(tiepoint) >= 6:
        sx, sy = float(scale[0]), float(scale[1])
        i, j, x, y = (float(tiepoint[k]) for k in (0, 1, 3, 4))
        a, b, c, d, e, f = sx, 0.0, x - i * sx, 0.0, -sy, y + j * sy
    else:
        return None
    keys = _geokeys(tags.get(GEOKEY_DIRECTORY))
    if keys.get(GT_RASTER_TYPE) == 2:  # PixelIsPoint: tiepoint is a pixel center
        c, f = c - 0.5 * (a + b), f - 0.5 * (d + e)
    epsg = None
    projected, geographic = keys.get(PROJECTED_CS_TYPE), keys.get(GEOGRAPHIC_TYPE)
    if projected and projected != USER_DEFINED:
        epsg = projected
    elif geographic and geographic != USER_DEFINED and keys.get(GT_MODEL_TYPE, 2) == 2:
        epsg = geographic
    return {
        "epsg": epsg,
        "transform": (a, b, c, d, e, f),
        "width": image.width,
        "height": image.height,
    }


@lru_cache(maxsize=32)
def _to_wgs84(epsg: int) -> Transformer:
    return Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)


def pixel_to_lonlat(georef: dict, px: float, py: float) -> tuple[float, float]:
    """Map a full-resolution pixel coordinate to WGS84; returns (lat, lon)."""
    epsg = (georef or {}).get("epsg")
    if not epsg:
        raise ValueError("GeoTIFF has no supported EPSG coordinate reference system")
    a, b, c, d, e, f = georef["transform"]
    x, y = a * px + b * py + c, d * px + e * py + f
    if int(epsg) == 4326:
        return float(y), float(x)
    try:
        lon, lat = _to_wgs84(int(epsg)).transform(x, y, errcheck=True)
    except Exception as exc:
        raise ValueError(f"Cannot transform EPSG:{epsg} to WGS84: {exc}") from exc
    return float(lat), float(lon)


def bounds_latlon(georef: dict) -> list[list[float]]:
    """[[south, west], [north, east]] of the raster footprint, for map overlays."""
    w, h = georef["width"], georef["height"]
    corners = [pixel_to_lonlat(georef, x, y) for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
    lats, lons = [p[0] for p in corners], [p[1] for p in corners]
    return [[min(lats), min(lons)], [max(lats), max(lons)]]


def downsample_for_inference(img: Image.Image, max_side: int = 1600) -> tuple[Image.Image, float]:
    """Resize so the long side <= max_side; returns (image, scale) with full_px = small_px * scale."""
    if max_side <= 0:
        raise ValueError("max_side must be positive")
    w, h = img.size
    if max(w, h) <= max_side:
        return img, 1.0
    ratio = max_side / max(w, h)
    size = (max(1, round(w * ratio)), max(1, round(h * ratio)))
    small = img.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
    return small, max(w, h) / max(size)
