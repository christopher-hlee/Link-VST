import httpx
import pytest
import respx

from crate import library
from crate.sources.base import Lead


def test_slugify_handles_real_record_titles():
    assert library.slugify("Sittin' On The Dock — Otis (1967)") == "sittin-on-the-dock-otis-1967"
    assert library.slugify("   ") == "untitled"
    assert library.slugify("Café Noir") == "cafe-noir"
    assert len(library.slugify("x" * 200)) <= 60


@pytest.mark.parametrize("url,ctype,expected", [
    ("https://x/a.flac", "", ".flac"),
    ("https://x/a.mp3?download=1", "", ".mp3"),
    ("https://x/stream", "audio/flac", ".flac"),
    ("https://x/stream", "audio/mpeg", ".mp3"),
    ("https://x/stream", "", ".mp3"),
])
def test_extension_detection(url, ctype, expected):
    assert library.extension_for(url, ctype) == expected


def test_analyze_file_reports_everything(audio_file):
    result = library.analyze_file(audio_file)
    assert result["duration"] == pytest.approx(12.0, rel=0.01)
    assert result["sample_rate"] == 22050
    assert result["bpm"] == pytest.approx(90, abs=3)
    assert result["musical_key"]
    assert len(result["peaks"]) == library.PEAK_BUCKETS
    assert result["ext"] == "flac"
    assert result["bytes"] > 0


def test_import_local_copies_and_analyses(db, settings, audio_file):
    row = library.import_local(db, audio_file, audio_dir=settings.audio_dir)
    assert row["status"] == "ready"
    assert row["source"] == "local"
    assert row["bpm"]
    assert settings.audio_dir in __import__("pathlib").Path(row["file_path"]).parents


def test_import_local_without_copying_points_at_the_original(db, settings, audio_file):
    row = library.import_local(db, audio_file, audio_dir=settings.audio_dir, copy=False)
    assert row["file_path"] == str(audio_file.resolve())


def test_import_local_missing_file(db, settings, tmp_path):
    with pytest.raises(FileNotFoundError):
        library.import_local(db, tmp_path / "nope.wav", audio_dir=settings.audio_dir)


@respx.mock
async def test_download_rejects_non_audio(tmp_path):
    respx.get("https://x/thing").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(library.DownloadError, match="Not audio"):
            await library.download(client, "https://x/thing", tmp_path, stem="s")
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_download_enforces_the_size_cap(tmp_path):
    respx.get("https://x/big.mp3").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "audio/mpeg", "content-length": "99000000"},
            content=b"x" * 10,
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(library.DownloadError, match="over the"):
            await library.download(client, "https://x/big.mp3", tmp_path, stem="s", max_mb=1)


@respx.mock
async def test_download_cap_also_applies_mid_stream(tmp_path):
    """A chunked response declares no length, so the cap has to hold while reading."""
    async def chunks():
        for _ in range(30):
            yield b"x" * 100_000

    respx.get("https://x/lying.mp3").mock(
        return_value=httpx.Response(200, headers={"content-type": "audio/mpeg"},
                                    content=chunks())
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(library.DownloadError, match="mid-stream"):
            await library.download(client, "https://x/lying.mp3", tmp_path, stem="s", max_mb=1)
    assert not list(tmp_path.glob("*.part"))   # partial file is cleaned up


@respx.mock
async def test_download_rejects_an_empty_body(tmp_path):
    respx.get("https://x/empty.mp3").mock(
        return_value=httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"")
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(library.DownloadError, match="Empty"):
            await library.download(client, "https://x/empty.mp3", tmp_path, stem="s")


@respx.mock
async def test_ingest_lead_downloads_and_analyses(db, settings, audio_file):
    respx.get("https://archive.test/side-a.flac").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "audio/flac"}, content=audio_file.read_bytes()
        )
    )
    lead = Lead(source="ia", source_id="rec/side-a.flac", title="Side A",
                stream_url="https://archive.test/side-a.flac")
    async with httpx.AsyncClient() as client:
        row = await library.ingest_lead(db, client, lead, audio_dir=settings.audio_dir)
    assert row["status"] == "ready"
    assert row["bpm"]
    assert row["ext"] == "flac"


@respx.mock
async def test_ingest_lead_records_the_failure_instead_of_raising(db, settings):
    respx.get("https://archive.test/gone.mp3").mock(return_value=httpx.Response(404))
    lead = Lead(source="ia", source_id="gone", title="Gone",
                stream_url="https://archive.test/gone.mp3")
    async with httpx.AsyncClient() as client:
        row = await library.ingest_lead(db, client, lead, audio_dir=settings.audio_dir)
    assert row["status"] == "error"
    assert "404" in row["error"]


@respx.mock
async def test_ingest_is_idempotent(db, settings, audio_file):
    route = respx.get("https://archive.test/a.flac").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "audio/flac"}, content=audio_file.read_bytes()
        )
    )
    lead = Lead(source="ia", source_id="a", title="A", stream_url="https://archive.test/a.flac")
    async with httpx.AsyncClient() as client:
        first = await library.ingest_lead(db, client, lead, audio_dir=settings.audio_dir)
        second = await library.ingest_lead(db, client, lead, audio_dir=settings.audio_dir)
    assert first["id"] == second["id"]
    assert route.call_count == 1   # the second time it never touches the network


async def test_ingest_of_a_lead_with_no_audio_stays_a_lead(db, settings):
    lead = Lead(source="youtube", source_id="abc123", title="A video")
    async with httpx.AsyncClient() as client:
        row = await library.ingest_lead(db, client, lead, audio_dir=settings.audio_dir)
    assert row["status"] == "lead"
    assert row["file_path"] is None
