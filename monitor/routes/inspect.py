"""Ask the monitor what it sees for one product, and what it would do.

Silence is ambiguous. A product that never alerted might be invisible to us,
might have been absorbed as already-on-the-shelf, or might be sitting in the
ledger waiting to become buyable — and from the outside those look identical.

The rule this file exists to obey: **report what each source said, and never
fill in a source that said nothing.** An earlier version read availability from
an endpoint that does not carry the flag and, through a default meant for
building cart links, announced "buyable now" about a product the storefront was
showing as Coming Soon. A diagnostic that invents the fact it was asked to
check is worse than no diagnostic, because it is believed.
"""
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..fetcher import fetch
from ..statemachine import ARRIVAL_KNOWN, classify_arrival
from ..strategies import retail, shopify
from ..strategies.shopify import origin
from ..timeutil import parse, parse_instant, stamp, utcnow

router = APIRouter()


def _same_store(a: str, b: str) -> bool:
    """Whether two URLs name the same shop.

    `www.satisfyrunning.com` and `satisfyrunning.com` are one store serving one
    catalogue, but comparing origins verbatim calls them different — so a watch
    added with the www form reports as not covering a product URL without it,
    and the answer reads "nothing is polling this store" about a store being
    polled every 35 seconds.
    """
    def host(url: str) -> str:
        return urlparse(url).netloc.lower().removeprefix("www.")
    return host(a) == host(b) and bool(host(a))


def _handle(url: str) -> str | None:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "products" in parts:
        index = parts.index("products")
        if index + 1 < len(parts):
            return parts[index + 1].split("?")[0]
    return None


async def _from_ajax(base: str, handle: str) -> dict:
    """`/products/<handle>.js` — the one product endpoint that states
    availability per variant. Absent flags stay absent here."""
    resp = await fetch(f"{base}/products/{handle}.js")
    if not (resp.ok and isinstance(resp.json, dict)):
        return {"read": False, "status": resp.status}
    data = resp.json
    variants = [{"title": v.get("title"), "available": v.get("available")}
                for v in (data.get("variants") or [])]
    stated = [v for v in variants if v["available"] is not None]
    return {
        "read": True,
        "title": data.get("title"),
        "published_at": None,
        "variants": variants,
        # None, not False: "no variant said yes" and "no variant said anything"
        # are different answers and only one of them is evidence.
        "available": (any(v["available"] for v in stated) if stated else None),
    }


async def _from_page(url: str) -> dict:
    """What the product PAGE claims, via schema.org. This is the source that
    agrees with what a person sees, so it is how a "Coming soon" storefront
    sitting on a buyable-looking API gets caught."""
    resp = await fetch(url)
    if not (resp.ok and resp.text):
        return {"read": False, "status": resp.status}
    found = retail.find_product(resp.text)
    if not found:
        return {"read": True, "availability": None,
                "note": "no schema.org Product data on this page"}
    offers = retail._offers(found)
    states = {retail._availability(o.get("availability")) for o in offers}
    states.discard(None)
    if not states:
        # A Product described but carrying no availability at all is the
        # "available at a later date" shape: listed, priced, not orderable.
        return {"read": True, "title": found.get("name"), "availability": None,
                "buyable": False, "preorder": False,
                "note": "the page describes the product but states no availability"}
    return {
        "read": True,
        "title": found.get("name"),
        "availability": sorted(states),
        "buyable": bool(states & retail.BUYABLE),
        "preorder": bool(states & retail.PREORDER),
    }


