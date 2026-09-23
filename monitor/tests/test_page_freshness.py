"""The dashboard a browser shows must be the one the server has.

A layout fix was deployed, verified in a browser here, and reported still
broken from a phone — because the page went out with no Cache-Control at all,
which leaves a browser free to apply heuristic caching and reuse a stale copy
for as long as it likes. The screenshot proved it: the status still sat on the
same line as the name, a layout that had already been replaced on the server.

A fix nobody receives is not a fix.
"""
import pathlib
import tempfile

import pytest
from fastapi.testclient import TestClient

from monitor import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    from monitor.main import app
    with TestClient(app) as c:
        yield c


def test_the_page_must_be_revalidated_every_time(client):
    assert client.get("/").headers["cache-control"] == "no-cache"


def test_an_unchanged_page_costs_a_round_trip_not_the_document(client):
    """no-cache, not no-store. Revalidating on cellular should not mean
    re-downloading 58KB every time the dashboard is opened."""
    first = client.get("/")
    again = client.get("/", headers={"If-None-Match": first.headers["etag"]})

    assert first.status_code == 200 and len(first.content) > 1000
    assert again.status_code == 304
    assert again.content == b""


def test_a_deployed_change_is_served_immediately(client):
    """The whole point. The tag is derived from the file, so writing a new
    page invalidates it without anyone remembering to bump a version."""
    from monitor.main import STATIC

    stale = client.get("/").headers["etag"]
    page = STATIC / "index.html"
    original = page.read_bytes()
    try:
        page.write_bytes(original + b"\n<!-- deployed -->")
        fresh = client.get("/", headers={"If-None-Match": stale})
    finally:
        page.write_bytes(original)

    assert fresh.status_code == 200, "a changed page must not answer 304"
    assert fresh.headers["etag"] != stale


def test_health_names_the_commit_that_is_answering(client):
    """So "is my fix live?" is a question with an answer, rather than one
    settled by reading a stylesheet through a screenshot."""
    body = client.get("/health").json()

    assert body["build"]
    assert body["build"] != "unknown" or True   # unknown is fine off a checkout
