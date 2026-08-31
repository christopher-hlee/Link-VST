"""Egress-proxy routing.

The proxy exists because retailer bot protection refuses datacenter IPs. It is
opt-in per host, so a misconfiguration must never silently reroute everything.
"""
import httpx
import pytest
import respx

from monitor import fetcher
from monitor.strategies import shopify

WORKER = "https://proxy.example.workers.dev"


@pytest.fixture
def proxied(monkeypatch):
    monkeypatch.setattr(fetcher, "PROXY_URL", WORKER)
    monkeypatch.setattr(fetcher, "PROXY_KEY", "s3cret")
    monkeypatch.setattr(fetcher, "PROXY_HOSTS", {"redsky.target.com"})


# --- routing decisions -----------------------------------------------------

def test_listed_host_is_rewritten_to_the_worker(proxied):
    url, headers = fetcher.route("https://redsky.target.com/v1/x?a=1")
    assert url.startswith(f"{WORKER}?url=")
    assert "redsky.target.com%2Fv1%2Fx%3Fa%3D1" in url, "target must be encoded"
    assert headers == {"X-Proxy-Key": "s3cret"}


def test_unlisted_host_goes_direct(proxied):
    url, headers = fetcher.route("https://satisfyrunning.com/products.json")
    assert url == "https://satisfyrunning.com/products.json"
    assert headers == {}


def test_no_proxy_configured_means_everything_direct(monkeypatch):
    monkeypatch.setattr(fetcher, "PROXY_URL", "")
    monkeypatch.setattr(fetcher, "PROXY_KEY", "")
    monkeypatch.setattr(fetcher, "PROXY_HOSTS", {"redsky.target.com"})
    url, headers = fetcher.route("https://redsky.target.com/v1/x")
    assert url == "https://redsky.target.com/v1/x" and headers == {}


def test_key_without_url_does_not_route(monkeypatch):
    """A half-configured proxy must fail open to direct, not to a broken URL."""
    monkeypatch.setattr(fetcher, "PROXY_URL", "")
    monkeypatch.setattr(fetcher, "PROXY_KEY", "s3cret")
    monkeypatch.setattr(fetcher, "PROXY_HOSTS", {"redsky.target.com"})
    url, _ = fetcher.route("https://redsky.target.com/v1/x")
    assert url == "https://redsky.target.com/v1/x"


# --- end to end through fetch() --------------------------------------------

@respx.mock
async def test_fetch_sends_proxied_request_with_key(proxied):
    route = respx.get(url__startswith=WORKER).mock(
        return_value=httpx.Response(200, json={"ok": True}))

    resp = await fetcher.fetch("https://redsky.target.com/v1/x")

    assert resp.ok and resp.json == {"ok": True}
    assert route.calls[0].request.headers["X-Proxy-Key"] == "s3cret"


@respx.mock
async def test_conditional_headers_survive_the_proxy(proxied):
    """Losing these would turn every cheap 304 poll into a full download."""
    route = respx.get(url__startswith=WORKER).mock(
        return_value=httpx.Response(304))

    resp = await fetcher.fetch("https://redsky.target.com/v1/x",
                               etag='W/"abc"')

    assert resp.not_modified
    assert route.calls[0].request.headers["If-None-Match"] == 'W/"abc"'


@respx.mock
async def test_rate_limiter_keys_on_the_target_not_the_proxy(proxied):
    """Otherwise every proxied host collapses into one bucket.

    The limiter is what keeps polling polite; routing through a proxy must not
    quietly discard it.
    """
    fetcher._limiters.clear()
    respx.get(url__startswith=WORKER).mock(
        return_value=httpx.Response(200, json={}))

    await fetcher.fetch("https://redsky.target.com/v1/x")

    assert "redsky.target.com" in fetcher._limiters
    assert "proxy.example.workers.dev" not in fetcher._limiters


@respx.mock
async def test_strategies_use_the_proxy_transparently(proxied, monkeypatch):
    """A strategy should need no knowledge that a host is proxied."""
    monkeypatch.setattr(fetcher, "PROXY_HOSTS", {"satisfyrunning.com"})
    route = respx.get(url__startswith=WORKER).mock(
        return_value=httpx.Response(200, json={
            "id": 1, "title": "T", "handle": "h",
            "variants": [{"id": 9, "available": True, "price": 100}]}))

    result = await shopify.check({
        "id": 1, "url": "https://satisfyrunning.com/products/h",
        "kind": "product", "target_ref": "h"})

    assert result.ok and result.state == "in_stock"
    assert result.cart_url == "https://satisfyrunning.com/cart/9:1", \
        "cart links must point at the store, never at the proxy"
    assert route.called


# --- content negotiation ---------------------------------------------------

def test_we_never_advertise_an_encoding_we_cannot_decode():
    """Regression: the app once claimed brotli it had no decoder for.

    Shopify honoured the claim, httpx got bytes it could not read, and the
    parse failure surfaced as "no supported platform detected" — a network
    capability bug wearing the costume of an unsupported store.
    """
    import httpx

    claimed = fetcher.DEFAULT_HEADERS.get("Accept-Encoding")
    if claimed is None:
        return  # httpx negotiates from its own installed codecs, which is correct

    supported = {p.strip() for p in
                 httpx.Client().headers.get("accept-encoding", "").split(",")}
    assert {p.strip() for p in claimed.split(",")} <= supported, (
        f"advertising {claimed!r} but this client can only decode {supported}")
