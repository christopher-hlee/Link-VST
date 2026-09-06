import numpy as np
import pytest
import soundfile as sf

from crate.audio import chop, decode

from .conftest import make_audio


def test_transient_chop_finds_the_hits():
    y, sr = make_audio(bpm=120, seconds=8)
    slices = chop.chop_transient(y, sr)
    assert 10 <= len(slices) <= 20
    assert slices[0].start == 0.0
    assert all(s.end > s.start for s in slices)
    # Slices tile the file without overlapping.
    for a, b in zip(slices, slices[1:]):
        assert b.start == pytest.approx(a.end, abs=1e-6)


def test_transient_chop_respects_min_length_and_cap():
    y, sr = make_audio(bpm=120, seconds=8)
    slices = chop.chop_transient(y, sr, min_length=1.5, max_slices=4)
    assert len(slices) <= 4
    assert all(s.length >= 1.5 - 1e-6 for s in slices)


def test_grid_chop_is_musically_spaced():
    y, sr = make_audio(seconds=8)
    slices = chop.chop_grid(y, sr, 120.0, division=4)   # one bar at 120 = 2s
    assert len(slices) == 4
    assert slices[0].length == pytest.approx(2.0)


def test_grid_chop_rejects_nonsense():
    y, sr = make_audio(seconds=4)
    assert chop.chop_grid(y, sr, 0.0) == []


def test_chop_within_a_region_only():
    y, sr = make_audio(bpm=100, seconds=10)
    slices = chop.chop_transient(y, sr, start=3.0, end=6.0)
    assert slices
    assert slices[0].start >= 3.0 - 1e-6
    assert slices[-1].end <= 6.0 + 1e-6


def test_fades_silence_the_edges():
    y, sr = make_audio(seconds=4)
    seg = chop.take(y, sr, 1.0, 2.0)
    assert abs(seg[0]) < 1e-6 and abs(seg[-1]) < 1e-6


def test_take_out_of_range_is_empty():
    y, sr = make_audio(seconds=2)
    assert chop.take(y, sr, 5.0, 6.0).size == 0


def test_loop_varispeed_matches_the_target_tempo():
    y, sr = make_audio(bpm=120, seconds=8)
    seg, info = chop.render_loop(y, sr, 0.0, 4.0, source_bpm=120.0, target_bpm=90.0)
    assert info["speed"] == pytest.approx(0.75)
    assert info["semitones"] == pytest.approx(-4.98, abs=0.02)
    # Slower means longer, in exact proportion.
    assert len(seg) / sr == pytest.approx(4.0 / 0.75, rel=0.01)


def test_loop_without_a_target_is_untouched():
    y, sr = make_audio(seconds=6)
    seg, info = chop.render_loop(y, sr, 1.0, 3.0)
    assert info["speed"] == 1.0
    assert len(seg) / sr == pytest.approx(2.0, rel=0.01)


def test_bars_and_semitone_helpers():
    assert chop.bars_of(90.0, 60.0 / 90.0 * 4) == pytest.approx(1.0)
    assert chop.semitones_for(2.0) == pytest.approx(12.0)
    assert chop.semitones_for(0.5) == pytest.approx(-12.0)
    assert chop.speed_for(0, 90) == 1.0


def test_written_wav_reads_back(tmp_path):
    y, sr = make_audio(seconds=3)
    path = chop.write_wav(tmp_path / "out" / "slice.wav", chop.take(y, sr, 0.0, 1.0), sr)
    assert path.is_file()
    info = decode.probe(path)
    assert info.sample_rate == sr
    assert info.duration == pytest.approx(1.0, rel=0.02)


def test_write_wav_normalises_overs(tmp_path):
    loud = np.linspace(-3.0, 3.0, 4410, dtype=np.float32)
    path = chop.write_wav(tmp_path / "hot.wav", loud, 44100)
    back, _ = sf.read(path)
    assert np.max(np.abs(back)) <= 1.0
