"""Run: streamlit run app.py. Demo mode needs no Torch or model download."""

from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from utils.detector import DamageDetector
from utils.exports import export_csv, export_geojson, priority_list
from utils.geo_utils import DEFAULT_CENTER, generate_damage_map, valid_coordinate
from utils.mock_data import CLASSES
from utils.pipeline import Settings, empty_batch, run_assessment

st.set_page_config(page_title="Disaster Review", page_icon=":material/public:", layout="wide")

st.markdown(f"<style>{Path(__file__).with_name('styles.css').read_text()}</style>", unsafe_allow_html=True)
st.markdown(
    '<div class="workspace-header"><p class="eyebrow">INCIDENT WORKSPACE</p>'
    '<h1>Disaster review</h1><p>Assess imagery. Locate observations. Prioritize review.</p></div>',
    unsafe_allow_html=True,
)

if "assessment" not in st.session_state:
    st.session_state.assessment = empty_batch()

with st.sidebar:
    st.markdown('<p class="eyebrow">DISASTER REVIEW</p>', unsafe_allow_html=True)
    st.header("Settings")
    st.caption("Configure your next assessment.")
    mode = st.radio(
        "Inference mode", ["Demo · synthetic detections", "Real · damage-trained YOLO"]
    )
    use_mock = mode.startswith("Demo")
    model_path = ""
    device = "cpu"
    if not use_mock:
        model_path = st.text_input(
            "Local damage model (.pt)", placeholder="models/damage_best.pt"
        )
        st.caption(
            "Use only weights from a trusted source. Standard yolov8n.pt / YOLO11 COCO models do not contain damage classes."
        )
        device = st.selectbox("Inference device", ["cpu", "mps", "cuda:0"])
    with st.expander("Detection settings"):
        confidence = st.slider("Confidence threshold", 0.0, 1.0, 0.5, 0.05)
        st.caption("Model scores are not probabilities of safety.")
    with st.expander("Location settings"):
        override = st.checkbox(
            "Use manual coordinates for all inputs",
            help="Overrides EXIF with one shared reference point. This is not an individual building location.",
        )
        lat = st.number_input(
            "Reference latitude", -90.0, 90.0, DEFAULT_CENTER[0], 0.0001, format="%.4f"
        )
        lon = st.number_input(
            "Reference longitude", -180.0, 180.0, DEFAULT_CENTER[1], 0.0001, format="%.4f"
        )
        st.caption("Demo locations are simulated around this point. Real inputs without GPS stay unmapped unless a manual reference is enabled.")
        tiles = st.checkbox(
            "Load OpenStreetMap background", value=True,
            help="Loads external map tiles. Folium also uses externally hosted JavaScript and CSS.",
        )
    with st.expander("Video settings"):
        interval = st.slider("Seconds between sampled frames", 1, 30, 3)
        max_frames = st.slider("Maximum frames per video", 1, 30, 8)
        st.caption("Frames may show the same building. GPS telemetry and tracking are not inferred.")
    st.divider()
    if st.button("Clear session", width="stretch"):
        st.session_state.assessment = empty_batch(use_mock)
        st.rerun()

if use_mock:
    st.info(
        "Demo workspace · Synthetic detections and simulated locations. For exploring the workflow only."
    )
else:
    st.warning(
        "Unverified model output · Qualified personnel must review observations. Images cannot confirm occupancy, structural safety, or safe access."
    )


@st.cache_resource(show_spinner=False, max_entries=2)
def load_detector(path, modified_ns, threshold, selected_device):
    """Cache only real model resources, never uploaded incident imagery."""
    return DamageDetector(False, path, threshold, selected_device)


def process_inputs(uploads, built_in_demo=False):
    # Commit one complete run at the end. Reruns never append duplicate observations.
    settings = Settings(
        use_mock=use_mock,
        confidence=confidence,
        model_name="synthetic-demo" if use_mock else Path(model_path).name,
        center=(lat, lon),
        override=override,
        interval=interval,
        max_frames=max_frames,
    )
    try:
        detector = (
            DamageDetector(True, confidence_threshold=confidence)
            if use_mock
            else load_detector(
                str(Path(model_path).resolve()),
                Path(model_path).stat().st_mtime_ns,
                confidence,
                device,
            )
        )
    except Exception as exc:
        st.error(f"Could not initialize the detector: {exc}")
        return False
    progress = st.progress(0, text="Preparing inputs…")
    st.session_state.assessment = run_assessment(
        detector,
        [] if built_in_demo else [(f.name, f.getvalue()) for f in uploads],
        settings,
        built_in_demo=built_in_demo,
        progress=lambda done, total: progress.progress(
            done / total, text=f"Processed {done}/{total} inputs"
        ),
    )
    progress.empty()
    return True


