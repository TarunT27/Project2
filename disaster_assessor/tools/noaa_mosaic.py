"""Build a georeferenced GeoTIFF from NOAA NGS post-storm aerial imagery tiles.

NOAA's National Geodetic Survey flies damaged areas after hurricanes and serves the imagery
as web-map tiles (https://storms.ngs.noaa.gov). This fetches a small grid of tiles around a
point and writes a Web Mercator (EPSG:3857) GeoTIFF the platform can ingest as an orthomosaic.

    python tools/noaa_mosaic.py --collection 20220930d --lat 26.4520 --lon -81.9505 \\
        --cols 8 --rows 6 --out demo/noaa-ian/fort-myers-beach.tif

Be polite: tiles are fetched sequentially, cached, and capped at 400 per run.
Imagery: NOAA National Geodetic Survey (U.S. Government work). For informational and
planning purposes only; NOAA states no accuracy or completeness guarantee.
"""

import argparse
import io
import math
from pathlib import Path
import time
import urllib.request

from PIL import Image

TILE_URL = "https://stormscdn.ngs.noaa.gov/{collection}-rgb/{z}/{x}/{y}"
USER_AGENT = "RapidDisasterAssessment-demo/1.0 (+https://github.com/TarunT27/Project2)"
ORIGIN = 20037508.342789244  # Web Mercator half-extent in metres
MAX_TILES = 400


def lonlat_to_tile(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def fetch_tile(collection, z, x, y, cache_dir, retries=3):
    path = Path(cache_dir) / collection / str(z) / str(x) / f"{y}.jpg"
    if path.is_file():
        return path.read_bytes()
    url = TILE_URL.format(collection=collection, z=z, x=x, y=y)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as r:
                data = r.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            time.sleep(0.05)
            return data
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    return b""


def build(collection, lat, lon, cols, rows, z, cache_dir):
    if cols * rows > MAX_TILES:
        raise SystemExit(f"Refusing to fetch more than {MAX_TILES} tiles per run.")
    cx, cy = lonlat_to_tile(lat, lon, z)
    x0, y0 = int(cx - cols / 2), int(cy - rows / 2)
    mosaic = Image.new("RGB", (cols * 256, rows * 256), (128, 128, 128))
    empty = 0
    for j in range(rows):
        for i in range(cols):
            data = fetch_tile(collection, z, x0 + i, y0 + j, cache_dir)
            with Image.open(io.BytesIO(data)) as tile:
                if tile.size != (256, 256):  # NOAA returns a 1x1 PNG where it has no imagery
                    empty += 1
                    continue
                mosaic.paste(tile.convert("RGB"), (i * 256, j * 256))
    res = 2 * ORIGIN / (256 * 2 ** z)
    west, north = x0 * 256 * res - ORIGIN, ORIGIN - y0 * 256 * res
    return mosaic, res, west, north, empty


def write_geotiff(image, res, west, north, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tags = {
        33550: (res, res, 0.0),                       # ModelPixelScale
        33922: (0.0, 0.0, 0.0, west, north, 0.0),     # ModelTiepoint: pixel (0,0) -> top-left corner
        # GeoKeyDirectory: projected model, pixel-is-area, EPSG:3857, metres
        34735: (1, 1, 0, 4, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, 3857, 3076, 0, 1, 9001),
    }
    try:
        image.save(out, "TIFF", tiffinfo=tags, compression="tiff_jpeg", quality=88)
    except (OSError, ValueError):
        image.save(out, "TIFF", tiffinfo=tags, compression="tiff_deflate")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--collection", default="20220930d", help="NOAA collection id, e.g. 20220930d (Hurricane Ian)")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--cols", type=int, default=8)
    p.add_argument("--rows", type=int, default=6)
    p.add_argument("--zoom", type=int, default=19)
    p.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "dra-noaa-tiles"))
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    mosaic, res, west, north, empty = build(args.collection, args.lat, args.lon, args.cols, args.rows, args.zoom,
                                            args.cache_dir)
    out = write_geotiff(mosaic, res, west, north, args.out)
    print(f"{out}: {mosaic.width}x{mosaic.height}px, {res:.3f} m/px (EPSG:3857), "
          f"{args.cols * args.rows - empty}/{args.cols * args.rows} tiles with imagery, {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
