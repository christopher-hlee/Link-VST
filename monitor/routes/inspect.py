"""Ask the monitor what it sees for one product, and what it would do.

Silence is ambiguous. A product that never alerted might be invisible to us,
might have been absorbed as old, or might be sitting in the ledger waiting to
become buyable — and from the outside those look identical. This turns the
question into an answer you can read off the dashboard, without a shell on the
box.
"""
from fastapi import APIRouter, HTTPException, Query
from urllib.parse import urlparse

from .. import db
from ..statemachine import (
    ARRIVAL_KNOWN, classify_arrival,
)
from ..strategies import shopify
from ..strategies.shopify import _collection_item, origin
from ..fetcher import fetch
from ..timeutil import parse, utcnow

router = APIRouter()


def _handle(url: str) -> str | None:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if "products" in parts:
        index = parts.index("products")
        if index + 1 < len(parts):
            return parts[index + 1].split("?")[0]
    return None


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
    resp = await fetch(f"{base}/products/{handle}.json")
    if not (resp.ok and isinstance(resp.json, dict)):
        raise HTTPException(
            502,
            f"The store did not return product data for {handle} "
            f"(HTTP {resp.status or '—'}). It may be unpublished, or not Shopify.")

    product = resp.json.get("product") or {}
    item = _collection_item(base, product)
    now = utcnow()

    # Which of our watches, if any, would ever see this product.
    covering = [w for w in db.list_watches()
                if (w.get("kind") == "collection")
                and origin(w["url"]).rstrip("/") == base.rstrip("/")]

    report = {
        "handle": handle,
        "title": item["title"],
        "published_at": item["published_at"],
        "created_at": item["created_at"],
        "buyable_now": item["available"],
        "offers": len(item["offers"]),
        "watches": [],
    }

    for w in covering:
        baseline = db.get_baseline(w) or []
        ledger = db.get_availability(w)
        entry = ledger.get(handle) or {}
        remembered = handle in baseline
        window = parse(w.get("last_sweep_at"))
        arrival = classify_arrival(
            item, window_start=window or now, now=now) if window else None

        # Does this watch's feed contain the product AT ALL? Reading the
        # baseline alone cannot tell "we have not swept since it appeared"
        # apart from "this collection will never contain it" — and only the
        # second means you need to watch something else. So do the real read,
        # with the cache validators stripped so a 304 cannot answer for us.
        probe = {**w, "etag": None, "last_modified": None}
        try:
            seen = await shopify.check(probe)
            visible = handle in (seen.handles or []) if seen.ok else None
        except Exception:
            visible = None

        if visible is False:
            verdict = ("NOT in this watch's feed — the product is not in this "
                       "collection, so this watch can never alert on it")
        elif visible is None:
            verdict = ("could not read this watch's feed just now, so I cannot "
                       "say whether it covers this product")
        elif not remembered:
            verdict = ("in the feed but not yet swept — the next check decides "
                       "whether the store published it recently enough to alert")
        elif item["available"] and entry.get("available") is False:
            verdict = "buyable now and last seen unbuyable — the next sweep alerts"
        elif not item["available"]:
            verdict = ("tracked, not buyable — you will be alerted the moment "
                       "a variant goes on sale")
        elif arrival == ARRIVAL_KNOWN:
            verdict = "already on the shelf when we adopted it; nothing pending"
        else:
            verdict = "tracked and buyable; already alerted or adopted"

        report["watches"].append({
            "id": w["id"], "name": w["name"],
            "in_feed": visible,
            "in_catalogue": remembered,
            "known_buyable": entry.get("available"),
            "last_sweep_at": w.get("last_sweep_at"),
            "interval_s": w.get("base_interval_s") or 300,
            "verdict": verdict,
        })

    # The whole-store feed is a superset of every collection, so if nothing
    # covers this product the fix is one store-wide watch, not one per
    # collection — which would multiply traffic to a store that already rate
    # limits us, and still miss whichever collection you did not think of.
    if covering and all(w["in_feed"] is False for w in report["watches"]):
        report["note"] = (
            f"No watch's feed contains this product. One watch on {base} "
            f"(no /collections/ path) reads the whole catalogue and covers "
            f"every collection at once.")

    if not covering:
        report["watches"] = []
        report["note"] = (f"No collection watch covers {base}. Nothing is "
                          f"polling this store, so nothing can alert.")
    return report
