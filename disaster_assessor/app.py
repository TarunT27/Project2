"""Run: streamlit run app.py. Local mode is loopback-only; shared hosting uses OIDC."""
from datetime import datetime
from pathlib import Path
import json
import os
import re
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from utils.auth import require_identity
from utils.evaluation import validate_report
from utils.exports import export_csv, export_geojson, priority_list
from utils.geo_utils import DEFAULT_CENTER, generate_damage_map, valid_coordinate
from utils.jobs import JobManager
from utils.mock_data import CLASSES
from utils.processing import Settings, Input, retry_inputs
from utils.review_ui import current_annotation, review_controls, location_controls
from utils.storage import Store, empty_batch, STATUSES

st.set_page_config(page_title="Disaster Review", page_icon=":material/public:", layout="wide")
ROOT = Path(__file__).resolve().parent


@st.cache_resource
def resources(path):
    database = Store(path)
    return database, JobManager(database)


store, jobs = resources(os.environ.get("DISASTER_DB_PATH", str(ROOT / "data" / "workspace.sqlite3")))
user = require_identity(st, store)
if st.session_state.get("workspace_user") != user:
    st.session_state.clear()
    st.session_state.workspace_user = user

st.markdown(f"<style>{ROOT.joinpath('styles.css').read_text()}</style>", unsafe_allow_html=True)
st.markdown('<div class="workspace-header"><p class="eyebrow">INCIDENT WORKSPACE</p><h1>Disaster review</h1><p>Assess imagery. Locate observations. Prioritize review.</p></div>', unsafe_allow_html=True)

incidents = store.list_incidents(user)
if not incidents:
    store.create_incident(user, "My first incident", datetime.now(ZoneInfo("America/New_York")).date().isoformat())
    incidents = store.list_incidents(user)
pending_incident = st.session_state.pop("next_incident", None)
if pending_incident:
    st.session_state.incident_choice = pending_incident
with st.sidebar:
    st.markdown('<p class="eyebrow">DISASTER REVIEW</p>', unsafe_allow_html=True)
    st.caption("Local workspace" if user == "local" else user)
    if user != "local" and st.button("Sign out"):
        st.logout()
    incident_ids = [i["id"] for i in incidents]
    if st.session_state.get("incident_choice") not in incident_ids:
        st.session_state.incident_choice = incident_ids[0]
    incident_id = st.selectbox("Incident", incident_ids, format_func=lambda value: next(f'{i["name"]} · {i["event_date"]}' for i in incidents if i["id"] == value), key="incident_choice")
    with st.expander("New incident"):
        with st.form("new_incident"):
            name = st.text_input("Incident name", max_chars=120)
            event_date = st.date_input("Incident date", datetime.now(ZoneInfo("America/New_York")).date())
            place = st.text_input("Incident location", max_chars=500)
            create = st.form_submit_button("Create incident")
        if create:
            try:
                new_id = store.create_incident(user, name, event_date.isoformat(), place)
                st.session_state["next_incident"] = new_id
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

role = store.role(incident_id, user)
editable = role in {"owner", "editor"}
runs = store.list_runs(incident_id, user)
if st.session_state.get("selected_incident") != incident_id:
    st.session_state.selected_incident = incident_id
    st.session_state.active_run = next((r["id"] for r in runs if r["status"] != "running"), None)
    st.session_state.pop("job_id", None)
if "active_run" not in st.session_state:
    st.session_state.active_run = None

