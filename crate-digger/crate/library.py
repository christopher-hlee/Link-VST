"""Getting a record off the web and into your crate.

Download -> analyse -> file it. Everything lands in your library folder as a
real file on disk, because a sample you can't find in Finder isn't a sample.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from .audio import decode, dsp
from .db import Database
from .sources.base import Lead, SourceError

#: Tempo/key on a long file is dominated by the middle. Analysing a window
#: keeps a 40-minute radio transcription from taking 40 seconds.
ANALYSIS_WINDOW_SEC = 120.0
PEAK_BUCKETS = 1600

AUDIO_CONTENT_TYPES = ("audio/", "application/ogg", "application/octet-stream", "video/")


def slugify(text: str, *, max_length: int = 60) -> str:
    """Filesystem-safe, readable, stable."""
    normalised = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^\w\s-]", "", ascii_text).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", cleaned).strip("-")
    return (slug[:max_length].rstrip("-")) or "untitled"


def extension_for(url: str, content_type: str = "") -> str:
    match = re.search(r"\.([A-Za-z0-9]{2,5})(?:\?|$)", url or "")
    if match and match.group(1).lower() in {
        "mp3", "flac", "ogg", "oga", "wav", "aiff", "aif", "m4a", "opus", "webm"
    }:
        return "." + match.group(1).lower()
    for token, ext in (
        ("flac", ".flac"), ("mpeg", ".mp3"), ("mp3", ".mp3"), ("ogg", ".ogg"),
        ("wav", ".wav"), ("aiff", ".aiff"), ("mp4", ".m4a"), ("webm", ".webm"),
    ):
        if token in (content_type or "").lower():
            return ext
    return ".mp3"


class DownloadError(RuntimeError):
    pass


async def download(
    client: httpx.AsyncClient,
    url: str,
    dest_dir: Path,
    *,
    stem: str,
    max_mb: int = 120,
) -> Path:
    """Stream a URL to disk, refusing anything oversized or non-audio."""
    if not url:
        raise DownloadError("No stream URL for this lead")
    dest_dir.mkdir(parents=True, exist_ok=True)
    limit = max_mb * 1024 * 1024

    async with client.stream("GET", url) as resp:
        if resp.status_code >= 400:
            raise DownloadError(f"{resp.status_code} fetching {url}")
        content_type = resp.headers.get("content-type", "")
        if content_type and not content_type.lower().startswith(AUDIO_CONTENT_TYPES):
            raise DownloadError(f"Not audio (content-type: {content_type})")
        declared = int(resp.headers.get("content-length") or 0)
        if declared and declared > limit:
            raise DownloadError(
                f"File is {declared / 1e6:.0f} MB, over the {max_mb} MB limit"
            )

        path = dest_dir / f"{stem}{extension_for(url, content_type)}"
        tmp = path.with_suffix(path.suffix + ".part")
        written = 0
        try:
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=1 << 16):
                    written += len(chunk)
                    if written > limit:
                        raise DownloadError(f"Exceeded the {max_mb} MB limit mid-stream")
                    fh.write(chunk)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    if written == 0:
        tmp.unlink(missing_ok=True)
        raise DownloadError("Empty response")
    tmp.replace(path)
    return path


def analyze_file(path: Path | str) -> dict[str, Any]:
    """Everything we can learn about a file without asking anyone."""
    path = Path(path)
    info = decode.probe(path)
    y, sr = decode.load(path, sr=decode.ANALYSIS_SR, mono=True)

    result: dict[str, Any] = {
        "duration": round(info.duration, 3),
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "bytes": path.stat().st_size,
        "ext": path.suffix.lstrip("."),
        "peaks": dsp.peaks(y, PEAK_BUCKETS),
        "loudness_db": dsp.loudness_db(y),
    }

    window = _centre_window(y, sr, ANALYSIS_WINDOW_SEC)
    tempo = dsp.estimate_tempo(window, sr)
    key = dsp.estimate_key(window, sr)
    result.update(
        bpm=tempo.bpm or None,
        bpm_confidence=tempo.confidence,
        musical_key=key.key or None,
        key_confidence=key.confidence,
        breakiness=dsp.breakiness(window),
    )
    return result


def _centre_window(y: np.ndarray, sr: int, seconds: float) -> np.ndarray:
    span = int(seconds * sr)
    if len(y) <= span:
        return y
    start = (len(y) - span) // 2
    return y[start : start + span]


def sample_dir(library_audio_dir: Path, source: str, source_id: str) -> Path:
    return library_audio_dir / source / slugify(source_id, max_length=80)


async def ingest_lead(
    db: Database,
    client: httpx.AsyncClient,
    lead: Lead,
    *,
    audio_dir: Path,
    max_mb: int = 120,
    analyse: bool = True,
) -> dict[str, Any]:
    """Pull a lead down, analyse it, and file it. Idempotent per (source, id)."""
    row = lead.as_sample_row()
    sample_id = db.upsert_sample(row)
    if not sample_id:
        raise SourceError(f"Could not file lead {lead.source}:{lead.source_id}")

    existing = db.get_sample(sample_id)
    if existing and existing.get("status") == "ready" and existing.get("file_path"):
        if Path(existing["file_path"]).exists():
            return existing

    if not lead.stream_url:
        # A lead without audio (YouTube, Discogs) is still worth keeping.
        db.update_sample(sample_id, status="lead")
        return db.get_sample(sample_id) or {}

    db.update_sample(sample_id, status="downloading", error=None)
    dest = sample_dir(audio_dir, lead.source, lead.source_id)
    stem = slugify(lead.title or lead.source_id)
    try:
        path = await download(
            client, lead.stream_url, dest, stem=stem, max_mb=max_mb
        )
    except (DownloadError, httpx.HTTPError) as exc:
        db.update_sample(sample_id, status="error", error=str(exc))
        return db.get_sample(sample_id) or {}

    db.update_sample(sample_id, file_path=str(path), status="analyzing")
    if analyse:
        try:
            db.update_sample(sample_id, **analyze_file(path), status="ready", error=None)
        except Exception as exc:  # a corrupt transfer shouldn't lose the file
            db.update_sample(
                sample_id, status="error", error=f"Analysis failed: {exc}"
            )
    else:
        db.update_sample(sample_id, status="ready")
    return db.get_sample(sample_id) or {}


def import_local(
    db: Database, path: Path | str, *, audio_dir: Path, copy: bool = True
) -> dict[str, Any]:
    """Bring a file you already own into the crate (your own rips, your own records)."""
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(src)

    source_id = str(src)
    sample_id = db.upsert_sample(
        {
            "source": "local",
            "source_id": source_id,
            "title": src.stem,
            "license": "Local file",
            "page_url": src.as_uri(),
            "status": "analyzing",
        }
    )

    target = src
    if copy:
        dest = sample_dir(audio_dir, "local", slugify(src.stem))
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / src.name
        if not target.exists():
            shutil.copy2(src, target)

    db.update_sample(sample_id, file_path=str(target))
    try:
        db.update_sample(sample_id, **analyze_file(target), status="ready", error=None)
    except Exception as exc:
        db.update_sample(sample_id, status="error", error=f"Analysis failed: {exc}")
    return db.get_sample(sample_id) or {}
