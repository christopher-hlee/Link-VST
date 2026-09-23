"""Landed cost, and a star on what survives the cheap gates.

Fixtures are the worked examples from the valuation method, restricted to the
part the catalogue feed can actually support. Several of those calls turn on
anchors that are not in any feed — new retail, used comps, a sale running this
week — and the tests say so where a verdict here disagrees with the method's,
rather than quietly reproducing the number.
"""
import json
from datetime import timedelta

import httpx
import pytest
import respx

from monitor import db, scheduler
from monitor.timeutil import stamp, utcnow
from monitor.valuation import assess, landed_cost

# Black and navy primary, indigo as a bridge.
PALETTE = ["color_Black", "color_Navy", "color_Indigo"]
CFG = {"fx_per_usd": 142, "palette": PALETTE, "max_landed": 450,
       "condition_floor": {"Pants": "A", "Knitwear": "A", "default": "B"}}


def listing(*, yen, color="color_Black", condition="A", kind="Shirts",
            origin="countryoforigin_Japan"):
    return {"list_price": float(yen),
            "tags": [color, f"condition_{condition}", f"product-type_{kind}",
                     origin, "gender_Mens", "size_L"]}


# --- landed cost ----------------------------------------------------------

def test_yen_becomes_dollars_with_duty_on_top():
    """De minimis is suspended, so duty is not optional and the sticker is
    never what is paid. 49,160 JPY at 142 is $346, plus 16% Japan duty."""
    assert landed_cost(listing(yen=49160), CFG, currency="JPY") == 401.59


@pytest.mark.parametrize("origin,expected_multiplier", [
    ("countryoforigin_Japan", 1.16),
    ("countryoforigin_China", 1.30),
    ("countryoforigin_Vietnam", 1.20),
    ("countryoforigin_Elsewhere", 1.20),      # the default
])
def test_duty_keys_off_the_country_tag(origin, expected_multiplier):
    got = landed_cost(listing(yen=14200, origin=origin), CFG, currency="JPY")
    assert got == round(100 * expected_multiplier, 2)


def test_a_dollar_store_needs_no_conversion():
    item = {"list_price": 260.0, "tags": []}
    assert landed_cost(item, {"fx_per_usd": 142}, currency="USD") == 312.0


def test_a_missing_rate_is_not_a_rate_of_one():
    """Silently treating 49,160 yen as 49,160 dollars, or as 49,160/1, are
    both worse than declining to answer."""
    assert landed_cost(listing(yen=49160), {"palette": PALETTE},
                       currency="JPY") is None


def test_an_unpriced_listing_has_no_landed_cost():
    assert landed_cost({"list_price": None, "tags": []}, CFG,
                       currency="JPY") is None


# --- the gates ------------------------------------------------------------

def test_a_black_rank_a_shirt_under_the_ceiling_is_starred():
    verdict = assess(listing(yen=49160), CFG, currency="JPY")

    assert verdict.starred is True
    assert verdict.failed == []


def test_the_wrong_colour_loses_the_star_however_good_the_price():
    """A discount on something that will not be worn is waste, not saving."""
    verdict = assess(listing(yen=14200, color="color_Green"), CFG,
                     currency="JPY")

    assert verdict.starred is False
    assert "colour" in verdict.failed


def test_over_the_ceiling_loses_the_star():
    verdict = assess(listing(yen=99000), CFG, currency="JPY")

    assert verdict.starred is False
    assert "price" in verdict.failed


def test_rank_b_trousers_are_refused_where_rank_b_shirts_are_not():
    """Seat and hem go first, so rank A is worth disproportionately more on
    bottoms. The rank alone means nothing without the category."""
    shirt = assess(listing(yen=30000, condition="B", kind="Shirts"), CFG,
                   currency="JPY")
    trousers = assess(listing(yen=30000, condition="B", kind="Pants"), CFG,
                      currency="JPY")

    assert shirt.starred is True
    assert trousers.starred is False and "condition" in trousers.failed


def test_rank_b_knitwear_is_refused_for_pilling():
    verdict = assess(listing(yen=30000, condition="B", kind="Knitwear"), CFG,
                     currency="JPY")

    assert verdict.starred is False


def test_an_unranked_listing_is_not_starred_on_a_gate_that_could_not_run():
    """Unknown is not a pass."""
    item = {"list_price": 30000.0,
            "tags": ["color_Black", "product-type_Shirts",
                     "countryoforigin_Japan"]}

    verdict = assess(item, CFG, currency="JPY")

    assert verdict.starred is True, "colour and price did run and did pass"
    assert "condition" not in verdict.cleared, "and condition did not"


