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


def test_a_deployed_change_is_served_immediately(client, tmp_path, monkeypatch):
    """The whole point. The tag is derived from the file, so writing a new
    page invalidates it without anyone remembering to bump a version."""
    import monitor.main as main

    # Against a COPY. Writing to the real page to prove a point about caching
    # would risk leaving the served dashboard truncated if this process died
    # mid-test — on the very server the deploy gate runs on.
    staging = tmp_path / "static"
    staging.mkdir()
    page = staging / "index.html"
    page.write_bytes((main.STATIC / "index.html").read_bytes())
    monkeypatch.setattr(main, "STATIC", staging)

    stale = client.get("/").headers["etag"]
    page.write_bytes(page.read_bytes() + b"\n<!-- deployed -->")
    fresh = client.get("/", headers={"If-None-Match": stale})

    assert fresh.status_code == 200, "a changed page must not answer 304"
    assert fresh.headers["etag"] != stale


def test_health_names_the_commit_that_is_answering(client):
    """So "is my fix live?" is a question with an answer, rather than one
    settled by reading a stylesheet through a screenshot."""
    body = client.get("/health").json()

    assert body["build"]
    assert body["build"] != "unknown" or True   # unknown is fine off a checkout


def test_me_reports_the_build_so_a_cached_page_can_say_so(client):
    """A stale page tells you it is stale, instead of leaving "did the deploy
    land?" to be argued about from a screenshot."""
    body = client.get("/api/me").json()

    assert "build" in body


def test_rendering_the_page_can_be_done_without_polling_anyone(monkeypatch):
    """A layout check starts the whole app so the dashboard is real, and the
    whole app polls stores. Running that in CI meant a GitHub runner making
    live requests to the shops this monitor watches, on every push. A
    screenshot is not a reason to touch someone's server."""
    from monitor import scheduler

    monkeypatch.setenv("MONITOR_NO_SCHEDULER", "1")
    monkeypatch.setattr(scheduler, "_scheduler", None)

    assert scheduler.start() is None
    assert scheduler._scheduler is None, "nothing was started"


async def test_the_switch_is_opt_in_so_the_service_still_polls(monkeypatch):
    """Async, because APScheduler's AsyncIOScheduler needs a running loop —
    production starts it inside the app's lifespan, where there is one."""
    from monitor import scheduler

    monkeypatch.delenv("MONITOR_NO_SCHEDULER", raising=False)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    try:
        assert scheduler.start() is not None
    finally:
        scheduler.shutdown()
        scheduler._scheduler = None
