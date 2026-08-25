"""Restock monitor API — watches product pages and alerts on Telegram.

Follows the LinkVST server layout (lifespan -> init_db, auth middleware,
routers under /api, single-file static dashboard) so the two apps stay
recognisably the same shape.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import db, scheduler, security
from .config import API_KEY, COOKIE_NAME
from .fetcher import close_client
from .routes import auth, detect, events, watches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("monitor")

VERSION = "0.1.0"
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()
        await close_client()


app = FastAPI(title="Restock Monitor", version=VERSION, lifespan=lifespan)

# No CORS middleware: the dashboard is served from this same origin, and the
# session cookie should not be reachable from anywhere else.

PUBLIC_PATHS = {"/health", "/", "/favicon.ico",
                "/api/login", "/api/logout", "/api/me",
                "/docs", "/openapi.json"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
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


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/health")
def health():
    """Unauthenticated so Caddy and uptime checks can reach it."""
    return {"status": "ok", "service": "restock-monitor", "version": VERSION}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.include_router(auth.router,    prefix="/api")
app.include_router(watches.router, prefix="/api")
app.include_router(events.router,  prefix="/api")
app.include_router(detect.router,  prefix="/api")
