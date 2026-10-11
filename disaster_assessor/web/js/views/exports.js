// Export & share: downloads in every format, expiring share links, incident settings.
import { api } from "../api.js";
import { $, CLASSES, COLORS, SHORT, STATUS_LABEL, confirmDialog, esc, fmtTime, icon, modal, pct, plural, toast } from "../ui.js";

let host, ctx;
const st = { reviewedOnly: false, minConf: 0 };
const FORMATS = [
  ["pdf", "Situation report", "PDF for briefings: counts, high-priority table, tasks, limitations.", "PDF", "#e9d25b"],
  ["csv", "Rescue priority list", "Every observation ranked by severity with review status, hazards and crew.", "CSV", "#fff"],
  ["geojson", "Observations (GeoJSON)", "Point features for GIS tools. [lon, lat], WGS84.", "GEO", "#5aa9e6"],
  ["buildings", "Buildings (GeoJSON)", "Footprint polygons with the worst damage class per building.", "BLD", "#5aa9e6"],
  ["kml", "Google Earth (KML)", "Colour-coded placemarks for Google Earth and ATAK.", "KML", "#16a34a"],
  ["shp", "Shapefile (zip)", "Point shapefile with .prj for ArcGIS / QGIS.", "SHP", "#f97316"],
];

export async function render(el, context) {
  host = el;
  ctx = context;
  host.innerHTML = `<div class="page">
    <div class="page-head"><div><h1>Export & share</h1><p class="muted" id="exp-sub"></p></div></div>
    <div class="export-filters card">
      <label class="switch"><input type="checkbox" id="reviewed-only"><span></span>Reviewed observations only (confirmed, corrected, added)</label>
      <label class="field inline">Minimum model score <output id="min-out">0%</output><input type="range" id="min-conf" min="0" max="1" step="0.05" value="0" style="--p:0%"></label>
    </div>
    <div class="format-grid" id="formats"></div>
    <div class="two-col">
      <section class="card pad-card">
        <div class="side-head row"><h3>${icon("share", 16)} Share links</h3>${ctx.store.can("coordinator") ? `<button class="btn ghost sm" id="new-share">${icon("plus", 13)} New link</button>` : ""}</div>
        <p class="sub">Read-only map, summary and data downloads for partner agencies without accounts. Links expire; crew assignments and reviewer names are never shared.</p>
        <ul class="share-list" id="share-list"><li class="fine">${ctx.store.can("coordinator") ? "Loading…" : "Coordinators manage share links."}</li></ul>
      </section>
      <section class="card pad-card">
        <div class="side-head row"><h3>Incident</h3>${ctx.store.can("coordinator") ? `<button class="btn ghost sm" id="edit-incident">Edit</button>` : ""}</div>
        <div id="incident-info"></div>
      </section>
    </div>
  </div>`;
  bind();
  refresh();
  loadShares();
}

export function refresh() {
  if (!host || !host.isConnected) return;
  const s = ctx.store.incident.summary, inc = ctx.store.incident;
  const rows = ctx.store.data.observations.filter((o) => o.confidence >= st.minConf && (st.reviewedOnly ? ["confirmed", "corrected", "added"].includes(o.status) : o.status !== "rejected"));
  $("#exp-sub", host).textContent = `${plural(rows.length, "observation")} match the filters · ${s.reviewed_pct}% of all observations reviewed${s.demo ? " · contains synthetic demo data" : ""}`;
  const q = `?reviewed_only=${st.reviewedOnly}&min_conf=${st.minConf}`;
  $("#formats", host).innerHTML = FORMATS.map(([fmt, title, desc, badge, color]) => `
    <a class="card dl" href="/api/incidents/${inc.id}/export/${fmt}${q}" download>
      <span class="dl-icon" style="background:${color}">${badge}</span>
      <span><b>${esc(title)}</b><span class="sub">${esc(desc)}</span></span>
    </a>`).join("");
  $("#incident-info", host).innerHTML = `<dl class="meta">
      <dt>Name</dt><dd>${esc(inc.name)}</dd>
      <dt>Status</dt><dd>${inc.status === "closed" ? "Closed" : "Active"}</dd>
      <dt>Created</dt><dd>${fmtTime(inc.created_at)}</dd>
      <dt>Map centre</dt><dd>${inc.center_lat != null ? `${inc.center_lat.toFixed(4)}, ${inc.center_lon.toFixed(4)}` : "—"}</dd>
      <dt>Assessments</dt><dd>${s.runs} runs · ${s.images} images</dd>
      <dt>Buildings</dt><dd>${s.buildings} matched of ${s.footprints} footprints</dd>
    </dl>
    <div class="mix-bar big">${[...CLASSES].reverse().map((c) => s.by_class[c] ? `<span style="flex:${s.by_class[c]};background:${COLORS[c]}"></span>` : "").join("") || `<span style="flex:1;background:#2b2c31"></span>`}</div>
    <p class="sub">${[...CLASSES].reverse().map((c) => `${SHORT[c]} ${s.by_class[c]}`).join(" · ")}</p>
    <p class="sub">${Object.entries(s.by_status).filter(([, v]) => v).map(([k, v]) => `${STATUS_LABEL[k]} ${v}`).join(" · ")}</p>`;
}

