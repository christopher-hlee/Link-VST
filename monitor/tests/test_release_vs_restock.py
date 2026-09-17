"""A release and a restock are not the same event.

The storefront says so plainly — "Coming Soon" against "Notify Me When
Available" — but the catalogue API collapses both into `available: false`, so
nothing in the feed separates them. Our own history does: a product we have
never once seen on sale has not been released yet. Everything here is about
keeping those two apart, and about not overclaiming a debut we cannot see
further back than the day we started watching.
"""
import json
from datetime import timedelta

import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.notify import telegram
from monitor.timeutil import stamp, utcnow

STORE = "https://www.satisfyrunning.com"
FEED = f"{STORE}/products.json"


def entry(handle, *, buyable, published=None):
    published = published or (utcnow() - timedelta(days=20)).isoformat()
    return {"handle": handle, "title": handle.replace("-", " ").title(),
            "published_at": published, "created_at": published,
            "variants": [{"id": abs(hash(handle)) % 9999, "title": "M",
                          "available": buyable, "price": "260.00"}]}


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
                  baseline_json=json.dumps(["anchor", "subject"]),
                  last_sweep_at=stamp())
    fields.update(kw)
    return db.create_watch(**fields)


def anchor():
    return entry("anchor", buyable=True,
                 published=(utcnow() - timedelta(days=300)).isoformat())


# --- the ledger has to remember having seen it on sale ---------------------

@respx.mock
async def test_seeing_it_on_sale_once_is_remembered(sent):
    wid = store_watch(availability_json=json.dumps(
        {"subject": {"available": True}}))
    route = respx.get(url__startswith=FEED)

    route.mock(return_value=feed(anchor(), entry("subject", buyable=True)))
    await scheduler.check_watch(db.get_watch(wid))
    route.mock(return_value=feed(anchor(), entry("subject", buyable=False)))
    await scheduler.check_watch(db.get_watch(wid))

    ledger = json.loads(db.get_watch(wid)["availability_json"])
    assert ledger["subject"] == {"available": False, "ever_buyable": True}, \
        "sold out is a product with a past; the past must survive it"


# --- coming soon -> on sale is a RELEASE -----------------------------------

@respx.mock
async def test_a_coming_soon_product_going_live_is_a_release(sent):
    """Never once seen on sale, then buyable. That is a drop."""
    wid = store_watch(availability_json=json.dumps(
        {"subject": {"available": False}}))
    respx.get(url__startswith=FEED).mock(
        return_value=feed(anchor(), entry("subject", buyable=True)))

    await scheduler.check_watch(db.get_watch(wid))

    assert sent[0]["first_sale"] == ["subject"]
    body = telegram.render({"name": "Satisfy", "brand": "satisfyrunning.com"},
                           "new_product", sent[0])
    assert "released" in body
    assert "first time since I started watching" in body
    assert "back in stock" not in body


# --- sold out -> on sale is a RESTOCK --------------------------------------

@respx.mock
async def test_a_sold_out_product_returning_is_a_restock(sent):
    wid = store_watch(availability_json=json.dumps(
        {"subject": {"available": False, "ever_buyable": True}}))
    respx.get(url__startswith=FEED).mock(
        return_value=feed(anchor(), entry("subject", buyable=True)))

    await scheduler.check_watch(db.get_watch(wid))

    assert sent[0]["first_sale"] == []
    body = telegram.render({"name": "Satisfy", "brand": "satisfyrunning.com"},
                           "new_product", sent[0])
    assert "back in stock" in body
    assert "released" not in body


def test_a_mixed_batch_counts_both_kinds():
    body = telegram.render(
        {"name": "Satisfy", "brand": "satisfyrunning.com"}, "new_product",
        {"handles": ["a", "b", "c"], "titles": {}, "arrival": "launched",
         "first_sale": ["a"]})

    assert "3 now on sale" in body
    assert "1 for the first time, 2 back in stock" in body


def test_the_claim_is_bounded_by_when_we_started_watching():
    """We cannot see what the store sold before we existed, so the alert says
    "since I started watching" rather than asserting a debut."""
    body = telegram.render(
        {"name": "Satisfy", "brand": "satisfyrunning.com"}, "new_product",
        {"handles": ["a"], "titles": {}, "arrival": "launched",
         "first_sale": ["a"]})

    assert "since I started watching" in body
    assert "never been on sale" not in body


# --- the whole sequence, as the storefront shows it ------------------------

@respx.mock
async def test_coming_soon_then_release_then_sold_out_then_restock(sent):
    """Rippy shorts, end to end: Coming Soon, drops, sells out, comes back.
    Two alerts, correctly named, and nothing in between."""
    wid = store_watch(baseline_json=json.dumps(["anchor", "rippy"]),
                      availability_json=json.dumps({"rippy": {"available": False}}))
    route = respx.get(url__startswith=FEED)

    async def sweep(buyable):
        route.mock(return_value=feed(anchor(), entry("rippy", buyable=buyable)))
        await scheduler.check_watch(db.get_watch(wid))

    await sweep(False)                      # still Coming Soon
    assert sent == []

    await sweep(True)                       # drops
    assert sent[-1]["first_sale"] == ["rippy"]

    await sweep(False)                      # sells out
    assert len(sent) == 1, "selling out is not an alert"

    # Past the relaunch cooldown, as a day would be.
    ledger = json.loads(db.get_watch(wid)["availability_json"])
    ledger["rippy"]["alerted_at"] = stamp(utcnow() - timedelta(hours=2))
    db.update_watch(wid, availability_json=json.dumps(ledger))

    await sweep(True)                       # comes back
    assert len(sent) == 2
    assert sent[-1]["first_sale"] == [], "the second time is a restock"
