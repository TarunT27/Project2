"""Field-ready exports: KML (Google Earth), zipped Shapefile (GIS), and a PDF SITREP.

Safety notes:
* KML is built with ElementTree, so every text/attribute value is XML-escaped.
  Descriptions are additionally HTML-escaped because KML viewers render
  <description> as HTML.
* DBF strings are written plain (DBF is not a formula-evaluating format) but are
  truncated to DBF byte limits.
* PDF text is coerced to latin-1 because only core fonts (Helvetica) are used.
"""

from __future__ import annotations

import html
import io
import math
import xml.etree.ElementTree as ET
import zipfile

import shapefile
from fpdf import FPDF

from utils.mock_data import CLASSES, COLORS, SEVERITY

from .routing import valid_point

KML_NS = "http://www.opengis.net/kml/2.2"
LIMITATIONS = (
    "Severity ranking orders review; it does not confirm occupancy, safe access, or routes."
)
WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,'
    '298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)
REVIEW_STATUSES = ("not_reviewed", "confirmed", "corrected", "rejected")
UNKNOWN_COLOR = "#6b7280"


# --------------------------------------------------------------------------- helpers
def _score(obs):
    try:
        value = float(obs.get("confidence"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _score_text(obs):
    score = _score(obs)
    return "n/a" if score is None else f"{round(score * 100)}%"


def _status(obs):
    return obs.get("status") or "not_reviewed"


def _hazards(obs):
    hazards = obs.get("hazards") or []
    if isinstance(hazards, str):
        hazards = [hazards]
    return [str(h) for h in hazards if h]


def _rank_key(obs):
    return (
        -SEVERITY.get(obs.get("damage_class"), -1),
        -(_score(obs) or 0.0),
        str(obs.get("id") or ""),
    )


def ranked(observations):
    """Observations sorted worst-first, matching utils.exports.priority_list."""
    return sorted(observations or [], key=_rank_key)


def _kml_color(hex_rgb):
    value = (hex_rgb or UNKNOWN_COLOR).lstrip("#")
    if len(value) != 6:
        value = UNKNOWN_COLOR.lstrip("#")
    rr, gg, bb = value[0:2], value[2:4], value[4:6]
    return f"ff{bb}{gg}{rr}".lower()


def _style_id(damage_class):
    if damage_class in COLORS:
        return "dmg-" + damage_class.lower().replace(" ", "-")
    return "dmg-unknown"


# --------------------------------------------------------------------------- KML
def export_kml(observations) -> bytes:
    """KML 2.2 Document with one styled Placemark per observation with valid coordinates."""
    ET.register_namespace("", KML_NS)
    q = lambda tag: f"{{{KML_NS}}}{tag}"  # noqa: E731
    kml = ET.Element(q("kml"))
    doc = ET.SubElement(kml, q("Document"))
    ET.SubElement(doc, q("name")).text = "Damage observations"
    for damage_class, color in list(COLORS.items()) + [("unknown", UNKNOWN_COLOR)]:
        style = ET.SubElement(doc, q("Style"), id=_style_id(damage_class))
        icon_style = ET.SubElement(style, q("IconStyle"))
        ET.SubElement(icon_style, q("color")).text = _kml_color(color)
        ET.SubElement(icon_style, q("scale")).text = "1.1"
        icon = ET.SubElement(icon_style, q("Icon"))
        ET.SubElement(icon, q("href")).text = (
            "http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png"
        )

    for obs in ranked(observations):
        lat, lon = obs.get("latitude"), obs.get("longitude")
        if not valid_point(lat, lon):
            continue
        damage_class = obs.get("damage_class") or "Unknown"
        pm = ET.SubElement(doc, q("Placemark"))
        if obs.get("id") is not None:
            pm.set("id", "obs-" + "".join(c if c.isalnum() or c in "-_" else "_" for c in str(obs["id"])))
        ET.SubElement(pm, q("name")).text = f"{damage_class} ({_score_text(obs)})"
        lines = [
            f"Class: {damage_class}",
            f"Model score: {_score_text(obs)}",
            f"Review status: {_status(obs)}",
            f"Source: {obs.get('source_file') or ''}",
            f"Location source: {obs.get('location_source') or ''}",
        ]
        if _hazards(obs):
            lines.append("Hazards: " + ", ".join(_hazards(obs)))
        if obs.get("assigned_crew"):
            lines.append(f"Crew: {obs['assigned_crew']}")
        if obs.get("is_demo"):
            lines.insert(0, "SYNTHETIC DEMO DATA")
        # HTML-escape (viewers render description as HTML); ET then XML-escapes.
        ET.SubElement(pm, q("description")).text = "<br/>".join(
            html.escape(str(line), quote=True) for line in lines
        )
        ET.SubElement(pm, q("styleUrl")).text = "#" + _style_id(obs.get("damage_class"))
        ext = ET.SubElement(pm, q("ExtendedData"))
        for key in ("id", "status", "location_source", "source_file"):
            value = _status(obs) if key == "status" else obs.get(key)
            data = ET.SubElement(ext, q("Data"), name=key)
            ET.SubElement(data, q("value")).text = "" if value is None else str(value)
        point = ET.SubElement(pm, q("Point"))
        ET.SubElement(point, q("coordinates")).text = f"{float(lon):.7f},{float(lat):.7f},0"

    return ET.tostring(kml, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------- Shapefile
_DBF_FIELDS = [
    ("OBS_ID", "C", 64, 0),
    ("CLASS", "C", 16, 0),
    ("SCORE", "N", 6, 3),
    ("STATUS", "C", 16, 0),
    ("SOURCE", "C", 120, 0),
    ("LOCSRC", "C", 40, 0),
    ("HAZARDS", "C", 200, 0),
    ("CREW", "C", 80, 0),
]


def _fit(value, size):
    """Truncate to ``size`` UTF-8 bytes without splitting a character."""
    text = "" if value is None else str(value)
    return text.encode("utf-8")[:size].decode("utf-8", "ignore")


def export_shapefile_zip(observations, basename="damage_observations") -> bytes:
    """In-memory zip of .shp/.shx/.dbf/.prj POINT features (WGS84) for GIS tools."""
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POINT)
    for name, ftype, size, decimals in _DBF_FIELDS:
        writer.field(name, ftype, size=size, decimal=decimals)
    for obs in ranked(observations):
        lat, lon = obs.get("latitude"), obs.get("longitude")
        if not valid_point(lat, lon):
            continue
        writer.point(float(lon), float(lat))
        score = _score(obs)
        writer.record(
            _fit(obs.get("id"), 64),
            _fit(obs.get("damage_class"), 16),
            None if score is None else round(min(max(score, 0.0), 1.0), 3),
            _fit(_status(obs), 16),
            _fit(obs.get("source_file"), 120),
            _fit(obs.get("location_source"), 40),
            _fit(";".join(_hazards(obs)), 200),
            _fit(obs.get("assigned_crew"), 80),
        )
    writer.close()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{basename}.shp", shp.getvalue())
        zf.writestr(f"{basename}.shx", shx.getvalue())
        zf.writestr(f"{basename}.dbf", dbf.getvalue())
        zf.writestr(f"{basename}.prj", WGS84_PRJ)
    return out.getvalue()


# --------------------------------------------------------------------------- PDF
_PUNCT = str.maketrans(
    {
        "—": "-", "–": "-", "‘": "'", "’": "'",
        "“": '"', "”": '"', "…": "...", "·": "-", "•": "-",
        " ": " ",
    }
)


def latin1(value) -> str:
    """Coerce any value to text safe for fpdf core fonts (latin-1)."""
    text = "" if value is None else str(value)
    text = text.translate(_PUNCT)
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return text.encode("latin-1", "replace").decode("latin-1")


class _Report(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(90, 90, 90)
        self.multi_cell(0, 3.5, latin1(f"Limitations: {LIMITATIONS}  Page {self.page_no()}"),
                        align="C")
        self.set_text_color(0, 0, 0)


def _heading(pdf, text):
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, latin1(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)


def _table(pdf, header, rows, widths):
    pdf.set_font("Helvetica", "", 8)
    with pdf.table(col_widths=widths, line_height=4.5, text_align="LEFT",
                   first_row_as_headings=True) as table:
        head = table.row()
        for cell in header:
            head.cell(latin1(cell))
        for values in rows:
            row = table.row()
            for cell in values:
                row.cell(latin1(cell))


def _coords(obs):
    if valid_point(obs.get("latitude"), obs.get("longitude")):
        return f"{float(obs['latitude']):.5f}, {float(obs['longitude']):.5f}"
    return "unknown"


def situation_report_pdf(incident: dict, observations, tasks: list, generated_by: str,
                         generated_at: str) -> bytes:
    """A4 situation report: banners, summary counts, ranked high-priority list, task summary."""
    incident = incident or {}
    observations = list(observations or [])
    tasks = list(tasks or [])
    pdf = _Report(format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_title(latin1(f"Situation report - {incident.get('name') or 'incident'}"))
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, "Damage Assessment Situation Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, latin1(f"Incident: {incident.get('name') or 'Unnamed incident'}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    if incident.get("description"):
        pdf.multi_cell(0, 4.5, latin1(incident["description"]), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, latin1(f"Generated at {generated_at} by {generated_by}"),
             new_x="LMARGIN", new_y="NEXT")

    banners = []
    if any(o.get("is_demo") for o in observations):
        banners.append("SYNTHETIC DEMO DATA - not a real incident assessment")
    if any(_status(o) == "not_reviewed" for o in observations):
        banners.append("Contains unreviewed model output")
    for banner in banners:
        pdf.ln(1)
        pdf.set_fill_color(254, 226, 226)
        pdf.set_text_color(153, 27, 27)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, latin1(banner), border=1, fill=True, align="C",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    # Summary counts
    _heading(pdf, f"Summary ({len(observations)} observations)")
    known = list(CLASSES)
    extra = sorted({o.get("damage_class") or "Unknown" for o in observations} - set(known))
    class_rows = []
    for cls in reversed(known):
        group = [o for o in observations if o.get("damage_class") == cls]
        class_rows.append(_class_row(cls, group))
    for cls in extra:
        group = [o for o in observations if (o.get("damage_class") or "Unknown") == cls]
        class_rows.append(_class_row(cls, group))
    _table(pdf, ["Class", "Total", "Confirmed/corrected", "Not reviewed", "Rejected"],
           class_rows, (50, 25, 40, 35, 30))
    pdf.ln(2)
    status_rows = [[s, str(sum(1 for o in observations if _status(o) == s))]
                   for s in REVIEW_STATUSES]
    other = sorted({_status(o) for o in observations} - set(REVIEW_STATUSES))
    status_rows += [[s, str(sum(1 for o in observations if _status(o) == s))] for s in other]
    _table(pdf, ["Review status", "Count"], status_rows, (60, 30))

    # High-priority list
    high = [o for o in ranked(observations)
            if o.get("damage_class") in ("Destroyed", "Major Damage")
            and _status(o) != "rejected"]
    _heading(pdf, f"High-priority observations (Destroyed / Major; top {min(50, len(high))} "
                  f"of {len(high)}, rejected excluded)")
    if high:
        rows = [
            [str(i), o.get("damage_class"), _score_text(o), _status(o), _coords(o),
             ", ".join(_hazards(o)) or "-", o.get("assigned_crew") or "-"]
            for i, o in enumerate(high[:50], start=1)
        ]
        _table(pdf, ["#", "Class", "Score", "Status", "Coordinates", "Hazards", "Crew"],
               rows, (8, 26, 13, 22, 36, 50, 35))
    else:
        pdf.cell(0, 5, "None.", new_x="LMARGIN", new_y="NEXT")

    # Tasks
    _heading(pdf, f"Field tasks ({len(tasks)})")
    if tasks:
        counts = {}
        for task in tasks:
            key = task.get("status") or task.get("task_status") or "unknown"
            counts[key] = counts.get(key, 0) + 1
        _table(pdf, ["Task status", "Count"],
               [[k, str(v)] for k, v in sorted(counts.items())], (60, 30))
    else:
        pdf.cell(0, 5, "No tasks recorded.", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 4, latin1(
        "Model scores are uncalibrated detector outputs, not probabilities of damage. "
        "Coordinates may be camera capture locations rather than building locations. "
        + LIMITATIONS), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def _class_row(cls, group):
    return [
        cls,
        str(len(group)),
        str(sum(1 for o in group if _status(o) in ("confirmed", "corrected"))),
        str(sum(1 for o in group if _status(o) == "not_reviewed")),
        str(sum(1 for o in group if _status(o) == "rejected")),
    ]
