"""A "coming soon" listing is a drop that has not happened yet.

Satisfy publishes a product, puts it in shop-all, and leaves it unbuyable for
days. Detecting new HANDLES is blind to what happens next: by the time the
button goes live the catalogue has not grown, so there is nothing new to
notice and the release passes in silence. These tests are about the second
question — "can I buy it now" — which is the one actually being asked.
"""
import json
from datetime import timedelta

import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.statemachine import (
    ARRIVAL_LAUNCHED, ARRIVAL_NEW, RELAUNCH_COOLDOWN_S,
)
from monitor.timeutil import stamp, utcnow

STORE = "https://satisfyrunning.com"
COLLECTION = f"{STORE}/collections/shop-all"
FEED = f"{STORE}/collections/shop-all/products.json"


def ago(**kw):
    return (utcnow() - timedelta(**kw)).isoformat()


def product(handle, *, buyable, published=None, created=None):
    """A Shopify catalogue entry. A coming-soon item is published and in the
    collection; every variant simply has available=false."""
    published = published or ago(days=9)
    return {"handle": handle, "title": handle.replace("-", " ").title(),
            "published_at": published, "created_at": created or published,
            "variants": [{"id": abs(hash(handle)) % 10000, "title": "M",
                          "available": buyable, "price": "295.00"}]}


def feed(*products):
    return httpx.Response(200, json={"products": list(products)})


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


def watch_row(**kw):
    fields = dict(name="satisfyrunning.com · shop-all", brand="satisfyrunning.com",
                  url=COLLECTION, strategy="shopify", kind="collection",
                  target_ref="shop-all", base_interval_s=300,
                  last_state="watching", last_sweep_at=stamp())
    fields.update(kw)
    return db.create_watch(**fields)


async def sweep(wid):
    await scheduler.check_watch(db.get_watch(wid))


# --- the case in hand ------------------------------------------------------

@respx.mock
async def test_a_coming_soon_listing_alerts_when_it_becomes_buyable(sent):
    """The whole point. Two sweeps: the product is unbuyable, then it is not."""
    wid = watch_row(baseline_json=json.dumps(["shell-jacket", "coming-soon-tee"]),
                    availability_json=json.dumps({
                        "shell-jacket": {"available": True},
                        "coming-soon-tee": {"available": False}}))
    route = respx.get(url__startswith=FEED)
    route.mock(return_value=feed(product("shell-jacket", buyable=True),
                                 product("coming-soon-tee", buyable=True)))

    await sweep(wid)

    assert [c["kind"] for c in sent] == ["new_product"]
    assert sent[0]["payload"]["arrival"] == ARRIVAL_LAUNCHED
    assert sent[0]["payload"]["handles"] == ["coming-soon-tee"]


@respx.mock
async def test_a_listing_that_is_still_coming_soon_stays_silent(sent):
    wid = watch_row(baseline_json=json.dumps(["coming-soon-tee"]),
                    availability_json=json.dumps(
                        {"coming-soon-tee": {"available": False}}))
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("coming-soon-tee", buyable=False)))

    await sweep(wid)

    assert sent == [], "an unbuyable listing has not dropped"


@respx.mock
async def test_a_new_coming_soon_listing_still_announces_itself(sent):
    """Appearing is worth knowing about even though you cannot buy it yet —
    it is how you learn a drop is coming. It just is not a launch."""
    wid = watch_row(baseline_json=json.dumps(["shell-jacket"]),
                    availability_json=json.dumps(
                        {"shell-jacket": {"available": True}}))
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("shell-jacket", buyable=True),
                          product("teaser", buyable=False, published=ago(minutes=3))))

    await sweep(wid)

    assert [c["payload"]["arrival"] for c in sent] == [ARRIVAL_NEW]
    assert sent[0]["payload"]["handles"] == ["teaser"]


