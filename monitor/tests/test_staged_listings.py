"""Alerts for a drop that is still being built.

Five alerts in one morning, every one a correctly classified NEW arrival and
none of them a release:

    09:49  TechSilk™ Drift Cordura Tights CL   733 -> 734   listed 36s before
    10:35  MothTech™ T-Shirt CL                734 -> 735   listed 28s before
    10:36  MothTech™ T-ShirtCL                 735 -> 736   listed 17s before
    10:53  MOTH SILVER WOMENS                  736 -> 737   listed 34s before
    11:16  moth pred women                     737 -> 738   listed 49s before

Two of those product pages now 404. The two that still load show 0 USD, no
photography, no sizes, and "Notify Me When Available". The titles are working
notes — "moth pred women", "T-ShirtCL" with the space missing — and one handle
carries a `-13` suffix, which is Shopify numbering the thirteenth attempt at
the same one. This is a merchandiser building a drop with the products
published, not a drop happening.
"""
import json
from datetime import timedelta

import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.statemachine import ARRIVAL_NEW
from monitor.timeutil import stamp, utcnow

STORE = "https://www.satisfyrunning.com"
FEED = f"{STORE}/products.json"


def just_now():
    return (utcnow() - timedelta(seconds=30)).isoformat()


def staged(handle, title, *, price="0.00", images=(), buyable=False,
           variants=None):
    """A record mid-construction, as the screenshots show them."""
    if variants is None:
        variants = [{"id": abs(hash(handle)) % 9999, "title": "Aged Black",
                     "available": buyable, "price": price}]
    return {"handle": handle, "title": title,
            "published_at": just_now(), "created_at": just_now(),
            "images": list(images), "variants": variants}


def feed(*entries):
    return httpx.Response(200, json={"products": list(entries)})


def anchor():
    old = (utcnow() - timedelta(days=200)).isoformat()
    return {"handle": "anchor", "title": "Anchor", "published_at": old,
            "created_at": old, "images": [{"src": "https://x/i.jpg"}],
            "variants": [{"id": 1, "title": "M", "available": True,
                          "price": "260.00"}]}


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


def store_watch(**kw):
    fields = dict(name="satisfyrunning.com · all products",
                  brand="satisfyrunning.com", url=STORE, strategy="shopify",
                  kind="collection", target_ref=None, last_state="watching",
                  baseline_json=json.dumps(["anchor"]), last_sweep_at=stamp())
    fields.update(kw)
    return db.create_watch(**fields)


# --- the morning itself ----------------------------------------------------

@respx.mock
async def test_the_whole_staging_morning_is_silent(sent):
    """All five, arriving one at a time as they did."""
    wid = store_watch()
    route = respx.get(url__startswith=FEED)
    batch = [anchor()]

    for handle, title in [
        ("techsilk™-drift-cordura-tights-cl", "TechSilk™ Drift Cordura Tights CL"),
        ("mothtech™-t-shirt-cl", "MothTech™ T-Shirt CL"),
        ("mothtech™-t-shirtcl", "MothTech™ T-ShirtCL"),
        ("moth-silver-womens", "MOTH SILVER WOMENS"),
        ("moth-pred-women", "moth pred women"),
    ]:
        batch.append(staged(handle, title))
        route.mock(return_value=feed(*batch))
        await scheduler.check_watch(db.get_watch(wid))

    assert sent == [], f"{len(sent)} alerts for a drop being assembled"


@respx.mock
async def test_a_finished_coming_soon_listing_still_speaks_up(sent):
    """The Rippy shorts: 260 USD, photographed, sized, not yet purchasable.
    That is a drop being announced, and it is the alert worth having."""
    wid = store_watch()
    respx.get(url__startswith=FEED).mock(return_value=feed(anchor(), staged(
        "rippy-3-trail-shorts-aged-black", 'Rippy™ 3" Trail Shorts',
        price="260.00", images=[{"src": "https://x/rippy.jpg"}])))

    await scheduler.check_watch(db.get_watch(wid))

    assert [p["arrival"] for p in sent] == [ARRIVAL_NEW]


@respx.mock
async def test_the_staged_product_is_armed_so_the_release_lands(sent):
    """Silence is only safe because nothing is forgotten. When the record
    acquires a price and goes on sale, that is the alert."""
    wid = store_watch()
    route = respx.get(url__startswith=FEED)

    route.mock(return_value=feed(anchor(), staged("moth-pred-women", "moth pred women")))
    await scheduler.check_watch(db.get_watch(wid))
    assert sent == []

    route.mock(return_value=feed(anchor(), staged(
        "moth-pred-women", "MothTech™ Pred Women", price="140.00",
        images=[{"src": "https://x/p.jpg"}], buyable=True)))
    await scheduler.check_watch(db.get_watch(wid))

    assert [p["arrival"] for p in sent] == ["launched"]
    assert sent[0]["first_sale"] == ["moth-pred-women"], "never seen on sale before"


