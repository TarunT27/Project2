// API client: JSON helpers, uploads with progress, and the live event stream.

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

let onUnauthorized = () => {};
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

async function request(method, url, body, { raw = false } = {}) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  let res;
  try { res = await fetch(url, opts); }
  catch { throw new ApiError("Can't reach the server. Check your connection.", 0); }
  if (res.status === 401 && !url.startsWith("/api/auth/")) onUnauthorized();
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try { const data = await res.json(); message = data.error || data.detail || message; } catch { /* not JSON */ }
    throw new ApiError(message, res.status);
  }
  if (raw) return res;
  return res.status === 204 ? null : res.json();
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body ?? {}),
  patch: (url, body) => request("PATCH", url, body),
  del: (url) => request("DELETE", url),
  form: (url, formData) => request("POST", url, formData),
};

// Multipart upload with progress (fetch has no upload progress events).
export function upload(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.responseType = "json";
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      const body = xhr.response || {};
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else {
        if (xhr.status === 401) onUnauthorized();
        reject(new ApiError(body.error || `Upload failed (${xhr.status})`, xhr.status));
      }
    };
    xhr.onerror = () => reject(new ApiError("Upload failed: connection lost.", 0));
    xhr.send(formData);
  });
}

// Server-Sent Events with automatic reconnect (EventSource handles retry) and status callbacks.
export function liveEvents(incidentId, onEvent, onStatus) {
  const url = incidentId ? `/api/events?incident_id=${encodeURIComponent(incidentId)}` : "/api/events";
  const source = new EventSource(url, { withCredentials: true });
  source.onopen = () => onStatus && onStatus(true);
  source.onerror = () => onStatus && onStatus(false);
  source.onmessage = (e) => { try { onEvent(JSON.parse(e.data)); } catch { /* ignore malformed */ } };
  return () => source.close();
}
