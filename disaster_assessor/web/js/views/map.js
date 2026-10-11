// Rescue map: capture points, building footprints, density heatmap, tasks, routes and before/after imagery.
import { api, upload } from "../api.js";
import {
  $, CLASSES, COLORS, LOCATION_LABEL, SEVERITY, SHORT, TASK_LABEL, classPill, esc, icon, modal, pct, plural,
  statusPill, toast, validCoord,
} from "../ui.js";
import { buildingStyle, clusterIcon, darkTiles, groupByPoint, pointIcon } from "../maputil.js";

let host, ctx, map, tiles, layers = {}, overlays = [], swipe = null, markerByKey = new Map(), markerByObs = new Map();
const st = {
  tiles: "dark", hidden: new Set(), reviewedOnly: false, runId: "", tab: "points",
  show: { points: true, buildings: true, heat: false, tasks: true, route: true, pre: false, post: true }, swipeX: 0.5,
  footprints: null, buildings: [], route: null, selectedKey: null,
};

const obs = () => ctx.store.data.observations.filter((o) => o.status !== "rejected"
  && !st.hidden.has(o.damage_class)
  && (!st.reviewedOnly || ["confirmed", "corrected", "added"].includes(o.status))
  && (!st.runId || String(o.run_id) === st.runId));

export async function render(el, context) {
  host = el;
  ctx = context;
  const coordinator = ctx.store.can("coordinator");
  host.innerHTML = `<section class="map-grid">
    <div class="card map-card">
      <div id="bigmap"></div>
      <div class="map-top">
        <div class="map-filters">
          <div class="chips" role="group" aria-label="Filter by severity" id="sev-chips"></div>
          <div class="map-selects">
            <label class="glass-select"><span class="sr-only">Assessment run</span><select id="run-filter"></select></label>
            <label class="glass-select"><span class="sr-only">Review status</span><select id="review-filter">
              <option value="">All non-rejected</option><option value="reviewed">Reviewed only</option></select></label>
          </div>
        </div>
        <div class="map-right">
          <div class="segmented glass" id="tile-switch"><button class="seg active" data-t="dark">Dark</button><button class="seg" data-t="streets">Streets</button><button class="seg" data-t="off">Off</button></div>
          <button class="glass icon-btn lg" id="layers-btn" aria-label="Layers" aria-expanded="false">${icon("layers", 17)}</button>
          <div class="layers-pop glass" id="layers-pop" hidden></div>
        </div>
      </div>
      <div class="swipe" id="swipe" hidden><div class="swipe-line"></div><input type="range" id="swipe-range" min="0" max="1" step="0.001" value="0.5" aria-label="Before / after comparison"><span class="swipe-tag l">BEFORE</span><span class="swipe-tag r">AFTER</span></div>
      <div class="map-legend glass" id="legend"></div>
      <div class="map-empty" id="map-empty" hidden></div>
    </div>
    <aside class="card side-list">
      <div class="side-head"><h3>Rescue priority</h3><p class="sub" id="map-sub"></p></div>
      <div class="segmented full" id="list-tabs">
        <button class="seg active" data-tab="points">Capture points</button><button class="seg" data-tab="obs">Observations</button><button class="seg" data-tab="buildings">Buildings</button>
      </div>
      <ol class="obs-list" id="obs-list"></ol>
      ${coordinator ? `<div class="tool-row">
        <button class="btn ghost sm" id="osm-btn">${icon("layers", 13)} Import OSM buildings</button>
        <button class="btn ghost sm" id="fp-btn">${icon("upload", 13)} Footprints</button>
        <button class="btn ghost sm" id="ov-btn">${icon("upload", 13)} Imagery</button>
        <button class="btn ghost sm" id="route-btn">${icon("route", 13)} Plan route</button>
      </div>` : ""}
      <p class="fine">Points mark where the camera was unless the source is an orthomosaic or a telemetry projection. Building colours use the most severe non-rejected observation.</p>
    </aside>
  </section>`;
  initMap();
  bind();
  await loadGeo();
  refresh();
}

