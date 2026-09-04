"""End-to-end: fetch -> decide -> persist -> notify, against a temp database."""
import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.notify import telegram
from monitor.statemachine import IN_STOCK, OUT_OF_STOCK, RESTOCK, UNKNOWN

STORE = "https://option-o.com"
URL = f"{STORE}/products/omni-grinder"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield


@pytest.fixture
def sent(monkeypatch):
    """Capture Telegram deliveries instead of making network calls."""
    calls = []

    async def fake_send(watch, kind, payload):
        calls.append({"watch": watch, "kind": kind, "payload": payload})

    monkeypatch.setattr("monitor.notify.telegram.send_event", fake_send)
    monkeypatch.setattr("monitor.notify.telegram.configured", lambda: True)
    return calls


def js(available: bool):
    return {"id": 1, "title": "Omni Grinder", "handle": "omni-grinder",
            "variants": [{"id": 555, "title": "Default",
                          "available": available, "price": 39500}]}


def make_watch(**kw):
    fields = dict(name="Omni Grinder", brand="Option-O", url=URL,
                  strategy="shopify", kind="product",
                  target_ref="omni-grinder", base_interval_s=300)
    fields.update(kw)
    return db.create_watch(**fields)


@respx.mock
async def test_full_restock_cycle_alerts_exactly_once(sent):
    watch_id = make_watch()
    route = respx.get(f"{STORE}/products/omni-grinder.js")

    # 1. First check: sold out. Establishes the baseline, stays quiet.
    route.mock(return_value=httpx.Response(200, json=js(False)))
    await scheduler.check_watch(db.get_watch(watch_id))
    assert db.get_watch(watch_id)["last_state"] == OUT_OF_STOCK
    assert sent == []

    # 2. It restocks. One alert, carrying a working cart permalink.
    route.mock(return_value=httpx.Response(200, json=js(True)))
    await scheduler.check_watch(db.get_watch(watch_id))
    assert db.get_watch(watch_id)["last_state"] == IN_STOCK
    assert [c["kind"] for c in sent] == [RESTOCK]
    assert sent[0]["payload"]["cart_url"] == f"{STORE}/cart/555:1"

    # 3. Still in stock on later polls: no repeat alerts.
    for _ in range(3):
        await scheduler.check_watch(db.get_watch(watch_id))
    assert len(sent) == 1

    events = db.list_events()
    assert len(events) == 1
    assert events[0]["notified_at"] is not None


@respx.mock
async def test_block_does_not_flip_state_or_alert(sent):
    """The failure mode this whole design exists to prevent."""
    watch_id = make_watch()
    route = respx.get(f"{STORE}/products/omni-grinder.js")

    route.mock(return_value=httpx.Response(200, json=js(True)))
    await scheduler.check_watch(db.get_watch(watch_id))
    assert db.get_watch(watch_id)["last_state"] == IN_STOCK

    # Now the store starts refusing us.
    route.mock(return_value=httpx.Response(403))
    for _ in range(4):
        await scheduler.check_watch(db.get_watch(watch_id))

    watch = db.get_watch(watch_id)
    assert watch["last_state"] == IN_STOCK, "403 must not read as out of stock"
    assert watch["consecutive_failures"] == 4
    assert sent == [], "below threshold, stay quiet"

    # Fifth failure crosses the threshold and reports the outage.
    await scheduler.check_watch(db.get_watch(watch_id))
    assert [c["kind"] for c in sent] == ["watch_failing"]

    # Further failures don't spam.
    for _ in range(5):
        await scheduler.check_watch(db.get_watch(watch_id))
    assert len(sent) == 1


@respx.mock
async def test_recovery_after_outage_is_reported(sent):
    watch_id = make_watch()
    route = respx.get(f"{STORE}/products/omni-grinder.js")

    route.mock(return_value=httpx.Response(500))
    for _ in range(5):
        await scheduler.check_watch(db.get_watch(watch_id))
    assert [c["kind"] for c in sent] == ["watch_failing"]

    route.mock(return_value=httpx.Response(200, json=js(False)))
    await scheduler.check_watch(db.get_watch(watch_id))

    assert sent[-1]["kind"] == "watch_recovered"
    assert db.get_watch(watch_id)["consecutive_failures"] == 0


