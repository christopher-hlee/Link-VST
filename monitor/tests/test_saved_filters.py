"""Saved searches against a consignment store.

RAGTAG Global sells unique secondhand garments: quantity one, a single variant
titled `Default Title`, gone permanently when sold. Nothing restocks, so the
only event that matters is *a new listing appeared matching my filter* — which
is the detection mode this app already has, pointed at a store whose metadata
lives somewhere else entirely.

Size is the trap. On Satisfy it is `variant.title`; here it is a tag, so every
variant-reading code path matches zero items without erroring.
"""
import json
from datetime import timedelta

import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.filters import describe, matches, parse_boost_url
from monitor.timeutil import stamp, utcnow

STORE = "https://ragtag-global.com"
COLLECTION = "yohjiyamamotopourhomme"
FEED = f"{STORE}/collections/{COLLECTION}/products.json"

# The two saved searches, verbatim.
YOHJI_M = (f"{STORE}/collections/men_all?pf_st_availability=in-stock"
           "&pf_t_country_of_manufacture=countryoforigin_Japan"
           "&pf_t_gender=gender_Mens&pf_t_size=size_M"
           "&pf_v_brand=yohji+yamamoto+POUR+HOMME")
COMOLI_L_XL = (f"{STORE}/collections/men_all?pf_st_availability=in-stock"
               "&pf_t_country_of_manufacture=countryoforigin_Japan"
               "&pf_t_gender=gender_Mens&pf_t_size=size_L&pf_t_size=size_XL"
               "&pf_v_brand=COMOLI")


def garment(handle, *, vendor="COMOLI", tags=(), price="49160.00",
            available=True, sku="2001426G0062", published=None):
    """A consignment listing: one variant, Default Title, quantity one."""
    published = published or (utcnow() - timedelta(minutes=3)).isoformat()
    return {"handle": handle, "title": f"{vendor} Other", "vendor": vendor,
            "product_type": "Jackets", "tags": list(tags),
            "published_at": published, "created_at": published,
            "images": [{"src": "https://x/i.jpg"}],
            "variants": [{"id": abs(hash(handle)) % 99999, "sku": sku,
                          "title": "Default Title", "available": available,
                          "price": price}]}


def item_of(product):
    from monitor.strategies.shopify import _collection_item
    return _collection_item(STORE, product)


# --- parsing the saved search ---------------------------------------------

def test_one_facet_repeated_is_an_or_not_an_and():
    """pf_t_size twice means L or XL. Flattening the facets into a single
    required-tags list would demand a garment be both, which nothing is."""
    spec = parse_boost_url(COMOLI_L_XL)

    assert ["size_L", "size_XL"] in spec["tag_groups"]
    assert spec["vendors"] == ["COMOLI"]
    assert spec["in_stock"] is True


def test_separate_facets_are_an_and():
    spec = parse_boost_url(YOHJI_M)

    assert spec["tag_groups"] == [["countryoforigin_Japan"], ["gender_Mens"],
                                  ["size_M"]]
    assert spec["vendors"] == ["yohji yamamoto POUR HOMME"]


def test_the_filter_reads_back_as_something_checkable():
    assert describe(parse_boost_url(COMOLI_L_XL)) == (
        "COMOLI · countryoforigin_Japan · gender_Mens · size_L or size_XL "
        "· in stock")


def test_a_url_with_no_filter_params_filters_nothing():
    assert parse_boost_url(f"{STORE}/collections/men_all") == {}
    assert matches({"tags": []}, {}) is True


# --- evaluating it ---------------------------------------------------------

def test_size_is_read_from_tags_not_from_the_variant():
    """Every variant here is `Default Title`. A size test that reads variants
    matches nothing, and matches nothing silently."""
    spec = parse_boost_url(COMOLI_L_XL)
    wanted = item_of(garment("a", tags=["gender_Mens", "size_L",
                                        "countryoforigin_Japan"]))
    wrong_size = item_of(garment("b", tags=["gender_Mens", "size_S",
                                            "countryoforigin_Japan"]))

    assert matches(wanted, spec) is True
    assert matches(wrong_size, spec) is False
    assert wanted["offers"][0]["title"] == "Default Title", \
        "the variant carries no size at all — this is the whole point"