export function destroy() {
  if (map) { map.remove(); map = null; }
  layers = {}; overlays = []; swipe = null;
}

export async function refresh(keys) {
  if (!host || !host.isConnected || !map) return;
  if (keys && (keys.includes("overlays") || keys.includes("observations"))) await loadGeo();
  renderControls();
  drawPoints();
  drawBuildings();
  drawHeat();
  drawTasks();
  drawRoute();
  drawOverlays();
  renderList();
}

export function onEvent(evt) {
  if (evt.type === "buildings.updated") { loadGeo().then(() => refresh()); return false; }
  return undefined;
}

function initMap() {
  const inc = ctx.store.incident;
  map = L.map($("#bigmap", host), { zoomControl: false, maxZoom: 19, minZoom: 2, preferCanvas: false })
    .setView([inc.center_lat ?? 37.7749, inc.center_lon ?? -122.4194], 13);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  L.control.scale({ position: "bottomleft", imperial: false }).addTo(map);
  map.createPane("prePane").style.zIndex = 250;
  map.createPane("postPane").style.zIndex = 260;
  setTiles();
  layers.points = L.markerClusterGroup({ showCoverageOnHover: false, maxClusterRadius: 44, iconCreateFunction: clusterIcon }).addTo(map);
  layers.buildings = L.layerGroup().addTo(map);
  layers.tasks = L.layerGroup().addTo(map);
  layers.route = L.layerGroup().addTo(map);
  map.on("move zoom resize", positionSwipe);
  let fitted = false;
  map.whenReady(() => setTimeout(() => {
    map.invalidateSize();
    if (!fitted) { fitted = true; fitAll(); }
  }, 50));
}

function setTiles() {
  if (tiles) map.removeLayer(tiles);
  tiles = st.tiles === "off" ? null : darkTiles(ctx.store.config, st.tiles === "dark").addTo(map);
  document.querySelectorAll("#tile-switch .seg").forEach((b) => b.classList.toggle("active", b.dataset.t === st.tiles));
}

async function loadGeo() {
  const id = ctx.store.incident.id;
  try {
    [st.footprints, st.buildings, overlays] = await Promise.all([
      api.get(`/api/incidents/${id}/footprints`), api.get(`/api/incidents/${id}/buildings`), api.get(`/api/incidents/${id}/overlays`),
    ]);
  } catch (err) { toast(err.message, "error"); }
}

function fitAll() {
  const pts = obs().filter((o) => validCoord(o.latitude, o.longitude)).map((o) => [o.latitude, o.longitude]);
  overlays.forEach((ov) => { if (ov.bounds) pts.push(...ov.bounds); });
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.2), { maxZoom: 17 });
}

// ---------- layers ----------

function popupFor(point) {
  const first = point.list[0];
  const items = point.list.slice(0, 12).map((o) => `<li><a href="${ctx.incidentPath("review", `?image=${o.image_id}&obs=${o.id}`)}">
    <span class="pip" style="background:${COLORS[o.damage_class]}"></span><b>${esc(SHORT[o.damage_class])}</b><span>${pct(o.confidence)}</span>
    <em>${esc(o.status.replace("_", " "))}${o.hazards.length ? ` · ⚠ ${o.hazards.length}` : ""}</em></a></li>`).join("");
  return `<div class="popup" style="width:260px">
    <div class="tag">${first.is_demo ? "SYNTHETIC DEMO" : "MODEL OUTPUT · REVIEW STATUS SHOWN"}</div>
    <img src="/api/images/${first.image_id}/file?variant=preview" alt="Preview of ${esc(first.source_file || "")}">
    <h4>${plural(point.list.length, "observation")} at this point</h4>
    <ul class="pop-list">${items}</ul>
    <div class="meta">${esc(first.source_file || "")}${first.frame_seconds != null ? ` · ${Number(first.frame_seconds).toFixed(1)}s` : ""}<br>
      ${esc(LOCATION_LABEL[first.location_source] || first.location_source)} · ${point.lat.toFixed(5)}, ${point.lon.toFixed(5)}</div>
    <div class="meta fine">${["ortho_georef", "telemetry_projection"].includes(first.location_source) ? "Projected building position (approximate)." : "Camera or reference point, not a verified building location."}</div>
  </div>`;
}