@respx.mock
async def test_appearing_and_launching_are_not_one_alert_twice(sent):
    """A product that arrives already buyable gets the 'new' alert and must
    not also be reported as a launch in the same breath."""
    wid = watch_row(baseline_json=json.dumps(["shell-jacket"]),
                    availability_json=json.dumps(
                        {"shell-jacket": {"available": True}}))
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("shell-jacket", buyable=True),
                          product("drop", buyable=True, published=ago(minutes=2))))

    await sweep(wid)

    assert len(sent) == 1
    assert sent[0]["payload"]["arrival"] == ARRIVAL_NEW


# --- adopting a catalogue must be silent -----------------------------------

@respx.mock
async def test_the_first_sweep_after_this_ships_does_not_alert_on_everything(sent):
    """The upgrade case. An existing watch has a full baseline and no ledger;
    if 'available with nothing recorded' counted as a flip, the first sweep
    after the deploy would announce the entire catalogue."""
    handles = [f"p{i}" for i in range(40)]
    wid = watch_row(baseline_json=json.dumps(handles), availability_json=None)
    respx.get(url__startswith=FEED).mock(
        return_value=feed(*[product(h, buyable=True) for h in handles]))

    await sweep(wid)

    assert sent == [], "adopting a ledger is not forty launches"
    ledger = json.loads(db.get_watch(wid)["availability_json"])
    assert len(ledger) == 40 and all(v["available"] for v in ledger.values())


@respx.mock
async def test_the_very_first_sweep_of_a_new_watch_records_the_ledger(sent):
    wid = watch_row(baseline_json=None, availability_json=None)
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("a", buyable=True),
                          product("b", buyable=False)))

    await sweep(wid)

    assert sent == []
    ledger = json.loads(db.get_watch(wid)["availability_json"])
    assert ledger["a"]["available"] is True
    assert ledger["b"]["available"] is False


# --- absence is not evidence ----------------------------------------------

@respx.mock
async def test_a_short_read_does_not_mark_the_missing_tail_unavailable(sent):
    """A page cut short by a rate limit shows fewer products. Recording the
    absent ones as unavailable would fire a launch for every one of them on
    the next full read — the same class of bug as treating a 403 as sold out."""
    wid = watch_row(baseline_json=json.dumps(["a", "b", "c"]),
                    availability_json=json.dumps({
                        "a": {"available": True}, "b": {"available": True},
                        "c": {"available": True}}))
    route = respx.get(url__startswith=FEED)

    route.mock(return_value=feed(product("a", buyable=True)))
    await sweep(wid)
    ledger = json.loads(db.get_watch(wid)["availability_json"])
    assert ledger["b"]["available"] is True, "b was not seen, not seen-as-gone"

    route.mock(return_value=feed(product("a", buyable=True),
                                 product("b", buyable=True),
                                 product("c", buyable=True)))
    await sweep(wid)

    assert sent == [], "nothing launched; the tail was simply out of view"


@respx.mock
async def test_a_failed_check_leaves_the_ledger_alone(sent):
    wid = watch_row(baseline_json=json.dumps(["a"]),
                    availability_json=json.dumps({"a": {"available": False}}))
    respx.get(url__startswith=FEED).mock(return_value=httpx.Response(500))
    respx.get(url__startswith=f"{STORE}/collections/shop-all.atom").mock(
        return_value=httpx.Response(500))

    await sweep(wid)

    assert json.loads(db.get_watch(wid)["availability_json"]) == {
        "a": {"available": False}}


# --- a drop makes a hot item flap -----------------------------------------

@respx.mock
async def test_one_release_is_one_alert_however_much_the_stock_flaps(sent):
    """Sizes sell out and are restocked all through a busy drop. Each flip is
    real, but twelve notifications for one release is a notification you learn
    to swipe away."""
    wid = watch_row(baseline_json=json.dumps(["hot"]),
                    availability_json=json.dumps({"hot": {"available": False}}))
    route = respx.get(url__startswith=FEED)

    for buyable in [True, False, True, False, True, True]:
        route.mock(return_value=feed(product("hot", buyable=buyable)))
        await sweep(wid)

    assert len(sent) == 1, f"{len(sent)} alerts for one release"


