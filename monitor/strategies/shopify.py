"""Shopify storefront strategy.

Most Shopify stores expose structured JSON with no auth, which makes this both
cheap and stable — far better than scraping HTML, which breaks silently on every
redesign. Two endpoints do all the work:

    /products/{handle}.js        one product, with per-variant availability
    /products.json?limit=250     the catalog, for detecting brand-new handles

Falls back to the Atom feed when products.json is gated, which some stores do.
"""
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urlunparse

from ..fetcher import fetch
from ..statemachine import CheckResult, IN_STOCK, OUT_OF_STOCK

NAME = "shopify"

_PRODUCT_RE = re.compile(r"/products/([^/?#]+)")
_COLLECTION_RE = re.compile(r"/collections/([^/?#]+)")
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def origin(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme or "https", p.netloc, "", "", "", ""))


def product_handle(url: str) -> str | None:
    m = _PRODUCT_RE.search(urlparse(url).path)
    return m.group(1) if m else None


def collection_handle(url: str) -> str | None:
    """Collection name, with any feed suffix stripped.

    `/collections/all.atom` names the collection `all` served as a feed — not a
    collection called `all.atom`. Keeping the suffix sends every later request
    to `/collections/all.atom/products.json`, which 404s forever.
    """
    m = _COLLECTION_RE.search(urlparse(url).path)
    if not m:
        return None
    return re.sub(r"\.(atom|rss|json|xml)$", "", m.group(1), flags=re.I)


def cart_url(base: str, variant_id) -> str:
    """Shopify cart permalink — drops the item straight into the cart."""
    return f"{base}/cart/{variant_id}:1"