function drawPoints() {
  layers.points.clearLayers();
  markerByKey = new Map();
  markerByObs = new Map();
  if (!st.show.points) return;
  groupByPoint(obs()).forEach((p) => {
    const m = L.marker([p.lat, p.lon], { icon: pointIcon(p, p.key === st.selectedKey), worst: p.worst, count: p.list.length,
      title: `${plural(p.list.length, "observation")}, most severe ${p.worst}`, keyboard: true });
    m.bindPopup(() => popupFor(p), { maxWidth: 300, autoPanPaddingTopLeft: [20, 90] });
    m.on("click", () => { st.selectedKey = p.key; });
    layers.points.addLayer(m);
    markerByKey.set(p.key, m);
    p.list.forEach((o) => markerByObs.set(o.id, m));
  });
  $("#map-empty", host).hidden = obs().some((o) => validCoord(o.latitude, o.longitude)) || ctx.store.data.observations.length === 0 ? true : false;
  if (!ctx.store.data.observations.length) {
    const e = $("#map-empty", host);
    e.hidden = false;
    e.innerHTML = `<h2>Your map starts with imagery</h2><p>Run an assessment from the console to place observations here.</p>`;
  } else if (!obs().some((o) => validCoord(o.latitude, o.longitude))) {
    const e = $("#map-empty", host);
    e.hidden = false;
    e.innerHTML = `<h2>No coordinates for these observations</h2><p>Use images with GPS/drone telemetry, a GeoTIFF, or set a manual reference point in the console's run settings.</p>`;
  }
}

function drawBuildings() {
  layers.buildings.clearLayers();
  if (!st.show.buildings || !st.footprints) return;
  const rolled = new Map(st.buildings.filter((b) => !st.hidden.has(b.damage_class)).map((b) => [b.footprint_id, b]));
  L.geoJSON(st.footprints, {
    style: (f) => {
      const b = rolled.get(f.properties.id);
      return b ? buildingStyle(b.damage_class, b.reviewed === b.count) : { color: "#5d626b", weight: 0.8, fillOpacity: 0.04, fillColor: "#9a9fa8" };
    },
    onEachFeature: (f, layer) => {
      const b = rolled.get(f.properties.id);
      if (b) layer.bindPopup(`<div class="popup"><h4>${classPill(b.damage_class)}</h4><div class="meta">${plural(b.count, "observation")} · ${b.reviewed}/${b.count} reviewed · max ${pct(b.confidence)}<br>Footprint ${esc(b.external_id)}</div></div>`);
    },
  }).addTo(layers.buildings);
}

function drawHeat() {
  if (layers.heat) { map.removeLayer(layers.heat); layers.heat = null; }
  if (!st.show.heat || typeof L.heatLayer !== "function") return;
  const pts = obs().filter((o) => validCoord(o.latitude, o.longitude)).map((o) => [o.latitude, o.longitude, (SEVERITY[o.damage_class] + 1) / 4]);
  layers.heat = L.heatLayer(pts, { radius: 28, blur: 22, maxZoom: 17, gradient: { 0.25: "#16a34a", 0.5: "#eab308", 0.75: "#f97316", 1: "#dc2626" } }).addTo(map);
}