def empty_state(title, description):
    st.markdown(
        f'<div class="empty-state"><h3>{title}</h3><p>{description}</p>'
        '<div class="workflow"><span><b>01</b> Add imagery</span>'
        '<span><b>02</b> Review observations</span><span><b>03</b> Export findings</span></div></div>',
        unsafe_allow_html=True,
    )


assessor_tab, map_tab, export_tab = st.tabs(["Assess", "Map", "Export"])

with assessor_tab:
    has_results = bool(st.session_state.assessment["processed"])
    with st.expander("Add more imagery" if has_results else "Add imagery", expanded=not has_results):
        st.caption("Images or drone video · Up to 20 files / 200 MB total. A new run replaces previous results.")
        files = st.file_uploader(
            "Images or drone video",
            type=["jpg", "jpeg", "png", "webp", "tif", "tiff", "mp4", "mov", "avi", "mkv"],
            accept_multiple_files=True,
            label_visibility="collapsed",
        )
        left, right = st.columns(2)
        run = left.button("Run assessment", type="primary", disabled=not files, width="stretch")
        demo = right.button("Load synthetic demo", disabled=not use_mock, width="stretch")
    if run:
        if len(files) > 20 or sum(f.size for f in files) > 200 * 1024 * 1024:
            st.error("Limit the batch to 20 files and 200 MB total.")
        else:
            with st.spinner("Assessing imagery…"):
                completed = process_inputs(files)
            if completed:
                st.rerun()
    if demo:
        process_inputs([], built_in_demo=True)
        st.rerun()
    batch = st.session_state.assessment
    df = pd.DataFrame(batch["rows"])
    high = int(df["damage_class"].isin(["Major Damage", "Destroyed"]).sum()) if not df.empty else 0
    mapped = sum(valid_coordinate(r.get("latitude"), r.get("longitude")) for r in batch["rows"])
    if batch["processed"] and (
        batch["demo"] != use_mock
        or batch.get("threshold") != confidence
        or batch.get("model") != ("synthetic-demo" if use_mock else Path(model_path).name)
    ):
        st.warning("Settings changed. These are the last completed results; run again to apply your changes.")
    for error in batch["errors"]:
        st.error(error)
    if batch["images"]:
        with st.container(key="assess-summary"):
            a, b, c = st.columns(3)
            a.metric("Observations", len(df))
            b.metric("High-priority reviews", high)
            c.metric("Images / frames", batch["processed"])
        st.subheader("Review imagery")
        st.caption("Synthetic demo results" if batch["demo"] else "Unverified model results · Human review required")
        image_index = st.selectbox(
            "Choose image / sampled frame", range(len(batch["images"])),
            format_func=lambda i: batch["images"][i]["label"],
        )
        item = batch["images"][image_index]
        col1, col2 = st.columns(2)
        with col1:
            st.caption("ORIGINAL")
            st.image(item["original"], width="stretch")
        with col2:
            st.caption("SYNTHETIC DETECTIONS" if batch["demo"] else "MODEL DETECTIONS · UNVERIFIED")
            st.image(item["annotated"], width="stretch")
        st.caption(f'{item["detections"]} observations · Location source: {item["location_source"].replace("_", " ")}. No detections does not mean no damage.')
        if not df.empty:
            counts = df["damage_class"].value_counts().reindex(CLASSES, fill_value=0)
            breakdown = pd.DataFrame({
                "Damage class": CLASSES,
                "Observations": counts.values,
                "Percent": (counts.values / len(df) * 100).round(1),
            })
            st.subheader("Damage overview")
            st.dataframe(
                breakdown, hide_index=True, width="stretch",
                column_config={"Percent": st.column_config.ProgressColumn("Share of observations", min_value=0, max_value=100, format="%.1f%%")},
            )
            st.caption("Counts include retained detections. Overlapping photos and video frames may show the same building.")
        else:
            st.info("No observations met the confidence threshold. Review the imagery; this does not establish that buildings are undamaged.")
        with st.expander("Assessment details"):
            st.write(f'Model: {batch.get("model", "unknown")} · Threshold: {batch.get("threshold", 0):.0%}')
            st.caption("Boxes use oriented, resized image pixels. Observations are not unique buildings, confirmed rescues, or proof of safe entry. Each exported record retains model, location, demo status, and review provenance.")
    else:
        empty_state("Start with a clearer picture", "Add incident imagery above, or load the synthetic demo to explore the complete workflow.")

