"""Detection latency — the property the whole app exists to deliver.

A Satisfy drop was caught by hand sixteen minutes before the alert arrived.
These tests pin the arithmetic that allowed that, so it cannot come back
quietly: they assert latency *ceilings* in seconds rather than the shape of the
formula, so the implementation stays free to change.
"""
import httpx
import pytest
import respx

from monitor.statemachine import (
    INTERVAL_CEILING, INTERVAL_FLOOR, adapt_interval,
)
from monitor.strategies import shopify

STORE = "https://satisfyrunning.com"


def collection_watch(**kw):
    return {"id": 2, "url": f"{STORE}/collections/shop-all",
            "kind": "collection", "target_ref": "shop-all", **kw}


# --- backing off must not cost ten minutes ---------------------------------

def test_one_rate_limit_no_longer_costs_a_ten_minute_blind_spot():
    """The regression that produced the sixteen-minute miss.

    The old rule was `interval * 2`, so a single 429 at the five-minute tier
    deferred the next poll by ten minutes. Expressed as a ceiling, because what
    matters is the wait, not how it is computed.
    """
    after = adapt_interval(300, rate_limited=True)

    assert after < 600, "a single 429 must not double the polling period"
    assert after <= 8 * 60
    assert after > 300, "but it must still actually slow down"


def test_retry_after_is_obeyed_when_the_store_states_its_terms():
    assert adapt_interval(60, rate_limited=True, retry_after=120) == 120


def test_retry_after_never_speeds_us_up_past_the_current_rate():
    """A short Retry-After is the store's floor, not an invitation."""
    assert adapt_interval(300, rate_limited=True, retry_after=5) == 300


def test_a_clean_check_earns_speed_back():
    assert adapt_interval(300, rate_limited=False) < 300


def test_a_slowed_watch_recovers_on_its_own():
    """Otherwise one bad afternoon leaves a watch slow forever."""
    interval = adapt_interval(300, rate_limited=True)
    for _ in range(200):
        interval = adapt_interval(interval, rate_limited=False)
    assert interval == INTERVAL_FLOOR


def test_the_interval_stays_inside_its_bounds_under_abuse():
    """Alternating success and rate limits must not walk out of range."""
    interval = 300
    for i in range(500):
        interval = adapt_interval(interval, rate_limited=(i % 3 == 0),
                                  retry_after=(9999 if i % 50 == 0 else None))
        assert INTERVAL_FLOOR <= interval <= INTERVAL_CEILING


def test_sustained_rate_limiting_never_silences_a_watch():
    interval = INTERVAL_FLOOR
    for _ in range(100):
        interval = adapt_interval(interval, rate_limited=True)
    assert interval == INTERVAL_CEILING, "capped, not unbounded"


# --- the catalogue must not be seen through a 250-item window --------------

def _page(n, start=0):
    return {"products": [
        {"handle": f"p{start + i}", "title": f"P{start + i}",
         "variants": [{"id": 1000 + start + i, "title": "M",
                       "available": True, "price": "10.00"}]}
        for i in range(n)]}


@respx.mock
async def test_a_catalogue_larger_than_one_page_is_seen_whole():
    """257 products were only ever read through a 250-item window, so a new
    item sorted outside it was invisible until the ordering shifted."""
    route = respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json")
    route.side_effect = [
        httpx.Response(200, json=_page(250)),
        httpx.Response(200, json=_page(7, start=250)),
    ]

    r = await shopify.check(collection_watch())

    assert len(r.handles) == 257
    assert "p256" in r.handles, "the tail of the catalogue must be visible"


@respx.mock
async def test_paging_stops_on_a_short_page():
    route = respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json")
    route.side_effect = [httpx.Response(200, json=_page(3))]

    r = await shopify.check(collection_watch())

    assert len(r.handles) == 3
    assert route.call_count == 1, "a short first page means there is no page 2"


@respx.mock
async def test_paging_stops_rather_than_pushing_a_rate_limited_host():
    """A 429 on page 2 must not become a retry storm; a short read is fine."""
    route = respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json")
    route.side_effect = [
        httpx.Response(200, json=_page(250)),
        httpx.Response(429, headers={"Retry-After": "30"}),
    ]

    r = await shopify.check(collection_watch())

    assert r.ok, "a partial sweep is still a usable sweep"
    assert len(r.handles) == 250


@respx.mock
async def test_paging_is_capped():
    route = respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json")
    route.side_effect = [httpx.Response(200, json=_page(250))
                         for _ in range(shopify.MAX_PAGES + 3)]

    await shopify.check(collection_watch())

    assert route.call_count == shopify.MAX_PAGES


# --- the lag has to be measured, not argued about --------------------------

@respx.mock
async def test_the_store_publish_time_is_captured():
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(200, json={"products": [{
            "handle": "climb", "title": "Climb Pants",
            "published_at": "2026-09-02T12:00:00-04:00",
            "variants": [{"id": 1, "title": "M", "available": True,
                          "price": "245.00"}]}]}))

    r = await shopify.check(collection_watch())

    assert r.extra["items"]["climb"]["published_at"] == "2026-09-02T12:00:00-04:00"


@respx.mock
async def test_created_at_stands_in_when_there_is_no_publish_time():
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(200, json={"products": [
            {"handle": "x", "title": "X", "created_at": "2026-09-02T10:00:00Z",
             "variants": []}]}))

    r = await shopify.check(collection_watch())

    assert r.extra["items"]["x"]["published_at"] == "2026-09-02T10:00:00Z"


# --- the alert has to state the lag ----------------------------------------

def test_the_alert_states_how_far_behind_the_store_it_is():
    from monitor.notify import telegram

    body = telegram.render(
        {"name": "Satisfy", "brand": "satisfyrunning.com"}, "new_product",
        {"handles": ["a"], "titles": {"a": "Climb Pants"},
         "baseline_count": 256, "listed_ago_s": 47})

    assert "listed 47s before this alert" in body


def test_the_alert_claims_no_lag_it_cannot_measure():
    from monitor.notify import telegram

    body = telegram.render(
        {"name": "Satisfy", "brand": "satisfyrunning.com"}, "new_product",
        {"handles": ["a"], "titles": {"a": "Climb Pants"}, "baseline_count": 256})

    assert "listed" not in body, "an unknown lag must not render as zero"


def test_the_alert_does_not_invent_a_polling_interval():
    """It used to say "an hour ago" regardless of the actual cadence."""
    from monitor.notify import telegram

    body = telegram.render(
        {"name": "Satisfy", "brand": "Satisfy"}, "new_product",
        {"handles": ["a"], "titles": {"a": "X"}, "baseline_count": 10})

    assert "an hour ago" not in body
