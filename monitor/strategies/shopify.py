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
from ..statemachine import CheckResult, HELD, IN_STOCK, OUT_OF_STOCK

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


# Shopify variant titles are free text: "M", "Medium", "M / Black", "Large".
# Normalise enough to match a stated size preference without being clever.
_SIZE_ALIASES = {
    "xs": {"xs", "extra small", "x-small"},
    "s": {"s", "small"},
    "m": {"m", "medium", "med"},
    "l": {"l", "large"},
    "xl": {"xl", "extra large", "x-large"},
    "xxl": {"xxl", "2xl", "xx-large"},
}


def _canonical(token: str) -> str:
    token = token.strip().lower()
    for canon, spellings in _SIZE_ALIASES.items():
        if token in spellings:
            return canon
    return token


def parse_sizes(raw: str | None) -> list[str]:
    """Ordered size preference, most wanted first. Empty means any size."""
    if not raw:
        return []
    return [_canonical(part) for part in raw.split(",") if part.strip()]


def variant_matches(variant_title: str | None, prefs: list[str]) -> bool:
    """True when any slash-separated option of the variant matches a preference.

    "M / Black" matches a preference of "m", so a colourway axis does not
    prevent a size match.
    """
    if not prefs:
        return True
    options = [_canonical(part) for part in (variant_title or "").split("/")]
    return any(pref in options for pref in prefs)


def _price(raw, *, in_cents: bool) -> float | None:
    """products.json gives a decimal string; the .js endpoint gives integer cents."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value / 100.0 if in_cents else value


def _image_url(raw) -> str | None:
    """Shopify serves protocol-relative image URLs; a bare // breaks an <img>."""
    if isinstance(raw, dict):
        raw = raw.get("src")
    if not isinstance(raw, str):
        return None
    return "https:" + raw if raw.startswith("//") else raw


def _collection_item(base: str, product: dict) -> dict:
    """Everything an alert needs about one catalogue entry, from data in hand.

    products.json already returns variants and images per product, so the cart
    permalinks cost nothing extra — the alternative is a second request per new
    product at exactly the moment a drop makes the store busiest.
    """
    handle = product.get("handle")
    variants = product.get("variants") or []
    # products.json has no `available` on some themes; treat a missing flag as
    # available rather than silently dropping every cart link.
    live = [v for v in variants if v.get("available", True) and v.get("id")]
    offers = [
        {
            "id": v.get("id"),
            "title": v.get("title"),
            "price": _price(v.get("price"), in_cents=False),
            "cart_url": cart_url(base, v.get("id")),
            "preferred": False,
        }
        for v in live
    ]
    images = product.get("images") or []
    return {
        "url": f"{base}/products/{handle}",
        "title": product.get("title"),
        "price": offers[0]["price"] if offers else None,
        "image": _image_url(images[0] if images else None),
        "offers": offers[:12],
    }


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
        return CheckResult(ok=False, http_status=resp.status, error=resp.error,
                           rate_limited=resp.rate_limited,
                           retry_after=resp.retry_after)

    data = resp.json
    if not isinstance(data, dict):
        return CheckResult(ok=False, http_status=resp.status,
                           error="response was not a JSON object")

    variants = data.get("variants") or []
    available = [v for v in variants if v.get("available")]
    prefs = parse_sizes(watch.get("size_pref"))

    # Every available variant gets its own cart permalink, so an alert can offer
    # one button per size rather than guessing which one the person wanted.
    offers = [
        {
            "id": v.get("id"),
            "title": v.get("title"),
            "price": _price(v.get("price"), in_cents=True),
            "cart_url": cart_url(base, v.get("id")),
            "preferred": variant_matches(v.get("title"), prefs),
        }
        for v in available
    ]
    wanted = [o for o in offers if o["preferred"]]

    # Three outcomes, not two. Nothing available is sold out. Something
    # available in a watched size is in stock. Something available but NOT in a
    # watched size is HELD — the watch is healthy and right to stay quiet, and
    # saying "sold out" there would be a lie the person cannot detect.
    if not available:
        state = OUT_OF_STOCK
    elif prefs and not wanted:
        state = HELD
    else:
        state = IN_STOCK

    # Lead with a size the person actually asked for.
    chosen = (wanted or offers or [None])[0]

    image = data.get("featured_image")
    if isinstance(image, str) and image.startswith("//"):
        image = "https:" + image

    return CheckResult(
        ok=True,
        state=state,
        price=(chosen or {}).get("price") if chosen else None,
        title=data.get("title"),
        product_url=f"{base}/products/{handle}",
        cart_url=(chosen or {}).get("cart_url") if state == IN_STOCK else None,
        image=image,
        http_status=resp.status,
        etag=resp.etag,
        last_modified=resp.last_modified,
        extra={
            "variants_total": len(variants),
            "variants_available": len(available),
            "size_prefs": prefs,
            "offers": offers[:12],
            "matched_offers": wanted[:12],
            # Named explicitly so a held alert can say what came back and what
            # is being watched, rather than leaving silence unexplained.
            "available_sizes": [o["title"] for o in offers if o.get("title")],
            "watched_sizes": [p.upper() for p in prefs],
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

    if resp.rate_limited:
        # Falling back to the Atom feed here would be a second request to a host
        # that just asked for fewer. The fallback exists for a gated endpoint,
        # not for backpressure.
        return CheckResult(ok=False, http_status=429, rate_limited=True,
                           retry_after=resp.retry_after, error=resp.error)

    if resp.ok and isinstance(resp.json, dict):
        products = resp.json.get("products") or []
        handles = [p.get("handle") for p in products if p.get("handle")]
        items = {p["handle"]: _collection_item(base, p)
                 for p in products if p.get("handle")}
        return CheckResult(
            ok=True,
            state=IN_STOCK if products else OUT_OF_STOCK,
            handles=handles,
            title=watch.get("name"),
            product_url=watch["url"],
            http_status=resp.status,
            etag=resp.etag,
            last_modified=resp.last_modified,
            extra={"product_count": len(products),
                   "source": "products.json",
                   "titles": {h: items[h]["title"] for h in items},
                   # The handle is the store's own canonical identifier, so the
                   # product URL is derivable with no search and no extra
                   # request. An alert that names an item but links to the
                   # collection makes the reader hunt for what we already knew.
                   "links": {h: items[h]["url"] for h in items},
                   "items": items},
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

    handles, titles, links, items = [], {}, {}, {}
    for entry in root.findall("a:entry", _ATOM_NS):
        link = entry.find("a:link", _ATOM_NS)
        href = link.get("href") if link is not None else None
        handle = product_handle(href) if href else None
        if handle:
            handles.append(handle)
            title_el = entry.find("a:title", _ATOM_NS)
            title = title_el.text if title_el is not None else None
            titles[handle] = title
            # The feed carries no variants and no prices, so an item here can
            # only ever open the product page — never a cart permalink.
            links[handle] = href
            items[handle] = {"url": href, "title": title,
                             "price": None, "image": None, "offers": []}

    return CheckResult(
        ok=True,
        state=IN_STOCK if handles else OUT_OF_STOCK,
        handles=handles,
        product_url=watch["url"],
        http_status=resp.status,
        extra={"product_count": len(handles), "source": "atom",
               "titles": titles, "links": links, "items": items},
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
