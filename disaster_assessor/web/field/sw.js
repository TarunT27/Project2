/* DRA Field service worker (scope /field/).
 * - App shell: cache-first, refreshed in the background so the next launch picks up new code.
 * - GET /api/field/tasks: network-first, falls back to the last good copy (marked X-DRA-Cache: stale).
 * - GET /api/images/*: cached opportunistically (cache-first); a missing full image falls back to its preview.
 * - Never caches POSTs, auth endpoints or anything else under /api/.
 */
const VERSION = "dra-field-v1";
const SHELL_CACHE = `${VERSION}-shell`;
const DATA_CACHE = `${VERSION}-data`;
const IMG_CACHE = `${VERSION}-img`;
const SHELL = [
  "/field/",
  "/static/field/field.js",
  "/static/field/field.css",
  "/field/manifest.webmanifest",
  "/static/field/icon.svg",
];
const MAX_IMAGES = 160;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL.map((url) => new Request(url, { cache: "reload", credentials: "same-origin" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = new Set([SHELL_CACHE, DATA_CACHE, IMG_CACHE]);
    for (const name of await caches.keys()) {
      if (name.startsWith("dra-field-") && !keep.has(name)) await caches.delete(name);
    }
    if (self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.disable(); } catch (_) { /* ignore */ }
    }
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return; // POSTs (reports, sign-in) always go straight to the network
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;

  if (path.startsWith("/api/auth/")) return;
  if (path === "/api/field/tasks") { event.respondWith(networkFirst(request)); return; }
  if (/^\/api\/images\/\d+\/file$/.test(path)) { event.respondWith(imageCacheFirst(request, url)); return; }
  if (path.startsWith("/api/")) return;

  if (request.mode === "navigate" && (path === "/field" || path.startsWith("/field/"))) {
    event.respondWith(shell("/field/", event));
    return;
  }
  if (SHELL.includes(path)) event.respondWith(shell(path, event));
});

async function shell(key, event) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(key);
  const refresh = fetch(new Request(key, { cache: "no-cache", credentials: "same-origin" }))
    .then((res) => {
      if (res && res.ok && res.type === "basic") return cache.put(key, res.clone()).then(() => res);
      return res;
    });
  if (cached) {
    event.waitUntil(refresh.catch(() => {}));
    return cached;
  }
  try {
    return await refresh;
  } catch (err) {
    return new Response("DRA Field is offline and not cached yet. Open it once with a connection.", {
      status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }
}

async function networkFirst(request) {
  const cache = await caches.open(DATA_CACHE);
  try {
    const res = await fetch(request);
    if (res.ok) await cache.put(request.url, res.clone());
    else if (res.status === 401 || res.status === 403) await cache.delete(request.url);
    return res;
  } catch (err) {
    const cached = await cache.match(request.url);
    if (!cached) throw err;
    const headers = new Headers(cached.headers);
    headers.set("X-DRA-Cache", "stale");
    return new Response(await cached.blob(), { status: 200, statusText: "OK (cached)", headers });
  }
}

async function imageCacheFirst(request, url) {
  const cache = await caches.open(IMG_CACHE);
  const key = url.pathname + url.search;
  const cached = await cache.match(key);
  if (cached) return cached;
  try {
    const res = await fetch(request);
    if (res.ok && res.type === "basic") {
      await cache.put(key, res.clone());
      trim(cache);
    }
    return res;
  } catch (err) {
    // Offline and the full image was never fetched: the preview may have been prefetched.
    if (url.searchParams.get("variant") !== "preview") {
      const preview = await cache.match(`${url.pathname}?variant=preview`);
      if (preview) return preview;
    }
    return new Response("", { status: 504, statusText: "Offline" });
  }
}

let trimming = false;
async function trim(cache) {
  if (trimming) return;
  trimming = true;
  try {
    const keys = await cache.keys();
    for (let i = 0; i < keys.length - MAX_IMAGES; i++) await cache.delete(keys[i]);
  } finally {
    trimming = false;
  }
}

self.addEventListener("message", (event) => {
  if (event.data === "clear-user-data") {
    event.waitUntil(Promise.all([caches.delete(DATA_CACHE), caches.delete(IMG_CACHE)]));
  }
});
