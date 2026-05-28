"""Generate MIDI phrases via Claude using the user's taste profile."""
import json
import os
import anthropic
from .midi_schema import GeneratedPhrase, TasteProfile

_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You are a MIDI composition engine. You generate musical phrases as structured JSON.

## Output format
Return a JSON object matching this exact schema:
{
  "phrase_type": "chord_progression"|"melody"|"arpeggio"|"bassline",
  "key": string,          // e.g. "C", "F#", "Bb"
  "mode": "major"|"minor"|"dorian"|"phrygian"|"lydian"|"mixolydian"|"locrian",
  "tempo_bpm": integer,
  "time_signature": "4/4"|"3/4"|"6/8",
  "bars": integer,
  "notes": [
    {"pitch": 60, "velocity": 80, "start_beat": 0.0, "duration_beats": 1.0},
    ...
  ],
  "description": string   // one sentence describing the phrase
}

## Rules
- pitch: MIDI note number 0-127 (middle C = 60)
- velocity: 1-127
- start_beat: 0-indexed, in beats from bar 1 beat 1
- duration_beats: note length in beats (0.25 = 16th, 0.5 = 8th, 1.0 = quarter, 2.0 = half)
- Chord progressions: voice multiple simultaneous notes (same start_beat, different pitches)
- Arpeggios: same chord tones, staggered start_beat
- Melodies: monophonic or lightly harmonized
- Keep phrases musically coherent and stylistically consistent with the user's taste profile
- Vary rhythm, dynamics, and register for interest — avoid mechanical repetition
- Apply light humanization: vary velocities ±5, allow occasional micro-timing ±0.02 beats

## Style guidance
Match the user's taste profile provided in each request. If exemplar phrases are given,
study their rhythmic density, interval choices, and contour, then write something new
that feels like it belongs in the same collection — not a copy, but a natural companion.
"""


def generate_phrase(
    profile: TasteProfile,
    phrase_type: str | None = None,
    key: str | None = None,
    mode: str | None = None,
    bars: int = 4,
    extra_hint: str = "",
) -> GeneratedPhrase:
    target_type = phrase_type or profile.preferred_phrase_types[0]
    target_key = key or profile.preferred_keys[0]
    target_mode = mode or profile.preferred_modes[0]

    exemplar_block = ""
    if profile.exemplar_phrases:
        exemplar_block = "\n\n## Exemplar phrases from user's library (study these for style):\n"
        for i, ex in enumerate(profile.exemplar_phrases, 1):
            exemplar_block += f"\nExample {i}:\n```json\n{json.dumps(ex, indent=2)}\n```"

    user_msg = f"""Generate a {bars}-bar {target_type} in {target_key} {target_mode}.

## User taste profile (from {profile.total_uploads} uploaded MIDI files):
- Preferred keys: {', '.join(profile.preferred_keys)}
- Preferred modes: {', '.join(profile.preferred_modes)}
- Preferred phrase types: {', '.join(profile.preferred_phrase_types)}
- Average note density: {profile.avg_note_density} notes/beat
- Average pitch range: {profile.avg_pitch_range} semitones
- Average melodic interval: {profile.avg_interval} semitones
- Chord complexity: {profile.avg_chord_complexity:.0%}
- Preferred melodic contours: {', '.join(profile.preferred_contours)}
{exemplar_block}
{f"Additional hint: {extra_hint}" if extra_hint else ""}

Return only the JSON object, no markdown fences, no commentary."""

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if Claude adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw)
    return GeneratedPhrase(**data)


def generate_batch(
    profile: TasteProfile,
    count: int = 4,
    variety: bool = True,
) -> list[GeneratedPhrase]:
    """Generate multiple phrases, optionally varying type/key across them."""
    results = []
    types = profile.preferred_phrase_types if variety else [profile.preferred_phrase_types[0]]
    keys = profile.preferred_keys if variety else [profile.preferred_keys[0]]

    for i in range(count):
        ptype = types[i % len(types)]
        key = keys[i % len(keys)]
        phrase = generate_phrase(profile, phrase_type=ptype, key=key)
        results.append(phrase)
    return results
