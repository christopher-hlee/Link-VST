"""Working the audio: playback, waveform, chopping, loops, and getting out to the DAW."""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .. import library
from ..audio import chop as chopper
from ..audio.cache import CACHE
from .deps import require_audio, require_sample, safe_library_path, state
from .schemas import ChopExportIn, ChopIn, LoopIn

router = APIRouter(prefix="/api", tags=["audio"])

MIME = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".wav": "audio/wav", ".aiff": "audio/aiff",
    ".aif": "audio/aiff", ".m4a": "audio/mp4", ".opus": "audio/opus",
    ".webm": "audio/webm",
}


def _mime(path: Path) -> str:
    return MIME.get(path.suffix.lower(), "application/octet-stream")


def _nice_name(row: dict, suffix: str = "") -> str:
    bits = [row.get("artist") or "", row.get("title") or f"sample-{row.get('id')}"]
    stem = library.slugify(" ".join(b for b in bits if b).strip() or "sample")
    return f"{stem}{suffix}"


def _serve(path: Path, filename: str, *, attachment: bool) -> FileResponse:
    disposition = "attachment" if attachment else "inline"
    return FileResponse(
        path,
        media_type=_mime(path),
        headers={
            "Content-Disposition":
                f"{disposition}; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, max-age=3600",
        },
    )


# -- playback and waveform ---------------------------------------------
@router.get("/samples/{sample_id}/file")
async def sample_file(request: Request, sample_id: int, download: bool = False):
    """The original audio. Supports Range, so scrubbing works on long records."""
    row, path = require_audio(request, sample_id)
    safe_library_path(state(request).settings, path)
    return _serve(path, f"{_nice_name(row)}{path.suffix}", attachment=download)


@router.get("/samples/{sample_id}/peaks")
async def sample_peaks(request: Request, sample_id: int) -> dict:
    row = require_sample(request, sample_id)
    if row.get("peaks"):
        return {"peaks": row["peaks"], "duration": row.get("duration")}
    _, path = require_audio(request, sample_id)
    y, _ = await asyncio.to_thread(CACHE.load, path, sr=8000)
    from ..audio import dsp

    peaks = dsp.peaks(y, library.PEAK_BUCKETS)
    state(request).db.update_sample(sample_id, peaks=peaks)
    return {"peaks": peaks, "duration": row.get("duration")}


@router.post("/samples/{sample_id}/analyze")
async def reanalyze(request: Request, sample_id: int) -> dict:
    _, path = require_audio(request, sample_id)
    db = state(request).db
    try:
        features = await asyncio.to_thread(library.analyze_file, path)
    except Exception as exc:
        db.update_sample(sample_id, status="error", error=f"Analysis failed: {exc}")
        raise HTTPException(500, f"Analysis failed: {exc}") from exc
    db.update_sample(sample_id, **features, status="ready", error=None)
    return require_sample(request, sample_id)


# -- chopping -----------------------------------------------------------
def _run_chop(path: Path, body: ChopIn, fallback_bpm: float | None) -> list[dict]:
    y, sr = CACHE.load(path)
    if body.mode == "grid":
        bpm = body.bpm or fallback_bpm
        if not bpm:
            raise ValueError("Grid chopping needs a BPM — analyse the sample first.")
        slices = chopper.chop_grid(
            y, sr, bpm, division=body.division, start=body.start,
            end=body.end, max_slices=body.max_slices,
        )
    else:
        slices = chopper.chop_transient(
            y, sr, sensitivity=body.sensitivity, min_length=body.min_length,
            max_slices=body.max_slices, start=body.start, end=body.end,
        )
    return [s.as_dict() for s in slices]


