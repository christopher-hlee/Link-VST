from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from crate.config import Settings
from crate.db import Database


def make_audio(
    bpm: float = 90.0, seconds: float = 12.0, sr: int = 22050, tone_hz: float = 220.0
) -> tuple[np.ndarray, int]:
    """A drum-ish click track over a drone — enough structure to analyse."""
    t = np.arange(int(sr * seconds)) / sr
    y = 0.25 * np.sin(2 * np.pi * tone_hz * t)
    step = 60.0 / bpm
    for i in range(int(seconds / step)):
        start = int(i * step * sr)
        n = int(0.04 * sr)
        hit = np.random.RandomState(i).randn(n) * np.exp(-np.linspace(0, 6, n))
        y[start : start + n] += hit * 0.7
    return y.astype(np.float32), sr


@pytest.fixture
def audio_file(tmp_path):
    y, sr = make_audio()
    path = tmp_path / "record.flac"
    sf.write(path, y, sr)
    return path


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        library_dir=tmp_path / "library",
        export_dir=tmp_path / "daw",
        max_download_mb=2,
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings) -> Database:
    database = Database(settings.db_path)
    yield database
    database.close()


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from crate.server.app import create_app

    with TestClient(create_app(settings)) as c:
        yield c
