"""Reading a storefront through the schema.org data it publishes.

Written for a specific case: an Ocarina of Time special-edition Switch 2 whose
page says "available at a later date" and will at some point say "preorder".
Those are two values of one schema.org property, so the flip is exactly the
transition this app was built to catch.
"""
import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.statemachine import IN_STOCK, OUT_OF_STOCK
from monitor.strategies import retail
from monitor.strategies import detect as detect_strategy

URL = "https://www.nintendo.com/us/store/products/oot-switch-2-edition/"


def page(availability=None, *, name="Ocarina of Time Edition", price="499.99",
         offers_key=True):
    offer = {"@type": "Offer", "price": price, "priceCurrency": "USD"}
    if availability:
        offer["availability"] = availability
    product = {"@context": "https://schema.org", "@type": "Product",
               "name": name, "image": "https://cdn.nintendo.com/oot.jpg"}
    if offers_key:
        product["offers"] = offer
    return (f'<html><head><script type="application/ld+json">'
            f'{__import__("json").dumps(product)}</script></head>'
            f'<body>Available at a later date</body></html>')


def watch(**kw):
    return {"id": 1, "url": URL, "kind": "product", "name": "OoT Edition", **kw}


# --- the case this was built for -------------------------------------------

@respx.mock
async def test_available_at_a_later_date_reads_as_not_buyable():
    respx.get(URL).mock(return_value=httpx.Response(
        200, text=page("https://schema.org/OutOfStock")))

    r = await retail.check(watch())

    assert r.ok and r.state == OUT_OF_STOCK
    assert r.title == "Ocarina of Time Edition"


@respx.mock
async def test_a_preorder_is_buyable_and_says_so():
    respx.get(URL).mock(return_value=httpx.Response(
        200, text=page("https://schema.org/PreOrder")))

    r = await retail.check(watch())

    assert r.state == IN_STOCK, "you can give them money — that is an alert"
    assert r.extra["preorder"] is True
    assert r.price == 499.99


@respx.mock
async def test_in_stock_is_not_labelled_a_preorder():
    respx.get(URL).mock(return_value=httpx.Response(
        200, text=page("https://schema.org/InStock")))

    r = await retail.check(watch())

    assert r.state == IN_STOCK and r.extra["preorder"] is False


@respx.mock
async def test_a_product_with_no_offer_is_listed_but_not_orderable():
    """The shape a page takes between announcement and preorder opening."""
    respx.get(URL).mock(return_value=httpx.Response(
        200, text=page(offers_key=False)))

    r = await retail.check(watch())

    assert r.ok and r.state == OUT_OF_STOCK


# --- an unreadable page must never read as sold out ------------------------

@pytest.mark.parametrize("body", [
    "<html><body>no structured data here</body></html>",
    '<html><script type="application/ld+json">{not json,,}</script></html>',
    '<html><script type="application/ld+json">'
    '{"@type":"WebPage","name":"Nintendo"}</script></html>',
])
@respx.mock
async def test_an_unreadable_page_fails_loudly_rather_than_lying(body):
    """The invariant the whole project rests on.

    Reporting OUT_OF_STOCK for a page we cannot parse looks identical to the
    truth and would stay wrong for the life of the watch — precisely how a
    monitor dies quietly while appearing to work.
    """
    respx.get(URL).mock(return_value=httpx.Response(200, text=body))

    r = await retail.check(watch())

    assert r.ok is False
    assert r.state is None, "no reading is not the same as sold out"
    assert "schema.org" in r.error


@respx.mock
async def test_a_broken_block_does_not_hide_a_good_one():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(
        '<script type="application/ld+json">{{{</script>'
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"OoT","offers":'
        '{"@type":"Offer","availability":"https://schema.org/PreOrder"}}'
        '</script>')))

    r = await retail.check(watch())

    assert r.ok and r.state == IN_STOCK


# --- the shapes real sites actually ship -----------------------------------

@respx.mock
async def test_graph_wrapped_documents_parse():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@graph":[{"@type":"BreadcrumbList"},'
        '{"@type":"Product","name":"OoT","offers":{"@type":"Offer",'
        '"price":"499.99","availability":"https://schema.org/PreOrder"}}]}'
        '</script>')))

    r = await retail.check(watch())

    assert r.state == IN_STOCK and r.extra["preorder"] is True


