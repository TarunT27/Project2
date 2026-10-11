// Review workspace: confirm, correct, reject or add detections; every decision is logged.
import { api } from "../api.js";
import {
  $, CLASSES, COLORS, HAZARDS, LOCATION_LABEL, SEVERITY, SHORT, STATUS_COLOR, STATUS_LABEL, TASK_LABEL, classPill,
  emptyState, esc, fmtTime, icon, modal, pct, plural, statusPill, toast,
} from "../ui.js";

const SVGNS = "http://www.w3.org/2000/svg";
let host, ctx;
const st = { imageId: null, obsId: null, status: "not_reviewed", cls: "", mode: "select", showRejected: true, drag: null, busy: false };

const canReview = () => ctx.store.can("reviewer");
const images = () => ctx.store.data.images.filter((i) => i.kind !== "field_photo");
const obsFor = (imageId) => ctx.store.data.observations.filter((o) => o.image_id === imageId)
  .sort((a, b) => SEVERITY[b.damage_class] - SEVERITY[a.damage_class] || b.confidence - a.confidence);
const selected = () => ctx.store.data.observations.find((o) => o.id === st.obsId) || null;

function listImages() {
  return images().filter((im) => {
    const obs = obsFor(im.id);
    if (st.cls && !obs.some((o) => o.damage_class === st.cls)) return false;
    if (st.status === "all") return true;
    return obs.some((o) => o.status === st.status);
  });
}

export async function render(el, context) {
  host = el;
  ctx = context;
  if (ctx.params.image) st.imageId = Number(ctx.params.image);
  if (ctx.params.obs) { st.obsId = Number(ctx.params.obs); st.status = "all"; }
  if (!images().some((i) => i.id === st.imageId)) st.imageId = (listImages()[0] || images()[0] || {}).id ?? null;
  host.innerHTML = `<section class="review-grid">
    <aside class="card review-list">
      <div class="side-head"><h3>Review queue</h3><p class="sub" id="queue-sub"></p></div>
      <div class="filters">
        <label class="field compact">Status<select id="f-status">
          <option value="not_reviewed">Not reviewed</option><option value="all">All</option><option value="corrected">Corrected</option>
          <option value="confirmed">Confirmed</option><option value="rejected">Rejected</option><option value="added">Added</option></select></label>
        <label class="field compact">Class<select id="f-class"><option value="">All classes</option>${CLASSES.map((c) => `<option>${c}</option>`).join("")}</select></label>
      </div>
      <div class="progress-line"><div class="bar"><i id="review-bar"></i></div><span id="review-pct"></span></div>
      <ol class="image-list" id="image-list"></ol>
    </aside>
    <div class="card review-stage">
      <div class="stage-bar">
        <button class="icon-btn" id="prev-img" aria-label="Previous image" title="Previous image ([)">${icon("chevronL", 16)}</button>
        <div class="stage-title" id="stage-title">—</div>
        <button class="icon-btn" id="next-img" aria-label="Next image" title="Next image (])">${icon("chevronR", 16)}</button>
        <span class="grow"></span>
        <div class="segmented small" id="tool">
          <button class="seg active" data-mode="select" title="Select (S)">Select</button>
          <button class="seg" data-mode="draw" title="Draw a missed building (D)">${icon("draw", 13)} Add box</button>
          <button class="seg" data-mode="redraw" title="Redraw the selected box (E)">Redraw</button>
        </div>
        <label class="switch small"><input type="checkbox" id="show-rejected" checked><span></span>Rejected</label>
        <button class="btn ghost sm" id="confirm-image" title="Confirm every unreviewed box in this image">Confirm image</button>
      </div>
      <div class="stage-wrap" id="stage-wrap"><svg id="review-svg" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Image under review"></svg></div>
      <p class="fine stage-help">Keys: <kbd>1</kbd>–<kbd>4</kbd> set class · <kbd>C</kbd>/<kbd>Enter</kbd> confirm · <kbd>X</kbd> reject · <kbd>N</kbd> next unreviewed · <kbd>J</kbd>/<kbd>K</kbd> next/prev box · <kbd>D</kbd> add box</p>
    </div>
    <aside class="card review-panel" id="panel"></aside>
  </section>`;
  $("#f-status", host).value = st.status;
  $("#f-class", host).value = st.cls;
  bind();
  refresh();
}

export function destroy() { document.removeEventListener("keydown", onKey); }

export function refresh() {
  if (!host || !host.isConnected) return;
  renderList();
  renderStage();
  renderPanel();
}

// ---------- list ----------