function drawTasks() {
  layers.tasks.clearLayers();
  if (!st.show.tasks) return;
  ctx.store.data.tasks.filter((t) => validCoord(t.latitude, t.longitude) && !["cancelled"].includes(t.status)).forEach((t) => {
    const done = ["done", "searched_clear"].includes(t.status);
    const m = L.marker([t.latitude, t.longitude], { icon: L.divIcon({ className: "", iconSize: [22, 22], iconAnchor: [-8, 30],
      html: `<span class="task-pin ${t.status}" style="--c:${COLORS[t.damage_class] || "#5aa9e6"}">${done ? "✓" : t.status === "needs_usar" ? "!" : "T"}</span>` }), zIndexOffset: 500 });
    m.bindPopup(`<div class="popup"><div class="tag">TASK #${t.id}</div><h4>${esc(t.title)}</h4><div class="meta">${esc(TASK_LABEL[t.status])} · ${esc(t.crew || "unassigned")}</div>
      <a href="${ctx.incidentPath("tasks", `?task=${t.id}`)}">Open task</a></div>`);
    layers.tasks.addLayer(m);
  });
}

function drawRoute() {
  layers.route.clearLayers();
  if (!st.show.route || !st.route) return;
  const r = st.route;
  if (r.road && r.road.geometry) {
    L.geoJSON(r.road.geometry, { style: { color: "#5aa9e6", weight: 4, opacity: 0.9 } }).addTo(layers.route);
  } else {
    const line = [];
    if (r.start) line.push(r.start);
    r.stops.forEach((s) => line.push([s.latitude, s.longitude]));
    L.polyline(line, { color: "#5aa9e6", weight: 3, dashArray: "8 6", opacity: 0.9 }).addTo(layers.route);
  }
  r.stops.forEach((s, i) => L.marker([s.latitude, s.longitude], { interactive: false, zIndexOffset: 800,
    icon: L.divIcon({ className: "", iconSize: [20, 20], html: `<span class="route-num">${i + 1}</span>` }) }).addTo(layers.route));
}

function drawOverlays() {
  overlays.forEach((ov) => {
    const want = st.show[ov.kind] && ov.bounds;
    if (want && !ov.layer) {
      ov.layer = L.imageOverlay(`/api/overlays/${ov.id}/file`, ov.bounds, { pane: ov.kind === "pre" ? "prePane" : "postPane", opacity: 0.95, interactive: false });
    }
    if (ov.layer) {
      if (want && !map.hasLayer(ov.layer)) ov.layer.addTo(map);
      if (!want && map.hasLayer(ov.layer)) map.removeLayer(ov.layer);
    }
  });
  const both = st.show.pre && st.show.post && overlays.some((o) => o.kind === "pre") && overlays.some((o) => o.kind === "post");
  $("#swipe", host).hidden = !both;
  swipe = both;
  positionSwipe();
}

// Before/after swipe: clip the post-event pane to the right of the slider, in pane coordinates.
function positionSwipe() {
  const pane = map && map.getPane("postPane");
  if (!pane) return;
  if (!swipe) { pane.style.clip = ""; return; }
  const size = map.getSize(), x = size.x * st.swipeX;
  const nw = map.containerPointToLayerPoint([0, 0]), se = map.containerPointToLayerPoint(size), clipX = map.containerPointToLayerPoint([x, 0]).x;
  pane.style.clip = `rect(${nw.y}px, ${se.x}px, ${se.y}px, ${clipX}px)`;
  $(".swipe-line", host).style.left = `${x}px`;
}

// ---------- controls & list ----------

