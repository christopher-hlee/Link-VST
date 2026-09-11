"""Alerts must describe the store, not our own memory.

Every case here is taken from a real false positive. The app reported a Pertex
Windbreaker that had been on sale for 329 days, a COROS watch listed 23 days
earlier, and a sold-out MothTech shirt whose publish date had been reset by a
relist. All three were "new" only in the sense that our baseline had not seen
them — which says nothing whatsoever about the store.
"""
import json

import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.statemachine import (
    ARRIVAL_KNOWN, ARRIVAL_NEW, ARRIVAL_RELISTED, ARRIVAL_UNCONFIRMED,
    classify_arrival,
)
from monitor.timeutil import stamp, utcnow
from datetime import timedelta

STORE = "https://satisfyrunning.com"
COLLECTION = f"{STORE}/collections/shop-all"


def ago(**kw):
    return (utcnow() - timedelta(**kw)).isoformat()


def item(published=None, created=None):
    return {"published_at": published, "created_at": created}


# --- the three alerts that prompted this, as a table -----------------------

@pytest.mark.parametrize("name,published,created,expected", [
    # "listed 7890h before this alert" — 329 days on sale.
    ("Pertex Diamond Fuse Windbreaker", ago(days=329), ago(days=331), ARRIVAL_KNOWN),
    # "listed 546h before this alert" — 23 days.
    ("SATISFY COROS APEX 4", ago(days=23), ago(days=25), ARRIVAL_KNOWN),
    # "listed 6m before this alert", but an old sold-out item: Shopify reset
    # published_at when it was put back on the site.
    ("MothTech LEVI'S T-Shirt", ago(minutes=6), ago(days=400), ARRIVAL_RELISTED),
    # What a genuine drop looks like.
    ("PeaceShell Technical Climb Pants", ago(minutes=2), ago(minutes=2), ARRIVAL_NEW),
])
def test_the_real_alerts_are_classified_correctly(name, published, created, expected):
    window = utcnow() - timedelta(minutes=30)

    assert classify_arrival(item(published, created),
                            window_start=window, now=utcnow()) == expected, name


def test_a_product_staged_before_launch_is_still_a_new_release():
    """Brands create a product days before publishing it. That gap is normal
    and must not be mistaken for a relist, or every planned drop reads wrong."""
    window = utcnow() - timedelta(minutes=30)

    assert classify_arrival(item(published=ago(minutes=1), created=ago(days=6)),
                            window_start=window, now=utcnow()) == ARRIVAL_NEW


def test_no_dates_at_all_is_admitted_not_guessed():
    window = utcnow() - timedelta(minutes=30)

    assert classify_arrival(item(), window_start=window,
                            now=utcnow()) == ARRIVAL_UNCONFIRMED


# --- end to end -------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    calls = []

    async def fake_send(watch, kind, payload):
        calls.append({"kind": kind, "payload": payload})

    monkeypatch.setattr("monitor.notify.telegram.send_event", fake_send)
    monkeypatch.setattr("monitor.notify.telegram.configured", lambda: True)
    return calls


def catalogue(specs):
    """specs: [(handle, published, created)]"""
    return {"products": [
        {"handle": h, "title": h.replace("-", " ").title(),
         "published_at": p, "created_at": c,
         "variants": [{"id": 900 + i, "title": "M", "available": True,
                       "price": "245.00"}]}
        for i, (h, p, c) in enumerate(specs)]}


def drop_watch(**kw):
    fields = dict(name="satisfyrunning.com · shop-all", brand="satisfyrunning.com",
                  url=COLLECTION, strategy="shopify", kind="collection",
                  target_ref="shop-all", base_interval_s=300)
    fields.update(kw)
    return db.create_watch(**fields)


@respx.mock
async def test_an_old_product_entering_view_is_absorbed_in_silence(sent):
    """The exact bug. A catalogue bigger than one page, a collection that
    re-sorts, or a widened sweep all put unseen-but-old products in front of
    us. None of them are releases."""
    wid = drop_watch(baseline_json=json.dumps(["old-1"]),
                     last_sweep_at=stamp(), last_state="watching")
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(200, json=catalogue([
            ("old-1", ago(days=200), ago(days=200)),
            ("pertex-diamond-fuse-windbreaker", ago(days=329), ago(days=331)),
        ])))

    await scheduler.check_watch(db.get_watch(wid))

    assert sent == [], "a 329-day-old product is not a new release"
    baseline = json.loads(db.get_watch(wid)["baseline_json"])
    assert "pertex-diamond-fuse-windbreaker" in baseline, \
        "but it must be remembered, so it cannot alert later either"


@respx.mock
async def test_a_genuine_drop_still_alerts(sent):
    wid = drop_watch(baseline_json=json.dumps(["old-1"]),
                     last_sweep_at=stamp(), last_state="watching")
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(200, json=catalogue([
            ("old-1", ago(days=200), ago(days=200)),
            ("climb-pants", ago(minutes=1), ago(minutes=1)),
        ])))

    await scheduler.check_watch(db.get_watch(wid))

    assert [c["kind"] for c in sent] == ["new_product"]
    assert sent[0]["payload"]["handles"] == ["climb-pants"]
    assert sent[0]["payload"]["arrival"] == ARRIVAL_NEW


