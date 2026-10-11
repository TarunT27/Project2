// Shared UI helpers: escaping, formatting, toasts, modals, icons and domain constants.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const CLASSES = ["No Damage", "Minor Damage", "Major Damage", "Destroyed"];
export const COLORS = { "No Damage": "#16a34a", "Minor Damage": "#eab308", "Major Damage": "#f97316", "Destroyed": "#dc2626" };
export const SHORT = { "No Damage": "Intact", "Minor Damage": "Minor", "Major Damage": "Major", "Destroyed": "Destroyed" };
export const SEVERITY = { "No Damage": 0, "Minor Damage": 1, "Major Damage": 2, "Destroyed": 3 };
export const STATUS_LABEL = { not_reviewed: "Not reviewed", confirmed: "Confirmed", corrected: "Corrected", rejected: "Rejected", added: "Added by reviewer" };
export const STATUS_COLOR = { not_reviewed: "#9a9fa8", confirmed: "#16a34a", corrected: "#5aa9e6", rejected: "#6b7079", added: "#a78bfa" };
export const TASK_LABEL = { open: "Open", assigned: "Assigned", in_progress: "In progress", searched_clear: "Searched – clear", needs_usar: "Needs USAR", done: "Done", cancelled: "Cancelled" };
export const HAZARDS = { fire: "Fire", flooding: "Flooding", debris_blocking_access: "Debris blocking access", gas_or_utility_damage: "Gas / utility damage", possible_entrapment: "Possible entrapment", unstable_structure: "Unstable structure", hazmat: "Hazmat", downed_power_lines: "Downed power lines" };
export const LOCATION_LABEL = { ortho_georef: "Orthomosaic (georeferenced)", telemetry_projection: "Drone telemetry projection", drone_telemetry: "Drone position", exif_camera: "Camera GPS", manual_reference: "Manual reference", synthetic_demo: "Synthetic demo", device_gps: "Device GPS", missing: "No location" };

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
export const pct = (v) => `${Math.round((v || 0) * 100)}%`;
export const pad2 = (n) => String(n).padStart(2, "0");
export const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;
export function validCoord(lat, lon) {
  return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
}
export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
export function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
export function dms(value, pos, neg) {
  const a = Math.abs(value), d = Math.floor(a), mf = (a - d) * 60, m = Math.floor(mf), s = Math.round((mf - m) * 60) % 60;
  return `${d}°${pad2(m)}'${pad2(s)}"${value >= 0 ? pos : neg}`;
}
export function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"], i = Math.min(4, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}
export const pip = (cls) => `<span class="pip" style="background:${COLORS[cls] || "#666"}"></span>`;
export const classPill = (cls) => `<span class="pill">${pip(cls)}${esc(cls)}</span>`;
export const statusPill = (s) => `<span class="pill status" style="--c:${STATUS_COLOR[s] || "#888"}">${esc(STATUS_LABEL[s] || s)}</span>`;
export function hazardChips(list) {
  return (list || []).map((h) => `<span class="chip warn">${esc(HAZARDS[h] || h)}</span>`).join("");
}

export function toast(message, kind = "info", ms) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms || (kind === "error" ? 9000 : 4500));
}

// Modal dialog built on <dialog>; resolves with the submitted FormData (or null when cancelled).
export function modal({ title, body, submit = "Save", danger = false, wide = false, onOpen }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = `modal${wide ? " wide" : ""}`;
    dlg.innerHTML = `<form method="dialog" class="modal-form">
      <div class="modal-head"><h3>${esc(title)}</h3><button type="button" class="icon-btn" data-x aria-label="Close">✕</button></div>
      <div class="modal-body">${body}</div>
      <div class="modal-actions">
        <button type="button" class="btn ghost" data-x>Cancel</button>
        ${submit ? `<button type="submit" class="btn ${danger ? "danger" : "primary"}" value="ok">${esc(submit)}</button>` : ""}
      </div></form>`;
    document.body.appendChild(dlg);
    const form = dlg.querySelector("form");
    let result = null;
    form.addEventListener("submit", (e) => {
      if (!form.reportValidity()) { e.preventDefault(); return; }
      result = new FormData(form);
    });
    dlg.querySelectorAll("[data-x]").forEach((b) => b.addEventListener("click", () => dlg.close()));
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
    dlg.showModal();
    if (onOpen) onOpen(dlg);
    const first = dlg.querySelector("input:not([type=hidden]), select, textarea");
    if (first) first.focus();
  });
}

export async function confirmDialog(title, message, action = "Confirm", danger = true) {
  return (await modal({ title, body: `<p class="muted">${esc(message)}</p>`, submit: action, danger })) !== null;
}

export function download(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

const ICONS = {
  incidents: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  console: '<circle cx="12" cy="12" r="9"/><path d="m9.5 14.5 1.6-4.4 4.4-1.6-1.6 4.4z"/>',
  review: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
  map: '<path d="M9 4 3 6.5v13.5l6-2.5 6 2.5 6-2.5V4l-6 2.5z"/><path d="M9 4v13.5M15 6.5V20"/>',
  tasks: '<rect x="3" y="4" width="5" height="16" rx="1"/><rect x="10" y="4" width="5" height="11" rx="1"/><rect x="17" y="4" width="4" height="7" rx="1"/>',
  export: '<path d="M12 4v11m0 0-4-4m4 4 4-4"/><path d="M5 17v2a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2"/>',
  admin: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  logout: '<path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/>',
  field: '<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>',
  upload: '<path d="M12 15V4m0 0L8 8m4-4 4 4"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  check: '<path d="m5 12 5 5L20 7"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  box: '<rect x="5" y="5" width="14" height="14" rx="1"/>',
  draw: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  chevronL: '<path d="m14 7-5 5 5 5"/>',
  chevronR: '<path d="m10 7 5 5-5 5"/>',
  chevronU: '<path d="m7 14 5-5 5 5"/>',
  chevronD: '<path d="m7 10 5 5 5-5"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  route: '<circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M8 19h6a4 4 0 0 0 0-8h-4a4 4 0 0 1 0-8h6"/>',
  share: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 13.5 6.8 4M15.4 6.5l-6.8 4"/>',
  sparkle: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  grid: '<path d="M4 9h16M4 15h16M9 4v16M15 4v16"/>',
  hud: '<path d="M4 5h6v14H4zM14 5h6v6h-6z"/>',
  tag: '<path d="M4 12V5a1 1 0 0 1 1-1h7l8 8-8 8z"/><circle cx="8.5" cy="8.5" r="1.2"/>',
  pin: '<circle cx="12" cy="10" r="3"/><path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/>',
};
export function icon(name, size = 18) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
}

export function debounce(fn, ms = 250) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export function emptyState(title, text, action = "") {
  return `<div class="empty"><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
}
