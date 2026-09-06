"""Openverse — one API over Free Music Archive, Jamendo, ccMixter and Wikimedia.

Everything it indexes is openly licensed, and it can be filtered down to
licences that permit both commercial use and modification, which is the set
that matters if you intend to release what you build.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Lead, SourceError

SEARCH_URL = "https://api.openverse.org/v1/audio/"


class Openverse:
    name = "openverse"
    label = "Openverse (FMA / Jamendo / ccMixter)"
    streamable = True

    def __init__(self, client: httpx.AsyncClient, *, token: str | None = None):
        self.client = client
        self.token = token

    async def search(
        self,
        query: str = "",
        *,
        rows: int = 40,
        page: int = 1,
        license_type: str = "commercial,modification",
        source: str | None = None,
        length: str | None = None,
        **_: Any,
    ) -> list[Lead]:
        params: dict[str, Any] = {
            "q": query or "instrumental",
            "page": max(1, page),
            "page_size": max(1, min(rows, 100)),
        }
        if license_type:
            params["license_type"] = license_type
        if source:
            params["source"] = source
        if length:
            params["length"] = length

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            resp = await self.client.get(SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise SourceError(f"Openverse search failed: {exc}") from exc
        except ValueError as exc:
            raise SourceError("Openverse returned a non-JSON response") from exc

        return [self._to_lead(r) for r in payload.get("results") or []]

    @staticmethod
    def _to_lead(item: dict[str, Any]) -> Lead:
        lic = str(item.get("license") or "").upper()
        version = str(item.get("license_version") or "")
        genres = item.get("genres") or []
        if isinstance(genres, str):
            genres = [genres]
        tags = [t.get("name") for t in (item.get("tags") or []) if isinstance(t, dict)]
        duration_ms = item.get("duration")
        return Lead(
            source="openverse",
            source_id=str(item.get("id") or ""),
            title=str(item.get("title") or "Untitled"),
            artist=str(item.get("creator") or ""),
            genre=", ".join(str(g) for g in (genres or tags)[:4]),
            license=f"CC {lic} {version}".strip() if lic else "",
            license_url=str(item.get("license_url") or ""),
            page_url=str(item.get("foreign_landing_url") or item.get("url") or ""),
            stream_url=str(item.get("url") or ""),
            duration=(float(duration_ms) / 1000.0) if duration_ms else None,
            extra={
                "provider": item.get("provider"),
                "source": item.get("source"),
                "tags": tags[:12],
            },
        )
