// Mission console: viewport with HUD, assessment controls with live job progress, telemetry, severity dial.
import { api, upload } from "../api.js";
import {
  $, CLASSES, COLORS, LOCATION_LABEL, SEVERITY, SHORT, STATUS_COLOR, dms, esc, fmtBytes, icon, modal, pad2, pct,
  plural, toast, validCoord,
} from "../ui.js";
import { darkTiles } from "../maputil.js";

const SVGNS = "http://www.w3.org/2000/svg";
const CHIP = { "No Damage": "N", "Minor Damage": "m", "Major Damage": "M", "Destroyed": "D" };
const PREFS_KEY = "dra-console-prefs";
let host, ctx, mini, miniLayer;
const st = { idx: 0, filter: "all", minConf: 0.5, hidden: new Set(), boxes: true, labels: true, grid: true, hud: true, queue: [], uploading: null };

function prefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch { return {}; }
}
const claudeReady = () => !!(ctx && ctx.store.config.integrations && ctx.store.config.integrations.claude_detect);
// Default to Claude vision when the server has a key; otherwise the synthetic demo detector.
function mode(p = prefs()) {
  if (p.mode === "claude" && !claudeReady()) return "demo";
  return p.mode || (claudeReady() ? "claude" : "demo");
}

function savePrefs(p) { try { localStorage.setItem(PREFS_KEY, JSON.stringify({ ...prefs(), ...p })); } catch { /* private mode */ } }

const images = () => ctx.store.data.images.filter((i) => i.kind !== "field_photo");
const navImages = () => images().filter((i) => st.filter === "all" || i.kind === st.filter);
const visible = () => ctx.store.data.observations.filter((o) => o.confidence >= st.minConf - 1e-9 && o.status !== "rejected");
const activeRun = () => ctx.store.data.runs.find((r) => r.status === "running" || r.status === "queued");

export async function render(el, context) {
  host = el;
  ctx = context;
  st.minConf = prefs().minConf ?? 0.5;
  st.hud = !window.matchMedia("(max-width: 760px)").matches;
  if (ctx.params.image) {
    const i = images().findIndex((im) => im.id === Number(ctx.params.image));
    if (i >= 0) st.idx = i;
  }
  host.innerHTML = template();
  bind();
  refresh();
}

export function destroy() {
  if (mini) { mini.remove(); mini = null; }
  document.removeEventListener("keydown", onKey);
}

export function refresh() {
  if (!host || !host.isConnected) return;
  renderViewport();
  renderUnit();
  renderTelemetry();
  renderDial();
  renderMini();
}

export function onEvent(evt) {
  if (evt.type === "run.progress" && evt.run_id) {
    const run = ctx.store.data.runs.find((r) => r.id === evt.run_id);
    if (run) Object.assign(run, { progress: evt.progress, message: evt.message, status: "running" });
    renderUnit();
  }
}