with map_tab:
    st.subheader("Observation map")
    st.caption("Explore approximate capture locations and review severity.")
    if df.empty:
        empty_state("Your map starts with imagery", "Run an assessment in Assess to place observations on the map.")
    else:
        with st.container(key="map-summary"):
            a, b = st.columns(2)
            a.metric("Mapped observations", mapped)
            b.metric("Without coordinates", len(df) - mapped)
        if batch["demo"]:
            st.info("Synthetic demo map · Locations are simulated, not a real disaster footprint.")
        if mapped:
            st.markdown('<div class="legend"><span class="intact">No damage</span><span class="minor">Minor</span><span class="major">Major</span><span class="destroyed">Destroyed</span></div>', unsafe_allow_html=True)
            damage_map = generate_damage_map(df, (lat, lon), show_tiles=tiles)
            st_folium(
                damage_map,
                zoom=damage_map.options["zoom"], center=tuple(damage_map.location),
                width=None, height=500, returned_objects=[], use_container_width=True,
            )
            st.caption("Select a marker for details. Expand clusters to see overlapping observations.")
        else:
            st.info("No valid coordinates in this batch. Add a manual reference under Location settings and rerun, or use images with GPS metadata.")
        with st.expander("How locations work"):
            st.caption("EXIF identifies the camera position, not the building. Manual points are shared reference locations. Building locations require georeferencing or field verification. Videos are not tracked or deduplicated.")

with export_tab:
    st.subheader("Export findings")
    st.caption("A severity-ranked queue for human review, with complete source metadata.")
    if df.empty:
        empty_state("No findings to export yet", "Run an assessment in Assess, review its observations, then download your findings here.")
    else:
        with st.container(key="export-summary"):
            a, b, c = st.columns(3)
            a.metric("Observations", len(df))
            b.metric("High-priority reviews", high)
            c.metric("Mapped observations", mapped)
        st.caption("Synthetic demo exports" if batch["demo"] else "Unverified model exports · Human review required")
        a, b = st.columns(2)
        prefix = "DEMO_" if batch["demo"] else "UNVERIFIED_"
        with a:
            with st.container(border=True):
                st.subheader("Review spreadsheet")
                st.caption("All observations, ordered by severity. Includes unmapped records.")
                st.download_button("Download priority CSV", export_csv(df), file_name=prefix + "damage_review_priority.csv", mime="text/csv", type="primary", width="stretch")
        with b:
            with st.container(border=True):
                st.subheader("Geographic data")
                st.caption("Mapped observations for GIS tools. Records without coordinates are excluded.")
                st.download_button("Download GeoJSON", export_geojson(df), file_name=prefix + "damage_observations.geojson", mime="application/geo+json", width="stretch")
        st.subheader("Priority preview")
        ranked = priority_list(df)
        preview = ranked[["priority_rank", "source_file", "damage_class", "confidence", "location_source", "review_status"]].copy()
        preview["confidence"] = preview["confidence"] * 100
        st.dataframe(preview, hide_index=True, width="stretch", column_config={
            "priority_rank": st.column_config.NumberColumn("Priority", width="small"),
            "source_file": "Source",
            "damage_class": "Damage",
            "confidence": st.column_config.NumberColumn("Model score", format="%.0f%%"),
            "location_source": "Location source",
            "review_status": "Review status",
        })
        with st.expander("Full source metadata"):
            st.dataframe(ranked.drop(columns=["bbox"]), hide_index=True, width="stretch")
        st.caption("Severity orders review; it does not provide travel directions or confirm safe entry. Both files retain demo status, model, threshold, timestamps, and review provenance.")

st.divider()
st.markdown('<p class="footer-note">Model inference runs on the host · No cloud image inference API</p>', unsafe_allow_html=True)
with st.expander("Data & privacy"):
    st.caption("Uploads and results stay in this Streamlit session on the machine hosting the app. Temporary video files are deleted after decoding. Optional map tiles request the viewed region externally. Clear session removes results; remove uploader files or close the tab to release uploaded inputs.")
