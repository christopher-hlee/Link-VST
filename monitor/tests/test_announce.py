"""Announcement watcher — detecting arrival rather than availability.

This is the only strategy that can track something with no product page yet,
which is the whole Zelda-console case: by the time there is a URL to poll,
preorders have already come and gone.
"""
import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.statemachine import IN_STOCK, OUT_OF_STOCK
from monitor.strategies import announce, detect

FEED = "https://www.nintendo.com/whatsnew.xml"

ATOM_TEMPLATE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
{entries}
</feed>"""

ATOM_ENTRY = """<entry>
  <id>{id}</id><title>{title}</title>
  <summary>{summary}</summary>
  <link href="https://www.nintendo.com/news/{id}"/>
</entry>"""

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><guid>n-1</guid><title>Zelda 40th Anniversary Switch 2 revealed</title>
    <description>A special edition console.</description>
    <link>https://example.com/1</link></item>
  <item><guid>n-2</guid><title>Mario Kart update</title>
    <description>Patch notes.</description><link>https://example.com/2</link></item>
</channel></rss>"""


def atom(*entries):
    return ATOM_TEMPLATE.format(entries="\n".join(
        ATOM_ENTRY.format(**e) for e in entries))


def watch(**kw):
    base = {"id": 1, "url": FEED, "kind": "collection",
            "target_ref": "zelda, switch 2"}
    base.update(kw)
    return base


# --- keyword filtering -----------------------------------------------------

@respx.mock
async def test_only_matching_entries_are_reported():
    respx.get(FEED).mock(return_value=httpx.Response(200, text=atom(
        {"id": "a1", "title": "Zelda 40th Anniversary console", "summary": ""},
        {"id": "a2", "title": "Splatoon splatfest results", "summary": ""},
    ), headers={"Content-Type": "application/atom+xml"}))

    r = await announce.check(watch())

    assert r.ok and r.handles == ["a1"]
    assert r.extra["entries_seen"] == 2 and r.extra["matches"] == 1
    assert r.extra["titles"]["a1"].startswith("Zelda")


@respx.mock
async def test_keyword_matches_against_summary_not_just_title():
    respx.get(FEED).mock(return_value=httpx.Response(200, text=atom(
        {"id": "a1", "title": "Nintendo Direct recap",
         "summary": "Includes the Ocarina of Time remake for Switch 2."},
    ), headers={"Content-Type": "application/atom+xml"}))

    r = await announce.check(watch(target_ref="ocarina"))
    assert r.handles == ["a1"]


@respx.mock
async def test_empty_keywords_match_everything():
    respx.get(FEED).mock(return_value=httpx.Response(200, text=atom(
        {"id": "a1", "title": "Anything", "summary": ""},
        {"id": "a2", "title": "Else", "summary": ""},
    ), headers={"Content-Type": "application/atom+xml"}))

    r = await announce.check(watch(target_ref=""))
    assert r.handles == ["a1", "a2"]


@respx.mock
async def test_no_matches_is_out_of_stock_not_an_error():
    respx.get(FEED).mock(return_value=httpx.Response(200, text=atom(
        {"id": "a1", "title": "Unrelated", "summary": ""},
    ), headers={"Content-Type": "application/atom+xml"}))

    r = await announce.check(watch())
    assert r.ok and r.state == OUT_OF_STOCK and r.handles == []


# --- feed formats ----------------------------------------------------------

@respx.mock
async def test_rss_two_point_oh_is_parsed():
    respx.get(FEED).mock(return_value=httpx.Response(
        200, text=RSS, headers={"Content-Type": "application/rss+xml"}))

    r = await announce.check(watch())
    assert r.ok and r.handles == ["n-1"]


@respx.mock
async def test_shopify_search_json_is_parsed():
    respx.get("https://satisfyrunning.com/search/suggest.json").mock(
        return_value=httpx.Response(200, json={"resources": {"results": {"products": [
            {"handle": "zelda-tee", "title": "Zelda Tee", "url": "/products/zelda-tee"},
            {"handle": "other", "title": "Other", "url": "/products/other"}]}}}))

    r = await announce.check(watch(url="https://satisfyrunning.com/search/suggest.json"))
    assert r.ok and r.handles == ["zelda-tee"]
    assert r.extra["source"] == "search-json"