function template() {
  return `<section class="console-grid">
    <div class="card viewport" id="viewport">
      <div class="vp-backdrop" id="vp-backdrop"></div>
      <svg class="vp-stage" id="vp-stage" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Assessed image with detection boxes"></svg>
      <div class="vp-grid" aria-hidden="true"><span></span><span></span><span></span><span></span><div class="reticle"><i></i><b></b><i></i></div></div>
      <aside class="hud" id="hud">
        <a class="hud-chip" id="hud-mode" href="#">DEMO</a>
        <p class="hud-res"><span class="yellow" id="hud-res">—</span> · <span id="hud-kind">—</span></p>
        <p class="hud-label">CONFIDENCE ≥</p>
        <div class="hud-level"><div class="track"><div class="fill" id="hud-level"></div></div><span id="hud-level-val">50%</span></div>
        <div class="hud-chips" id="hud-classes"></div>
        <p class="hud-label">REVIEW</p>
        <div class="hud-review" id="hud-review"></div>
        <div class="hud-histo"><span class="histo-label">LUMA</span><span class="corner tl"></span><span class="corner tr"></span><span class="corner bl"></span><span class="corner br"></span>
          <svg id="histo" viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true"><path d=""/></svg></div>
      </aside>
      <div class="vp-tr">
        <a class="vp-btn" id="open-review" title="Review this image" aria-label="Review this image">${icon("review", 16)}</a>
        <button class="vp-btn red" id="toggle-boxes" aria-pressed="true" title="Detection boxes (B)" aria-label="Toggle detection boxes">${icon("box", 16)}</button>
        <div class="vp-pill"><span class="rec"></span><span id="obs-chip">00 OBS</span></div>
      </div>
      <div class="vp-bl" id="vp-caption"></div>
      <div class="vp-br"><span class="pager" id="pager"></span></div>
      <div class="dropzone" id="dropzone">
        <div class="drop-inner">
          <div class="drop-icon">${icon("upload", 22)}</div>
          <h2>Drop drone footage, photos or GeoTIFFs</h2>
          <p>JPG · PNG · WebP · TIFF/GeoTIFF · MP4 · MOV (+ DJI .SRT telemetry). Up to 40 files / 2 GB per run.</p>
          <div class="drop-actions">
            <button class="btn primary" id="browse">Choose files</button>
            <button class="btn ghost" id="load-demo">Run the synthetic demo</button>
          </div>
        </div>
      </div>
      <div class="scanning" id="scanning" hidden><div class="scanline"></div>
        <div class="scan-text"><span id="scan-text">Assessing…</span><div class="scan-bar"><i id="scan-bar"></i></div></div></div>
    </div>

    <div class="card unit" id="unit"></div>

    <div class="card telemetry">
      <div class="tele-head">
        <div class="segmented" role="tablist" aria-label="Input type" id="kind-filter">
          <button class="seg active" data-filter="all" role="tab">All</button>
          <button class="seg" data-filter="image" role="tab">Photo</button>
          <button class="seg" data-filter="video_frame" role="tab">Video</button>
          <button class="seg" data-filter="ortho" role="tab">Ortho</button>
        </div>
        <div class="strip" id="strip" aria-label="Images"></div>
        <div class="quick">
          <a class="round sm" id="quick-csv" title="Download priority CSV">CSV</a>
          <a class="round sm" id="quick-pdf" title="Situation report (PDF)">PDF</a>
        </div>
      </div>
      <div class="tele-body">
        <a class="minimap" id="minimap" title="Open the rescue map"><span class="minimap-empty" id="minimap-empty">NO FIX</span></a>
        <dl class="stats">
          <div><dt>OBSERVATIONS</dt><dd id="s-total">0</dd></div>
          <div><dt>REVIEWED</dt><dd id="s-reviewed">0%</dd></div>
          <div><dt>MAPPED</dt><dd id="s-mapped">0</dd></div>
          <div><dt>MODEL</dt><dd id="s-model">—</dd></div>
          <div><dt>IMAGES / FRAMES</dt><dd id="s-frames">0</dd></div>
          <div><dt>LOCATION</dt><dd id="s-location">—</dd></div>
        </dl>
        <div class="sevlist"><dt>SEVERITY</dt><ul id="sevlist"></ul></div>
        <div class="dpad" aria-label="Navigator">
          <button class="dp up" id="dp-up" aria-label="Raise confidence filter">${icon("chevronU", 17)}</button>
          <span class="dp-label top">conf +</span>
          <button class="dp left" id="dp-left" aria-label="Previous image">${icon("chevronL", 17)}</button>
          <button class="dp-center" id="dp-center" aria-label="Run assessment">RUN</button>
          <button class="dp right" id="dp-right" aria-label="Next image">${icon("chevronR", 17)}</button>
          <span class="dp-label bottom">conf −</span>
          <button class="dp down" id="dp-down" aria-label="Lower confidence filter">${icon("chevronD", 17)}</button>
        </div>
      </div>
    </div>

    <div class="card dial">
      <div class="si-chip" id="si-chip">SI 0.00</div>
      <svg id="dial" viewBox="0 0 220 220" role="img" aria-label="Severity distribution dial"></svg>
      <div class="dial-legend" id="dial-legend"></div>
      <p class="coords" id="coords">—</p>
    </div>
  </section>`;
}

// ---------- viewport ----------

function current() {
  const list = navImages();
  if (!list.length) return null;
  if (!list.includes(images()[st.idx])) st.idx = images().indexOf(list[0]);
  return images()[st.idx];
}