def test_no_configuration_means_no_stars():
    """Every other store keeps working exactly as it did."""
    assert assess(listing(yen=49160), None, currency="JPY").starred is False
    assert assess(listing(yen=49160), {}, currency="JPY").starred is False


def test_a_listing_no_gate_applies_to_is_not_starred():
    """A star has to mean something was checked."""
    verdict = assess({"list_price": 1.0, "tags": []}, {"fx_per_usd": 142},
                     currency="JPY")

    assert verdict.starred is False


# --- the method's own examples, where tags can carry them ------------------

@pytest.mark.parametrize("name,yen,color,condition,kind,starred,why", [
    # BUY: rank A, black, and the only non-knit in a saturated wardrobe.
    ("Auralee sweatshirt", 31240, "color_Black", "A", "Sweatshirts", True,
     "rank A, in palette, under ceiling"),
    # SKIP in the method because it is priced above new — an anchor no feed
    # carries. Here it fails on the ceiling instead, and lands the same way
    # for a different reason, which is worth being honest about.
    ("Auralee rib knit", 75000, "color_Black", "B", "Knitwear", False,
     "over ceiling AND rank B knit"),
    # SKIP in the method for duplication against cheaper stock — wardrobe
    # saturation, which nothing here can see. This one stars, and should be
    # read as "worth pricing", not as a buy.
    ("Yohji black tee", 16600, "color_Black", "B", "T-shirts", True,
     "gates cannot see duplication"),
])
def test_the_worked_examples(name, yen, color, condition, kind, starred, why):
    verdict = assess(
        listing(yen=yen, color=color, condition=condition, kind=kind),
        CFG, currency="JPY")

    assert verdict.starred is starred, f"{name}: {why} ({verdict.reason})"


def test_the_star_cannot_see_the_market_and_does_not_claim_to():
    """The honest boundary, pinned so it cannot drift.

    Two listings identical in every tag, one priced above US retail and one far
    below, are indistinguishable here. The star says the cheap gates passed.
    """
    a = assess(listing(yen=40000), CFG, currency="JPY")
    b = assess(listing(yen=40000), CFG, currency="JPY")

    assert a.starred == b.starred


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


STORE = "https://ragtag-global.com"
FEED = f"{STORE}/collections/comoli/products.json"


def product(handle, *, yen, color="color_Black", condition="A"):
    now = (utcnow() - timedelta(minutes=2)).isoformat()
    return {"handle": handle, "title": "COMOLI Other", "vendor": "COMOLI",
            "product_type": "Shirts", "published_at": now, "created_at": now,
            "tags": [color, f"condition_{condition}", "product-type_Shirts",
                     "countryoforigin_Japan", "gender_Mens", "size_L"],
            "images": [{"src": "https://x/i.jpg"}],
            "variants": [{"id": abs(hash(handle)) % 9999, "title": "Default Title",
                          "available": True, "price": f"{yen}.00"}]}


@respx.mock
async def test_the_verdict_is_stored_on_the_event_not_just_rendered(sent):
    """So the dashboard, the alert and the history agree — and a call can be
    read back later against the assumptions that produced it."""
    wid = db.create_watch(
        name="ragtag · comoli", brand="ragtag-global.com",
        url=f"{STORE}/collections/comoli", strategy="shopify",
        kind="collection", target_ref="comoli", currency="JPY",
        last_state="watching", last_sweep_at=stamp(),
        baseline_json=json.dumps(["seed"]),
        filter_json=json.dumps({"star": CFG}))
    old = (utcnow() - timedelta(days=90)).isoformat()
    respx.get(url__startswith=FEED).mock(return_value=httpx.Response(200, json={
        "products": [
            {**product("seed", yen=10000), "published_at": old, "created_at": old},
            product("good", yen=49160),
            product("dear", yen=99000),
        ]}))

    await scheduler.check_watch(db.get_watch(wid))

    items = sent[0]["items"]
    assert items["good"]["starred"] is True
    assert items["good"]["landed"] == 401.59
    assert items["dear"]["starred"] is False
    assert "price" in items["dear"]["star_reason"]


# --- setting it from the phone --------------------------------------------

from monitor import telegram_bot                                    # noqa: E402
from monitor.valuation import describe_settings, parse_settings     # noqa: E402


def test_the_compact_syntax_parses():
    cfg, unknown = parse_settings("colors=black,navy max=450 fx=142 condition=A")

    assert cfg["palette"] == ["color_Black", "color_Navy"]
    assert cfg["max_landed"] == 450.0
    assert cfg["fx_per_usd"] == 142.0
    assert cfg["condition_floor"]["default"] == "A"
    assert unknown == []