@respx.mock
async def test_a_genuine_restock_the_next_day_does_alert(sent):
    """The cooldown must not become a permanent mute."""
    wid = watch_row(baseline_json=json.dumps(["hot"]),
                    availability_json=json.dumps({"hot": {"available": False}}))
    route = respx.get(url__startswith=FEED)

    route.mock(return_value=feed(product("hot", buyable=True)))
    await sweep(wid)
    route.mock(return_value=feed(product("hot", buyable=False)))
    await sweep(wid)

    # Age the recorded alert past the cooldown, as a day passing would.
    ledger = json.loads(db.get_watch(wid)["availability_json"])
    ledger["hot"]["alerted_at"] = stamp(
        utcnow() - timedelta(seconds=RELAUNCH_COOLDOWN_S + 60))
    db.update_watch(wid, availability_json=json.dumps(ledger))

    route.mock(return_value=feed(product("hot", buyable=True)))
    await sweep(wid)

    assert len(sent) == 2


# --- what the alert says ---------------------------------------------------

def test_the_launch_alert_does_not_claim_the_catalogue_grew():
    from monitor.notify import telegram

    body = telegram.render(
        {"name": "Satisfy", "brand": "satisfyrunning.com"}, "new_product",
        {"handles": ["a"], "titles": {"a": "Coming Soon Tee"},
         "baseline_count": 333, "arrival": "launched"})

    assert "now buyable" in body
    assert "333 → 334" not in body, "the product was already counted"
    assert "Just listed" not in body


def test_the_launch_alert_does_not_report_a_lag_it_would_misread():
    """published_at is when the teaser went up, days before the button did.
    Reporting it as detection lag would claim we were a week late."""
    from monitor.notify import telegram

    body = telegram.render(
        {"name": "Satisfy", "brand": "satisfyrunning.com"}, "new_product",
        {"handles": ["a"], "titles": {"a": "Tee"}, "arrival": "launched",
         "listed_ago_s": 9 * 86400})

    assert "before this alert" not in body


@respx.mock
async def test_the_launch_alert_carries_a_cart_link(sent):
    """The reason this alert is worth sending at all: by definition the
    product is buyable the moment it fires, so the button works."""
    wid = watch_row(baseline_json=json.dumps(["tee"]),
                    availability_json=json.dumps({"tee": {"available": False}}))
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("tee", buyable=True)))

    await sweep(wid)

    items = sent[0]["payload"]["items"]["tee"]
    assert items["offers"], "a launch with no cart link is just a notification"
    assert "/cart/" in items["offers"][0]["cart_url"]


# --- asking the monitor what it sees ---------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr("monitor.config.API_KEY", "k")
    monkeypatch.setattr("monitor.main.API_KEY", "k")
    from monitor.main import app
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer k"})
        yield c


def product_json(handle, *, buyable, published=None):
    """`/products/<handle>.js` — the endpoint that states availability."""
    return httpx.Response(200, json={
        "title": handle.replace("-", " ").title(),
        "variants": [{"id": 1, "title": "M", "available": buyable,
                      "price": 29500}]})


def page_html(*, availability="https://schema.org/InStock"):
    """A product page carrying schema.org, as a real storefront does."""
    return httpx.Response(200, text=f"""<html><head>
      <script type="application/ld+json">{{"@type":"Product",
        "name":"Tee","offers":{{"@type":"Offer","price":"295.00",
        "availability":"{availability}"}}}}</script></head><body></body></html>""",
        headers={"content-type": "text/html"})