with st.sidebar:
    st.caption(f'Access: {role}' + (f' · {next(i["location"] for i in incidents if i["id"] == incident_id)}' if next(i["location"] for i in incidents if i["id"] == incident_id) else ""))
    st.header("Settings")
    mode = st.radio("Inference mode", ["Demo · synthetic detections", "Real · damage-trained YOLO"])
    use_mock = mode.startswith("Demo")
    model_path, device = "", "cpu"
    if not use_mock:
        model_path = st.text_input("Local damage model (.pt)", placeholder="models/damage_best.pt")
        device = st.selectbox("Inference device", ["cpu", "mps", "cuda:0"])
        st.caption("Only use trusted, damage-trained weights. Generic COCO weights are rejected.")
    with st.expander("Detection settings"):
        confidence = st.slider("Confidence threshold", 0.0, 1.0, 0.5, 0.05)
        st.caption("Model scores are not probabilities of safety.")
    with st.expander("Location settings"):
        override = st.checkbox("Use manual coordinates for all inputs", help="One reference point for the next run. Edit individual images after assessment.")
        lat = st.number_input("Reference latitude", -90.0, 90.0, DEFAULT_CENTER[0], 0.0001, format="%.4f")
        lon = st.number_input("Reference longitude", -180.0, 180.0, DEFAULT_CENTER[1], 0.0001, format="%.4f")
        tiles = st.checkbox("Load OpenStreetMap background", value=True, help="Loads external map tiles; Folium also uses externally hosted JavaScript and CSS.")
    with st.expander("Video settings"):
        interval = st.slider("Seconds between sampled frames", 1, 30, 3)
        max_frames = st.slider("Maximum frames per video", 1, 30, 8)
        st.caption("Frames may show the same building. Tracking is not inferred.")
    if role == "owner" and user != "local":
        with st.expander("Incident access"):
            st.caption("Viewer: read and export. Editor: assess, review, and locate. Owners manage access.")
            with st.form("grant_access"):
                member_email = st.text_input("Member sign-in email")
                member_role = st.selectbox("Member role", ["viewer", "editor"])
                grant = st.form_submit_button("Grant incident access")
            if grant:
                try:
                    store.grant(incident_id, user, member_email, member_role)
                    st.rerun()
                except (ValueError, PermissionError) as exc:
                    st.error(str(exc))
            for member in store.members(incident_id, user):
                st.caption(f'{member["user_id"]} · {member["role"]}')
                if st.button("Revoke access", key="revoke_" + member["user_id"]):
                    store.revoke(incident_id, user, member["user_id"])
                    st.rerun()
    st.divider()
    if st.button("Clear session", width="stretch"):
        st.session_state.active_run = None
        st.session_state.assessment = empty_batch()
        st.rerun()
    st.caption("Saved incidents and history are retained.")

settings = Settings(use_mock, confidence, model_path, device, lat, lon, override, interval, max_frames)
active_job = st.session_state.get("job_id") or jobs.active_for(user, incident_id)
if active_job:
    st.session_state.job_id = active_job


@st.fragment(run_every="1s")
def job_progress(identity):
    # Recheck identity and ACL on periodic reruns, not just the initial page load.
    current_user = require_identity(st, store)
    try:
        snapshot = jobs.snapshot(identity, current_user)
    except PermissionError:
        st.error("Access to the active incident was removed.")
        return
    if not snapshot:
        st.session_state.pop("job_id", None)
        st.rerun()
    if snapshot["status"] != "running":
        st.session_state.active_run = snapshot["run_id"]
        st.session_state.pop("job_id", None)
        st.session_state.notice = snapshot["message"]
        st.session_state.notice_level = "success" if snapshot["status"] == "completed" else ("error" if snapshot["status"] == "failed" else "warning")
        st.rerun()
    st.progress(snapshot["progress"], text=snapshot["message"])
    if st.button("Cancel assessment", key="cancel_job"):
        jobs.cancel(identity, current_user)
        st.info("Cancellation requested. The current model call must finish first; complete files are saved.")


if active_job:
    job_progress(active_job)
if st.session_state.get("notice"):
    getattr(st, st.session_state.pop("notice_level", "success"))(st.session_state.pop("notice"))
run = store.get_run(st.session_state.active_run, user) if st.session_state.active_run else None
batch = run["batch"] if run else empty_batch()
st.session_state.assessment = batch
if run:
    timestamp = datetime.fromisoformat(run["created_at"]).strftime("%b %d, %H:%M UTC")
    st.caption(f'Assessment: {timestamp} · {run["status"]} · Saved to this incident')
if batch.get("demo", use_mock) if run else use_mock:
    st.info("Demo workspace · Synthetic detections and simulated locations. For exploring the workflow only.")
else:
    st.warning("Unverified model output · Qualified personnel must review observations. Images cannot confirm occupancy, structural safety, or safe access.")


def empty_state(title, description):
    # These strings are fixed application content, not user-entered incident names.
    st.markdown(f'<div class="empty-state"><h3>{title}</h3><p>{description}</p></div>', unsafe_allow_html=True)


def launch(inputs, selected_settings=settings, demo=False, previous=None):
    try:
        st.session_state.job_id = jobs.start(incident_id, user, selected_settings, inputs, demo, previous)
        st.rerun()
    except (ValueError, PermissionError) as exc:
        st.error(str(exc))