@pytest.mark.parametrize("tags,expected", [
    (["gender_Mens", "size_L", "countryoforigin_Japan"], True),
    (["gender_Mens", "size_XL", "countryoforigin_Japan"], True),
    (["gender_Mens", "size_M", "countryoforigin_Japan"], False),
    (["gender_Womens", "size_L", "countryoforigin_Japan"], False),
    (["gender_Mens", "size_L", "countryoforigin_China"], False),
    (["gender_Mens", "size_L"], False),
])
def test_the_facets_combine_as_the_storefront_does(tags, expected):
    spec = parse_boost_url(COMOLI_L_XL)
    assert matches(item_of(garment("g", tags=tags)), spec) is expected


def test_the_vendor_facet_is_the_brand():
    spec = parse_boost_url(YOHJI_M)
    tags = ["gender_Mens", "size_M", "countryoforigin_Japan"]

    assert matches(item_of(garment("a", vendor="yohji yamamoto POUR HOMME",
                                   tags=tags)), spec) is True
    assert matches(item_of(garment("b", vendor="COMOLI", tags=tags)),
                   spec) is False


def test_vendor_matching_survives_the_store_changing_its_capitalisation():
    spec = parse_boost_url(YOHJI_M)
    assert matches(item_of(garment("a", vendor="Yohji Yamamoto Pour Homme",
                                   tags=["gender_Mens", "size_M",
                                         "countryoforigin_Japan"])), spec) is True


def test_a_sold_garment_does_not_match_an_in_stock_filter():
    """Consignment: quantity one, and gone for good once sold."""
    spec = parse_boost_url(COMOLI_L_XL)
    sold = item_of(garment("a", tags=["gender_Mens", "size_L",
                                      "countryoforigin_Japan"], available=False))

    assert matches(sold, spec) is False


def test_a_price_ceiling_excludes_what_it_cannot_price():
    spec = {"max_price": 60000}
    cheap = item_of(garment("a", price="49160.00"))
    dear = item_of(garment("b", price="88000.00"))
    unpriced = item_of(garment("c", price="0.00"))

    assert matches(cheap, spec) is True
    assert matches(dear, spec) is False
    assert matches(unpriced, spec) is False, \
        "no price cannot be shown to be under the ceiling"


# --- the fields a consignment listing is identified by ---------------------

def test_the_identifying_fields_survive_a_useless_title():
    """Titles are "COMOLI Other". The SKU and vendor are what a person reads."""
    item = item_of(garment("x", tags=["size_L"]))

    assert item["vendor"] == "COMOLI"
    assert item["sku"] == "2001426G0062"
    assert item["product_type"] == "Jackets"
    assert item["list_price"] == 49160.0, "yen, read as a number, not dollars"


# --- end to end ------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    calls = []

    async def fake(watch, kind, payload):
        calls.append(payload)

    monkeypatch.setattr("monitor.notify.telegram.send_event", fake)
    monkeypatch.setattr("monitor.notify.telegram.configured", lambda: True)
    return calls


def ragtag_watch(spec, **kw):
    fields = dict(name=f"ragtag · {COLLECTION}", brand="ragtag-global.com",
                  url=f"{STORE}/collections/{COLLECTION}", strategy="shopify",
                  kind="collection", target_ref=COLLECTION, currency="JPY",
                  last_state="watching", filter_json=json.dumps(spec),
                  baseline_json=json.dumps(["seed"]), last_sweep_at=stamp())
    fields.update(kw)
    return db.create_watch(**fields)


def seed():
    old = (utcnow() - timedelta(days=90)).isoformat()
    return garment("seed", tags=["gender_Mens", "size_L"], published=old)