@router.get("/inspect")
async def inspect(url: str = Query(..., min_length=8)):
    url = url.strip()
    if not url.startswith("https://"):
        raise HTTPException(400, "URL must start with https://")

    handle = _handle(url)
    if not handle:
        raise HTTPException(
            422, "That is not a product URL — it needs a /products/<handle> path.")

    base = origin(url)
    product_url = f"{base}/products/{handle}"

    ajax = await _from_ajax(base, handle)
    page = await _from_page(product_url)
    if not ajax["read"] and not page["read"]:
        raise HTTPException(
            502,
            f"The store returned nothing readable for {handle}. It may be "
            f"unpublished, or not Shopify.")

    now = utcnow()
    report = {
        "handle": handle,
        "title": ajax.get("title") or page.get("title") or handle,
        "sources": {"product_api": ajax, "product_page": page},
        "watches": [],
    }

    covering = [w for w in db.list_watches()
                if w.get("kind") == "collection" and _same_store(w["url"], url)]

    feed_available = None
    for w in covering:
        baseline = db.get_baseline(w) or []
        entry = (db.get_availability(w).get(handle)) or {}
        remembered = handle in baseline
        window = parse(w.get("last_sweep_at"))

        # Does this watch's feed contain the product at all, and what does it
        # say about it? Read it for real, with the cache validators stripped so
        # a 304 cannot answer for us. This is the authoritative source, because
        # it is the exact data the state machine decides on.
        item, visible = None, None
        try:
            seen = await shopify.check({**w, "etag": None, "last_modified": None})
            if seen.ok:
                visible = handle in (seen.handles or [])
                item = ((seen.extra or {}).get("items") or {}).get(handle)
        except Exception:
            visible = None

        stated = bool(item and item.get("available_stated"))
        buyable = item.get("available") if stated else None
        if buyable is not None and feed_available is None:
            feed_available = buyable

        arrival = (classify_arrival(item, window_start=window, now=now)
                   if item and window else None)

        if visible is False:
            verdict = ("NOT in this watch's feed — the product is not in this "
                       "collection, so this watch can never alert on it")
        elif visible is None:
            verdict = ("could not read this watch's feed just now, so I cannot "
                       "say whether it covers this product")
        elif not remembered:
            verdict = ("in the feed but not yet swept — the next check decides "
                       "whether the store published it recently enough to alert")
        elif buyable is None:
            verdict = ("in the feed, but it states no availability for this "
                       "product, so I cannot tell you whether it is buyable")
        elif buyable and entry.get("available") is False:
            verdict = "buyable in the feed and last seen unbuyable — the next sweep alerts"
        elif not buyable:
            verdict = ("tracked, and the feed says not buyable — you will be "
                       "alerted the moment a variant goes on sale")
        elif arrival == ARRIVAL_KNOWN:
            verdict = "already on the shelf when we adopted it; nothing pending"
        else:
            verdict = "tracked and buyable; already alerted or adopted"

        if item:
            report.setdefault("published_at", stamp(parse_instant(item.get("published_at")))
                              if item.get("published_at") else None)
            report.setdefault("created_at", stamp(parse_instant(item.get("created_at")))
                              if item.get("created_at") else None)
        report["sources"].setdefault("collection_feed", {
            "read": visible is not None, "in_feed": visible,
            "available": buyable, "stated": stated})

        report["watches"].append({
            "id": w["id"], "name": w["name"],
            "in_feed": visible, "in_catalogue": remembered,
            "known_buyable": entry.get("available"),
            "last_sweep_at": w.get("last_sweep_at"),
            "interval_s": w.get("base_interval_s") or 300,
            "verdict": verdict,
        })

    # What the monitor itself would conclude — the feed, because that is what it
    # decides on. Never the product endpoint, which does not carry the flag.
    report["buyable_now"] = feed_available if covering else ajax.get("available")

    # The storefront and the API disagreeing is the whole explanation for a
    # "Coming soon" page that looks purchasable to us, so it gets said plainly
    # rather than left for someone to notice in the raw fields.
    page_buyable = page.get("buyable")
    if page_buyable is False and report["buyable_now"] is True:
        report["disagreement"] = (
            "The product page says it is NOT purchasable"
            + (f" ({', '.join(page['availability'])})" if page.get("availability") else "")
            + ", but the catalogue API says it is. The storefront is what a "
              "person sees, so treat this as not yet dropped — and tell me, "
              "because it means this store signals 'coming soon' somewhere "
              "other than the variant flag we watch.")
    elif page_buyable is True and report["buyable_now"] is False:
        report["disagreement"] = (
            "The product page says it IS purchasable but the catalogue API says "
            "it is not. The feed is what we poll, so an alert may be late here.")

    if not covering:
        report["note"] = (f"No collection watch covers {base}. Nothing is "
                          f"polling this store, so nothing can alert.")
    elif all(w["in_feed"] is False for w in report["watches"]):
        # The whole-store feed is a superset of every collection, so the fix is
        # one store-wide watch, not one per collection — which would multiply
        # traffic to a store that already rate-limits us, and still miss
        # whichever collection you did not think of.
        report["note"] = (
            f"No watch's feed contains this product. One watch on {base} "
            f"(no /collections/ path) reads the whole catalogue and covers "
            f"every collection at once.")
    return report
