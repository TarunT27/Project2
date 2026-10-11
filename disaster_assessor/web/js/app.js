// Console shell: authentication, hash router, incident data store and live updates.
import { api, liveEvents, setUnauthorizedHandler } from "./api.js";
import { $, $$, debounce, esc, icon, toast } from "./ui.js";

const VIEWS = {
  incidents: () => import("./views/incidents.js"),
  console: () => import("./views/console.js"),
  review: () => import("./views/review.js"),
  map: () => import("./views/map.js"),
  tasks: () => import("./views/tasks.js"),
  export: () => import("./views/exports.js"),
  admin: () => import("./views/admin.js"),
};
const INCIDENT_NAV = [
  ["console", "console", "Console"], ["review", "review", "Review"], ["map", "map", "Rescue map"],
  ["tasks", "tasks", "Tasks & crews"], ["export", "export", "Export & share"],
];

export const store = {
  user: null,
  config: {},
  incidents: [],
  incident: null,
  data: { runs: [], images: [], observations: [], tasks: [], crews: [], summary: null },
  can(role) {
    const order = ["field", "reviewer", "coordinator", "admin"];
    return this.user && order.indexOf(this.user.role) >= order.indexOf(role);
  },
};

const loaders = {
  summary: (id) => api.get(`/api/incidents/${id}`).then((inc) => { store.incident = inc; return inc.summary; }),
  runs: (id) => api.get(`/api/incidents/${id}/runs`),
  images: (id) => api.get(`/api/incidents/${id}/images`),
  observations: (id) => api.get(`/api/incidents/${id}/observations`),
  tasks: (id) => api.get(`/api/incidents/${id}/tasks`),
  crews: (id) => api.get(`/api/incidents/${id}/crews`),
};

export async function reload(keys = Object.keys(loaders)) {
  const id = store.incident && store.incident.id;
  if (!id) return;
  const results = await Promise.allSettled(keys.map((k) => loaders[k](id)));
  results.forEach((r, i) => {
    if (r.status === "fulfilled") store.data[keys[i]] = r.value;
    else if (r.reason && r.reason.status !== 401) toast(r.reason.message, "error");
  });
  updateStatusChip();
}

let current = { name: null, module: null, incidentId: null };
let closeEvents = null;
const pendingReload = new Set();
const flushReload = debounce(async () => {
  const keys = [...pendingReload];
  pendingReload.clear();
  await reload(keys);
  current.module && current.module.refresh ? current.module.refresh(keys) : rerender();
}, 400);

function queueReload(...keys) { keys.forEach((k) => pendingReload.add(k)); flushReload(); }

function onEvent(evt) {
  if (current.module && current.module.onEvent && current.module.onEvent(evt) === false) return;
  switch (evt.type) {
    case "run.progress": case "run.started": case "run.queued": queueReload("runs"); break;
    case "image.added": queueReload("images", "observations", "summary"); break;
    case "run.finished":
      queueReload("runs", "images", "observations", "summary");
      if (evt.status === "done") toast(`Assessment #${evt.run_id} finished: ${evt.observations} observations.`, "success");
      else if (evt.status === "failed") toast(`Assessment #${evt.run_id} failed: ${evt.message}`, "error");
      (evt.errors || []).slice(0, 3).forEach((e) => toast(e, "warn"));
      break;
    case "observation.updated": case "observations.bulk": case "buildings.updated":
      queueReload("observations", "summary"); break;
    case "task.updated": case "tasks.updated":
      queueReload("tasks", "summary");
      if (evt.field_report) toast(`Field report from ${evt.by}: ${evt.task.title} → ${evt.task.status.replace(/_/g, " ")}`, "info");
      break;
    case "crews.updated": queueReload("crews"); break;
    case "incident.updated": queueReload("summary"); break;
    case "overlays.updated": if (current.module && current.module.refresh) current.module.refresh(["overlays"]); break;
    default: break;
  }
}

function setLive(on) {
  $("#live").classList.toggle("on", on);
  $("#live-text").textContent = on ? "Live" : "Reconnecting…";
}

function updateStatusChip() {
  const chip = $("#status-chip"), s = store.incident && store.incident.summary;
  if (!s) { chip.hidden = true; return; }
  chip.hidden = false;
  const demo = s.demo > 0, unreviewed = s.by_status ? s.by_status.not_reviewed : 0;
  chip.className = `status ${demo ? "" : unreviewed ? "warn" : "ok"}`;
  $("#status-text").textContent = demo ? "Contains synthetic demo data"
    : unreviewed ? `${unreviewed} awaiting review` : s.observations ? "All observations reviewed" : "No observations yet";
}

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = hash.split("?");
  const parts = path.split("/").filter(Boolean);
  const params = Object.fromEntries(new URLSearchParams(query));
  if (parts[0] === "i" && parts[1]) return { name: parts[2] || "console", incidentId: Number(parts[1]), params };
  if (parts[0] === "admin") return { name: "admin", params };
  return { name: "incidents", params };
}

export function go(path) { location.hash = path; }
export const incidentPath = (view, extra = "") => `#/i/${store.incident.id}/${view}${extra}`;

