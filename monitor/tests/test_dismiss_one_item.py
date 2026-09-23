"""Dismissing one product from an alert that found several.

The dashboard groups drops by watch and lists them product by product, each
with its own delete. One sweep can store several products as a single event,
so a delete that removed the event would take the others found alongside it —
tap x on one jacket and two more vanish unread.
"""
import pytest
from fastapi.testclient import TestClient

from monitor import db


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


def sweep_found(*handles, launched=False):
    wid = db.create_watch(name="RAGTAG · yohji", brand="ragtag-global.com",
                          url="https://ragtag-global.com/collections/y",
                          strategy="shopify", kind="collection", target_ref="y")
    payload = {
        "handles": list(handles),
        "titles": {h: h.upper() for h in handles},
        "links": {h: f"https://ragtag-global.com/products/{h}" for h in handles},
        "items": {h: {"title": h.upper(), "price": 1000, "image": f"https://i/{h}.jpg"}
                  for h in handles},
        "arrival": "launched" if launched else "new",
    }
    if launched:
        payload["first_sale"] = list(handles)
    return db.insert_event(wid, "new_product", "watching", "watching", payload)


def payload_of(event_id):
    return next(e["payload"] for e in db.list_events() if e["id"] == event_id)


def test_one_product_goes_and_its_siblings_stay(client):
    eid = sweep_found("coat", "shirt", "pants")

    r = client.delete(f"/api/events/{eid}/items/shirt")

    assert r.status_code == 200 and r.json()["event"] == "pruned"
    p = payload_of(eid)
    assert p["handles"] == ["coat", "pants"]
    # Every per-product map loses it too, or the page would rebuild it.
    for key in ("titles", "links", "items"):
        assert set(p[key]) == {"coat", "pants"}, key
    assert p["arrival"] == "new", "the rest of the event is untouched"


def test_the_last_product_takes_the_event_with_it(client):
    eid = sweep_found("coat", "shirt")

    client.delete(f"/api/events/{eid}/items/coat")
    r = client.delete(f"/api/events/{eid}/items/shirt")

    assert r.json()["event"] == "deleted"
    assert db.list_events() == [], "an alert about nothing must not linger"


def test_first_sale_is_pruned_so_the_wording_stays_true(client):
    eid = sweep_found("coat", "shirt", launched=True)

    client.delete(f"/api/events/{eid}/items/coat")

    assert payload_of(eid)["first_sale"] == ["shirt"]


def test_a_handle_the_alert_never_mentioned_is_a_404(client):
    eid = sweep_found("coat")

    assert client.delete(f"/api/events/{eid}/items/boots").status_code == 404
    assert client.delete("/api/events/99999/items/coat").status_code == 404
    assert payload_of(eid)["handles"] == ["coat"], "nothing changed"


def test_twice_is_a_404_not_a_second_deletion(client):
    eid = sweep_found("coat", "shirt")

    assert client.delete(f"/api/events/{eid}/items/coat").status_code == 200
    assert client.delete(f"/api/events/{eid}/items/coat").status_code == 404
    assert payload_of(eid)["handles"] == ["shirt"]


def test_real_satisfy_handles_survive_the_round_trip(client):
    """Satisfy's handles carry a trademark sign; the page URL-encodes it."""
    handle = "mothtech™-t-shirt-cl"
    eid = sweep_found(handle, "other")

    r = client.delete(f"/api/events/{eid}/items/mothtech%E2%84%A2-t-shirt-cl")

    assert r.status_code == 200
    assert payload_of(eid)["handles"] == ["other"]


def test_it_needs_auth(client):
    eid = sweep_found("coat")
    client.headers.pop("Authorization")

    # 401 with a password set, 503 when this box has no auth configured at
    # all; either way a stranger does not get to delete anything.
    assert client.delete(f"/api/events/{eid}/items/coat").status_code in (401, 503)
    assert payload_of(eid)["handles"] == ["coat"]