async function loadShares() {
  if (!ctx.store.can("coordinator")) return;
  try {
    const links = await api.get(`/api/incidents/${ctx.store.incident.id}/share`);
    $("#share-list", host).innerHTML = links.length ? links.map((l) => `<li class="${l.active ? "" : "inactive"}">
      <div><b>${esc(l.label || "Untitled link")}</b><span class="sub">by ${esc(l.creator || "?")} · ${l.active ? `expires ${fmtTime(l.expires_at)}` : "expired or revoked"}</span></div>
      ${l.active ? `<button class="btn ghost sm" data-revoke="${l.id}">Revoke</button>` : ""}</li>`).join("")
      : `<li class="fine">No share links yet.</li>`;
  } catch (err) { toast(err.message, "error"); }
}

function bind() {
  $("#reviewed-only", host).addEventListener("change", (e) => { st.reviewedOnly = e.target.checked; refresh(); });
  $("#min-conf", host).addEventListener("input", (e) => {
    st.minConf = Number(e.target.value);
    e.target.style.setProperty("--p", pct(st.minConf));
    $("#min-out", host).textContent = pct(st.minConf);
    refresh();
  });
  const on = (id, fn) => { const el = $(id, host); if (el) el.addEventListener("click", fn); };
  on("#new-share", async () => {
    const fd = await modal({
      title: "New share link", submit: "Create link",
      body: `<label class="field">Label<input name="label" maxlength="100" placeholder="e.g. County EOC, Red Cross"></label>
        <label class="field">Expires after<select name="hours"><option value="12">12 hours</option><option value="72" selected>3 days</option><option value="168">1 week</option><option value="720">30 days</option></select></label>`,
    });
    if (!fd) return;
    try {
      const link = await api.post(`/api/incidents/${ctx.store.incident.id}/share`, { label: fd.get("label"), hours: Number(fd.get("hours")) });
      const url = link.url || `${location.origin}${link.path}`;
      await modal({ title: "Share link created", submit: null, body: `<p class="muted">Copy it now. For security it is shown only once.</p>
        <label class="field">Link<input readonly value="${esc(url)}" id="share-url"></label>
        <button type="button" class="btn primary" id="copy-share">Copy link</button>`,
      onOpen: (dlg) => dlg.querySelector("#copy-share").addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(url); toast("Copied.", "success"); } catch { dlg.querySelector("#share-url").select(); }
      }) });
      loadShares();
    } catch (err) { toast(err.message, "error"); }
  });
  $("#share-list", host).addEventListener("click", async (e) => {
    const b = e.target.closest("[data-revoke]");
    if (!b || !(await confirmDialog("Revoke link?", "Anyone using it will lose access immediately.", "Revoke"))) return;
    try { await api.del(`/api/share/${b.dataset.revoke}`); loadShares(); } catch (err) { toast(err.message, "error"); }
  });
  on("#edit-incident", async () => {
    const inc = ctx.store.incident;
    const fd = await modal({
      title: "Edit incident", submit: "Save",
      body: `<label class="field">Name<input name="name" required minlength="2" maxlength="120" value="${esc(inc.name)}"></label>
        <label class="field">Description<textarea name="description" rows="3" maxlength="2000">${esc(inc.description)}</textarea></label>
        <div class="two"><label class="field">Centre latitude<input name="lat" type="number" step="any" value="${inc.center_lat ?? ""}"></label>
        <label class="field">Centre longitude<input name="lon" type="number" step="any" value="${inc.center_lon ?? ""}"></label></div>
        <label class="field">Status<select name="status"><option value="active" ${inc.status === "active" ? "selected" : ""}>Active</option><option value="closed" ${inc.status === "closed" ? "selected" : ""}>Closed</option></select></label>
        <p class="note">Closed incidents stay readable. If a retention period is configured, closed incidents are deleted after it.</p>`,
    });
    if (!fd) return;
    try {
      const lat = fd.get("lat"), lon = fd.get("lon");
      ctx.store.incident = await api.patch(`/api/incidents/${inc.id}`, {
        name: fd.get("name"), description: fd.get("description"), status: fd.get("status"),
        center_lat: lat === "" ? null : Number(lat), center_lon: lon === "" ? null : Number(lon),
      });
      toast("Incident updated.", "success");
      refresh();
    } catch (err) { toast(err.message, "error"); }
  });
}