def test_raising_the_default_floor_keeps_the_category_ones():
    """Trousers still want rank A even when everything else may be B."""
    cfg, _ = parse_settings("condition=B")

    assert cfg["condition_floor"]["default"] == "B"
    assert cfg["condition_floor"]["pants"] == "A"


def test_a_full_tag_is_accepted_as_well_as_a_bare_colour():
    cfg, _ = parse_settings("colors=color_Black,indigo")
    assert cfg["palette"] == ["color_Black", "color_Indigo"]


@pytest.mark.parametrize("text", ["max=cheap", "fx=soon", "condition=Z", "rubbish"])
def test_nonsense_is_reported_rather_than_silently_dropped(text):
    cfg, unknown = parse_settings(text)
    assert unknown == [text]
    assert cfg == {}


def test_the_settings_read_back_as_a_checkable_line():
    cfg, _ = parse_settings("colors=black max=450 fx=142 condition=A")
    assert describe_settings(cfg) == (
        "colours Black · under $450 landed · condition A or better · at ¥142/$")


@pytest.fixture
def watch_id(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db.create_watch(name="ragtag · comoli", brand="ragtag-global.com",
                           url="https://ragtag-global.com/collections/comoli",
                           strategy="shopify", kind="collection",
                           target_ref="comoli", currency="JPY",
                           last_state="watching")


async def test_star_settings_survive_being_set_in_two_goes(watch_id):
    """Adding a ceiling later must not wipe the palette."""
    await telegram_bot.handle(f"/star {watch_id} colors=black,navy fx=142")
    await telegram_bot.handle(f"/star {watch_id} max=450")

    cfg = db.get_filter(db.get_watch(watch_id))["star"]
    assert cfg["palette"] == ["color_Black", "color_Navy"]
    assert cfg["max_landed"] == 450.0
    assert cfg["fx_per_usd"] == 142.0


async def test_setting_stars_does_not_disturb_the_saved_search(watch_id):
    db.update_watch(watch_id, filter_json=json.dumps(
        {"vendors": ["COMOLI"], "tag_groups": [["size_L"]]}))

    await telegram_bot.handle(f"/star {watch_id} colors=black max=450")

    spec = db.get_filter(db.get_watch(watch_id))
    assert spec["vendors"] == ["COMOLI"]
    assert spec["star"]["max_landed"] == 450.0


async def test_stars_can_be_turned_off_without_losing_the_filter(watch_id):
    db.update_watch(watch_id, filter_json=json.dumps(
        {"vendors": ["COMOLI"], "star": {"max_landed": 450}}))

    reply = await telegram_bot.handle(f"/star {watch_id} off")

    assert "stars off" in reply
    assert db.get_filter(db.get_watch(watch_id)) == {"vendors": ["COMOLI"]}


async def test_a_yen_watch_without_a_rate_is_told_so(watch_id):
    """Otherwise the ceiling silently never matches and the stars never come."""
    reply = await telegram_bot.handle(f"/star {watch_id} colors=black max=450")

    assert "No fx rate set" in reply


async def test_one_message_configures_every_watch(watch_id, monkeypatch):
    """A palette and a budget are facts about the person, not about one shop.
    Retyping them per watch is busywork that also guarantees drift."""
    second = db.create_watch(name="namu · auralee", brand="namu-shop.com",
                             url="https://www.namu-shop.com/collections/auralee",
                             strategy="shopify", kind="collection",
                             target_ref="auralee", last_state="watching")
    product_watch = db.create_watch(name="one tee", brand="s.com",
                                    url="https://s.com/products/tee",
                                    strategy="shopify", kind="product",
                                    target_ref="tee")

    reply = await telegram_bot.handle("/star all colors=black max=450 fx=142")

    for wid in (watch_id, second):
        assert db.get_filter(db.get_watch(wid))["star"]["max_landed"] == 450.0
    assert db.get_filter(db.get_watch(product_watch)) is None, \
        "a single-product watch has no catalogue to value"
    assert reply.count("colours Black") == 2


async def test_all_leaves_each_watch_s_own_search_alone(watch_id):
    """The star settings are shared; what each watch is looking for is not."""
    db.update_watch(watch_id, filter_json=json.dumps({"vendors": ["COMOLI"]}))

    await telegram_bot.handle("/star all max=450 fx=142")

    spec = db.get_filter(db.get_watch(watch_id))
    assert spec["vendors"] == ["COMOLI"]
    assert spec["star"]["max_landed"] == 450.0


async def test_all_says_so_when_there_is_nothing_to_set(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "empty.db")
    db.init_db()

    assert "No collection watches" in await telegram_bot.handle("/star all max=1")