function renderViewport() {
  const vp = $("#viewport", host), stage = $("#vp-stage", host);
  vp.classList.toggle("grid-off", !st.grid);
  vp.classList.toggle("hud-off", !st.hud);
  stage.classList.toggle("no-boxes", !st.boxes);
  const img = current();
  $("#dropzone", host).hidden = !!img;
  stage.replaceChildren();
  const run = activeRun();
  $("#scanning", host).hidden = !(run || st.uploading !== null);
  if (st.uploading !== null) {
    $("#scan-text", host).textContent = `Uploading… ${pct(st.uploading)}`;
    $("#scan-bar", host).style.width = pct(st.uploading * 0.4);
  } else if (run) {
    $("#scan-text", host).textContent = `${run.status === "queued" ? "Queued" : run.message || "Assessing…"}`;
    $("#scan-bar", host).style.width = pct(0.4 + (run.progress || 0) * 0.6);
  }
  if (!img) {
    $("#vp-backdrop", host).style.backgroundImage = "";
    $("#vp-caption", host).textContent = "";
    $("#pager", host).textContent = "";
    $("#open-review", host).removeAttribute("href");
    drawHistogram(null);
    renderHud(null, []);
    return;
  }
  const src = `/api/images/${img.id}/file`;
  $("#vp-backdrop", host).style.backgroundImage = `url("${src}")`;
  stage.setAttribute("viewBox", `0 0 ${img.width} ${img.height}`);
  const pic = document.createElementNS(SVGNS, "image");
  pic.setAttribute("href", src);
  pic.setAttribute("width", img.width);
  pic.setAttribute("height", img.height);
  stage.appendChild(pic);
  const sw = Math.max(2, img.width / 320), fs = Math.max(12, Math.min(26, img.width / 44));
  const rows = visible().filter((o) => o.image_id === img.id && !st.hidden.has(o.damage_class))
    .sort((a, b) => SEVERITY[a.damage_class] - SEVERITY[b.damage_class]);
  rows.forEach((o) => {
    const [x1, y1, x2, y2] = o.bbox, color = COLORS[o.damage_class];
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", `box ${o.status}`);
    g.style.setProperty("--sw", sw);
    const title = document.createElementNS(SVGNS, "title");
    title.textContent = `${o.damage_class} · ${pct(o.confidence)} · ${o.status.replace("_", " ")}`;
    const rect = document.createElementNS(SVGNS, "rect");
    Object.entries({ class: "frame", x: x1, y: y1, width: x2 - x1, height: y2 - y1, rx: sw, stroke: color }).forEach(([k, v]) => rect.setAttribute(k, v));
    g.append(title, rect);
    if (st.labels) {
      const mark = o.status === "not_reviewed" ? "" : o.status === "confirmed" || o.status === "added" ? "✓ " : "✎ ";
      const text = `${mark}${o.is_demo ? "DEMO · " : ""}${SHORT[o.damage_class]} ${pct(o.confidence)}`;
      const w = text.length * fs * 0.58 + fs * 0.8, h = fs * 1.5, ty = y1 - h < 0 ? y1 : y1 - h;
      const tag = document.createElementNS(SVGNS, "rect");
      Object.entries({ class: "tag", x: x1, y: ty, width: w, height: h, rx: fs * 0.25, fill: color }).forEach(([k, v]) => tag.setAttribute(k, v));
      const t = document.createElementNS(SVGNS, "text");
      Object.entries({ x: x1 + fs * 0.4, y: ty + h * 0.7, "font-size": fs }).forEach(([k, v]) => t.setAttribute(k, v));
      t.textContent = text;
      if (o.damage_class === "Minor Damage") t.style.fill = "#111";
      g.append(tag, t);
    }
    g.addEventListener("click", () => ctx.go(ctx.incidentPath("review", `?image=${img.id}&obs=${o.id}`)));
    stage.appendChild(g);
  });
  const list = navImages(), pos = list.indexOf(img);
  $("#vp-caption", host).textContent = `${img.label} · ${rows.length} shown · ${LOCATION_LABEL[img.location_source] || img.location_source}`;
  $("#pager", host).textContent = `${pad2(pos + 1)} / ${pad2(list.length)}`;
  $("#open-review", host).href = ctx.incidentPath("review", `?image=${img.id}`);
  drawHistogram(src);
  renderHud(img, rows);
}

function renderHud(img, rows) {
  const run = ctx.store.data.runs[0];
  const demo = ctx.store.data.observations.some((o) => o.is_demo);
  $("#hud-mode", host).textContent = demo ? "DEMO" : run && run.mode !== "demo" ? "LIVE" : "—";
  $("#hud-mode", host).href = ctx.incidentPath("review");
  $("#hud-res", host).textContent = img ? `${img.width}×${img.height}` : "—";
  $("#hud-kind", host).textContent = img ? { image: "photo", video_frame: "video frame", ortho: "orthomosaic" }[img.kind] || img.kind : "no image";
  $("#hud-level", host).style.width = pct(st.minConf);
  $("#hud-level-val", host).textContent = pct(st.minConf);
  $("#hud-classes", host).innerHTML = CLASSES.map((c) => `<span><i style="background:${COLORS[c]}"></i>${CHIP[c]}<em>${rows.filter((r) => r.damage_class === c).length}</em></span>`).join("");
  const all = img ? ctx.store.data.observations.filter((o) => o.image_id === img.id) : [];
  const done = all.filter((o) => o.status !== "not_reviewed").length;
  $("#hud-review", host).innerHTML = `<div class="track"><div class="fill" style="width:${all.length ? (done / all.length) * 100 : 0}%"></div></div><span>${done}/${all.length}</span>`;
  $("#obs-chip", host).textContent = `${pad2(rows.length)} OBS`;
  $("#toggle-boxes", host).setAttribute("aria-pressed", String(st.boxes));
}

let histoToken = 0;
function drawHistogram(src) {
  const path = $("#histo path", host);
  if (!src) { path.setAttribute("d", ""); return; }
  const token = ++histoToken, im = new Image();
  im.onload = () => {
    if (token !== histoToken) return;
    const c = document.createElement("canvas"), w = 160, h = Math.max(1, Math.round(160 * im.height / im.width));
    c.width = w; c.height = h;
    const g = c.getContext("2d");
    g.drawImage(im, 0, 0, w, h);
    const data = g.getImageData(0, 0, w, h).data, bins = new Array(48).fill(0);
    for (let i = 0; i < data.length; i += 4) bins[Math.min(47, Math.floor((0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2]) / 256 * 48))] += 1;
    const max = Math.max(...bins.map(Math.sqrt)) || 1;
    path.setAttribute("d", `M0 40 ${bins.map((b, i) => `L${(i / 47) * 100} ${40 - (Math.sqrt(b) / max) * 38}`).join(" ")} L100 40 Z`);
  };
  im.src = src;
}

