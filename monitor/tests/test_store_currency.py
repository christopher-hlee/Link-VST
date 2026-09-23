"""Prices are shown in dollars, because that is what the feed gives us.

A detector once asked each Shopify store for its currency via /meta.json and
trusted the answer. That field is the shop's BASE currency; products.json
prices in the currency the shop presents to the visitor, and this server is in
the US. RAGTAG's base is JPY, its feed says 575 for trousers the site sells at
$575.00, and the alert went out as "¥575" — off by a factor of about 150, in
the flattering direction. These tests pin the correction.
"""
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from monitor import db, scheduler
from monitor.notify import telegram
from monitor.timeutil import stamp, utcnow

STORE = "https://ragtag-global.com"
COLLECTION = f"{STORE}/collections/yohjiyamamotopourhomme"
FEED = f"{COLLECTION}/products.json"


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


def ragtag(**kw):
    fields = dict(name="ragtag-global.com · yohjiyamamotopourhomme",
                  brand="ragtag-global.com", url=COLLECTION, strategy="shopify",
                  kind="collection", target_ref="yohjiyamamotopourhomme",
                  last_state="watching", baseline_json=json.dumps(["old"]),
                  last_sweep_at=stamp())
    fields.update(kw)
    return db.create_watch(**fields)


def trousers():
    now = utcnow().strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
    return {"handle": "yohji-other", "title": "yohji yamamoto POUR HOMME Other",
            "published_at": now, "created_at": now,
            "variants": [{"id": 1, "title": "3", "available": True,
                          "price": "575.00"}]}


@respx.mock
async def test_the_store_is_never_asked_for_its_currency(sent):
    """Its answer is the base currency, which is not what the feed uses."""
    wid = ragtag()
    meta = respx.get(f"{STORE}/meta.json").mock(
        return_value=httpx.Response(200, json={"currency": "JPY"}))
    respx.get(url__startswith=FEED).mock(
        return_value=httpx.Response(200, json={"products": [trousers()]}))

    await scheduler.check_watch(db.get_watch(wid))

    assert meta.call_count == 0
    assert (db.get_watch(wid)["currency"] or "USD") == "USD"


@respx.mock
async def test_the_alert_from_the_screenshot_reads_in_dollars(sent):
    """The exact message: 1 back in stock, yohji ... Other — $575, not ¥575."""
    wid = ragtag()
    respx.get(url__startswith=FEED).mock(
        return_value=httpx.Response(200, json={"products": [trousers()]}))

    await scheduler.check_watch(db.get_watch(wid))
    text = telegram.render(db.get_watch(wid), sent[0]["kind"], sent[0]["payload"])

    assert "$575" in text
    assert "¥" not in text


def test_watches_the_detector_switched_go_back_to_dollars(sent):
    """What the deployed detector already did is undone at the next start."""
    switched = ragtag(currency="JPY")
    db.kv_set(f"currency_probe:{switched}", "JPY")
    untouched = ragtag(currency="EUR")          # set some other way: leave it
    unrecorded = ragtag()
    db.kv_set(f"currency_probe:{unrecorded}", "unstated")

    db.init_db()

    assert db.get_watch(switched)["currency"] == "USD"
    assert db.get_watch(untouched)["currency"] == "EUR"
    assert db.kv_get(f"currency_probe:{switched}") is None
    assert db.kv_get(f"currency_probe:{unrecorded}") is None


def test_undoing_it_twice_changes_nothing(sent):
    wid = ragtag(currency="JPY")
    db.kv_set(f"currency_probe:{wid}", "JPY")
    db.init_db()
    db.update_watch(wid, currency="EUR")        # a later, deliberate change

    db.init_db()

    assert db.get_watch(wid)["currency"] == "EUR"


# --- the API still refuses nonsense ------------------------------------------

@pytest.fixture
def client(sent, monkeypatch):
    monkeypatch.setattr("monitor.config.API_KEY", "k")
    monkeypatch.setattr("monitor.main.API_KEY", "k")
    from monitor.main import app
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer k"})
        yield c


def test_a_currency_must_be_a_code(client):
    wid = ragtag()
    for bad in ("¥", "jp", "Japanese yen"):
        assert client.patch(f"/api/watches/{wid}",
                            json={"currency": bad}).status_code == 400, bad
    r = client.patch(f"/api/watches/{wid}", json={"currency": "usd"})
    assert r.status_code == 200 and r.json()["watch"]["currency"] == "USD"


async def test_an_fx_rate_on_a_dollar_watch_is_called_out(sent):
    from monitor import telegram_bot
    wid = ragtag()

    reply = await telegram_bot.handle(f"/star {wid} max=450 fx=142")

    assert "priced in USD" in reply and "not used" in reply