function renderList() {
  const all = ctx.store.data.observations, done = all.filter((o) => o.status !== "not_reviewed").length;
  $("#queue-sub", host).textContent = `${plural(all.length - done, "observation")} awaiting review`;
  $("#review-bar", host).style.width = all.length ? `${(done / all.length) * 100}%` : "0";
  $("#review-pct", host).textContent = all.length ? `${Math.round(done / all.length * 100)}%` : "—";
  const list = listImages();
  if (!list.length) {
    $("#image-list", host).innerHTML = `<li class="fine pad">${images().length ? "Nothing matches these filters." : "No imagery yet. Run an assessment from the console."}</li>`;
    return;
  }
  $("#image-list", host).innerHTML = list.map((im) => {
    const obs = obsFor(im.id), open = obs.filter((o) => o.status === "not_reviewed").length;
    const ticks = obs.map((o) => `<i style="background:${COLORS[o.damage_class]};opacity:${o.status === "rejected" ? 0.3 : 1}"></i>`).join("");
    return `<li><button class="image-row${im.id === st.imageId ? " active" : ""}" data-id="${im.id}">
      <img src="/api/images/${im.id}/file?variant=preview" alt="" loading="lazy">
      <span class="ir-main"><span class="ir-name">${esc(im.label)}</span><span class="ticks">${ticks}</span></span>
      <span class="ir-badge${open ? "" : " done"}">${open ? open : icon("check", 12)}</span></button></li>`;
  }).join("");
}

// ---------- stage ----------

function renderStage() {
  const svg = $("#review-svg", host), img = images().find((i) => i.id === st.imageId);
  svg.replaceChildren();
  $("#stage-wrap", host).classList.toggle("drawing", st.mode !== "select");
  document.querySelectorAll("#tool .seg").forEach((b) => b.classList.toggle("active", b.dataset.mode === st.mode));
  if (!img) {
    $("#stage-title", host).textContent = "No image selected";
    return;
  }
  $("#stage-title", host).innerHTML = `${esc(img.label)} <span class="muted">· ${img.width}×${img.height} · ${esc(LOCATION_LABEL[img.location_source] || img.location_source)}</span>`;
  svg.setAttribute("viewBox", `0 0 ${img.width} ${img.height}`);
  const pic = document.createElementNS(SVGNS, "image");
  pic.setAttribute("href", `/api/images/${img.id}/file`);
  pic.setAttribute("width", img.width);
  pic.setAttribute("height", img.height);
  svg.appendChild(pic);
  const sw = Math.max(2, img.width / 300), fs = Math.max(12, Math.min(24, img.width / 48));
  obsFor(img.id).filter((o) => st.showRejected || o.status !== "rejected").reverse().forEach((o) => {
    const [x1, y1, x2, y2] = o.bbox, color = COLORS[o.damage_class], sel = o.id === st.obsId;
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", `rbox ${o.status}${sel ? " sel" : ""}`);
    g.innerHTML = `<rect class="hit" x="${x1}" y="${y1}" width="${x2 - x1}" height="${y2 - y1}" fill="transparent"/>
      <rect x="${x1}" y="${y1}" width="${x2 - x1}" height="${y2 - y1}" fill="${sel ? "rgba(255,255,255,.08)" : "transparent"}" stroke="${color}" stroke-width="${sel ? sw * 1.8 : sw}" ${o.status === "rejected" ? `stroke-dasharray="${sw * 3} ${sw * 2}"` : ""}/>
      <rect x="${x1}" y="${Math.max(0, y1 - fs * 1.5)}" width="${(SHORT[o.damage_class].length + 6) * fs * 0.6}" height="${fs * 1.5}" rx="${fs * 0.25}" fill="${o.status === "rejected" ? "#3a3c42" : color}"/>
      <text x="${x1 + fs * 0.4}" y="${Math.max(0, y1 - fs * 1.5) + fs * 1.05}" font-size="${fs}" fill="${o.damage_class === "Minor Damage" && o.status !== "rejected" ? "#111" : "#fff"}">${o.status === "not_reviewed" ? "" : o.status === "rejected" ? "✕ " : "✓ "}${SHORT[o.damage_class]} ${pct(o.confidence)}</text>`;
    g.addEventListener("pointerdown", (e) => { if (st.mode === "select") { e.stopPropagation(); select(o.id); } });
    svg.appendChild(g);
  });
}

function svgPoint(e) {
  const svg = $("#review-svg", host), pt = svg.createSVGPoint();
  pt.x = e.clientX; pt.y = e.clientY;
  const p = pt.matrixTransform(svg.getScreenCTM().inverse());
  const img = images().find((i) => i.id === st.imageId);
  return [Math.max(0, Math.min(img.width, p.x)), Math.max(0, Math.min(img.height, p.y))];
}

