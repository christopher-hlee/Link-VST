"""Post-process generated phrases with swing, velocity variance, and timing jitter."""
import random
from .midi_schema import NoteEvent, GeneratedPhrase


def humanize(
    phrase: GeneratedPhrase,
    swing: float = 0.0,           # 0 = straight, 1 = full triplet swing
    velocity_variance: int = 0,   # ±random per note
    timing_variance: float = 0.0, # max random offset in beats
) -> GeneratedPhrase:
    if swing == 0.0 and velocity_variance == 0 and timing_variance == 0.0:
        return phrase

    new_notes = []
    for note in phrase.notes:
        beat     = note.start_beat
        velocity = note.velocity
        duration = note.duration_beats

        # --- Swing ---
        # Upbeat 8th notes (positions n + 0.5 for integer n) are pushed forward.
        # Full triplet swing moves them from 0.5 to 0.667 of the beat pair.
        # swing=0 → no change; swing=1 → ×(2/3 - 0.5) = +0.1667 beats delay.
        if swing > 0.0:
            # Identify upbeat 8th notes within ±0.08 beat tolerance
            eighth_units = beat * 2.0
            nearest_eighth = round(eighth_units)
            if abs(eighth_units - nearest_eighth) < 0.12 and nearest_eighth % 2 == 1:
                delay = swing * (2.0 / 3.0 - 0.5)
                beat = beat + delay

        # --- Velocity variance ---
        if velocity_variance > 0:
            v = velocity + random.randint(-velocity_variance, velocity_variance)
            velocity = max(1, min(127, v))

        # --- Timing micro-jitter ---
        if timing_variance > 0.0:
            jitter = random.uniform(-timing_variance, timing_variance)
            beat = max(0.0, beat + jitter)

        new_notes.append(NoteEvent(
            pitch=note.pitch,
            velocity=velocity,
            start_beat=round(beat, 4),
            duration_beats=duration,
        ))

    return phrase.model_copy(update={"notes": new_notes})
