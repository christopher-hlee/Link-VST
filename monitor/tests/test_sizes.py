"""Size selection for apparel.

A single "Add to cart" button silently picks a variant. Being dropped into
checkout holding the wrong size is worse than having no shortcut, so a stated
size preference gates the alert and every available size gets its own link.
"""
import httpx
import pytest
import respx

from monitor.notify import telegram
from monitor.statemachine import HELD, IN_STOCK, OUT_OF_STOCK
from monitor.strategies import shopify

STORE = "https://satisfyrunning.com"


def js(*variants):
    return {"id": 1, "title": "Justice Short", "handle": "justice-short",
            "variants": [{"id": i, "title": t, "available": a, "price": 18500}
                         for i, (t, a) in enumerate(variants, start=100)]}


def watch(**kw):
    base = {"id": 1, "url": f"{STORE}/products/justice-short",
            "kind": "product", "target_ref": "justice-short"}
    base.update(kw)
    return base


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("M", ["m"]), ("m, l", ["m", "l"]), ("Medium,Large", ["m", "l"]),
    ("XS", ["xs"]), ("", []), (None, []),
])
def test_size_preferences_normalise(raw, expected):
    assert shopify.parse_sizes(raw) == expected


@pytest.mark.parametrize("title,prefs,hit", [
    ("M", ["m"], True),
    ("Medium", ["m"], True),
    ("M / Black", ["m"], True),          # a colourway axis must not block a match
    ("L / Black", ["m"], False),
    ("Small", ["m", "s"], True),
    ("M", [], True),                      # no preference means anything counts
])
def test_variant_matching(title, prefs, hit):
    assert shopify.variant_matches(title, prefs) is hit


# --- gating ----------------------------------------------------------------

@respx.mock
async def test_wrong_size_in_stock_is_held_not_sold_out():
    """L being back does not help someone who wears M — but it is not sold out.

    Reporting sold out here would be a lie the person cannot detect: the item is
    on the shelf. Held says the watch is working and deliberately quiet.
    """
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=js(("S", False), ("M", False), ("L", True))))

    r = await shopify.check(watch(size_pref="M"))

    assert r.ok and r.state == HELD
    assert r.cart_url is None, "no cart link — nothing here is buyable in your size"
    assert r.extra["available_sizes"] == ["L"]
    assert r.extra["watched_sizes"] == ["M"]


@respx.mock
async def test_nothing_available_is_sold_out_even_with_a_size_preference():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=js(("S", False), ("M", False))))

    r = await shopify.check(watch(size_pref="M"))

    assert r.state == OUT_OF_STOCK, "held requires something to actually be in stock"


@respx.mock
async def test_preferred_size_available_fires_and_links_to_that_size():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=js(("S", True), ("M", True), ("L", True))))

    r = await shopify.check(watch(size_pref="M"))

    assert r.state == IN_STOCK
    assert r.cart_url == f"{STORE}/cart/101:1", "must link to M, not the first available"
    assert [o["title"] for o in r.extra["matched_offers"]] == ["M"]


@respx.mock
async def test_preference_order_is_respected():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=js(("S", True), ("M", False), ("L", True))))

    r = await shopify.check(watch(size_pref="M, L"))

    assert r.state == IN_STOCK
    assert r.cart_url == f"{STORE}/cart/102:1", "M unavailable, so fall back to L"


@respx.mock
async def test_no_preference_alerts_on_any_size():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=js(("S", False), ("L", True))))

    r = await shopify.check(watch())

    assert r.state == IN_STOCK
    assert len(r.extra["offers"]) == 1