function renderControls() {
  const all = ctx.store.data.observations.filter((o) => o.status !== "rejected");
  $("#sev-chips", host).innerHTML = [...CLASSES].reverse().map((c) => `<button class="chip-btn glass${st.hidden.has(c) ? " off" : ""}" data-cls="${c}" aria-pressed="${!st.hidden.has(c)}">
    <span class="pip" style="background:${COLORS[c]}"></span>${SHORT[c]}<span class="mono">${all.filter((o) => o.damage_class === c).length}</span></button>`).join("");
  const runs = ctx.store.data.runs.filter((r) => r.status === "done");
  $("#run-filter", host).innerHTML = `<option value="">All assessments (${runs.length})</option>` +
    runs.map((r) => `<option value="${r.id}" ${String(r.id) === st.runId ? "selected" : ""}>Run #${r.id} · ${new Date(r.created_at).toLocaleDateString()} · ${r.observations} obs</option>`).join("");
  const hasPre = overlays.some((o) => o.kind === "pre"), hasPost = overlays.some((o) => o.kind === "post");
  $("#layers-pop", host).innerHTML = [
    ["points", "Capture points", true], ["buildings", `Building footprints (${st.footprints ? st.footprints.features.length : 0})`, true],
    ["heat", "Damage density heatmap", typeof L.heatLayer === "function"], ["tasks", `Tasks (${ctx.store.data.tasks.length})`, true],
    ["route", "Planned route", !!st.route], ["pre", "Pre-event imagery", hasPre], ["post", "Post-event imagery", hasPost],
  ].map(([k, label, ok]) => `<label class="switch small${ok ? "" : " dim"}"><input type="checkbox" data-layer="${k}" ${st.show[k] ? "checked" : ""} ${ok ? "" : "disabled"}><span></span>${esc(label)}</label>`).join("")
    + (hasPre && hasPost ? `<p class="fine">Turn on both imagery layers to compare with the slider.</p>` : "");
  $("#legend", host).innerHTML = `<span class="lg-title">Legend</span>${[...CLASSES].reverse().map((c) => `<span>${`<i style="background:${COLORS[c]}"></i>`}${SHORT[c]}</span>`).join("")}
    <span><i class="ring"></i>Split ring = mixed damage at one point</span>${st.footprints && st.footprints.features.length ? `<span><i class="dash"></i>Dashed outline = not fully reviewed</span>` : ""}`;
}

function renderList() {
  const list = $("#obs-list", host), rows = obs();
  document.querySelectorAll("#list-tabs .seg").forEach((b) => b.classList.toggle("active", b.dataset.tab === st.tab));
  const mapped = rows.filter((o) => validCoord(o.latitude, o.longitude)).length;
  $("#map-sub", host).textContent = `${mapped} mapped of ${rows.length} · most severe first`;
  if (st.tab === "buildings") {
    const b = st.buildings.filter((x) => !st.hidden.has(x.damage_class));
    list.innerHTML = b.length ? b.map((x, i) => `<li><button data-lat="${x.centroid[0]}" data-lon="${x.centroid[1]}">
      <span class="rank">${i + 1}</span><span class="swatch" style="background:${COLORS[x.damage_class]}"></span>
      <span><span class="cls">${esc(SHORT[x.damage_class])}</span><span class="name">${plural(x.count, "observation")} · ${x.reviewed}/${x.count} reviewed</span></span>
      <span class="score">${pct(x.confidence)}</span></button></li>`).join("")
      : `<li class="fine pad">No buildings matched yet. Import OpenStreetMap buildings or upload footprints to group observations by building.</li>`;
  } else if (st.tab === "points") {
    const pts = groupByPoint(rows).sort((a, b) => SEVERITY[b.worst] - SEVERITY[a.worst] || b.list.length - a.list.length);
    list.innerHTML = pts.map((p, i) => `<li><button data-key="${p.key}">
      <span class="rank">${i + 1}</span><img src="/api/images/${p.list[0].image_id}/file?variant=preview" alt="" loading="lazy">
      <span><span class="cls">${pip(p.worst)}${esc(SHORT[p.worst])}</span><span class="name">${plural(p.list.length, "observation")} · ${esc(p.list[0].source_file || "")}</span></span>
      <span class="score">${p.list.filter((o) => o.status === "not_reviewed").length ? `${p.list.filter((o) => o.status === "not_reviewed").length} open` : "✓"}</span></button></li>`).join("")
      || `<li class="fine pad">No mapped observations.</li>`;
  } else {
    list.innerHTML = rows.slice(0, 400).map((o, i) => `<li><button data-obs="${o.id}">
      <span class="rank">${i + 1}</span><img src="/api/images/${o.image_id}/file?variant=preview" alt="" loading="lazy">
      <span><span class="cls">${pip(o.damage_class)}${esc(SHORT[o.damage_class])}</span><span class="name">${esc(o.status.replace("_", " "))} · ${esc(o.source_file || "")}</span></span>
      <span class="score">${pct(o.confidence)}</span></button></li>`).join("") || `<li class="fine pad">No observations.</li>`;
  }
}

