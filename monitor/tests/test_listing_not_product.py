"""A grid of products is not a product.

Reported from the add screen: pasting `havenshop.com/collections/auralee`
produced `retail · havenshop.com · Ultra Fine Tropical Wool Zip Blouson Top
Charcoal` — one garment, picked because it happened to sort first. The watch
would have monitored that single item while its owner believed it was watching
the brand. It returns data, reports healthy, and answers a different question
than the one asked.

The cause: every product card in a collection grid emits its own schema.org
Product block, and the JSON-LD reader took the first one it found. "This page
contains a Product" is not the claim "this page IS a product".
"""
import httpx
import pytest
import respx

from monitor import strategies
from monitor.strategies import retail

HAVEN = "https://havenshop.com"


def grid(*names):
    """A collection page: one Product block per card, as themes emit them."""
    cards = "".join(
        f'<script type="application/ld+json">{{"@type":"Product",'
        f'"name":"{n}","offers":{{"@type":"Offer","price":"450.00",'
        f'"availability":"https://schema.org/InStock"}}}}</script>'
        for n in names)
    return httpx.Response(200, text=f"<html><body>{cards}</body></html>",
                          headers={"content-type": "text/html"})


def product_page(name):
    return httpx.Response(200, text=f'''<html><script type="application/ld+json">
      {{"@type":"Product","name":"{name}","offers":{{"@type":"Offer",
      "price":"450.00","availability":"https://schema.org/InStock"}}}}
      </script></html>''', headers={"content-type": "text/html"})


@pytest.mark.parametrize("url,listing", [
    (f"{HAVEN}/collections/auralee", True),
    (f"{HAVEN}/collections/auralee/", True),
    (f"{HAVEN}/collections/all", True),
    (f"{HAVEN}/search?q=auralee", True),
    (f"{HAVEN}/", True),
    (HAVEN, True),
    # The one collection-shaped URL that really is a product.
    (f"{HAVEN}/collections/auralee/products/wool-blouson", False),
    (f"{HAVEN}/products/wool-blouson", False),
    ("https://www.nintendo.com/us/store/products/zelda-oot", False),
])
def test_a_listing_address_is_told_apart_from_a_product_one(url, listing):
    assert retail.looks_like_a_listing(url) is listing


@respx.mock
async def test_a_collection_page_is_not_detected_as_its_first_card():
    """The exact report."""
    respx.get(f"{HAVEN}/collections/auralee").mock(return_value=grid(
        "Ultra Fine Tropical Wool Zip Blouson Top Charcoal",
        "Washed Finx Twill Shirt"))

    assert await retail.detect(f"{HAVEN}/collections/auralee") is None


@respx.mock
async def test_it_is_refused_before_the_page_is_even_fetched():
    """No answer a collection page could give would make it a product, so
    asking is a request spent to reach a foregone conclusion."""
    route = respx.get(f"{HAVEN}/collections/auralee").mock(
        return_value=grid("Anything At All"))

    await retail.detect(f"{HAVEN}/collections/auralee")

    assert not route.called


@respx.mock
async def test_a_page_declaring_itself_a_collection_is_refused():
    """The URL shape is a heuristic; this is the page saying so outright."""
    respx.get(f"{HAVEN}/x").mock(return_value=httpx.Response(200, text='''
        <html><script type="application/ld+json">{"@type":"CollectionPage",
        "name":"AURALEE"}</script>
        <script type="application/ld+json">{"@type":"Product","name":"First Card",
        "offers":{"@type":"Offer","price":"450.00"}}</script></html>''',
        headers={"content-type": "text/html"}))

    assert await retail.detect(f"{HAVEN}/x") is None


@respx.mock
async def test_a_real_product_page_still_detects():
    """The guard must not cost the strategy its actual job."""
    respx.get(f"{HAVEN}/products/wool-blouson").mock(
        return_value=product_page("Ultra Fine Tropical Wool Zip Blouson"))

    found = await retail.detect(f"{HAVEN}/products/wool-blouson")

    assert found["kind"] == "product"
    assert found["name"] == "Ultra Fine Tropical Wool Zip Blouson"


@respx.mock
async def test_a_product_under_a_collection_path_still_detects():
    respx.get(f"{HAVEN}/collections/auralee/products/wool-blouson").mock(
        return_value=product_page("Wool Blouson"))

    found = await retail.detect(f"{HAVEN}/collections/auralee/products/wool-blouson")

    assert found is not None


# --- what the add screen should say now ------------------------------------

@respx.mock
async def test_a_gated_shopify_collection_is_reported_as_unidentified():
    """Haven appears to gate products.json. With retail no longer claiming the
    page, detection fails honestly instead of inventing a single-item watch —
    which is the difference between "I cannot read this" and a watch that
    silently monitors the wrong thing."""
    respx.get(url__startswith=f"{HAVEN}/collections/auralee/products.json").mock(
        return_value=httpx.Response(403))
    respx.get(url__startswith=f"{HAVEN}/collections/auralee.atom").mock(
        return_value=httpx.Response(403))
    respx.get(url__startswith=f"{HAVEN}/collections/auralee").mock(
        return_value=grid("Ultra Fine Tropical Wool Zip Blouson Top Charcoal"))

    assert await strategies.detect(f"{HAVEN}/collections/auralee") is None


# --- and the bot says which endpoint refused -------------------------------

@respx.mock
async def test_check_names_the_endpoints_that_refused():
    """"Not Shopify, or the catalogue is gated" leaves the one useful fact for
    someone with a shell on the server, which is what is unavailable here."""
    from monitor import telegram_bot

    respx.get(url__startswith=f"{HAVEN}/collections/auralee/products.json").mock(
        return_value=httpx.Response(403))
    respx.get(url__startswith=f"{HAVEN}/collections/auralee.atom").mock(
        return_value=httpx.Response(403))
    respx.get(url__startswith=f"{HAVEN}/collections/auralee").mock(
        return_value=grid("Whatever Sorted First"))

    reply = await telegram_bot.handle(f"/check {HAVEN}/collections/auralee")

    assert "products.json" in reply and "403" in reply
    assert ".atom" in reply
    assert "Whatever Sorted First" not in reply, \
        "it must not fall back to naming a card in the grid"