async function route() {
  const r = parseRoute();
  if (r.name === "admin" && !store.can("admin")) return go("#/incidents");
  if (r.incidentId && (!store.incident || store.incident.id !== r.incidentId)) {
    try {
      store.incident = await api.get(`/api/incidents/${r.incidentId}`);
    } catch (err) {
      toast(err.message, "error");
      return go("#/incidents");
    }
    store.data = { runs: [], images: [], observations: [], tasks: [], crews: [], summary: store.incident.summary };
    await reload();
    if (closeEvents) closeEvents();
    closeEvents = liveEvents(store.incident.id, onEvent, setLive);
  }
  if (!r.incidentId && r.name === "incidents") {
    store.incident = null;
    if (closeEvents) closeEvents();
    closeEvents = liveEvents(null, (e) => { if (e.type.startsWith("run.") || e.type === "incident.updated") current.module && current.module.refresh && current.module.refresh(); }, setLive);
  }
  renderNav(r);
  if (current.module && current.module.destroy) current.module.destroy();
  const module = await VIEWS[r.name]();
  current = { name: r.name, module, incidentId: r.incidentId, params: r.params };
  const host = $("#view");
  host.className = `view-host view-${r.name}`;
  host.innerHTML = "";
  await module.render(host, { store, params: r.params, reload, go, incidentPath });
  updateStatusChip();
}

function rerender() {
  if (!current.module) return;
  const host = $("#view");
  if (current.module.destroy) current.module.destroy();
  host.innerHTML = "";
  current.module.render(host, { store, params: current.params || {}, reload, go, incidentPath });
}

async function renderNav(r) {
  const inc = store.incident;
  $("#nav-incident").innerHTML = inc ? INCIDENT_NAV.map(([name, ic, label]) =>
    `<a class="rail-btn${r.name === name ? " active" : ""}" href="#/i/${inc.id}/${name}" aria-label="${label}" title="${label}">${icon(ic, 19)}</a>`).join("") : "";
  $("#nav-global").innerHTML = [
    `<a class="rail-btn${r.name === "incidents" ? " active" : ""}" href="#/incidents" aria-label="Incidents" title="Incidents">${icon("incidents", 19)}</a>`,
    store.can("admin") ? `<a class="rail-btn${r.name === "admin" ? " active" : ""}" href="#/admin" aria-label="Administration" title="Administration">${icon("admin", 19)}</a>` : "",
    `<a class="rail-btn" href="/field/" target="_blank" rel="noopener" aria-label="Field app" title="Field app (phones)">${icon("field", 19)}</a>`,
  ].join("");
  const sw = $("#incident-switch"), sel = $("#incident-select");
  sw.hidden = !inc;
  $("#crumb-sep").hidden = !inc;
  if (inc) {
    try { store.incidents = await api.get("/api/incidents"); } catch { /* keep previous */ }
    sel.innerHTML = store.incidents.map((i) => `<option value="${i.id}"${i.id === inc.id ? " selected" : ""}>${esc(i.name)}${i.status === "closed" ? " (closed)" : ""}</option>`).join("");
  }
}

// ---------- auth ----------

function showAuth(setup) {
  $("#boot").hidden = true;
  $("#app").hidden = true;
  $("#auth").hidden = false;
  $("#auth-title").textContent = setup ? "Create the administrator" : "Sign in";
  $("#auth-sub").textContent = setup
    ? "First run: this account manages users, incidents and settings. Use a password of 10+ characters."
    : "Use the account your coordinator created for you.";
  $("#auth-name-row").hidden = !setup;
  $("#auth-submit").textContent = setup ? "Create account" : "Sign in";
  const pw = $("#auth-form input[name=password]");
  pw.autocomplete = setup ? "new-password" : "current-password";
  pw.minLength = setup ? 10 : 1;
  const form = $("#auth-form");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    $("#auth-error").textContent = "";
    $("#auth-submit").disabled = true;
    try {
      const body = { username: fd.get("username"), password: fd.get("password") };
      if (setup) body.name = fd.get("name");
      await api.post(setup ? "/api/auth/setup" : "/api/auth/login", body);
      form.reset();
      boot();
    } catch (err) {
      $("#auth-error").textContent = err.message;
    } finally {
      $("#auth-submit").disabled = false;
    }
  };
}

async function boot() {
  let status;
  try { status = await api.get("/api/auth/status"); }
  catch (err) { $("#boot p").textContent = err.message; return; }
  store.config = status.config;
  if (!status.user) return showAuth(status.setup_required);
  store.user = status.user;
  $("#boot").hidden = true;
  $("#auth").hidden = true;
  $("#app").hidden = false;
  $("#user-btn").textContent = (store.user.name || store.user.username).slice(0, 1).toUpperCase();
  $("#user-btn").title = `${store.user.name} · ${store.user.role}`;
  $("#logout").innerHTML = icon("logout", 19);
  route();
}

setUnauthorizedHandler(debounce(() => { toast("Your session ended. Sign in again.", "warn"); store.user = null; showAuth(false); }, 100));

$("#logout").addEventListener("click", async () => {
  try { await api.post("/api/auth/logout"); } catch { /* ignore */ }
  if (closeEvents) closeEvents();
  store.user = null;
  store.incident = null;
  showAuth(false);
});
$("#user-btn").addEventListener("click", async () => {
  const { modal } = await import("./ui.js");
  const fd = await modal({
    title: `${store.user.name} · ${store.user.role}`, submit: "Change password",
    body: `<p class="muted">Signed in as <b>${esc(store.user.username)}</b>.</p>
      <label class="field">Current password<input type="password" name="current" required autocomplete="current-password"></label>
      <label class="field">New password<input type="password" name="new" required minlength="10" autocomplete="new-password"></label>`,
  });
  if (!fd) return;
  try {
    await api.post("/api/auth/password", { current: fd.get("current"), new: fd.get("new") });
    toast("Password changed. Sign in again.", "success");
    showAuth(false);
  } catch (err) { toast(err.message, "error"); }
});
$("#incident-select").addEventListener("change", (e) => {
  const view = parseRoute().name;
  go(`#/i/${e.target.value}/${view === "incidents" || view === "admin" ? "console" : view}`);
});
window.addEventListener("hashchange", () => { if (store.user) route(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $$("dialog[open]").forEach((d) => d.close());
});

boot();

// Dropping a file outside a drop zone must not navigate away from the console.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());
