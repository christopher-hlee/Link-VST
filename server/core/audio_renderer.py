"""Render MIDI bytes to WAV using FluidSynth."""
import os
import subprocess
import tempfile

SOUNDFONT = os.environ.get(
    "LINKVST_SOUNDFONT",
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
)


def render_to_wav(midi_bytes: bytes, sample_rate: int = 44100) -> bytes:
    with tempfile.TemporaryDirectory() as tmpdir:
        midi_path = os.path.join(tmpdir, "input.mid")
        wav_path  = os.path.join(tmpdir, "output.wav")

        with open(midi_path, "wb") as f:
            f.write(midi_bytes)

        result = subprocess.run(
            [
                "fluidsynth",
                "-ni",           # no interactive shell, non-realtime
                "-g", "1.0",     # gain
                "-r", str(sample_rate),
                SOUNDFONT,
                midi_path,
                "-F", wav_path,
            ],
            capture_output=True,
            timeout=30,
        )

        if result.returncode != 0 or not os.path.exists(wav_path):
            raise RuntimeError(
                f"fluidsynth error: {result.stderr.decode(errors='replace')}"
            )

        with open(wav_path, "rb") as f:
            return f.read()