df = pd.DataFrame(batch["rows"])
high = int(df["damage_class"].isin(["Major Damage", "Destroyed"]).sum()) if not df.empty else 0
mapped = sum(valid_coordinate(r.get("latitude"), r.get("longitude")) for r in batch["rows"])
assessor_tab, map_tab, export_tab, history_tab, quality_tab = st.tabs(["Assess", "Map", "Export", "History", "Model quality"])
with assessor_tab:
    with st.expander("Add more imagery" if batch["processed"] else "Add imagery", expanded=not batch["processed"]):
        st.caption("Up to 20 files / 200 MB total. Each run is saved separately in History.")
        files = st.file_uploader("Images or drone video", type=["jpg", "jpeg", "png", "webp", "tif", "tiff", "mp4", "mov", "avi", "mkv"], accept_multiple_files=True, label_visibility="collapsed")
        a, b = st.columns(2)
        start = a.button("Run assessment", type="primary", disabled=not files or not editable or bool(active_job), width="stretch")
        demo = b.button("Load synthetic demo", disabled=not use_mock or not editable or bool(active_job), width="stretch")
        if start:
            # Check bounds before materializing uploaded bytes for the background worker.
            if len(files) > 20 or sum(f.size for f in files) > 200 * 1024 * 1024:
                st.error("Limit the batch to 20 files and 200 MB total.")
            else:
                launch([Input(f.name, f.getvalue()) for f in files])
        if demo:
            launch([], demo=True)
    for error in batch["errors"]:
        st.error(error)
    if run and batch.get("failed_files"):
        with st.expander("Retry failed files", expanded=True):
            st.write("Failed: " + ", ".join(f["name"] for f in batch["failed_files"]))
            st.caption("Use the same uploaded files above, or re-upload them after reopening the app. Retry preserves successful observations and reviews in a new run.")
            if st.button("Retry failed files", disabled=not files or not editable or bool(active_job)):
                try:
                    if len(files) > 20 or sum(f.size for f in files) > 200 * 1024 * 1024:
                        raise ValueError("Limit retries to 20 files / 200 MB.")
                    inputs = retry_inputs([Input(f.name, f.getvalue()) for f in files], batch)
                    launch(inputs, Settings(**run["settings"]), previous=run["id"])
                except ValueError as exc:
                    st.error(str(exc))
    if batch["images"]:
        with st.container(key="assess-summary"):
            a, b, c = st.columns(3)
            a.metric("Observations", len(df))
            b.metric("High-priority reviews", high)
            c.metric("Images / frames", batch["processed"])
        st.subheader("Review imagery")
        image_ids = [i["image_id"] for i in batch["images"]]
        image_id = st.selectbox("Choose image / sampled frame", image_ids, format_func=lambda value: next(i["label"] for i in batch["images"] if i["image_id"] == value), key="image_" + run["id"])
        image = next(i for i in batch["images"] if i["image_id"] == image_id)
        a, b = st.columns(2)
        a.image(image["original"], caption="ORIGINAL", width="stretch")
        b.image(current_annotation(image, batch["rows"]), caption="CURRENT REVIEW LABELS · SYNTHETIC" if batch["demo"] else "CURRENT REVIEW LABELS · UNVERIFIED", width="stretch")
        image_rows = [r for r in batch["rows"] if r["image_id"] == image_id]
        st.caption(f'{len(image_rows)} observations · {image["location_source"].replace("_", " ")}. No detections does not mean no damage.')
        if image_rows:
            obs = st.selectbox("Observation to review", [r["observation_id"] for r in image_rows], format_func=lambda value: next(f'{index + 1} · {r["damage_class"]} · {r["review_status"]}' for index, r in enumerate(image_rows) if r["observation_id"] == value), key="observation_" + run["id"] + image_id)
            row = next(r for r in image_rows if r["observation_id"] == obs)
            with st.expander("Selected observation", expanded=True):
                st.image(current_annotation(image, image_rows, obs), caption="Selected region", width="stretch")
                review_controls(store, user, run, row, editable, "assess")
        else:
            st.info("No observations met the threshold. Review the image; this does not establish that buildings are undamaged.")
        location_controls(store, user, run, image, editable, "assess")
        if not df.empty:
            counts = df["damage_class"].value_counts().reindex(CLASSES, fill_value=0)
            st.subheader("Damage overview")
            st.dataframe(pd.DataFrame({"Damage class": CLASSES, "Observations": counts.values, "Percent": (counts.values / len(df) * 100).round(1)}), hide_index=True, width="stretch")
        st.caption("Counts are observations, not unique buildings. Overlapping photos and video frames may show the same structure.")
    else:
        empty_state("Start with a clearer picture", "Add incident imagery or load the synthetic demo. Completed assessments are saved in History.")