@respx.mock
async def test_only_the_matching_listing_is_announced(sent):
    wid = ragtag_watch(parse_boost_url(COMOLI_L_XL))
    respx.get(url__startswith=FEED).mock(return_value=httpx.Response(200, json={
        "products": [
            seed(),
            garment("comoli-jacket-l", tags=["gender_Mens", "size_L",
                                             "countryoforigin_Japan"]),
            garment("comoli-jacket-s", tags=["gender_Mens", "size_S",
                                             "countryoforigin_Japan"]),
            garment("yohji-coat-l", vendor="yohji yamamoto POUR HOMME",
                    tags=["gender_Mens", "size_L", "countryoforigin_Japan"]),
        ]}))

    await scheduler.check_watch(db.get_watch(wid))

    assert [p["handles"] for p in sent] == [["comoli-jacket-l"]]


@respx.mock
async def test_everything_seen_is_remembered_even_when_it_does_not_match(sent):
    """The filter decides what is said, never what is swept. Otherwise
    widening a filter later replays a catalogue you have already been shown."""
    wid = ragtag_watch(parse_boost_url(COMOLI_L_XL))
    respx.get(url__startswith=FEED).mock(return_value=httpx.Response(200, json={
        "products": [seed(),
                     garment("not-my-size", tags=["gender_Mens", "size_S",
                                                  "countryoforigin_Japan"])]}))

    await scheduler.check_watch(db.get_watch(wid))

    assert sent == []
    assert "not-my-size" in json.loads(db.get_watch(wid)["baseline_json"])


@respx.mock
async def test_a_yen_price_is_not_printed_as_dollars(sent):
    from monitor.notify import telegram

    wid = ragtag_watch(parse_boost_url(COMOLI_L_XL))
    respx.get(url__startswith=FEED).mock(return_value=httpx.Response(200, json={
        "products": [seed(), garment("comoli-coat", tags=[
            "gender_Mens", "size_L", "countryoforigin_Japan"])]}))
    await scheduler.check_watch(db.get_watch(wid))

    body = telegram.render(db.get_watch(wid), "new_product", sent[0])

    assert "¥49,160" in body
    assert "$49,160" not in body, "a 142x error, in the flattering direction"


# --- adding one through the API -------------------------------------------

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


@respx.mock
def test_pasting_a_saved_search_url_stores_the_filter(client):
    """The parameters do not reach products.json, so recognising them at the
    door is what stops the watch from polling the unfiltered collection while
    appearing to work."""
    respx.get(url__startswith=f"{STORE}/collections/men_all/products.json").mock(
        return_value=httpx.Response(200, json={"products": []}))

    r = client.post("/api/watches", json={"url": COMOLI_L_XL, "strategy": "shopify",
                                      "kind": "collection",
                                      "target_ref": "men_all",
                                      "currency": "JPY"})

    assert r.status_code == 201, r.text
    spec = json.loads(r.json()["watch"]["filter_json"])
    assert spec["vendors"] == ["COMOLI"]
    assert ["size_L", "size_XL"] in spec["tag_groups"]
    assert r.json()["watch"]["currency"] == "JPY"


@respx.mock
def test_a_plain_url_stores_no_filter(client):
    respx.get(url__startswith=f"{STORE}/collections/{COLLECTION}/products.json").mock(
        return_value=httpx.Response(200, json={"products": []}))

    r = client.post("/api/watches", json={"url": f"{STORE}/collections/{COLLECTION}",
                                      "strategy": "shopify", "kind": "collection",
                                      "target_ref": COLLECTION})

    assert r.json()["watch"]["filter_json"] is None


@respx.mock
def test_narrowing_a_filter_does_not_replay_the_catalogue(client):
    """Changing what you want to hear about must not rebaseline. Otherwise
    every garment already in the collection arrives as news."""
    respx.get(url__startswith=f"{STORE}/collections/men_all/products.json").mock(
        return_value=httpx.Response(200, json={"products": []}))
    wid = client.post("/api/watches", json={"url": COMOLI_L_XL, "strategy": "shopify",
                                        "kind": "collection", "target_ref": "men_all"}
                      ).json()["watch"]["id"]
    db.update_watch(wid, baseline_json=json.dumps(["a", "b", "c"]))

    r = client.patch(f"/api/watches/{wid}",
                     json={"filter_json": {"tag_groups": [["size_XL"]]}})

    assert r.status_code == 200, r.text
    assert json.loads(db.get_watch(wid)["baseline_json"]) == ["a", "b", "c"]
    assert json.loads(db.get_watch(wid)["filter_json"]) == {
        "tag_groups": [["size_XL"]]}