def mock_product_sources(handle, *, buyable=True, availability="https://schema.org/InStock"):
    respx.get(url__regex=rf".*/products/{handle}\.js$").mock(
        return_value=product_json(handle, buyable=buyable))
    respx.get(url__regex=rf".*/products/{handle}$").mock(
        return_value=page_html(availability=availability))


@respx.mock
def test_inspect_explains_a_coming_soon_listing(client):
    """The question that prompted all this: why was there no alert?"""
    watch_row(baseline_json=json.dumps(["coming-soon-tee"]),
              availability_json=json.dumps(
                  {"coming-soon-tee": {"available": False}}))
    mock_product_sources("coming-soon-tee", buyable=False)
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("coming-soon-tee", buyable=False)))

    r = client.get("/api/inspect",
                   params={"url": f"{STORE}/products/coming-soon-tee"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["buyable_now"] is False
    assert body["watches"][0]["in_catalogue"] is True
    assert "not buyable" in body["watches"][0]["verdict"]


@respx.mock
def test_inspect_says_when_nothing_is_watching_the_store(client):
    mock_product_sources("x", buyable=True)

    r = client.get("/api/inspect",
                   params={"url": "https://othershop.com/products/x"})

    assert r.status_code == 200, r.text
    assert "No collection watch covers" in r.json()["note"]


@respx.mock
def test_inspect_reports_a_pending_launch(client):
    """Buyable at the store, last recorded unbuyable: an alert is due."""
    watch_row(baseline_json=json.dumps(["tee"]),
              availability_json=json.dumps({"tee": {"available": False}}))
    mock_product_sources("tee", buyable=True)
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("tee", buyable=True)))

    r = client.get("/api/inspect", params={"url": f"{STORE}/products/tee"})

    assert "the next sweep alerts" in r.json()["watches"][0]["verdict"]


def test_inspect_refuses_a_url_that_is_not_a_product(client):
    r = client.get("/api/inspect", params={"url": f"{STORE}/collections/shop-all"})
    assert r.status_code == 422


@respx.mock
def test_inspect_does_not_invent_data_when_the_store_refuses(client):
    """An unreadable page must not come back looking like an answer."""
    respx.get(url__regex=r".*/products/ghost(\.js)?$").mock(
        return_value=httpx.Response(404))

    r = client.get("/api/inspect", params={"url": f"{STORE}/products/ghost"})

    assert r.status_code == 502
    assert "unpublished" in r.json()["detail"]


# --- does the collection we poll even contain it? --------------------------

@respx.mock
def test_inspect_names_a_product_the_watched_collection_cannot_see(client):
    """The decisive question behind "should I watch every collection?".

    A baseline miss is ambiguous — it means either "not swept yet" or "this
    collection will never contain it", and only the second is a reason to
    watch something else. So the answer comes from the feed, not the memory.
    """
    watch_row(baseline_json=json.dumps(["shell-jacket"]))
    mock_product_sources("secret-drop", buyable=False)
    # shop-all does not carry it.
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("shell-jacket", buyable=True)))

    r = client.get("/api/inspect", params={"url": f"{STORE}/products/secret-drop"})

    body = r.json()
    assert body["watches"][0]["in_feed"] is False
    assert "can never alert on it" in body["watches"][0]["verdict"]
    assert "covers every collection at once" in body["note"]


@respx.mock
def test_inspect_separates_not_swept_yet_from_not_covered(client):
    """In the feed but not in the baseline is a timing gap, not a blind spot,
    and must not push you into creating watches you do not need."""
    watch_row(baseline_json=json.dumps(["shell-jacket"]))
    mock_product_sources("brand-new", buyable=True)
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("shell-jacket", buyable=True),
                          product("brand-new", buyable=True)))

    r = client.get("/api/inspect", params={"url": f"{STORE}/products/brand-new"})

    w = r.json()["watches"][0]
    assert w["in_feed"] is True and w["in_catalogue"] is False
    assert "not yet swept" in w["verdict"]
    assert "note" not in r.json()


