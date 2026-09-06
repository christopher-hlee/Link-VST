"""Optional local audio extraction via yt-dlp.

Off unless you turn it on (``CRATE_ENABLE_RIPPER=true``). It shells out to
yt-dlp on your own machine, one URL at a time, when you ask it to — it is a
convenience wrapper around the command you would otherwise type, not a
crawler and not a bulk downloader.

What you may do with the result is between you and the rights holder. See
docs/SOURCES.md; the archives wired into this tool are the ones that come with
an answer already.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from .config import Settings


class RipperError(RuntimeError):
    pass


class RipperDisabled(RipperError):
    pass


def available(settings: Settings) -> bool:
    return bool(shutil.which(settings.ripper_bin))


def status(settings: Settings) -> dict:
    return {
        "enabled": settings.enable_ripper,
        "binary": settings.ripper_bin,
        "installed": available(settings),
    }


async def rip(settings: Settings, url: str, dest_dir: Path, *, stem: str) -> Path:
    """Extract the audio track of a single URL into ``dest_dir``."""
    if not settings.enable_ripper:
        raise RipperDisabled(
            "The ripper is off. Set CRATE_ENABLE_RIPPER=true to enable it, or "
            "download the file yourself and use Import."
        )
    if not available(settings):
        raise RipperError(
            f"{settings.ripper_bin} is not installed. `pip install yt-dlp` "
            f"or `brew install yt-dlp`."
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    template = str(dest_dir / f"{stem}.%(ext)s")
    cmd = [
        settings.ripper_bin,
        "--no-playlist",             # one URL means one track
        "--no-progress",
        "--quiet",
        "--extract-audio",
        "--audio-format", "flac",
        "--format", settings.ripper_format,
        "--print-json",
        "--no-simulate",
        "-o", template,
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RipperError(detail[-1] if detail else f"yt-dlp exited {proc.returncode}")

    # yt-dlp reports the pre-conversion path, so find what actually landed.
    produced = sorted(
        dest_dir.glob(f"{stem}.*"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    audio = [p for p in produced if p.suffix.lower() in
             {".flac", ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".webm"}]
    if not audio:
        raise RipperError("yt-dlp finished but produced no audio file")
    return audio[0]


def parse_metadata(stdout: bytes) -> dict:
    try:
        return json.loads((stdout or b"").decode("utf-8", "replace").splitlines()[0])
    except (ValueError, IndexError):
        return {}