# --- two settings, one column ----------------------------------------------

@pytest.fixture
def wid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db.create_watch(name="mohawk · sale", brand="mohawkgeneralstore.com",
                           url="https://mohawkgeneralstore.com/collections/sale",
                           strategy="shopify", kind="collection",
                           target_ref="sale", last_state="watching")


async def test_a_vendor_filter_can_be_set_without_a_url(wid):
    """Most shops do not run a facet service, so most saved searches cannot be
    expressed as a URL — and a vendor is the one nearly every multi-brand
    store needs: "tell me when AURALEE is marked down here"."""
    from monitor import telegram_bot

    reply = await telegram_bot.handle(f"/filter {wid} vendor=AURALEE,COMOLI")

    assert "AURALEE or COMOLI" in reply
    assert db.get_filter(db.get_watch(wid))["vendors"] == ["AURALEE", "COMOLI"]


async def test_setting_a_filter_does_not_destroy_the_star_settings(wid):
    """They share a column. Narrowing a search used to replace the whole
    thing, so configuring stars and then filtering silently undid the stars."""
    from monitor import telegram_bot

    await telegram_bot.handle(f"/star {wid} colors=black max=450 fx=142")
    await telegram_bot.handle(f"/filter {wid} vendor=AURALEE")

    spec = db.get_filter(db.get_watch(wid))
    assert spec["vendors"] == ["AURALEE"]
    assert spec["star"]["max_landed"] == 450.0, "stars survived the filter"


async def test_clearing_the_filter_does_not_destroy_the_stars(wid):
    from monitor import telegram_bot

    await telegram_bot.handle(f"/star {wid} colors=black max=450 fx=142")
    await telegram_bot.handle(f"/filter {wid} vendor=AURALEE")

    reply = await telegram_bot.handle(f"/filter {wid} off")

    spec = db.get_filter(db.get_watch(wid))
    assert "vendors" not in spec
    assert spec["star"]["max_landed"] == 450.0
    assert "Stars left as they were" in reply


async def test_clearing_the_stars_does_not_destroy_the_filter(wid):
    from monitor import telegram_bot

    await telegram_bot.handle(f"/filter {wid} vendor=AURALEE")
    await telegram_bot.handle(f"/star {wid} max=450")

    reply = await telegram_bot.handle(f"/star {wid} off")

    spec = db.get_filter(db.get_watch(wid))
    assert spec["vendors"] == ["AURALEE"]
    assert "star" not in spec
    assert "Filter left as it was" in reply


async def test_a_new_search_replaces_the_old_one_rather_than_accumulating(wid):
    """Otherwise narrowing twice leaves both, and the watch matches nothing."""
    from monitor import telegram_bot

    await telegram_bot.handle(f"/filter {wid} vendor=AURALEE tags=size_L")
    await telegram_bot.handle(f"/filter {wid} vendor=COMOLI")

    spec = db.get_filter(db.get_watch(wid))
    assert spec["vendors"] == ["COMOLI"]
    assert "tag_groups" not in spec, "the old group did not linger"


async def test_status_says_whether_stars_are_on(wid):
    """A star that never appears because nothing was configured looks exactly
    like one that is broken."""
    from monitor import telegram_bot

    off = await telegram_bot._status()
    assert "★ off" in off

    await telegram_bot.handle(f"/star {wid} colors=black max=450 fx=142")
    on = await telegram_bot._status()
    assert "★ colours Black" in on


async def test_a_vendor_filter_actually_filters(wid):
    """End to end, on the shape these shops have: a sale collection carrying
    many brands, one of which is wanted."""
    from monitor import telegram_bot

    await telegram_bot.handle(f"/filter {wid} vendor=AURALEE")
    spec = db.get_filter(db.get_watch(wid))

    auralee = item_of({**garment("a", vendor="AURALEE", tags=["size_L"]),
                       "vendor": "AURALEE"})
    other = item_of({**garment("b", vendor="Nanamica", tags=["size_L"]),
                     "vendor": "Nanamica"})

    assert matches(auralee, spec) is True
    assert matches(other, spec) is False
