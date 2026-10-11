"""Review controls shared by the assessor and map detail panel."""
import base64
import io

from PIL import Image
import streamlit as st

from .detector import annotate_image
from .mock_data import CLASSES
from .storage import ConflictError, STATUSES


def current_annotation(image, rows, selected=None):
    original = Image.open(io.BytesIO(base64.b64decode(image["original"].split(",", 1)[1]))).convert("RGB")
    detections = [{"bbox": r["bbox"], "label": r["damage_class"], "confidence": r["confidence"], "is_demo": r["is_demo"]}
                  for r in rows if r["image_id"] == image["image_id"] and (selected is None or r["observation_id"] == selected)]
    return annotate_image(original, detections)


def review_controls(store, user, run, row, editable, scope):
    identity = f'{scope}-{run["id"]}-{row["observation_id"]}'
    version_key = "review_version_" + identity
    st.session_state.setdefault(version_key, run["version"])
    st.caption(f'Original prediction: {row.get("predicted_class", row["damage_class"])} · Model score: {row["confidence"]:.0%}')
    if row["scope"] == "whole_image_classification":
        st.warning("This prediction labels the whole image; it does not locate an individual building.")
    with st.form("review_" + identity):
        label = st.selectbox("Damage label", CLASSES, index=CLASSES.index(row["damage_class"]), key="label_" + identity, disabled=not editable)
        status = st.selectbox("Review status", STATUSES, index=STATUSES.index(row["review_status"]), key="status_" + identity, disabled=not editable)
        notes = st.text_area("Review notes", value=row.get("review_notes", ""), max_chars=4000, key="notes_" + identity, disabled=not editable)
        submitted = st.form_submit_button("Save review", type="primary", disabled=not editable)
    if submitted:
        try:
            store.review(run["id"], user, st.session_state[version_key], row["observation_id"], label, status, notes)
            st.session_state.pop(version_key, None)
            st.session_state["notice"] = "Review saved. The original prediction is preserved."
            st.rerun()
        except (ValueError, PermissionError) as exc:
            st.error(str(exc))
    if st.session_state[version_key] != run["version"]:
        st.warning("This review has changed since you opened it. Reload before saving.")
        if st.button("Reload review", key="reload_" + identity):
            for prefix in ("label_", "status_", "notes_", "review_version_"):
                st.session_state.pop(prefix + identity, None)
            st.rerun()
    if row.get("reviewed_by"):
        st.caption(f'Last saved by {row["reviewed_by"]} · {row["reviewed_at_utc"]} (UTC)')


def location_controls(store, user, run, image, editable, scope):
    identity = f'{scope}-{run["id"]}-{image["image_id"]}'
    key = "location_version_" + identity
    st.session_state.setdefault(key, run["version"])
    with st.expander("Edit this image's capture location"):
        st.caption("Applies to observations from this image/frame only. This remains a reference point, not a verified building location.")
        with st.form("location_" + identity):
            lat = st.number_input("Image latitude", -90.0, 90.0, float(image.get("latitude") if image.get("latitude") is not None else 0), 0.0001, format="%.4f", key="lat_" + identity, disabled=not editable)
            lon = st.number_input("Image longitude", -180.0, 180.0, float(image.get("longitude") if image.get("longitude") is not None else 0), 0.0001, format="%.4f", key="lon_" + identity, disabled=not editable)
            save = st.form_submit_button("Save image location", disabled=not editable)
        if save:
            try:
                store.locate_image(run["id"], user, st.session_state[key], image["image_id"], lat, lon)
                st.session_state.pop(key, None)
                st.session_state["notice"] = "Image location saved. Original coordinates remain in the audit history."
                st.rerun()
            except (ValueError, PermissionError) as exc:
                st.error(str(exc))
        if st.session_state[key] != run["version"]:
            st.warning("The assessment changed. Reload location fields before saving.")
            if st.button("Reload location", key="reload_location_" + identity):
                for prefix in ("lat_", "lon_", "location_version_"):
                    st.session_state.pop(prefix + identity, None)
                st.rerun()