// ---------- assessment controls ----------

function renderUnit() {
  const p = prefs(), run = activeRun(), lastRun = ctx.store.data.runs[0], q = st.queue.length;
  const rows = visible(), high = rows.filter((o) => SEVERITY[o.damage_class] >= 2).length;
  const canRun = ctx.store.can("reviewer");
  const modeName = { demo: "Synthetic demo detector", claude: "Claude vision detector", single: "YOLO damage model", two_stage: "Two-stage YOLO" }[mode(p)];
  $("#unit", host).innerHTML = `
    <div class="unit-head">
      <div><h3>${esc(modeName)}</h3><p class="sub">${esc(modelLine(p))}</p></div>
      <svg class="drone" viewBox="0 0 64 28" aria-hidden="true"><path d="M8 9h14M42 9h14M15 9v4M49 9v4M20 15h24l-4 6H24z"/><path d="M24 21l-6 5M40 21l6 5M32 15v-3"/></svg>
    </div>
    <div class="unit-row"><div><p class="row-title">High-priority</p><p class="sub">${high} of ${rows.length} · major + destroyed</p></div>
      <span class="big yellow">${rows.length ? Math.round(high / rows.length * 100) + "%" : "—"}</span></div>
    <div class="unit-row stack">
      <div class="row-line"><label class="row-title" for="conf">Confidence filter</label><span class="val">${pct(st.minConf)}</span></div>
      <input type="range" id="conf" min="0" max="1" step="0.05" value="${st.minConf}" style="--p:${pct(st.minConf)}">
      ${lastRun && st.minConf < lastRun.threshold - 1e-9 ? `<p class="hint">The latest run kept detections ≥ ${pct(lastRun.threshold)}.</p>` : ""}
    </div>
    <div class="divider"></div>
    ${run ? `<div class="run-progress">
        <div class="row-line"><span class="row-title">Run #${run.id} · ${esc(run.status)}</span><span class="val">${pct(run.progress)}</span></div>
        <div class="bar"><i style="width:${pct(run.progress)}"></i></div>
        <p class="sub">${esc(run.message || "")}</p>
        ${canRun ? `<button class="btn ghost wide" id="cancel-run">Cancel run</button>` : ""}
      </div>` : `
    <div class="round-row">
      <button class="round ${st.grid ? "on" : ""}" id="toggle-grid" aria-pressed="${st.grid}" title="Grid (G)">${icon("grid", 15)}<span>GRID</span></button>
      <button class="round ${st.hud ? "on" : ""}" id="toggle-hud" aria-pressed="${st.hud}" title="HUD (H)">${icon("hud", 15)}<span>HUD</span></button>
      <button class="round ${st.labels ? "on" : ""}" id="toggle-labels" aria-pressed="${st.labels}" title="Labels (L)">${icon("tag", 15)}<span>LBL</span></button>
      <button class="round" id="run-settings" title="Run settings" ${canRun ? "" : "disabled"}>${icon("layers", 15)}<span>SET</span></button>
    </div>
    <button class="btn primary wide" id="run" ${!q || !canRun ? "disabled" : ""}>${q ? `Run assessment · ${plural(q, "file")}` : "Run assessment"}</button>
    <p class="queue">${q ? esc(st.queue.slice(0, 3).map((f) => f.name).join(", ") + (q > 3 ? ` +${q - 3}` : "")) + ` · ${fmtBytes(st.queue.reduce((n, f) => n + f.size, 0))}`
      : canRun ? "Drop files on the viewport or choose files" : "Reviewer role needed to run assessments"}</p>`}
    <button class="link-btn" id="runs-history">${plural(ctx.store.data.runs.length, "assessment run")} · history</button>`;
  const conf = $("#conf", host);
  conf.addEventListener("input", (e) => { st.minConf = Number(e.target.value); savePrefs({ minConf: st.minConf }); refresh(); });
  const on = (id, fn) => { const el = $(id, host); if (el) el.addEventListener("click", fn); };
  on("#toggle-grid", () => { st.grid = !st.grid; refresh(); });
  on("#toggle-hud", () => { st.hud = !st.hud; refresh(); });
  on("#toggle-labels", () => { st.labels = !st.labels; refresh(); });
  on("#run", runQueued);
  on("#run-settings", runSettings);
  on("#runs-history", runsHistory);
  on("#cancel-run", async () => {
    try { await api.post(`/api/runs/${run.id}/cancel`); toast("Cancelling…"); } catch (err) { toast(err.message, "error"); }
  });
  $("#dp-center", host).disabled = !canRun;
}

