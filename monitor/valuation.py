"""Landed cost, and a star on the listings worth spending a search on.

Scoped deliberately. The full method anchors every judgement to what the same
money buys delivered to the US today — new retail, used comps, and whatever
sale the brand happens to be running this week. None of those are in the
catalogue feed, and none of them have an API worth trusting, so nothing here
pretends to know them.

What it does instead is the cheap half, which is the half that does the work:
the tag-only gates that, in practice, eliminate most candidates before anyone
looks at anything. Seventy listings become a handful. The search pass is still
a person's job — the star says *this one survived*, not *this one is a buy*.

Two rules from the method that this file exists to obey:

- **Never compare a RAGTAG price to another RAGTAG price.** The catalogue is
  internally consistent and externally mispriced; roughly a third of listings
  sit above what the same piece costs new from a US retailer. A percentile
  against the store's own history would be a confident number measuring
  nothing.
- **Print every assumption.** The FX rate and the duty multiplier are both
  stale the moment they are written down, and an estimate whose inputs are
  invisible is worse than no estimate.
"""
from dataclasses import dataclass, field

# Duty multipliers by country-of-origin tag. De minimis was suspended in June
# 2026, so everything owes duty and the sticker price is never what is paid.
#
# A flat rate per origin is a simplification the method names explicitly: the
# real rate keys off material, and cashmere to polyester is a seventeen-point
# spread that flips marginal calls. Material is not in products.json — it is in
# the product page's spec table — so this is the low-confidence fallback, and
# it says so.
DUTY = {
    "countryoforigin_japan": 1.16,      # wool / cotton MFN, the working average
    "countryoforigin_china": 1.30,
    "countryoforigin_vietnam": 1.20,
}
DEFAULT_DUTY = 1.20

# Condition ranks, best first. B is "slight signs of use".
CONDITION_ORDER = ["NEW", "A", "B", "C", "D"]

# How much a rank-B risk costs, by what the garment is. Fabrics fail
# differently and garments wear in different places: a seat and hem go before
# anything on a shirt does, and a fine knit pills where a shell only scuffs.
CONDITION_FLOOR = {
    "pants": "A", "trousers": "A",
    "knitwear": "A", "knit": "A",
    "default": "B",
}


@dataclass
class Assessment:
    landed: float | None = None          # USD, duty included
    starred: bool = False
    confidence: str = "low"              # material unknown → low
    cleared: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return ", ".join(self.failed or self.cleared)


def parse_settings(text: str) -> tuple[dict, list[str]]:
    """`colors=black,navy max=450 fx=142 condition=A` into a config.

    A compact syntax rather than JSON because this gets typed on a phone, and
    because a config nobody can edit where they are is a config that stays
    wrong.
    """
    cfg: dict = {}
    unknown: list[str] = []
    for part in text.split():
        key, sep, value = part.partition("=")
        key, value = key.strip().casefold(), value.strip()
        if not sep or not value:
            unknown.append(part)
        elif key in ("colors", "colours", "palette"):
            cfg["palette"] = [c if c.casefold().startswith("color_")
                              else f"color_{c.strip().capitalize()}"
                              for c in value.split(",") if c.strip()]
        elif key in ("max", "ceiling"):
            try:
                cfg["max_landed"] = float(value.lstrip("$").replace(",", ""))
            except ValueError:
                unknown.append(part)
        elif key == "fx":
            try:
                cfg["fx_per_usd"] = float(value)
            except ValueError:
                unknown.append(part)
        elif key == "condition":
            rank = value.strip().upper()
            if rank not in CONDITION_ORDER:
                unknown.append(part)
            else:
                # Keep the per-category floors; only the fallback moves.
                cfg["condition_floor"] = {**CONDITION_FLOOR, "default": rank}
        else:
            unknown.append(part)
    return cfg, unknown


def describe_settings(cfg: dict | None) -> str:
    if not cfg:
        return "off"
    bits = []
    if cfg.get("palette"):
        bits.append("colours " + ", ".join(
            c.replace("color_", "") for c in cfg["palette"]))
    if cfg.get("max_landed") is not None:
        bits.append(f"under ${cfg['max_landed']:,.0f} landed")
    floors = cfg.get("condition_floor") or {}
    if floors.get("default"):
        bits.append(f"condition {floors['default']} or better")
    if cfg.get("fx_per_usd"):
        bits.append(f"at ¥{cfg['fx_per_usd']:,.0f}/$")
    return " · ".join(bits) or "on, with nothing to check"


def _tags(item: dict) -> set[str]:
    return {str(t).strip().casefold() for t in (item.get("tags") or [])}


def _tag_value(item: dict, prefix: str) -> str | None:
    for tag in (item.get("tags") or []):
        text = str(tag).strip()
        if text.casefold().startswith(prefix):
            return text[len(prefix):]
    return None


def duty_multiplier(item: dict, cfg: dict) -> float:
    table = {k.casefold(): v for k, v in (cfg.get("duty") or DUTY).items()}
    for tag in _tags(item):
        if tag in table:
            return float(table[tag])
    return float(cfg.get("default_duty", DEFAULT_DUTY))


def landed_cost(item: dict, cfg: dict, *, currency: str = "USD") -> float | None:
    """What the thing actually costs to have, in dollars.

    Shipping drops out — RAGTAG ships free worldwide at any order size — so it
    is sticker, converted, plus duty.
    """
    price = item.get("list_price") or item.get("price")
    if not price:
        return None
    rate = float(cfg.get("fx_per_usd") or 0)
    if (currency or "USD").upper() != "USD":
        if rate <= 0:
            return None                  # no rate is not a rate of one
        price = price / rate
    return round(price * duty_multiplier(item, cfg), 2)


def _condition_ok(item: dict, cfg: dict) -> bool | None:
    rank = (_tag_value(item, "condition_") or "").upper()
    if rank not in CONDITION_ORDER:
        return None                      # unranked: not a failure, not a pass
    category = (_tag_value(item, "product-type_") or "").casefold()
    floors = {k.casefold(): v for k, v in
              (cfg.get("condition_floor") or CONDITION_FLOOR).items()}
    floor = floors.get(category, floors.get("default", "B"))
    return CONDITION_ORDER.index(rank) <= CONDITION_ORDER.index(floor.upper())


def assess(item: dict, cfg: dict | None, *, currency: str = "USD") -> Assessment:
    """Landed cost, and whether this one clears the gates.

    A star is not a verdict. It means the cheap tag-only checks passed and the
    listing is worth pricing against US retail — the step this cannot do.
    """
    result = Assessment()
    if not cfg:
        return result

    result.landed = landed_cost(item, cfg, currency=currency)
    checks: list[tuple[str, bool | None]] = []

    palette = [c.casefold() for c in (cfg.get("palette") or [])]
    if palette:
        checks.append(("colour", bool(_tags(item) & set(palette))))

    ceiling = cfg.get("max_landed")
    if ceiling is not None:
        # No price is not "under the ceiling". Same rule as the price filter:
        # what cannot be shown to pass does not pass.
        checks.append(("price", result.landed is not None
                       and result.landed <= float(ceiling)))

    if cfg.get("condition_floor") is not None or cfg.get("check_condition"):
        checks.append(("condition", _condition_ok(item, cfg)))

    for name, ok in checks:
        if ok is False:
            result.failed.append(name)
        elif ok is True:
            result.cleared.append(name)

    # Unknown is not a pass. A star has to mean something was checked, so a
    # listing with no gate that actually applied does not get one.
    result.starred = bool(result.cleared) and not result.failed
    return result