@respx.mock
async def test_next_check_is_scheduled_with_jitter(sent):
    watch_id = make_watch(base_interval_s=300)
    respx.get(f"{STORE}/products/omni-grinder.js").mock(
        return_value=httpx.Response(200, json=js(False)))

    await scheduler.check_watch(db.get_watch(watch_id))

    watch = db.get_watch(watch_id)
    assert watch["next_check_at"] > watch["last_checked_at"]
    assert watch["id"] not in [w["id"] for w in db.due_watches()], \
        "a just-checked watch must not be immediately due again"


@respx.mock
async def test_notify_failure_is_recorded_not_swallowed(monkeypatch):
    async def boom(watch, kind, payload):
        raise RuntimeError("Telegram HTTP 401")

    monkeypatch.setattr("monitor.notify.telegram.send_event", boom)
    watch_id = make_watch()
    route = respx.get(f"{STORE}/products/omni-grinder.js")

    route.mock(return_value=httpx.Response(200, json=js(False)))
    await scheduler.check_watch(db.get_watch(watch_id))
    route.mock(return_value=httpx.Response(200, json=js(True)))
    await scheduler.check_watch(db.get_watch(watch_id))

    events = db.list_events()
    assert len(events) == 1, "the event is still recorded"
    assert "401" in events[0]["notify_error"]


@respx.mock
async def test_collection_new_drop_alerts(sent):
    watch_id = db.create_watch(name="Satisfy new arrivals", brand="Satisfy",
                               url=f"{STORE}/collections/new", strategy="shopify",
                               kind="collection", target_ref="new")
    route = respx.get(url__startswith=f"{STORE}/collections/new/products.json")

    route.mock(return_value=httpx.Response(200, json={
        "products": [{"handle": "old-tee", "title": "Old Tee"}]}))
    await scheduler.check_watch(db.get_watch(watch_id))
    assert sent == []

    route.mock(return_value=httpx.Response(200, json={"products": [
        {"handle": "old-tee", "title": "Old Tee"},
        {"handle": "new-short", "title": "New Short"}]}))
    await scheduler.check_watch(db.get_watch(watch_id))

    assert [c["kind"] for c in sent] == ["new_product"]
    assert sent[0]["payload"]["handles"] == ["new-short"]


@respx.mock
async def test_a_drop_alert_links_to_the_new_product_not_the_collection(sent):
    """The whole chain: sweep -> diff -> stored event -> outbound keyboard."""
    watch_id = db.create_watch(name="Satisfy new arrivals", brand="Satisfy",
                               url=f"{STORE}/collections/new", strategy="shopify",
                               kind="collection", target_ref="new")
    route = respx.get(url__startswith=f"{STORE}/collections/new/products.json")
    old = {"handle": "old-tee", "title": "Old Tee",
           "variants": [{"id": 1, "title": "M", "available": True, "price": "40.00"}]}

    route.mock(return_value=httpx.Response(200, json={"products": [old]}))
    await scheduler.check_watch(db.get_watch(watch_id))

    route.mock(return_value=httpx.Response(200, json={"products": [old, {
        "handle": "climb-pants", "title": "Climb Pants",
        "variants": [
            {"id": 222, "title": "M", "available": True, "price": "245.00"},
            {"id": 333, "title": "L", "available": True, "price": "245.00"}]}]}))
    await scheduler.check_watch(db.get_watch(watch_id))

    payload = sent[0]["payload"]
    assert payload["handles"] == ["climb-pants"]
    assert list(payload["items"]) == ["climb-pants"], \
        "the stored event must not carry the whole catalogue"

    kb = telegram._keyboard(db.get_watch(watch_id), payload)
    urls = [b["url"] for row in kb["inline_keyboard"] for b in row]
    assert urls == [f"{STORE}/cart/222:1", f"{STORE}/cart/333:1",
                    f"{STORE}/products/climb-pants"]
    assert f"{STORE}/collections/new" not in urls


def test_summary_counts():
    make_watch()
    db.update_watch(make_watch(), last_state=IN_STOCK)
    s = db.summary()
    assert s["total"] == 2 and s["in_stock"] == 1 and s["enabled"] == 2


# --- detection latency, end to end ------------------------------------------

def _catalogue(handles, published=None):
    return {"products": [
        {"handle": h, "title": h.title(),
         "published_at": published,
         "variants": [{"id": 900 + i, "title": "M", "available": True,
                       "price": "245.00"}]}
        for i, h in enumerate(handles)]}