with map_tab:
    st.subheader("Observation map")
    if df.empty:
        empty_state("Your map starts with imagery", "Run an assessment or open a saved run in History.")
    else:
        a, b = st.columns(2)
        severity = a.multiselect("Map severity", CLASSES, default=list(CLASSES))
        statuses = b.multiselect("Map review status", STATUSES, default=list(STATUSES))
        sources = st.multiselect("Map sources", sorted(df["source_file"].unique()), default=sorted(df["source_file"].unique()), key="map_sources_" + run["id"])
        filtered = df[df["damage_class"].isin(severity) & df["review_status"].isin(statuses) & df["source_file"].isin(sources)]
        mapped_df = filtered[[valid_coordinate(r["latitude"], r["longitude"]) for r in filtered.to_dict("records")]] if not filtered.empty else filtered
        with st.container(key="map-summary"):
            a, b = st.columns(2)
            a.metric("Mapped / filtered", len(mapped_df))
            b.metric("Without coordinates", len(filtered) - len(mapped_df))
        st.caption("Synthetic demo locations" if batch["demo"] else "Camera / reference locations · Field verification required")
        if mapped_df.empty:
            st.info("No mapped observations match these filters. Adjust filters or edit an image's capture location in Assess.")
        else:
            st.markdown('<div class="legend"><span class="intact">No damage</span><span class="minor">Minor</span><span class="major">Major</span><span class="destroyed">Destroyed</span></div>', unsafe_allow_html=True)
            damage_map = generate_damage_map(mapped_df, (lat, lon), show_tiles=tiles)
            result = st_folium(damage_map, zoom=damage_map.options["zoom"], center=tuple(damage_map.location),
                key=f'map_{run["id"]}_{run["version"]}_{"_".join(severity)}_{"_".join(statuses)}_{"_".join(sources)}',
                height=450, returned_objects=["last_object_clicked_tooltip"], use_container_width=True)
            observation_ids = list(mapped_df["observation_id"])
            selected_key = "map_observation_" + run["id"]
            tooltip = (result or {}).get("last_object_clicked_tooltip") or ""
            match = re.search(r"Observation ([a-f0-9]{24}-\d+)", tooltip)
            click_key = f'map_click_{run["id"]}'
            if match and match[1] in observation_ids and st.session_state.get(click_key) != tooltip:
                st.session_state[click_key] = tooltip
                st.session_state[selected_key] = match[1]
            if st.session_state.get(selected_key) not in observation_ids:
                st.session_state[selected_key] = observation_ids[0]
            selected = st.selectbox("Mapped observation", observation_ids, format_func=lambda value: next(f'{r["source_file"]} · {r["damage_class"]} · {r["observation_id"][-4:]}' for r in batch["rows"] if r["observation_id"] == value), key=selected_key)
            row = next(r for r in batch["rows"] if r["observation_id"] == selected)
            image = next(i for i in batch["images"] if i["image_id"] == row["image_id"])
            st.subheader("Linked source image")
            st.image(current_annotation(image, batch["rows"], selected), caption=image["label"], width="stretch")
            review_controls(store, user, run, row, editable, "map")
            location_controls(store, user, run, image, editable, "map")
        st.caption("Marker selection links to its source image below. Capture points do not verify individual building positions; video frames are not deduplicated.")

with export_tab:
    st.subheader("Export findings")
    if df.empty:
        empty_state("No findings to export yet", "Run an assessment or open a saved run in History.")
    else:
        with st.container(key="export-summary"):
            a, b, c = st.columns(3)
            a.metric("Observations", len(df))
            b.metric("Reviewed", int((df["review_status"] == "reviewed").sum()))
            c.metric("Mapped observations", mapped)
        st.caption("Exports include all observations from this run, using current review labels. Map filters do not change exports.")
        prefix = "DEMO_" if batch["demo"] else "UNVERIFIED_"
        a, b = st.columns(2)
        with a.container(border=True):
            st.subheader("Review spreadsheet")
            st.caption("All observations with original predictions, review notes, and provenance.")
            st.download_button("Download priority CSV", export_csv(df), file_name=prefix + "damage_review_priority.csv", mime="text/csv", type="primary", width="stretch")
        with b.container(border=True):
            st.subheader("Geographic data")
            st.caption("Mapped observations for GIS tools. Unmapped rows are excluded.")
            st.download_button("Download GeoJSON", export_geojson(df), file_name=prefix + "damage_observations.geojson", mime="application/geo+json", width="stretch")
        ranked = priority_list(df)
        st.subheader("Priority preview")
        st.dataframe(ranked[["priority_rank", "source_file", "damage_class", "predicted_class", "confidence", "review_status", "reviewed_by"]], hide_index=True, width="stretch")
        with st.expander("Full source metadata"):
            st.dataframe(ranked.drop(columns=["bbox"]), hide_index=True, width="stretch")
        st.caption("Severity orders human review. It does not provide travel directions or confirm safe entry.")