function startDrag(e) {
  if (st.mode === "select" || !st.imageId || !canReview()) return;
  if (st.mode === "redraw" && !selected()) { toast("Select a box first, then redraw it.", "warn"); return; }
  e.preventDefault();
  const [x, y] = svgPoint(e), svg = $("#review-svg", host);
  const rect = document.createElementNS(SVGNS, "rect");
  rect.setAttribute("class", "drawing-rect");
  svg.appendChild(rect);
  st.drag = { x, y, rect };
  svg.setPointerCapture(e.pointerId);
}

function moveDrag(e) {
  if (!st.drag) return;
  const [x, y] = svgPoint(e), d = st.drag;
  d.rect.setAttribute("x", Math.min(d.x, x));
  d.rect.setAttribute("y", Math.min(d.y, y));
  d.rect.setAttribute("width", Math.abs(x - d.x));
  d.rect.setAttribute("height", Math.abs(y - d.y));
  d.box = [Math.round(Math.min(d.x, x)), Math.round(Math.min(d.y, y)), Math.round(Math.max(d.x, x)), Math.round(Math.max(d.y, y))];
}

async function endDrag() {
  const d = st.drag;
  st.drag = null;
  if (!d) return;
  d.rect.remove();
  if (!d.box || d.box[2] - d.box[0] < 6 || d.box[3] - d.box[1] < 6) return;
  if (st.mode === "redraw") {
    await review({ bbox: d.box });
    st.mode = "select";
    renderStage();
    return;
  }
  const fd = await modal({
    title: "Add a missed building", submit: "Add observation",
    body: `<fieldset class="class-pick">${CLASSES.map((c, i) => `<label><input type="radio" name="cls" value="${c}" ${i === 2 ? "checked" : ""}><span>${classPill(c)}</span></label>`).join("")}</fieldset>
      <label class="field">Note<textarea name="note" rows="2" maxlength="2000" placeholder="What you see"></textarea></label>`,
  });
  if (!fd) return;
  try {
    const o = await api.post(`/api/images/${st.imageId}/observations`, { bbox: d.box, damage_class: fd.get("cls"), review_note: fd.get("note") || "" });
    ctx.store.data.observations.push(o);
    st.obsId = o.id;
    st.mode = "select";
    toast("Observation added.", "success");
    refresh();
  } catch (err) { toast(err.message, "error"); }
}

// ---------- panel ----------

