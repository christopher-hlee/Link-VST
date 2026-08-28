"""Shopify parsing against recorded response shapes."""
import httpx
import pytest
import respx

from monitor.statemachine import IN_STOCK, OUT_OF_STOCK
from monitor.strategies import shopify

STORE = "https://satisfyrunning.com"

PRODUCT_JS = {
    "id": 900,
    "title": 'Justice Short 8"',
    "handle": "justice-short",
    "featured_image": "//cdn.shopify.com/s/files/justice.jpg",
    "variants": [
        {"id": 111, "title": "S", "available": False, "price": 12000},
        {"id": 222, "title": "M", "available": True, "price": 12000},
        {"id": 333, "title": "L", "available": True, "price": 12000},
    ],
}

SOLD_OUT_JS = {**PRODUCT_JS, "variants": [
    {"id": 111, "title": "S", "available": False, "price": 12000}]}


def product_watch(**kw):
    return {"id": 1, "url": f"{STORE}/products/justice-short",
            "kind": "product", "target_ref": "justice-short", **kw}


def collection_watch(**kw):
    return {"id": 2, "url": f"{STORE}/collections/shop-new-arrivals",
            "kind": "collection", "target_ref": "shop-new-arrivals", **kw}


# --- product ---------------------------------------------------------------

@respx.mock
async def test_available_variant_yields_in_stock_and_cart_link():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=PRODUCT_JS))

    r = await shopify.check(product_watch())

    assert r.ok and r.state == IN_STOCK
    assert r.price == 120.0, "the .js endpoint reports cents"
    assert r.cart_url == f"{STORE}/cart/222:1", "first available variant"
    assert r.image.startswith("https://"), "protocol-relative URL normalised"
    assert r.extra["variants_available"] == 2


@respx.mock
async def test_all_variants_unavailable_is_out_of_stock():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=SOLD_OUT_JS))

    r = await shopify.check(product_watch())

    assert r.ok and r.state == OUT_OF_STOCK
    assert r.cart_url is None


@pytest.mark.parametrize("status", [403, 404, 429, 503])
@respx.mock
async def test_http_errors_report_failure_not_out_of_stock(status):
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(status))

    r = await shopify.check(product_watch())

    assert r.ok is False
    assert r.state is None, "a blocked request must not look like a sold-out item"


@respx.mock
async def test_304_is_success_with_not_modified():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(304))

    r = await shopify.check(product_watch(etag='W/"abc"'))

    assert r.ok and r.not_modified


@respx.mock
async def test_conditional_headers_are_sent():
    route = respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=PRODUCT_JS))

    await shopify.check(product_watch(etag='W/"abc"',
                                      last_modified="Mon, 01 Jan 2026 00:00:00 GMT"))

    sent = route.calls[0].request.headers
    assert sent["If-None-Match"] == 'W/"abc"'
    assert sent["If-Modified-Since"] == "Mon, 01 Jan 2026 00:00:00 GMT"


@respx.mock
async def test_non_json_body_is_a_failure():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, text="<html>nope</html>"))

    r = await shopify.check(product_watch())

    assert r.ok is False and r.state is None


# --- collection ------------------------------------------------------------

@respx.mock
async def test_collection_returns_handles():
    respx.get(url__startswith=f"{STORE}/collections/shop-new-arrivals/products.json"
              ).mock(return_value=httpx.Response(200, json={"products": [
                  {"handle": "a", "title": "Alpha"},
                  {"handle": "b", "title": "Beta"}]}))

    r = await shopify.check(collection_watch())

    assert r.ok and r.handles == ["a", "b"]
    assert r.extra["source"] == "products.json"
    assert r.extra["titles"]["a"] == "Alpha"


@respx.mock
async def test_collection_falls_back_to_atom_when_json_gated():
    respx.get(url__startswith=f"{STORE}/collections/shop-new-arrivals/products.json"
              ).mock(return_value=httpx.Response(403))
    respx.get(f"{STORE}/collections/shop-new-arrivals.atom").mock(
        return_value=httpx.Response(200, text="""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Alpha</title>
    <link href="https://satisfyrunning.com/products/alpha"/></entry>
  <entry><title>Beta</title>
    <link href="https://satisfyrunning.com/products/beta"/></entry>
</feed>"""))

    r = await shopify.check(collection_watch())

    assert r.ok, "a gated products.json must not take the watch down"
    assert r.handles == ["alpha", "beta"]
    assert r.extra["source"] == "atom"


@respx.mock
async def test_both_endpoints_failing_is_a_failure():
    respx.get(url__startswith=f"{STORE}/collections/shop-new-arrivals/products.json"
              ).mock(return_value=httpx.Response(403))
    respx.get(f"{STORE}/collections/shop-new-arrivals.atom").mock(
        return_value=httpx.Response(403))

    r = await shopify.check(collection_watch())

    assert r.ok is False and r.state is None


# --- detection -------------------------------------------------------------

@respx.mock
async def test_detect_product_url():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=PRODUCT_JS))

    found = await shopify.detect(f"{STORE}/products/justice-short")

    assert found["strategy"] == "shopify"
    assert found["kind"] == "product"
    assert found["target_ref"] == "justice-short"
    assert found["brand"] == "satisfyrunning.com"
    assert len(found["variants"]) == 3


@respx.mock
async def test_detect_returns_none_for_non_shopify():
    respx.get(url__startswith="https://example.com").mock(
        return_value=httpx.Response(404))

    assert await shopify.detect("https://example.com/products/thing") is None


# --- url parsing -----------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    (f"{STORE}/products/justice-short", "justice-short"),
    (f"{STORE}/collections/new/products/justice-short?variant=1", "justice-short"),
    (f"{STORE}/collections/new", None),
])
def test_product_handle_extraction(url, expected):
    assert shopify.product_handle(url) == expected


@pytest.mark.parametrize("url,expected", [
    (f"{STORE}/collections/shop-new-arrivals", "shop-new-arrivals"),
    (f"{STORE}/collections/all.atom", "all"),
    (f"{STORE}/collections/all.rss", "all"),
    (f"{STORE}/collections/new/products.json", "new"),
    (f"{STORE}/products/x", None),
])
def test_collection_handle_strips_feed_suffixes(url, expected):
    """`/collections/all.atom` is the collection `all`, served as a feed.

    Keeping the suffix pointed every later request at
    /collections/all.atom/products.json, which 404s forever.
    """
    assert shopify.collection_handle(url) == expected


def test_cart_permalink_shape():
    assert shopify.cart_url(STORE, 222) == f"{STORE}/cart/222:1"