@respx.mock
async def test_a_relist_alerts_but_says_what_it_is(sent):
    wid = drop_watch(baseline_json=json.dumps(["old-1"]),
                     last_sweep_at=stamp(), last_state="watching")
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(200, json=catalogue([
            ("old-1", ago(days=200), ago(days=200)),
            ("mothtech-levis-tee", ago(minutes=6), ago(days=400)),
        ])))

    await scheduler.check_watch(db.get_watch(wid))

    assert [c["kind"] for c in sent] == ["new_product"]
    assert sent[0]["payload"]["arrival"] == ARRIVAL_RELISTED


@respx.mock
async def test_the_flood_this_replaces(sent):
    """A catalogue whose visible window slides, sweep after sweep — the model
    of what has been happening. Eighty unseen old products arrive over forty
    sweeps and not one of them is an alert."""
    seen = ["p0"]
    wid = drop_watch(baseline_json=json.dumps(seen),
                     last_sweep_at=stamp(), last_state="watching")
    route = respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json")

    for i in range(1, 41):
        # Each sweep reveals two more products that have been on sale for
        # months, exactly as a re-sorting collection does.
        specs = [(f"p{j}", ago(days=100 + j), ago(days=101 + j))
                 for j in range(0, i * 2)]
        route.mock(return_value=httpx.Response(200, json=catalogue(specs)))
        await scheduler.check_watch(db.get_watch(wid))

    assert sent == [], f"{len(sent)} false positives over forty sweeps"
    assert len(json.loads(db.get_watch(wid)["baseline_json"])) == 80


@respx.mock
async def test_a_drop_missed_during_an_outage_still_alerts_on_recovery(sent):
    """The window runs from the last successful sweep, so three days of
    downtime does not silently eat the releases that happened during it."""
    wid = drop_watch(baseline_json=json.dumps(["old-1"]),
                     last_sweep_at=stamp(utcnow() - timedelta(days=3)),
                     last_state="watching")
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(200, json=catalogue([
            ("old-1", ago(days=200), ago(days=200)),
            ("dropped-while-we-were-down", ago(days=2), ago(days=2)),
        ])))

    await scheduler.check_watch(db.get_watch(wid))

    assert [c["payload"]["handles"] for c in sent] == [["dropped-while-we-were-down"]]


@respx.mock
async def test_the_first_ever_sweep_never_alerts(sent):
    wid = drop_watch()
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(200, json=catalogue([
            ("a", ago(minutes=1), ago(minutes=1)),
            ("b", ago(days=300), ago(days=300)),
        ])))

    await scheduler.check_watch(db.get_watch(wid))

    assert sent == [], "adopting a catalogue is not a drop"
    assert db.get_watch(wid)["last_sweep_at"] is not None


@respx.mock
async def test_a_failed_check_does_not_close_the_window(sent):
    """last_checked_at moves on failures; last_sweep_at must not. Otherwise a
    watch that fails for days ends up with a window of seconds."""
    wid = drop_watch(baseline_json=json.dumps(["old-1"]),
                     last_sweep_at=stamp(utcnow() - timedelta(days=2)),
                     last_state="watching")
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(500))
    respx.get(url__startswith=f"{STORE}/collections/shop-all.atom").mock(
        return_value=httpx.Response(500))

    await scheduler.check_watch(db.get_watch(wid))

    w = db.get_watch(wid)
    assert w["last_checked_at"] is not None
    assert scheduler.parse(w["last_sweep_at"]) < utcnow() - timedelta(days=1)


# --- the sweep has to be honest about what it saw --------------------------

@respx.mock
async def test_a_store_that_ignores_page_cannot_be_mistaken_for_a_full_read():
    """If `&page=` is ignored, every page is the same 250 products. Counting
    rows would say 1000 and hide that we still see only a 250-item window."""
    from monitor.strategies import shopify

    page = catalogue([(f"p{i}", ago(days=10), ago(days=10)) for i in range(250)])
    route = respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json")
    route.mock(return_value=httpx.Response(200, json=page))

    r = await shopify.check({"id": 1, "url": COLLECTION, "kind": "collection",
                             "target_ref": "shop-all"})

    assert len(r.handles) == 250, "duplicates must not inflate the catalogue"
    assert len(set(r.handles)) == 250
    assert r.extra["product_count"] == 250
    assert r.extra["rows_read"] > 250, "and the raw count still exposes the repeat"


# --- the alert has to say which kind of arrival it is ----------------------

@pytest.mark.parametrize("arrival,must_say,must_not_say", [
    ("new", "1 new from", "relisted"),
    ("relisted", "relisted", "1 new from"),
    ("unconfirmed", "cannot confirm", "Just listed"),
])
def test_the_alert_names_the_kind_of_arrival(arrival, must_say, must_not_say):
    from monitor.notify import telegram

    body = telegram.render(
        {"name": "Satisfy", "brand": "satisfyrunning.com"}, "new_product",
        {"handles": ["a"], "titles": {"a": "MothTech Tee"},
         "baseline_count": 333, "arrival": arrival, "listed_ago_s": 360})

    assert must_say in body
    assert must_not_say not in body


def test_a_relist_does_not_claim_it_was_just_listed():
    """The timestamp is when it came back, not when it was first sold."""
    from monitor.notify import telegram

    body = telegram.render(
        {"name": "Satisfy", "brand": "satisfyrunning.com"}, "new_product",
        {"handles": ["a"], "titles": {"a": "MothTech Tee"},
         "baseline_count": 333, "arrival": "relisted", "listed_ago_s": 360})

    assert "relisted 6m before this alert" in body
    assert "listed 6m before this alert" not in body.replace("relisted", "")
