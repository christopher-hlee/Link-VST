"""Render a GeneratedPhrase to MIDI bytes."""
import io
import mido
from .midi_schema import GeneratedPhrase


def phrase_to_midi(phrase: GeneratedPhrase) -> bytes:
    ticks_per_beat = 480
    tempo = mido.bpm2tempo(phrase.tempo_bpm)

    mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    mid.tracks.append(track)

    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    track.append(mido.MetaMessage("track_name", name=phrase.description[:30], time=0))

    # Build absolute-tick events then convert to delta-tick
    events: list[tuple[int, str, int, int]] = []  # (abs_tick, type, pitch, velocity)
    for note in phrase.notes:
        start = int(note.start_beat * ticks_per_beat)
        end = int((note.start_beat + note.duration_beats) * ticks_per_beat)
        events.append((start, "note_on", note.pitch, note.velocity))
        events.append((end, "note_off", note.pitch, 0))

    events.sort(key=lambda e: (e[0], 0 if e[1] == "note_off" else 1))

    prev_tick = 0
    for abs_tick, msg_type, pitch, velocity in events:
        delta = abs_tick - prev_tick
        track.append(mido.Message(msg_type, note=pitch, velocity=velocity, time=delta))
        prev_tick = abs_tick

    track.append(mido.MetaMessage("end_of_track", time=0))

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue()