@respx.mock
async def test_every_available_size_gets_its_own_cart_link():
    respx.get(f"{STORE}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json=js(("S", True), ("M", True), ("L", False))))

    r = await shopify.check(watch())

    urls = {o["title"]: o["cart_url"] for o in r.extra["offers"]}
    assert urls == {"S": f"{STORE}/cart/100:1", "M": f"{STORE}/cart/101:1"}
    assert "L" not in urls, "sold-out sizes must not be offered"


# --- the notification ------------------------------------------------------

def test_telegram_offers_one_button_per_size():
    payload = {"title": "Justice Short", "product_url": f"{STORE}/products/justice-short",
               "offers": [{"title": "S", "cart_url": f"{STORE}/cart/100:1"},
                          {"title": "M", "cart_url": f"{STORE}/cart/101:1"},
                          {"title": "L", "cart_url": f"{STORE}/cart/102:1"}]}
    kb = telegram._keyboard({"url": STORE}, payload)["inline_keyboard"]

    labels = [b["text"] for row in kb for b in row]
    assert labels == ["⚡ S", "⚡ M", "⚡ L", "Open product page"]
    assert kb[0][1]["url"] == f"{STORE}/cart/101:1"


def test_single_variant_product_keeps_one_plain_button():
    """A grinder has no sizes — don't show a size picker for one option."""
    payload = {"title": "Robot", "cart_url": f"{STORE}/cart/555:1",
               "product_url": f"{STORE}/products/robot",
               "offers": [{"title": "Default Title", "cart_url": f"{STORE}/cart/555:1"}]}
    kb = telegram._keyboard({"url": STORE}, payload)["inline_keyboard"]

    assert [b["text"] for row in kb for b in row] == ["⚡ Add to cart", "Open product page"]


def test_size_buttons_wrap_and_are_capped():
    payload = {"offers": [{"title": f"S{i}", "cart_url": f"{STORE}/cart/{i}:1"}
                          for i in range(12)],
               "product_url": STORE}
    kb = telegram._keyboard({"url": STORE}, payload)["inline_keyboard"]

    size_rows = kb[:-1]
    assert all(len(r) <= 4 for r in size_rows), "rows stay tappable"
    assert sum(len(r) for r in size_rows) == 8, "capped, not unbounded"


def test_sized_restock_message_shape():
    body = telegram.render(
        {"name": "Justice Short", "brand": "Satisfy"}, "restock",
        {"title": 'Justice Short 8"', "price": 185.0,
         "matched_offers": [{"title": "M"}],
         "offers": [{"title": "M"}, {"title": "S"}, {"title": "L"}]})
    assert "Back in your size" in body
    assert "<b>Your sizes: M</b>" in body
    assert "Also back: S, L" in body
    assert "$185" in body and "$185.0" not in body


def test_single_variant_restock_says_so():
    body = telegram.render(
        {"name": "Robot", "brand": "Cafelat"}, "restock",
        {"title": "Robot Barista", "price": 415.0,
         "offers": [{"title": "Default Title"}]})
    assert "Back in stock" in body
    assert "Back in your size" not in body
    assert "One variant, no sizes." in body


def test_held_note_names_both_sizes_and_disclaims_itself():
    body = telegram.render(
        {"name": "Tee", "brand": "Satisfy"}, "held_note",
        {"title": "Short Distance Tee", "price": 95.0,
         "available_sizes": ["XL"], "watched_sizes": ["M"]})
    assert "<b>Held — Short Distance Tee</b>" in body
    assert "<b>XL</b>" in body and "<b>M</b>" in body
    assert "not an alert" in body, "silence must explain itself"


def test_broken_message_carries_the_code_and_the_staleness_warning():
    body = telegram.render(
        {"name": "Zelda", "brand": "Target", "last_state": "sold_out"},
        "watch_failing",
        {"failures": 5, "error": "HTTP 410 — page is gone", "paused": True})
    assert "<code>HTTP 410 — page is gone</code>" in body
    assert "should not be trusted" in body
    assert "paused after 5 failures" in body


def test_new_drop_lists_products_and_the_count_change():
    body = telegram.render(
        {"name": "Satisfy", "brand": "Satisfy"}, "new_product",
        {"handles": ["a", "b"], "titles": {"a": 'Justice Short 8"', "b": "Rise Short"},
         "baseline_count": 214})
    assert "<b>🆕 2 new from Satisfy</b>" in body
    assert '· Justice Short 8"' in body
    assert "<code>214 → 216 products</code>" in body