@respx.mock
async def test_unparseable_body_is_an_error_not_an_empty_result():
    """An empty result would silently establish a baseline of nothing."""
    respx.get(FEED).mock(return_value=httpx.Response(
        200, text="<html>not a feed</html>", headers={"Content-Type": "text/html"}))

    r = await announce.check(watch())
    assert r.ok is False and r.state is None


# --- ids are stable --------------------------------------------------------

@respx.mock
async def test_reworded_title_does_not_realert_when_id_is_stable():
    route = respx.get(FEED)
    route.mock(return_value=httpx.Response(200, text=atom(
        {"id": "a1", "title": "Zelda console announced", "summary": ""},
    ), headers={"Content-Type": "application/atom+xml"}))
    first = await announce.check(watch())

    route.mock(return_value=httpx.Response(200, text=atom(
        {"id": "a1", "title": "Zelda console announced (updated)", "summary": ""},
    ), headers={"Content-Type": "application/atom+xml"}))
    second = await announce.check(watch())

    assert first.handles == second.handles == ["a1"]


def test_entry_without_id_falls_back_to_a_title_hash():
    a = announce.entry_id(None, "Zelda Switch 2")
    b = announce.entry_id(None, "  zelda switch 2  ")
    assert a == b, "whitespace and case must not create a phantom new entry"
    assert a != announce.entry_id(None, "Something else")


# --- detection routing -----------------------------------------------------

@respx.mock
async def test_detect_claims_a_feed():
    respx.get(FEED).mock(return_value=httpx.Response(200, text=atom(
        {"id": "a1", "title": "x", "summary": ""}),
        headers={"Content-Type": "application/atom+xml"}))

    found = await detect(FEED)
    assert found["strategy"] == "announce"
    assert found["kind"] == "collection"


@respx.mock
async def test_detect_defers_shopify_collection_feeds_to_shopify():
    """Both can parse it, but shopify pulls real product handles out."""
    url = "https://satisfyrunning.com/collections/all.atom"
    respx.get(url).mock(return_value=httpx.Response(200, text=(
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><title>T</title>'
        '<link href="https://satisfyrunning.com/products/t"/></entry></feed>')))
    respx.get(url__startswith="https://satisfyrunning.com/collections/all/products.json"
              ).mock(return_value=httpx.Response(200, json={"products": []}))

    assert await announce.detect(url) is None
    found = await detect(url)
    assert found["strategy"] == "shopify"


@respx.mock
async def test_detect_ignores_a_plain_product_page():
    assert await announce.detect("https://shop.com/products/thing") is None


# --- end to end through the scheduler --------------------------------------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


@respx.mock
async def test_new_announcement_alerts_once(temp_db, monkeypatch):
    sent = []

    async def fake_send(w, kind, payload):
        sent.append((kind, payload))

    monkeypatch.setattr("monitor.notify.telegram.send_event", fake_send)
    monkeypatch.setattr("monitor.notify.telegram.configured", lambda: True)
    monkeypatch.setattr("monitor.notify.heartbeat.ping", lambda suffix="": None)

    wid = db.create_watch(name="Nintendo news", brand="Nintendo", url=FEED,
                          strategy="announce", kind="collection",
                          target_ref="zelda, switch 2")
    route = respx.get(FEED)

    # Baseline: an existing matching entry must not fire.
    route.mock(return_value=httpx.Response(200, text=atom(
        {"id": "old", "title": "Zelda retrospective", "summary": ""}),
        headers={"Content-Type": "application/atom+xml"}))
    await scheduler.check_watch(db.get_watch(wid))
    assert sent == []

    # The announcement lands.
    route.mock(return_value=httpx.Response(200, text=atom(
        {"id": "old", "title": "Zelda retrospective", "summary": ""},
        {"id": "new", "title": "Zelda 40th Anniversary Switch 2 edition", "summary": ""}),
        headers={"Content-Type": "application/atom+xml"}))
    await scheduler.check_watch(db.get_watch(wid))

    assert [k for k, _ in sent] == ["new_product"]
    assert sent[0][1]["handles"] == ["new"]

    # And does not fire again on subsequent polls.
    await scheduler.check_watch(db.get_watch(wid))
    assert len(sent) == 1
