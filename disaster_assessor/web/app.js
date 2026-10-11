/* Rapid Disaster Assessment — mission console client. No build step. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  const SEVERITY = { "No Damage": 0, "Minor Damage": 1, "Major Damage": 2, "Destroyed": 3 };
  const SHORT = { "No Damage": "Intact", "Minor Damage": "Minor", "Major Damage": "Major", "Destroyed": "Destroyed" };
  const CHIP = { "No Damage": "N", "Minor Damage": "m", "Major Damage": "M", "Destroyed": "D" };
  const DEFAULT_SETTINGS = { mode: "demo", modelPath: "", device: "cpu", override: false, lat: 37.7749, lon: -122.4194, interval: 3, maxFrames: 8, tiles: "dark" };
  const TILES = {
    dark: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png", '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', "tiles-dark"],
    osm: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png", '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', ""],
  };

  const state = {
    colors: { "No Damage": "#16a34a", "Minor Damage": "#eab308", "Major Damage": "#f97316", "Destroyed": "#dc2626" },
    classes: ["No Damage", "Minor Damage", "Major Damage", "Destroyed"],
    settings: loadSettings(),
    run: null,
    queue: [],
    idx: 0,
    filter: "all",
    minConf: 0.5,
    hidden: new Set(),
    // The HUD would cover most of the image on a phone, so start with it tucked away there.
    boxes: true, labels: true, grid: true, hud: !window.matchMedia("(max-width: 760px)").matches,
    view: "console",
    busy: false,
  };

  // ---------- helpers ----------
  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function safeImage(url) {
    return typeof url === "string" && /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(url) ? url : "";
  }
  function pct(v) { return `${Math.round(v * 100)}%`; }
  function pad2(n) { return String(n).padStart(2, "0"); }
  function validCoord(lat, lon) {
    return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
  }
  function dms(value, pos, neg) {
    const a = Math.abs(value), d = Math.floor(a), mf = (a - d) * 60, m = Math.floor(mf), s = Math.round((mf - m) * 60);
    return `${d}°${pad2(m)}'${pad2(s % 60)}"${value >= 0 ? pos : neg}`;
  }
  function loadSettings() {
    try { return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem("dra-settings") || "{}") }; }
    catch { return { ...DEFAULT_SETTINGS }; }
  }
  function saveSettings() {
    try { localStorage.setItem("dra-settings", JSON.stringify(state.settings)); } catch { /* private mode */ }
  }
  function toast(message, kind = "info") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    $("toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "error" ? 9000 : 4500);
  }
  function rank(rows) {
    return [...rows].sort((a, b) =>
      SEVERITY[b.damage_class] - SEVERITY[a.damage_class] || b.confidence - a.confidence ||
      String(a.observation_id).localeCompare(String(b.observation_id)));
  }

  // ---------- derived data ----------
  const visibleRows = () => (state.run ? state.run.rows.filter((r) => r.confidence >= state.minConf - 1e-9) : []);
  const mappedRows = (rows) => rows.filter((r) => validCoord(r.latitude, r.longitude));
  function navImages() {
    if (!state.run) return [];
    return state.run.images.map((img, i) => ({ ...img, i })).filter((img) => state.filter === "all" || img.kind === state.filter);
  }
  function summary(rows) {
    const by = Object.fromEntries(state.classes.map((c) => [c, 0]));
    rows.forEach((r) => { by[r.damage_class] += 1; });
    const high = by["Major Damage"] + by["Destroyed"];
    const si = rows.length ? rows.reduce((s, r) => s + SEVERITY[r.damage_class], 0) / rows.length : 0;
    return { total: rows.length, by, high, si, mapped: mappedRows(rows).length };
  }

  // ---------- run ----------
  function formFor(extra = {}) {
    const s = state.settings, fd = new FormData();
    fd.append("confidence", String(state.minConf));
    fd.append("lat", String(s.lat));
    fd.append("lon", String(s.lon));
    fd.append("override", String(!!s.override));
    Object.entries(extra).forEach(([k, v]) => fd.append(k, v));
    return fd;
  }

  function send(url, form, label) {
    state.busy = true;
    $("scanning").hidden = false;
    $("scan-text").textContent = label;
    $("scan-bar").style.width = "4%";
    renderControls();
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.responseType = "json";
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          $("scan-bar").style.width = `${Math.max(4, (e.loaded / e.total) * 70)}%`;
          if (e.loaded >= e.total) $("scan-text").textContent = "Running detection…";
        }
      };
      xhr.onload = () => {
        $("scan-bar").style.width = "100%";
        const body = xhr.response || {};
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else { toast(body.error || body.detail || `Request failed (${xhr.status})`, "error"); resolve(null); }
      };
      xhr.onerror = () => { toast("Could not reach the assessment server. Is it running?", "error"); resolve(null); };
      xhr.send(form);
    }).then((result) => {
      state.busy = false;
      $("scanning").hidden = true;
      if (result) {
        state.run = result;
        state.idx = 0;
        state.filter = "all";
        state.hidden.clear();
        if (url.endsWith("assess")) state.queue = [];
        result.errors.forEach((e) => toast(e, "error"));
        if (!result.images.length && !result.errors.length) toast("Nothing was processed.", "warn");
        else if (result.images.length && !result.rows.length) toast("No detections met the threshold. That does not mean the buildings are undamaged.", "warn");
        else toast(`${result.summary.total} observations across ${result.summary.processed} ${result.summary.processed === 1 ? "image" : "images/frames"}.`);
      }
      render();
    });
  }

  function runQueued() {
    if (state.busy) return;
    if (!state.queue.length) { $("file-input").click(); return; }
    const s = state.settings;
    const fd = formFor({ mode: s.mode, model_path: s.modelPath, device: s.device, interval: String(s.interval), max_frames: String(s.maxFrames) });
    state.queue.forEach((f) => fd.append("files", f, f.name));
    send("/api/assess", fd, `Uploading ${state.queue.length} file${state.queue.length > 1 ? "s" : ""}…`);
  }

  function runDemo() {
    if (state.busy) return;
    if (state.settings.mode !== "demo") setMode("demo");
    send("/api/demo", formFor(), "Generating synthetic scenes…");
  }

  function queueFiles(list) {
    const ok = [...list].filter((f) => /\.(jpe?g|png|webp|tiff?|mp4|mov|avi|mkv)$/i.test(f.name));
    if (ok.length < list.length) toast("Some files were skipped: unsupported type.", "warn");
    const merged = [...state.queue, ...ok].slice(0, 20);
    if (state.queue.length + ok.length > 20) toast("Batches are limited to 20 files.", "warn");
    const bytes = merged.reduce((s, f) => s + f.size, 0);
    if (bytes > 200 * 1024 * 1024) { toast("Batches are limited to 200 MB total.", "error"); return; }
    state.queue = merged;
    renderControls();
    if (ok.length && !state.run) runQueued();
  }

  // ---------- render: viewport ----------
  function renderViewport() {
    const vp = $("viewport"), stage = $("vp-stage");
    vp.classList.toggle("grid-off", !state.grid);
    vp.classList.toggle("hud-off", !state.hud);
    stage.classList.toggle("no-boxes", !state.boxes);
    const imgs = navImages();
    const has = !!(state.run && state.run.images.length);
    $("dropzone").hidden = has;
    stage.replaceChildren();
    if (!has) {
      $("vp-backdrop").style.backgroundImage = "";
      $("vp-caption").textContent = "";
      $("pager").textContent = "";
      drawHistogram(null);
      return;
    }
    if (!imgs.find((x) => x.i === state.idx)) state.idx = imgs.length ? imgs[0].i : 0;
    const img = state.run.images[state.idx];
    const src = safeImage(img.original);
    $("vp-backdrop").style.backgroundImage = src ? `url("${src}")` : "";
    stage.setAttribute("viewBox", `0 0 ${img.width} ${img.height}`);
    const pic = document.createElementNS(SVGNS, "image");
    pic.setAttribute("href", src);
    pic.setAttribute("width", img.width);
    pic.setAttribute("height", img.height);
    stage.appendChild(pic);

    const sw = Math.max(2, img.width / 320), fs = Math.max(12, Math.min(26, img.width / 44));
    const rows = visibleRows().filter((r) => r.image_index === state.idx && !state.hidden.has(r.damage_class));
    rows.sort((a, b) => SEVERITY[a.damage_class] - SEVERITY[b.damage_class]).forEach((r) => {
      const [x1, y1, x2, y2] = r.bbox, color = state.colors[r.damage_class];
      const g = document.createElementNS(SVGNS, "g");
      g.setAttribute("class", "box");
      g.style.setProperty("--sw", sw);
      const title = document.createElementNS(SVGNS, "title");
      title.textContent = `${r.damage_class} · ${pct(r.confidence)}${r.is_demo ? " · synthetic" : ""}`;
      const rect = document.createElementNS(SVGNS, "rect");
      Object.entries({ class: "frame", x: x1, y: y1, width: x2 - x1, height: y2 - y1, rx: sw, stroke: color }).forEach(([k, v]) => rect.setAttribute(k, v));
      g.append(title, rect);
      if (state.labels) {
        const text = `${r.is_demo ? "DEMO · " : ""}${SHORT[r.damage_class]} ${pct(r.confidence)}`;
        const w = text.length * fs * 0.58 + fs * 0.8, h = fs * 1.5, ty = y1 - h < 0 ? y1 : y1 - h;
        const tag = document.createElementNS(SVGNS, "rect");
        Object.entries({ class: "tag", x: x1, y: ty, width: w, height: h, rx: fs * 0.25, fill: color }).forEach(([k, v]) => tag.setAttribute(k, v));
        const t = document.createElementNS(SVGNS, "text");
        Object.entries({ x: x1 + fs * 0.4, y: ty + h * 0.7, "font-size": fs }).forEach(([k, v]) => t.setAttribute(k, v));
        t.textContent = text;
        if (r.damage_class === "Minor Damage") t.style.fill = "#111";
        g.append(tag, t);
      }
      stage.appendChild(g);
    });

    const loc = img.location_source.replace(/_/g, " ");
    $("vp-caption").textContent = `${img.label} · ${rows.length} shown · location: ${loc}`;
    const pos = imgs.findIndex((x) => x.i === state.idx);
    $("pager").textContent = `${pad2(pos + 1)} / ${pad2(imgs.length)}`;
    drawHistogram(src);
  }

  let histoToken = 0;
  function drawHistogram(src) {
    const path = $("histo").querySelector("path");
    if (!src) { path.setAttribute("d", ""); return; }
    const token = ++histoToken, im = new Image();
    im.onload = () => {
      if (token !== histoToken) return;
      const c = document.createElement("canvas"), w = 160, h = Math.max(1, Math.round(160 * im.height / im.width));
      c.width = w; c.height = h;
      const ctx = c.getContext("2d");
      ctx.drawImage(im, 0, 0, w, h);
      const data = ctx.getImageData(0, 0, w, h).data, bins = new Array(48).fill(0);
      for (let i = 0; i < data.length; i += 4) {
        const l = 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
        bins[Math.min(47, Math.floor(l / 256 * 48))] += 1;
      }
      const max = Math.max(...bins.map((b) => Math.sqrt(b))) || 1;
      let d = "M0 40";
      bins.forEach((b, i) => { d += ` L${(i / 47) * 100} ${40 - (Math.sqrt(b) / max) * 38}`; });
      path.setAttribute("d", `${d} L100 40 Z`);
    };
    im.src = src;
  }

  // ---------- render: HUD, unit, telemetry ----------
  function renderControls() {
    const s = state.settings, run = state.run, demo = run ? run.demo : s.mode === "demo";
    const rows = visibleRows(), sum = summary(rows);

    // status + mode
    const chip = $("status-chip");
    chip.classList.toggle("real", !demo);
    chip.querySelector(".long").textContent = demo ? "Demo workspace · synthetic detections" : "Unverified model output · human review required";
    chip.querySelector(".short").textContent = demo ? "Demo" : "Unverified";
    $("mode-badge").textContent = s.mode === "demo" ? "D" : "L";
    $("mode-badge").classList.toggle("real", s.mode !== "demo");
    $("mode-badge").title = s.mode === "demo" ? "Demo mode" : "Live model mode";
    $("hud-mode").textContent = demo ? "DEMO" : "LIVE";

    // HUD
    const cur = run && run.images[state.idx];
    $("hud-res").textContent = cur ? `${cur.width}×${cur.height}` : "—";
    $("hud-frames").textContent = run ? `${run.images.length} frame${run.images.length === 1 ? "" : "s"}` : "0 frames";
    $("hud-level").style.width = pct(state.minConf);
    $("hud-level-val").textContent = pct(state.minConf);
    const curRows = rows.filter((r) => cur && r.image_index === state.idx);
    $("hud-classes").innerHTML = state.classes.map((c) =>
      `<span><i style="background:${state.colors[c]}"></i>${CHIP[c]}<em>${curRows.filter((r) => r.damage_class === c).length}</em></span>`).join("");
    $("obs-chip").textContent = `${pad2(curRows.length)} OBS`;
    $("toggle-boxes").setAttribute("aria-pressed", String(state.boxes));

    // unit card
    $("unit-title").textContent = s.mode === "demo" ? "Synthetic detector" : (s.modelPath.split(/[\\/]/).pop() || "YOLO damage model");
    $("unit-sub").textContent = s.mode === "demo" ? "Demo boxes · no model loaded" : `Ultralytics YOLO · ${s.device.toUpperCase()}`;
    $("high-pct").textContent = sum.total ? `${Math.round(sum.high / sum.total * 100)}%` : "—";
    $("high-sub").textContent = sum.total ? `${sum.high} of ${sum.total} · major + destroyed` : "Major + destroyed";
    const conf = $("conf");
    conf.value = state.minConf;
    conf.style.setProperty("--p", pct(state.minConf));
    $("conf-val").textContent = pct(state.minConf);
    $("conf-hint").textContent = run && state.minConf < run.threshold - 1e-9 ? `This run kept detections ≥ ${pct(run.threshold)}. Rerun to include lower scores.` : "";
    const mappedShare = sum.total ? sum.mapped / sum.total : 0;
    $("mapped-fill").style.width = pct(mappedShare);
    $("mapped-thumb").style.left = pct(mappedShare);
    $("mapped-val").textContent = sum.mapped;
    $("toggle-grid").classList.toggle("on", state.grid);
    $("toggle-grid").setAttribute("aria-pressed", String(state.grid));
    $("toggle-hud").classList.toggle("on", state.hud);
    $("toggle-hud").setAttribute("aria-pressed", String(state.hud));
    $("toggle-labels").classList.toggle("on", state.labels);
    $("toggle-labels").setAttribute("aria-pressed", String(state.labels));
    const q = state.queue.length;
    $("run").disabled = !q || state.busy;
    $("run").textContent = state.busy ? "Assessing…" : q ? `Run assessment · ${q} file${q > 1 ? "s" : ""}` : "Run assessment";
    $("queue").textContent = q ? state.queue.map((f) => f.name).slice(0, 3).join(", ") + (q > 3 ? ` +${q - 3}` : "") : "Drop files on the viewport or use the rail";
    $("dp-center").disabled = state.busy;
    $("dp-center").title = q ? "Run assessment" : "Choose files";

    // telemetry
    const counts = { all: run ? run.images.length : 0, image: 0, video: 0 };
    (run ? run.images : []).forEach((im) => { counts[im.kind] += 1; });
    document.querySelectorAll(".telemetry .seg").forEach((b) => {
      b.classList.toggle("active", b.dataset.filter === state.filter);
      b.disabled = b.dataset.filter !== "all" && !counts[b.dataset.filter];
    });
    const strip = $("strip");
    strip.replaceChildren(...navImages().map((im) => {
      const b = document.createElement("button");
      b.className = `thumb${im.i === state.idx ? " active" : ""}`;
      b.title = im.label;
      const ticks = rows.filter((r) => r.image_index === im.i).map((r) => `<i style="background:${state.colors[r.damage_class]}"></i>`).join("");
      b.innerHTML = `<img src="${safeImage(im.preview)}" alt=""><span class="ticks">${ticks}</span>`;
      b.addEventListener("click", () => { state.idx = im.i; render(); });
      return b;
    }));
    // Scroll only the strip itself; scrollIntoView would also shift overflow-hidden ancestors.
    const active = strip.querySelector(".active");
    if (active) {
      const left = active.offsetLeft - strip.offsetLeft, right = left + active.offsetWidth;
      if (left < strip.scrollLeft) strip.scrollLeft = left;
      else if (right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = right - strip.clientWidth;
    }

    $("s-total").textContent = sum.total;
    $("s-threshold").textContent = pct(state.minConf);
    $("s-mapped").textContent = `${sum.mapped}/${sum.total}`;
    $("s-model").textContent = run ? run.model : "—";
    $("s-frames").textContent = run ? run.images.length : 0;
    $("s-location").textContent = cur ? cur.location_source.replace(/_/g, " ") : "—";

    const order = [...state.classes].reverse(), top = order.find((c) => sum.by[c] > 0);
    $("sevlist").innerHTML = order.map((c) =>
      `<li data-cls="${esc(c)}" class="${c === top ? "top" : ""}${state.hidden.has(c) ? " off" : ""}" title="Show / hide ${esc(c)}"><span>${SHORT[c]}</span><b>${sum.by[c]}</b></li>`).join("");

    const links = run ? exportLinks() : null;
    [["quick-csv", "csv"], ["quick-geo", "geo"], ["dl-csv", "csv"], ["dl-geo", "geo"]].forEach(([id, k]) => {
      const a = $(id);
      if (links) { a.href = links[k].href; a.setAttribute("download", links[k].name); a.setAttribute("aria-disabled", "false"); }
      else { a.removeAttribute("href"); a.setAttribute("aria-disabled", "true"); }
    });

    renderDial(sum);
  }

  function exportLinks() {
    const run = state.run, prefix = run.demo ? "DEMO_" : "UNVERIFIED_", q = `?min_conf=${state.minConf}`;
    return {
      csv: { href: `/api/runs/${encodeURIComponent(run.run_id)}/export.csv${q}`, name: `${prefix}damage_review_priority.csv` },
      geo: { href: `/api/runs/${encodeURIComponent(run.run_id)}/export.geojson${q}`, name: `${prefix}damage_observations.geojson` },
    };
  }

  // ---------- dial ----------
  function renderDial(sum) {
    const svg = $("dial"), cx = 110, cy = 110, parts = [];
    const polar = (r, deg) => [cx + r * Math.sin(deg * Math.PI / 180), cy - r * Math.cos(deg * Math.PI / 180)];
    // Outer ring: 72 ticks, coloured by share of each class (distribution).
    const ticks = 72, order = state.classes;
    let cursor = 0;
    const tickColors = new Array(ticks).fill("#3a3c42");
    if (sum.total) {
      order.forEach((c) => {
        const n = Math.round((sum.by[c] / sum.total) * ticks);
        for (let k = 0; k < n && cursor < ticks; k += 1) tickColors[cursor++] = state.colors[c];
      });
      while (cursor < ticks && cursor > 0) tickColors[cursor] = tickColors[cursor - 1], cursor += 1;
    }
    for (let k = 0; k < ticks; k += 1) {
      const a = (k / ticks) * 360, [x1, y1] = polar(100, a), [x2, y2] = polar(k % 6 === 0 ? 90 : 93, a);
      parts.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${tickColors[k]}" stroke-width="${k % 6 === 0 ? 2 : 1.6}"/>`);
    }
    [0, 25, 50, 75].forEach((p, i) => {
      const [x, y] = polar(81, i * 90);
      parts.push(`<text x="${x}" y="${y + 3}">${p}%</text>`);
    });
    // Inner gauge: severity index 0..3 mapped to -135..135 degrees.
    parts.push(`<circle cx="${cx}" cy="${cy}" r="66" fill="#0e0f11" stroke="#2c2e33"/>`);
    for (let a = -135; a <= 135; a += 9) {
      const [x1, y1] = polar(64, a), [x2, y2] = polar(a % 45 === 0 ? 56 : 60, a);
      parts.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${a % 45 === 0 ? "#9a9ea6" : "#4a4d54"}" stroke-width="1"/>`);
    }
    order.forEach((c, i) => {
      const [x, y] = polar(45, -135 + i * 90);
      parts.push(`<text class="card-lbl" x="${x}" y="${y + 4}" style="fill:${state.colors[c]}">${CHIP[c] === "m" ? "m" : CHIP[c]}</text>`);
    });
    parts.push(`<line x1="${cx - 30}" y1="${cy}" x2="${cx + 30}" y2="${cy}" stroke="#2c2e33"/><line x1="${cx}" y1="${cy - 30}" x2="${cx}" y2="${cy + 30}" stroke="#2c2e33"/>`);
    const angle = -135 + (sum.si / 3) * 270;
    parts.push(`<g transform="rotate(${angle} ${cx} ${cy})"><path d="M${cx} ${cy - 30} L${cx + 8} ${cy + 10} L${cx} ${cy + 4} L${cx - 8} ${cy + 10} Z" fill="#fff" stroke="none"/></g>`);
    parts.push(`<circle cx="${cx}" cy="${cy}" r="3" fill="#0e0f11" stroke="#fff" stroke-width="1.2"/>`);
    svg.innerHTML = parts.join("");
    svg.setAttribute("aria-label", `Severity index ${sum.si.toFixed(2)} of 3. ` + order.map((c) => `${c}: ${sum.by[c]}`).join(", "));
    $("si-chip").textContent = `SI ${sum.si.toFixed(2)}`;
    $("si-chip").title = "Severity index: mean of Intact 0, Minor 1, Major 2, Destroyed 3";
    $("dial-legend").innerHTML = order.map((c) =>
      `<span><i style="background:${state.colors[c]}"></i>${SHORT[c]} <b>${sum.total ? Math.round(sum.by[c] / sum.total * 100) : 0}%</b></span>`).join("");
    const mapped = mappedRows(visibleRows());
    const [lat, lon] = mapped.length
      ? [mapped.reduce((s, r) => s + r.latitude, 0) / mapped.length, mapped.reduce((s, r) => s + r.longitude, 0) / mapped.length]
      : [state.settings.lat, state.settings.lon];
    $("coords").textContent = `${dms(lat, "N", "S")}  ${dms(lon, "E", "W")}${mapped.length ? "" : " · ref"}`;
  }

  // ---------- maps ----------
  let mini = null, miniLayer = null, big = null, bigLayer = null, bigTiles = null, markerIndex = new Map();

  function tileLayer(kind) {
    const t = TILES[kind];
    return t ? L.tileLayer(t[0], { attribution: t[1], maxZoom: 19, className: t[2] }) : null;
  }
  function pointIcon(color, count) {
    const size = count > 1 ? 26 : 16;
    return L.divIcon({ className: "", iconSize: [size, size], html: `<span class="pt" style="width:${size}px;height:${size}px;background:${color};box-shadow:0 0 0 4px ${color}40,0 2px 8px #0008">${count > 1 ? count : ""}</span>` });
  }
  // One popup per capture point: every observation from that location, most severe first.
  function popupHtml(list, focusId) {
    const focus = list.find((r) => r.observation_id === focusId) || list[0];
    const img = state.run.images[focus.image_index], src = safeImage(img && img.preview);
    const tag = focus.is_demo ? "SYNTHETIC DEMO" : "UNVERIFIED MODEL OUTPUT";
    const items = list.slice(0, 12).map((r) =>
      `<li class="${r === focus ? "focus" : ""}"><span class="pip" style="background:${state.colors[r.damage_class]}"></span><b>${esc(SHORT[r.damage_class])}</b><span>${pct(r.confidence)}</span><em>${esc(r.source_file)}${r.frame_seconds != null ? ` · ${Number(r.frame_seconds).toFixed(1)}s` : ""}</em></li>`).join("");
    return `<div class="popup" style="width:250px"><div class="tag">${tag}</div>${src ? `<img src="${src}" alt="Annotated preview of ${esc(focus.source_file)}">` : ""}
      <h4>${list.length} observation${list.length > 1 ? "s" : ""} at this point</h4>
      <ul class="pop-list">${items}</ul>${list.length > 12 ? `<div class="meta">+${list.length - 12} more in the export</div>` : ""}
      <div class="meta">Location: ${esc(focus.location_source.replace(/_/g, " "))} · Capture time: ${esc(focus.timestamp || "unknown")}</div>
      <div class="meta" style="color:#5d626b">Camera or reference point, not a verified building location.</div></div>`;
  }

  function renderMini() {
    if (typeof L === "undefined") return;
    const rows = mappedRows(visibleRows()).filter((r) => !state.hidden.has(r.damage_class));
    $("minimap-empty").hidden = rows.length > 0;
    if (!mini) {
      mini = L.map("minimap", { maxZoom: 19, zoomControl: false, attributionControl: false, dragging: true, scrollWheelZoom: false, keyboard: false });
      (tileLayer("dark") || L.layerGroup()).addTo(mini);
      miniLayer = L.layerGroup().addTo(mini);
      mini.setView([state.settings.lat, state.settings.lon], 13);
    }
    miniLayer.clearLayers();
    rows.forEach((r) => {
      const current = r.image_index === state.idx;
      L.circleMarker([r.latitude, r.longitude], { radius: current ? 5 : 3.5, color: current ? "#fff" : state.colors[r.damage_class], weight: current ? 2 : 0, fillColor: state.colors[r.damage_class], fillOpacity: 1 }).addTo(miniLayer);
    });
    mini.invalidateSize();
    if (rows.length) mini.fitBounds(L.latLngBounds(rows.map((r) => [r.latitude, r.longitude])).pad(0.25), { maxZoom: 15 });
  }

  function renderBigMap() {
    if (typeof L === "undefined") { $("map-empty").hidden = false; return; }
    const rows = rank(mappedRows(visibleRows()).filter((r) => !state.hidden.has(r.damage_class)));
    if (!big) {
      big = L.map("bigmap", { zoomControl: false, maxZoom: 19, minZoom: 2 }).setView([state.settings.lat, state.settings.lon], 13);
      bigLayer = L.markerClusterGroup({
        showCoverageOnHover: false, maxClusterRadius: 40, spiderfyOnMaxZoom: true,
        iconCreateFunction: (cluster) => {
          const worst = cluster.getAllChildMarkers().reduce((m, k) => Math.max(m, SEVERITY[k.options.cls]), 0);
          const color = state.colors[state.classes[worst]];
          return L.divIcon({ className: "marker-cluster", iconSize: [38, 38], html: `<div style="width:30px;height:30px;margin:4px;border-radius:50%;display:grid;place-items:center;border:2px solid ${color}">${cluster.getAllChildMarkers().reduce((n, k) => n + k.options.count, 0)}</div>` });
        },
      }).addTo(big);
      L.control.zoom({ position: "bottomright" }).addTo(big);
    }
    if (bigTiles) big.removeLayer(bigTiles);
    bigTiles = tileLayer(state.settings.tiles);
    if (bigTiles) bigTiles.addTo(big);
    document.querySelectorAll("#tile-switch .seg").forEach((b) => b.classList.toggle("active", b.dataset.tiles === state.settings.tiles));

    bigLayer.clearLayers();
    markerIndex = new Map();
    const points = new Map();
    rows.forEach((r) => {
      const key = `${r.latitude.toFixed(6)},${r.longitude.toFixed(6)}`;
      if (!points.has(key)) points.set(key, []);
      points.get(key).push(r);
    });
    points.forEach((list) => {
      const worst = list[0].damage_class;
      const m = L.marker([list[0].latitude, list[0].longitude], {
        icon: pointIcon(state.colors[worst], list.length), cls: worst, count: list.length,
        title: `${list.length} observation(s), most severe: ${worst}`,
      });
      m.bindPopup(() => popupHtml(list, m.focusId), { maxWidth: 290, autoPanPaddingTopLeft: [20, 70] });
      m.on("popupclose", () => { m.focusId = null; });
      bigLayer.addLayer(m);
      list.forEach((r) => markerIndex.set(r.observation_id, m));
    });
    $("map-empty").hidden = rows.length > 0;
    if (!state.run || !visibleRows().length) {
      $("map-empty").querySelector("h2").textContent = "Your map starts with imagery";
      $("map-empty").querySelector("p").textContent = "Run an assessment to place observations here.";
    } else if (!rows.length) {
      $("map-empty").querySelector("h2").textContent = "No coordinates in this run";
      $("map-empty").querySelector("p").textContent = "Turn on the manual reference point in Settings and rerun, or use images with GPS metadata.";
    }
    big.invalidateSize();
    if (rows.length) big.fitBounds(L.latLngBounds(rows.map((r) => [r.latitude, r.longitude])).pad(0.2), { maxZoom: 16 });

    $("map-legend").innerHTML = [...state.classes].reverse().map((c) => `<span><i style="background:${state.colors[c]}"></i>${SHORT[c]}</span>`).join("");
    const all = rank(visibleRows());
    $("map-sub").textContent = all.length ? `${rows.length} mapped of ${all.length} · most severe first` : "Most severe first · select to locate";
    $("obs-list").innerHTML = all.length ? "" : `<li class="fine" style="padding:8px">No observations yet.</li>`;
    all.forEach((r, i) => {
      const li = document.createElement("li"), img = state.run.images[r.image_index];
      li.innerHTML = `<button><span class="rank">${i + 1}</span><img src="${safeImage(img && img.preview)}" alt=""><span><span class="cls"><span class="pip" style="background:${state.colors[r.damage_class]}"></span>${esc(SHORT[r.damage_class])}</span><span class="name">${esc(r.source_file)}</span></span><span class="score">${pct(r.confidence)}</span></button>`;
      li.firstElementChild.addEventListener("click", () => {
        const m = markerIndex.get(r.observation_id);
        if (m) { m.focusId = r.observation_id; m.closePopup(); bigLayer.zoomToShowLayer(m, () => m.openPopup()); }
        else toast("This observation has no coordinates.", "warn");
      });
      $("obs-list").appendChild(li);
    });
  }

  // ---------- export ----------
  function renderExport() {
    const rows = rank(visibleRows()), sum = summary(rows);
    $("e-total").textContent = sum.total;
    $("e-high").textContent = sum.high;
    $("e-mapped").textContent = sum.mapped;
    $("export-sub").textContent = state.run
      ? `${sum.total} observations ≥ ${pct(state.minConf)} · ${state.run.demo ? "synthetic demo" : "unverified model output"} · exports match this filter`
      : "Run an assessment to build the queue.";
    const body = $("queue-body");
    if (!rows.length) { body.innerHTML = `<tr class="empty-row"><td colspan="8">No findings to export yet.</td></tr>`; return; }
    body.innerHTML = rows.slice(0, 500).map((r, i) => {
      const img = state.run.images[r.image_index];
      const coords = validCoord(r.latitude, r.longitude) ? `${r.latitude.toFixed(5)}, ${r.longitude.toFixed(5)}` : "—";
      return `<tr><td class="dim">${i + 1}</td><td><img src="${safeImage(img && img.preview)}" alt=""></td>
        <td><span class="pill"><span class="pip" style="background:${state.colors[r.damage_class]}"></span>${esc(r.damage_class)}</span></td>
        <td>${pct(r.confidence)}</td><td class="src" title="${esc(r.source_file)}">${esc(r.source_file)}${r.frame_seconds != null ? ` · ${Number(r.frame_seconds).toFixed(1)}s` : ""}</td>
        <td class="dim">${esc(r.location_source.replace(/_/g, " "))}</td><td class="dim">${coords}</td><td class="dim">not reviewed</td></tr>`;
    }).join("");
  }

  // ---------- orchestration ----------
  function render() {
    renderViewport();
    renderControls();
    if (state.view === "console") renderMini();
    if (state.view === "map") renderBigMap();
    if (state.view === "export") renderExport();
  }

  function setView(view) {
    state.view = view;
    document.querySelectorAll(".rail-btn[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === view));
    render();
  }

  function setMode(mode) {
    state.settings.mode = mode;
    saveSettings();
    document.querySelectorAll("#mode-switch .seg").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    $("real-fields").hidden = mode !== "real";
    $("load-demo").disabled = false;
    renderControls();
  }

  function step(delta) {
    const imgs = navImages();
    if (!imgs.length) return;
    const pos = imgs.findIndex((x) => x.i === state.idx);
    state.idx = imgs[(pos + delta + imgs.length) % imgs.length].i;
    render();
  }
  function nudgeConf(delta) {
    state.minConf = Math.round(Math.min(1, Math.max(0, state.minConf + delta)) * 20) / 20;
    render();
  }

  function openDrawer(id) {
    document.querySelectorAll(".drawer").forEach((d) => { d.classList.remove("open"); d.setAttribute("aria-hidden", "true"); });
    const d = $(id);
    d.classList.add("open");
    d.setAttribute("aria-hidden", "false");
    $("scrim").hidden = false;
    d.querySelector("[data-close]").focus();
  }
  function closeDrawers() {
    document.querySelectorAll(".drawer").forEach((d) => { d.classList.remove("open"); d.setAttribute("aria-hidden", "true"); });
    $("scrim").hidden = true;
  }

  function bind() {
    document.querySelectorAll(".rail-btn[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
    $("open-settings").addEventListener("click", () => openDrawer("settings"));
    $("open-privacy").addEventListener("click", () => openDrawer("privacy"));
    $("open-files").addEventListener("click", () => $("file-input").click());
    document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", closeDrawers));
    $("scrim").addEventListener("click", closeDrawers);
    $("clear-session").addEventListener("click", () => {
      state.run = null; state.queue = []; state.idx = 0; state.hidden.clear();
      render();
      toast("Session cleared. Results were removed from this page.");
    });

    $("browse").addEventListener("click", () => $("file-input").click());
    $("load-demo").addEventListener("click", runDemo);
    $("file-input").addEventListener("change", (e) => { queueFiles(e.target.files); e.target.value = ""; });
    $("run").addEventListener("click", runQueued);
    $("dp-center").addEventListener("click", runQueued);
    $("dp-left").addEventListener("click", () => step(-1));
    $("dp-right").addEventListener("click", () => step(1));
    $("dp-up").addEventListener("click", () => nudgeConf(0.05));
    $("dp-down").addEventListener("click", () => nudgeConf(-0.05));
    $("conf").addEventListener("input", (e) => { state.minConf = Number(e.target.value); render(); });
    $("hud-mode").addEventListener("click", () => openDrawer("settings"));

    const toggles = { "toggle-boxes": "boxes", "toggle-grid": "grid", "toggle-hud": "hud", "toggle-labels": "labels" };
    Object.entries(toggles).forEach(([id, key]) => $(id).addEventListener("click", () => { state[key] = !state[key]; render(); }));

    document.querySelectorAll(".telemetry .seg").forEach((b) => b.addEventListener("click", () => { state.filter = b.dataset.filter; render(); }));
    $("sevlist").addEventListener("click", (e) => {
      const li = e.target.closest("li");
      if (!li) return;
      const c = li.dataset.cls;
      state.hidden.has(c) ? state.hidden.delete(c) : state.hidden.add(c);
      render();
    });

    document.querySelectorAll("#tile-switch .seg").forEach((b) => b.addEventListener("click", () => {
      state.settings.tiles = b.dataset.tiles; saveSettings(); renderBigMap();
    }));

    // Drag & drop anywhere on the viewport.
    const vp = $("viewport");
    let depth = 0;
    vp.addEventListener("dragenter", (e) => { e.preventDefault(); depth += 1; vp.classList.add("dragging"); });
    vp.addEventListener("dragover", (e) => e.preventDefault());
    vp.addEventListener("dragleave", () => { depth -= 1; if (depth <= 0) { depth = 0; vp.classList.remove("dragging"); } });
    vp.addEventListener("drop", (e) => { e.preventDefault(); depth = 0; vp.classList.remove("dragging"); queueFiles(e.dataTransfer.files); });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => e.preventDefault());

    // Settings drawer fields.
    const s = state.settings;
    document.querySelectorAll("#mode-switch .seg").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
    const bindField = (id, key, parse = (v) => v, prop = "value") => {
      const el = $(id);
      el[prop] = s[key];
      el.addEventListener(el.type === "range" ? "input" : "change", () => {
        const v = parse(el[prop]);
        if (typeof v === "number" && !Number.isFinite(v)) { el[prop] = s[key]; return; }
        s[key] = v; saveSettings();
        $("interval-out").textContent = s.interval;
        $("frames-out").textContent = s.maxFrames;
        renderControls();
      });
    };
    bindField("model-path", "modelPath", (v) => v.trim());
    bindField("device", "device");
    bindField("override", "override", Boolean, "checked");
    bindField("lat", "lat", (v) => { const n = parseFloat(v); return Math.abs(n) <= 90 ? n : NaN; });
    bindField("lon", "lon", (v) => { const n = parseFloat(v); return Math.abs(n) <= 180 ? n : NaN; });
    bindField("interval", "interval", Number);
    bindField("max-frames", "maxFrames", Number);
    $("interval-out").textContent = s.interval;
    $("frames-out").textContent = s.maxFrames;
    // Paint the filled part of every range track.
    document.querySelectorAll('input[type="range"]').forEach((el) => {
      const paint = () => el.style.setProperty("--p", `${((el.value - el.min) / (el.max - el.min)) * 100}%`);
      el.addEventListener("input", paint);
      paint();
    });
    setMode(s.mode);

    document.addEventListener("keydown", (e) => {
      if (e.target.closest?.("input, select, textarea")) return;
      if (e.key === "Escape") return closeDrawers();
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "arrowleft") step(-1);
      else if (k === "arrowright") step(1);
      else if (k in { b: 1, g: 1, h: 1, l: 1 }) {
        const key = { b: "boxes", g: "grid", h: "hud", l: "labels" }[k];
        state[key] = !state[key];
        render();
      }
    });
    window.addEventListener("resize", () => { mini && mini.invalidateSize(); big && big.invalidateSize(); });
  }

  async function init() {
    try {
      const cfg = await fetch("/api/config").then((r) => r.json());
      state.classes = cfg.classes;
      state.colors = cfg.colors;
    } catch { toast("Could not load server configuration.", "error"); }
    if (typeof L === "undefined") toast("Map library failed to load (offline?). Maps are unavailable; assessment and exports still work.", "warn");
    bind();
    render();
  }

  init();
})();
