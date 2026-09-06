"""Wiring the sources together behind one HTTP client."""
from __future__ import annotations

import httpx

from ..config import Settings
from .base import Lead, Source, SourceError
from .discogs import Discogs
from .internet_archive import InternetArchive
from .openverse import Openverse
from .youtube import YouTube

__all__ = ["Registry", "Lead", "Source", "SourceError"]


class Registry:
    """Holds one shared AsyncClient and the source adapters built on it."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=settings.http_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        self.ia = InternetArchive(self.client)
        self.openverse = Openverse(self.client)
        self.youtube = YouTube(self.client)
        self.discogs = Discogs(self.client, token=settings.discogs_token)

    @property
    def sources(self) -> dict[str, Source]:
        return {
            self.ia.name: self.ia,
            self.openverse.name: self.openverse,
            self.youtube.name: self.youtube,
            self.discogs.name: self.discogs,
        }

    def get(self, name: str) -> Source:
        try:
            return self.sources[name]
        except KeyError:
            raise SourceError(f"Unknown source: {name!r}") from None

    def describe(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "label": s.label,
                "streamable": s.streamable,
                "enabled": getattr(s, "enabled", True),
            }
            for s in self.sources.values()
        ]

    async def aclose(self) -> None:
        await self.client.aclose()
