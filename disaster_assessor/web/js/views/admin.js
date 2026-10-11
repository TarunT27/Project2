// Administration: users and roles, integrations, models, backups, retention, audit log.
import { api } from "../api.js";
import { $, confirmDialog, esc, fmtBytes, fmtTime, icon, modal, toast } from "../ui.js";

let host, ctx;
const ROLE_HELP = {
  admin: "Everything, including users, backups and retention.",
  coordinator: "Incidents, crews, tasks, footprints, imagery and share links.",
  reviewer: "Run assessments and review observations.",
  field: "Field app: see assigned tasks and send reports.",
};

export async function render(el, context) {
  host = el;
  ctx = context;
  host.innerHTML = `<div class="page">
    <div class="page-head"><div><h1>Administration</h1><p class="muted">Users, integrations, backups and data retention.</p></div></div>
    <div class="admin-grid">
      <section class="card pad-card wide-card">
        <div class="side-head row"><h3>Users</h3><button class="btn primary sm" id="new-user">${icon("plus", 13)} User</button></div>
        <div class="table-wrap"><table><thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Status</th><th></th></tr></thead><tbody id="users"></tbody></table></div>
      </section>
      <section class="card pad-card"><div class="side-head"><h3>System</h3></div><div id="system"></div></section>
      <section class="card pad-card">
        <div class="side-head row"><h3>Backups</h3><button class="btn ghost sm" id="backup">Back up now</button></div>
        <ul class="share-list" id="backups"></ul>
        <h4 class="group-label">RETENTION</h4>
        <div class="row-form"><label class="field compact">Delete closed incidents older than<input type="number" id="ret-days" min="1" value="90"> days</label>
        <button class="btn ghost sm" id="ret-preview">Preview</button></div>
        <div id="ret-result"></div>
      </section>
      <section class="card pad-card wide-card"><div class="side-head"><h3>Audit log</h3></div>
        <div class="table-wrap tall"><table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead><tbody id="audit"></tbody></table></div></section>
    </div></div>`;
  bind();
  await refresh();
}

