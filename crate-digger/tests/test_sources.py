import httpx
import pytest
import respx

from crate.sources.base import SourceError
from crate.sources.discogs import Discogs
from crate.sources.internet_archive import (
    InternetArchive,
    _license_name,
    _parse_length,
    _parse_year,
    pick_audio_files,
)
from crate.sources.openverse import Openverse
from crate.sources.youtube import YouTube, parse_timestamp, parse_video_id


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


# ── Internet Archive ──────────────────────────────────────────
def test_ia_query_builder():
    q = InternetArchive.build_query(
        "horns", collections=["georgeblood"], subjects=["Jazz"],
        year_from=1960, year_to=1969,
    )
    assert "mediatype:(audio)" in q
    assert "(horns)" in q
    assert 'collection:("georgeblood")' in q
    assert 'subject:("Jazz")' in q
    assert "date:[1960-01-01 TO 1969-12-31]" in q


def test_ia_query_builder_is_fine_with_nothing():
    assert InternetArchive.build_query() == "mediatype:(audio)"


def test_pick_audio_prefers_lossless_and_drops_junk():
    files = [
        {"name": "t1.flac", "format": "Flac", "size": "9000000", "original": "t1.flac"},
        {"name": "t1.mp3", "format": "VBR MP3", "size": "4000000", "original": "t1.flac"},
        {"name": "t1_sample.mp3", "format": "MP3", "size": "900000"},
        {"name": "t2.mp3", "format": "VBR MP3", "size": "3000000"},
        {"name": "tiny.mp3", "format": "MP3", "size": "1000"},
        {"name": "cover.jpg", "format": "JPEG", "size": "50000"},
    ]
    assert sorted(pick_audio_files(files)) == ["t1.flac", "t2.mp3"]


@pytest.mark.parametrize(
    "raw,expected",
    [("1967", 1967), ("1967-04-02", 1967), ("c. 1955", 1955), ("", None), (None, None)],
)
def test_year_parsing(raw, expected):
    assert _parse_year(raw) == expected


@pytest.mark.parametrize(
    "raw,expected", [("3:24", 204.0), ("1:02:03", 3723.0), ("187.5", 187.5), ("", None)]
)
def test_length_parsing(raw, expected):
    assert _parse_length(raw) == expected


def test_license_naming():
    assert _license_name("https://creativecommons.org/publicdomain/mark/1.0/", []) == "Public Domain"
    assert _license_name("https://creativecommons.org/licenses/by-sa/4.0/", []) == "CC BY-SA"
    assert _license_name("", ["georgeblood"]) == "Public Domain (78rpm transfer)"
    assert _license_name("", ["random"]) == ""


@respx.mock
async def test_ia_search_parses_results(client):
    respx.get("https://archive.org/advancedsearch.php").mock(
        return_value=httpx.Response(200, json={"response": {"docs": [
            {
                "identifier": "78_blue-moon", "title": "Blue Moon",
                "creator": ["The Trio"], "year": "1948",
                "collection": ["georgeblood"], "subject": "Jazz",
                "publisher": "Decca",
            }
        ]}})
    )
    leads = await InternetArchive(client).search("blue moon")
    assert len(leads) == 1
    lead = leads[0]
    assert lead.source_id == "78_blue-moon"
    assert lead.artist == "The Trio"
    assert lead.year == 1948
    assert lead.label == "Decca"
    assert lead.license == "Public Domain (78rpm transfer)"
    assert lead.page_url.endswith("78_blue-moon")


@respx.mock
async def test_ia_search_surfaces_failures(client):
    respx.get("https://archive.org/advancedsearch.php").mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(SourceError, match="search failed"):
        await InternetArchive(client).search("anything")


@respx.mock
async def test_ia_tracks_builds_download_urls(client):
    respx.get("https://archive.org/metadata/78_blue-moon").mock(
        return_value=httpx.Response(200, json={
            "metadata": {"identifier": "78_blue-moon", "title": "Blue Moon",
                         "creator": "The Trio", "collection": ["georgeblood"]},
            "files": [
                {"name": "side-a.flac", "format": "Flac", "size": "8000000",
                 "title": "Side A", "length": "3:02"},
                {"name": "side-a.mp3", "format": "VBR MP3", "size": "3000000",
                 "original": "side-a.flac"},
                {"name": "meta.xml", "format": "Metadata", "size": "800"},
            ],
        })
    )
    tracks = await InternetArchive(client).tracks("78_blue-moon")
    assert len(tracks) == 1
    assert tracks[0].stream_url == "https://archive.org/download/78_blue-moon/side-a.flac"
    assert tracks[0].duration == 182.0
    assert tracks[0].album == "Blue Moon"
    assert tracks[0].source_id == "78_blue-moon/side-a.flac"


