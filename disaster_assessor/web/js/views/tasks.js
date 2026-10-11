// Tasks & crews: status board, crew roster, assignment and field report history.
import { api } from "../api.js";
import {
  $, CLASSES, COLORS, HAZARDS, SHORT, TASK_LABEL, ago, classPill, emptyState, esc, fmtTime, hazardChips, icon, modal,
  plural, toast,
} from "../ui.js";

let host, ctx, users = [];
const COLUMNS = [["open", "Open"], ["assigned", "Assigned"], ["in_progress", "In progress"], ["needs_usar", "Needs USAR"], ["searched_clear", "Searched – clear"], ["done", "Done"]];
const st = { crew: "", q: "" };
const coordinator = () => ctx.store.can("coordinator");

export async function render(el, context) {
  host = el;
  ctx = context;
  if (coordinator()) { try { users = await api.get("/api/users"); } catch { users = []; } }
  host.innerHTML = `<section class="tasks-grid">
    <div class="page-head tight">
      <div><h1>Tasks & crews</h1><p class="muted" id="task-sub"></p></div>
      <div class="head-actions">
        <label class="field compact"><span class="sr-only">Filter by crew</span><select id="crew-filter"></select></label>
        <label class="field compact"><span class="sr-only">Search tasks</span><input id="task-q" placeholder="Search tasks" type="search"></label>
        ${coordinator() ? `<button class="btn ghost" id="new-task">${icon("plus", 15)} Task</button>
        <button class="btn primary" id="from-high">${icon("tasks", 15)} Create tasks for high-priority</button>` : ""}
      </div>
    </div>
    <div class="board" id="board"></div>
    <aside class="card crews">
      <div class="side-head row"><h3>Crews</h3>${coordinator() ? `<button class="btn ghost sm" id="new-crew">${icon("plus", 13)} Crew</button>` : ""}</div>
      <ul class="crew-list" id="crew-list"></ul>
      <p class="fine">Crew members see their tasks in the <a href="/field/" target="_blank" rel="noopener">field app</a>, which works offline and syncs reports when back in coverage.</p>
    </aside>
  </section>`;
  bind();
  refresh();
  if (ctx.params.task) openTask(Number(ctx.params.task));
}

