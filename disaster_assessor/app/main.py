"""FastAPI application: API routers, static web apps, security headers, background services."""

import asyncio
from contextlib import asynccontextmanager
import logging
import re
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import LOOPBACK_HOSTS, ROOT, get_config
from .jobs import runner
from .routes import admin, assessment, auth as auth_routes, exports, geo, incidents, operations, share

WEB = ROOT / "web"
log = logging.getLogger("dra")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


async def _retention_loop():
    cfg = get_config()
    while cfg.retention_days:
        try:
            deleted = admin.apply_retention(cfg.retention_days, dry_run=False)
            if deleted:
                log.info("Retention removed %d closed incident(s)", len(deleted))
        except Exception:
            log.exception("Retention sweep failed")
        await asyncio.sleep(24 * 3600)


def check_startup(cfg):
    if cfg.no_login and cfg.host not in LOOPBACK_HOSTS:
        raise SystemExit("DRA_NO_LOGIN only works when HOST is 127.0.0.1/localhost. Remove it to expose the server.")
    if cfg.no_login:
        log.warning("No-login mode: anyone who can reach this server is an administrator.")


@asynccontextmanager
async def lifespan(_app):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    check_startup(get_config())
    db.init_db()
    runner.recover()
    task = asyncio.create_task(_retention_loop())
    yield
    task.cancel()
    runner.shutdown()


app = FastAPI(title="Rapid Disaster Assessment", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
for module in (auth_routes, incidents, assessment, operations, geo, exports, share, admin):
    app.include_router(module.router)


def _tile_origin(url):
    parsed = urlparse(url.replace("{s}", "a"))
    host = parsed.netloc
    if "{s}" in url:
        host = "*." + host.split(".", 1)[1]
    return f"{parsed.scheme}://{host}"


def _csp():
    tiles = _tile_origin(get_config().tile_url)
    return ("default-src 'self'; "
            "script-src 'self' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            f"img-src 'self' data: blob: {tiles} https://unpkg.com; "
            "connect-src 'self'; worker-src 'self'; manifest-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security(request: Request, call_next):
    if get_config().no_login:
        # Without sign-in, only accept requests addressed to this machine (blocks DNS-rebinding pages).
        hostname = request.headers.get("host", "").rsplit(":", 1)[0] if not request.headers.get("host", "").startswith("[") \
            else request.headers.get("host", "").split("]")[0] + "]"
        if hostname not in LOOPBACK_HOSTS:
            return JSONResponse({"error": "No-login mode only answers requests to localhost."}, status_code=403)
    if request.method in UNSAFE:
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            allowed = {f"http://{host}", f"https://{host}", *get_config().allowed_origins}
            if origin not in allowed:
                return JSONResponse({"error": "Cross-site request refused."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # OSM tile servers require a Referer; send only the origin so share tokens in paths never leak.
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(self), geolocation=(self), microphone=()")
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Content-Security-Policy", _csp())
    return response


@app.exception_handler(HTTPException)
async def http_error(_, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(_, exc):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
    return JSONResponse({"error": f"{field}: {first.get('msg', 'invalid value')}"}, status_code=422)


@app.get("/api/health")
def health():
    with db.connect() as conn:
        db.one(conn, "SELECT 1 AS ok")
    return {"ok": True}


# ---------- web apps ----------

app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(WEB / "index.html")


@app.get("/field", include_in_schema=False)
@app.get("/field/", include_in_schema=False)
def field_app():
    return FileResponse(WEB / "field" / "index.html")


@app.get("/field/sw.js", include_in_schema=False)
def field_worker():
    return FileResponse(WEB / "field" / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/field/", "Cache-Control": "no-cache"})


@app.get("/field/manifest.webmanifest", include_in_schema=False)
def field_manifest():
    return FileResponse(WEB / "field" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/share/{token}", include_in_schema=False)
def share_page(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        raise HTTPException(404, "Not found")
    return FileResponse(WEB / "share.html")
