// Incident list: create, open, close and see progress at a glance.
import { api } from "../api.js";
import { COLORS, CLASSES, SHORT, ago, emptyState, esc, icon, modal, plural, toast } from "../ui.js";

let host, ctx;

export async function render(el, context) {
  host = el;
  ctx = context;
  host.innerHTML = `<div class="page">
    <div class="page-head">
      <div><h1>Incidents</h1><p class="muted">Each incident holds its imagery, review decisions, crews and tasks.</p></div>
      ${ctx.store.can("coordinator") ? `<button class="btn primary" id="new-incident">${icon("plus", 16)} New incident</button>` : ""}
    </div>
    <div id="incident-grid" class="incident-grid"><div class="skeleton"></div><div class="skeleton"></div></div>
  </div>`;
  const btn = host.querySelector("#new-incident");
  if (btn) btn.addEventListener("click", createIncident);
  await refresh();
}

export async function refresh() {
  let list;
  try { list = await api.get("/api/incidents"); }
  catch (err) { toast(err.message, "error"); return; }
  ctx.store.incidents = list;
  const grid = host.querySelector("#incident-grid");
  if (!grid) return;
  if (!list.length) {
    grid.innerHTML = emptyState("No incidents yet",
      ctx.store.can("coordinator") ? "Create one to start assessing imagery." : "Ask a coordinator to create an incident.",
      ctx.store.can("coordinator") ? `<button class="btn primary" id="empty-new">Create the first incident</button>` : "");
    const b = grid.querySelector("#empty-new");
    if (b) b.addEventListener("click", createIncident);
    return;
  }
  grid.innerHTML = list.map(card).join("");
}

function card(inc) {
  const s = inc.summary, total = CLASSES.reduce((n, c) => n + s.by_class[c], 0);
  const bar = total ? [...CLASSES].reverse().map((c) => s.by_class[c] ? `<span style="flex:${s.by_class[c]};background:${COLORS[c]}" title="${SHORT[c]} ${s.by_class[c]}"></span>` : "").join("")
    : `<span style="flex:1;background:#2b2c31"></span>`;
  const last = s.timeline.length ? s.timeline[s.timeline.length - 1].created_at : inc.created_at;
  const open = Object.entries(s.tasks).filter(([k]) => !["done", "cancelled", "searched_clear"].includes(k)).reduce((n, [, v]) => n + v, 0);
  return `<a class="card incident-card${inc.status === "closed" ? " closed" : ""}" href="#/i/${inc.id}/console">
    <div class="ic-head"><h2>${esc(inc.name)}</h2><span class="pill ${inc.status === "closed" ? "" : "live-pill"}">${inc.status === "closed" ? "Closed" : "Active"}</span></div>
    <p class="muted ic-desc">${esc(inc.description) || "No description"}</p>
    <div class="mix-bar">${bar}</div>
    <dl class="ic-stats">
      <div><dt>Observations</dt><dd>${s.observations}</dd></div>
      <div><dt>High-priority</dt><dd class="yellow">${s.high_priority}</dd></div>
      <div><dt>Reviewed</dt><dd>${s.reviewed_pct}%</dd></div>
      <div><dt>Open tasks</dt><dd>${open}</dd></div>
    </dl>
    <p class="fine">${plural(s.runs, "assessment")} · last activity ${ago(last)}${s.demo ? " · includes demo data" : ""}</p>
  </a>`;
}

async function createIncident() {
  const fd = await modal({
    title: "New incident", submit: "Create incident",
    body: `<label class="field">Name<input name="name" required minlength="2" maxlength="120" placeholder="e.g. Hurricane Delta – Lake Charles"></label>
      <label class="field">Description<textarea name="description" rows="3" maxlength="2000" placeholder="Area, date, responding agencies"></textarea></label>
      <div class="two">
        <label class="field">Map centre latitude<input name="lat" type="number" step="any" min="-90" max="90" placeholder="optional"></label>
        <label class="field">Map centre longitude<input name="lon" type="number" step="any" min="-180" max="180" placeholder="optional"></label>
      </div>
      <p class="note">The centre is used to place demo data and as a default map view. Real imagery is located from GPS, drone telemetry or GeoTIFF georeferencing.</p>`,
  });
  if (!fd) return;
  const lat = fd.get("lat"), lon = fd.get("lon");
  try {
    const inc = await api.post("/api/incidents", {
      name: fd.get("name"), description: fd.get("description"),
      center_lat: lat === "" ? null : Number(lat), center_lon: lon === "" ? null : Number(lon),
    });
    toast(`Created “${inc.name}”.`, "success");
    ctx.go(`#/i/${inc.id}/console`);
  } catch (err) { toast(err.message, "error"); }
}
