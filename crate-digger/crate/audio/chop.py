"""Turning a record into usable pieces: loops and one-shots."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from . import decode
from . import dsp


@dataclass
class Slice:
    idx: int
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict:
        return {
            "idx": int(self.idx),
            "start_sec": round(float(self.start), 4),
            "end_sec": round(float(self.end), 4),
            "length_sec": round(float(self.length), 4),
        }


def chop_transient(
    y: np.ndarray,
    sr: int,
    *,
    sensitivity: float = 1.0,
    min_length: float = 0.05,
    max_slices: int = 64,
    start: float = 0.0,
    end: float | None = None,
) -> list[Slice]:
    """Slice at detected hits — the classic 'chop the break' move."""
    end = float(len(y) / sr if end is None else end)
    seg = y[int(start * sr) : int(end * sr)]
    if not len(seg):
        return []

    times = dsp.detect_onsets(seg, sr, sensitivity=sensitivity, min_gap_sec=min_length)
    times = dsp.refine_onsets(seg, sr, times)
    bounds = [0.0, *[t for t in times if t > min_length], float(len(seg) / sr)]
    bounds = sorted(dict.fromkeys(round(b, 6) for b in bounds))

    out: list[Slice] = []
    for i in range(len(bounds) - 1):
        if bounds[i + 1] - bounds[i] < min_length:
            continue
        out.append(Slice(len(out), start + bounds[i], start + bounds[i + 1]))
        if len(out) >= max_slices:
            break
    return out


def chop_grid(
    y: np.ndarray,
    sr: int,
    bpm: float,
    *,
    division: float = 1.0,
    start: float = 0.0,
    end: float | None = None,
    max_slices: int = 64,
) -> list[Slice]:
    """Slice on a musical grid. ``division`` is in beats (1 = 1/4 note, 4 = a bar)."""
    if bpm <= 0 or division <= 0:
        return []
    end = float(len(y) / sr if end is None else end)
    step = (60.0 / bpm) * division
    out: list[Slice] = []
    t = start
    while t + step <= end + 1e-6 and len(out) < max_slices:
        out.append(Slice(len(out), t, min(t + step, end)))
        t += step
    return out


def apply_fades(seg: np.ndarray, sr: int, fade_ms: float = 3.0) -> np.ndarray:
    """Micro fade in/out so a slice can never click at its edges."""
    n = int(sr * fade_ms / 1000.0)
    if n <= 1 or seg.shape[-1] < 2 * n:
        return seg
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out = np.array(seg, copy=True)
    out[..., :n] *= ramp
    out[..., -n:] *= ramp[::-1]
    return out


def take(
    y: np.ndarray, sr: int, start: float, end: float, *, fade_ms: float = 3.0
) -> np.ndarray:
    a, b = max(0, int(start * sr)), min(y.shape[-1], int(end * sr))
    if b <= a:
        return np.zeros(0, dtype=np.float32)
    return apply_fades(y[..., a:b], sr, fade_ms)


def bars_of(bpm: float, seconds: float, beats_per_bar: int = 4) -> float:
    """How many bars a span covers at a given tempo."""
    if bpm <= 0:
        return 0.0
    return seconds / (60.0 / bpm * beats_per_bar)


def speed_for(source_bpm: float, target_bpm: float) -> float:
    """Varispeed ratio to pull a loop to a target tempo (tape-style)."""
    if source_bpm <= 0 or target_bpm <= 0:
        return 1.0
    return target_bpm / source_bpm


def semitones_for(ratio: float) -> float:
    """Pitch shift, in semitones, that a varispeed ratio implies."""
    if ratio <= 0:
        return 0.0
    return round(12 * float(np.log2(ratio)), 2)


def render_loop(
    y: np.ndarray,
    sr: int,
    start: float,
    end: float,
    *,
    source_bpm: float | None = None,
    target_bpm: float | None = None,
    fade_ms: float = 3.0,
) -> tuple[np.ndarray, dict]:
    """Cut a loop, optionally varispeeding it to a target tempo.

    Returns the audio plus what was done to it — the pitch shift matters when
    you go to play keys over the result.
    """
    seg = take(y, sr, start, end, fade_ms=fade_ms)
    info: dict = {
        "source_bpm": source_bpm,
        "target_bpm": target_bpm,
        "speed": 1.0,
        "semitones": 0.0,
        "bars": round(bars_of(source_bpm or 0, end - start), 3),
    }
    if source_bpm and target_bpm:
        ratio = speed_for(source_bpm, target_bpm)
        if abs(ratio - 1.0) > 1e-6:
            seg = decode.change_speed(seg, ratio)
            info["speed"] = round(ratio, 6)
            info["semitones"] = semitones_for(ratio)
    return seg, info


def write_wav(path: Path | str, data: np.ndarray, sr: int, subtype: str = "PCM_24") -> Path:
    """Write a slice/loop as WAV — the format every DAW and sampler accepts."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = data.T if data.ndim > 1 else data
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    if peak > 1.0:  # varispeed interpolation can nudge past full scale
        arr = arr / peak
    sf.write(str(path), arr, sr, subtype=subtype)
    return path
