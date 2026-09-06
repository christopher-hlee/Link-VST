"""Request bodies."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LeadIn(BaseModel):
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
    stream_url: str = ""
    duration: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class IngestIn(BaseModel):
    leads: list[LeadIn]
    analyse: bool = True


class VerdictIn(BaseModel):
    source: str
    source_id: str
    verdict: Literal["keep", "pass"]


class YouTubeIn(BaseModel):
    url: str
    #: Pull the audio down now with yt-dlp (needs CRATE_ENABLE_RIPPER=true).
    rip: bool = False
    note: str = ""


class ImportIn(BaseModel):
    """`copy` is the wire name; `copy_file` avoids shadowing BaseModel.copy."""

    model_config = ConfigDict(populate_by_name=True)

    path: str
    copy_file: bool = Field(default=True, alias="copy")


class SampleUpdate(BaseModel):
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    label: str | None = None
    year: int | None = None
    genre: str | None = None
    notes: str | None = None
    starred: bool | None = None
    bpm: float | None = None
    musical_key: str | None = None


class ChopIn(BaseModel):
    mode: Literal["transient", "grid"] = "transient"
    sensitivity: float = Field(default=1.0, gt=0, le=8)
    #: Grid step in beats: 1 = 1/4 note, 4 = one bar.
    division: float = Field(default=1.0, gt=0, le=64)
    bpm: float | None = None
    start: float = Field(default=0.0, ge=0)
    end: float | None = None
    min_length: float = Field(default=0.05, gt=0, le=10)
    max_slices: int = Field(default=64, ge=1, le=256)


class ChopExportIn(ChopIn):
    #: Also drop the slices in the DAW watch folder.
    to_export_dir: bool = False


class LoopIn(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    target_bpm: float | None = Field(default=None, gt=20, le=300)
    source_bpm: float | None = Field(default=None, gt=20, le=300)
    to_export_dir: bool = False
    name: str = ""


class MarkerIn(BaseModel):
    kind: Literal["loop", "hit", "note"] = "loop"
    start_sec: float = Field(ge=0)
    end_sec: float | None = None
    label: str = ""


class CrateIn(BaseModel):
    name: str
    color: str = "#c9a227"
    notes: str = ""


class CrateItemIn(BaseModel):
    sample_id: int