# ── Openverse ─────────────────────────────────────────────────
@respx.mock
async def test_openverse_parses_results(client):
    respx.get("https://api.openverse.org/v1/audio/").mock(
        return_value=httpx.Response(200, json={"results": [{
            "id": "abc-123", "title": "Rhodes Loop", "creator": "someone",
            "url": "https://cdn.example/audio.mp3",
            "foreign_landing_url": "https://freemusicarchive.org/x",
            "license": "by", "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "duration": 32000, "genres": ["Jazz"],
            "tags": [{"name": "rhodes"}, {"name": "loop"}],
            "provider": "freesound",
        }]})
    )
    leads = await Openverse(client).search("rhodes")
    assert leads[0].license == "CC BY 4.0"
    assert leads[0].duration == 32.0
    assert leads[0].stream_url.endswith(".mp3")
    assert leads[0].extra["tags"] == ["rhodes", "loop"]


@respx.mock
async def test_openverse_requests_reusable_licences(client):
    route = respx.get("https://api.openverse.org/v1/audio/").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await Openverse(client).search("drums")
    assert "license_type=commercial%2Cmodification" in str(route.calls[0].request.url)


# ── YouTube ───────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=x",
    "youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    "dQw4w9WgXcQ",
])
def test_video_id_from_every_url_shape(url):
    assert parse_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", ["https://vimeo.com/1", "not a url", "", "https://youtube.com/"])
def test_video_id_rejects_the_rest(url):
    assert parse_video_id(url) is None


@pytest.mark.parametrize("url,secs", [
    ("https://youtu.be/dQw4w9WgXcQ?t=90", 90.0),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1h2m3s", 3723.0),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=2m", 120.0),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
])
def test_timestamp_parsing(url, secs):
    assert parse_timestamp(url) == secs


@respx.mock
async def test_youtube_lead_uses_oembed(client):
    respx.get("https://www.youtube.com/oembed").mock(
        return_value=httpx.Response(200, json={
            "title": "Rare Soul 45", "author_name": "Crate Channel",
            "thumbnail_url": "https://i.ytimg.com/x.jpg",
        })
    )
    lead = await YouTube(client).lead_for("https://youtu.be/dQw4w9WgXcQ?t=42")
    assert lead.title == "Rare Soul 45"
    assert lead.artist == "Crate Channel"
    assert lead.extra["timestamp"] == 42.0
    assert lead.stream_url == ""     # never resolves audio itself


@respx.mock
async def test_youtube_lead_survives_a_dead_video(client):
    respx.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(404))
    lead = await YouTube(client).lead_for("https://youtu.be/dQw4w9WgXcQ")
    assert lead.title == "dQw4w9WgXcQ"


async def test_youtube_search_is_honest_about_being_unavailable(client):
    with pytest.raises(SourceError, match="API key"):
        await YouTube(client).search("soul")


# ── Discogs ───────────────────────────────────────────────────
async def test_discogs_without_a_token_says_so(client):
    with pytest.raises(SourceError, match="token"):
        await Discogs(client).search("blue note")


@respx.mock
async def test_discogs_splits_artist_and_title(client):
    respx.get("https://api.discogs.com/database/search").mock(
        return_value=httpx.Response(200, json={"results": [{
            "id": 42, "title": "Bobbi Humphrey - Blacks And Blues",
            "year": "1974", "label": ["Blue Note"], "style": ["Jazz-Funk"],
            "uri": "/release/42",
        }]})
    )
    leads = await Discogs(client, token="x").search("blacks and blues")
    assert leads[0].artist == "Bobbi Humphrey"
    assert leads[0].title == "Blacks And Blues"
    assert leads[0].label == "Blue Note"
    assert leads[0].year == 1974
    assert leads[0].extra["search_hint"] == "Bobbi Humphrey Blacks And Blues"
