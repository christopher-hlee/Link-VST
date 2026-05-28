"""Build a taste profile from stored MIDI feature rows."""
from collections import Counter
from .midi_schema import MidiFeatures, TasteProfile


def build_profile(features_rows: list[dict], exemplar_notes: list[dict]) -> TasteProfile:
    """
    features_rows: list of dicts matching MidiFeatures fields (from DB)
    exemplar_notes: up to 5 condensed note arrays for few-shot prompting
    """
    if not features_rows:
        return TasteProfile(
            preferred_keys=["C"],
            preferred_modes=["major"],
            avg_note_density=1.0,
            avg_pitch_range=12,
            avg_interval=2.0,
            preferred_phrase_types=["melody"],
            avg_chord_complexity=0.2,
            preferred_contours=["arch"],
            exemplar_phrases=[],
            total_uploads=0,
        )

    n = len(features_rows)

    def avg(field: str) -> float:
        return sum(r[field] for r in features_rows) / n

    def top_n(field: str, count: int = 3) -> list[str]:
        return [v for v, _ in Counter(r[field] for r in features_rows).most_common(count)]

    return TasteProfile(
        preferred_keys=top_n("key"),
        preferred_modes=top_n("mode"),
        avg_note_density=round(avg("note_density"), 3),
        avg_pitch_range=round(avg("pitch_range")),
        avg_interval=round(avg("avg_interval"), 2),
        preferred_phrase_types=top_n("phrase_type"),
        avg_chord_complexity=round(avg("chord_complexity"), 3),
        preferred_contours=top_n("contour"),
        exemplar_phrases=exemplar_notes[-5:],  # most recent 5
        total_uploads=n,
    )