export function refresh() {
  if (!host || !host.isConnected) return;
  const tasks = ctx.store.data.tasks, crews = ctx.store.data.crews;
  $("#crew-filter", host).innerHTML = `<option value="">All crews</option><option value="none" ${st.crew === "none" ? "selected" : ""}>Unassigned</option>` +
    crews.map((c) => `<option value="${c.id}" ${String(c.id) === st.crew ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  const q = st.q.toLowerCase();
  const shown = tasks.filter((t) => t.status !== "cancelled"
    && (!st.crew || (st.crew === "none" ? !t.crew_id : String(t.crew_id) === st.crew))
    && (!q || `${t.title} ${t.notes} ${t.crew || ""}`.toLowerCase().includes(q)));
  const open = tasks.filter((t) => !["done", "cancelled", "searched_clear"].includes(t.status)).length;
  $("#task-sub", host).textContent = `${plural(open, "open task")} · ${plural(tasks.filter((t) => t.status === "needs_usar").length, "building")} flagged for heavy rescue`;
  $("#board", host).innerHTML = COLUMNS.map(([key, label]) => {
    const col = shown.filter((t) => t.status === key);
    return `<div class="column" data-status="${key}"><div class="col-head"><span>${label}</span><b>${col.length}</b></div>
      <div class="col-body" data-status="${key}">${col.map(card).join("") || `<p class="fine pad">—</p>`}</div></div>`;
  }).join("");
  $("#crew-list", host).innerHTML = crews.length ? crews.map((c) => `<li class="crew">
      <div class="crew-head"><b>${esc(c.name)}</b><span class="pill">${plural(c.open_tasks, "open task")}</span></div>
      <p class="sub">${c.members.map((m) => esc(m.name)).join(", ") || "No members"}${c.contact ? ` · ${esc(c.contact)}` : ""}</p>
      ${coordinator() ? `<div class="crew-actions"><button class="link-btn" data-edit-crew="${c.id}">Edit</button><button class="link-btn" data-route-crew="${c.id}">Route on map</button></div>` : ""}
    </li>`).join("") : `<li class="fine pad">${coordinator() ? "Create crews, then assign tasks to them." : "No crews yet."}</li>`;
  if (!tasks.length) {
    $("#board", host).insertAdjacentHTML("afterbegin", `<div class="board-empty">${emptyState("No tasks yet",
      coordinator() ? "Turn reviewed Major and Destroyed observations into tasks, then assign crews." : "A coordinator will create tasks from reviewed observations.")}</div>`);
  }
}

function card(t) {
  const crewOptions = ctx.store.data.crews.map((c) => `<option value="${c.id}" ${c.id === t.crew_id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  return `<article class="task-card" draggable="${coordinator()}" data-id="${t.id}" style="--c:${COLORS[t.damage_class] || "#5aa9e6"}">
    <button class="task-open" data-open="${t.id}">
      <span class="task-title">${esc(t.title)}</span>
      <span class="task-meta">${t.damage_class ? `${esc(SHORT[t.damage_class] || t.damage_class)} · ` : ""}#${t.id}${t.last_report ? ` · report ${ago(t.last_report)}` : ""}</span>
      ${t.hazards.length ? `<span class="task-hz">${hazardChips(t.hazards)}</span>` : ""}
    </button>
    ${coordinator() ? `<label class="task-crew"><span class="sr-only">Crew</span><select data-assign="${t.id}"><option value="">Unassigned</option>${crewOptions}</select></label>`
      : `<span class="task-crew-ro">${esc(t.crew || "Unassigned")}</span>`}
  </article>`;
}

async function update(id, body) {
  try {
    const t = await api.patch(`/api/tasks/${id}`, body);
    const i = ctx.store.data.tasks.findIndex((x) => x.id === id);
    if (i >= 0) ctx.store.data.tasks[i] = t;
    refresh();
    ctx.reload(["crews", "summary"]).then(refresh);
    return t;
  } catch (err) { toast(err.message, "error"); return null; }
}

async function openTask(id) {
  const t = ctx.store.data.tasks.find((x) => x.id === id);
  if (!t) return;
  let reports = { reports: [], photos: [] };
  try { reports = await api.get(`/api/tasks/${id}/reports`); } catch { /* shown as empty */ }
  const statuses = Object.entries(TASK_LABEL).filter(([k]) => coordinator() || ["in_progress", "searched_clear", "needs_usar", "done"].includes(k));
  const fd = await modal({
    title: `Task #${t.id}`, submit: "Save task", wide: true,
    body: `<div class="task-detail">
      <div>
        ${t.image_id ? `<img class="task-img" src="/api/images/${t.image_id}/file" alt="Assessment image for this task">` : ""}
        <p>${t.damage_class ? classPill(t.damage_class) : ""} ${hazardChips(t.hazards)}</p>
        <p class="sub">${t.latitude != null ? `${t.latitude.toFixed(5)}, ${t.longitude.toFixed(5)} · <a href="https://www.google.com/maps/dir/?api=1&destination=${t.latitude},${t.longitude}" target="_blank" rel="noopener">Directions</a>` : "No coordinates"}
          ${t.observation_id ? ` · <a href="${ctx.incidentPath("review", `?image=${t.image_id}&obs=${t.observation_id}`)}">Open in review</a>` : ""}</p>
        <h4 class="group-label">FIELD REPORTS</h4>
        <ol class="reports">${reports.reports.map((r) => `<li><b>${esc(TASK_LABEL[r.status] || r.status)}</b> · ${esc(r.user || "")} · ${fmtTime(r.reported_at)}
          ${r.notes ? `<br>${esc(r.notes)}` : ""}${r.hazards.length ? `<br>${hazardChips(r.hazards)}` : ""}</li>`).join("") || `<li class="fine">No reports yet.</li>`}</ol>
        ${reports.photos.length ? `<div class="photo-row">${reports.photos.map((p) => `<a href="/api/images/${p.id}/file" target="_blank" rel="noopener"><img src="/api/images/${p.id}/file?variant=preview" alt="Field photo"></a>`).join("")}</div>` : ""}
      </div>
      <div class="stack">
        <label class="field">Status<select name="status">${statuses.map(([k, v]) => `<option value="${k}" ${k === t.status ? "selected" : ""}>${v}</option>`).join("")}</select></label>
        ${coordinator() ? `<label class="field">Crew<select name="crew_id"><option value="">Unassigned</option>${ctx.store.data.crews.map((c) => `<option value="${c.id}" ${c.id === t.crew_id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label>
        <label class="field">Priority<input type="number" name="priority" value="${t.priority}" min="0" max="1000"></label>` : ""}
        <label class="field">Notes<textarea name="notes" rows="8">${esc(t.notes)}</textarea></label>
      </div></div>`,
  });
  if (!fd) return;
  const body = { status: fd.get("status"), notes: fd.get("notes") };
  if (coordinator()) {
    body.crew_id = fd.get("crew_id") ? Number(fd.get("crew_id")) : null;
    body.priority = Number(fd.get("priority"));
  }
  if (await update(id, body)) toast("Task updated.", "success");
}

async function crewDialog(crew) {
  const members = new Set((crew ? crew.members : []).map((m) => m.id));
  const fd = await modal({
    title: crew ? `Edit ${crew.name}` : "New crew", submit: crew ? "Save crew" : "Create crew",
    body: `<label class="field">Name<input name="name" required maxlength="80" value="${esc(crew ? crew.name : "")}" placeholder="e.g. Engine 7 / USAR Team B"></label>
      <label class="field">Contact (radio channel, phone)<input name="contact" maxlength="200" value="${esc(crew ? crew.contact : "")}"></label>
      <fieldset class="member-pick"><legend>Members</legend>${users.filter((u) => u.active).map((u) =>
        `<label><input type="checkbox" name="m" value="${u.id}" ${members.has(u.id) ? "checked" : ""}> ${esc(u.name)} <span class="muted">· ${esc(u.role)}</span></label>`).join("") || `<p class="fine">No users yet. An admin can add field accounts.</p>`}</fieldset>
      ${crew ? `<button type="button" class="btn danger sm" id="delete-crew">Delete crew</button>` : ""}`,
    onOpen: (dlg) => {
      const del = dlg.querySelector("#delete-crew");
      if (del) del.addEventListener("click", async () => {
        try { await api.del(`/api/crews/${crew.id}`); toast("Crew deleted."); dlg.close(); await ctx.reload(["crews", "tasks"]); refresh(); }
        catch (err) { toast(err.message, "error"); }
      });
    },
  });
  if (!fd) return;
  const body = { name: fd.get("name"), contact: fd.get("contact"), member_ids: fd.getAll("m").map(Number) };
  try {
    if (crew) await api.patch(`/api/crews/${crew.id}`, body);
    else await api.post(`/api/incidents/${ctx.store.incident.id}/crews`, body);
    await ctx.reload(["crews"]);
    refresh();
  } catch (err) { toast(err.message, "error"); }
}

async function newTask() {
  const fd = await modal({
    title: "New task", submit: "Create task",
    body: `<label class="field">Title<input name="title" required maxlength="200" placeholder="e.g. Check 412 Elm St – reported trapped resident"></label>
      <div class="two"><label class="field">Latitude<input name="lat" type="number" step="any"></label><label class="field">Longitude<input name="lon" type="number" step="any"></label></div>
      <label class="field">Damage class<select name="cls"><option value="">—</option>${CLASSES.map((c) => `<option>${c}</option>`).join("")}</select></label>
      <label class="field">Crew<select name="crew"><option value="">Unassigned</option>${ctx.store.data.crews.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select></label>`,
  });
  if (!fd) return;
  try {
    await api.post(`/api/incidents/${ctx.store.incident.id}/tasks`, {
      title: fd.get("title"), latitude: fd.get("lat") ? Number(fd.get("lat")) : null, longitude: fd.get("lon") ? Number(fd.get("lon")) : null,
      damage_class: fd.get("cls") || null, crew_id: fd.get("crew") ? Number(fd.get("crew")) : null,
    });
    await ctx.reload(["tasks"]);
    refresh();
  } catch (err) { toast(err.message, "error"); }
}

function bind() {
  $("#crew-filter", host).addEventListener("change", (e) => { st.crew = e.target.value; refresh(); });
  $("#task-q", host).addEventListener("input", (e) => { st.q = e.target.value; refresh(); });
  const on = (id, fn) => { const el = $(id, host); if (el) el.addEventListener("click", fn); };
  on("#new-crew", () => crewDialog(null));
  on("#new-task", newTask);
  on("#from-high", async () => {
    const s = ctx.store.incident.summary;
    if (s && s.by_status && s.by_status.not_reviewed) toast(`${s.by_status.not_reviewed} observations are still unreviewed; tasks include unreviewed model output.`, "warn");
    try {
      const created = await api.post(`/api/incidents/${ctx.store.incident.id}/tasks`, { high_priority: true });
      toast(created.length ? `Created ${plural(created.length, "task")}.` : "Every high-priority observation already has a task.", created.length ? "success" : "info");
      await ctx.reload(["tasks", "observations"]);
      refresh();
    } catch (err) { toast(err.message, "error"); }
  });
  const board = $("#board", host);
  board.addEventListener("click", (e) => {
    const open = e.target.closest("[data-open]");
    if (open) openTask(Number(open.dataset.open));
  });
  board.addEventListener("change", (e) => {
    const sel = e.target.closest("[data-assign]");
    if (sel) update(Number(sel.dataset.assign), { crew_id: sel.value ? Number(sel.value) : null, ...(sel.value ? {} : { status: "open" }) });
  });
  board.addEventListener("dragstart", (e) => {
    const card = e.target.closest(".task-card");
    if (card) { e.dataTransfer.setData("text/plain", card.dataset.id); card.classList.add("dragging"); }
  });
  board.addEventListener("dragend", (e) => { const c = e.target.closest(".task-card"); if (c) c.classList.remove("dragging"); });
  board.addEventListener("dragover", (e) => { if (e.target.closest(".col-body")) e.preventDefault(); });
  board.addEventListener("drop", (e) => {
    const col = e.target.closest(".col-body");
    if (!col) return;
    e.preventDefault();
    update(Number(e.dataTransfer.getData("text/plain")), { status: col.dataset.status });
  });
  $("#crew-list", host).addEventListener("click", (e) => {
    const edit = e.target.closest("[data-edit-crew]"), route = e.target.closest("[data-route-crew]");
    if (edit) crewDialog(ctx.store.data.crews.find((c) => c.id === Number(edit.dataset.editCrew)));
    if (route) ctx.go(ctx.incidentPath("map"));
  });
}