def _price(raw, *, in_cents: bool) -> float | None:
    """products.json gives a decimal string; the .js endpoint gives integer cents."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value / 100.0 if in_cents else value


# ------------------------------------------------------------------ checking

async def check(watch: dict) -> CheckResult:
    if watch.get("kind") == "collection":
        return await _check_collection(watch)
    return await _check_product(watch)


async def _check_product(watch: dict) -> CheckResult:
    base = origin(watch["url"])
    handle = watch.get("target_ref") or product_handle(watch["url"])
    if not handle:
        return CheckResult(ok=False, error="no product handle in URL")

    resp = await fetch(f"{base}/products/{handle}.js",
                       etag=watch.get("etag"),
                       last_modified=watch.get("last_modified"))
    if resp.not_modified:
        return CheckResult(ok=True, not_modified=True, etag=resp.etag,
                           last_modified=resp.last_modified,
                           http_status=304)
    if not resp.ok:
        return CheckResult(ok=False, http_status=resp.status, error=resp.error)

    data = resp.json
    if not isinstance(data, dict):
        return CheckResult(ok=False, http_status=resp.status,
                           error="response was not a JSON object")

    variants = data.get("variants") or []
    available = [v for v in variants if v.get("available")]
    chosen = available[0] if available else (variants[0] if variants else None)

    image = data.get("featured_image")
    if isinstance(image, str) and image.startswith("//"):
        image = "https:" + image

    return CheckResult(
        ok=True,
        state=IN_STOCK if available else OUT_OF_STOCK,
        price=_price((chosen or {}).get("price"), in_cents=True),
        title=data.get("title"),
        product_url=f"{base}/products/{handle}",
        cart_url=cart_url(base, chosen["id"]) if available and chosen else None,
        image=image,
        http_status=resp.status,
        etag=resp.etag,
        last_modified=resp.last_modified,
        extra={
            "variants_total": len(variants),
            "variants_available": len(available),
            "variant_titles": [v.get("title") for v in available[:8]],
        },
    )


async def _check_collection(watch: dict) -> CheckResult:
    base = origin(watch["url"])
    coll = watch.get("target_ref") or collection_handle(watch["url"])
    path = f"/collections/{coll}/products.json" if coll else "/products.json"

    resp = await fetch(f"{base}{path}?limit=250",
                       etag=watch.get("etag"),
                       last_modified=watch.get("last_modified"))
    if resp.not_modified:
        return CheckResult(ok=True, not_modified=True, etag=resp.etag,
                           last_modified=resp.last_modified, http_status=304)

    if resp.ok and isinstance(resp.json, dict):
        products = resp.json.get("products") or []
        return CheckResult(
            ok=True,
            state=IN_STOCK if products else OUT_OF_STOCK,
            handles=[p.get("handle") for p in products if p.get("handle")],
            title=watch.get("name"),
            product_url=watch["url"],
            http_status=resp.status,
            etag=resp.etag,
            last_modified=resp.last_modified,
            extra={"product_count": len(products),
                   "source": "products.json",
                   "titles": {p.get("handle"): p.get("title") for p in products}},
        )

    # products.json blocked or unparseable — try the Atom feed instead.
    return await _check_collection_atom(base, coll, watch, resp.status)


async def _check_collection_atom(base: str, coll: str | None, watch: dict,
                                 prior_status: int | None) -> CheckResult:
    feed = f"{base}/collections/{coll or 'all'}.atom"
    resp = await fetch(feed)
    if not resp.ok:
        return CheckResult(
            ok=False, http_status=prior_status or resp.status,
            error=f"products.json failed (HTTP {prior_status}); "
                  f"atom fallback failed ({resp.error})",
        )
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        return CheckResult(ok=False, http_status=resp.status,
                           error=f"atom parse error: {exc}")

    handles, titles = [], {}
    for entry in root.findall("a:entry", _ATOM_NS):
        link = entry.find("a:link", _ATOM_NS)
        href = link.get("href") if link is not None else None
        handle = product_handle(href) if href else None
        if handle:
            handles.append(handle)
            title_el = entry.find("a:title", _ATOM_NS)
            titles[handle] = title_el.text if title_el is not None else None

    return CheckResult(
        ok=True,
        state=IN_STOCK if handles else OUT_OF_STOCK,
        handles=handles,
        product_url=watch["url"],
        http_status=resp.status,
        extra={"product_count": len(handles), "source": "atom", "titles": titles},
    )


# ----------------------------------------------------------------- detection

async def detect(url: str) -> dict | None:
    """Return watch config if this URL is a Shopify store, else None."""
    base = origin(url)
    handle = product_handle(url)

    if handle:
        resp = await fetch(f"{base}/products/{handle}.js")
        if resp.ok and isinstance(resp.json, dict) and resp.json.get("variants"):
            data = resp.json
            return {
                "strategy": NAME,
                "kind": "product",
                "name": data.get("title") or handle,
                "brand": urlparse(base).netloc.replace("www.", ""),
                "url": f"{base}/products/{handle}",
                "target_ref": handle,
                "detected_via": "products/{handle}.js",
                "variants": [
                    {"id": v.get("id"), "title": v.get("title"),
                     "available": bool(v.get("available")),
                     "price": _price(v.get("price"), in_cents=True)}
                    for v in (data.get("variants") or [])
                ],
            }
        return None

    coll = collection_handle(url)
    path = f"/collections/{coll}/products.json" if coll else "/products.json"
    resp = await fetch(f"{base}{path}?limit=1")
    if resp.ok and isinstance(resp.json, dict) and "products" in resp.json:
        host = urlparse(base).netloc.replace("www.", "")
        return {
            "strategy": NAME,
            "kind": "collection",
            "name": f"{host} · {coll or 'all products'}",
            "brand": host,
            "url": url,
            "target_ref": coll,
            "detected_via": path,
        }

    # Last resort: a store that gates JSON still ships an Atom feed.
    resp = await fetch(f"{base}/collections/{coll or 'all'}.atom")
    if resp.ok and "<feed" in resp.text[:2000]:
        host = urlparse(base).netloc.replace("www.", "")
        return {
            "strategy": NAME,
            "kind": "collection",
            "name": f"{host} · {coll or 'all products'}",
            "brand": host,
            "url": url,
            "target_ref": coll,
            "detected_via": "collections/*.atom",
        }
    return None
