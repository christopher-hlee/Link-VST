"""Discogs — crate leads, not audio.

Discogs holds no audio, but it knows which label put out which record in which
year, and what everyone else calls the style. That's how you decide what to go
looking for on the Archive: "Blue Note, 1969, soul-jazz" is a better starting
point than a keyword.

Needs a free personal access token; without one this source stays dormant.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Lead, SourceError

SEARCH_URL = "https://api.discogs.com/database/search"


class Discogs:
    name = "discogs"
    label = "Discogs (leads only, no audio)"
    streamable = False

    def __init__(self, client: httpx.AsyncClient, *, token: str | None = None):
        self.client = client
        self.token = token

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def search(
        self,
        query: str = "",
        *,
        style: str | None = None,
        genre: str | None = None,
        year: str | None = None,
        label: str | None = None,
        country: str | None = None,
        rows: int = 40,
        page: int = 1,
        **_: Any,
    ) -> list[Lead]:
        if not self.token:
            raise SourceError(
                "Discogs needs a token. Create one at discogs.com/settings/developers "
                "and set CRATE_DISCOGS_TOKEN."
            )
        params = {
            k: v
            for k, v in {
                "q": query or None, "style": style, "genre": genre, "year": year,
                "label": label, "country": country, "type": "release",
                "per_page": max(1, min(rows, 100)), "page": max(1, page),
            }.items()
            if v
        }
        try:
            resp = await self.client.get(
                SEARCH_URL, params=params,
                headers={"Authorization": f"Discogs token={self.token}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise SourceError(f"Discogs search failed: {exc}") from exc
        except ValueError as exc:
            raise SourceError("Discogs returned a non-JSON response") from exc

        return [self._to_lead(r) for r in payload.get("results") or []]

    @staticmethod
    def _to_lead(item: dict[str, Any]) -> Lead:
        title = str(item.get("title") or "")
        artist, _, name = title.partition(" - ")
        labels = item.get("label") or []
        if isinstance(labels, str):
            labels = [labels]
        styles = item.get("style") or []
        genres = item.get("genre") or []
        year = item.get("year")
        try:
            year_int = int(str(year)[:4]) if year else None
        except ValueError:
            year_int = None
        return Lead(
            source="discogs",
            source_id=str(item.get("id") or ""),
            title=(name or title).strip(),
            artist=artist.strip() if name else "",
            label=", ".join(str(x) for x in labels[:2]),
            year=year_int,
            genre=", ".join(str(x) for x in (styles or genres)[:4]),
            page_url="https://www.discogs.com" + str(item.get("uri") or ""),
            extra={
                "thumb": item.get("thumb"),
                "country": item.get("country"),
                "formats": item.get("format"),
                #: Feed this straight back into an Archive search.
                "search_hint": f"{artist} {name}".strip() or title,
            },
        )
