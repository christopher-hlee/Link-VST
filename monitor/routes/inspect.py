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
        in_feed = handle in baseline
        interval = w.get("base_interval_s") or 300
        window = parse(w.get("last_sweep_at"))
        arrival = classify_arrival(
            item, window_start=window or now, now=now) if window else None

        if not in_feed:
            # Either it is not in the collection we poll, or we have not swept
            # since it appeared. The next sweep settles which.
            verdict = ("not in this watch's catalogue yet — the next sweep "
                       "will alert if the store published it recently")
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
            "in_catalogue": in_feed,
            "known_buyable": entry.get("available"),
            "last_sweep_at": w.get("last_sweep_at"),
            "interval_s": interval,
            "verdict": verdict,
        })

    if not covering:
        report["watches"] = []
        report["note"] = (f"No collection watch covers {base}. Nothing is "
                          f"polling this store, so nothing can alert.")
    return report