with history_tab:
    st.subheader("Assessment history")
    st.caption("Saved runs and reviews survive refreshes and server restarts. Times below are UTC.")
    if runs:
        history_id = st.selectbox("Saved assessment", [r["id"] for r in runs], format_func=lambda value: next(f'{r["created_at"]} · {r["status"]}' for r in runs if r["id"] == value))
        if st.button("Open saved assessment"):
            st.session_state.active_run = history_id
            st.rerun()
        st.dataframe(pd.DataFrame(runs).rename(columns={"created_at": "Created (UTC)", "status": "Status", "actor": "Started by"}), hide_index=True, width="stretch")
    else:
        st.info("No assessments saved for this incident yet.")
    audit = store.audit_log(incident_id, user)
    with st.expander("Change history"):
        st.dataframe(pd.DataFrame(audit), hide_index=True, width="stretch")
        st.caption("The latest 1,000 changes are displayed; the download includes all recorded changes.")
        st.download_button("Download audit history", json.dumps(store.audit_log(incident_id, user, limit=None), indent=2).encode(), file_name="incident_audit.json", mime="application/json")

with quality_tab:
    st.subheader("Model quality")
    st.info("A damage-trained checkpoint and labeled test imagery are required to measure real accuracy. No checkpoint or test dataset is bundled.")
    st.caption("Evaluate a held-out test set with the command below, then import its report. The evaluator measures class errors, unmatched regions, and missed high-priority damage at a fixed confidence / IoU threshold.")
    st.code("python evaluate_model.py --manifest evaluation/manifest.json --model models/damage_best.pt --output evaluation/report.json", language="bash")
    st.download_button("Download label manifest template", (ROOT / "evaluation" / "manifest.example.json").read_bytes(), file_name="manifest.example.json", mime="application/json")
    saved_reports = store.evaluations(incident_id, user)
    report = None
    if saved_reports:
        report_id = st.selectbox("Saved evaluation", [r["id"] for r in saved_reports], format_func=lambda value: next(f'{r["report"].get("model", "Model")} · {r["created_at"]} UTC' for r in saved_reports if r["id"] == value))
        report = next(r["report"] for r in saved_reports if r["id"] == report_id)
    report_file = st.file_uploader("Evaluation report (.json)", type=["json"])
    if report_file:
        try:
            if report_file.size > 5 * 1024 * 1024:
                raise ValueError("Use an evaluation report below 5 MB.")
            report = validate_report(json.loads(report_file.getvalue()))
            if st.button("Save evaluation report", disabled=not editable):
                store.save_evaluation(incident_id, user, report)
                st.success("Evaluation report saved to this incident.")
        except (ValueError, KeyError, TypeError, PermissionError) as exc:
            report = None
            st.error(f"Could not read evaluation report: {exc}")
    if report:
        st.caption("Imported evaluation report · Results and dataset coverage require independent review.")
        st.write("Model: " + str(report.get("model", "unknown")))
        a, b, c = st.columns(3)
        a.metric("Test images", report["image_count"])
        b.metric("Missed damaged regions", report["missed_damage"])
        c.metric("Missed high-priority regions", report["missed_high_priority"])
        per_class = pd.DataFrame(report["per_class"]).T
        st.dataframe(per_class.drop(columns=["recall_95pct_interval"], errors="ignore"), width="stretch")
        st.subheader("Label confusion")
        st.dataframe(pd.DataFrame(report["confusion_matrix"]).T, width="stretch")
        st.caption(str(report["limitations"]))
        with st.expander("Evaluation provenance"):
            st.json({key: report.get(key) for key in ("model_sha256", "dataset_sha256", "threshold", "match_iou", "declared_split", "generated_at_utc")})
        st.download_button("Download evaluation report", json.dumps(report, indent=2, allow_nan=False).encode(), file_name="model_evaluation.json", mime="application/json")

st.divider()
with st.expander("Data & privacy"):
    st.caption("Incidents, run previews, reviews, coordinates, and audit history are saved on the host in the workspace database. Raw uploaded files stay in session/worker memory; temporary video files are deleted after decoding. Clear session does not delete saved history. Optional map tiles request the viewed region externally. Protect and back up the host database; shared hosting requires OIDC and incident permissions.")