export async function refresh() {
  try {
    const [users, system, audit] = await Promise.all([api.get("/api/users"), api.get("/api/admin/system"), api.get("/api/admin/audit?limit=300")]);
    $("#users", host).innerHTML = users.map((u) => `<tr class="${u.active ? "" : "inactive"}"><td>${esc(u.name)}</td><td class="mono">${esc(u.username)}</td>
      <td><span class="pill">${esc(u.role)}</span></td><td>${u.active ? "Active" : "Disabled"}</td>
      <td><button class="link-btn" data-edit="${u.id}">Edit</button></td></tr>`).join("");
    host._users = users;
    const i = system.integrations, yes = (b) => (b ? `<span class="ok-dot">Configured</span>` : `<span class="muted">Not configured</span>`);
    $("#system", host).innerHTML = `<dl class="meta">
      <dt>Data folder</dt><dd class="mono small">${esc(system.data_dir)}</dd>
      <dt>Disk used</dt><dd>${fmtBytes(system.disk_bytes)}</dd>
      <dt>Records</dt><dd>${system.counts.incidents} incidents · ${system.counts.images} images · ${system.counts.observations} observations</dd>
      <dt>Models</dt><dd>${system.models.length ? system.models.map((m) => `${esc(m.name)} <span class="muted">(${m.task || "?"}, ${m.size_mb} MB)</span>`).join("<br>") : `<span class="muted">None in ${esc(system.models_dir)}</span>`}</dd>
      <dt>Claude second opinion</dt><dd>${yes(i.vision_review)} <span class="muted">ANTHROPIC_API_KEY</span></dd>
      <dt>Road routing</dt><dd>${yes(i.osrm)} <span class="muted">DRA_OSRM_URL</span></dd>
      <dt>Slack alerts</dt><dd>${yes(i.slack)} <span class="muted">DRA_SLACK_WEBHOOK</span></dd>
      <dt>Webhook</dt><dd>${yes(i.webhook)} <span class="muted">DRA_WEBHOOK_URL</span></dd>
      <dt>Public URL</dt><dd>${i.public_url ? esc(i.public_url) : `<span class="muted">DRA_PUBLIC_URL not set</span>`}</dd>
      <dt>Retention</dt><dd>${system.retention_days ? `${system.retention_days} days (automatic)` : `<span class="muted">Off (DRA_RETENTION_DAYS)</span>`}</dd></dl>`;
    $("#backups", host).innerHTML = system.backups.length ? system.backups.slice(0, 8).map((b) => `<li><div><b class="mono small">${esc(b.name)}</b><span class="sub">${fmtBytes(b.size)} · ${fmtTime(b.created_at)}</span></div>
      <a class="btn ghost sm" href="/api/admin/backups/${encodeURIComponent(b.name)}" download>Download</a></li>`).join("") : `<li class="fine">No backups yet.</li>`;
    $("#audit", host).innerHTML = audit.map((a) => `<tr><td class="dim">${fmtTime(a.created_at)}</td><td>${esc(a.user || "system")}</td><td class="mono small">${esc(a.action)}</td>
      <td class="dim">${a.target_type ? `${esc(a.target_type)} #${a.target_id}` : ""}</td><td class="dim small">${esc(JSON.stringify(a.detail)).slice(0, 140)}</td></tr>`).join("");
  } catch (err) { toast(err.message, "error"); }
}

async function userDialog(user) {
  const fd = await modal({
    title: user ? `Edit ${user.name}` : "New user", submit: user ? "Save" : "Create user",
    body: `${user ? "" : `<label class="field">Username<input name="username" required minlength="2" maxlength="60" pattern="[A-Za-z0-9._@-]+" autocomplete="off"></label>`}
      <label class="field">Name<input name="name" maxlength="100" value="${esc(user ? user.name : "")}"></label>
      <label class="field">Role<select name="role">${Object.keys(ROLE_HELP).map((r) => `<option value="${r}" ${user && user.role === r ? "selected" : ""}>${r}</option>`).join("")}</select></label>
      <p class="note">${Object.entries(ROLE_HELP).map(([r, h]) => `<b>${r}</b>: ${esc(h)}`).join("<br>")}</p>
      <label class="field">${user ? "New password (leave blank to keep)" : "Temporary password"}<input type="password" name="password" ${user ? "" : "required"} minlength="10" autocomplete="new-password"></label>
      ${user ? `<label class="switch"><input type="checkbox" name="active" ${user.active ? "checked" : ""}><span></span>Account active</label>` : ""}`,
  });
  if (!fd) return;
  try {
    if (user) {
      const body = { name: fd.get("name"), role: fd.get("role"), active: fd.get("active") === "on" };
      if (fd.get("password")) body.password = fd.get("password");
      await api.patch(`/api/admin/users/${user.id}`, body);
    } else {
      await api.post("/api/admin/users", { username: fd.get("username"), name: fd.get("name"), role: fd.get("role"), password: fd.get("password") });
    }
    toast("Saved.", "success");
    refresh();
  } catch (err) { toast(err.message, "error"); }
}

function bind() {
  $("#new-user", host).addEventListener("click", () => userDialog(null));
  $("#users", host).addEventListener("click", (e) => {
    const b = e.target.closest("[data-edit]");
    if (b) userDialog(host._users.find((u) => u.id === Number(b.dataset.edit)));
  });
  $("#backup", host).addEventListener("click", async () => {
    try { const b = await api.post("/api/admin/backups"); toast(`Backup written (${fmtBytes(b.size)}).`, "success"); refresh(); }
    catch (err) { toast(err.message, "error"); }
  });
  $("#ret-preview", host).addEventListener("click", async () => {
    const days = Number($("#ret-days", host).value);
    try {
      const r = await api.post("/api/admin/retention", { days, dry_run: true });
      const out = $("#ret-result", host);
      if (!r.incidents.length) { out.innerHTML = `<p class="fine">Nothing to delete.</p>`; return; }
      out.innerHTML = `<p class="sub">Would delete: ${r.incidents.map((i) => esc(i.name)).join(", ")}</p><button class="btn danger sm" id="ret-apply">Delete ${r.incidents.length} incident(s)</button>`;
      $("#ret-apply", host).addEventListener("click", async () => {
        if (!(await confirmDialog("Delete permanently?", "Imagery, observations, reviews and tasks of these closed incidents will be erased. Make a backup first.", "Delete"))) return;
        await api.post("/api/admin/retention", { days, dry_run: false });
        toast("Deleted.", "success");
        out.innerHTML = "";
        refresh();
      });
    } catch (err) { toast(err.message, "error"); }
  });
}