@router.post("/samples/{sample_id}/chop")
async def chop_preview(request: Request, sample_id: int, body: ChopIn) -> dict:
    """Where the cuts would fall. Nothing is written to disk."""
    row, path = require_audio(request, sample_id)
    try:
        slices = await asyncio.to_thread(_run_chop, path, body, row.get("bpm"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"mode": body.mode, "count": len(slices), "slices": slices}


@router.post("/samples/{sample_id}/chop/export")
async def chop_export(request: Request, sample_id: int, body: ChopExportIn) -> dict:
    """Write the slices out as WAVs — a drum-rack-ready kit."""
    app_state = state(request)
    row, path = require_audio(request, sample_id)
    settings = app_state.settings

    try:
        slices = await asyncio.to_thread(_run_chop, path, body, row.get("bpm"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not slices:
        raise HTTPException(400, "No slices found — try raising the sensitivity.")

    def write_all() -> list[dict]:
        y, sr = CACHE.load(path)
        stem = _nice_name(row)
        out_dir = settings.slices_dir / f"{sample_id:05d}-{stem}"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        written = []
        for entry in slices:
            seg = chopper.take(y, sr, entry["start_sec"], entry["end_sec"])
            name = f"{stem}-{entry['idx']:02d}.wav"
            target = chopper.write_wav(out_dir / name, seg, sr)
            if body.to_export_dir and settings.export_dir:
                shutil.copy2(target, Path(settings.export_dir) / name)
            written.append({**entry, "file_path": str(target), "filename": name})
        return written

    written = await asyncio.to_thread(write_all)

    db = app_state.db
    db.execute("DELETE FROM slices WHERE sample_id=?", (sample_id,))
    now = time.time()
    out = []
    for entry in written:
        cur = db.execute(
            "INSERT INTO slices(sample_id, idx, start_sec, end_sec, file_path, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (sample_id, entry["idx"], entry["start_sec"], entry["end_sec"],
             entry["file_path"], now),
        )
        out.append({**entry, "id": cur.lastrowid,
                    "url": f"/api/slices/{cur.lastrowid}/file"})
    return {"count": len(out), "slices": out,
            "exported_to_daw": bool(body.to_export_dir and settings.export_dir)}


@router.get("/slices/{slice_id}/file")
async def slice_file(request: Request, slice_id: int):
    app_state = state(request)
    row = app_state.db.one("SELECT * FROM slices WHERE id=?", (slice_id,))
    if not row or not row.get("file_path"):
        raise HTTPException(404, f"No slice {slice_id}")
    path = safe_library_path(app_state.settings, row["file_path"])
    if not path.is_file():
        raise HTTPException(410, "Slice file is gone — re-export the kit.")
    return _serve(path, path.name, attachment=True)


# -- loops --------------------------------------------------------------
@router.post("/samples/{sample_id}/loop")
async def render_loop(request: Request, sample_id: int, body: LoopIn) -> dict:
    """Cut a loop, optionally varispeeding it to sit at your project tempo."""
    app_state = state(request)
    row, path = require_audio(request, sample_id)
    settings = app_state.settings
    if body.end <= body.start:
        raise HTTPException(400, "Loop end must come after loop start")

    source_bpm = body.source_bpm or row.get("bpm")

    def render() -> tuple[Path, dict]:
        y, sr = CACHE.load(path)
        seg, info = chopper.render_loop(
            y, sr, body.start, body.end,
            source_bpm=source_bpm, target_bpm=body.target_bpm,
        )
        stem = library.slugify(body.name) if body.name else _nice_name(row)
        bpm_tag = f"-{round(body.target_bpm or source_bpm or 0)}bpm" if (
            body.target_bpm or source_bpm) else ""
        name = f"{stem}{bpm_tag}-{int(body.start)}s.wav"
        target = chopper.write_wav(settings.loops_dir / name, seg, sr)
        if body.to_export_dir and settings.export_dir:
            shutil.copy2(target, Path(settings.export_dir) / name)
        return target, info

    target, info = await asyncio.to_thread(render)
    return {
        "filename": target.name,
        "url": f"/api/renders/{quote(target.name)}",
        "path": str(target),
        "exported_to_daw": bool(body.to_export_dir and settings.export_dir),
        **info,
    }


@router.get("/renders/{filename}")
async def render_file(request: Request, filename: str):
    settings = state(request).settings
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(400, "Bad filename")
    path = safe_library_path(settings, settings.loops_dir / filename)
    if not path.is_file():
        raise HTTPException(404, f"No render called {filename}")
    return _serve(path, filename, attachment=True)


# -- getting it into the DAW -------------------------------------------
@router.post("/samples/{sample_id}/export")
async def export_to_daw(request: Request, sample_id: int) -> dict:
    """Drop the original into the folder your DAW browser watches."""
    app_state = state(request)
    settings = app_state.settings
    if not settings.export_dir:
        raise HTTPException(
            409,
            "No export folder configured. Set CRATE_EXPORT_DIR to a folder your "
            "DAW browser watches.",
        )
    row, path = require_audio(request, sample_id)
    target = Path(settings.export_dir) / f"{_nice_name(row)}{path.suffix}"
    await asyncio.to_thread(shutil.copy2, path, target)
    return {"exported": str(target)}


@router.get("/dragout/{sample_id}")
async def dragout_manifest(
    request: Request,
    sample_id: int,
    start: float = Query(default=0.0, ge=0),
    end: float | None = None,
) -> dict:
    """What the browser needs to hand a file to the OS on drag.

    Chromium's ``DownloadURL`` drag type wants ``mime:filename:url``; this
    endpoint assembles that string so a drag straight into Ableton, FL or Logic
    lands a real file.
    """
    row, path = require_audio(request, sample_id)
    if end and end > start:
        loop = await render_loop(
            request, sample_id,
            LoopIn(start=start, end=end, source_bpm=row.get("bpm")),
        )
        url = loop["url"]
        filename = loop["filename"]
        mime = "audio/wav"
    else:
        url = f"/api/samples/{sample_id}/file?download=true"
        filename = f"{_nice_name(row)}{path.suffix}"
        mime = _mime(path)
    base = str(request.base_url).rstrip("/")
    return {
        "filename": filename,
        "mime": mime,
        "url": f"{base}{url}",
        "download_url": f"{mime}:{filename}:{base}{url}",
    }
