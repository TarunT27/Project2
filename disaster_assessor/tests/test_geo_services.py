import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image, TiffImagePlugin

from app.services import footprints as fpmod
from app.services.footprints import (
    FootprintIndex,
    dedupe_observations,
    fetch_osm_buildings,
    haversine_m,
    load_footprints_geojson,
    match_point,
    point_in_polygon,
)
from app.services.ortho import (
    bounds_latlon,
    downsample_for_inference,
    pixel_to_lonlat,
    read_geotiff_georef,
)
from app.services.telemetry import (
    DEFAULT_HFOV_DEG,
    EARTH_RADIUS_M,
    hfov_from_35mm,
    parse_dji_srt,
    project_bbox_center,
    project_pixel_to_ground,
    read_dji_xmp,
    telemetry_at,
)
from app.services.tracking import associate_tracks, iou, summarize_tracks

LAT, LON = 37.7749, -122.4194
M_PER_DEG_LAT = math.radians(1) * EARTH_RADIUS_M


def offsets_m(lat, lon):
    """(east, north) meters of a point from the reference camera."""
    north = (lat - LAT) * M_PER_DEG_LAT
    east = (lon - LON) * M_PER_DEG_LAT * math.cos(math.radians(LAT))
    return east, north


class ProjectionTests(unittest.TestCase):
    def test_nadir_center_is_camera_position(self):
        lat, lon = project_pixel_to_ground(800, 600, 1600, 1200, LAT, LON, 60, -90, 37, 73.7)
        self.assertAlmostEqual(lat, LAT, places=9)
        self.assertAlmostEqual(lon, LON, places=9)

    def test_right_of_center_moves_east_with_heading_zero(self):
        alt, hfov, offset = 100.0, 73.7, 0.5
        px = 800 + offset * 800
        lat, lon = project_pixel_to_ground(px, 600, 1600, 1200, LAT, LON, alt, -90, 0, hfov)
        east, north = offsets_m(lat, lon)
        self.assertAlmostEqual(east, alt * math.tan(math.radians(hfov / 2)) * offset, delta=0.1)
        self.assertAlmostEqual(north, 0, delta=0.01)

    def test_heading_90_rotates_right_to_south(self):
        lat, lon = project_pixel_to_ground(1200, 600, 1600, 1200, LAT, LON, 100, -90, 90, 73.7)
        east, north = offsets_m(lat, lon)
        self.assertAlmostEqual(east, 0, delta=0.01)
        self.assertAlmostEqual(north, -100 * math.tan(math.radians(73.7 / 2)) * 0.5, delta=0.1)

    def test_top_of_nadir_image_points_along_heading(self):
        lat, lon = project_pixel_to_ground(800, 0, 1600, 1200, LAT, LON, 50, -90, 0, 90)
        east, north = offsets_m(lat, lon)
        self.assertAlmostEqual(east, 0, delta=0.01)
        self.assertAlmostEqual(north, 50 * 600 / 800, delta=0.05)

    def test_oblique_center_lands_ahead(self):
        lat, lon = project_pixel_to_ground(800, 600, 1600, 1200, LAT, LON, 100, -45, 0, 73.7)
        east, north = offsets_m(lat, lon)
        self.assertAlmostEqual(north, 100, delta=0.1)
        self.assertAlmostEqual(east, 0, delta=0.01)

    def test_above_horizon_and_far_hits_return_none(self):
        self.assertIsNone(project_pixel_to_ground(800, 100, 1600, 1200, LAT, LON, 100, 0, 0, 73.7))
        self.assertIsNone(project_pixel_to_ground(800, 600, 1600, 1200, LAT, LON, 100, 0, 0, 73.7))
        # pitch -1 deg from 100 m lands ~5.7 km away
        self.assertIsNone(project_pixel_to_ground(800, 600, 1600, 1200, LAT, LON, 100, -1, 0, 73.7))

    def test_bad_inputs_return_none(self):
        self.assertIsNone(project_pixel_to_ground(1, 1, 0, 10, LAT, LON, 50, -90, 0, 70))
        self.assertIsNone(project_pixel_to_ground(1, 1, 10, 10, 95, LON, 50, -90, 0, 70))
        self.assertIsNone(project_pixel_to_ground(1, 1, 10, 10, LAT, LON, -5, -90, 0, 70))
        self.assertIsNone(project_pixel_to_ground("x", 1, 10, 10, LAT, LON, 50, -90, 0, 70))

    def test_resolution_independent(self):
        a = project_pixel_to_ground(1200, 300, 1600, 1200, LAT, LON, 80, -70, 30, 70)
        b = project_pixel_to_ground(600, 150, 800, 600, LAT, LON, 80, -70, 30, 70)
        self.assertAlmostEqual(a[0], b[0], places=10)
        self.assertAlmostEqual(a[1], b[1], places=10)

    def test_hfov_and_bbox_center(self):
        self.assertAlmostEqual(hfov_from_35mm(24), 73.74, places=1)
        with self.assertRaises(ValueError):
            hfov_from_35mm(0)
        tele = {"latitude": LAT, "longitude": LON, "rel_alt": 40, "gimbal_pitch": -90, "gimbal_yaw": 0}
        lat, lon, source = project_bbox_center([700, 500, 900, 700], 1600, 1200, tele)
        self.assertEqual(source, "telemetry_projection")
        self.assertAlmostEqual(lat, LAT, places=8)
        self.assertIsNone(project_bbox_center([0, 0, 1, 1], 10, 10, {"latitude": LAT, "longitude": LON}))
        self.assertEqual(DEFAULT_HFOV_DEG, 73.7)


