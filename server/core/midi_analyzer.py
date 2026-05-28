"""Extract musical features from a MIDI file."""
import io
from collections import Counter
import mido
from .midi_schema import MidiFeatures


PITCH_CLASS_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def _correlate(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = (sum((x - ma) ** 2 for x in a)) ** 0.5
    db = (sum((x - mb) ** 2 for x in b)) ** 0.5
    return num / (da * db) if da * db else 0.0


def detect_key_mode(pitch_classes: list[int]) -> tuple[str, str]:
    counts = [0.0] * 12
    for pc in pitch_classes:
        counts[pc % 12] += 1
    total = sum(counts) or 1
    dist = [c / total for c in counts]

    best_key, best_mode, best_r = 0, "major", -2.0
    for root in range(12):
        rotated = dist[root:] + dist[:root]
        r_maj = _correlate(rotated, MAJOR_PROFILE)
        r_min = _correlate(rotated, MINOR_PROFILE)
        if r_maj > best_r:
            best_r, best_key, best_mode = r_maj, root, "major"
        if r_min > best_r:
            best_r, best_key, best_mode = r_min, root, "minor"

    return PITCH_CLASS_NAMES[best_key], best_mode


def _contour(pitches: list[int]) -> str:
    if len(pitches) < 2:
        return "static"
    mid = len(pitches) // 2
    first_half = pitches[:mid]
    second_half = pitches[mid:]
    avg_first = sum(first_half) / len(first_half)
    avg_second = sum(second_half) / len(second_half)
    overall_slope = pitches[-1] - pitches[0]
    if abs(overall_slope) < 2:
        return "static"
    if avg_second > avg_first + 1 and overall_slope > 0:
        return "ascending"
    if avg_second < avg_first - 1 and overall_slope < 0:
        return "descending"
    if pitches[mid] > avg_first + 1 and pitches[mid] > avg_second + 1:
        return "arch"
    return "valley"


def _classify_phrase_type(note_density: float, chord_complexity: float, pitch_range: int) -> str:
    if chord_complexity > 0.4:
        return "chord_progression"
    if note_density > 2.0 and pitch_range < 15:
        return "arpeggio"
    if pitch_range < 8:
        return "bassline"
    return "melody"


def analyze(midi_bytes: bytes) -> MidiFeatures:
    mid = mido.MidiFile(file=io.BytesIO(midi_bytes))
    ticks_per_beat = mid.ticks_per_beat or 480

    tempo = 500000  # default 120 bpm
    note_ons: list[tuple[float, int, int]] = []  # (beat, pitch, velocity)
    simultaneous: dict[int, float] = {}  # pitch -> start_beat

    abs_tick = 0
    beats_per_tick = 1.0 / ticks_per_beat

    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            beat = abs_tick * beats_per_tick
            if msg.type == "set_tempo":
                tempo = msg.tempo
            elif msg.type == "note_on" and msg.velocity > 0:
                note_ons.append((beat, msg.note, msg.velocity))

    if not note_ons:
        raise ValueError("No note events found in MIDI file")

    beats_total = max(b for b, _, _ in note_ons) if note_ons else 1.0
    bars = max(1, int(beats_total / 4) + 1)
    tempo_bpm = round(60_000_000 / tempo) if tempo else 120

    pitches = [p for _, p, _ in note_ons]
    pitch_classes = [p % 12 for p in pitches]
    key, mode = detect_key_mode(pitch_classes)

    note_density = len(note_ons) / max(beats_total, 1)
    pitch_range = max(pitches) - min(pitches) if pitches else 0

    intervals = [abs(pitches[i + 1] - pitches[i]) for i in range(len(pitches) - 1)]
    avg_interval = sum(intervals) / len(intervals) if intervals else 0

    # rhythmic regularity: fraction of note starts that fall on 8th-note grid
    grid = 0.5
    on_grid = sum(1 for b, _, _ in note_ons if abs(b % grid) < 0.05 or abs(b % grid - grid) < 0.05)
    rhythmic_regularity = on_grid / len(note_ons)

    # chord complexity: fraction of beats where >1 note starts within 0.1 beat
    beat_buckets: dict[int, int] = Counter(int(b * 10) for b, _, _ in note_ons)
    polyphonic = sum(1 for v in beat_buckets.values() if v > 1)
    chord_complexity = polyphonic / max(len(beat_buckets), 1)

    contour = _contour(pitches)
    phrase_type = _classify_phrase_type(note_density, chord_complexity, pitch_range)

    top_pcs = [pc for pc, _ in Counter(pitch_classes).most_common(5)]

    return MidiFeatures(
        key=key,
        mode=mode,
        tempo_bpm=float(tempo_bpm),
        time_signature="4/4",
        bars=bars,
        phrase_type=phrase_type,
        note_density=round(note_density, 3),
        pitch_range=pitch_range,
        avg_interval=round(avg_interval, 2),
        rhythmic_regularity=round(rhythmic_regularity, 3),
        chord_complexity=round(chord_complexity, 3),
        contour=contour,
        dominant_pitches=top_pcs,
    )