@respx.mock
async def test_a_list_of_offers_is_buyable_if_any_offer_is():
    respx.get(URL).mock(return_value=httpx.Response(200, text=(
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"OoT","offers":['
        '{"@type":"Offer","availability":"https://schema.org/OutOfStock"},'
        '{"@type":"Offer","availability":"https://schema.org/InStock",'
        '"price":"499.99"}]}</script>')))

    r = await retail.check(watch())

    assert r.state == IN_STOCK


@respx.mock
async def test_bare_availability_values_without_the_url_prefix_work():
    respx.get(URL).mock(return_value=httpx.Response(200, text=page("PreOrder")))

    r = await retail.check(watch())

    assert r.state == IN_STOCK and r.extra["preorder"] is True


# --- detection must not steal Shopify watches ------------------------------

@respx.mock
async def test_a_nintendo_page_is_detected_as_retail():
    respx.get(URL).mock(return_value=httpx.Response(
        200, text=page("https://schema.org/PreOrder")))

    found = await retail.detect(URL)

    assert found["strategy"] == "retail"
    assert found["name"] == "Ocarina of Time Edition"
    assert found["brand"] == "nintendo.com"


@respx.mock
async def test_a_shopify_product_is_still_claimed_by_shopify():
    """Shopify pages carry JSON-LD too. If retail were tried first it would
    win and silently give up variants and cart permalinks."""
    shop = "https://satisfyrunning.com"
    respx.get(f"{shop}/products/justice-short.js").mock(
        return_value=httpx.Response(200, json={
            "title": "Justice Short", "handle": "justice-short",
            "variants": [{"id": 1, "title": "M", "available": True,
                          "price": 12000}]}))
    respx.get(url__startswith=shop).mock(
        return_value=httpx.Response(200, text=page("InStock")))

    found = await detect_strategy(f"{shop}/products/justice-short")

    assert found["strategy"] == "shopify", "ordering regression"


@respx.mock
async def test_a_page_with_nothing_readable_is_not_detected():
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    assert await retail.detect(URL) is None


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


@respx.mock
async def test_later_date_to_preorder_fires_exactly_one_alert(sent):
    wid = db.create_watch(name="OoT Edition", brand="nintendo.com", url=URL,
                          strategy="retail", kind="product")
    route = respx.get(URL)

    route.mock(return_value=httpx.Response(
        200, text=page("https://schema.org/OutOfStock")))
    await scheduler.check_watch(db.get_watch(wid))
    assert sent == [], "not orderable yet — stay quiet"

    route.mock(return_value=httpx.Response(
        200, text=page("https://schema.org/PreOrder")))
    await scheduler.check_watch(db.get_watch(wid))

    assert [c["kind"] for c in sent] == ["restock"]
    assert sent[0]["payload"]["preorder"] is True

    # Still open on later polls: no repeat alerts.
    for _ in range(3):
        await scheduler.check_watch(db.get_watch(wid))
    assert len(sent) == 1


@respx.mock
async def test_an_unreadable_page_never_flips_a_known_state(sent):
    """If Nintendo starts rendering the page in the browser, the watch must go
    'failing', not quietly report the console as sold out."""
    wid = db.create_watch(name="OoT Edition", brand="nintendo.com", url=URL,
                          strategy="retail", kind="product")
    route = respx.get(URL)

    route.mock(return_value=httpx.Response(
        200, text=page("https://schema.org/PreOrder")))
    await scheduler.check_watch(db.get_watch(wid))
    assert db.get_watch(wid)["last_state"] == IN_STOCK

    route.mock(return_value=httpx.Response(200, text="<html>rendered</html>"))
    for _ in range(3):
        await scheduler.check_watch(db.get_watch(wid))

    w = db.get_watch(wid)
    assert w["last_state"] == IN_STOCK, "an unreadable page is not a sell-out"
    assert w["consecutive_failures"] == 3


def test_the_alert_calls_a_preorder_a_preorder():
    from monitor.notify import telegram

    body = telegram.render(
        {"name": "OoT Edition", "brand": "nintendo.com"}, "restock",
        {"title": "Ocarina of Time Edition", "price": 499.99, "preorder": True})

    assert "Preorder open" in body
    assert "Back in stock" not in body


def test_a_normal_restock_still_reads_as_a_restock():
    from monitor.notify import telegram

    body = telegram.render(
        {"name": "Tee", "brand": "Satisfy"}, "restock",
        {"title": "Justice Short", "price": 120.0})

    assert "Back in stock" in body
    assert "Preorder" not in body