XMP = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description
 xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"
 drone-dji:GpsLatitude="37.774900" drone-dji:GpsLongtitude="-122.419400"
 drone-dji:AbsoluteAltitude="+85.50" drone-dji:RelativeAltitude="+60.20"
 drone-dji:GimbalRollDegree="+0.00" drone-dji:GimbalYawDegree="-12.30"
 drone-dji:FlightYawDegree="-11.90">
 <drone-dji:GimbalPitchDegree>-90.0</drone-dji:GimbalPitchDegree>
 </rdf:Description></rdf:RDF></x:xmpmeta>"""


class XmpTests(unittest.TestCase):
    def test_parse_attribute_and_element_forms(self):
        jpeg = b"\xff\xd8\xff\xe1\x00\x10http://ns.adobe.com/xap/1.0/\x00" + XMP + b"\xff\xd9"
        data = read_dji_xmp(jpeg, focal_35mm=24)
        self.assertAlmostEqual(data["latitude"], 37.7749)
        self.assertAlmostEqual(data["longitude"], -122.4194)
        self.assertEqual(data["abs_alt"], 85.5)
        self.assertEqual(data["rel_alt"], 60.2)
        self.assertEqual(data["gimbal_yaw"], -12.3)
        self.assertEqual(data["gimbal_pitch"], -90.0)
        self.assertEqual(data["flight_yaw"], -11.9)
        self.assertEqual(data["focal_35mm"], 24)
        self.assertIsNotNone(project_bbox_center([0, 0, 10, 10], 100, 100, data))

    def test_real_jpeg_and_missing_xmp(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "JPEG", xmp=XMP)
        self.assertEqual(read_dji_xmp(buf.getvalue())["rel_alt"], 60.2)
        self.assertIsNone(read_dji_xmp(b"\xff\xd8 no xmp \xff\xd9"))
        self.assertIsNone(read_dji_xmp(b'<x:xmpmeta><tiff:Make>DJI</tiff:Make></x:xmpmeta>'))
        self.assertIsNone(read_dji_xmp(None))


SRT_NEW = """1
00:00:00,000 --> 00:00:00,033
<font size="28">SrtCnt : 1, DiffTime : 33ms
2023-05-01 10:00:00.000
[iso: 100] [shutter: 1/500.0] [fnum: 2.8] [ev: 0] [focal_len: 24.00] [latitude: 37.774900] [longitude: -122.419400] [rel_alt: 50.000 abs_alt: 80.500] [gb_yaw: 15.2 gb_pitch: -90.0 gb_roll: 0.0] </font>

