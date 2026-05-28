"""LinkVST API Server — MIDI taste modeling + generation via Claude."""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

load_dotenv()

from . import db
from .routes import upload, generate, library, preview, presets


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="LinkVST API", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = os.environ.get("LINK_API_KEY", "")

_NO_AUTH = {"/health", "/docs", "/openapi.json", "/"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in _NO_AUTH or path.startswith("/api/preview/"):
        return await call_next(request)
    if API_KEY:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != API_KEY:
            raise HTTPException(401, "Unauthorized")
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "service": "link-vst-api", "version": "0.3.0"}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.include_router(upload.router,   prefix="/api")
app.include_router(generate.router, prefix="/api")
app.include_router(library.router,  prefix="/api")
app.include_router(preview.router,  prefix="/api")
app.include_router(presets.router,  prefix="/api")
