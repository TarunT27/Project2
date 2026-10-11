"""Transparent priority ranking and standards-compliant geospatial exports."""

import json
import pandas as pd
from .geo_utils import valid_coordinate
from .mock_data import SEVERITY

EXPORT_COLUMNS = [
    "priority_rank",
    "observation_id",
    "source_file",
    "frame_seconds",
    "damage_class",
    "confidence",
    "latitude",
    "longitude",
    "location_source",
    "location_is_synthetic",
    "timestamp",
    "assessed_at_utc",
    "is_demo",
    "scope",
    "model",
    "confidence_threshold",
    "review_status",
    "bbox",
]


def priority_list(df):
    if df.empty:
        return pd.DataFrame(columns=EXPORT_COLUMNS)
    ranked = df.copy()
    ranked["severity"] = ranked["damage_class"].map(SEVERITY)
    ranked = ranked.sort_values(
        ["severity", "confidence", "observation_id"],
        ascending=[False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    ranked["priority_rank"] = range(1, len(ranked) + 1)
    return ranked.reindex(columns=EXPORT_COLUMNS)


def export_csv(df):
    output = priority_list(df).copy()
    # Filenames are untrusted; avoid spreadsheet formula execution on opening CSV.
    for col in output.select_dtypes(include="object").columns:
        output[col] = output[col].map(
            lambda v: (
                "'" + v
                if isinstance(v, str)
                and v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
                else v
            )
        )
    return output.to_csv(index=False).encode("utf-8-sig")


def export_geojson(df):
    features = []
    for row in priority_list(df).to_dict("records"):
        if not valid_coordinate(row.get("latitude"), row.get("longitude")):
            continue
        properties = {
            k: (None if isinstance(v, float) and pd.isna(v) else v)
            for k, v in row.items()
        }
        features.append(
            {
                "type": "Feature",
                "id": row["observation_id"],
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(row["longitude"]), float(row["latitude"])],
                },
                "properties": properties,
            }
        )
    return json.dumps(
        {"type": "FeatureCollection", "features": features}, allow_nan=False, indent=2
    ).encode("utf-8")