@respx.mock
async def test_a_priced_product_you_can_buy_is_never_silenced(sent):
    wid = store_watch()
    respx.get(url__startswith=FEED).mock(return_value=feed(anchor(), staged(
        "real-drop", "Real Drop", price="295.00", buyable=True,
        images=[{"src": "https://x/r.jpg"}])))

    await scheduler.check_watch(db.get_watch(wid))

    assert [p["arrival"] for p in sent] == [ARRIVAL_NEW]


# --- the guards that keep this from muting real stores ---------------------

@respx.mock
async def test_a_feed_with_no_variants_cannot_be_judged_on_price(sent):
    """No variants means no price information, not a price of zero. Reading
    those as the same thing mutes every store whose feed omits variants."""
    wid = store_watch()
    respx.get(url__startswith=FEED).mock(return_value=feed(anchor(), {
        "handle": "no-variants", "title": "No Variants",
        "published_at": just_now(), "created_at": just_now()}))

    await scheduler.check_watch(db.get_watch(wid))

    assert [p["handles"] for p in sent] == [["no-variants"]]


@respx.mock
async def test_an_atom_only_store_is_not_muted_by_a_price_it_never_sends(sent):
    """The Atom fallback carries no prices at all. Same trap as the
    availability flag, and it has to be sprung the same way."""
    wid = store_watch(url=f"{STORE}/collections/shop-all", target_ref="shop-all",
                      baseline_json=json.dumps(["anchor"]))
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(403))
    respx.get(url__startswith=f"{STORE}/collections/shop-all.atom").mock(
        return_value=httpx.Response(200, text=f"""<?xml version="1.0"?>
          <feed xmlns="http://www.w3.org/2005/Atom">
            <entry><title>Anchor</title>
              <link href="{STORE}/products/anchor"/>
              <published>{(utcnow() - timedelta(days=200)).isoformat()}</published></entry>
            <entry><title>Real Drop</title>
              <link href="{STORE}/products/real-drop"/>
              <published>{just_now()}</published></entry>
          </feed>""", headers={"content-type": "application/atom+xml"}))

    await scheduler.check_watch(db.get_watch(wid))

    assert [p["handles"] for p in sent] == [["real-drop"]]


# --- the link we hand you has to survive being a link ----------------------

@respx.mock
async def test_a_trademark_sign_is_encoded_in_the_link_we_send():
    """A raw ™ in an href works in a browser by accident, not by rule."""
    from monitor.strategies import shopify

    respx.get(url__startswith=FEED).mock(return_value=feed(staged(
        "mothtech™-t-shirt-cl", "MothTech™ T-Shirt CL",
        price="140.00", images=[{"src": "https://x/m.jpg"}])))

    r = await shopify.check({"id": 1, "url": STORE, "kind": "collection",
                             "target_ref": None})

    url = r.extra["items"]["mothtech™-t-shirt-cl"]["url"]
    assert url.endswith("/products/mothtech%E2%84%A2-t-shirt-cl")
    assert "™" not in url


# --- and it explains itself when asked -------------------------------------

@respx.mock
def test_inspect_says_a_staged_record_is_staged(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr("monitor.config.API_KEY", "k")
    monkeypatch.setattr("monitor.main.API_KEY", "k")
    store_watch(baseline_json=json.dumps(["anchor", "moth-pred-women"]),
                availability_json=json.dumps(
                    {"moth-pred-women": {"available": False}}))

    respx.get(url__regex=r".*/products/moth-pred-women\.js$").mock(
        return_value=httpx.Response(200, json={
            "title": "moth pred women",
            "variants": [{"id": 1, "title": "Aged Black", "available": False,
                          "price": 0}]}))
    respx.get(url__regex=r".*/products/moth-pred-women$").mock(
        return_value=httpx.Response(200, text='''<html><script
          type="application/ld+json">{"@type":"Product","name":"moth pred women",
          "offers":{"@type":"Offer","price":"0.00",
          "availability":"https://schema.org/OutOfStock"}}</script></html>''',
            headers={"content-type": "text/html"}))
    respx.get(url__startswith=FEED).mock(
        return_value=feed(anchor(), staged("moth-pred-women", "moth pred women")))

    from monitor.main import app
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer k"})
        body = c.get("/api/inspect", params={
            "url": f"{STORE}/products/moth-pred-women"}).json()

    assert body["list_price"] is None
    assert body["has_image"] is False
    assert body["watches"][0]["still_being_built"] is True
    assert "still being built" in body["watches"][0]["verdict"]
    assert "alerts as a release" in body["watches"][0]["verdict"]
