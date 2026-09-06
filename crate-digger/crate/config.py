"""Runtime configuration.

Everything is local-first: the library lives in a folder you own, the database
is a single SQLite file next to it, and nothing is uploaded anywhere.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_library() -> Path:
    return Path(os.environ.get("CRATE_LIBRARY_DIR", Path.home() / "CrateDigger"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRATE_", env_file=".env", extra="ignore"
    )

    # --- storage -------------------------------------------------------
    library_dir: Path = Field(default_factory=_default_library)
    #: Optional folder your DAW browser watches. Exported loops/chops land here.
    export_dir: Path | None = None

    # --- server --------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8770

    # --- network -------------------------------------------------------
    user_agent: str = "crate-digger/0.1 (+https://github.com/christopher-hlee/crate-digger)"
    http_timeout: float = 30.0
    max_download_mb: int = 120

    # --- optional integrations ----------------------------------------
    #: Discogs personal access token. Metadata only (no audio) — used for
    #: crate leads: labels, years, styles, "what else did this drummer play on".
    discogs_token: str | None = None
    #: Openverse works anonymously but a client id raises the rate limit.
    openverse_client_id: str | None = None
    openverse_client_secret: str | None = None

    # --- the ripper ----------------------------------------------------
    #: Off by default. When enabled, YouTube leads can be pulled down locally
    #: with yt-dlp instead of you doing it by hand. See docs/SOURCES.md.
    enable_ripper: bool = False
    ripper_bin: str = "yt-dlp"
    ripper_format: str = "bestaudio/best"

    @property
    def db_path(self) -> Path:
        return self.library_dir / "crate.sqlite3"

    @property
    def audio_dir(self) -> Path:
        return self.library_dir / "audio"

    @property
    def slices_dir(self) -> Path:
        return self.library_dir / "slices"

    @property
    def loops_dir(self) -> Path:
        return self.library_dir / "loops"

    def ensure_dirs(self) -> None:
        for d in (self.library_dir, self.audio_dir, self.slices_dir, self.loops_dir):
            d.mkdir(parents=True, exist_ok=True)
        if self.export_dir:
            Path(self.export_dir).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def reset_settings_cache() -> None:
    get_settings.cache_clear()