function pip(c) { return `<span class="pip" style="background:${COLORS[c]}"></span>`; }

function focusMarker(m) {
  if (!m) return toast("That observation has no coordinates.", "warn");
  layers.points.zoomToShowLayer(m, () => m.openPopup());
}

async function planRoute() {
  const crews = ctx.store.data.crews;
  const fd = await modal({
    title: "Plan a route", submit: "Plan route",
    body: `<label class="field">Crew<select name="crew"><option value="">All open tasks</option>${crews.map((c) => `<option value="${c.id}">${esc(c.name)} (${c.open_tasks})</option>`).join("")}</select></label>
      <div class="two"><label class="field">Start latitude<input name="lat" type="number" step="any" placeholder="map centre"></label>
      <label class="field">Start longitude<input name="lon" type="number" step="any" placeholder="map centre"></label></div>
      <label class="switch"><input type="checkbox" name="road" ${ctx.store.config.integrations.osrm ? "" : "disabled"}><span></span>Follow roads (OSRM)</label>
      <p class="note">${ctx.store.config.integrations.osrm ? "Road routing uses pre-disaster road data; it cannot see closures or debris." : "Straight-line ordering: most severe first, nearest next. Configure DRA_OSRM_URL for road routing."}</p>`,
  });
  if (!fd) return;
  const c = map.getCenter(), lat = fd.get("lat") || c.lat, lon = fd.get("lon") || c.lng;
  const q = new URLSearchParams({ start_lat: lat, start_lon: lon, road: fd.get("road") === "on" });
  if (fd.get("crew")) q.set("crew_id", fd.get("crew"));
  try {
    const plan = await api.get(`/api/incidents/${ctx.store.incident.id}/route?${q}`);
    if (!plan.stops.length) return toast("No open tasks with coordinates to route.", "warn");
    st.route = { ...plan, start: [Number(lat), Number(lon)] };
    st.show.route = true;
    if (plan.road_error) toast(`Road routing failed (${plan.road_error}); showing straight-line order.`, "warn");
    toast(`Route: ${plural(plan.stops.length, "stop")} · ${(plan.total_distance_m / 1000).toFixed(1)} km straight-line${plan.unrouted.length ? ` · ${plan.unrouted.length} without coordinates` : ""}.`, "success");
    refresh();
    map.fitBounds(L.latLngBounds(plan.stops.map((s) => [s.latitude, s.longitude]).concat([st.route.start])).pad(0.2));
  } catch (err) { toast(err.message, "error"); }
}

async function importOsm() {
  const b = map.getBounds();
  if (b.getNorth() - b.getSouth() > 0.05 || b.getEast() - b.getWest() > 0.05) {
    return toast("Zoom in further (≈5 km across or less) to import buildings for the visible area.", "warn");
  }
  try {
    toast("Requesting OpenStreetMap buildings…");
    const r = await api.post(`/api/incidents/${ctx.store.incident.id}/footprints/osm`, { south: b.getSouth(), west: b.getWest(), north: b.getNorth(), east: b.getEast() });
    toast(`Imported ${r.added} new buildings; matched ${r.matched_observations} observations.`, "success");
    await loadGeo();
    await ctx.reload(["observations", "summary"]);
    refresh();
  } catch (err) { toast(err.message, "error"); }
}

