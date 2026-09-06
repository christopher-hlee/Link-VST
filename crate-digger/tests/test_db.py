import pytest

from crate.db import Database, hydrate


def test_upsert_is_idempotent(db):
    first = db.upsert_sample({"source": "ia", "source_id": "abc", "title": "One"})
    second = db.upsert_sample({"source": "ia", "source_id": "abc", "title": "One again"})
    assert first == second
    assert db.get_sample(first)["title"] == "One"   # first write wins


def test_update_and_hydrate_peaks(db):
    sid = db.upsert_sample({"source": "local", "source_id": "/x.wav", "title": "X"})
    db.update_sample(sid, peaks=[0.1, 0.9], bpm=88.0, starred=1)
    row = db.get_sample(sid)
    assert row["peaks"] == [0.1, 0.9]
    assert row["bpm"] == 88.0
    assert row["starred"] is True


def test_update_with_no_fields_is_a_noop(db):
    sid = db.upsert_sample({"source": "local", "source_id": "/y.wav", "title": "Y"})
    db.update_sample(sid)
    assert db.get_sample(sid)["title"] == "Y"


def test_hydrate_survives_corrupt_peaks():
    assert hydrate({"peaks": "not json", "starred": 0})["peaks"] is None


def test_verdicts_and_seen_ids(db):
    db.upsert_sample({"source": "ia", "source_id": "kept", "title": "K"})
    db.record_verdict("ia", "passed", "pass")
    seen = db.seen_ids("ia")
    assert seen == {"kept", "passed"}
    assert db.seen_ids("openverse") == set()


def test_delete_cascades_to_markers(db):
    sid = db.upsert_sample({"source": "local", "source_id": "/z.wav", "title": "Z"})
    db.execute(
        "INSERT INTO markers(sample_id, kind, start_sec, created_at) VALUES (?,?,?,?)",
        (sid, "loop", 1.0, 0.0),
    )
    db.delete_sample(sid)
    assert db.query("SELECT * FROM markers WHERE sample_id=?", (sid,)) == []


def test_fts_index_tracks_updates(db):
    if not db.has_fts:
        pytest.skip("SQLite built without FTS5")
    sid = db.upsert_sample(
        {"source": "ia", "source_id": "f1", "title": "Midnight Session",
         "artist": "Trio", "label": "Blue Note"}
    )
    hits = db.query(
        "SELECT s.id FROM samples s JOIN samples_fts f ON f.rowid = s.id"
        " WHERE samples_fts MATCH ?", ('"midnight"*',)
    )
    assert [h["id"] for h in hits] == [sid]

    db.update_sample(sid, title="Morning Session")
    stale = db.query(
        "SELECT s.id FROM samples s JOIN samples_fts f ON f.rowid = s.id"
        " WHERE samples_fts MATCH ?", ('"midnight"*',)
    )
    assert stale == []
    fresh = db.query(
        "SELECT s.id FROM samples s JOIN samples_fts f ON f.rowid = s.id"
        " WHERE samples_fts MATCH ?", ('"blue"*',)
    )
    assert [h["id"] for h in fresh] == [sid]


def test_schema_is_created_once(settings):
    a = Database(settings.db_path)
    b = Database(settings.db_path)
    assert b.one("SELECT value FROM meta WHERE key='schema_version'")["value"] == "1"
    a.close()
    b.close()
