"""Alert routing and the dead-man's switch.

Both of these were previously declared but not wired: alert_level was stored and
never read, and the heartbeat pinged success unconditionally — so a banned IP
would have kept the alarm green while every check failed.
"""
import httpx
import pytest
import respx

from monitor import db, scheduler

STORE = "https://option-o.com"
URL = f"{STORE}/products/omni-grinder"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture
def channels(monkeypatch):
    """Capture every outbound channel instead of hitting the network."""
    sent = {"telegram": [], "ntfy": [], "beats": []}

    async def fake_tg(watch, kind, payload):
        sent["telegram"].append(kind)

    async def fake_ntfy(title, body, url=None, priority="high", tags="bell"):
        sent["ntfy"].append({"title": title, "priority": priority, "url": url})

    async def fake_ping(suffix=""):
        sent["beats"].append(suffix or "/ok")

    monkeypatch.setattr("monitor.notify.telegram.send_event", fake_tg)
    monkeypatch.setattr("monitor.notify.telegram.configured", lambda: True)
    monkeypatch.setattr("monitor.notify.ntfy.send", fake_ntfy)
    monkeypatch.setattr("monitor.notify.ntfy.configured", lambda: True)
    monkeypatch.setattr("monitor.notify.heartbeat.ping", fake_ping)
    return sent


def js(available):
    return {"id": 1, "title": "Omni Grinder", "handle": "omni-grinder",
            "variants": [{"id": 555, "available": available, "price": 39500}]}


def make_watch(**kw):
    fields = dict(name="Omni Grinder", brand="Option-O", url=URL,
                  strategy="shopify", kind="product", target_ref="omni-grinder")
    fields.update(kw)
    return db.create_watch(**fields)


# --- alert_level actually routes -------------------------------------------

@respx.mock
async def test_info_watch_does_not_hit_ntfy(channels):
    wid = make_watch(alert_level="info")
    route = respx.get(f"{STORE}/products/omni-grinder.js")
    route.mock(return_value=httpx.Response(200, json=js(False)))
    await scheduler.check_watch(db.get_watch(wid))
    route.mock(return_value=httpx.Response(200, json=js(True)))
    await scheduler.check_watch(db.get_watch(wid))

    assert channels["telegram"] == ["restock"]
    assert channels["ntfy"] == [], "info-level must stay on the quiet channel"


@respx.mock
async def test_critical_watch_also_hits_ntfy_at_urgent(channels):
    wid = make_watch(alert_level="critical")
    route = respx.get(f"{STORE}/products/omni-grinder.js")
    route.mock(return_value=httpx.Response(200, json=js(False)))
    await scheduler.check_watch(db.get_watch(wid))
    route.mock(return_value=httpx.Response(200, json=js(True)))
    await scheduler.check_watch(db.get_watch(wid))

    assert channels["telegram"] == ["restock"]
    assert len(channels["ntfy"]) == 1
    assert channels["ntfy"][0]["priority"] == "urgent"
    assert channels["ntfy"][0]["url"] == f"{STORE}/cart/555:1"


@respx.mock
async def test_watch_failing_escalates_even_on_an_info_watch(channels):
    """A broken watch is urgent regardless of how the watch is configured."""
    wid = make_watch(alert_level="info")
    respx.get(f"{STORE}/products/omni-grinder.js").mock(
        return_value=httpx.Response(403))
    for _ in range(5):
        await scheduler.check_watch(db.get_watch(wid))

    assert channels["telegram"] == ["watch_failing"]
    assert len(channels["ntfy"]) == 1, "failures escalate past alert_level"


@respx.mock
async def test_event_counts_as_delivered_if_any_channel_works(channels, monkeypatch):
    async def tg_down(watch, kind, payload):
        raise RuntimeError("Telegram HTTP 502")

    monkeypatch.setattr("monitor.notify.telegram.send_event", tg_down)
    wid = make_watch(alert_level="critical")
    route = respx.get(f"{STORE}/products/omni-grinder.js")
    route.mock(return_value=httpx.Response(200, json=js(False)))
    await scheduler.check_watch(db.get_watch(wid))
    route.mock(return_value=httpx.Response(200, json=js(True)))
    await scheduler.check_watch(db.get_watch(wid))

    assert len(channels["ntfy"]) == 1, "ntfy still delivers when Telegram is down"
    event = db.list_events()[0]
    assert "telegram" in event["notify_error"]
    assert db.get_watch(wid)["last_alert_at"] is not None, "partial delivery counts"


# --- the dead-man's switch tells the truth ---------------------------------

@respx.mock
async def test_all_checks_failing_trips_the_alarm(channels):
    """A banned IP must not read as a quiet market."""
    for _ in range(3):
        make_watch()
    respx.get(f"{STORE}/products/omni-grinder.js").mock(
        return_value=httpx.Response(403))

    await scheduler.tick()

    assert channels["beats"] == ["/fail"], \
        "every check failed — the heartbeat must not report success"


@respx.mock
async def test_partial_failure_still_reports_healthy(channels):
    ok_id = make_watch()
    bad_id = make_watch(name="Broken", target_ref="missing")
    respx.get(f"{STORE}/products/omni-grinder.js").mock(
        return_value=httpx.Response(200, json=js(False)))
    respx.get(f"{STORE}/products/missing.js").mock(
        return_value=httpx.Response(404))

    await scheduler.tick()

    assert channels["beats"] == ["/ok"], "one site being down is not an outage"
    assert db.get_watch(ok_id)["consecutive_failures"] == 0
    assert db.get_watch(bad_id)["consecutive_failures"] == 1


async def test_idle_tick_reports_healthy(channels):
    await scheduler.tick()
    assert channels["beats"] == ["/ok"], "nothing due is healthy, not failed"