function modelLine(p) {
  if (mode(p) === "demo") return claudeReady() ? "Synthetic boxes · switch to Claude under SET" : "Synthetic boxes · connect Claude to enable vision";
  if (mode(p) === "claude") return "Finds buildings, classifies damage, explains why";
  if (p.mode === "single") return `${p.model || "choose a model"} · ${(p.device || "cpu").toUpperCase()}`;
  return `${p.building_model || "building?"} → ${p.damage_model || "damage?"}`;
}

async function runSettings() {
  let info = { models: [], vision_review: false };
  try { info = await api.get("/api/models"); } catch (err) { toast(err.message, "error"); }
  const p = prefs(), opts = (sel) => info.models.map((m) => `<option ${m.name === sel ? "selected" : ""}>${esc(m.name)}</option>`).join("");
  const fd = await modal({
    title: "Assessment settings", submit: "Save settings", wide: true,
    body: `<div class="segmented full" id="mode-seg">
        ${["claude", "demo", "single", "two_stage"].map((m) => `<label class="seg ${mode(p) === m ? "active" : ""}${m === "claude" && !info.claude_detect ? " dim" : ""}"><input type="radio" name="mode" value="${m}" ${mode(p) === m ? "checked" : ""} ${m === "claude" && !info.claude_detect ? "disabled" : ""}>${{ claude: "Claude vision", demo: "Demo · synthetic", single: "Single YOLO", two_stage: "Two-stage YOLO" }[m]}</label>`).join("")}
      </div>
      <p class="note ${info.claude_detect ? "" : "warn"}">${info.claude_detect
        ? `Claude vision (${esc(info.claude_model || "")}) finds each building, draws a box, picks a damage class and explains the visible evidence. Images are sent to the Anthropic API; large aerial mosaics are split into overlapping tiles. Estimated cost: roughly 5–15¢ per photo, ~50¢ per aerial mosaic; each run records its actual cost.`
        : "Claude vision is off. Give the server Claude API access (ANTHROPIC_API_KEY in disaster_assessor/.env, or sign in once with <code>ant auth login</code>) and restart it."}</p>
      ${info.models.length ? "" : `<p class="note warn">No model weights in the models folder yet. Train one (see training/README.md) or copy trusted .pt files there; only demo mode is available until then.</p>`}
      <div class="two">
        <label class="field">Single model (4 damage classes)<select name="model"><option value="">—</option>${opts(p.model)}</select></label>
        <label class="field">Device<select name="device">${["cpu", "mps", "cuda:0"].map((d) => `<option ${p.device === d ? "selected" : ""}>${d}</option>`).join("")}</select></label>
        <label class="field">Building detector<select name="building_model"><option value="">—</option>${opts(p.building_model)}</select></label>
        <label class="field">Damage classifier<select name="damage_model"><option value="">—</option>${opts(p.damage_model)}</select></label>
      </div>
      <h4 class="group-label">VIDEO</h4>
      <div class="two">
        <label class="field">Seconds between frames<input type="number" name="interval" min="1" max="30" value="${p.interval || 3}"></label>
        <label class="field">Max frames per video<input type="number" name="max_frames" min="1" max="30" value="${p.max_frames || 8}"></label>
      </div>
      <h4 class="group-label">LOCATION</h4>
      <label class="switch"><input type="checkbox" name="manual_on" ${p.manual_on ? "checked" : ""}><span></span>Use one manual reference point for every input</label>
      <div class="two">
        <label class="field">Latitude<input type="number" step="any" name="manual_lat" value="${p.manual_lat ?? ctx.store.incident.center_lat ?? ""}"></label>
        <label class="field">Longitude<input type="number" step="any" name="manual_lon" value="${p.manual_lon ?? ctx.store.incident.center_lon ?? ""}"></label>
      </div>
      <h4 class="group-label">SECOND OPINION</h4>
      <label class="switch"><input type="checkbox" name="vision_review" ${p.vision_review ? "checked" : ""} ${info.vision_review ? "" : "disabled"}><span></span>Ask Claude to independently classify Major/Destroyed detections</label>
      <p class="note">${info.vision_review ? "Crops of high-severity detections are sent to the Anthropic API. Results appear in Review; they never change classes on their own." : "Set ANTHROPIC_API_KEY on the server to enable this."}</p>`,
    onOpen: (dlg) => dlg.querySelectorAll("#mode-seg input").forEach((r) => r.addEventListener("change", () =>
      dlg.querySelectorAll("#mode-seg .seg").forEach((s) => s.classList.toggle("active", s.querySelector("input").checked)))),
  });
  if (!fd) return;
  savePrefs({
    mode: fd.get("mode"), model: fd.get("model"), device: fd.get("device"), building_model: fd.get("building_model"),
    damage_model: fd.get("damage_model"), interval: Number(fd.get("interval")), max_frames: Number(fd.get("max_frames")),
    manual_on: fd.get("manual_on") === "on", manual_lat: fd.get("manual_lat"), manual_lon: fd.get("manual_lon"),
    vision_review: fd.get("vision_review") === "on",
  });
  renderUnit();
}

