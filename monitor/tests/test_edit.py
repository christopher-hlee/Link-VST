"""Editing a watch.

The interesting cases are not the field updates — they are the side effects of
changing what a watch is looking at, which the person doing the editing has no
way to reason about themselves.
"""
import pytest
from fastapi.testclient import TestClient

from monitor import db
from monitor.statemachine import HELD, IN_STOCK, UNKNOWN


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr("monitor.config.API_KEY", "k")
    monkeypatch.setattr("monitor.main.API_KEY", "k")
    from monitor.main import app
    with TestClient(app) as c:
        c.headers.update({"Authorization": "Bearer k"})
        yield c


def make(**kw):
    fields = dict(name="Tee", brand="Satisfy", url="https://s.com/products/tee",
                  strategy="shopify", kind="product", target_ref="tee")
    fields.update(kw)
    return db.create_watch(**fields)


def patch(client, wid, **body):
    r = client.patch(f"/api/watches/{wid}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- plain field edits -----------------------------------------------------

def test_renaming_leaves_state_alone(client):
    wid = make(last_state=IN_STOCK, baseline_json='["a"]')
    w = patch(client, wid, name="Renamed")["watch"]
    assert w["name"] == "Renamed"
    assert w["last_state"] == IN_STOCK, "a rename is not a change of subject"
    assert w["baseline_json"] == '["a"]'


def test_tier_maps_to_an_interval(client):
    wid = make()
    assert patch(client, wid, tier="fast")["watch"]["base_interval_s"] == 45
    assert patch(client, wid, tier="slow")["watch"]["base_interval_s"] == 900


def test_bad_tier_is_rejected(client):
    wid = make()
    assert client.patch(f"/api/watches/{wid}", json={"tier": "warp"}).status_code == 400


def test_bad_url_is_rejected(client):
    wid = make()
    assert client.patch(f"/api/watches/{wid}", json={"url": "nope"}).status_code == 400


# --- changing what the watch looks at --------------------------------------

def test_changing_keywords_resets_the_baseline(client):
    """Broadening a filter against an old baseline would report every
    previously-ignored entry as brand new."""
    wid = make(strategy="announce", kind="collection", target_ref="zelda + ocarina",
               baseline_json='["a","b","c"]', last_state="watching")

    out = patch(client, wid, target_ref="zelda")

    assert out["rebaselined"] is True
    assert out["watch"]["baseline_json"] is None
    assert out["watch"]["last_state"] == UNKNOWN
    assert out["watch"]["next_check_at"] == "1970-01-01 00:00:00"


def test_changing_the_url_resets_the_baseline_and_clears_the_error(client):
    wid = make(url="https://s.com/products/old", consecutive_failures=4,
               last_error="HTTP 410", baseline_json='["a"]')

    out = patch(client, wid, url="https://s.com/products/new")

    assert out["rebaselined"] is True
    assert out["watch"]["consecutive_failures"] == 0
    assert out["watch"]["last_error"] is None


def test_setting_the_same_value_is_not_a_change(client):
    wid = make(target_ref="tee", baseline_json='["a"]', last_state=IN_STOCK)
    out = patch(client, wid, target_ref="tee")
    assert out["rebaselined"] is False
    assert out["watch"]["baseline_json"] == '["a"]', "a no-op edit must not reset anything"


def test_changing_size_reschedules_but_keeps_the_baseline(client):
    """A new size changes what counts as a match, not what is being watched."""
    wid = make(size_pref="m", last_state=HELD, baseline_json='["a"]')

    out = patch(client, wid, size_pref="m, xl")

    assert out["rebaselined"] is False
    assert out["watch"]["baseline_json"] == '["a"]'
    assert out["watch"]["next_check_at"] == "1970-01-01 00:00:00", \
        "held should resolve on the next tick, not at the next scheduled poll"


def test_clearing_a_size_preference_back_to_any(client):
    """Sending null must clear it — the field has to be un-settable."""
    wid = make(size_pref="m")
    assert patch(client, wid, size_pref=None)["watch"]["size_pref"] is None


# --- pause and resume ------------------------------------------------------

def test_resuming_an_auto_paused_watch_clears_the_failure_count(client):
    """Otherwise it re-pauses on the very next tick and looks unfixable."""
    wid = make(enabled=0, consecutive_failures=6, last_error="HTTP 410")

    w = patch(client, wid, enabled=True)["watch"]

    assert w["enabled"] == 1
    assert w["consecutive_failures"] == 0


def test_pausing_does_not_clear_history(client):
    wid = make(consecutive_failures=3, last_error="HTTP 500")
    w = patch(client, wid, enabled=False)["watch"]
    assert w["enabled"] == 0
    assert w["consecutive_failures"] == 3


def test_delete_removes_the_watch(client):
    wid = make()
    assert client.delete(f"/api/watches/{wid}").status_code == 200
    assert client.get(f"/api/watches/{wid}").status_code == 404


# --- creating ---------------------------------------------------------------

def test_pinning_a_strategy_still_produces_a_readable_name(client):
    """Naming a strategy skips detection, which is where a name usually comes
    from — the list would otherwise show the raw feed URL as the title."""
    r = client.post("/api/watches", json={
        "url": "https://www.nintendolife.com/feeds/latest",
        "strategy": "announce", "kind": "collection",
        "target_ref": "zelda + ocarina"})

    assert r.status_code == 201, r.text
    w = r.json()["watch"]
    assert w["name"] == "nintendolife.com · zelda + ocarina"
    assert w["brand"] == "nintendolife.com"


def test_an_explicit_name_is_never_overwritten(client):
    r = client.post("/api/watches", json={
        "url": "https://www.nintendolife.com/feeds/latest", "name": "Zelda watch",
        "brand": "Nintendo Life", "strategy": "announce", "kind": "collection"})

    w = r.json()["watch"]
    assert (w["name"], w["brand"]) == ("Zelda watch", "Nintendo Life")


# --- deleting alerts, and the name backfill --------------------------------

def test_deleting_one_alert_leaves_the_rest(client):
    wid = make()
    keep = db.insert_event(wid, "restock", "out_of_stock", "in_stock", {"title": "Tee"})
    drop = db.insert_event(wid, "new_product", "watching", "watching", {"handles": ["a"]})

    assert client.delete(f"/api/events/{drop}").status_code == 200
    assert [e["id"] for e in db.list_events()] == [keep]
    assert client.delete(f"/api/events/{drop}").status_code == 404


def test_clearing_alerts_keeps_the_watches(client):
    wid = make()
    db.insert_event(wid, "restock", None, "in_stock", {})
    db.insert_event(wid, "new_product", None, "watching", {})

    r = client.delete("/api/events")

    assert r.status_code == 200 and r.json()["deleted"] == 2
    assert db.list_events() == []
    assert db.get_watch(wid) is not None, "clearing history must not touch watches"


def test_backfill_renames_a_watch_that_shows_its_url(client):
    wid = make(name="https://www.nintendolife.com/feeds/latest", brand="",
               url="https://www.nintendolife.com/feeds/latest",
               strategy="announce", kind="collection", target_ref="zelda + ocarina")

    assert db.backfill_watch_names() == 1
    w = db.get_watch(wid)
    assert w["name"] == "nintendolife.com · zelda + ocarina"
    assert w["brand"] == "nintendolife.com"
    assert db.backfill_watch_names() == 0, "must be idempotent"


def test_backfill_leaves_real_names_alone(client):
    wid = make(name="Satisfy new arrivals", brand="Satisfy")
    db.backfill_watch_names()
    assert db.get_watch(wid)["name"] == "Satisfy new arrivals"


def test_bestbuy_without_a_key_is_refused_at_creation(client, monkeypatch):
    """Otherwise it fails five checks, auto-pauses, and reads as broken
    rather than as unconfigured."""
    monkeypatch.setattr("monitor.routes.watches.BESTBUY_API_KEY", "")
    r = client.post("/api/watches", json={
        "url": "https://www.bestbuy.com/site/x/6521430.p", "strategy": "bestbuy"})

    assert r.status_code == 422
    assert "developer.bestbuy.com" in r.json()["detail"]