@respx.mock
def test_inspect_does_not_guess_when_the_feed_will_not_load(client):
    """An unreadable feed is not evidence that the collection lacks the
    product — saying so would send you off creating watches for nothing."""
    watch_row(baseline_json=json.dumps(["shell-jacket"]))
    mock_product_sources("x", buyable=True)
    respx.get(url__startswith=FEED).mock(return_value=httpx.Response(503))
    respx.get(url__startswith=f"{STORE}/collections/shop-all.atom").mock(
        return_value=httpx.Response(503))

    r = client.get("/api/inspect", params={"url": f"{STORE}/products/x"})

    w = r.json()["watches"][0]
    assert w["in_feed"] is None
    assert "cannot say" in w["verdict"]


# --- one store, two spellings ----------------------------------------------

@pytest.mark.parametrize("watch_url,asked", [
    ("https://www.satisfyrunning.com", "https://satisfyrunning.com"),
    ("https://satisfyrunning.com", "https://www.satisfyrunning.com"),
    ("https://www.satisfyrunning.com/collections/shop-all",
     "https://www.satisfyrunning.com"),
])
@respx.mock
def test_www_is_not_a_different_store(client, watch_url, asked):
    """Reported from the dashboard: a watch added as www.satisfyrunning.com
    answered "nothing is polling this store" about a store it polls every 35
    seconds, because the origins were compared verbatim."""
    watch_row(url=watch_url, target_ref=None, baseline_json=json.dumps(["tee"]))
    mock_product_sources("tee", buyable=True)
    respx.get(url__regex=r".*/products\.json.*").mock(
        return_value=feed(product("tee", buyable=True)))

    r = client.get("/api/inspect", params={"url": f"{asked}/products/tee"})

    body = r.json()
    assert body["watches"], f"{watch_url} should cover {asked}"
    assert "note" not in body


@respx.mock
def test_a_genuinely_different_store_is_still_not_covered(client):
    watch_row(url="https://www.satisfyrunning.com", target_ref=None)
    mock_product_sources("x", buyable=True)

    r = client.get("/api/inspect", params={"url": "https://othershop.com/products/x"})

    assert r.json()["watches"] == []
    assert "No collection watch covers" in r.json()["note"]


@respx.mock
def test_inspect_returns_a_timestamp_the_page_can_read(client):
    """"listed invalid Date" on the dashboard: the store's published_at carries
    a timezone offset, and the page appends Z to what it is given."""
    watch_row(baseline_json=json.dumps(["tee"]))
    mock_product_sources("tee", buyable=True)
    respx.get(url__startswith=FEED).mock(return_value=feed(
        product("tee", buyable=True, published="2026-09-16T22:36:47-04:00")))

    body = client.get("/api/inspect", params={"url": f"{STORE}/products/tee"}).json()

    assert body["published_at"] == "2026-09-17 02:36:47", "normalised to UTC"


# --- never invent the fact you were asked to check -------------------------

@respx.mock
def test_a_silent_source_is_reported_as_silent_not_as_buyable(client):
    """The bug behind "Buyable now" on a page showing Coming Soon.

    Inspect read a product endpoint that does not carry `available`, and the
    permissive default meant for building cart links turned that silence into
    a claim. A diagnostic that invents the one fact it was asked to check is
    worse than no diagnostic, because it gets believed.
    """
    watch_row(baseline_json=json.dumps(["tee"]),
              availability_json=json.dumps({"tee": {"available": False}}))
    # A feed whose variants state nothing at all.
    respx.get(url__regex=r".*/products/tee\.js$").mock(return_value=httpx.Response(
        200, json={"title": "Tee", "variants": [{"id": 1, "title": "M",
                                                 "price": 29500}]}))
    respx.get(url__regex=r".*/products/tee$").mock(return_value=page_html())
    respx.get(url__startswith=FEED).mock(return_value=httpx.Response(
        200, json={"products": [{"handle": "tee", "title": "Tee",
                                 "published_at": ago(days=9), "created_at": ago(days=9),
                                 "variants": [{"id": 1, "title": "M",
                                               "price": "295.00"}]}]}))

    body = client.get("/api/inspect", params={"url": f"{STORE}/products/tee"}).json()

    assert body["buyable_now"] is None, "silence is not a yes"
    assert "cannot tell you whether it is buyable" in body["watches"][0]["verdict"]
    assert "the next sweep alerts" not in body["watches"][0]["verdict"]


