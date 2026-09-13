"""Will a real drop actually reach the phone, and inside a minute?

Written after a silent week. Silence is ambiguous — a monitor with nothing to
report and a monitor that has quietly stopped working look exactly the same
from the outside — so each way a drop could vanish gets a test that fails
loudly instead.
"""
from datetime import timedelta

import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.config import JITTER_FRACTION, TICK_SECONDS
from monitor.statemachine import (
    ARRIVAL_NEW, INTERVAL_FLOOR, classify_arrival,
)
from monitor.strategies import shopify
from monitor.timeutil import parse_instant, stamp, utcnow

STORE = "https://satisfyrunning.com"
COLLECTION = f"{STORE}/collections/shop-all"

# Shopify serves published_at in the SHOP's timezone with the offset attached.
# Every one of these describes the same instant.
OFFSETS = ["+00:00", "-04:00", "-07:00", "-08:00", "+02:00", "+09:00"]


def published_now(offset: str) -> str:
    hours = int(offset[1:3]) * (1 if offset[0] == "+" else -1)
    local = utcnow() + timedelta(hours=hours)
    return local.strftime("%Y-%m-%dT%H:%M:%S") + offset


# --- a drop must not be lost to a timezone --------------------------------

@pytest.mark.parametrize("offset", OFFSETS)
def test_a_drop_is_seen_whatever_timezone_the_shop_uses(offset):
    """The regression that made the app go quiet.

    parse() truncates at 19 characters, which throws the offset away: a product
    published this minute by a New York shop read as four hours old, landed
    outside the sweep window, and was absorbed in silence. Permanently.
    """
    raw = published_now(offset)
    window = utcnow() - timedelta(minutes=15)

    assert classify_arrival({"published_at": raw, "created_at": raw},
                            window_start=window, now=utcnow()) == ARRIVAL_NEW, \
        f"a drop from a {offset} shop was silently swallowed"


@pytest.mark.parametrize("offset", OFFSETS)
def test_the_offset_is_applied_rather_than_discarded(offset):
    drift = abs((parse_instant(published_now(offset)) - utcnow()).total_seconds())
    assert drift < 5, f"{offset} parsed {drift/3600:.0f}h away from the truth"


def test_our_own_database_stamps_still_parse():
    assert parse_instant("2026-09-13 02:36:47") is not None


# --- a drop must not be lost behind a conditional request ------------------

def test_a_multi_page_catalogue_does_not_trust_a_page_one_validator():
    """An ETag validates page one, not the catalogue. With 333 products a new
    item can land on page two while page one is byte-identical: the store says
    304, page two is never fetched, and the window still advances — so the
    product reads as old whenever it finally becomes visible."""
    big = {"baseline_json": "[" + ",".join(f'"p{i}"' for i in range(333)) + "]"}
    small = {"baseline_json": '["a", "b"]'}

    assert shopify._multi_page(big) is True
    assert shopify._multi_page(small) is False


@respx.mock
async def test_a_paged_store_is_never_sent_a_conditional_header():
    baseline = "[" + ",".join(f'"p{i}"' for i in range(333)) + "]"
    seen = {}

    def record(request):
        seen.update(request.headers)
        return httpx.Response(200, json={"products": []})

    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        side_effect=record)

    await shopify.check({"id": 1, "url": COLLECTION, "kind": "collection",
                         "target_ref": "shop-all", "baseline_json": baseline,
                         "etag": 'W/"abc"', "last_modified": "Mon, 1 Sep 2026 00:00:00 GMT"})

    assert "if-none-match" not in seen, "304 would hide page two entirely"
    assert "if-modified-since" not in seen


@respx.mock
async def test_a_single_page_store_still_gets_the_cheap_path():
    """Conditional requests are still worth having where they are honest."""
    seen = {}

    def record(request):
        seen.update(request.headers)
        return httpx.Response(304)

    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        side_effect=record)

    r = await shopify.check({"id": 1, "url": COLLECTION, "kind": "collection",
                             "target_ref": "shop-all",
                             "baseline_json": '["a"]', "etag": 'W/"abc"'})

    assert seen.get("if-none-match") == 'W/"abc"'
    assert r.not_modified is True


# --- and it has to be fast enough to matter -------------------------------

def test_the_worst_case_alert_arrives_inside_a_minute():
    """Asserted as a budget in seconds, so any future change to the polling
    floor or the tick that breaks the promise fails here rather than in the
    field during a drop."""
    poll_gap = INTERVAL_FLOOR * (1 + JITTER_FRACTION)
    network_and_delivery = 5.0          # generous: 2 pages fetched + Telegram

    worst = poll_gap + TICK_SECONDS + network_and_delivery

    assert worst < 60, f"worst-case detection is {worst:.0f}s"


def test_the_tick_never_dominates_the_polling_floor():
    assert TICK_SECONDS <= INTERVAL_FLOOR / 4, \
        "scheduler granularity would be a large share of total latency"


# --- end to end, with a real-world timestamp ------------------------------

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


@respx.mock
async def test_a_new_york_shop_dropping_right_now_reaches_the_phone(sent):
    """The full path, with the timestamp format that actually broke it."""
    import json as _json
    wid = db.create_watch(
        name="satisfyrunning.com · shop-all", brand="satisfyrunning.com",
        url=COLLECTION, strategy="shopify", kind="collection",
        target_ref="shop-all", last_state="watching", base_interval_s=35,
        baseline_json=_json.dumps([f"p{i}" for i in range(333)]),
        last_sweep_at=stamp())

    old = [{"handle": f"p{i}", "title": f"P{i}",
            "published_at": "2025-01-01T00:00:00-04:00",
            "created_at": "2025-01-01T00:00:00-04:00",
            "variants": [{"id": i, "title": "M", "available": True,
                          "price": "100.00"}]} for i in range(333)]
    fresh = {"handle": "peaceshell-climb-pants", "title": "PeaceShell Climb Pants",
             "published_at": published_now("-04:00"),
             "created_at": published_now("-04:00"),
             "variants": [{"id": 9999, "title": "M", "available": True,
                           "price": "345.00"}]}

    route = respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json")
    route.side_effect = [
        httpx.Response(200, json={"products": old[:250]}),
        httpx.Response(200, json={"products": old[250:] + [fresh]}),
    ]

    await scheduler.check_watch(db.get_watch(wid))

    assert [c["kind"] for c in sent] == ["new_product"], \
        "a New York shop's drop must not be lost to a timezone"
    assert sent[0]["payload"]["handles"] == ["peaceshell-climb-pants"]
    assert sent[0]["payload"]["arrival"] == ARRIVAL_NEW
    lag = sent[0]["payload"].get("listed_ago_s")
    assert lag is not None and lag < 60, f"reported lag was {lag}s"
