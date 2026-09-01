"""Rate limiting.

A 429 is the host asking for fewer requests. Everything here exists because the
app's first response to that was to send more: it retried the 429 twice, then
fell back to a second endpoint which retried twice again — six requests to a
server that had just said slow down — and then counted the result as a broken
watch and paused it. A recoverable condition turned into a self-inflicted outage.
"""
import httpx
import pytest
import respx

from monitor import db, fetcher, scheduler
from monitor.statemachine import (
    IN_STOCK, WATCHING, CheckResult, decide,
)
from monitor.strategies import shopify

STORE = "https://satisfyrunning.com"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


# --- the amplification bugs ------------------------------------------------

@respx.mock
async def test_a_429_is_requested_exactly_once():
    """Retrying a rate limit is the opposite of complying with it."""
    route = respx.get(f"{STORE}/products/x.js").mock(
        return_value=httpx.Response(429))

    r = await fetcher.fetch(f"{STORE}/products/x.js")

    assert route.call_count == 1, f"sent {route.call_count} requests to a 429"
    assert r.rate_limited and r.status == 429


@respx.mock
async def test_server_errors_still_get_one_retry():
    """A 500 is a fault, not backpressure — retrying it is reasonable."""
    route = respx.get(f"{STORE}/products/x.js").mock(
        return_value=httpx.Response(503))

    await fetcher.fetch(f"{STORE}/products/x.js", retries=1)

    assert route.call_count == 2


@respx.mock
async def test_collection_does_not_fall_back_to_atom_on_a_429():
    """The fallback is for a gated endpoint, not for backpressure."""
    json_route = respx.get(url__startswith=f"{STORE}/collections/all/products.json").mock(
        return_value=httpx.Response(429))
    atom_route = respx.get(f"{STORE}/collections/all.atom").mock(
        return_value=httpx.Response(200, text="<feed/>"))

    r = await shopify.check({"id": 1, "url": f"{STORE}/collections/all",
                             "kind": "collection", "target_ref": "all"})

    assert json_route.call_count == 1
    assert atom_route.call_count == 0, "piled a second request onto a rate limit"
    assert r.rate_limited


@respx.mock
async def test_a_gated_endpoint_still_falls_back():
    """403 means try the other door; 429 means wait. Don't conflate them."""
    respx.get(url__startswith=f"{STORE}/collections/all/products.json").mock(
        return_value=httpx.Response(403))
    atom = respx.get(f"{STORE}/collections/all.atom").mock(
        return_value=httpx.Response(200, text=(
            '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            '<entry><title>T</title><link href="' + STORE + '/products/t"/></entry></feed>')))

    r = await shopify.check({"id": 1, "url": f"{STORE}/collections/all",
                             "kind": "collection", "target_ref": "all"})

    assert atom.call_count == 1
    assert r.ok and r.handles == ["t"]


# --- a rate limit is not a broken watch ------------------------------------

def test_rate_limit_changes_nothing():
    d = decide(kind="product", prev_state=IN_STOCK, prev_failures=2,
               prev_baseline=["a"], prev_price=185.0,
               result=CheckResult(ok=False, rate_limited=True, retry_after=90))

    assert d.state == IN_STOCK, "a rate limit says nothing about stock"
    assert d.failures == 2, "and nothing about the watch's health"
    assert d.baseline == ["a"] and d.price == 185.0
    assert d.pause is False
    assert d.events == []
    assert d.defer_seconds == 90


def test_repeated_rate_limits_never_pause_a_watch():
    """The reported failure: five 429s in a row paused a healthy watch."""
    failures = 0
    for _ in range(10):
        d = decide(kind="collection", prev_state=WATCHING, prev_failures=failures,
                   prev_baseline=["a"], prev_price=None,
                   result=CheckResult(ok=False, rate_limited=True))
        failures = d.failures
        assert d.pause is False
    assert failures == 0, "429s must not accumulate toward the pause threshold"


def test_retry_after_is_parsed_and_bad_values_ignored():
    assert fetcher.parse_retry_after("120") == 120.0
    assert fetcher.parse_retry_after("0") == 0.0
    assert fetcher.parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert fetcher.parse_retry_after(None) is None


# --- the scheduler backs off -----------------------------------------------

@respx.mock
async def test_scheduler_defers_instead_of_failing(monkeypatch):
    monkeypatch.setattr("monitor.notify.heartbeat.ping", lambda suffix="": None)
    wid = db.create_watch(name="Satisfy", brand="Satisfy",
                          url=f"{STORE}/collections/all", strategy="shopify",
                          kind="collection", target_ref="all",
                          base_interval_s=45, last_state=WATCHING,
                          baseline_json='["a"]')
    respx.get(url__startswith=f"{STORE}/collections/all/products.json").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "600"}))

    await scheduler.check_watch(db.get_watch(wid))

    w = db.get_watch(wid)
    assert w["consecutive_failures"] == 0, "not a failure"
    assert w["enabled"] == 1, "must not pause itself"
    assert w["last_state"] == WATCHING, "state preserved"
    assert "rate limited" in (w["last_error"] or "")
    assert w["next_check_at"] > w["last_checked_at"], "next poll pushed out"


@respx.mock
async def test_backoff_is_at_least_double_the_interval(monkeypatch):
    """Even with no Retry-After, polling at the same cadence would re-trigger it."""
    monkeypatch.setattr("monitor.notify.heartbeat.ping", lambda suffix="": None)
    wid = db.create_watch(name="S", url=f"{STORE}/products/x", strategy="shopify",
                          kind="product", target_ref="x", base_interval_s=45)
    respx.get(f"{STORE}/products/x.js").mock(return_value=httpx.Response(429))

    await scheduler.check_watch(db.get_watch(wid))

    from monitor.timeutil import parse, utcnow
    gap = (parse(db.get_watch(wid)["next_check_at"]) - utcnow()).total_seconds()
    assert gap > 45, f"backed off only {gap:.0f}s from a 45s interval"