2
00:00:00,033 --> 00:00:00,066
<font size="28">SrtCnt : 2, DiffTime : 33ms
[latitude: 37.775000] [longitude: -122.419300] [rel_alt: 51.000 abs_alt: 81.500] </font>
"""

SRT_OLD = """1
00:00:01,000 --> 00:00:02,000
HOME(-122.4100,37.7700) 2017.08.05 14:11:51
GPS(-122.4194,37.7749,50) BAROMETER:48.5

2
00:00:02,000 --> 00:00:03,000
HOME(-122.4100,37.7700) 2017.08.05 14:11:52
GPS(-122.4195,37.7750,52)
"""


class SrtTests(unittest.TestCase):
    def test_bracket_format(self):
        entries = parse_dji_srt(SRT_NEW)
        self.assertEqual(len(entries), 2)
        first = entries[0]
        self.assertEqual((first["start_s"], first["end_s"]), (0.0, 0.033))
        self.assertEqual((first["latitude"], first["longitude"]), (37.7749, -122.4194))
        self.assertEqual((first["rel_alt"], first["abs_alt"]), (50.0, 80.5))
        self.assertEqual((first["gimbal_pitch"], first["yaw"]), (-90.0, 15.2))
        self.assertNotIn("gimbal_pitch", entries[1])

    def test_legacy_gps_format(self):
        entries = parse_dji_srt(SRT_OLD)
        self.assertEqual(len(entries), 2)
        self.assertEqual((entries[0]["latitude"], entries[0]["longitude"]), (37.7749, -122.4194))
        self.assertEqual(entries[0]["rel_alt"], 48.5)  # barometer preferred
        self.assertEqual(entries[1]["rel_alt"], 52.0)  # falls back to GPS altitude

    def test_telemetry_at(self):
        entries = parse_dji_srt(SRT_OLD)
        self.assertIs(telemetry_at(entries, 1.5), entries[0])
        self.assertIs(telemetry_at(entries, 2.0), entries[1])
        self.assertIs(telemetry_at(entries, 99), entries[1])
        self.assertIs(telemetry_at(entries, 0), entries[0])
        self.assertIsNone(telemetry_at([], 1))
        self.assertEqual(parse_dji_srt("garbage"), [])
        with self.assertRaises(ValueError):
            parse_dji_srt(None)


def make_geotiff(size, scale, tiepoint, geokeys):
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    ifd[33550], ifd.tagtype[33550] = scale, 12
    ifd[33922], ifd.tagtype[33922] = tiepoint, 12
    ifd[34735], ifd.tagtype[34735] = geokeys, 3
    buf = io.BytesIO()
    Image.new("RGB", size, "gray").save(buf, "TIFF", tiffinfo=ifd)
    return buf.getvalue()


class OrthoTests(unittest.TestCase):
    def test_epsg4326(self):
        tiff = make_geotiff(
            (200, 100), (0.0001, 0.0001, 0.0), (0, 0, 0, -122.42, 37.78, 0),
            (1, 1, 0, 2, 1024, 0, 1, 2, 2048, 0, 1, 4326),
        )
        georef = read_geotiff_georef(tiff)
        self.assertEqual(georef["epsg"], 4326)
        self.assertEqual((georef["width"], georef["height"]), (200, 100))
        lat, lon = pixel_to_lonlat(georef, 100, 50)
        self.assertAlmostEqual(lat, 37.775, places=9)
        self.assertAlmostEqual(lon, -122.41, places=9)
        (s, w), (n, e) = bounds_latlon(georef)
        self.assertAlmostEqual(s, 37.77)
        self.assertAlmostEqual(n, 37.78)
        self.assertAlmostEqual(w, -122.42)
        self.assertAlmostEqual(e, -122.40)

    def test_utm_10n(self):
        tiff = make_geotiff(
            (100, 80), (0.5, 0.5, 0.0), (0, 0, 0, 500000.0, 4180000.0, 0),
            (1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, 32610),
        )
        georef = read_geotiff_georef(tiff)
        self.assertEqual(georef["epsg"], 32610)
        lat, lon = pixel_to_lonlat(georef, 0, 0)
        self.assertAlmostEqual(lon, -123.0, places=6)  # zone 10 central meridian
        self.assertAlmostEqual(lat, 37.7673, places=3)
        lat2, lon2 = pixel_to_lonlat(georef, 100, 80)  # 50 m east, 40 m south
        self.assertAlmostEqual(haversine_m(lat, lon, lat2, lon2), math.hypot(50, 40), delta=0.5)
        self.assertLess(lat2, lat)
        self.assertGreater(lon2, lon)

    def test_invalid_inputs(self):
        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, "TIFF")
        self.assertIsNone(read_geotiff_georef(buf.getvalue()))
        self.assertIsNone(read_geotiff_georef(b"not a tiff"))
        with self.assertRaises(ValueError):
            pixel_to_lonlat({"epsg": None, "transform": (1, 0, 0, 0, -1, 0)}, 0, 0)
        tiff = make_geotiff((4, 4), (1.0, 1.0, 0.0), (0, 0, 0, 0, 0, 0), (1, 1, 0, 1, 3072, 0, 1, 32767))
        self.assertIsNone(read_geotiff_georef(tiff)["epsg"])

    def test_downsample(self):
        small, scale = downsample_for_inference(Image.new("RGB", (4000, 2000)), 1600)
        self.assertEqual(small.size, (1600, 800))
        self.assertAlmostEqual(scale, 2.5)
        same, scale = downsample_for_inference(Image.new("RGB", (100, 50)))
        self.assertEqual((same.size, scale), ((100, 50), 1.0))


def square(lon, lat, half=0.0001):
    return [[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat + half],
            [lon - half, lat + half], [lon - half, lat - half]]


GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "id": "a", "properties": {"name": "A"},
         "geometry": {"type": "Polygon", "coordinates": [square(LON, LAT)]}},
        {"type": "Feature", "properties": {},
         "geometry": {"type": "MultiPolygon", "coordinates": [[square(LON + 0.001, LAT)]]}},
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0, 0]}},
    ],
}


class FootprintTests(unittest.TestCase):
    def setUp(self):
        self.fps = load_footprints_geojson(json.dumps(GEOJSON).encode())

    def test_load(self):
        self.assertEqual([f["id"] for f in self.fps], ["a", "fp-1"])
        self.assertAlmostEqual(self.fps[0]["centroid"][0], LAT, places=9)
        self.assertAlmostEqual(self.fps[0]["centroid"][1], LON, places=9)
        self.assertEqual(len(self.fps[0]["polygon"][0]), 4)
        self.assertEqual(self.fps[0]["properties"], {"name": "A"})
        with self.assertRaises(ValueError):
            load_footprints_geojson("{bad")
        with self.assertRaises(ValueError):
            load_footprints_geojson({"type": "Point"})

    def test_point_in_polygon_and_match(self):
        ring = self.fps[0]["polygon"][0]
        self.assertTrue(point_in_polygon(LAT, LON, ring))
        self.assertFalse(point_in_polygon(LAT + 0.001, LON, ring))
        self.assertEqual(match_point(LAT + 0.00005, LON, self.fps)["id"], "a")
        near = match_point(LAT + 0.00018, LON, self.fps)  # ~20 m north of centroid
        self.assertEqual(near["id"], "a")
        self.assertIsNone(match_point(LAT + 0.001, LON, self.fps))
        self.assertIsNone(match_point(None, None, self.fps))

    def test_grid_index_matches_linear(self):
        many = load_footprints_geojson({
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": f"b{i}-{j}", "properties": {},
                 "geometry": {"type": "Polygon",
                              "coordinates": [square(LON + i * 0.0005, LAT + j * 0.0005, 0.0001)]}}
                for i in range(25) for j in range(25)
            ],
        })
        index = FootprintIndex(many)

        def brute(lat, lon, max_d=25):
            for fp in many:
                if any(point_in_polygon(lat, lon, r) for r in fp["polygon"]):
                    return fp
            near = [(haversine_m(lat, lon, *fp["centroid"]), i) for i, fp in enumerate(many)]
            d, i = min(near)
            return many[i] if d <= max_d else None

        points = [(LAT + 0.00502, LON + 0.0031), (LAT + 0.00025, LON + 0.0101),
                  (LAT + 0.0026, LON + 0.0101), (LAT - 1, LON), (LAT + 0.0049, LON + 0.0119)]
        for lat, lon in points:
            expected = brute(lat, lon)
            self.assertIs(match_point(lat, lon, index), expected)
            self.assertIs(match_point(lat, lon, many), expected)
        self.assertIsNotNone(brute(*points[0]))
        near_only = match_point(LAT + 0.0002, LON + 0.01, index)  # outside, ~22 m from b20-0
        self.assertEqual(near_only["id"], "b20-0")
        self.assertIs(near_only, brute(LAT + 0.0002, LON + 0.01))

    def test_dedupe(self):
        obs = [
            {"id": 1, "latitude": LAT, "longitude": LON, "damage_class": "Minor Damage", "confidence": 0.9},
            {"id": 2, "latitude": LAT + 0.00005, "longitude": LON, "damage_class": "Destroyed", "confidence": 0.4},
            {"id": 3, "latitude": LAT, "longitude": LON + 0.00005, "damage_class": "Destroyed", "confidence": 0.7},
            {"id": 4, "latitude": LAT + 0.01, "longitude": LON, "damage_class": "No Damage", "confidence": 0.8},
            {"id": 5, "latitude": None, "longitude": None, "damage_class": "Major Damage", "confidence": 0.5},
        ]
        groups = dedupe_observations(obs, self.fps)
        self.assertEqual(len(groups), 3)
        first = groups[0]
        self.assertEqual(first["footprint_id"], "a")
        self.assertEqual(first["observation_ids"], [1, 2, 3])
        self.assertEqual((first["damage_class"], first["confidence"], first["count"]), ("Destroyed", 0.7, 3))
        self.assertIsNotNone(first["polygon"])
        self.assertEqual([g["footprint_id"] for g in groups[1:]], [None, None])
        self.assertEqual(groups[1]["centroid"], (LAT + 0.01, LON))
        self.assertIsNone(groups[2]["centroid"])
        self.assertNotIn("footprint_id", obs[0])


OVERPASS = {
    "elements": [
        {"type": "way", "id": 11, "tags": {"building": "yes"},
         "geometry": [{"lat": LAT - 0.0001, "lon": LON - 0.0001}, {"lat": LAT - 0.0001, "lon": LON + 0.0001},
                      {"lat": LAT + 0.0001, "lon": LON + 0.0001}, {"lat": LAT + 0.0001, "lon": LON - 0.0001},
                      {"lat": LAT - 0.0001, "lon": LON - 0.0001}]},
        {"type": "relation", "id": 22, "tags": {"building": "yes", "type": "multipolygon"},
         "members": [{"type": "way", "role": "outer", "geometry": [
             {"lat": LAT, "lon": LON + 0.001}, {"lat": LAT, "lon": LON + 0.0012},
             {"lat": LAT + 0.0002, "lon": LON + 0.0012}, {"lat": LAT, "lon": LON + 0.001}]}]},
        {"type": "node", "id": 33, "lat": LAT, "lon": LON},
    ]
}


class OverpassTests(unittest.TestCase):
    def test_fetch_parses_and_caches(self):
        response = MagicMock()
        response.json.return_value = OVERPASS
        with tempfile.TemporaryDirectory() as tmp, patch.object(fpmod.httpx, "post", return_value=response) as post:
            fps = fetch_osm_buildings(LAT - 0.005, LON - 0.005, LAT + 0.005, LON + 0.005, cache_dir=tmp)
            self.assertEqual([f["id"] for f in fps], ["osm-way-11", "osm-relation-22"])
            self.assertEqual(match_point(LAT, LON, fps)["id"], "osm-way-11")
            args, kwargs = post.call_args
            self.assertEqual(args[0], fpmod.OVERPASS_URL)
            self.assertIn('way["building"]', kwargs["data"]["data"])
            self.assertIn("out geom;", kwargs["data"]["data"])
            self.assertIn("disaster-assessor", kwargs["headers"]["User-Agent"])
            self.assertEqual(len(list(Path(tmp).glob("overpass_*.json"))), 1)
            again = fetch_osm_buildings(LAT - 0.005, LON - 0.005, LAT + 0.005, LON + 0.005, cache_dir=tmp)
            self.assertEqual(post.call_count, 1)
            self.assertEqual([f["id"] for f in again], ["osm-way-11", "osm-relation-22"])

    def test_rejects_bad_bbox_without_network(self):
        with patch.object(fpmod.httpx, "post") as post:
            for bbox in [(37.0, -122.5, 37.1, -122.4), (37.8, -122.4, 37.7, -122.3), ("a", 0, 1, 1)]:
                with self.assertRaises(ValueError):
                    fetch_osm_buildings(*bbox)
            post.assert_not_called()

    def test_http_error(self):
        with patch.object(fpmod.httpx, "post", side_effect=fpmod.httpx.ConnectError("down")):
            with self.assertRaises(RuntimeError):
                fetch_osm_buildings(LAT, LON, LAT + 0.01, LON + 0.01)


class TrackingTests(unittest.TestCase):
    def test_iou(self):
        self.assertEqual(iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0)
        self.assertAlmostEqual(iou([0, 0, 10, 10], [5, 0, 15, 10]), 1 / 3)
        self.assertEqual(iou([0, 0, 1, 1], [5, 5, 6, 6]), 0.0)
        self.assertEqual(iou(None, [0, 0, 1, 1]), 0.0)

    def test_iou_association(self):
        frames = [
            [{"bbox": [0, 0, 100, 100], "damage_class": "Minor Damage", "confidence": 0.6},
             {"bbox": [300, 300, 400, 400], "damage_class": "No Damage", "confidence": 0.9}],
            [{"bbox": [305, 300, 405, 400], "damage_class": "No Damage", "confidence": 0.8},
             {"bbox": [10, 0, 110, 100], "damage_class": "Major Damage", "confidence": 0.5},
             {"bbox": [800, 800, 900, 900], "damage_class": "Destroyed", "confidence": 0.7}],
            [],
            [{"bbox": [10, 0, 110, 100], "damage_class": "Major Damage", "confidence": 0.9}],
        ]
        ids = associate_tracks(frames)
        self.assertEqual(ids, [[0, 1], [1, 0, 2], [], [3]])
        summary = summarize_tracks(frames, ids)
        self.assertEqual([s["track_id"] for s in summary], [0, 1, 2, 3])
        self.assertEqual((summary[0]["damage_class"], summary[0]["confidence"], summary[0]["count"]),
                         ("Major Damage", 0.5, 2))
        self.assertEqual((summary[1]["damage_class"], summary[1]["confidence"]), ("No Damage", 0.9))
        self.assertEqual(summary[0]["detections"], [(0, 0), (1, 1)])

    def test_geo_association_when_boxes_move(self):
        frames = [
            [{"bbox": [0, 0, 50, 50], "latitude": LAT, "longitude": LON, "label": "Destroyed", "confidence": 0.5}],
            [{"bbox": [600, 600, 650, 650], "latitude": LAT + 0.00005, "longitude": LON,
              "label": "Destroyed", "confidence": 0.8},
             {"bbox": [0, 0, 50, 50], "latitude": LAT + 0.001, "longitude": LON,
              "label": "No Damage", "confidence": 0.8}],
        ]
        ids = associate_tracks(frames)
        self.assertEqual(ids[1][0], ids[0][0])
        summary = summarize_tracks(frames, ids)
        self.assertAlmostEqual(summary[0]["latitude"], LAT + 0.000025)
        self.assertEqual(summary[0]["confidence"], 0.8)


if __name__ == "__main__":
    unittest.main()
