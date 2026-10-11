# Hurricane Ian demo imagery (NOAA)

Post-storm aerial imagery of Lee County, Florida, flown by NOAA's National Geodetic Survey on
30 September 2022, two days after Hurricane Ian made landfall (collection `20220930d`, ~27 cm/pixel).
Each file is an 8 × 6 tile mosaic (2048 × 1536 px, about 550 × 410 m) saved as a Web Mercator
(EPSG:3857) GeoTIFF, so detections land on the real map.

| File | Area | Centre (lat, lon) |
| --- | --- | --- |
| `fort-myers-beach-north.tif` | Fort Myers Beach, north end / Times Square | 26.4521, -81.9511 |
| `fort-myers-beach-estero.tif` | Fort Myers Beach, Estero Blvd | 26.4495, -81.9440 |
| `san-carlos-island.tif` | San Carlos Island | 26.4630, -81.9520 |
| `matlacha.tif` | Matlacha (Pine Island Rd) | 26.6333, -82.0706 |

**Source and terms:** NOAA National Geodetic Survey Emergency Response Imagery,
https://storms.ngs.noaa.gov/storms/ian/ (a U.S. Government work). NOAA provides it "for informational and
planning purposes only" with no accuracy or completeness guarantee. Rebuild or extend it with
`python tools/noaa_mosaic.py --lat=… --lon=… --out demo/noaa-ian/<name>.tif`.
