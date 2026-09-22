"""A search results page is not a place you can watch.

`havenshop.com/search?q=auralee&sort=newest` is a perfectly good way for a
person to find AURALEE, and a trap for a monitor: Shopify's search is not
exposed through the API, so a watch built from that URL polls the entire
catalogue and ignores the term. It returns products, reports healthy, and
answers a different question than the one asked — the failure mode this whole
codebase keeps running into, and the only one worth catching at the door.
"""
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from monitor import db, telegram_bot
from monitor.strategies.shopify import search_query

HAVEN = "https://havenshop.com"


@pytest.mark.parametrize("url,expected", [
    (f"{HAVEN}/search?q=auralee&sort=newest", "auralee"),
    (f"{HAVEN}/search?q=yohji+yamamoto", "yohji yamamoto"),
    (f"{HAVEN}/search/?q=auralee", "auralee"),
    (f"{HAVEN}/collections/auralee", None),
    (f"{HAVEN}/collections/auralee?pf_t_size=size_L", None),
    (f"{HAVEN}/search", None),
    (f"{HAVEN}/search?q=", None),
    ("https://ragtag-global.com/collections/comoli", None),
])
def test_a_search_page_is_told_apart_from_a_collection(url, expected):
    assert search_query(url) == expected


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr("monitor.config.API_KEY", "k")
    monkeypatch.setattr("monitor.main.API_KEY", "k")
    from monitor.main import app
    with TestClient(app, base_url="https://testserver") as c:
        c.headers.update({"Authorization": "Bearer k"})
        yield c


@respx.mock
def test_creating_a_watch_from_a_search_url_is_refused(client):
    """It would otherwise succeed, which is the problem."""
    whole_store = respx.get(url__startswith=f"{HAVEN}/products.json").mock(
        return_value=httpx.Response(200, json={"products": [
            {"handle": "some-other-brand-tee", "title": "Not AURALEE"}]}))

    r = client.post("/api/watches", json={"url": f"{HAVEN}/search?q=auralee"})

    assert r.status_code == 422
    assert "auralee" in r.json()["detail"]
    assert "/collections/auralee" in r.json()["detail"]
    assert not whole_store.called, "and it never went looking"
    assert db.list_watches() == []


@respx.mock
def test_the_collection_form_is_accepted(client):
    respx.get(url__startswith=f"{HAVEN}/collections/auralee/products.json").mock(
        return_value=httpx.Response(200, json={"products": [
            {"handle": "auralee-coat", "title": "AURALEE Coat"}]}))

    r = client.post("/api/watches", json={"url": f"{HAVEN}/collections/auralee"})

    assert r.status_code == 201, r.text
    assert db.get_watch(r.json()["watch"]["id"])["target_ref"] == "auralee"


async def test_the_bot_refuses_it_too_and_says_what_to_use():
    reply = await telegram_bot.handle(f"/add {HAVEN}/search?q=auralee&sort=newest")

    assert "search results page" in reply
    assert "/collections/auralee" in reply


async def test_check_refuses_it_without_probing():
    reply = await telegram_bot.handle(f"/check {HAVEN}/search?q=auralee")

    assert "search results page" in reply
