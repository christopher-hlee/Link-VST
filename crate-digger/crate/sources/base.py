"""Normalised shape for anything we find out on the web."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class Lead:
    """One candidate record, before you've committed it to the crate."""

    source: str
    source_id: str
    title: str = ""
    artist: str = ""
    album: str = ""
    label: str = ""
    year: int | None = None
    genre: str = ""
    license: str = ""
    license_url: str = ""
    page_url: str = ""
    #: Direct, streamable audio URL. Empty for leads you must fetch by hand.
    stream_url: str = ""
    duration: float | None = None
    #: Anything source-specific worth keeping (collection, provider, tags...).
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_sample_row(self) -> dict[str, Any]:
        """The subset that maps onto the `samples` table."""
        row = self.as_dict()
        row.pop("extra", None)
        row["notes"] = ""
        return {k: v for k, v in row.items() if v not in (None, "")} | {
            "source": self.source,
            "source_id": self.source_id,
            "title": self.title,
        }


class Source(Protocol):
    """A place to dig."""

    name: str
    label: str
    #: True when results carry a directly streamable/downloadable URL.
    streamable: bool

    async def search(self, query: str, **kwargs: Any) -> list[Lead]: ...


class SourceError(RuntimeError):
    """A source refused, rate-limited us, or returned something unusable."""
