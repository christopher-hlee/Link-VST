"""Internet Archive — the deepest legal crate on the web.

The Great 78 Project alone holds hundreds of thousands of digitised 78rpm
discs: pre-war jazz, blues, gospel, big band, all of it surface noise and
saturated horns. That is the raw material this whole tool exists to reach.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .base import Lead, SourceError

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{name}"
DETAILS_URL = "https://archive.org/details/{identifier}"

FIELDS = [
    "identifier", "title", "creator", "date", "year", "collection",
    "licenseurl", "subject", "publisher", "description", "downloads",
]

#: Preference order when an item offers several encodings of the same track.
FORMAT_RANK = {
    "flac": 0, "24bit flac": 0, "vbr mp3": 1, "mp3": 2, "128kbps mp3": 2,
    "64kbps mp3": 3, "ogg vorbis": 3, "wave": 4, "aiff": 5,
}
AUDIO_EXTS = (".flac", ".mp3", ".ogg", ".wav", ".aiff", ".aif", ".m4a")
#: Archive's own low-quality previews and non-musical junk.
SKIP_PATTERNS = re.compile(r"(_sample|_spectrogram|__?preview)", re.I)


class InternetArchive:
    name = "ia"
    label = "Internet Archive"
    streamable = True

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    # -- searching ------------------------------------------------------
    @staticmethod
    def build_query(
        query: str = "",
        *,
        collections: list[str] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        subjects: list[str] | None = None,
        extra: str = "",
    ) -> str:
        parts = ["mediatype:(audio)"]
        if query.strip():
            parts.append(f"({query.strip()})")
        if collections:
            joined = " OR ".join(f'"{c}"' for c in collections)
            parts.append(f"collection:({joined})")
        if subjects:
            joined = " OR ".join(f'"{s}"' for s in subjects)
            parts.append(f"subject:({joined})")
        if year_from or year_to:
            lo = f"{year_from}-01-01" if year_from else "0001-01-01"
            hi = f"{year_to}-12-31" if year_to else "9999-12-31"
            parts.append(f"date:[{lo} TO {hi}]")
        if extra:
            parts.append(f"({extra})")
        return " AND ".join(parts)

    async def search(
        self,
        query: str = "",
        *,
        collections: list[str] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        subjects: list[str] | None = None,
        rows: int = 40,
        page: int = 1,
        sort: str = "downloads desc",
        **_: Any,
    ) -> list[Lead]:
        params: list[tuple[str, str]] = [
            ("q", self.build_query(
                query, collections=collections, year_from=year_from,
                year_to=year_to, subjects=subjects)),
            ("rows", str(max(1, min(rows, 100)))),
            ("page", str(max(1, page))),
            ("output", "json"),
        ]
        params += [("fl[]", f) for f in FIELDS]
        if sort:
            params.append(("sort[]", sort))

        try:
            resp = await self.client.get(SEARCH_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise SourceError(f"Internet Archive search failed: {exc}") from exc
        except ValueError as exc:
            raise SourceError("Internet Archive returned a non-JSON response") from exc

        docs = (payload.get("response") or {}).get("docs") or []
        return [self._to_lead(d) for d in docs]

    @staticmethod
    def _to_lead(doc: dict[str, Any]) -> Lead:
        ident = doc.get("identifier", "")
        creator = doc.get("creator") or ""
        if isinstance(creator, list):
            creator = ", ".join(str(c) for c in creator if c)
        subject = doc.get("subject") or []
        if isinstance(subject, str):
            subject = [subject]
        collection = doc.get("collection") or []
        if isinstance(collection, str):
            collection = [collection]
        return Lead(
            source="ia",
            source_id=ident,
            title=str(doc.get("title") or ident),
            artist=str(creator),
            year=_parse_year(doc.get("year") or doc.get("date")),
            genre=", ".join(str(s) for s in subject[:4]),
            label=str(doc.get("publisher") or ""),
            license_url=str(doc.get("licenseurl") or ""),
            license=_license_name(str(doc.get("licenseurl") or ""), collection),
            page_url=DETAILS_URL.format(identifier=ident),
            extra={
                "collection": collection,
                "downloads": doc.get("downloads"),
                "description": _first_str(doc.get("description")),
            },
        )

    # -- resolving an item to actual audio -------------------------------
    async def tracks(self, identifier: str) -> list[Lead]:
        """Expand an archive item into its individual playable tracks."""
        try:
            resp = await self.client.get(METADATA_URL.format(identifier=identifier))
            resp.raise_for_status()
            meta = resp.json()
        except httpx.HTTPError as exc:
            raise SourceError(f"Internet Archive metadata failed: {exc}") from exc
        except ValueError as exc:
            raise SourceError("Internet Archive metadata was not JSON") from exc

        item = meta.get("metadata") or {}
        files = meta.get("files") or []
        base = self._to_lead({**item, "identifier": identifier})

        out: list[Lead] = []
        for name, entry in pick_audio_files(files).items():
            out.append(
                Lead(
                    source="ia",
                    source_id=f"{identifier}/{name}",
                    title=str(entry.get("title") or name.rsplit(".", 1)[0]),
                    artist=str(entry.get("artist") or base.artist),
                    album=str(item.get("album") or base.title),
                    label=base.label,
                    year=base.year,
                    genre=base.genre,
                    license=base.license,
                    license_url=base.license_url,
                    page_url=base.page_url,
                    stream_url=DOWNLOAD_URL.format(identifier=identifier, name=name),
                    duration=_parse_length(entry.get("length")),
                    extra={
                        "identifier": identifier,
                        "format": entry.get("format"),
                        "size": _to_int(entry.get("size")),
                        "collection": base.extra.get("collection"),
                    },
                )
            )
        return out


def pick_audio_files(files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Choose one file per track, preferring the best available encoding.

    Archive items carry the same recording several times over (FLAC, VBR MP3,
    a 64kbps derivative, a 30-second sample). Group by the original name and
    keep the highest-ranked real file.
    """
    grouped: dict[str, tuple[int, str, dict[str, Any]]] = {}
    for entry in files:
        name = str(entry.get("name") or "")
        if not name.lower().endswith(AUDIO_EXTS) or SKIP_PATTERNS.search(name):
            continue
        if _to_int(entry.get("size")) < 32_000:  # 30s previews and stubs
            continue
        fmt = str(entry.get("format") or "").lower()
        rank = FORMAT_RANK.get(fmt, 9)
        key = str(entry.get("original") or name).rsplit(".", 1)[0]
        current = grouped.get(key)
        if current is None or rank < current[0]:
            grouped[key] = (rank, name, entry)
    return {name: entry for _, name, entry in grouped.values()}


def _parse_year(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"(1[5-9]\d{2}|20\d{2})", str(value))
    return int(match.group(1)) if match else None


def _parse_length(value: Any) -> float | None:
    """Archive lengths come as seconds or as `M:SS`."""
    if value in (None, ""):
        return None
    text = str(value)
    if ":" in text:
        try:
            parts = [float(p) for p in text.split(":")]
        except ValueError:
            return None
        total = 0.0
        for part in parts:
            total = total * 60 + part
        return total
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_str(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _license_name(license_url: str, collections: list[str]) -> str:
    url = license_url.lower()
    if "publicdomain" in url or "/zero/" in url or "mark/1.0" in url:
        return "Public Domain"
    if "creativecommons.org/licenses/" in url:
        code = url.split("/licenses/")[1].split("/")[0].upper()
        return f"CC {code}"
    # The Great 78 Project digitises recordings out of US copyright.
    if any(c in {"georgeblood", "78rpm"} for c in collections):
        return "Public Domain (78rpm transfer)"
    if "unlockedrecordings" in collections:
        return "Unlocked Recordings"
    return ""
