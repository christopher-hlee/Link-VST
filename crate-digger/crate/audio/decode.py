"""Audio loading.

libsndfile (via soundfile) reads WAV/FLAC/OGG/MP3 directly, so a plain install
needs no ffmpeg. ffmpeg is used only as a fallback for the odd container
(m4a/webm/aiff-c) that libsndfile will not open.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

ANALYSIS_SR = 22050


@dataclass(frozen=True)
class AudioInfo:
    duration: float
    sample_rate: int
    channels: int
    frames: int


def probe(path: Path | str) -> AudioInfo:
    info = sf.info(str(path))
    return AudioInfo(
        duration=float(info.duration),
        sample_rate=int(info.samplerate),
        channels=int(info.channels),
        frames=int(info.frames),
    )


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _ffmpeg_to_wav(path: Path) -> Path:
    tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn", str(tmp)],
        check=True,
    )
    return tmp


def load(
    path: Path | str,
    *,
    sr: int | None = None,
    mono: bool = True,
    offset: float = 0.0,
    duration: float | None = None,
) -> tuple[np.ndarray, int]:
    """Load audio as float32.

    Returns ``(samples, sample_rate)``. Mono output is 1-D; stereo is
    ``(channels, n)``. ``sr`` resamples (linear, good enough for analysis and
    for the tape-speed trick we use for pitching loops).
    """
    path = Path(path)
    try:
        data, file_sr = _read(path, offset, duration)
    except (sf.LibsndfileError, RuntimeError):
        if not have_ffmpeg():
            raise
        tmp = _ffmpeg_to_wav(path)
        try:
            data, file_sr = _read(tmp, offset, duration)
        finally:
            tmp.unlink(missing_ok=True)

    if data.ndim == 1:
        data = data[np.newaxis, :]
    else:
        data = data.T  # soundfile gives (frames, channels)

    if mono:
        data = data.mean(axis=0)

    if sr and sr != file_sr:
        data = resample(data, file_sr, sr)
        file_sr = sr

    return np.ascontiguousarray(data, dtype=np.float32), file_sr


def _read(path: Path, offset: float, duration: float | None):
    with sf.SoundFile(str(path)) as f:
        if offset:
            f.seek(int(offset * f.samplerate))
        frames = int(duration * f.samplerate) if duration else -1
        data = f.read(frames=frames, dtype="float32", always_2d=False)
        return data, f.samplerate


def resample(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Linear-interpolation resample.

    This is deliberately the naive kind: speeding a loop up or down shifts
    pitch and tempo together, which is exactly the varispeed/tape move behind
    the sound we're chasing.
    """
    if src_sr == dst_sr:
        return data
    ratio = dst_sr / src_sr
    if data.ndim == 1:
        n_out = int(round(len(data) * ratio))
        if n_out <= 1:
            return data[:1].copy()
        x = np.linspace(0, len(data) - 1, n_out, dtype=np.float64)
        return np.interp(x, np.arange(len(data)), data).astype(np.float32)
    return np.stack([resample(ch, src_sr, dst_sr) for ch in data])


def change_speed(data: np.ndarray, ratio: float) -> np.ndarray:
    """Varispeed: ratio > 1 plays faster and higher, < 1 slower and darker."""
    if ratio <= 0:
        raise ValueError("speed ratio must be positive")
    if abs(ratio - 1.0) < 1e-9:
        return data
    return resample(data, int(1_000_000 * ratio), 1_000_000)
