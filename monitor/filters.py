"""Saved-search predicates, evaluated against a catalogue entry.

Written for consignment stores, where the metadata a person filters on lives in
the product's `tags` rather than in its variants. A RAGTAG listing is a single
unique garment whose only variant is `Default Title`; its size, gender, country
of origin and condition are all tags. Any code reading size from a variant
matches nothing there, and matches nothing *quietly*, which is the dangerous
kind of nothing.

Pure by design, like the state machine it feeds: a filter is data, and deciding
whether an item satisfies it must never depend on the network.
"""
from urllib.parse import parse_qsl, urlparse

# Boost AI Search & Discovery, which RAGTAG runs, encodes a saved filter in the
# storefront URL. These prefixes are its conventions.
TAG_PREFIX = "pf_t_"        # a tag facet, e.g. pf_t_size=size_M
VENDOR_PREFIX = "pf_v_"     # a vendor facet, e.g. pf_v_brand=COMOLI
STOCK_PREFIX = "pf_st_"     # stock status, e.g. pf_st_availability=in-stock


def _norm(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def parse_boost_url(url: str) -> dict:
    """Turn a saved storefront filter into predicates we can evaluate.

    These parameters belong to Boost and are applied by Boost's own service.
    They have NO effect on `products.json` — handing the URL through to Shopify
    returns the whole unfiltered collection, which looks like it is working
    while being wrong. So the filter has to be re-implemented on our side, and
    this is where the URL stops being a URL.

    Repeated parameters within one facet are an OR; separate facets are an AND.
    `pf_t_size=size_L&pf_t_size=size_XL` means "L or XL", and combined with
    `pf_t_gender=gender_Mens` it means "(L or XL) and Mens". Flattening those
    into one list of required tags would demand a garment be both L and XL,
    which nothing is.
    """
    parsed = urlparse(url)
    groups: dict[str, list[str]] = {}
    spec: dict = {}

    for key, raw in parse_qsl(parsed.query, keep_blank_values=False):
        value = raw.strip()
        if not value:
            continue
        if key.startswith(TAG_PREFIX):
            groups.setdefault(key[len(TAG_PREFIX):], []).append(value)
        elif key.startswith(VENDOR_PREFIX):
            spec.setdefault("vendors", []).append(value)
        elif key.startswith(STOCK_PREFIX) and value.replace("_", "-") == "in-stock":
            spec["in_stock"] = True

    if groups:
        # Sorted so the same filter always serialises identically, which keeps
        # a stored spec diffable and a test deterministic.
        spec["tag_groups"] = [sorted(v) for _, v in sorted(groups.items())]
    return spec


# The keys that say WHAT TO WATCH FOR. Everything else in a spec — the star
# settings, say — belongs to a different question and must survive a change to
# this one.
SEARCH_KEYS = ("tag_groups", "vendors", "in_stock", "max_price")


def parse_settings(text: str) -> tuple[dict, list[str]]:
    """`vendor=AURALEE tags=size_L,size_XL in_stock=yes` into predicates.

    Most shops do not run Boost, so most saved searches cannot be expressed as
    a URL. A vendor is the one filter nearly every multi-brand store needs —
    "tell me when AURALEE is marked down here" — and without this there was no
    way to say it.

    Each `tags=` is one OR group; repeat the key for an AND of ORs, the same
    shape a storefront's facets produce.
    """
    spec: dict = {}
    unknown: list[str] = []
    groups: list[list[str]] = []

    for part in text.split():
        key, sep, value = part.partition("=")
        key, value = key.strip().casefold(), value.strip()
        if not sep or not value:
            unknown.append(part)
        elif key in ("vendor", "vendors", "brand", "brands"):
            spec["vendors"] = [v.strip() for v in value.split(",") if v.strip()]
        elif key in ("tag", "tags"):
            group = [t.strip() for t in value.split(",") if t.strip()]
            if group:
                groups.append(sorted(group))
        elif key in ("in_stock", "instock", "available"):
            spec["in_stock"] = value.lower() in ("1", "yes", "true", "y")
        elif key in ("max", "max_price"):
            try:
                spec["max_price"] = float(value.lstrip("$").replace(",", ""))
            except ValueError:
                unknown.append(part)
        else:
            unknown.append(part)

    if groups:
        spec["tag_groups"] = groups
    return spec, unknown


def matches(item: dict, spec: dict | None) -> bool:
    """Whether one catalogue entry satisfies the filter. No spec means yes."""
    if not spec:
        return True

    tags = {_norm(t) for t in (item.get("tags") or [])}
    for group in spec.get("tag_groups") or []:
        if not any(_norm(t) in tags for t in group):
            return False

    vendors = spec.get("vendors")
    if vendors and _norm(item.get("vendor")) not in {_norm(v) for v in vendors}:
        return False

    if spec.get("in_stock") and not item.get("available"):
        return False

    ceiling = spec.get("max_price")
    if ceiling is not None:
        price = item.get("list_price")
        # A listing with no price cannot be shown to be under the ceiling, and
        # guessing in either direction is worse than saying so: excluded, and
        # visible as excluded in the filter's description.
        if price is None or price > ceiling:
            return False

    return True


def describe(spec: dict | None) -> str:
    """One line a person can check against what they meant."""
    if not spec:
        return "everything in the collection"
    parts = []
    if spec.get("vendors"):
        parts.append(" or ".join(spec["vendors"]))
    for group in spec.get("tag_groups") or []:
        parts.append(" or ".join(group))
    if spec.get("in_stock"):
        parts.append("in stock")
    if spec.get("max_price") is not None:
        parts.append(f"under {spec['max_price']:,.0f}")
    return " · ".join(parts) or "everything in the collection"
