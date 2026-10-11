/* DRA Field — offline-first task list and field reports for rescue crews.
 *
 * Every report is written to an IndexedDB outbox before anything touches the network. The outbox is
 * drained on start, on the 'online' event, every 30 s and from "Sync now". The server de-duplicates by
 * client_id, so a retry after a lost response is harmless.
 */
(() => {
  "use strict";

  // ---------- constants ----------
  const API = {
    status: "/api/auth/status",
    login: "/api/auth/login",
    logout: "/api/auth/logout",
    tasks: "/api/field/tasks",
    report: (id) => `/api/tasks/${encodeURIComponent(id)}/reports`,
    image: (id, variant) => `/api/images/${encodeURIComponent(id)}/file?variant=${variant}`,
  };
  const SEV = {
    "No Damage": "#16a34a", "Minor Damage": "#eab308", "Major Damage": "#f97316", "Destroyed": "#dc2626",
  };
  const STATUS = {
    open: "Open", assigned: "Assigned", in_progress: "Searching", searched_clear: "Searched – clear",
    needs_usar: "Needs USAR", done: "Done", cancelled: "Cancelled",
  };
  const REPORT_LABEL = {
    in_progress: "Start search", searched_clear: "Searched – clear",
    needs_usar: "Needs USAR / heavy rescue", done: "Done",
  };
  const FINISHED = new Set(["done", "searched_clear", "cancelled"]);
  const HAZARDS = [
    ["fire", "Fire"], ["flooding", "Flooding"], ["debris_blocking_access", "Debris blocking access"],
    ["gas_or_utility_damage", "Gas / utility damage"], ["possible_entrapment", "Possible entrapment"],
    ["unstable_structure", "Unstable structure"], ["hazmat", "Hazmat"], ["downed_power_lines", "Downed power lines"],
  ];
  const HAZARD_LABEL = Object.fromEntries(HAZARDS);
  const MAX_PHOTOS = 6;
  const MAX_SIDE = 1600;
  const SYNC_EVERY = 30000;
  const REFRESH_EVERY = 60000;

  // ---------- tiny DOM helpers ----------
  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value == null || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "style") node.style.cssText = value;
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? "" : value);
      }
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }
  function svg(paths, cls) {
    const ns = "http://www.w3.org/2000/svg";
    const s = document.createElementNS(ns, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    if (cls) s.setAttribute("class", cls);
    for (const d of paths) {
      const p = document.createElementNS(ns, "path");
      p.setAttribute("d", d);
      s.append(p);
    }
    return s;
  }
  const ICON = {
    warn: ["M12 3 2 20h20z", "M12 10v4", "M12 17v.5"],
    check: ["M5 12l5 5L20 7"],
    x: ["M6 6l12 12", "M18 6 6 18"],
    clock: ["M12 7v5l3 2", "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z"],
  };

  // ---------- formatting ----------
  function parseTime(value) {
    if (!value) return null;
    if (typeof value === "number") return new Date(value);
    let s = String(value).trim();
    if (/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(s)) s += ":00Z"; // note-log stamps are UTC minutes
    else if (/^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?$/.test(s)) s += "Z";
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  function clock(value) {
    const d = parseTime(value);
    if (!d) return "";
    const today = new Date().toDateString() === d.toDateString();
    const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return today ? t : `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${t}`;
  }
  function ago(value) {
    const d = parseTime(value);
    if (!d) return "never";
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }
  function haversine(lat1, lon1, lat2, lon2) {
    const R = 6371008.8;
    const rad = Math.PI / 180;
    const dLat = (lat2 - lat1) * rad;
    const dLon = (lon2 - lon1) * rad;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  }
  function bearing(lat1, lon1, lat2, lon2) {
    const rad = Math.PI / 180;
    const y = Math.sin((lon2 - lon1) * rad) * Math.cos(lat2 * rad);
    const x = Math.cos(lat1 * rad) * Math.sin(lat2 * rad) - Math.sin(lat1 * rad) * Math.cos(lat2 * rad) * Math.cos((lon2 - lon1) * rad);
    const deg = (Math.atan2(y, x) / rad + 360) % 360;
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
  }
  function fmtDistance(m) {
    if (m == null) return "";
    if (m < 1000) return `${Math.max(10, Math.round(m / 10) * 10)} m`;
    return `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`;
  }
  function hasCoords(t) {
    return typeof t.latitude === "number" && typeof t.longitude === "number" && Number.isFinite(t.latitude) && Number.isFinite(t.longitude);
  }
  function distanceTo(t) {
    if (!S.pos || !hasCoords(t)) return null;
    return haversine(S.pos.lat, S.pos.lon, t.latitude, t.longitude);
  }
  function uuid() {
    if (window.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }
  async function errorText(res) {
    try {
      const data = await res.clone().json();
      if (data && (data.error || data.detail)) return String(data.error || data.detail);
    } catch (_) { /* not JSON */ }
    return `Server answered ${res.status}${res.statusText ? " " + res.statusText : ""}`;
  }

  // ---------- storage (IndexedDB, with an in-memory fallback) ----------
  const store = (() => {
    let dbPromise = null;
    const memory = { kv: new Map(), outbox: new Map() };
    let persistent = true;

    function open() {
      if (dbPromise) return dbPromise;
      dbPromise = new Promise((resolve, reject) => {
        if (!("indexedDB" in window)) { reject(new Error("IndexedDB unavailable")); return; }
        const req = indexedDB.open("dra-field", 1);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv");
          if (!db.objectStoreNames.contains("outbox")) db.createObjectStore("outbox", { keyPath: "client_id" });
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
        req.onblocked = () => reject(new Error("Storage is blocked by another tab"));
      }).catch((err) => {
        persistent = false;
        console.warn("DRA Field: offline storage unavailable", err);
        return null;
      });
      return dbPromise;
    }

    async function run(name, mode, fn) {
      const db = await open();
      if (!db) return fn(null, memory[name]);
      return new Promise((resolve, reject) => {
        const tx = db.transaction(name, mode);
        let result;
        const req = fn(tx.objectStore(name));
        if (req) req.onsuccess = () => { result = req.result; };
        tx.oncomplete = () => resolve(result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error || new Error("Storage write aborted"));
      });
    }

    return {
      get isPersistent() { return persistent; },
      ready: open,
      get: (key) => run("kv", "readonly", (s, m) => (s ? s.get(key) : m.get(key))),
      set: (key, value) => run("kv", "readwrite", (s, m) => (s ? s.put(value, key) : void m.set(key, value))),
      del: (key) => run("kv", "readwrite", (s, m) => (s ? s.delete(key) : void m.delete(key))),
      outbox: () => run("outbox", "readonly", (s, m) => (s ? s.getAll() : [...m.values()])),
      put: (item) => run("outbox", "readwrite", (s, m) => (s ? s.put(item) : void m.set(item.client_id, item))),
      remove: (id) => run("outbox", "readwrite", (s, m) => (s ? s.delete(id) : void m.delete(id))),
    };
  })();

  // ---------- state ----------
  const S = {
    user: null,
    userVerified: false,
    tasks: [],
    updatedAt: null,
    outbox: [],
    filter: "open",
    sort: "priority",
    route: { name: "list" },
    pos: null,
    geoDenied: false,
    watchId: null,
    reachable: true,
    syncing: false,
    refreshing: false,
    lastSyncError: null,
    drafts: new Map(), // task id -> { status, notes, hazards:Set, photos:[], gps }
    sessionExpired: false,
    saving: false,
  };

  // ---------- network state ----------
  function isOnline() { return navigator.onLine !== false; }
  function markReachable(ok) {
    if (S.reachable !== ok) { S.reachable = ok; renderChrome(); }
  }
  function renderNet() {
    const net = $("net");
    const label = $("net-label");
    net.classList.remove("online", "offline", "degraded");
    if (!isOnline()) { net.classList.add("offline"); label.textContent = "Offline"; }
    else if (!S.reachable) { net.classList.add("degraded"); label.textContent = "No server"; }
    else { net.classList.add("online"); label.textContent = "Online"; }
  }

  // ---------- outbox views ----------
  function mine(item) { return S.user && item.user_id === S.user.id; }
  function outboxCounts() {
    let waiting = 0, failed = 0, others = 0;
    for (const item of S.outbox) {
      if (S.user && !mine(item)) { others++; continue; }
      if (item.state === "failed") failed++;
      else waiting++;
    }
    return { waiting, failed, others, total: S.outbox.length };
  }
  async function loadOutbox() {
    try {
      const items = await store.outbox();
      S.outbox = (items || []).sort((a, b) => a.created_at - b.created_at);
    } catch (err) {
      console.error(err);
    }
  }

  /** Server task + any reports still queued on this phone (optimistic view). */
  function effective(task) {
    const queued = S.outbox.filter((i) => i.task_id === task.id && (!S.user || mine(i)));
    if (!queued.length) return task;
    const out = { ...task, hazards: [...(task.hazards || [])], _pending: 0, _failed: 0 };
    for (const item of queued) {
      if (item.state === "failed") { out._failed++; continue; }
      out._pending++;
      out.status = item.status;
      out.hazards = [...new Set([...out.hazards, ...item.hazards])].sort();
    }
    return out;
  }
  function allTasks() { return S.tasks.map(effective); }
  function findTask(id) {
    const t = S.tasks.find((x) => String(x.id) === String(id));
    return t ? effective(t) : null;
  }

  // ---------- banner + chrome ----------
  let bannerAction = null;
  function setBanner(kind, text, actionLabel, action) {
    const banner = $("banner");
    banner.className = `banner ${kind}`;
    if ($("banner-text").textContent !== text) $("banner-text").textContent = text;
    const btn = $("banner-action");
    bannerAction = action || null;
    btn.hidden = !actionLabel;
    if (actionLabel) btn.textContent = actionLabel;
  }
  function renderBanner() {
    const c = outboxCounts();
    $("banner").hidden = !S.user && !c.total && isOnline();
    const updated = S.updatedAt ? `tasks from ${clock(S.updatedAt)} (${ago(S.updatedAt)})` : "no tasks downloaded yet";
    if (!S.user) {
      if (c.total) setBanner("queue", `${plural(c.total, "report")} saved on this phone — sign in to send.`);
      else if (!isOnline()) setBanner("offline", "Offline — sign-in needs a connection.");
      else setBanner("", "Sign in to see your crew's tasks.");
      return;
    }
    if (S.syncing) { setBanner("queue", `Sending ${plural(c.waiting, "report")}…`); return; }
    if (S.sessionExpired && c.waiting) {
      setBanner("error", `Session expired — ${plural(c.waiting, "report")} waiting. Sign in again to send.`, "Sign in", () => showLogin(true));
      return;
    }
    if (c.failed) {
      setBanner("error", `${plural(c.failed, "report")} rejected by the server — review needed.`, "Review", () => go("#/outbox"));
      return;
    }
    if (!isOnline() || !S.reachable) {
      const where = !isOnline() ? "Offline" : "Server unreachable";
      const queued = c.waiting ? ` · ${plural(c.waiting, "report")} waiting to sync` : "";
      setBanner("offline", `${where}${queued} · ${updated}`, c.waiting ? "Outbox" : null, c.waiting ? () => go("#/outbox") : null);
      return;
    }
    if (c.waiting) {
      setBanner("queue", `${plural(c.waiting, "report")} waiting to sync${S.lastSyncError ? ` — ${S.lastSyncError}` : ""}`, "Sync now", () => syncOutbox(true));
      return;
    }
    setBanner("ok", `All reports synced · updated ${S.updatedAt ? ago(S.updatedAt) : "—"}`);
  }
  function renderBadge() {
    const c = outboxCounts();
    const badge = $("outbox-badge");
    const n = c.waiting + c.failed + c.others;
    badge.hidden = n === 0;
    badge.textContent = String(n);
    badge.classList.toggle("failed", c.failed > 0);
    $("ab-outbox").setAttribute("aria-label", n ? `Outbox, ${n} waiting` : "Outbox, empty");
    const syncBtn = $("ab-sync");
    syncBtn.classList.toggle("spin", S.syncing);
    syncBtn.disabled = S.syncing;
    $("ab-sync2").disabled = S.syncing;
    $("ab-sync2").textContent = S.syncing ? "Sending…" : "Sync now";
    $("ab-refresh").classList.toggle("spin", S.refreshing);
  }
  function renderChrome() {
    renderNet();
    renderBanner();
    renderBadge();
    if (S.route.name === "detail") updateSubmitButton();
  }

  let toastTimer = null;
  function toast(message, bad) {
    const t = $("toast");
    t.textContent = message;
    t.classList.toggle("bad", !!bad);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3800);
  }

  // ---------- auth ----------
  function setUser(user) {
    S.user = user;
    const btn = $("user-btn");
    btn.hidden = !user;
    if (user) {
      const name = user.name || user.username || "?";
      $("user-initials").textContent = name.split(/\s+/).map((p) => p[0]).join("").slice(0, 2).toUpperCase();
      $("user-name").textContent = name;
      $("user-role").textContent = `${user.username} · ${user.role}`;
    }
  }

  async function checkAuth() {
    const cachedUser = await store.get("user").catch(() => null);
    try {
      const res = await fetch(API.status, { credentials: "same-origin", cache: "no-store" });
      if (!res.ok) throw new Error(await errorText(res));
      const data = await res.json();
      markReachable(true);
      if (data.user) {
        S.userVerified = true;
        S.sessionExpired = false;
        setUser(data.user);
        await store.set("user", data.user);
        return true;
      }
      setUser(null);
      if (cachedUser) $("login-user").value = cachedUser.username || "";
      showLogin(!!cachedUser, data.setup_required);
      return false;
    } catch (err) {
      // Offline (or server down): trust the last signed-in user so the crew keeps working.
      markReachable(false);
      if (cachedUser) { setUser(cachedUser); return true; }
      showLogin(false);
      return false;
    }
  }

  function showLogin(expired, setupRequired) {
    if (expired) S.sessionExpired = true;
    const c = outboxCounts();
    $("login-intro").textContent = setupRequired
      ? "This platform has not been set up yet. Ask your coordinator to finish setup on the main console."
      : expired
        ? `Your session ended. Sign in again${c.total ? ` to send ${plural(c.total, "saved report")}` : ""}.`
        : "Use the account your coordinator gave your crew.";
    $("login-offline").hidden = isOnline();
    render({ name: "login" });
  }

  async function login(event) {
    event.preventDefault();
    const username = $("login-user").value.trim();
    const password = $("login-pass").value;
    const err = $("login-error");
    err.hidden = true;
    if (!username || !password) {
      err.textContent = "Enter your username and password.";
      err.hidden = false;
      return;
    }
    const btn = $("login-submit");
    btn.disabled = true;
    btn.textContent = "Signing in…";
    try {
      const res = await fetch(API.login, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) throw new Error(res.status === 401 ? "Incorrect username or password." : await errorText(res));
      const data = await res.json();
      markReachable(true);
      $("login-pass").value = "";
      const previous = await store.get("tasks").catch(() => null);
      if (!previous || previous.userId !== data.user.id) {
        S.tasks = [];
        S.updatedAt = null;
      }
      S.userVerified = true;
      S.sessionExpired = false;
      setUser(data.user);
      await store.set("user", data.user);
      requestPersistence();
      for (const item of S.outbox) {
        if (item.user_id === data.user.id && item.state === "auth") { item.state = "pending"; await store.put(item); }
      }
      const target = location.hash && location.hash !== "#/login" ? location.hash : "#/";
      go(target, true);
      route();
      await refreshTasks();
      syncOutbox();
    } catch (e) {
      err.textContent = e instanceof TypeError ? "Can't reach the server. Check your connection and try again." : e.message;
      err.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = "Sign in";
    }
  }

  async function logout() {
    closeMenu();
    const c = outboxCounts();
    if (c.waiting + c.failed > 0) {
      const ok = window.confirm(`${plural(c.waiting + c.failed, "report")} from you have not reached the server yet. They stay on this phone and send after you sign in again.\n\nSign out anyway?`);
      if (!ok) return;
    }
    try {
      await fetch(API.logout, { method: "POST", credentials: "same-origin" });
    } catch (_) { /* offline: the cookie expires on its own */ }
    await Promise.all([store.del("user"), store.del("tasks")]).catch(() => {});
    if ("caches" in window) {
      try {
        for (const name of await caches.keys()) {
          if (/-(data|img)$/.test(name)) await caches.delete(name);
        }
      } catch (_) { /* ignore */ }
    }
    S.tasks = [];
    S.updatedAt = null;
    S.userVerified = false;
    S.sessionExpired = false;
    S.drafts.forEach(clearDraftPhotos);
    S.drafts.clear();
    setUser(null);
    showLogin(false);
    toast("Signed out");
  }

  function handleUnauthorized() {
    // Session expired or revoked: keep the outbox (it is re-sent after sign-in), drop the identity.
    const previous = S.user;
    S.userVerified = false;
    setUser(null);
    store.del("user").catch(() => {});
    if (previous) $("login-user").value = previous.username || "";
    showLogin(true);
  }

  function requestPersistence() {
    if (navigator.storage && navigator.storage.persist) {
      navigator.storage.persisted().then((p) => p || navigator.storage.persist()).catch(() => {});
    }
  }

  // ---------- tasks ----------
  async function loadCachedTasks() {
    const cached = await store.get("tasks").catch(() => null);
    if (cached && Array.isArray(cached.tasks) && (!S.user || cached.userId === S.user.id)) {
      S.tasks = cached.tasks;
      S.updatedAt = cached.updatedAt;
    }
  }

  async function refreshTasks(opts = {}) {
    if (S.refreshing || !S.user) return;
    S.refreshing = true;
    renderBadge();
    try {
      const res = await fetch(API.tasks, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
      if (res.status === 401) { handleUnauthorized(); return; }
      if (!res.ok) throw new Error(await errorText(res));
      const fromCache = res.headers.get("X-DRA-Cache") === "stale";
      const data = await res.json();
      if (fromCache) {
        markReachable(false);
        if (!S.tasks.length && Array.isArray(data.tasks)) S.tasks = data.tasks;
        if (opts.announce) toast("No connection — showing saved tasks", true);
        return;
      }
      markReachable(true);
      S.userVerified = true;
      S.tasks = Array.isArray(data.tasks) ? data.tasks : [];
      S.updatedAt = new Date().toISOString();
      await store.set("tasks", { tasks: S.tasks, updatedAt: S.updatedAt, userId: S.user.id, crews: data.crews || [] }).catch(() => {});
      if (opts.announce) toast(`Updated · ${plural(S.tasks.filter((t) => !FINISHED.has(t.status)).length, "open task")}`);
      prefetchPreviews();
    } catch (err) {
      markReachable(false);
      if (opts.announce) toast(err instanceof TypeError ? "No connection — showing saved tasks" : err.message, true);
    } finally {
      S.refreshing = false;
      renderView();
      renderChrome();
    }
  }

  function mergeTask(task) {
    if (!task || task.id == null) return;
    const i = S.tasks.findIndex((t) => t.id === task.id);
    const incident = i >= 0 ? S.tasks[i].incident : undefined;
    const merged = { incident, ...task };
    if (i >= 0) S.tasks[i] = merged; else S.tasks.push(merged);
  }

  let prefetching = false;
  async function prefetchPreviews() {
    if (prefetching || !("serviceWorker" in navigator) || !navigator.serviceWorker.controller) return;
    const conn = navigator.connection;
    if (conn && (conn.saveData || /(^|-)2g$/.test(conn.effectiveType || ""))) return;
    prefetching = true;
    try {
      const ids = sortTasks(allTasks().filter((t) => !FINISHED.has(t.status) && t.image_id))
        .slice(0, 40).map((t) => t.image_id);
      for (const id of [...new Set(ids)]) {
        if (!isOnline()) break;
        try { await fetch(API.image(id, "preview"), { credentials: "same-origin" }); } catch (_) { break; }
      }
    } finally {
      prefetching = false;
    }
  }

  // ---------- sync ----------
  function formFor(item) {
    const form = new FormData();
    form.append("status", item.status);
    form.append("client_id", item.client_id);
    form.append("notes", item.notes || "");
    form.append("hazards", JSON.stringify(item.hazards || []));
    if (item.latitude != null && item.longitude != null) {
      form.append("latitude", String(item.latitude));
      form.append("longitude", String(item.longitude));
    }
    form.append("reported_at", item.reported_at);
    (item.photos || []).forEach((p, i) => {
      form.append("photos", new Blob([p.data], { type: p.type || "image/jpeg" }), p.name || `photo-${i + 1}.jpg`);
    });
    return form;
  }

  async function syncOutbox(manual) {
    if (S.syncing) return;
    await loadOutbox();
    if (!S.user) { renderChrome(); return; }
    const queue = S.outbox.filter((i) => mine(i) && i.state !== "failed");
    if (!queue.length) {
      if (manual) { await refreshTasks({ announce: true }); }
      renderChrome();
      return;
    }
    if (!isOnline()) {
      if (manual) toast("Offline — reports will send when you have signal", true);
      renderChrome();
      return;
    }
    S.syncing = true;
    S.lastSyncError = null;
    renderChrome();
    let sent = 0;
    let stopped = false;
    try {
      for (const item of queue) {
        let res;
        try {
          res = await fetch(API.report(item.task_id), { method: "POST", body: formFor(item), credentials: "same-origin" });
        } catch (_) {
          item.attempts = (item.attempts || 0) + 1;
          item.last_error = "No connection";
          item.last_attempt = Date.now();
          await store.put(item);
          markReachable(false);
          stopped = true;
          break;
        }
        markReachable(true);
        if (res.ok) {
          const data = await res.json().catch(() => ({}));
          await store.remove(item.client_id);
          if (data.task) mergeTask(data.task);
          sent++;
          continue;
        }
        const message = await errorText(res);
        item.attempts = (item.attempts || 0) + 1;
        item.last_attempt = Date.now();
        if (res.status === 401) {
          item.state = "auth";
          item.last_error = "Sign in again to send";
          await store.put(item);
          stopped = true;
          handleUnauthorized();
          break;
        }
        if (res.status === 408 || res.status === 429 || res.status >= 500) {
          item.last_error = message;
          await store.put(item);
          S.lastSyncError = res.status === 429 ? "server busy, will retry" : "server error, will retry";
          stopped = true;
          break;
        }
        item.state = "failed";
        item.error = message;
        await store.put(item);
      }
    } finally {
      S.syncing = false;
      await loadOutbox();
      if (sent) {
        await store.set("tasks", { tasks: S.tasks, updatedAt: S.updatedAt, userId: S.user && S.user.id }).catch(() => {});
        toast(`${plural(sent, "report")} sent`);
      } else if (manual && stopped && !S.sessionExpired) {
        toast("Couldn't send yet — will keep retrying", true);
      }
      renderView();
      renderChrome();
    }
    if (sent && !stopped) refreshTasks();
  }

  // ---------- geolocation ----------
  let lastGeoRender = 0;
  function startLocation() {
    if (!("geolocation" in navigator) || S.watchId != null) return;
    S.watchId = navigator.geolocation.watchPosition((p) => {
      S.pos = { lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy, at: Date.now() };
      S.geoDenied = false;
      $("locate-btn").hidden = true;
      if (Date.now() - lastGeoRender > 5000) { lastGeoRender = Date.now(); renderView(); }
      else updateGpsStatus();
    }, (err) => {
      if (err.code === 1) {
        S.geoDenied = true;
        navigator.geolocation.clearWatch(S.watchId);
        S.watchId = null;
      }
      updateGpsStatus();
    }, { enableHighAccuracy: true, maximumAge: 30000, timeout: 30000 });
  }
  async function initLocation() {
    if (!("geolocation" in navigator)) return;
    try {
      if (navigator.permissions && navigator.permissions.query) {
        const status = await navigator.permissions.query({ name: "geolocation" });
        if (status.state === "granted") { startLocation(); return; }
        if (status.state === "denied") { S.geoDenied = true; }
      }
    } catch (_) { /* Safari without permissions API */ }
    $("locate-btn").hidden = S.geoDenied;
  }
  function currentPosition(timeout) {
    if (S.pos && Date.now() - S.pos.at < 120000) return Promise.resolve(S.pos);
    if (!("geolocation" in navigator) || S.geoDenied) return Promise.resolve(null);
    // Hard deadline: the browser's own timeout does not run while a permission prompt is unanswered,
    // and a report must never be blocked on GPS.
    return new Promise((resolve) => {
      let done = false;
      const finish = (value) => { if (!done) { done = true; resolve(value); } };
      setTimeout(() => finish(S.pos), timeout);
      navigator.geolocation.getCurrentPosition((p) => {
        S.pos = { lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy, at: Date.now() };
        startLocation();
        finish(S.pos);
      }, (err) => { if (err.code === 1) S.geoDenied = true; finish(S.pos); },
      { enableHighAccuracy: true, maximumAge: 60000, timeout });
    });
  }
  function updateGpsStatus() {
    const status = $("gps-status");
    if (!status) return;
    if (!("geolocation" in navigator)) status.textContent = "This phone can't share its location";
    else if (S.geoDenied) status.textContent = "Location blocked — allow it in browser settings";
    else if (S.pos) status.textContent = `GPS fix ±${Math.round(S.pos.acc)} m · ${ago(S.pos.at)}`;
    else status.textContent = "Uses this phone's GPS when you send";
  }

  // ---------- routing ----------
  function go(hash, replace) {
    if (replace) history.replaceState(null, "", hash);
    else if (location.hash !== hash) location.hash = hash;
    else route();
  }
  function parseRoute() {
    const h = location.hash.replace(/^#/, "");
    let m = h.match(/^\/task\/(\d+)$/);
    if (m) return { name: "detail", id: Number(m[1]) };
    if (h === "/outbox") return { name: "outbox" };
    return { name: "list" };
  }
  function route() {
    if (!S.user) { render({ name: "login" }); return; }
    render(parseRoute());
  }

  function render(r) {
    const previous = S.route;
    S.route = r;
    for (const v of ["login", "list", "detail", "outbox"]) $(`view-${v}`).hidden = v !== r.name;
    $("actionbar").hidden = r.name === "login";
    $("ab-list").hidden = r.name !== "list";
    $("ab-detail").hidden = r.name !== "detail";
    $("ab-outbox-set").hidden = r.name !== "outbox";
    const changed = previous.name !== r.name || previous.id !== r.id;
    renderView(changed);
    renderChrome();
    if (changed) {
      window.scrollTo(0, 0);
      if (r.name === "login") setTimeout(() => (S.user || $("login-user").value ? $("login-pass") : $("login-user")).focus(), 50);
    }
  }
  function renderView(fresh) {
    if (S.route.name === "list") renderList();
    else if (S.route.name === "detail") renderDetail(fresh);
    else if (S.route.name === "outbox") renderOutbox();
    document.title = S.route.name === "detail" ? `Task · DRA Field` : "DRA Field";
  }

  // ---------- list ----------
  function sortTasks(list) {
    const byPriority = (a, b) => (b.priority || 0) - (a.priority || 0) || a.id - b.id;
    const byDistance = (a, b) => {
      const da = distanceTo(a), db = distanceTo(b);
      if (da == null && db == null) return byPriority(a, b);
      if (da == null) return 1;
      if (db == null) return -1;
      return da - db;
    };
    const urgent = (t) => (t.status === "needs_usar" ? 0 : 1);
    return [...list].sort((a, b) => {
      const fa = FINISHED.has(a.status) ? 1 : 0, fb = FINISHED.has(b.status) ? 1 : 0;
      if (fa !== fb) return fa - fb;
      if (fa) return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
      if (S.sort === "distance" && S.pos) return byDistance(a, b);
      return urgent(a) - urgent(b) || byPriority(a, b);
    });
  }

  function statusPill(status) {
    return el("span", { class: `pill st-${status}` }, el("i", { class: "pd", "aria-hidden": "true" }), STATUS[status] || status);
  }
  function hazardChips(list) {
    return (list || []).map((h) => el("span", { class: "chip" }, svg(ICON.warn), HAZARD_LABEL[h] || h.replace(/_/g, " ")));
  }

  function taskCard(t) {
    const finished = FINISHED.has(t.status);
    const d = distanceTo(t);
    const color = SEV[t.damage_class] || "#8a909a";
    const sub = [];
    if (d != null) sub.push(el("span", { class: "dist" }, `${fmtDistance(d)} ${bearing(S.pos.lat, S.pos.lon, t.latitude, t.longitude)}`));
    else if (!hasCoords(t)) sub.push(el("span", null, "No coordinates"));
    if (t.incident) sub.push(el("span", null, t.incident));
    if (t.last_report) sub.push(el("span", null, `Reported ${ago(t.last_report)}`));
    const side = [statusPill(t.status)];
    if (t._pending) side.push(el("span", { class: "pill pending" }, svg(ICON.clock), "Unsent"));
    if (t._failed) side.push(el("span", { class: "pill failed" }, "Rejected"));
    const thumb = t.image_id ? el("img", { class: "task-thumb", alt: "", loading: "lazy", decoding: "async", src: API.image(t.image_id, "preview") }) : null;
    if (thumb) thumb.addEventListener("error", () => thumb.remove(), { once: true });
    const label = `${t.damage_class || "Unclassified"}, ${t.title}, ${STATUS[t.status] || t.status}${d != null ? `, ${fmtDistance(d)} away` : ""}${t._pending ? ", report not yet sent" : ""}`;
    return el("li", null, el("a", {
      class: `task${finished ? " closed" : ""}`, href: `#/task/${t.id}`, style: `--sev:${color}`, "aria-label": label,
    },
    el("div", { class: "task-main" },
      el("div", { class: "task-top" },
        el("span", { class: "sev-label" }, t.damage_class || "Unclassified"),
        el("span", { class: "prio", title: "Priority score" }, `P${t.priority || 0}`)),
      el("div", { class: "task-title" }, t.title || `Task ${t.id}`),
      sub.length ? el("div", { class: "task-sub" }, sub) : null,
      t.hazards && t.hazards.length ? el("div", { class: "chips" }, hazardChips(t.hazards)) : null),
    el("div", { class: "task-side" }, thumb, side)));
  }

  function renderList() {
    const tasks = allTasks();
    const open = tasks.filter((t) => !FINISHED.has(t.status));
    const closed = tasks.filter((t) => FINISHED.has(t.status));
    $("count-open").textContent = String(open.length);
    $("count-closed").textContent = String(closed.length);
    const shown = sortTasks(S.filter === "open" ? open : S.filter === "closed" ? closed : tasks);
    const usar = open.filter((t) => t.status === "needs_usar").length;
    $("list-sub").textContent = S.updatedAt
      ? `${plural(open.length, "open task")}${usar ? ` · ${usar} need USAR` : ""} · updated ${clock(S.updatedAt)}`
      : (isOnline() ? "Loading tasks…" : "Offline — no saved tasks on this phone yet");
    const list = $("task-list");
    list.replaceChildren(...shown.map(taskCard));
    const empty = $("list-empty");
    empty.hidden = shown.length > 0;
    if (!shown.length) {
      if (!S.updatedAt && !tasks.length) {
        $("empty-title").textContent = isOnline() ? "Loading tasks" : "No saved tasks";
        $("empty-text").textContent = isOnline() ? "Fetching your crew's assignments…" : "Connect once to download your crew's tasks for offline use.";
      } else if (S.filter === "closed") {
        $("empty-title").textContent = "Nothing finished yet";
        $("empty-text").textContent = "Tasks you mark Searched – clear or Done appear here.";
      } else {
        $("empty-title").textContent = "No open tasks";
        $("empty-text").textContent = "Nothing is assigned to your crew right now. Tap Refresh to check again.";
      }
    }
    document.querySelectorAll(".seg-btn").forEach((b) => {
      const on = b.dataset.filter === S.filter;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", String(on));
    });
    document.querySelectorAll(".sort-btn").forEach((b) => {
      const on = b.dataset.sort === S.sort;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", String(on));
      if (b.dataset.sort === "distance") {
        b.disabled = !S.pos && S.geoDenied;
        b.title = S.pos ? "" : "Needs your location";
      }
    });
    $("locate-btn").hidden = !!S.pos || S.geoDenied || !("geolocation" in navigator);
  }

  // ---------- detail ----------
  function draftFor(id) {
    let d = S.drafts.get(id);
    if (!d) {
      d = { status: "", notes: "", hazards: null, photos: [], gps: true };
      S.drafts.set(id, d);
    }
    return d;
  }
  function clearDraftPhotos(d) {
    for (const p of d.photos || []) if (p.url) URL.revokeObjectURL(p.url);
  }

  function parseLog(task) {
    const lines = String(task.notes || "").split("\n").map((l) => l.trim()).filter(Boolean);
    const out = lines.map((line) => {
      const m = line.match(/^\[(\d{4}-\d\d-\d\dT\d\d:\d\d)(?::\d\d)?\s*([^\]]*)\]\s*(.*)$/);
      if (m) return { when: clock(m[1]), who: m[2], what: m[3] || "" };
      return { when: "", who: "", what: line };
    }).reverse();
    const queued = S.outbox.filter((i) => i.task_id === task.id).sort((a, b) => b.created_at - a.created_at);
    const pending = queued.map((i) => ({
      when: clock(i.reported_at), who: i.user_name || "You",
      what: `${REPORT_LABEL[i.status] || i.status}${i.notes ? `: ${i.notes}` : ""}${i.photos && i.photos.length ? ` (+${plural(i.photos.length, "photo")})` : ""}`,
      state: i.state === "failed" ? "failed" : "pending",
      note: i.state === "failed" ? `Rejected: ${i.error || "unknown error"}` : "Saved on phone · not sent yet",
    }));
    return [...pending, ...out];
  }

  let heroToken = 0;
  function setHero(task) {
    const img = $("hero-img");
    const ph = $("hero-ph");
    const token = ++heroToken;
    $("hero").classList.remove("contain");
    $("hero-class").textContent = task.damage_class || "";
    $("hero").style.setProperty("--sev", SEV[task.damage_class] || "#8a909a");
    if (!task.image_id) {
      img.hidden = true;
      img.removeAttribute("src");
      ph.hidden = false;
      $("hero-ph-text").textContent = "No image for this task";
      return;
    }
    const sources = [API.image(task.image_id, "full"), API.image(task.image_id, "preview")];
    let i = 0;
    img.alt = `${task.damage_class || "Damage"} imagery for ${task.title}`;
    ph.hidden = false;
    $("hero-ph-text").textContent = "Loading image…";
    const tryNext = () => {
      if (token !== heroToken) return;
      if (i >= sources.length) {
        img.hidden = true;
        ph.hidden = false;
        $("hero-ph-text").textContent = isOnline() && S.reachable ? "Image unavailable" : "Image not saved for offline use";
        return;
      }
      img.src = sources[i++];
    };
    img.onload = () => { if (token === heroToken) { img.hidden = false; ph.hidden = true; } };
    img.onerror = tryNext;
    tryNext();
  }

  function renderDetail(fresh) {
    const id = S.route.id;
    const task = findTask(id);
    const view = $("view-detail");
    if (!task) {
      $("detail-title").textContent = S.updatedAt || S.tasks.length ? "Task not found" : "Loading task…";
      $("detail-meta").textContent = "It may have been reassigned or cancelled. Go back to your task list.";
      $("detail-pills").replaceChildren();
      view.querySelectorAll(".card").forEach((c) => { c.hidden = true; });
      $("hero").hidden = true;
      $("ab-submit").disabled = true;
      return;
    }
    view.querySelectorAll(".card").forEach((c) => { c.hidden = false; });
    $("hero").hidden = false;
    if (fresh || $("hero").dataset.task !== String(task.id) || $("hero").dataset.image !== String(task.image_id)) {
      $("hero").dataset.task = String(task.id);
      $("hero").dataset.image = String(task.image_id);
      setHero(task);
    }
    document.title = `${task.title} · DRA Field`;
    $("detail-title").textContent = task.title || `Task ${task.id}`;
    const meta = [task.incident, task.crew ? `Crew: ${task.crew}` : null, `Priority ${task.priority || 0}`,
      task.last_report ? `Last report ${ago(task.last_report)}` : null].filter(Boolean);
    $("detail-meta").textContent = meta.join(" · ");
    const pills = [statusPill(task.status)];
    if (task._pending) pills.push(el("span", { class: "pill pending" }, svg(ICON.clock), `${task._pending} unsent`));
    if (task._failed) pills.push(el("span", { class: "pill failed" }, `${task._failed} rejected`));
    $("detail-pills").replaceChildren(...pills);

    // location
    const geo = $("geo-link"), gmaps = $("gmaps-link");
    if (hasCoords(task)) {
      const lat = task.latitude.toFixed(6), lon = task.longitude.toFixed(6);
      $("detail-coords").textContent = `${lat}, ${lon}`;
      geo.href = `geo:${lat},${lon}?q=${lat},${lon}`;
      gmaps.href = `https://www.google.com/maps/dir/?api=1&destination=${lat},${lon}`;
      $("map-links").hidden = false;
      const d = distanceTo(task);
      $("detail-distance").textContent = d != null
        ? `${fmtDistance(d)} ${bearing(S.pos.lat, S.pos.lon, task.latitude, task.longitude)} of you (GPS ±${Math.round(S.pos.acc)} m)`
        : S.geoDenied ? "Location blocked — distance unavailable" : "Distance shows once your phone has a GPS fix";
    } else {
      $("detail-coords").textContent = "No coordinates recorded";
      $("detail-distance").textContent = "Ask your coordinator for the address.";
      $("map-links").hidden = true;
    }

    // log
    $("detail-hazards").replaceChildren(...hazardChips(task.hazards));
    const entries = parseLog(task);
    $("notes-log").replaceChildren(...(entries.length ? entries.map((e) => el("li", { class: e.state || "" },
      el("span", { class: "bullet", "aria-hidden": "true" }),
      el("div", null,
        el("div", { class: "when" }, [e.when, e.who].filter(Boolean).join(" · ") || "Note"),
        el("div", { class: "what" }, e.what),
        e.note ? el("div", { class: "fine" }, e.note) : null)))
      : [el("li", null, el("span", { class: "empty-log" }, "No reports yet. Your first report starts the log."))]));

    // form (only rebuilt when opening a task so typing is never interrupted)
    if (fresh || $("report-form").dataset.task !== String(task.id)) buildForm(task);
    updateSubmitButton();
    updateGpsStatus();
  }

  function buildForm(task) {
    const form = $("report-form");
    form.dataset.task = String(task.id);
    const draft = draftFor(task.id);
    if (!draft.hazards) draft.hazards = new Set(task.hazards || []);
    const known = new Set(task.hazards || []);
    form.querySelectorAll('input[name="status"]').forEach((input) => {
      input.checked = input.value === draft.status;
      input.closest(".status-opt").classList.toggle("checked", input.checked);
    });
    $("hazard-grid").replaceChildren(...HAZARDS.map(([key, label]) => {
      const on = draft.hazards.has(key);
      const input = el("input", { type: "checkbox", value: key, checked: on });
      const wrap = el("label", { class: `haz${on ? " on" : ""}` }, input,
        el("span", { class: "box", "aria-hidden": "true" }, svg(ICON.check)),
        el("span", null, label, known.has(key) ? el("span", { class: "known" }, "Already reported") : null));
      input.addEventListener("change", () => {
        if (input.checked) draft.hazards.add(key); else draft.hazards.delete(key);
        wrap.classList.toggle("on", input.checked);
      });
      return wrap;
    }));
    $("report-notes").value = draft.notes;
    $("attach-gps").checked = draft.gps && !S.geoDenied;
    $("report-error").hidden = true;
    renderPhotos(draft);
  }

  function currentDraft() {
    return S.route.name === "detail" ? draftFor(S.route.id) : null;
  }

  function updateSubmitButton() {
    const btn = $("ab-submit");
    const draft = currentDraft();
    const task = draft && findTask(S.route.id);
    if (!task) { btn.disabled = true; btn.textContent = "Task unavailable"; return; }
    const busy = draft.photos.some((p) => p.busy);
    if (!draft.status) { btn.disabled = true; btn.textContent = "Choose a status"; return; }
    if (busy) { btn.disabled = true; btn.textContent = "Preparing photos…"; return; }
    if (S.saving) { btn.disabled = true; btn.textContent = $("attach-gps").checked && !S.pos ? "Getting GPS fix…" : "Saving…"; return; }
    btn.disabled = false;
    btn.textContent = isOnline() && S.reachable ? `Send: ${REPORT_LABEL[draft.status]}` : `Save offline: ${REPORT_LABEL[draft.status]}`;
  }

  // ---------- photos ----------
  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.decoding = "async";
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("This photo format can't be read on this phone."));
      img.src = url;
    });
  }
  async function downscale(file) {
    const url = URL.createObjectURL(file);
    try {
      const img = await loadImage(url); // browsers apply EXIF orientation when decoding <img>
      const w = img.naturalWidth, h = img.naturalHeight;
      const scale = Math.min(1, MAX_SIDE / Math.max(w, h));
      const cw = Math.max(1, Math.round(w * scale)), ch = Math.max(1, Math.round(h * scale));
      const canvas = document.createElement("canvas");
      canvas.width = cw;
      canvas.height = ch;
      const ctx = canvas.getContext("2d");
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(img, 0, 0, cw, ch);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.8));
      canvas.width = canvas.height = 0;
      if (!blob) throw new Error("Couldn't compress this photo.");
      return blob;
    } finally {
      URL.revokeObjectURL(url);
    }
  }
  async function addPhotos(files) {
    const draft = currentDraft();
    if (!draft) return;
    const room = MAX_PHOTOS - draft.photos.length;
    const picked = [...files].filter((f) => !f.type || f.type.startsWith("image/")).slice(0, Math.max(0, room));
    if (files.length > room) toast(`Up to ${MAX_PHOTOS} photos per report`, true);
    const jobs = picked.map((file, i) => {
      const entry = { id: uuid(), name: `photo-${Date.now()}-${i + 1}.jpg`, type: "image/jpeg", busy: true, url: null, data: null };
      draft.photos.push(entry);
      return { file, entry };
    });
    renderPhotos(draft);
    updateSubmitButton();
    for (const { file, entry } of jobs) {
      try {
        const blob = await downscale(file);
        entry.data = await blob.arrayBuffer();
        entry.size = blob.size;
        entry.url = URL.createObjectURL(blob);
      } catch (err) {
        draft.photos = draft.photos.filter((p) => p !== entry);
        toast(err.message || "Couldn't add that photo", true);
      }
      entry.busy = false;
      if (currentDraft() === draft) { renderPhotos(draft); updateSubmitButton(); }
    }
  }
  function renderPhotos(draft) {
    const grid = $("photo-grid");
    const add = $("photo-add");
    const tiles = draft.photos.map((p, i) => {
      const remove = el("button", { type: "button", class: "rm", "aria-label": `Remove photo ${i + 1}` }, svg(ICON.x));
      remove.addEventListener("click", () => {
        if (p.url) URL.revokeObjectURL(p.url);
        draft.photos = draft.photos.filter((x) => x !== p);
        renderPhotos(draft);
        updateSubmitButton();
      });
      return el("div", { class: "photo" },
        p.url ? el("img", { src: p.url, alt: `Photo ${i + 1}` }) : null,
        p.busy ? el("div", { class: "busy" }, "Shrinking…") : null,
        p.busy ? null : remove);
    });
    grid.replaceChildren(...tiles, add);
    add.classList.toggle("full", draft.photos.length >= MAX_PHOTOS);
    $("photo-count").textContent = `${draft.photos.length} / ${MAX_PHOTOS}`;
  }

  // ---------- submit ----------
  async function submitReport(event) {
    event.preventDefault();
    if (S.saving) return;
    const task = findTask(S.route.id);
    const draft = currentDraft();
    const err = $("report-error");
    err.hidden = true;
    if (!task || !draft) return;
    if (!draft.status) {
      err.textContent = "Choose a status first.";
      err.hidden = false;
      return;
    }
    if (draft.photos.some((p) => p.busy)) return;
    S.saving = true;
    updateSubmitButton();
    try {
      let pos = null;
      if ($("attach-gps").checked) pos = await currentPosition(6000);
      const item = {
        client_id: uuid(),
        task_id: task.id,
        task_title: task.title || `Task ${task.id}`,
        incident: task.incident || null,
        user_id: S.user.id,
        user_name: S.user.name || S.user.username,
        status: draft.status,
        notes: draft.notes.trim(),
        hazards: [...draft.hazards].sort(),
        latitude: pos ? Number(pos.lat.toFixed(7)) : null,
        longitude: pos ? Number(pos.lon.toFixed(7)) : null,
        accuracy: pos ? Math.round(pos.acc) : null,
        reported_at: new Date().toISOString(),
        photos: draft.photos.filter((p) => p.data).map((p) => ({ name: p.name, type: p.type, data: p.data, size: p.size })),
        state: "pending",
        attempts: 0,
        created_at: Date.now(),
        error: null,
      };
      try {
        await store.put(item);
      } catch (e) {
        err.textContent = /quota/i.test(String(e && (e.name || e.message)))
          ? "Phone storage is full. Remove some photos and try again."
          : `Couldn't save the report on this phone: ${e && e.message ? e.message : e}`;
        err.hidden = false;
        return;
      }
      if (!store.isPersistent) toast("Warning: this browser can't keep reports offline — stay online until sent", true);
      const status = draft.status;
      clearDraftPhotos(draft);
      S.drafts.delete(task.id);
      await loadOutbox();
      toast(isOnline() ? "Report saved — sending…" : "Saved on phone — will send when online");
      if (status === "done" || status === "searched_clear") go("#/");
      else { renderDetail(true); window.scrollTo({ top: 0, behavior: "smooth" }); }
      renderChrome();
      syncOutbox();
    } finally {
      S.saving = false;
      updateSubmitButton();
    }
  }

  // ---------- outbox ----------
  function renderOutbox() {
    const items = [...S.outbox].sort((a, b) => b.created_at - a.created_at);
    $("outbox-empty").hidden = items.length > 0;
    $("outbox-list").replaceChildren(...items.map((item) => {
      const other = S.user && !mine(item);
      const state = other ? "auth" : item.state === "failed" ? "failed" : item.state === "auth" ? "auth" : "pending";
      const stateText = other
        ? `Saved by ${item.user_name || "another user"} — they must sign in on this phone to send it`
        : state === "failed" ? `Rejected: ${item.error || "unknown error"}`
          : state === "auth" ? "Waiting for sign-in"
            : S.syncing ? "Sending…"
              : item.attempts ? `Waiting to retry (${plural(item.attempts, "attempt")}${item.last_error ? ` · ${item.last_error}` : ""})` : "Waiting to send";
      const size = (item.photos || []).reduce((n, p) => n + (p.size || (p.data ? p.data.byteLength : 0)), 0);
      const actions = [];
      if (!other && state === "failed") {
        actions.push(el("button", { type: "button", class: "btn primary", onclick: () => retryItem(item) }, "Retry"));
      }
      actions.push(el("button", { type: "button", class: "btn ghost", onclick: () => discardItem(item) }, "Discard"));
      return el("li", { class: `ob ${state}` },
        el("div", { class: "ob-top" },
          el("a", { class: "ob-title", href: `#/task/${item.task_id}`, style: "color:inherit" }, item.task_title),
          statusPill(item.status)),
        el("div", { class: "ob-meta" },
          [`Saved ${clock(item.created_at)}`, item.photos && item.photos.length ? `${plural(item.photos.length, "photo")} (${(size / 1048576).toFixed(1)} MB)` : null,
            item.latitude != null ? "GPS attached" : null, item.hazards.length ? plural(item.hazards.length, "hazard") : null].filter(Boolean).join(" · ")),
        item.notes ? el("div", { class: "what" }, item.notes) : null,
        el("div", { class: state === "failed" ? "ob-err" : "ob-meta" }, stateText),
        el("div", { class: "ob-actions" }, actions));
    }));
  }
  async function retryItem(item) {
    item.state = "pending";
    item.error = null;
    await store.put(item);
    await loadOutbox();
    renderView();
    syncOutbox(true);
  }
  async function discardItem(item) {
    const what = item.state === "failed" ? "this rejected report" : "this unsent report";
    if (!window.confirm(`Delete ${what} for “${item.task_title}” from this phone? It has not reached the server.`)) return;
    await store.remove(item.client_id);
    await loadOutbox();
    renderView();
    renderChrome();
  }

  // ---------- menu ----------
  function closeMenu() {
    $("user-menu").hidden = true;
    $("user-btn").setAttribute("aria-expanded", "false");
  }

  // ---------- wiring ----------
  function wire() {
    $("login-form").addEventListener("submit", login);
    $("logout-btn").addEventListener("click", logout);
    $("user-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      const open = $("user-menu").hidden;
      $("user-menu").hidden = !open;
      $("user-btn").setAttribute("aria-expanded", String(open));
      if (open) $("logout-btn").focus();
    });
    document.addEventListener("click", (e) => { if (!$("user-menu").contains(e.target)) closeMenu(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
    $("banner-action").addEventListener("click", () => { if (bannerAction) bannerAction(); });

    $("ab-refresh").addEventListener("click", async () => {
      if (!isOnline()) { toast("Offline — showing saved tasks", true); return; }
      await refreshTasks({ announce: true });
      syncOutbox();
    });
    $("ab-sync").addEventListener("click", () => syncOutbox(true));
    $("ab-sync2").addEventListener("click", () => syncOutbox(true));
    $("ab-outbox").addEventListener("click", () => go("#/outbox"));
    $("ab-back").addEventListener("click", () => go("#/"));
    $("ab-back2").addEventListener("click", () => go("#/"));

    document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => { S.filter = b.dataset.filter; renderList(); }));
    document.querySelectorAll(".sort-btn").forEach((b) => b.addEventListener("click", () => {
      S.sort = b.dataset.sort;
      if (S.sort === "distance" && !S.pos) { startLocation(); toast("Waiting for GPS fix…"); }
      renderList();
    }));
    $("locate-btn").addEventListener("click", () => { startLocation(); toast("Waiting for GPS fix…"); });

    $("report-form").addEventListener("submit", submitReport);
    $("status-grid").addEventListener("change", (e) => {
      const draft = currentDraft();
      if (!draft || e.target.name !== "status") return;
      draft.status = e.target.value;
      document.querySelectorAll(".status-opt").forEach((o) => o.classList.toggle("checked", o.querySelector("input").checked));
      updateSubmitButton();
    });
    $("report-notes").addEventListener("input", (e) => { const d = currentDraft(); if (d) d.notes = e.target.value; });
    $("attach-gps").addEventListener("change", (e) => {
      const d = currentDraft();
      if (d) d.gps = e.target.checked;
      if (e.target.checked) startLocation();
    });
    $("photo-input").addEventListener("change", (e) => {
      const files = e.target.files ? [...e.target.files] : [];
      e.target.value = "";
      if (files.length) addPhotos(files);
    });
    $("hero").addEventListener("click", () => $("hero").classList.toggle("contain"));

    window.addEventListener("hashchange", route);
    window.addEventListener("online", async () => {
      S.reachable = true;
      renderChrome();
      await syncOutbox();
      refreshTasks();
    });
    window.addEventListener("offline", () => { renderChrome(); if (S.route.name === "login") $("login-offline").hidden = false; });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible" || !S.user) return;
      syncOutbox();
      const age = S.updatedAt ? Date.now() - parseTime(S.updatedAt).getTime() : Infinity;
      if (isOnline() && age > REFRESH_EVERY) refreshTasks();
    });

    setInterval(() => {
      if (S.user && isOnline() && outboxCounts().waiting) syncOutbox();
      renderBanner();
      updateGpsStatus();
    }, SYNC_EVERY);
    setInterval(() => {
      if (S.user && isOnline() && document.visibilityState === "visible") refreshTasks();
    }, REFRESH_EVERY);
  }

  function registerWorker() {
    if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
    navigator.serviceWorker.register("/field/sw.js", { scope: "/field/" }).catch((err) => {
      console.warn("DRA Field: service worker registration failed", err);
    });
  }

  async function start() {
    if (location.pathname === "/field") history.replaceState(null, "", `/field/${location.hash}`);
    wire();
    registerWorker();
    await store.ready();
    await loadOutbox();
    const authed = await checkAuth();
    if (!authed) { renderChrome(); return; }
    await loadCachedTasks();
    route();
    initLocation();
    if (isOnline()) {
      await syncOutbox();
      await refreshTasks();
    } else {
      renderChrome();
    }
  }

  start();
})();
