"""Restock monitor API — watches product pages and alerts on Telegram.

Layout: a `lifespan` hook opens the database and starts the scheduler, one
middleware handles auth for everything under /api, and the dashboard is a single
static file served from the same origin so the session cookie never travels.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import db, scheduler, security, telegram_bot
from .config import API_KEY, COOKIE_NAME
from .fetcher import close_client
from .routes import auth, detect, events, inspect, watches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("monitor")

VERSION = "0.1.0"


def _build() -> str:
    """The deployed commit, read once at import.

    Read from git rather than written in by the deploy script, so it cannot
    drift from what is actually checked out.
    """
    import subprocess
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent.parent, capture_output=True,
            text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


BUILD = _build()
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Idempotent, so it costs one indexed query per boot and repairs any watch
    # created before creation derived a readable name.
    renamed = db.backfill_watch_names()
    if renamed:
        log.info("renamed %d watch(es) that were showing a raw URL", renamed)
    scheduler.start()
    telegram_bot.start()
    try:
        yield
    finally:
        await telegram_bot.shutdown()
        scheduler.shutdown()
        await close_client()


app = FastAPI(title="Restock Monitor", version=VERSION, lifespan=lifespan)

# No CORS middleware: the dashboard is served from this same origin, and the
# session cookie should not be reachable from anywhere else.

PUBLIC_PATHS = {"/health", "/", "/favicon.ico",
                "/api/login", "/api/logout", "/api/me",
                # Carries its own single-use signed token; requiring a session
                # to reach the thing that grants a session is a closed loop.
                "/api/auth/telegram",
                "/docs", "/openapi.json"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    _remember_origin(request)
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)

    if not path.startswith("/api/"):
        return await call_next(request)

    if security.session_valid(request.cookies.get(COOKIE_NAME)):
        return await call_next(request)

    header = request.headers.get("Authorization", "")
    if API_KEY and header.startswith("Bearer ") and header[7:] == API_KEY:
        return await call_next(request)

    if not security.configured():
        return JSONResponse(
            {"detail": "Auth is not configured. Run `python -m monitor.hashpw` "
                       "on the server and fill in monitor/.env."},
            status_code=503,
        )
    return JSONResponse({"detail": "Not authenticated"}, status_code=401)


def _remember_origin(request: Request) -> None:
    """Record the public URL this app is reached on.

    The bot has to put an absolute link in a chat message, and the app has no
    other way to learn its own address: it listens on localhost behind a proxy,
    and the hostname lives in the proxy's config. Every inbound request carries
    it, so the first visit to the dashboard teaches it permanently. PUBLIC_URL
    overrides this when set.
    """
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host or host.startswith(("localhost", "127.0.0.1")):
        return
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    origin = f"{scheme}://{host}"
    try:
        if db.kv_get("public_origin") != origin:
            db.kv_set("public_origin", origin)
    except Exception:          # never fail a request over a convenience
        pass


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/health")
def health():
    """Unauthenticated so Caddy and uptime checks can reach it."""
    return {"status": "ok", "service": "restock-monitor", "version": VERSION,
            # Which commit is actually answering. "Is my fix live?" should not
            # require reading a stylesheet through a screenshot.
            "build": BUILD}


@app.get("/")
def index(request: Request):
    """The dashboard, always the current one.

    no-cache, not no-store: the browser must revalidate every time, but an
    unchanged page answers 304 with no body — a round trip rather than 58KB
    over cellular. Without any Cache-Control at all a browser applies
    heuristic caching, which is how a layout fix that was live on the server
    stayed invisible on the phone that reported the bug.
    """
    page = STATIC / "index.html"
    stat = page.stat()
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    headers = {"Cache-Control": "no-cache", "ETag": etag}

    if etag in [t.strip() for t in
                request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return FileResponse(page, headers=headers)


app.include_router(auth.router,    prefix="/api")
app.include_router(watches.router, prefix="/api")
app.include_router(events.router,  prefix="/api")
app.include_router(detect.router,  prefix="/api")
app.include_router(inspect.router, prefix="/api")