async function runsHistory() {
  const rows = ctx.store.data.runs.map((r) => `<tr><td>#${r.id}</td><td>${esc(r.mode)}</td><td>${esc(r.model)}</td>
    <td><span class="pill status" style="--c:${{ done: "#16a34a", failed: "#dc2626", running: "#5aa9e6", queued: "#9a9fa8", cancelled: "#6b7079" }[r.status]}">${esc(r.status)}</span></td>
    <td>${r.observations ?? 0}</td><td>${pct(r.threshold)}</td><td class="dim">${esc(r.created_by || "")}</td><td class="dim">${new Date(r.created_at).toLocaleString()}</td>
    <td class="dim">${esc(r.message || "")}${r.errors.length ? ` · <span title="${esc(r.errors.join("\n"))}">${plural(r.errors.length, "error")}</span>` : ""}</td></tr>`).join("");
  await modal({ title: "Assessment runs", submit: null, wide: true,
    body: rows ? `<div class="table-wrap"><table><thead><tr><th>Run</th><th>Mode</th><th>Model</th><th>Status</th><th>Obs.</th><th>Threshold</th><th>By</th><th>Started</th><th>Result</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<p class="muted">No runs yet.</p>` });
}

function queueFiles(list) {
  const ok = [...list].filter((f) => /\.(jpe?g|png|webp|tiff?|mp4|mov|avi|mkv|srt)$/i.test(f.name));
  if (ok.length < list.length) toast("Some files were skipped: unsupported type.", "warn");
  st.queue = [...st.queue, ...ok].slice(0, 40);
  if (st.queue.reduce((n, f) => n + f.size, 0) > 2 * 1024 ** 3) { toast("A run is limited to 2 GB.", "error"); st.queue = []; }
  renderUnit();
}

function runForm(extra = {}) {
  const p = prefs(), fd = new FormData();
  const fields = { mode: mode(p), threshold: String(st.minConf), model: p.model || "", building_model: p.building_model || "",
    damage_model: p.damage_model || "", device: p.device || "", interval: String(p.interval || 3), max_frames: String(p.max_frames || 8),
    vision_review: String(!!p.vision_review), ...extra };
  if (p.manual_on && p.manual_lat !== "" && p.manual_lon !== "") { fields.manual_lat = p.manual_lat; fields.manual_lon = p.manual_lon; }
  Object.entries(fields).forEach(([k, v]) => fd.append(k, v));
  return fd;
}

async function runQueued() {
  if (!ctx.store.can("reviewer")) return;
  if (!st.queue.length) { $("#file-input").click(); return; }
  const fd = runForm();
  st.queue.forEach((f) => fd.append("files", f, f.name));
  st.uploading = 0;
  refresh();
  try {
    await upload(`/api/incidents/${ctx.store.incident.id}/runs`, fd, (p) => { st.uploading = p; renderViewport(); });
    st.queue = [];
    toast("Upload complete. Assessment is running in the background.", "success");
  } catch (err) { toast(err.message, "error"); }
  st.uploading = null;
  await ctx.reload(["runs"]);
  refresh();
}

async function runDemo() {
  try {
    await api.form(`/api/incidents/${ctx.store.incident.id}/runs`, runForm({ mode: "demo", builtin_demo: "true" }));
    await ctx.reload(["runs"]);
    refresh();
  } catch (err) { toast(err.message, "error"); }
}

// ---------- telemetry ----------

