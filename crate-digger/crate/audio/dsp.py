"""Analysis primitives: onsets, tempo, key, loudness, harmonic/percussive split.

Pure numpy + scipy. No librosa, no numba, no compile step — it installs
anywhere and runs fast enough to analyse a 3-minute record in well under a
second.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d

HOP = 512
N_FFT = 2048
#: Onset-envelope detrending window, in seconds.
DETREND_SEC = 4.0

# Krumhansl-Kessler tonal hierarchy profiles.
MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)
PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
#: Camelot wheel codes, for DJ-style harmonic matching of loops.
CAMELOT = {
    ("C", "major"): "8B", ("C#", "major"): "3B", ("D", "major"): "10B",
    ("D#", "major"): "5B", ("E", "major"): "12B", ("F", "major"): "7B",
    ("F#", "major"): "2B", ("G", "major"): "9B", ("G#", "major"): "4B",
    ("A", "major"): "11B", ("A#", "major"): "6B", ("B", "major"): "1B",
    ("C", "minor"): "5A", ("C#", "minor"): "12A", ("D", "minor"): "7A",
    ("D#", "minor"): "2A", ("E", "minor"): "9A", ("F", "minor"): "4A",
    ("F#", "minor"): "11A", ("G", "minor"): "6A", ("G#", "minor"): "1A",
    ("A", "minor"): "8A", ("A#", "minor"): "3A", ("B", "minor"): "10A",
}


def stft_magnitude(
    y: np.ndarray, n_fft: int = N_FFT, hop: int = HOP, center: bool = True
) -> np.ndarray:
    """Magnitude spectrogram, shape (bins, frames).

    ``center`` pads by half a window so frame *k* is centred on sample
    ``k * hop`` — without it every onset reads about one window early.
    """
    if center:
        y = np.pad(y, (n_fft // 2, n_fft // 2), mode="constant")
    if len(y) < n_fft:
        y = np.pad(y, (0, n_fft - len(y)))
    window = np.hanning(n_fft).astype(np.float32)
    n_frames = 1 + (len(y) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = y[idx] * window
    return np.abs(np.fft.rfft(frames, axis=1)).T.astype(np.float32)


def onset_envelope(y: np.ndarray, sr: int, hop: int = HOP) -> np.ndarray:
    """Half-wave-rectified log-magnitude spectral flux."""
    spec = stft_magnitude(y, hop=hop)
    log_spec = np.log1p(spec * 1000.0)
    flux = np.diff(log_spec, axis=1, prepend=log_spec[:, :1])
    env = np.maximum(flux, 0.0).sum(axis=0)
    if env.size and env.max() > 0:
        env = env / env.max()
    # Remove slow drift so autocorrelation sees rhythm, not loudness shape.
    # The window must sit well outside the lag range we search (0.3-1.0 s),
    # or it stamps its own period onto the autocorrelation.
    drift = max(3, int(DETREND_SEC * sr / hop))
    env = env - uniform_filter1d(env, size=drift, mode="nearest")
    return np.maximum(env, 0.0)


@dataclass
class TempoEstimate:
    bpm: float
    confidence: float
    candidates: list[tuple[float, float]] = field(default_factory=list)


def estimate_tempo(
    y: np.ndarray,
    sr: int,
    *,
    min_bpm: float = 60.0,
    max_bpm: float = 190.0,
    prior_bpm: float = 100.0,
    hop: int = HOP,
) -> TempoEstimate:
    """Autocorrelation tempo estimate with a log-normal prior.

    The prior stops the classic octave error (reporting 176 for a 88 BPM
    record) without hard-coding a range.
    """
    env = onset_envelope(y, sr, hop=hop)
    if env.size < 16 or not np.any(env):
        return TempoEstimate(0.0, 0.0, [])

    frame_rate = sr / hop
    n = int(2 ** np.ceil(np.log2(len(env) * 2)))
    spec = np.fft.rfft(env - env.mean(), n=n)
    acf = np.fft.irfft(spec * np.conj(spec), n=n)[: len(env)]
    if acf[0] > 0:
        acf = acf / acf[0]

    min_lag = max(1, int(frame_rate * 60.0 / max_bpm))
    max_lag = min(len(acf) - 1, int(frame_rate * 60.0 / min_bpm))
    if max_lag <= min_lag:
        return TempoEstimate(0.0, 0.0, [])

    lags = np.arange(min_lag, max_lag + 1)
    bpms = 60.0 * frame_rate / lags
    prior = np.exp(-0.5 * (np.log2(bpms / prior_bpm) / 0.9) ** 2)
    score = np.maximum(acf[lags], 0.0) * prior

    if not np.any(score):
        return TempoEstimate(0.0, 0.0, [])

    order = np.argsort(score)[::-1]
    top = [(round(float(bpms[i]), 2), float(score[i])) for i in order[:5]]

    # Refine the winning lag by fitting a parabola to its two neighbours; the
    # frame grid is coarse enough that the raw peak can be a full BPM out.
    best_i = int(order[0])
    best_lag = float(lags[best_i])
    if 0 < best_i < len(score) - 1:
        a, b, c = float(score[best_i - 1]), float(score[best_i]), float(score[best_i + 1])
        denom = a - 2 * b + c
        if abs(denom) > 1e-12:
            best_lag += np.clip(0.5 * (a - c) / denom, -0.5, 0.5)
    best_bpm = 60.0 * frame_rate / best_lag
    best_score = top[0][1]

    # Half/double-time correction. Autocorrelation is just as happy at twice
    # or half the true period, so when a related tempo is nearly as well
    # supported we take the one the prior likes best.
    def raw_at(bpm_value: float) -> float:
        lag = int(round(frame_rate * 60.0 / bpm_value))
        if lag < 1 or lag >= len(acf):
            return -1.0
        return float(np.max(acf[max(1, lag - 1) : lag + 2]))

    best_raw = raw_at(best_bpm)
    for factor in (0.5, 2.0):
        alt = best_bpm * factor
        if not (min_bpm <= alt <= max_bpm):
            continue
        alt_raw = raw_at(alt)
        if alt_raw < 0.5 * best_raw:
            continue
        prior_of = lambda b: np.exp(-0.5 * (np.log2(b / prior_bpm) / 0.9) ** 2)
        if prior_of(alt) > prior_of(best_bpm):
            best_bpm = alt

    best_bpm = round(best_bpm, 2)
    top[0] = (best_bpm, best_score)
    total = float(score.sum()) or 1.0
    confidence = float(min(1.0, best_score / (total / len(score)) / 12.0))
    return TempoEstimate(best_bpm, round(confidence, 3), top)


def chroma(y: np.ndarray, sr: int, hop: int = HOP) -> np.ndarray:
    """12-bin pitch-class energy, averaged over the file."""
    spec = stft_magnitude(y, hop=hop)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    with np.errstate(divide="ignore", invalid="ignore"):
        midi = 69 + 12 * np.log2(np.where(freqs > 0, freqs, np.nan) / 440.0)
    valid = np.isfinite(midi) & (midi >= 24) & (midi <= 108)  # C1..C8
    pc = np.zeros(12, dtype=np.float64)
    weights = spec[valid].mean(axis=1)
    classes = (np.round(midi[valid]).astype(int) % 12)
    np.add.at(pc, classes, weights)
    if pc.max() > 0:
        pc = pc / pc.max()
    return pc


@dataclass
class KeyEstimate:
    key: str            # e.g. "F# minor"
    tonic: str
    mode: str
    camelot: str
    confidence: float


def estimate_key(y: np.ndarray, sr: int) -> KeyEstimate:
    pc = chroma(y, sr)
    if not np.any(pc):
        return KeyEstimate("", "", "", "", 0.0)

    scores: list[tuple[float, str, str]] = []
    for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
        prof = (profile - profile.mean()) / profile.std()
        vec = (pc - pc.mean()) / (pc.std() or 1.0)
        for shift in range(12):
            corr = float(np.dot(vec, np.roll(prof, shift)) / 12.0)
            scores.append((corr, PITCH_NAMES[shift], mode))

    scores.sort(reverse=True)
    best, runner = scores[0], scores[1]
    spread = max(1e-6, best[0] - runner[0])
    confidence = float(min(1.0, max(0.0, best[0]) * 0.7 + spread * 2.0))
    tonic, mode = best[1], best[2]
    return KeyEstimate(
        key=f"{tonic} {mode}",
        tonic=tonic,
        mode=mode,
        camelot=CAMELOT.get((tonic, mode), ""),
        confidence=round(confidence, 3),
    )


def hpss(y: np.ndarray, *, kernel: int = 17) -> tuple[np.ndarray, np.ndarray]:
    """Median-filter harmonic/percussive separation on the magnitude spectrum."""
    spec = stft_magnitude(y)
    harmonic = median_filter(spec, size=(1, kernel), mode="nearest")
    percussive = median_filter(spec, size=(kernel, 1), mode="nearest")
    total = harmonic**2 + percussive**2 + 1e-12
    return (harmonic**2 / total) * spec, (percussive**2 / total) * spec


def breakiness(y: np.ndarray) -> float:
    """0..1 — how drum-forward this audio is.

    High values mean transient energy dominates: breaks, drum solos, the four
    bars before the horns come in. Low values mean sustained tone: strings,
    Rhodes, an upright bass walking on its own.
    """
    if len(y) < N_FFT * 2:
        return 0.0
    harm, perc = hpss(y)
    p, h = float(np.sum(perc**2)), float(np.sum(harm**2))
    if p + h <= 0:
        return 0.0
    return round(float(p / (p + h)), 4)


def loudness_db(y: np.ndarray) -> float:
    """RMS level in dBFS."""
    if not len(y):
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(y, dtype=np.float64))))
    return round(float(20 * np.log10(max(rms, 1e-9))), 2)


def detect_onsets(
    y: np.ndarray,
    sr: int,
    *,
    sensitivity: float = 1.0,
    min_gap_sec: float = 0.06,
    hop: int = HOP,
) -> np.ndarray:
    """Onset times in seconds, via adaptive-threshold peak picking.

    ``sensitivity`` above 1 finds more (and smaller) transients; below 1 keeps
    only the obvious hits.
    """
    env = onset_envelope(y, sr, hop=hop)
    if env.size < 3:
        return np.zeros(0)

    frame_rate = sr / hop
    local_mean = uniform_filter1d(env, size=max(3, int(frame_rate * 0.25)), mode="nearest")
    delta = float(np.mean(env[env > 0]) if np.any(env > 0) else 0.0)
    threshold = local_mean + delta / max(sensitivity, 1e-3)

    peaks = (env > threshold) & (env >= np.roll(env, 1)) & (env >= np.roll(env, -1))
    idx = np.flatnonzero(peaks)
    if idx.size == 0:
        return np.zeros(0)

    min_gap = int(min_gap_sec * frame_rate)
    kept = [idx[0]]
    for i in idx[1:]:
        if i - kept[-1] >= min_gap:
            kept.append(i)
        elif env[i] > env[kept[-1]]:
            kept[-1] = i
    return np.asarray(kept, dtype=np.float64) * hop / sr


def peaks(y: np.ndarray, buckets: int = 1600) -> list[float]:
    """Downsampled |amplitude| envelope for waveform drawing."""
    if not len(y):
        return []
    buckets = max(1, min(buckets, len(y)))
    edges = np.linspace(0, len(y), buckets + 1, dtype=int)
    mag = np.abs(y)
    out = np.maximum.reduceat(mag, edges[:-1])
    top = float(out.max()) or 1.0
    return [round(float(v / top), 4) for v in out]


def refine_onsets(
    y: np.ndarray,
    sr: int,
    times: np.ndarray,
    *,
    search_sec: float = 0.04,
    backoff_sec: float = 0.004,
) -> np.ndarray:
    """Snap coarse onset times to clean cut points.

    For each onset we find the steepest short-time energy rise nearby, back off
    a few milliseconds so the attack is not clipped, then land on the closest
    zero crossing. Slices cut this way start silently instead of popping.
    """
    if not len(times) or not len(y):
        return times

    win = max(8, int(0.002 * sr))
    search = max(win, int(search_sec * sr))
    backoff = int(backoff_sec * sr)
    energy = uniform_filter1d(np.square(y.astype(np.float64)), size=win, mode="nearest")
    rise = np.diff(energy, prepend=energy[:1])

    out = []
    for t in times:
        centre = int(round(t * sr))
        lo, hi = max(0, centre - search), min(len(y), centre + search)
        if hi - lo < 2:
            out.append(max(0.0, float(t)))
            continue
        pos = lo + int(np.argmax(rise[lo:hi]))
        pos = max(0, pos - backoff)
        # Walk back to the nearest zero crossing (bounded, so we never drift far).
        limit = max(0, pos - win)
        while pos > limit and not (y[pos - 1] <= 0 <= y[pos] or y[pos - 1] >= 0 >= y[pos]):
            pos -= 1
        out.append(pos / sr)

    return np.asarray(sorted(dict.fromkeys(out)), dtype=np.float64)