@respx.mock
def test_a_coming_soon_page_contradicting_the_api_is_called_out(client):
    """A storefront showing Coming Soon while the catalogue says buyable means
    the store signals it somewhere other than the variant flag — which is a
    thing to be told, not a thing to quietly resolve in the API's favour."""
    watch_row(baseline_json=json.dumps(["rippy-shorts"]),
              availability_json=json.dumps({"rippy-shorts": {"available": False}}))
    respx.get(url__regex=r".*/products/rippy-shorts\.js$").mock(
        return_value=product_json("rippy-shorts", buyable=True))
    respx.get(url__regex=r".*/products/rippy-shorts$").mock(
        return_value=page_html(availability="https://schema.org/OutOfStock"))
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("rippy-shorts", buyable=True)))

    body = client.get("/api/inspect",
                      params={"url": f"{STORE}/products/rippy-shorts"}).json()

    assert body["buyable_now"] is True
    assert body["sources"]["product_page"]["buyable"] is False
    assert "treat this as not yet dropped" in body["disagreement"]


@respx.mock
def test_a_page_with_no_availability_at_all_is_not_read_as_buyable(client):
    """"Available at a later date" — described, priced, not orderable."""
    watch_row(baseline_json=json.dumps(["tee"]))
    respx.get(url__regex=r".*/products/tee\.js$").mock(
        return_value=product_json("tee", buyable=True))
    respx.get(url__regex=r".*/products/tee$").mock(return_value=httpx.Response(
        200, text='''<html><script type="application/ld+json">
          {"@type":"Product","name":"Tee"}</script></html>''',
        headers={"content-type": "text/html"}))
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("tee", buyable=True)))

    page = client.get("/api/inspect",
                      params={"url": f"{STORE}/products/tee"}).json()["sources"]["product_page"]

    assert page["buyable"] is False
    assert "states no availability" in page["note"]


@respx.mock
def test_the_agreeing_case_raises_nothing(client):
    watch_row(baseline_json=json.dumps(["tee"]),
              availability_json=json.dumps({"tee": {"available": True}}))
    mock_product_sources("tee", buyable=True)
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("tee", buyable=True)))

    body = client.get("/api/inspect", params={"url": f"{STORE}/products/tee"}).json()

    assert "disagreement" not in body


# --- a promise has to be backed by the ledger ------------------------------

@respx.mock
def test_armed_and_merely_unbuyable_are_told_apart(client):
    """The launch detector flips from a RECORDED false, so "the feed says not
    buyable" is not by itself grounds to promise an alert."""
    armed = watch_row(name="armed", baseline_json=json.dumps(["tee"]),
                      availability_json=json.dumps({"tee": {"available": False}}))
    mock_product_sources("tee", buyable=False,
                         availability="https://schema.org/OutOfStock")
    respx.get(url__startswith=FEED).mock(
        return_value=feed(product("tee", buyable=False)))

    body = client.get("/api/inspect", params={"url": f"{STORE}/products/tee"}).json()
    assert "tracked and armed" in body["watches"][0]["verdict"]

    db.update_watch(armed, availability_json=None)
    body = client.get("/api/inspect", params={"url": f"{STORE}/products/tee"}).json()
    assert "not on record yet" in body["watches"][0]["verdict"]
    assert "the next sweep arms it" in body["watches"][0]["verdict"]
