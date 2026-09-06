"""YouTube leads.

No API key and no scraping: oEmbed is a public, documented endpoint that
returns a video's title, channel and thumbnail. That's enough to file a video
in your crate with a note about the timestamp you liked.

Getting the audio itself is a separate, deliberate step — see crate/ripper.py.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .base import Lead, SourceError

OEMBED_URL = "https://www.youtube.com/oembed"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_id(url: str) -> str | None:
    """Pull the video id out of any of the URL shapes YouTube hands out."""
    text = (url or "").strip()
    if _ID_RE.match(text):
        return text
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if _ID_RE.match(candidate) else None
    if host not in {"youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        return None

    query = parse_qs(parsed.query)
    if "v" in query and _ID_RE.match(query["v"][0]):
        return query["v"][0]
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in {"embed", "v", "shorts", "live"}:
        return parts[1] if _ID_RE.match(parts[1]) else None
    return None


def parse_timestamp(url: str) -> float | None:
    """Read `?t=1h2m3s` / `?t=90` / `&start=90` into seconds."""
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    query = parse_qs(parsed.query)
    raw = (query.get("t") or query.get("start") or [None])[0]
    if raw is None and parsed.fragment.startswith("t="):
        raw = parsed.fragment[2:]
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", raw.strip(), re.I)
    if not match or not any(match.groups()):
        return None
    h, m, s = (int(g) if g else 0 for g in match.groups())
    return float(h * 3600 + m * 60 + s)


class YouTube:
    name = "youtube"
    label = "YouTube (leads)"
    #: Metadata only. The audio URL is never resolved here.
    streamable = False

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def lead_for(self, url: str) -> Lead:
        video_id = parse_video_id(url)
        if not video_id:
            raise SourceError(f"Not a YouTube URL: {url!r}")

        watch = WATCH_URL.format(video_id=video_id)
        title, author, thumb = video_id, "", ""
        try:
            resp = await self.client.get(
                OEMBED_URL, params={"url": watch, "format": "json"}
            )
            if resp.status_code == 200:
                data = resp.json()
                title = str(data.get("title") or video_id)
                author = str(data.get("author_name") or "")
                thumb = str(data.get("thumbnail_url") or "")
        except (httpx.HTTPError, ValueError):
            # A private or removed video still deserves a placeholder row.
            pass

        return Lead(
            source="youtube",
            source_id=video_id,
            title=title,
            artist=author,
            page_url=watch,
            license="Unknown — check before you release",
            extra={
                "video_id": video_id,
                "thumbnail": thumb,
                "timestamp": parse_timestamp(url),
                "original_url": url,
            },
        )

    async def search(self, query: str = "", **kwargs: Any) -> list[Lead]:
        """YouTube has no keyless search API — paste URLs instead."""
        raise SourceError(
            "YouTube search needs an API key. Paste a video URL to add it as a lead."
        )