function renderPanel() {
  const o = selected(), panel = $("#panel", host);
  if (!o || o.image_id !== st.imageId) {
    const img = images().find((i) => i.id === st.imageId);
    const obs = img ? obsFor(img.id) : [];
    panel.innerHTML = img ? `<div class="side-head"><h3>${plural(obs.length, "observation")}</h3><p class="sub">Select a box on the image, or press <kbd>J</kbd>.</p></div>
      <ul class="obs-mini">${obs.map((x) => `<li><button data-obs="${x.id}">${classPill(x.damage_class)}<span>${pct(x.confidence)}</span>${statusPill(x.status)}</button></li>`).join("")}</ul>`
      : emptyState("Nothing to review", "Run an assessment from the console to start reviewing.");
    panel.querySelectorAll("[data-obs]").forEach((b) => b.addEventListener("click", () => select(Number(b.dataset.obs))));
    return;
  }
  const v = o.vision;
  const ro = !canReview();
  panel.innerHTML = `
    <div class="side-head row"><h3>Observation #${o.id}</h3>${statusPill(o.status)}</div>
    <p class="sub">Model said <b>${esc(o.model_class)}</b> at ${pct(o.confidence)}${o.is_demo ? " · synthetic demo" : ""} · ${esc(o.scope.replace(/_/g, " "))}</p>
    <div class="class-buttons" role="group" aria-label="Damage class">
      ${CLASSES.map((c, i) => `<button class="class-btn${o.damage_class === c ? " on" : ""}" data-cls="${c}" style="--c:${COLORS[c]}" ${ro ? "disabled" : ""}><kbd>${i + 1}</kbd>${SHORT[c]}</button>`).join("")}
    </div>
    <div class="action-row">
      <button class="btn primary" id="act-confirm" ${ro ? "disabled" : ""}>${icon("check", 15)} Confirm <kbd>C</kbd></button>
      <button class="btn danger" id="act-reject" ${ro ? "disabled" : ""}>${icon("x", 15)} Reject <kbd>X</kbd></button>
    </div>
    ${v ? `<div class="vision${v.error ? " err" : ""}"><p class="vision-head">${icon("sparkle", 14)} Claude second opinion</p>
      ${v.error ? `<p class="sub">${esc(v.rationale)}</p>` : `<p>${classPill(v.damage_class === "Uncertain" ? "Uncertain" : v.damage_class)} <span class="mono">${pct(v.confidence)}</span>
      ${v.damage_class !== "Uncertain" && v.damage_class !== o.model_class ? `<span class="chip warn">disagrees with model</span>` : ""}</p>
      <p class="sub">${esc(v.rationale)}</p>${(v.hazards || []).length ? `<p>${v.hazards.map((h) => `<span class="chip warn">${esc(HAZARDS[h] || h)}</span>`).join("")}</p>` : ""}`}</div>` : ""}
    <h4 class="group-label">HAZARDS</h4>
    <div class="hazard-grid">${Object.entries(HAZARDS).map(([k, label]) => `<label class="hz"><input type="checkbox" value="${k}" ${o.hazards.includes(k) ? "checked" : ""} ${ro ? "disabled" : ""}><span>${esc(label)}</span></label>`).join("")}</div>
    <label class="field">Reviewer note<textarea id="note" rows="3" maxlength="2000" ${ro ? "disabled" : ""}>${esc(o.review_note)}</textarea></label>
    <dl class="meta">
      <dt>Location</dt><dd>${o.latitude != null ? `${o.latitude.toFixed(5)}, ${o.longitude.toFixed(5)}` : "—"} · ${esc(LOCATION_LABEL[o.location_source] || o.location_source)}</dd>
      <dt>Building</dt><dd>${o.footprint_id ? `footprint #${o.footprint_id}` : "not matched"}</dd>
      ${o.track_id ? `<dt>Video track</dt><dd>#${o.track_id % 1000}</dd>` : ""}
      <dt>Reviewed</dt><dd>${o.reviewer ? `${esc(o.reviewer)} · ${fmtTime(o.reviewed_at)}` : "—"}</dd>
      <dt>Task</dt><dd>${o.task_id ? `#${o.task_id} · ${esc(TASK_LABEL[o.task_status] || o.task_status)}` : "none"}</dd>
    </dl>
    <div class="action-row">
      <button class="btn ghost sm" id="act-next">Next unreviewed <kbd>N</kbd></button>
      ${o.status !== "not_reviewed" && !ro ? `<button class="btn ghost sm" id="act-reset">Reset</button>` : ""}
      ${ctx.store.can("coordinator") && !o.task_id && o.status !== "rejected" ? `<button class="btn ghost sm" id="act-task">${icon("tasks", 13)} Create task</button>` : ""}
    </div>
    <details class="history" id="history"><summary>History</summary><ol id="history-list"><li class="fine">Loading…</li></ol></details>`;
  panel.querySelectorAll(".class-btn").forEach((b) => b.addEventListener("click", () => review({ damage_class: b.dataset.cls })));
  const on = (id, fn) => { const el = $(id, panel); if (el) el.addEventListener("click", fn); };
  on("#act-confirm", () => review({ status: "confirmed" }, true));
  on("#act-reject", () => review({ status: "rejected" }, true));
  on("#act-reset", () => review({ status: "not_reviewed" }));
  on("#act-next", nextUnreviewed);
  on("#act-task", async () => {
    try { await api.post(`/api/incidents/${ctx.store.incident.id}/tasks`, { observation_ids: [o.id] }); toast("Task created.", "success"); await ctx.reload(["observations", "tasks"]); refresh(); }
    catch (err) { toast(err.message, "error"); }
  });
  panel.querySelectorAll(".hz input").forEach((c) => c.addEventListener("change", () =>
    review({ hazards: [...panel.querySelectorAll(".hz input:checked")].map((x) => x.value) })));
  const note = $("#note", panel);
  note.addEventListener("change", () => review({ review_note: note.value }));
  $("#history", panel).addEventListener("toggle", async (e) => {
    if (!e.target.open) return;
    try {
      const detail = await api.get(`/api/observations/${o.id}`);
      $("#history-list", panel).innerHTML = detail.history.length ? detail.history.map((h) =>
        `<li><b>${esc(h.action.replace(/_/g, " "))}</b> by ${esc(h.user || "system")} · ${fmtTime(h.at)}${h.before && h.after && h.before.damage_class !== h.after.damage_class ? `<br><span class="sub">${esc(h.before.damage_class)} → ${esc(h.after.damage_class)}</span>` : ""}</li>`).join("")
        : `<li class="fine">No changes yet.</li>`;
    } catch (err) { toast(err.message, "error"); }
  });
}

// ---------- actions ----------

async function review(body, advance = false) {
  const o = selected();
  if (!o || !canReview() || st.busy) return;
  st.busy = true;
  try {
    const updated = await api.patch(`/api/observations/${o.id}`, body);
    Object.assign(o, updated);
    if (advance) nextUnreviewed(true);
    else refresh();
  } catch (err) { toast(err.message, "error"); }
  st.busy = false;
}

function select(id) {
  st.obsId = id;
  const o = selected();
  if (o && o.image_id !== st.imageId) st.imageId = o.image_id;
  refresh();
}

function nextUnreviewed(fromAction = false) {
  const imgs = images(), start = Math.max(0, imgs.findIndex((i) => i.id === st.imageId));
  for (let k = 0; k < imgs.length; k += 1) {
    const im = imgs[(start + k) % imgs.length];
    const next = obsFor(im.id).find((o) => o.status === "not_reviewed" && (!st.cls || o.damage_class === st.cls));
    if (next) { st.imageId = im.id; st.obsId = next.id; refresh(); return; }
  }
  if (!fromAction) toast("Everything matching the filters is reviewed.", "success");
  refresh();
}

function stepBox(delta) {
  const obs = obsFor(st.imageId).filter((o) => st.showRejected || o.status !== "rejected");
  if (!obs.length) return;
  const i = obs.findIndex((o) => o.id === st.obsId);
  select(obs[(i + delta + obs.length) % obs.length].id);
}

function stepImage(delta) {
  const list = listImages().length ? listImages() : images();
  if (!list.length) return;
  const i = list.findIndex((im) => im.id === st.imageId);
  st.imageId = list[(i + delta + list.length) % list.length].id;
  st.obsId = null;
  refresh();
}

function onKey(e) {
  if (e.target.closest && e.target.closest("input, select, textarea, dialog")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (["1", "2", "3", "4"].includes(k)) review({ damage_class: CLASSES[Number(k) - 1] });
  else if (k === "c" || k === "enter") { e.preventDefault(); review({ status: "confirmed" }, true); }
  else if (k === "x") review({ status: "rejected" }, true);
  else if (k === "n") nextUnreviewed();
  else if (k === "j") stepBox(1);
  else if (k === "k") stepBox(-1);
  else if (k === "]") stepImage(1);
  else if (k === "[") stepImage(-1);
  else if (k === "d") { st.mode = st.mode === "draw" ? "select" : "draw"; renderStage(); }
  else if (k === "e") { st.mode = st.mode === "redraw" ? "select" : "redraw"; renderStage(); }
  else if (k === "s" || k === "escape") { st.mode = "select"; renderStage(); }
}

function bind() {
  $("#f-status", host).addEventListener("change", (e) => { st.status = e.target.value; refresh(); });
  $("#f-class", host).addEventListener("change", (e) => { st.cls = e.target.value; refresh(); });
  $("#image-list", host).addEventListener("click", (e) => {
    const b = e.target.closest(".image-row");
    if (b) { st.imageId = Number(b.dataset.id); st.obsId = null; refresh(); }
  });
  $("#prev-img", host).addEventListener("click", () => stepImage(-1));
  $("#next-img", host).addEventListener("click", () => stepImage(1));
  $("#tool", host).addEventListener("click", (e) => {
    const b = e.target.closest(".seg");
    if (b) { st.mode = b.dataset.mode; renderStage(); }
  });
  $("#show-rejected", host).addEventListener("change", (e) => { st.showRejected = e.target.checked; renderStage(); });
  $("#confirm-image", host).addEventListener("click", async () => {
    const ids = obsFor(st.imageId).filter((o) => o.status === "not_reviewed").map((o) => o.id);
    if (!ids.length) return toast("Nothing unreviewed in this image.");
    try {
      const r = await api.post("/api/observations/bulk", { ids, action: "confirm" });
      toast(`Confirmed ${r.changed}.`, "success");
      await ctx.reload(["observations", "summary"]);
      refresh();
    } catch (err) { toast(err.message, "error"); }
  });
  $("#confirm-image", host).disabled = !canReview();
  const svg = $("#review-svg", host);
  svg.addEventListener("pointerdown", (e) => { if (st.mode === "select") { st.obsId = null; refresh(); } else startDrag(e); });
  svg.addEventListener("pointermove", moveDrag);
  svg.addEventListener("pointerup", endDrag);
  svg.addEventListener("pointercancel", () => { if (st.drag) { st.drag.rect.remove(); st.drag = null; } });
  document.addEventListener("keydown", onKey);
}
