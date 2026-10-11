/* Read-only shared incident view for partner agencies (no account). All untrusted text goes through textContent. */
(() => {
  "use strict";

  const token = (location.pathname.split("/").filter(Boolean).pop() || "").trim();
  const BASE = `/api/public/${encodeURIComponent(token)}`;
  const CLASSES = ["Destroyed", "Major Damage", "Minor Damage", "No Damage"];
  const COLOR = { "Destroyed": "#dc2626", "Major Damage": "#f97316", "Minor Damage": "#eab308", "No Damage": "#16a34a" };
  const SHORT = { "Destroyed": "Destroyed", "Major Damage": "Major", "Minor Damage": "Minor", "No Damage": "No damage" };
  const RANK = { "No Damage": 0, "Minor Damage": 1, "Major Damage": 2, "Destroyed": 3 };
  const REVIEW = {
    not_reviewed: "Unreviewed (model output)", confirmed: "Confirmed", corrected: "Corrected",
    added: "Added by analyst", rejected: "Rejected (hidden)",
  };
  const SOURCE = {
    exif_gps: "Photo GPS (camera position)", drone_telemetry: "Drone telemetry (camera position)",
    ortho_georef: "Georeferenced orthomosaic", manual_reference: "Manual reference point",
    device_gps: "Field device GPS", synthetic_demo: "Synthetic demo location", missing: "No location",
  };

  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value == null || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "style") node.style.cssText = value;
        else node.setAttribute(key, value === true ? "" : String(value));
      }
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  function icon(paths) {
    const ns = "http://www.w3.org/2000/svg";
    const s = document.createElementNS(ns, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    for (const d of paths) {
      const p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      s.append(p);
    }
    return s;
  }
  const num = (n) => Number(n || 0).toLocaleString();
  const pct = (n) => `${Math.round(Number(n || 0) * 100)}%`;
  const plural = (n, w) => `${num(n)} ${w}${Number(n) === 1 ? "" : "s"}`;
  const finite = (v) => typeof v === "number" && Number.isFinite(v);
  function when(value) {
    if (!value) return "";
    const d = new Date(String(value));
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  class Gone extends Error {}

  async function getJSON(path) {
    const res = await fetch(BASE + path, { headers: { Accept: "application/json" }, cache: "no-store", credentials: "omit" });
    if (res.status === 404) throw new Gone("gone");
    if (!res.ok) {
      let msg = `The server answered ${res.status}.`;
      try { const d = await res.json(); if (d && d.error) msg = String(d.error); } catch (_) { /* ignore */ }
      throw new Error(msg);
    }
    return res.json();
  }

  // ---------- header, notices, summary ----------
  function renderHeader(info) {
    const inc = info.incident || {};
    document.title = `${inc.name || "Incident"} · Shared assessment`;
    $("inc-name").textContent = inc.name || "Untitled incident";
    const desc = $("inc-desc");
    desc.textContent = inc.description || "";
    desc.hidden = !inc.description;
    const st = $("inc-status");
    if (inc.status) {
      st.textContent = inc.status === "active" ? "Active incident" : inc.status === "closed" ? "Closed incident" : inc.status;
      st.className = `badge-status ${inc.status}`;
      st.hidden = false;
    }
    const fmt = (d) => d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const now = info.generated_at ? new Date(info.generated_at) : new Date();
    const gen = $("generated");
    gen.dateTime = now.toISOString();
    gen.textContent = fmt(now) + (info.expires_at ? ` · link expires ${fmt(new Date(info.expires_at))}` : "");
  }

  function renderNotices(info) {
    const s = info.summary || {};
    const notices = [];
    if (Number(s.demo) > 0) {
      notices.push(el("div", { class: "notice demo", role: "note" },
        icon(["M12 3 2 20h20z", "M12 10v4", "M12 17v.5"]),
        el("div", null, el("b", null, "Contains synthetic demo data. "),
          `${plural(s.demo, "observation")} in this incident were generated for demonstration and do not describe real damage.`)));
    }
    const unreviewed = (s.by_status && s.by_status.not_reviewed) || 0;
    if (unreviewed > 0) {
      notices.push(el("div", { class: "notice model", role: "note" },
        icon(["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 8v5", "M12 16v.5"]),
        el("div", null, el("b", null, "Unreviewed items are model output. "),
          `${plural(unreviewed, "observation")} have not been checked by an analyst yet and may be wrong.`)));
    }
    if (info.incident && info.incident.status === "closed") {
      notices.push(el("div", { class: "notice closed", role: "note" },
        icon(["M5 11h14v10H5z", "M8 11V7a4 4 0 0 1 8 0v4"]),
        el("div", null, "This incident is closed. Figures are final and no longer updated.")));
    }
    $("notices").replaceChildren(...notices);
    $("notices").style.paddingBottom = notices.length ? "4px" : "0";
  }

  function renderSummary(info) {
    const s = info.summary || {};
    const active = s.active_observations != null ? s.active_observations : s.observations;
    $("st-obs").textContent = num(active);
    $("st-obs-sub").textContent = `${num(s.mapped)} on the map · ${num(s.images)} images`;
    $("st-high").textContent = num(s.high_priority);
    $("st-bld").textContent = num(s.buildings);
    $("st-bld-sub").textContent = s.footprints ? `of ${num(s.footprints)} footprints loaded` : "No footprints loaded";
    $("st-rev").textContent = `${Number(s.reviewed_pct || 0).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
    $("st-rev-sub").textContent = `of ${plural(s.observations, "observation")}`;

    const byClass = s.by_class || {};
    const total = CLASSES.reduce((n, c) => n + (byClass[c] || 0), 0);
    $("sev-total").textContent = total ? `${plural(total, "observation")} excluding rejected` : "No observations yet";
    const bar = $("sev-bar");
    bar.replaceChildren(...CLASSES.filter((c) => byClass[c]).map((c) =>
      el("span", { style: `background:${COLOR[c]};flex-grow:${byClass[c]}`, title: `${c}: ${num(byClass[c])}` })));
    bar.setAttribute("aria-label", total
      ? `Damage breakdown: ${CLASSES.map((c) => `${c} ${byClass[c] || 0}`).join(", ")}`
      : "No observations yet");
    $("sev-legend").replaceChildren(...CLASSES.map((c) => el("li", { style: `--c:${COLOR[c]}` },
      el("b", null, num(byClass[c])),
      el("span", null, `${SHORT[c]}${total ? ` · ${Math.round(((byClass[c] || 0) / total) * 100)}%` : ""}`))));

    const byStatus = s.by_status || {};
    $("review-row").replaceChildren(...Object.keys(REVIEW).filter((k) => byStatus[k]).map((k) =>
      el("span", { class: "pill" }, REVIEW[k], el("b", null, num(byStatus[k])))));

    const runs = Array.isArray(s.timeline) ? s.timeline : [];
    $("runs-sub").textContent = runs.length ? plural(runs.length, "run") : "";
    $("runs").replaceChildren(...(runs.length ? [...runs].reverse().map((r) => {
      const parts = [["Destroyed", r.destroyed], ["Major Damage", r.major], ["Minor Damage", r.minor], ["No Damage", r.intact]];
      const sum = parts.reduce((n, p) => n + (p[1] || 0), 0);
      return el("li", null,
        el("div", null,
          el("div", { class: "r-title" }, `Run #${r.run_id}`, el("span", { class: `tag${r.mode === "demo" ? " demo" : ""}` }, r.mode || "run")),
          el("div", { class: "r-sub" }, `${when(r.created_at)} · ${r.status || ""} · ${plural(sum, "detection")}`)),
        el("div", { class: "mini", role: "img", "aria-label": parts.map((p) => `${SHORT[p[0]]} ${p[1] || 0}`).join(", ") },
          parts.filter((p) => p[1]).map((p) => el("span", { style: `background:${COLOR[p[0]]};flex-grow:${p[1]}` }))));
    }) : [el("li", { class: "empty" }, "No assessment runs yet.")]));

    document.querySelectorAll(".dl").forEach((a) => {
      a.href = `${BASE}/export/${a.dataset.fmt}`;
      if (a.dataset.fmt === "buildings" && !s.buildings) {
        a.setAttribute("aria-disabled", "true");
        a.title = "No building footprints matched yet";
      }
    });
    $("content").setAttribute("aria-busy", "false");
  }

  // ---------- map ----------
  let map = null;
  function mapMessage(text) {
    const m = $("map-msg");
    m.textContent = text || "";
    m.hidden = !text;
  }

  function clusterIcon(worst, count) {
    const size = Math.round(22 + Math.min(18, Math.log2(count) * 5));
    const node = el("div", {
      class: `cluster${worst === "Destroyed" ? " light" : ""}`,
      style: `width:${size}px;height:${size}px;background:${COLOR[worst] || "#8b9099"};font-size:${count > 99 ? 10 : 11}px`,
    }, count > 999 ? "999+" : String(count));
    return L.divIcon({ className: "", html: node, iconSize: [size, size], iconAnchor: [size / 2, size / 2], popupAnchor: [0, -size / 2] });
  }

  function observationPopup(group) {
    const sorted = [...group].sort((a, b) => (RANK[b.damage_class] - RANK[a.damage_class]) || (b.confidence - a.confidence));
    const worst = sorted[0];
    const images = [...new Set(sorted.map((o) => o.image_id).filter((id) => id != null))].slice(0, 4);
    const imgs = images.map((id) => {
      const img = el("img", { src: `${BASE}/images/${encodeURIComponent(id)}`, alt: `Source image ${id}`, loading: "lazy" });
      const link = el("a", { href: `${BASE}/images/${encodeURIComponent(id)}?variant=full`, target: "_blank", rel: "noopener noreferrer", title: "Open full image" }, img);
      img.addEventListener("error", () => link.remove(), { once: true });
      return link;
    });
    const shown = sorted.slice(0, 25);
    const sources = [...new Set(sorted.map((o) => SOURCE[o.location_source] || String(o.location_source || "unknown").replace(/_/g, " ")))];
    return el("div", { class: "pop" },
      el("div", { class: "pop-head" },
        el("span", { class: "pop-dot", style: `background:${COLOR[worst.damage_class] || "#8b9099"}` }),
        el("b", null, group.length === 1 ? worst.damage_class : `${plural(group.length, "observation")} · worst ${SHORT[worst.damage_class] || worst.damage_class}`)),
      imgs.length ? el("div", { class: `pop-imgs${imgs.length === 1 ? " one" : ""}` }, imgs) : null,
      el("ul", { class: "pop-list" }, shown.map((o) => el("li", null,
        el("span", { class: "pop-dot", style: `background:${COLOR[o.damage_class] || "#8b9099"}` }),
        el("div", null,
          el("div", null, `${SHORT[o.damage_class] || o.damage_class} · ${pct(o.confidence)}`, o.is_demo ? el("span", { class: "tag demo" }, "demo") : null),
          el("div", { class: "src", title: o.source_file || "" }, o.source_file || `Image ${o.image_id}`)),
        el("span", { class: `st${o.status === "confirmed" || o.status === "corrected" || o.status === "added" ? " ok" : ""}` },
          o.status === "not_reviewed" ? "Model" : (REVIEW[o.status] || o.status))))),
      sorted.length > shown.length ? el("div", { class: "pop-meta" }, `+ ${num(sorted.length - shown.length)} more at this point`) : null,
      el("div", { class: "pop-meta" }, `${worst.latitude.toFixed(5)}, ${worst.longitude.toFixed(5)} · ${sources.join(", ")}`),
      el("div", { class: "pop-note" }, "Camera or reference point, not a verified building location."));
  }

  function buildingPopup(b) {
    return el("div", { class: "pop" },
      el("div", { class: "pop-head" },
        el("span", { class: "pop-dot", style: `background:${COLOR[b.damage_class] || "#8b9099"}` }),
        el("b", null, `Building · ${b.damage_class}`)),
      el("div", { class: "pop-meta" }, `Worst match ${pct(b.confidence)} confidence · ${plural(b.count, "observation")}, ${num(b.reviewed)} reviewed`),
      b.external_id ? el("div", { class: "pop-meta" }, `Footprint ${b.external_id}`) : null,
      b.reviewed ? null : el("div", { class: "pop-note" }, "Not yet reviewed — model output."));
  }

  function validBounds(b) {
    return Array.isArray(b) && b.length === 2 && b.every((p) => Array.isArray(p) && p.length === 2 && p.every(finite));
  }

  function initMap(info) {
    if (!window.L) {
      mapMessage("The map library could not load. Summary and downloads still work.");
      $("map-sub").textContent = "";
      return null;
    }
    const cfg = info.config || {};
    map = L.map("map", { zoomControl: true, worldCopyJump: true, preferCanvas: false, attributionControl: true });
    map.createPane("imagery").style.zIndex = "350";
    const tileUrl = cfg.tile_url || "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
    const dark = L.tileLayer(tileUrl, { attribution: cfg.tile_attribution || "", maxZoom: 19, className: "tiles-dark" });
    const light = L.tileLayer(tileUrl, { attribution: cfg.tile_attribution || "", maxZoom: 19 });
    dark.addTo(map);
    const inc = info.incident || {};
    if (finite(inc.center_lat) && finite(inc.center_lon)) map.setView([inc.center_lat, inc.center_lon], 15);
    else map.setView([20, 0], 2);
    return { base: { "Dark streets": dark, "Standard streets": light } };
  }

  async function loadLayers(info, ctx) {
    const [obsResult, bldResult] = await Promise.allSettled([getJSON("/observations"), getJSON("/buildings")]);
    if (obsResult.status === "rejected" && obsResult.reason instanceof Gone) { showGone(); return; }
    const observations = obsResult.status === "fulfilled" && Array.isArray(obsResult.value) ? obsResult.value : [];
    const buildings = bldResult.status === "fulfilled" && Array.isArray(bldResult.value) ? bldResult.value : [];
    if (!map) return;

    const bounds = L.latLngBounds([]);
    const overlays = {};

    // Imagery first (own pane, under the vectors).
    for (const o of info.overlays || []) {
      if (!validBounds(o.bounds)) continue;
      const layer = L.imageOverlay(`${BASE}/overlays/${encodeURIComponent(o.id)}`, o.bounds, {
        pane: "imagery", opacity: 0.92, alt: o.name || "Imagery overlay", interactive: false,
      });
      layer.addTo(map);
      overlays[`Imagery · ${o.name || o.kind || `#${o.id}`}`] = layer;
      bounds.extend(o.bounds);
    }

    // Building footprints.
    const bLayer = L.featureGroup();
    for (const b of buildings) {
      const rings = (b.polygon || []).map((ring) => (ring || []).filter((p) => Array.isArray(p) && finite(p[0]) && finite(p[1])).map(([lon, lat]) => [lat, lon]))
        .filter((r) => r.length >= 3);
      if (!rings.length) continue;
      const color = COLOR[b.damage_class] || "#8b9099";
      L.polygon(rings, { color, weight: 1.5, opacity: 0.95, fillColor: color, fillOpacity: b.reviewed ? 0.45 : 0.3, dashArray: b.reviewed ? null : "4 3" })
        .bindPopup(() => buildingPopup(b), { maxWidth: 300 })
        .addTo(bLayer);
    }
    if (bLayer.getLayers().length) {
      bLayer.addTo(map);
      overlays[`Building footprints (${num(bLayer.getLayers().length)})`] = bLayer;
      bounds.extend(bLayer.getBounds());
    }

    // Observation points grouped by identical coordinate.
    const groups = new Map();
    for (const o of observations) {
      if (!finite(o.latitude) || !finite(o.longitude)) continue;
      const key = `${o.latitude.toFixed(6)},${o.longitude.toFixed(6)}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(o);
    }
    const oLayer = L.featureGroup();
    const ordered = [...groups.values()].map((g) => ({ g, worst: g.reduce((w, o) => (RANK[o.damage_class] > RANK[w] ? o.damage_class : w), "No Damage") }))
      .sort((a, b) => RANK[a.worst] - RANK[b.worst]); // most severe drawn last (on top)
    for (const { g, worst } of ordered) {
      L.marker([g[0].latitude, g[0].longitude], {
        icon: clusterIcon(worst, g.length), zIndexOffset: RANK[worst] * 100,
        title: `${g.length} observation${g.length === 1 ? "" : "s"}, worst ${worst}`, alt: `${worst} marker`, keyboard: true, riseOnHover: true,
      }).bindPopup(() => observationPopup(g), { maxWidth: 300, minWidth: 240 }).addTo(oLayer);
    }
    const mapped = observations.filter((o) => finite(o.latitude)).length;
    if (oLayer.getLayers().length) {
      oLayer.addTo(map);
      overlays[`Observation points (${num(groups.size)})`] = oLayer;
      bounds.extend(oLayer.getBounds());
    }

    L.control.layers(ctx.base, overlays, { collapsed: window.innerWidth < 720, position: "topright" }).addTo(map);
    L.control.scale({ imperial: false, position: "bottomleft" }).addTo(map);

    if (bounds.isValid()) map.fitBounds(bounds.pad(0.12), { maxZoom: 18 });
    const unmapped = observations.length - mapped;
    $("map-sub").textContent = [
      `${plural(groups.size, "point")} · ${plural(mapped, "observation")}`,
      bLayer.getLayers().length ? plural(bLayer.getLayers().length, "building") : null,
      unmapped ? `${num(unmapped)} without location` : null,
    ].filter(Boolean).join(" · ");
    if (obsResult.status === "rejected") mapMessage("Observation points could not be loaded. Try Refresh.");
    else if (!groups.size && !bLayer.getLayers().length && !(info.overlays || []).length) mapMessage("No mapped observations yet.");
    else mapMessage("");
    setTimeout(() => map.invalidateSize(), 50);
  }

  // ---------- states ----------
  function showGone(message, retry) {
    document.title = retry ? "Shared view unavailable" : "Link expired";
    $("content").hidden = true;
    $("notices").hidden = true;
    document.querySelector(".head").hidden = true;
    $("gone").hidden = false;
    if (message) {
      $("gone-title").textContent = "This shared view could not load";
      $("gone-text").textContent = message;
    }
    $("gone-retry").hidden = !retry;
  }

  async function start() {
    $("reload").addEventListener("click", () => location.reload());
    $("gone-retry").addEventListener("click", () => location.reload());
    window.addEventListener("resize", () => { if (map) map.invalidateSize(); });
    if (!/^[A-Za-z0-9_-]{16,64}$/.test(token)) { showGone(); return; }
    let info;
    try {
      info = await getJSON("");
    } catch (err) {
      if (err instanceof Gone) showGone();
      else showGone(err instanceof TypeError ? "Check your internet connection and try again." : err.message, true);
      return;
    }
    renderHeader(info);
    renderNotices(info);
    renderSummary(info);
    const ctx = initMap(info);
    if (ctx) {
      try { await loadLayers(info, ctx); } catch (err) { console.error(err); mapMessage("Map layers could not be loaded. Try Refresh."); }
    }
  }

  start();
})();
