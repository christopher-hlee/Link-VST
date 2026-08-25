"""Target strategy via the RedSky aggregations API.

RedSky is the JSON API target.com's own frontend calls. It exposes per-store
pickup quantities, which is the genuinely useful part: local pickup stock is far
less contested than online stock, because resellers optimize for shipping.

Caveat worth knowing: RedSky is an internal API. The public web key rotates, the
response shape changes without notice, and requests from datacenter IP ranges
are frequently refused. Every parse below is defensive, and a refusal surfaces
as an `error` state rather than a bogus "out of stock".
"""
import re
from typing import Any

from ..config import TARGET_API_KEY
from ..fetcher import fetch
from ..statemachine import CheckResult, IN_STOCK, OUT_OF_STOCK

NAME = "target"

BASE = "https://redsky.target.com/redsky_aggregations/v1/web"
_TCIN_RE = re.compile(r"A-(\d{6,})")

IN_STOCK_STATUSES = {"IN_STOCK", "PRE_ORDER_SELLABLE", "AVAILABLE"}


def tcin_from_url(url: str) -> str | None:
    m = _TCIN_RE.search(url)
    return m.group(1) if m else None


def _dig(obj: Any, *keys: str, default: Any = None) -> Any:
    """Walk nested dicts without raising on a shape change."""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


async def check(watch: dict) -> CheckResult:
    tcin = watch.get("target_ref") or tcin_from_url(watch["url"])
    if not tcin:
        return CheckResult(ok=False, error="no TCIN in URL (expected .../A-12345678)")

    store = watch.get("store_ref") or ""
    params = [
        f"key={TARGET_API_KEY}",
        f"tcin={tcin}",
        "channel=WEB",
        f"page=%2Fp%2FA-{tcin}",
    ]
    if store.isdigit():
        params += [f"store_id={store}", f"pricing_store_id={store}",
                   "has_required_store_id=true", f"required_store_id={store}"]
    elif store:
        params.append(f"zip={store}")

    resp = await fetch(f"{BASE}/pdp_fulfillment_v1?{'&'.join(params)}")
    if not resp.ok:
        hint = ""
        if resp.status == 403:
            hint = " (403 — RedSky commonly refuses datacenter IPs; see phase 2)"
        return CheckResult(ok=False, http_status=resp.status,
                           error=f"{resp.error}{hint}")
    if not isinstance(resp.json, dict):
        return CheckResult(ok=False, http_status=resp.status,
                           error="RedSky returned non-JSON")

    product = _dig(resp.json, "data", "product", default={})
    fulfillment = product.get("fulfillment") or {}

    ship_status = _dig(fulfillment, "shipping_options", "availability_status",
                       default="UNKNOWN")
    shippable = ship_status in IN_STOCK_STATUSES

    pickup_stores = []
    for opt in (fulfillment.get("store_options") or []):
        qty = opt.get("location_available_to_promise_quantity") or 0
        status = _dig(opt, "order_pickup", "availability_status", default="")
        if status in IN_STOCK_STATUSES or qty > 0:
            pickup_stores.append({
                "store": _dig(opt, "location_name", default=opt.get("store_id")),
                "quantity": qty,
                "status": status,
            })

    price = _dig(product, "price", "current_retail")
    title = _dig(product, "item", "product_description", "title")

    return CheckResult(
        ok=True,
        state=IN_STOCK if (shippable or pickup_stores) else OUT_OF_STOCK,
        price=float(price) if isinstance(price, (int, float)) else None,
        title=title or watch.get("name"),
        product_url=f"https://www.target.com/p/A-{tcin}",
        http_status=resp.status,
        extra={
            "shipping_status": ship_status,
            "shippable": shippable,
            "pickup_stores": pickup_stores[:5],
            "pickup_store_count": len(pickup_stores),
        },
    )


async def detect(url: str) -> dict | None:
    if "target.com" not in url.lower():
        return None
    tcin = tcin_from_url(url)
    if not tcin:
        return None
    return {
        "strategy": NAME,
        "kind": "product",
        "name": f"Target · {tcin}",
        "brand": "Target",
        "url": url,
        "target_ref": tcin,
        "detected_via": "target.com URL pattern (A-<tcin>)",
        "note": "Set store_ref to a ZIP or store id to track local pickup stock.",
    }