@respx.mock
async def test_a_drop_reports_how_far_behind_the_store_we_were(sent):
    """The number that turns 'the alert was late' into a measurement."""
    from datetime import datetime, timedelta, timezone

    wid = db.create_watch(name="Satisfy", brand="Satisfy",
                          url=f"{STORE}/collections/new", strategy="shopify",
                          kind="collection", target_ref="new")
    route = respx.get(url__startswith=f"{STORE}/collections/new/products.json")

    route.mock(return_value=httpx.Response(200, json=_catalogue(["old"])))
    await scheduler.check_watch(db.get_watch(wid))

    listed = datetime.now(timezone.utc) - timedelta(minutes=4)
    route.mock(return_value=httpx.Response(200, json=_catalogue(
        ["old", "fresh"], published=listed.isoformat())))
    await scheduler.check_watch(db.get_watch(wid))

    lag = sent[0]["payload"]["listed_ago_s"]
    assert 200 <= lag <= 320, "should read about four minutes behind"


@respx.mock
async def test_an_unknown_publish_time_makes_no_claim(sent):
    """A missing timestamp must read as unknown, never as zero lag."""
    wid = db.create_watch(name="Satisfy", brand="Satisfy",
                          url=f"{STORE}/collections/new", strategy="shopify",
                          kind="collection", target_ref="new")
    route = respx.get(url__startswith=f"{STORE}/collections/new/products.json")

    route.mock(return_value=httpx.Response(200, json=_catalogue(["old"])))
    await scheduler.check_watch(db.get_watch(wid))
    route.mock(return_value=httpx.Response(200, json=_catalogue(["old", "new1"])))
    await scheduler.check_watch(db.get_watch(wid))

    assert "listed_ago_s" not in sent[0]["payload"]


@respx.mock
async def test_a_rate_limit_no_longer_defers_the_next_poll_by_ten_minutes():
    """The exact sequence that cost sixteen minutes: a 429 at the five-minute
    tier used to push the next check out by another ten."""
    from monitor.timeutil import parse, utcnow

    wid = db.create_watch(name="Satisfy", brand="Satisfy",
                          url=f"{STORE}/collections/new", strategy="shopify",
                          kind="collection", target_ref="new",
                          base_interval_s=300)
    respx.get(url__startswith=f"{STORE}/collections/new/products.json").mock(
        return_value=httpx.Response(429))

    await scheduler.check_watch(db.get_watch(wid))

    w = db.get_watch(wid)
    gap = (parse(w["next_check_at"]) - utcnow()).total_seconds()
    assert gap < 9 * 60, f"a single 429 deferred the next poll by {gap:.0f}s"
    assert w["enabled"] == 1, "and a 429 still never pauses a healthy watch"


@respx.mock
async def test_clean_checks_speed_a_watch_back_up():
    wid = db.create_watch(name="Satisfy", brand="Satisfy",
                          url=f"{STORE}/collections/new", strategy="shopify",
                          kind="collection", target_ref="new",
                          base_interval_s=300)
    respx.get(url__startswith=f"{STORE}/collections/new/products.json").mock(
        return_value=httpx.Response(200, json=_catalogue(["a"])))

    await scheduler.check_watch(db.get_watch(wid))

    assert db.get_watch(wid)["base_interval_s"] < 300


@respx.mock
async def test_arming_a_watch_does_not_permanently_reset_its_cadence():
    """Arming is temporary. The controller learns the base cadence, so feeding
    it the armed interval would make a one-hour arming permanent."""
    from monitor.timeutil import stamp_in

    wid = db.create_watch(name="Satisfy", brand="Satisfy",
                          url=f"{STORE}/collections/new", strategy="shopify",
                          kind="collection", target_ref="new",
                          base_interval_s=300, hot_interval_s=45,
                          hot_until=stamp_in(3600))
    respx.get(url__startswith=f"{STORE}/collections/new/products.json").mock(
        return_value=httpx.Response(200, json=_catalogue(["a"])))

    await scheduler.check_watch(db.get_watch(wid))

    w = db.get_watch(wid)
    assert w["base_interval_s"] > 45, \
        "the armed 45s must not be written back as the normal cadence"
    assert w["base_interval_s"] < 300, "the base still learns from a clean check"
