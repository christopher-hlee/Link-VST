"""A partial read is not a sweep.

The catalogue is paged, and paging can stop for two very different reasons:
the store ran out of products, or we ran out of permission to keep asking.
Treating the second as a complete sweep loses drops in silence — the tail we
never fetched stays out of the baseline while the window advances past it, so
the products there read as old whenever they finally become visible. This is
the same failure as a 304 answering for a page that was never requested.
"""
import json
from datetime import timedelta

import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.strategies import shopify
from monitor.strategies.shopify import MAX_PAGES, PAGE_SIZE
from monitor.timeutil import parse, stamp, utcnow

STORE = "https://satisfyrunning.com"
COLLECTION = f"{STORE}/collections/shop-all"
FEED = f"{STORE}/collections/shop-all/products.json"


def page_of(n, start=0):
    return {"products": [
        {"handle": f"p{i}", "title": f"P{i}",
         "published_at": "2025-01-01T00:00:00Z", "created_at": "2025-01-01T00:00:00Z",
         "variants": [{"id": i, "title": "M", "available": True, "price": "1.00"}]}
        for i in range(start, start + n)]}


def watch_row(**kw):
    fields = dict(name="shop-all", brand="satisfyrunning.com", url=COLLECTION,
                  strategy="shopify", kind="collection", target_ref="shop-all",
                  last_state="watching")
    fields.update(kw)
    return db.create_watch(**fields)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


@respx.mock
async def test_a_catalogue_that_ends_is_a_complete_sweep():
    respx.get(url__startswith=FEED).mock(
        return_value=httpx.Response(200, json=page_of(40)))

    r = await shopify.check({"id": 1, "url": COLLECTION, "kind": "collection",
                             "target_ref": "shop-all"})

    assert r.extra["truncated"] is False


@respx.mock
async def test_hitting_our_own_page_cap_is_reported_as_truncated():
    """Every page full, right up to the cap: the store has more and we stopped
    asking. The ceiling used to be 1000 products and silent about it."""
    respx.get(url__startswith=FEED).mock(
        side_effect=lambda req: httpx.Response(200, json=page_of(PAGE_SIZE)))

    r = await shopify.check({"id": 1, "url": COLLECTION, "kind": "collection",
                             "target_ref": "shop-all"})

    assert r.extra["truncated"] is True
    assert r.ok, "a partial read is still usable; it is just not complete"


@respx.mock
async def test_a_rate_limit_mid_page_is_truncated_not_finished():
    calls = {"n": 0}

    def answer(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=page_of(PAGE_SIZE))
        return httpx.Response(429, headers={"Retry-After": "5"})

    respx.get(url__startswith=FEED).mock(side_effect=answer)

    r = await shopify.check({"id": 1, "url": COLLECTION, "kind": "collection",
                             "target_ref": "shop-all"})

    assert r.extra["truncated"] is True


@respx.mock
async def test_a_truncated_read_does_not_close_the_sweep_window():
    """The point of the whole file. The window must still cover the products
    we never reached."""
    old = stamp(utcnow() - timedelta(days=2))
    wid = watch_row(baseline_json=json.dumps(["p0"]), last_sweep_at=old)
    respx.get(url__startswith=FEED).mock(
        side_effect=lambda req: httpx.Response(200, json=page_of(PAGE_SIZE)))

    await scheduler.check_watch(db.get_watch(wid))

    w = db.get_watch(wid)
    assert w["last_sweep_at"] == old, "a partial read must not claim the sweep"
    assert w["last_checked_at"] is not None, "but the check itself did happen"


@respx.mock
async def test_a_complete_read_does_close_it():
    wid = watch_row(baseline_json=json.dumps(["p0"]),
                    last_sweep_at=stamp(utcnow() - timedelta(days=2)))
    respx.get(url__startswith=FEED).mock(
        return_value=httpx.Response(200, json=page_of(10)))

    await scheduler.check_watch(db.get_watch(wid))

    assert parse(db.get_watch(wid)["last_sweep_at"]) > utcnow() - timedelta(minutes=1)


@respx.mock
async def test_the_drop_beyond_the_cap_is_not_absorbed_in_silence():
    """End to end: a truncated sweep, then a complete one revealing a product
    published during the gap. It must still alert."""
    wid = watch_row(baseline_json=json.dumps([f"p{i}" for i in range(PAGE_SIZE)]),
                    last_sweep_at=stamp(utcnow() - timedelta(hours=6)))
    sent = []

    async def fake(watch, kind, payload):
        sent.append(payload)

    import monitor.notify.telegram as tg
    tg.send_event, tg.configured = fake, (lambda: True)

    route = respx.get(url__startswith=FEED)
    route.side_effect = lambda req: httpx.Response(200, json=page_of(PAGE_SIZE))
    await scheduler.check_watch(db.get_watch(wid))

    fresh = (utcnow() - timedelta(minutes=2)).isoformat()
    complete = page_of(PAGE_SIZE)
    complete["products"].append(
        {"handle": "drop", "title": "Drop", "published_at": fresh,
         "created_at": fresh,
         "variants": [{"id": 1, "title": "M", "available": True, "price": "1.00"}]})
    route.side_effect = None
    route.mock(return_value=httpx.Response(200, json=complete))
    await scheduler.check_watch(db.get_watch(wid))

    assert [p["handles"] for p in sent] == [["drop"]]


def test_the_ceiling_is_well_clear_of_the_catalogue_we_watch():
    """717 products against a 1000 ceiling was 72% of the way to a silent
    truncation, with no signal that it was close."""
    assert MAX_PAGES * PAGE_SIZE >= 2500


# --- what the number on screen means ---------------------------------------

@respx.mock
async def test_the_live_count_is_recorded_so_drift_is_visible():
    """The baseline unions and never shrinks. Without the live figure beside
    it there is no way to tell 717 on sale from 717 remembered."""
    wid = watch_row(baseline_json=json.dumps([f"p{i}" for i in range(40)]))
    respx.get(url__startswith=FEED).mock(
        return_value=httpx.Response(200, json=page_of(30)))

    await scheduler.check_watch(db.get_watch(wid))

    w = db.get_watch(wid)
    assert len(json.loads(w["baseline_json"])) == 40, "memory keeps the retired ones"
    assert w["last_seen_count"] == 30, "the store is serving thirty"


@respx.mock
async def test_a_truncated_read_does_not_publish_a_count_it_cannot_stand_behind():
    wid = watch_row(baseline_json=json.dumps(["p0"]), last_seen_count=700)
    respx.get(url__startswith=FEED).mock(
        side_effect=lambda req: httpx.Response(200, json=page_of(PAGE_SIZE)))

    await scheduler.check_watch(db.get_watch(wid))

    assert db.get_watch(wid)["last_seen_count"] == 700, "the old figure stands"
