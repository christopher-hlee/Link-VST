"""Best Buy strategy via the official developer API.

Unlike the other two, this one needs a (free) API key from
https://developer.bestbuy.com — set BESTBUY_API_KEY. Using the documented API
rather than scraping means no bot-detection fight, and it exposes store-level
availability, which is the useful signal for contested hardware.
"""
import re

from ..config import BESTBUY_API_KEY
from ..fetcher import fetch
from ..statemachine import CheckResult, IN_STOCK, OUT_OF_STOCK

NAME = "bestbuy"

BASE = "https://api.bestbuy.com/v1"
_SKU_RE = re.compile(r"(?:skuId=|/)(\d{7,8})(?:\.p|\b)")


def sku_from_url(url: str) -> str | None:
    m = _SKU_RE.search(url)
    return m.group(1) if m else None


async def check(watch: dict) -> CheckResult:
    if not BESTBUY_API_KEY:
        return CheckResult(ok=False, error="BESTBUY_API_KEY is not set")

    sku = watch.get("target_ref") or sku_from_url(watch["url"])
    if not sku:
        return CheckResult(ok=False, error="no SKU in URL")

    show = "sku,name,salePrice,onlineAvailability,inStoreAvailability,orderable,url"
    resp = await fetch(
        f"{BASE}/products(sku={sku})?apiKey={BESTBUY_API_KEY}"
        f"&format=json&show={show}"
    )
    if not resp.ok:
        return CheckResult(ok=False, http_status=resp.status, error=resp.error)
    if not isinstance(resp.json, dict):
        return CheckResult(ok=False, http_status=resp.status,
                           error="Best Buy returned non-JSON")

    products = resp.json.get("products") or []
    if not products:
        return CheckResult(ok=False, http_status=resp.status,
                           error=f"no product for SKU {sku}")

    product = products[0]
    online = bool(product.get("onlineAvailability"))
    orderable = str(product.get("orderable") or "").lower() in {"available", "yes"}

    stores = []
    if watch.get("store_ref"):
        stores = await _nearby_stores(sku, watch["store_ref"])

    return CheckResult(
        ok=True,
        state=IN_STOCK if (online or orderable or stores) else OUT_OF_STOCK,
        price=product.get("salePrice"),
        title=product.get("name") or watch.get("name"),
        product_url=product.get("url") or watch["url"],
        http_status=resp.status,
        extra={
            "online_availability": online,
            "orderable": product.get("orderable"),
            "in_store_availability": product.get("inStoreAvailability"),
            "stores": stores[:5],
            "store_count": len(stores),
        },
    )


async def _nearby_stores(sku: str, area: str, radius: int = 25) -> list[dict]:
    """Stores within `radius` miles of a ZIP that have the SKU on hand."""
    resp = await fetch(
        f"{BASE}/products(sku={sku})+stores(area({area},{radius}))"
        f"?apiKey={BESTBUY_API_KEY}&format=json"
        f"&show=stores.storeId,stores.name,stores.city,stores.distance"
    )
    if not resp.ok or not isinstance(resp.json, dict):
        return []
    out = []
    for product in resp.json.get("products") or []:
        for store in product.get("stores") or []:
            out.append({
                "store": store.get("name"),
                "city": store.get("city"),
                "distance": store.get("distance"),
            })
    return out


async def detect(url: str) -> dict | None:
    if "bestbuy.com" not in url.lower():
        return None
    sku = sku_from_url(url)
    if not sku:
        return None
    return {
        "strategy": NAME,
        "kind": "product",
        "name": f"Best Buy · {sku}",
        "brand": "Best Buy",
        "url": url,
        "target_ref": sku,
        "detected_via": "bestbuy.com URL pattern (SKU)",
        "note": "Requires BESTBUY_API_KEY. Set store_ref to a ZIP for local stock.",
    }
