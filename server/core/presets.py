"""Built-in style presets — each pre-fills generation params and a Claude hint."""

PRESETS: list[dict] = [
    {
        "id":          "jazz",
        "name":        "Jazz",
        "emoji":       "🎷",
        "phrase_type": "chord_progression",
        "key":         "F",
        "mode":        "dorian",
        "bars":        4,
        "hint": (
            "Jazz vocabulary: ii-V-I progressions, extended chords (maj7, min7, dom9, 13ths), "
            "chromatic voice-leading, tritone substitutions, syncopated comping rhythm"
        ),
    },
    {
        "id":          "lo-fi",
        "name":        "Lo-fi Hip Hop",
        "emoji":       "🎧",
        "phrase_type": "chord_progression",
        "key":         "F",
        "mode":        "major",
        "bars":        4,
        "hint": (
            "Lo-fi hip hop: warm major 7ths and minor 7ths, lazy unrushed feel, "
            "nostalgic chord colours, borrowed chords, 75-90 bpm"
        ),
    },
    {
        "id":          "cinematic",
        "name":        "Cinematic",
        "emoji":       "🎬",
        "phrase_type": "melody",
        "key":         "D",
        "mode":        "minor",
        "bars":        8,
        "hint": (
            "Cinematic orchestral: sweeping melody with a clear emotional arc, "
            "large interval leaps for drama, tension and release, dynamic contrast"
        ),
    },
    {
        "id":          "dark-ambient",
        "name":        "Dark Ambient",
        "emoji":       "🌑",
        "phrase_type": "chord_progression",
        "key":         "C",
        "mode":        "phrygian",
        "bars":        8,
        "hint": (
            "Dark ambient: slow-moving dissonant clusters, hollow sustained drones, "
            "tritones and minor 2nds, sparse and unsettling, very low note density"
        ),
    },
    {
        "id":          "pop",
        "name":        "Pop",
        "emoji":       "✨",
        "phrase_type": "melody",
        "key":         "G",
        "mode":        "major",
        "bars":        4,
        "hint": (
            "Catchy pop melody: instantly singable, mostly stepwise motion with occasional "
            "leaps, clear 4-bar phrase structure, hook-first thinking"
        ),
    },
    {
        "id":          "blues",
        "name":        "Blues",
        "emoji":       "🎸",
        "phrase_type": "melody",
        "key":         "A",
        "mode":        "minor",
        "bars":        4,
        "hint": (
            "Blues: blue notes (b3, b5, b7), call and response phrasing, "
            "expressive note bends implied, pentatonic vocabulary with chromatic passing tones"
        ),
    },
    {
        "id":          "funk",
        "name":        "Funk",
        "emoji":       "🕺",
        "phrase_type": "bassline",
        "key":         "E",
        "mode":        "minor",
        "bars":        4,
        "hint": (
            "Funk bassline: rhythmically tight, heavily syncopated, ghost notes, "
            "locked to the one, octave jumps, minimal pitch range"
        ),
    },
    {
        "id":          "dream-arp",
        "name":        "Dream Arp",
        "emoji":       "🌊",
        "phrase_type": "arpeggio",
        "key":         "D",
        "mode":        "lydian",
        "bars":        4,
        "hint": (
            "Dreamy lydian arpeggio: floating and uplifting, fast 16th notes, "
            "wide voicing spanning 2+ octaves, ethereal shimmering feel"
        ),
    },
    {
        "id":          "classical",
        "name":        "Classical",
        "emoji":       "🎻",
        "phrase_type": "melody",
        "key":         "C",
        "mode":        "major",
        "bars":        8,
        "hint": (
            "Classical period melody: clear periodic phrasing, balanced antecedent-consequent, "
            "ornamentation (turns, appoggiaturas), voice-leading by step"
        ),
    },
]

PRESET_MAP: dict[str, dict] = {p["id"]: p for p in PRESETS}
