"""End-to-end tests against the running app, with the archives mocked out."""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
import respx

IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_META = "https://archive.org/metadata/78_test-record"
IA_FILE = "https://archive.org/download/78_test-record/side-a.flac"


def wait_for_jobs(client, timeout: float = 30.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get("/api/jobs").json()
        if data["active"] == 0:
            return data["jobs"]
        time.sleep(0.05)
    raise AssertionError("jobs never finished")


def mock_archive(audio_bytes: bytes) -> None:
    respx.get(IA_SEARCH).mock(return_value=httpx.Response(200, json={"response": {"docs": [
        {"identifier": "78_test-record", "title": "Test Record", "creator": "The Trio",
         "year": "1968", "collection": ["georgeblood"], "subject": "Jazz"}
    ]}}))
    respx.get(IA_META).mock(return_value=httpx.Response(200, json={
        "metadata": {"identifier": "78_test-record", "title": "Test Record",
                     "creator": "The Trio", "collection": ["georgeblood"]},
        "files": [{"name": "side-a.flac", "format": "Flac", "size": "900000",
                   "title": "Side A", "length": "0:12"}],
    }))
    respx.get(IA_FILE).mock(return_value=httpx.Response(
        200, headers={"content-type": "audio/flac"}, content=audio_bytes))


@pytest.fixture
def kept(client, audio_file):
    """One record downloaded, analysed and sitting in the crate."""
    with respx.mock:
        mock_archive(audio_file.read_bytes())
        tracks = client.get("/api/ia/item/78_test-record").json()["results"]
        client.post("/api/ingest", json={"leads": [
            {k: v for k, v in tracks[0].items()
             if k in {"source", "source_id", "title", "artist", "album", "year",
                      "genre", "license", "page_url", "stream_url", "duration"}}
        ]})
        wait_for_jobs(client)
    rows = client.get("/api/library").json()["results"]
    assert rows, "ingest produced nothing"
    return rows[0]


# ── basics ────────────────────────────────────────────────────
def test_health(client, settings):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["library_dir"] == str(settings.library_dir)


def test_index_page_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Crate Digger" in res.text


def test_sources_and_digs(client):
    sources = client.get("/api/sources").json()
    assert {s["name"] for s in sources["sources"]} >= {"ia", "openverse", "youtube"}
    assert sources["ripper"]["enabled"] is False   # off unless asked for

    digs = client.get("/api/digs").json()["digs"]
    assert any(d["slug"] == "dusty-78s" for d in digs)
    assert all(d["blurb"] for d in digs)


def test_unknown_dig_is_404(client):
    assert client.get("/api/dig/not-a-seam").status_code == 404


# ── digging ───────────────────────────────────────────────────
@respx.mock
def test_dig_returns_results_and_hides_what_youve_seen(client, audio_file):
    mock_archive(audio_file.read_bytes())

    first = client.get("/api/dig/dusty-78s?seed=1").json()
    assert first["count"] == 1
    assert first["results"][0]["title"] == "Test Record"
    assert first["results"][0]["sample_id"] is None

    client.post("/api/verdict", json={
        "source": "ia", "source_id": "78_test-record", "verdict": "pass"})
    again = client.get("/api/dig/dusty-78s?seed=1").json()
    assert again["count"] == 0


@respx.mock
def test_dig_reports_an_upstream_outage(client):
    respx.get(IA_SEARCH).mock(return_value=httpx.Response(503))
    assert client.get("/api/dig/dusty-78s").status_code == 502


@respx.mock
def test_search_passes_filters_through(client, audio_file):
    mock_archive(audio_file.read_bytes())
    res = client.get("/api/search?q=trio&source=ia&year_from=1960&year_to=1969"
                     "&collections=georgeblood&subjects=Jazz")
    assert res.status_code == 200
    sent = str(respx.calls[0].request.url)
    assert "1960-01-01" in sent and "georgeblood" in sent


def test_search_with_an_unknown_source_is_404(client):
    assert client.get("/api/search?source=napster").status_code == 404


# ── the crate ─────────────────────────────────────────────────
def test_ingest_produces_an_analysed_sample(kept):
    assert kept["status"] == "ready"
    assert kept["title"] == "Side A"
    assert kept["bpm"] == pytest.approx(90, abs=4)
    assert kept["musical_key"]
    assert kept["breakiness"] is not None
    assert Path(kept["file_path"]).is_file()


def test_ingest_failure_is_reported_on_the_job(client):
    with respx.mock:
        respx.get("https://archive.test/missing.flac").mock(
            return_value=httpx.Response(404))
        client.post("/api/ingest", json={"leads": [{
            "source": "ia", "source_id": "missing", "title": "Missing",
            "stream_url": "https://archive.test/missing.flac"}]})
        wait_for_jobs(client)
    row = client.get("/api/library?status=error").json()["results"][0]
    assert "404" in row["error"]


def test_library_filters(client, kept):
    bpm = kept["bpm"]
    assert client.get(f"/api/library?bpm_min={bpm - 1}&bpm_max={bpm + 1}").json()["total"] == 1
    assert client.get(f"/api/library?bpm_min={bpm + 40}").json()["total"] == 0
    assert client.get("/api/library?q=side").json()["total"] == 1
    assert client.get("/api/library?q=zzzznothing").json()["total"] == 0
    assert client.get("/api/library?source=ia").json()["total"] == 1
    assert client.get("/api/library?starred=true").json()["total"] == 0
    # List rows drop the 1600-point waveform but say it exists.
    row = client.get("/api/library").json()["results"][0]
    assert "peaks" not in row and row["has_peaks"] is True


def test_library_sorts_are_all_valid_sql(client, kept):
    for sort in ["recent", "oldest", "bpm", "bpm_desc", "breaks", "smooth",
                 "duration", "title", "year", "loud", "nonsense"]:
        assert client.get(f"/api/library?sort={sort}").status_code == 200


def test_library_stats(client, kept):
    stats = client.get("/api/library/stats").json()
    assert stats["samples"] == 1
    assert stats["seconds"] > 0
    assert stats["by_source"][0]["source"] == "ia"


def test_patch_and_star_a_sample(client, kept):
    sid = kept["id"]
    updated = client.patch(f"/api/samples/{sid}",
                           json={"starred": True, "notes": "bar 33, the horn stab"}).json()
    assert updated["starred"] is True
    assert updated["notes"] == "bar 33, the horn stab"
    assert client.get("/api/library?starred=true").json()["total"] == 1


def test_missing_sample_is_404(client):
    assert client.get("/api/samples/9999").status_code == 404
    assert client.patch("/api/samples/9999", json={"notes": "x"}).status_code == 404


def test_delete_removes_the_row_and_optionally_the_file(client, kept):
    path = Path(kept["file_path"])
    res = client.delete(f"/api/samples/{kept['id']}?delete_file=true").json()
    assert str(path) in res["files_removed"]
    assert not path.exists()
    assert client.get("/api/library").json()["total"] == 0


# ── audio ─────────────────────────────────────────────────────
def test_file_is_served_with_range_support(client, kept):
    res = client.get(f"/api/samples/{kept['id']}/file")
    assert res.status_code == 200
    assert res.headers["accept-ranges"] == "bytes"
    assert "inline" in res.headers["content-disposition"]

    part = client.get(f"/api/samples/{kept['id']}/file", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206
    assert len(part.content) == 100


def test_download_flag_sets_attachment(client, kept):
    res = client.get(f"/api/samples/{kept['id']}/file?download=true")
    assert "attachment" in res.headers["content-disposition"]


def test_peaks_endpoint(client, kept):
    peaks = client.get(f"/api/samples/{kept['id']}/peaks").json()["peaks"]
    assert len(peaks) == 1600
    assert max(peaks) == pytest.approx(1.0)


def test_reanalyze(client, kept):
    row = client.post(f"/api/samples/{kept['id']}/analyze").json()
    assert row["status"] == "ready"
    assert row["bpm"]


def test_audio_endpoints_reject_a_lead_with_no_file(client):
    client.post("/api/youtube", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    sid = client.get("/api/library?source=youtube").json()["results"][0]["id"]
    assert client.get(f"/api/samples/{sid}/file").status_code == 409


# ── chopping ──────────────────────────────────────────────────
def test_chop_preview_writes_nothing(client, kept, settings):
    res = client.post(f"/api/samples/{kept['id']}/chop",
                      json={"mode": "transient", "sensitivity": 1.0}).json()
    assert res["count"] > 4
    assert all(s["end_sec"] > s["start_sec"] for s in res["slices"])
    assert not any(settings.slices_dir.iterdir())


def test_grid_chop_needs_a_tempo(client, kept):
    client.patch(f"/api/samples/{kept['id']}", json={"bpm": 90.0})
    res = client.post(f"/api/samples/{kept['id']}/chop",
                      json={"mode": "grid", "division": 4}).json()
    assert res["count"] >= 2
    assert res["slices"][0]["length_sec"] == pytest.approx(60 / 90 * 4, rel=0.01)


def test_chop_export_writes_a_kit(client, kept, settings):
    res = client.post(f"/api/samples/{kept['id']}/chop/export",
                      json={"mode": "grid", "division": 4, "bpm": 90.0,
                            "to_export_dir": True}).json()
    assert res["count"] >= 2
    assert res["exported_to_daw"] is True
    for entry in res["slices"]:
        assert Path(entry["file_path"]).is_file()
        served = client.get(entry["url"])
        assert served.status_code == 200
        assert "attachment" in served.headers["content-disposition"]
    assert list(Path(settings.export_dir).glob("*.wav"))

    detail = client.get(f"/api/samples/{kept['id']}").json()
    assert len(detail["slices"]) == res["count"]


def test_re_exporting_replaces_the_old_kit(client, kept):
    first = client.post(f"/api/samples/{kept['id']}/chop/export",
                        json={"mode": "grid", "division": 4, "bpm": 90.0}).json()
    second = client.post(f"/api/samples/{kept['id']}/chop/export",
                         json={"mode": "grid", "division": 8, "bpm": 90.0}).json()
    detail = client.get(f"/api/samples/{kept['id']}").json()
    assert len(detail["slices"]) == second["count"] != first["count"]


def test_missing_slice_is_404(client):
    assert client.get("/api/slices/999/file").status_code == 404


# ── loops and getting out ─────────────────────────────────────
def test_loop_render_varispeeds_and_serves(client, kept):
    res = client.post(f"/api/samples/{kept['id']}/loop", json={
        "start": 1.0, "end": 4.0, "source_bpm": 120.0, "target_bpm": 90.0}).json()
    assert res["speed"] == pytest.approx(0.75)
    assert res["semitones"] == pytest.approx(-4.98, abs=0.02)
    assert "90bpm" in res["filename"]

    served = client.get(res["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "audio/wav"


def test_loop_rejects_a_backwards_region(client, kept):
    assert client.post(f"/api/samples/{kept['id']}/loop",
                       json={"start": 4.0, "end": 2.0}).status_code == 400


def test_render_path_traversal_is_refused(client):
    assert client.get("/api/renders/..%2F..%2Fcrate.sqlite3").status_code in (400, 404)
    assert client.get("/api/renders/nope.wav").status_code == 404


def test_dragout_manifest_shape(client, kept):
    manifest = client.get(f"/api/dragout/{kept['id']}").json()
    mime, filename, url = manifest["download_url"].split(":", 2)
    assert mime.startswith("audio/")
    assert filename.endswith(".flac")
    assert url.startswith("http")


def test_dragout_of_a_region_renders_a_loop(client, kept):
    manifest = client.get(f"/api/dragout/{kept['id']}?start=1&end=3").json()
    assert manifest["mime"] == "audio/wav"
    assert client.get(manifest["url"].split("/api/")[0] and
                      "/api/" + manifest["url"].split("/api/")[1]).status_code == 200


def test_export_to_daw_folder(client, kept, settings):
    res = client.post(f"/api/samples/{kept['id']}/export").json()
    assert Path(res["exported"]).is_file()
    assert Path(res["exported"]).parent == Path(settings.export_dir)


# ── crates, markers, leads ────────────────────────────────────
def test_crate_lifecycle(client, kept):
    crate = client.post("/api/crates", json={"name": "Beat tape 3"}).json()
    assert client.post("/api/crates", json={"name": "Beat tape 3"}).json()["id"] == crate["id"]

    client.post(f"/api/crates/{crate['id']}/items", json={"sample_id": kept["id"]})
    listed = client.get("/api/crates").json()["crates"]
    assert listed[0]["item_count"] == 1
    assert client.get(f"/api/library?crate_id={crate['id']}").json()["total"] == 1
    assert client.get(f"/api/library?crate_id={crate['id']}&q=side").json()["total"] == 1

    client.delete(f"/api/crates/{crate['id']}/items/{kept['id']}")
    assert client.get(f"/api/library?crate_id={crate['id']}").json()["total"] == 0

    client.delete(f"/api/crates/{crate['id']}")
    assert client.get("/api/crates").json()["crates"] == []


def test_adding_to_a_missing_crate_is_404(client, kept):
    assert client.post("/api/crates/999/items",
                       json={"sample_id": kept["id"]}).status_code == 404


def test_markers(client, kept):
    marker = client.post(f"/api/samples/{kept['id']}/markers",
                         json={"kind": "loop", "start_sec": 4.0, "end_sec": 8.0,
                               "label": "the break"}).json()
    detail = client.get(f"/api/samples/{kept['id']}").json()
    assert detail["markers"][0]["label"] == "the break"
    client.delete(f"/api/markers/{marker['id']}")
    assert client.get(f"/api/samples/{kept['id']}").json()["markers"] == []


@respx.mock
def test_youtube_lead_and_timestamp_marker(client):
    respx.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(
        200, json={"title": "Rare Soul 45", "author_name": "Crate Channel"}))
    res = client.post("/api/youtube",
                      json={"url": "https://youtu.be/dQw4w9WgXcQ?t=125", "note": "the intro"}).json()
    assert res["ripped"] is False
    detail = client.get(f"/api/samples/{res['sample_id']}").json()
    assert detail["title"] == "Rare Soul 45"
    assert detail["notes"] == "the intro"
    assert detail["markers"][0]["start_sec"] == 125.0


def test_youtube_rejects_a_non_youtube_url(client):
    assert client.post("/api/youtube", json={"url": "https://vimeo.com/1"}).status_code == 400


@respx.mock
def test_ripping_while_disabled_explains_itself(client):
    respx.get("https://www.youtube.com/oembed").mock(return_value=httpx.Response(
        200, json={"title": "A video", "author_name": "Someone"}))
    res = client.post("/api/youtube",
                      json={"url": "https://youtu.be/dQw4w9WgXcQ", "rip": True}).json()
    assert res["ripped"] is False
    assert "CRATE_ENABLE_RIPPER" in res["rip_error"]


def test_import_a_local_file(client, audio_file):
    row = client.post("/api/import", json={"path": str(audio_file)}).json()
    assert row["status"] == "ready"
    assert row["source"] == "local"
    assert client.post("/api/import", json={"path": "/nope/x.wav"}).status_code == 404
