from pydantic import BaseModel, Field
from typing import Literal


class NoteEvent(BaseModel):
    pitch: int = Field(..., ge=0, le=127, description="MIDI note number")
    velocity: int = Field(..., ge=1, le=127)
    start_beat: float = Field(..., ge=0)
    duration_beats: float = Field(..., gt=0)


class GeneratedPhrase(BaseModel):
    phrase_type: Literal["chord_progression", "melody", "arpeggio", "bassline"]
    key: str  # e.g. "C", "F#"
    mode: Literal["major", "minor", "dorian", "phrygian", "lydian", "mixolydian", "locrian"]
    tempo_bpm: int = Field(..., ge=40, le=240)
    time_signature: str = Field("4/4")
    bars: int = Field(..., ge=1, le=16)
    notes: list[NoteEvent]
    description: str


class MidiFeatures(BaseModel):
    """Extracted features from an uploaded MIDI file."""
    key: str
    mode: str
    tempo_bpm: float
    time_signature: str
    bars: int
    phrase_type: str  # heuristic classification
    note_density: float        # notes per beat
    pitch_range: int           # semitones between lowest and highest
    avg_interval: float        # avg melodic interval in semitones
    rhythmic_regularity: float # 0-1, how quantized
    chord_complexity: float    # 0-1, avg notes sounding together
    contour: str               # "ascending", "descending", "arch", "valley", "static"
    dominant_pitches: list[int]  # most common pitch classes


class TasteProfile(BaseModel):
    """Aggregated preference model from all uploaded MIDI."""
    preferred_keys: list[str]
    preferred_modes: list[str]
    avg_note_density: float
    avg_pitch_range: int
    avg_interval: float
    preferred_phrase_types: list[str]
    avg_chord_complexity: float
    preferred_contours: list[str]
    exemplar_phrases: list[dict]  # condensed note arrays for few-shot
    total_uploads: int
