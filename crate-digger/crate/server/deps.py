"""Shared application state."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from ..config import Settings
from ..db import Database
from ..jobs import JobRunner
from ..sources.registry import Registry


class AppState:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.ensure_dirs()
        self.db = Database(settings.db_path)
        self.registry = Registry(settings)
        self.jobs = JobRunner()

    async def startup(self) -> None:
        await self.jobs.start()

    async def shutdown(self) -> None:
        await self.jobs.stop()
        await self.registry.aclose()
        self.db.close()


def state(request: Request) -> AppState:
    return request.app.state.crate


def get_db(request: Request) -> Database:
    return state(request).db


def get_settings(request: Request) -> Settings:
    return state(request).settings


def require_sample(request: Request, sample_id: int) -> dict:
    row = state(request).db.get_sample(sample_id)
    if not row:
        raise HTTPException(404, f"No sample {sample_id}")
    return row


def require_audio(request: Request, sample_id: int) -> tuple[dict, Path]:
    row = require_sample(request, sample_id)
    if not row.get("file_path"):
        raise HTTPException(
            409, "That's a lead, not a file yet — download it first."
        )
    path = Path(row["file_path"])
    if not path.is_file():
        raise HTTPException(410, f"File has gone missing: {path}")
    return row, path


def safe_library_path(settings: Settings, path: Path | str) -> Path:
    """Refuse to serve anything outside the library folder."""
    resolved = Path(path).resolve()
    roots = [settings.library_dir.resolve()]
    if settings.export_dir:
        roots.append(Path(settings.export_dir).resolve())
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise HTTPException(403, "Path is outside the library")
