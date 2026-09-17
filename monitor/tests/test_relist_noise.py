"""Alerts for old sold-out stock passing through the catalogue again.

Reported after moving from the curated shop-all collection to the whole store:
"a few alerts for items that were released a while ago and have been sold out
for a while and still are". The whole-store feed carries the archive, so
republished old stock churns through it far more than a curated collection —
which is a direct cost of the wider coverage, and has to be paid for somewhere
other than the notification.
"""
import json
from datetime import timedelta

import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.statemachine import (
    ARRIVAL_NEW, ARRIVAL_RELISTED, ARRIVAL_UNCONFIRMED,
)
from monitor.timeutil import stamp, utcnow

STORE = "https://www.satisfyrunning.com"
FEED = f"{STORE}/products.json"


def ago(**kw):
    return (utcnow() - timedelta(**kw)).isoformat()


def entry(handle, *, published, created, buyable, state_it=True):
    variant = {"id": abs(hash(handle)) % 9999, "title": "M", "price": "145.00"}
    if state_it:
        variant["available"] = buyable
    return {"handle": handle, "title": handle.replace("-", " ").title(),
            "published_at": published, "created_at": created,
            "variants": [variant]}


def feed(*entries):
    return httpx.Response(200, json={"products": list(entries)})


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


def anchor():
    return entry("anchor", published=ago(days=300), created=ago(days=300),
                 buyable=True)


@respx.mock
async def test_an_old_sold_out_item_republished_says_nothing(sent):
    """The exact complaint: released long ago, sold out since, still sold out,
    and the store put it back in the feed."""
    wid = store_watch()
    respx.get(url__startswith=FEED).mock(return_value=feed(anchor(), entry(
        "heatcrush-desert-shorts-moonstruck",
        published=ago(minutes=4), created=ago(days=400), buyable=False)))

    await scheduler.check_watch(db.get_watch(wid))

    assert sent == [], "a republished item you cannot buy is not news"


@respx.mock
async def test_but_it_is_armed_so_the_real_restock_still_lands(sent):
    """Silence costs nothing only if the product is still being watched."""
    wid = store_watch()
    route = respx.get(url__startswith=FEED)
    route.mock(return_value=feed(anchor(), entry(
        "heatcrush-arm-sleeves", published=ago(minutes=4),
        created=ago(days=400), buyable=False)))
    await scheduler.check_watch(db.get_watch(wid))
    assert sent == []

    route.mock(return_value=feed(anchor(), entry(
        "heatcrush-arm-sleeves", published=ago(minutes=4),
        created=ago(days=400), buyable=True)))
    await scheduler.check_watch(db.get_watch(wid))

    assert [p["arrival"] for p in sent] == ["launched"], \
        "the moment you can buy it, that is the alert worth having"


@respx.mock
async def test_a_relist_you_can_actually_buy_still_alerts(sent):
    """Old stock coming back ON SALE is a restock, and that is the thing this
    app is named after."""
    wid = store_watch()
    respx.get(url__startswith=FEED).mock(return_value=feed(anchor(), entry(
        "mothtech-tee", published=ago(minutes=3), created=ago(days=400),
        buyable=True)))

    await scheduler.check_watch(db.get_watch(wid))

    assert [p["arrival"] for p in sent] == [ARRIVAL_RELISTED]


@respx.mock
async def test_a_genuinely_new_listing_speaks_up_even_unbuyable(sent):
    """Coming soon is worth knowing about: it is how you learn a drop is due."""
    wid = store_watch()
    respx.get(url__startswith=FEED).mock(return_value=feed(anchor(), entry(
        "rippy-trail-shorts", published=ago(minutes=2), created=ago(days=6),
        buyable=False)))

    await scheduler.check_watch(db.get_watch(wid))

    assert [p["arrival"] for p in sent] == [ARRIVAL_NEW]


@respx.mock
async def test_an_undateable_arrival_you_cannot_buy_stays_quiet(sent):
    wid = store_watch()
    respx.get(url__startswith=FEED).mock(return_value=feed(anchor(), entry(
        "mystery", published=None, created=None, buyable=False)))

    await scheduler.check_watch(db.get_watch(wid))

    assert sent == []


@respx.mock
async def test_a_store_we_can_only_read_through_atom_is_not_silenced(sent):
    """Absence of the flag is not a "no".

    The Atom fallback — what we use when a store gates its JSON — carries no
    availability at all. Reading that silence as "not buyable" would suppress
    every relisted and undateable arrival from such a store, permanently and
    invisibly. This is the case the available_stated guard exists for; without
    it the whole fallback path goes quiet.
    """
    wid = store_watch(url=f"{STORE}/collections/shop-all", target_ref="shop-all")
    respx.get(url__startswith=f"{STORE}/collections/shop-all/products.json").mock(
        return_value=httpx.Response(403))
    respx.get(url__startswith=f"{STORE}/collections/shop-all.atom").mock(
        return_value=httpx.Response(200, text=f"""<?xml version="1.0"?>
          <feed xmlns="http://www.w3.org/2005/Atom">
            <entry><title>Anchor</title>
              <link href="{STORE}/products/anchor"/>
              <published>{ago(days=300)}</published></entry>
            <entry><title>Old Stock</title>
              <link href="{STORE}/products/old-stock"/>
              <published>{ago(minutes=4)}</published></entry>
          </feed>""", headers={"content-type": "application/atom+xml"}))

    await scheduler.check_watch(db.get_watch(wid))

    assert [p["handles"] for p in sent] == [["old-stock"]], \
        "an Atom-only store must not be muted by a flag it never sends"


@respx.mock
async def test_the_suppressed_item_is_still_remembered(sent):
    """It must not alert later either — silence is not the same as unseen."""
    wid = store_watch()
    respx.get(url__startswith=FEED).mock(return_value=feed(anchor(), entry(
        "old-stock", published=ago(minutes=4), created=ago(days=400),
        buyable=False)))

    await scheduler.check_watch(db.get_watch(wid))

    w = db.get_watch(wid)
    assert "old-stock" in json.loads(w["baseline_json"])
    assert json.loads(w["availability_json"])["old-stock"]["available"] is False
