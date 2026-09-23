"""A watch learns what currency its store charges in.

products.json prices are bare numbers. Every watch defaulted to dollars and
nothing — not the dashboard, not /add — could set anything else, so a RAGTAG
coat at 49,160 yen rendered as $49,160, and with stars configured its landed
cost came out near $59,000. No star could ever clear a $450 ceiling, and the
fx rate the person supplied was silently ignored.
"""
import json
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from monitor import db, scheduler
from monitor.timeutil import stamp, utcnow

STORE = "https://ragtag-global.com"
COLLECTION = f"{STORE}/collections/comoli"
FEED = f"{COLLECTION}/products.json"
META = f"{STORE}/meta.json"


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
    fields = dict(name="ragtag-global.com · comoli", brand="ragtag-global.com",
                  url=COLLECTION, strategy="shopify", kind="collection",
                  target_ref="comoli", last_state="watching",
                  baseline_json=json.dumps(["old"]), last_sweep_at=stamp(),
                  filter_json=json.dumps({"star": {"max_landed": 450,
                                                   "fx_per_usd": 142}}))
    fields.update(kw)
    return db.create_watch(**fields)


def coat():
    now = utcnow().strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
    return {"handle": "comoli-coat", "title": "COMOLI Tielocken Coat",
            "published_at": now, "created_at": now,
            "variants": [{"id": 1, "title": "2", "available": True,
                          "price": "49160"}]}


def feed():
    return httpx.Response(200, json={"products": [coat()]})


@respx.mock
async def test_a_yen_store_is_valued_in_yen_from_its_first_sweep(sent):
    """The bug, end to end: default currency, fx configured, yen prices."""
    wid = ragtag()
    respx.get(META).mock(return_value=httpx.Response(
        200, json={"name": "RAGTAG", "currency": "JPY"}))
    respx.get(url__startswith=FEED).mock(return_value=feed())

    await scheduler.check_watch(db.get_watch(wid))

    assert db.get_watch(wid)["currency"] == "JPY"
    item = sent[0]["payload"]["items"]["comoli-coat"]
    assert item["landed"] < 450, f"¥49,160 valued as ${item['landed']:,.0f}"
    assert item["starred"] is True, item.get("star_reason")


@respx.mock
async def test_the_store_is_asked_once_not_every_sweep(sent):
    wid = ragtag()
    meta = respx.get(META).mock(return_value=httpx.Response(
        200, json={"currency": "JPY"}))
    respx.get(url__startswith=FEED).mock(return_value=feed())

    for _ in range(3):
        await scheduler.check_watch(db.get_watch(wid))

    assert meta.call_count == 1


@respx.mock
async def test_an_unreachable_store_is_asked_again_a_day_later(sent):
    wid = ragtag()
    meta = respx.get(META).mock(return_value=httpx.Response(503))
    respx.get(url__startswith=FEED).mock(return_value=feed())

    await scheduler.check_watch(db.get_watch(wid))
    await scheduler.check_watch(db.get_watch(wid))
    assert meta.call_count == 1, "a failing store must not be asked every tick"

    yesterday = (utcnow() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    db.kv_set(scheduler.currency_key(wid), "retry@" + yesterday)
    meta.mock(return_value=httpx.Response(200, json={"currency": "JPY"}))
    await scheduler.check_watch(db.get_watch(wid))

    assert meta.call_count == 2
    assert db.get_watch(wid)["currency"] == "JPY"


@respx.mock
@pytest.mark.parametrize("reply", [
    httpx.Response(404),
    httpx.Response(200, json={"name": "no currency here"}),
    httpx.Response(200, json={"currency": "¥"}),
    httpx.Response(200, text="<html>not json</html>"),
])
async def test_a_store_that_does_not_say_leaves_the_currency_alone(sent, reply):
    wid = ragtag(currency="EUR")
    meta = respx.get(META).mock(return_value=reply)
    respx.get(url__startswith=FEED).mock(return_value=feed())

    await scheduler.check_watch(db.get_watch(wid))
    await scheduler.check_watch(db.get_watch(wid))

    assert db.get_watch(wid)["currency"] == "EUR"
    assert meta.call_count == 1, "an answer without a currency is still final"


@respx.mock
async def test_only_shopify_stores_are_asked(sent):
    wid = db.create_watch(name="feed", brand="x", url="https://news.example/feed",
                          strategy="announce", kind="collection", target_ref="zelda")
    meta = respx.get("https://news.example/meta.json").mock(
        return_value=httpx.Response(200, json={"currency": "JPY"}))
    respx.get("https://news.example/feed").mock(
        return_value=httpx.Response(200, text="<rss/>"))

    await scheduler.check_watch(db.get_watch(wid))

    assert meta.call_count == 0


# --- a person's choice wins -------------------------------------------------

@pytest.fixture
def client(sent, monkeypatch):
    monkeypatch.setattr("monitor.config.API_KEY", "k")
    monkeypatch.setattr("monitor.main.API_KEY", "k")
    from monitor.main import app
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer k"})
        yield c


@respx.mock
async def test_a_currency_set_by_hand_is_never_overridden(client, sent):
    wid = ragtag()
    r = client.patch(f"/api/watches/{wid}", json={"currency": "usd"})
    assert r.status_code == 200 and r.json()["watch"]["currency"] == "USD"

    meta = respx.get(META).mock(return_value=httpx.Response(
        200, json={"currency": "JPY"}))
    respx.get(url__startswith=FEED).mock(return_value=feed())
    await scheduler.check_watch(db.get_watch(wid))

    assert meta.call_count == 0
    assert db.get_watch(wid)["currency"] == "USD"


def test_a_currency_must_be_a_code(client):
    wid = ragtag()
    for bad in ("¥", "jp", "Japanese yen"):
        assert client.patch(f"/api/watches/{wid}",
                            json={"currency": bad}).status_code == 400, bad
    assert db.get_watch(wid)["currency"] in (None, "USD")


def test_moving_a_watch_to_another_store_asks_again(client):
    wid = ragtag()
    db.kv_set(scheduler.currency_key(wid), "JPY")

    client.patch(f"/api/watches/{wid}",
                 json={"url": "https://www.satisfyrunning.com/collections/all"})

    assert db.kv_get(scheduler.currency_key(wid)) is None


def test_but_not_when_the_currency_was_chosen_by_hand(client):
    wid = ragtag()
    client.patch(f"/api/watches/{wid}", json={"currency": "JPY"})

    client.patch(f"/api/watches/{wid}",
                 json={"url": "https://www.satisfyrunning.com/collections/all"})

    assert db.kv_get(scheduler.currency_key(wid)) == "manual"


# --- and the bot says so, rather than ignoring the rate --------------------

async def test_an_fx_rate_on_a_dollar_watch_is_called_out(sent):
    from monitor import telegram_bot
    wid = ragtag(filter_json=None)

    reply = await telegram_bot.handle(f"/star {wid} max=450 fx=142")

    assert "priced in USD" in reply and "fx= is not used" in reply


async def test_no_warning_once_the_currency_is_right(sent):
    from monitor import telegram_bot
    wid = ragtag(filter_json=None, currency="JPY")

    reply = await telegram_bot.handle(f"/star {wid} max=450 fx=142")

    assert "not used" not in reply