function renderTelemetry() {
  const all = images(), rows = visible();
  document.querySelectorAll("#kind-filter .seg").forEach((b) => {
    b.classList.toggle("active", b.dataset.filter === st.filter);
    b.disabled = b.dataset.filter !== "all" && !all.some((i) => i.kind === b.dataset.filter);
  });
  const strip = $("#strip", host), img = current();
  strip.replaceChildren(...navImages().map((im) => {
    const b = document.createElement("button");
    b.className = `thumb${im === img ? " active" : ""}`;
    b.title = im.label;
    const ticks = rows.filter((o) => o.image_id === im.id).map((o) => `<i style="background:${COLORS[o.damage_class]}"></i>`).join("");
    b.innerHTML = `<img src="/api/images/${im.id}/file?variant=preview" alt="" loading="lazy"><span class="ticks">${ticks}</span>${im.unreviewed ? `<span class="dotmark" title="${im.unreviewed} unreviewed"></span>` : ""}`;
    b.addEventListener("click", () => { st.idx = all.indexOf(im); refresh(); });
    return b;
  }));
  const active = strip.querySelector(".active");
  if (active) {
    const left = active.offsetLeft - strip.offsetLeft, right = left + active.offsetWidth;
    if (left < strip.scrollLeft) strip.scrollLeft = left;
    else if (right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = right - strip.clientWidth;
  }
  const s = ctx.store.incident.summary || {};
  const mapped = rows.filter((o) => validCoord(o.latitude, o.longitude)).length;
  $("#s-total", host).textContent = rows.length;
  $("#s-reviewed", host).textContent = `${s.reviewed_pct ?? 0}%`;
  $("#s-mapped", host).textContent = `${mapped}/${rows.length}`;
  $("#s-model", host).textContent = ctx.store.data.runs[0] ? ctx.store.data.runs[0].model : "—";
  $("#s-frames", host).textContent = all.length;
  $("#s-location", host).textContent = img ? (LOCATION_LABEL[img.location_source] || img.location_source) : "—";
  const by = Object.fromEntries(CLASSES.map((c) => [c, rows.filter((o) => o.damage_class === c).length]));
  const order = [...CLASSES].reverse(), top = order.find((c) => by[c] > 0);
  $("#sevlist", host).innerHTML = order.map((c) => `<li data-cls="${esc(c)}" class="${c === top ? "top" : ""}${st.hidden.has(c) ? " off" : ""}" title="Show / hide ${esc(c)}"><span>${SHORT[c]}</span><b>${by[c]}</b></li>`).join("");
  const base = `/api/incidents/${ctx.store.incident.id}/export`;
  $("#quick-csv", host).href = `${base}/csv?min_conf=${st.minConf}`;
  $("#quick-pdf", host).href = `${base}/pdf?min_conf=${st.minConf}`;
  $("#minimap", host).href = ctx.incidentPath("map");
}

// ---------- dial ----------

function renderDial() {
  const rows = visible(), n = rows.length, cx = 110, cy = 110, parts = [];
  const by = Object.fromEntries(CLASSES.map((c) => [c, rows.filter((o) => o.damage_class === c).length]));
  const si = n ? rows.reduce((s, o) => s + SEVERITY[o.damage_class], 0) / n : 0;
  const polar = (r, deg) => [cx + r * Math.sin(deg * Math.PI / 180), cy - r * Math.cos(deg * Math.PI / 180)];
  const colors = new Array(72).fill("#3a3c42");
  let k = 0;
  if (n) CLASSES.forEach((c) => { for (let i = 0; i < Math.round(by[c] / n * 72) && k < 72; i += 1) colors[k++] = COLORS[c]; });
  while (n && k < 72 && k > 0) { colors[k] = colors[k - 1]; k += 1; }
  for (let i = 0; i < 72; i += 1) {
    const a = i * 5, [x1, y1] = polar(100, a), [x2, y2] = polar(i % 6 === 0 ? 90 : 93, a);
    parts.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${colors[i]}" stroke-width="${i % 6 === 0 ? 2 : 1.6}"/>`);
  }
  [0, 25, 50, 75].forEach((p, i) => { const [x, y] = polar(81, i * 90); parts.push(`<text x="${x}" y="${y + 3}">${p}%</text>`); });
  parts.push(`<circle cx="${cx}" cy="${cy}" r="66" fill="#0e0f11" stroke="#2c2e33"/>`);
  for (let a = -135; a <= 135; a += 9) {
    const [x1, y1] = polar(64, a), [x2, y2] = polar(a % 45 === 0 ? 56 : 60, a);
    parts.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${a % 45 === 0 ? "#9a9ea6" : "#4a4d54"}" stroke-width="1"/>`);
  }
  CLASSES.forEach((c, i) => { const [x, y] = polar(45, -135 + i * 90); parts.push(`<text class="card-lbl" x="${x}" y="${y + 4}" style="fill:${COLORS[c]}">${CHIP[c]}</text>`); });
  parts.push(`<g transform="rotate(${-135 + (si / 3) * 270} ${cx} ${cy})"><path d="M${cx} ${cy - 30} L${cx + 8} ${cy + 10} L${cx} ${cy + 4} L${cx - 8} ${cy + 10} Z" fill="#fff"/></g><circle cx="${cx}" cy="${cy}" r="3" fill="#0e0f11" stroke="#fff" stroke-width="1.2"/>`);
  $("#dial", host).innerHTML = parts.join("");
  $("#dial", host).setAttribute("aria-label", `Severity index ${si.toFixed(2)} of 3. ${CLASSES.map((c) => `${c} ${by[c]}`).join(", ")}`);
  $("#si-chip", host).textContent = `SI ${si.toFixed(2)}`;
  $("#si-chip", host).title = "Severity index: mean of Intact 0, Minor 1, Major 2, Destroyed 3";
  $("#dial-legend", host).innerHTML = CLASSES.map((c) => `<span><i style="background:${COLORS[c]}"></i>${SHORT[c]} <b>${n ? Math.round(by[c] / n * 100) : 0}%</b></span>`).join("");
  const mapped = rows.filter((o) => validCoord(o.latitude, o.longitude));
  const inc = ctx.store.incident;
  const [lat, lon] = mapped.length ? [mapped.reduce((s, o) => s + o.latitude, 0) / mapped.length, mapped.reduce((s, o) => s + o.longitude, 0) / mapped.length]
    : [inc.center_lat ?? 37.7749, inc.center_lon ?? -122.4194];
  $("#coords", host).textContent = `${dms(lat, "N", "S")}  ${dms(lon, "E", "W")}${mapped.length ? "" : " · ref"}`;
}

// ---------- minimap ----------

function renderMini() {
  if (typeof L === "undefined") return;
  const rows = visible().filter((o) => validCoord(o.latitude, o.longitude) && !st.hidden.has(o.damage_class));
  $("#minimap-empty", host).hidden = rows.length > 0;
  if (!mini || !document.body.contains(mini.getContainer())) {
    mini = L.map($("#minimap", host), { maxZoom: 19, zoomControl: false, attributionControl: false, dragging: false, scrollWheelZoom: false, keyboard: false, doubleClickZoom: false, touchZoom: false, boxZoom: false });
    darkTiles(ctx.store.config).addTo(mini);
    miniLayer = L.layerGroup().addTo(mini);
    const inc = ctx.store.incident;
    mini.setView([inc.center_lat ?? 37.7749, inc.center_lon ?? -122.4194], 13);
  }
  miniLayer.clearLayers();
  const img = current();
  rows.forEach((o) => {
    const cur = img && o.image_id === img.id;
    L.circleMarker([o.latitude, o.longitude], { radius: cur ? 5 : 3.5, color: cur ? "#fff" : COLORS[o.damage_class], weight: cur ? 2 : 0, fillColor: COLORS[o.damage_class], fillOpacity: 1, interactive: false }).addTo(miniLayer);
  });
  mini.invalidateSize();
  if (rows.length) mini.fitBounds(L.latLngBounds(rows.map((o) => [o.latitude, o.longitude])).pad(0.25), { maxZoom: 16 });
}

// ---------- events ----------

function step(delta) {
  const list = navImages();
  if (!list.length) return;
  const pos = list.indexOf(current());
  st.idx = images().indexOf(list[(pos + delta + list.length) % list.length]);
  refresh();
}
function nudge(delta) {
  st.minConf = Math.round(Math.min(1, Math.max(0, st.minConf + delta)) * 20) / 20;
  savePrefs({ minConf: st.minConf });
  refresh();
}

function onKey(e) {
  if (e.target.closest && e.target.closest("input, select, textarea, dialog")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === "arrowleft") step(-1);
  else if (k === "arrowright") step(1);
  else if (k === "b") { st.boxes = !st.boxes; refresh(); }
  else if (k === "g") { st.grid = !st.grid; refresh(); }
  else if (k === "h") { st.hud = !st.hud; refresh(); }
  else if (k === "l") { st.labels = !st.labels; refresh(); }
  else if (k === "r" && current()) ctx.go(ctx.incidentPath("review", `?image=${current().id}`));
}

function bind() {
  const fileInput = $("#file-input");
  fileInput.onchange = (e) => { queueFiles(e.target.files); e.target.value = ""; };
  $("#browse", host).addEventListener("click", () => fileInput.click());
  $("#load-demo", host).addEventListener("click", runDemo);
  $("#load-demo", host).disabled = !ctx.store.can("reviewer");
  $("#browse", host).disabled = !ctx.store.can("reviewer");
  $("#toggle-boxes", host).addEventListener("click", () => { st.boxes = !st.boxes; refresh(); });
  $("#dp-left", host).addEventListener("click", () => step(-1));
  $("#dp-right", host).addEventListener("click", () => step(1));
  $("#dp-up", host).addEventListener("click", () => nudge(0.05));
  $("#dp-down", host).addEventListener("click", () => nudge(-0.05));
  $("#dp-center", host).addEventListener("click", runQueued);
  $("#kind-filter", host).addEventListener("click", (e) => {
    const b = e.target.closest(".seg");
    if (b && !b.disabled) { st.filter = b.dataset.filter; refresh(); }
  });
  $("#sevlist", host).addEventListener("click", (e) => {
    const li = e.target.closest("li");
    if (!li) return;
    const c = li.dataset.cls;
    if (st.hidden.has(c)) st.hidden.delete(c); else st.hidden.add(c);
    refresh();
  });
  const vp = $("#viewport", host);
  let depth = 0;
  vp.addEventListener("dragenter", (e) => { e.preventDefault(); depth += 1; vp.classList.add("dragging"); });
  vp.addEventListener("dragover", (e) => e.preventDefault());
  vp.addEventListener("dragleave", () => { depth -= 1; if (depth <= 0) { depth = 0; vp.classList.remove("dragging"); } });
  vp.addEventListener("drop", (e) => {
    e.preventDefault(); depth = 0; vp.classList.remove("dragging");
    if (ctx.store.can("reviewer")) queueFiles(e.dataTransfer.files);
  });
  document.addEventListener("keydown", onKey);
}