async function uploadFile(kind) {
  const isFootprint = kind === "footprints";
  const fd = await modal({
    title: isFootprint ? "Upload building footprints" : "Upload pre/post-event imagery", submit: "Upload",
    body: isFootprint
      ? `<label class="field">GeoJSON FeatureCollection of polygons<input type="file" name="file" accept=".geojson,.json,application/geo+json" required></label>
         <p class="note">e.g. Microsoft Global ML Building Footprints or a county parcel export, clipped to the incident area.</p>`
      : `<div class="segmented full"><label class="seg active"><input type="radio" name="kind" value="pre" checked>Before (pre-event)</label><label class="seg"><input type="radio" name="kind" value="post">After (post-event)</label></div>
         <label class="field">GeoTIFF (EPSG:4326, 3857 or UTM)<input type="file" name="file" accept=".tif,.tiff" required></label>
         <p class="note">Large mosaics are downsampled for display. Upload post-event GeoTIFFs through the console to also run damage detection on them.</p>`,
    onOpen: (dlg) => dlg.querySelectorAll(".seg input").forEach((r) => r.addEventListener("change", () =>
      dlg.querySelectorAll(".seg").forEach((s) => s.classList.toggle("active", s.querySelector("input").checked)))),
  });
  if (!fd) return;
  const file = fd.get("file");
  const body = new FormData();
  body.append("file", file, file.name);
  if (!isFootprint) body.append("kind", fd.get("kind"));
  try {
    const url = `/api/incidents/${ctx.store.incident.id}/${isFootprint ? "footprints/upload" : "overlays"}`;
    const r = await upload(url, body, (p) => p < 1 && toast(`Uploading… ${pct(p)}`, "info", 1200));
    toast(isFootprint ? `Loaded ${r.added} buildings; matched ${r.matched_observations} observations.` : `Added ${r.kind}-event imagery.`, "success");
    if (!isFootprint) st.show[r.kind] = true;
    await loadGeo();
    await ctx.reload(["observations", "summary"]);
    refresh();
    if (!isFootprint && r.bounds) map.fitBounds(r.bounds);
  } catch (err) { toast(err.message, "error"); }
}

function bind() {
  $("#tile-switch", host).addEventListener("click", (e) => {
    const b = e.target.closest(".seg");
    if (b) { st.tiles = b.dataset.t; setTiles(); }
  });
  $("#sev-chips", host).addEventListener("click", (e) => {
    const b = e.target.closest(".chip-btn");
    if (!b) return;
    const c = b.dataset.cls;
    if (st.hidden.has(c)) st.hidden.delete(c); else st.hidden.add(c);
    refresh();
  });
  $("#run-filter", host).addEventListener("change", (e) => { st.runId = e.target.value; refresh(); fitAll(); });
  $("#review-filter", host).addEventListener("change", (e) => { st.reviewedOnly = e.target.value === "reviewed"; refresh(); });
  const pop = $("#layers-pop", host), lb = $("#layers-btn", host);
  lb.addEventListener("click", () => { pop.hidden = !pop.hidden; lb.setAttribute("aria-expanded", String(!pop.hidden)); });
  pop.addEventListener("change", (e) => {
    const k = e.target.dataset.layer;
    if (!k) return;
    st.show[k] = e.target.checked;
    refresh();
  });
  $("#swipe-range", host).addEventListener("input", (e) => { st.swipeX = Number(e.target.value); positionSwipe(); });
  $("#list-tabs", host).addEventListener("click", (e) => {
    const b = e.target.closest(".seg");
    if (b) { st.tab = b.dataset.tab; renderList(); }
  });
  $("#obs-list", host).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.key) { st.selectedKey = b.dataset.key; focusMarker(markerByKey.get(b.dataset.key)); }
    else if (b.dataset.obs) focusMarker(markerByObs.get(Number(b.dataset.obs)));
    else if (b.dataset.lat) map.flyTo([Number(b.dataset.lat), Number(b.dataset.lon)], 18);
  });
  const on = (id, fn) => { const el = $(id, host); if (el) el.addEventListener("click", fn); };
  on("#osm-btn", importOsm);
  on("#fp-btn", () => uploadFile("footprints"));
  on("#ov-btn", () => uploadFile("overlay"));
  on("#route-btn", planRoute);
}
