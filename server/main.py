"""LinkVST API Server — MIDI taste modeling + generation via Claude."""
import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from . import db
from .routes import upload, generate, library


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="LinkVST API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = os.environ.get("LINK_API_KEY", "")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path in ("/health", "/docs", "/openapi.json"):
        return await call_next(request)
    if API_KEY:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != API_KEY:
            raise HTTPException(401, "Unauthorized")
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "service": "link-vst-api"}


app.include_router(upload.router, prefix="/api")
app.include_router(generate.router, prefix="/api")
app.include_router(library.router, prefix="/api")
