"""Generic storefront strategy: read the structured data retailers publish.

Most retailers embed schema.org product data as JSON-LD, because Google Shopping
requires it. That makes it a documented public contract rather than a private
API reverse-engineered from a site's own frontend — the distinction that matters
after RedSky retired `pdp_fulfillment_v1` and took the Target strategy with it.

It also carries the one field a console launch needs: `PreOrder`. "Available at
a later date" and "preorder now" are different values of the same property, so
the moment a listing flips, this sees it.

Deliberately the LAST strategy tried. Shopify product pages also ship JSON-LD,
and the Shopify strategy is strictly better for them — per-variant availability
and a cart permalink, neither of which schema.org exposes.
"""
import json
import re
from urllib.parse import urlparse

from ..fetcher import fetch
from ..statemachine import CheckResult, IN_STOCK, OUT_OF_STOCK

NAME = "retail"

_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)

# schema.org availability, lowercased and stripped of its URL prefix. Anything
# that means "you can give them money right now" is buyable; a preorder very
# much is, which is the whole point of this strategy.
BUYABLE = {"instock", "onlineonly", "instoreonly", "limitedavailability"}
PREORDER = {"preorder", "presale", "backorder"}
SOLD_OUT = {"outofstock", "soldout", "discontinued"}


def _availability(raw) -> str | None:
    """Normalise `https://schema.org/PreOrder` to `preorder`."""
    if not isinstance(raw, str):
        return None
    return raw.rsplit("/", 1)[-1].strip().lower() or None


def _blocks(html: str) -> list:
    """Every JSON-LD payload on the page, skipping any that will not parse."""
    out = []
    for match in _LD_RE.findall(html or ""):
        try:
            out.append(json.loads(match.strip()))
        except (ValueError, TypeError):
            continue           # one broken block must not hide the others
    return out


def _walk(node, depth: int = 0):
    """Yield every dict in a JSON-LD document.

    Sites ship three shapes interchangeably: a bare Product, a list of nodes,
    and an `@graph` wrapper. Walking handles all three without caring which.
    """
    if depth > 6:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1)


def _is_product(node: dict) -> bool:
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.lower() in
               ("product", "productgroup", "individualproduct") for t in types)


def _offers(node: dict) -> list[dict]:
    """Offers, whether the site ships one object or a list of them."""
    raw = node.get("offers")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for offer in raw:
        if isinstance(offer, dict):
            out.append(offer)
            # AggregateOffer nests the real offers one level down.
            nested = offer.get("offers")
            if isinstance(nested, list):
                out += [o for o in nested if isinstance(o, dict)]
    return out


def _price(offers: list[dict]) -> float | None:
    for offer in offers:
        for key in ("price", "lowPrice"):
            try:
                return float(str(offer.get(key)).replace(",", ""))
            except (TypeError, ValueError):
                continue
    return None


def _image(node: dict) -> str | None:
    image = node.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url")
    return image if isinstance(image, str) else None


def find_product(html: str) -> dict | None:
    """The first schema.org Product carrying an offer, or None."""
    fallback = None
    for block in _blocks(html):
        for node in _walk(block):
            if not isinstance(node, dict) or not _is_product(node):
                continue
            if _offers(node):
                return node
            fallback = fallback or node        # a Product with no offer at all
    return fallback


async def check(watch: dict) -> CheckResult:
    resp = await fetch(watch["url"], etag=watch.get("etag"),
                       last_modified=watch.get("last_modified"))
    if resp.not_modified:
        return CheckResult(ok=True, not_modified=True, etag=resp.etag,
                           last_modified=resp.last_modified, http_status=304)
    if not resp.ok:
        return CheckResult(ok=False, http_status=resp.status, error=resp.error,
                           rate_limited=resp.rate_limited,
                           retry_after=resp.retry_after)

    product = find_product(resp.text)
    if product is None:
        # Emphatically NOT out of stock. A page we cannot read tells us nothing
        # about stock, and reporting "sold out" here would be a lie that looks
        # exactly like the truth for as long as the watch lives.
        return CheckResult(
            ok=False, http_status=resp.status,
            error="no schema.org Product data on this page — the listing may be "
                  "rendered in the browser, or this is not a product page",
        )

    offers = _offers(product)
    states = {_availability(o.get("availability")) for o in offers}
    states.discard(None)

    buyable = bool(states & BUYABLE)
    preorder = bool(states & PREORDER)

    if not states:
        # A Product with no availability at all is the "available at a later
        # date" shape: listed, described, but not yet orderable.
        state = OUT_OF_STOCK
    elif buyable or preorder:
        state = IN_STOCK
    else:
        state = OUT_OF_STOCK

    return CheckResult(
        ok=True,
        state=state,
        price=_price(offers),
        title=product.get("name") or watch.get("name"),
        product_url=watch["url"],
        image=_image(product),
        http_status=resp.status,
        etag=resp.etag,
        last_modified=resp.last_modified,
        extra={
            # Lets the alert say "preorder open" rather than "back in stock",
            # which is a materially different thing to be told.
            "preorder": preorder and not buyable,
            "availability": sorted(states),
            "source": "json-ld",
        },
    )


async def detect(url: str) -> dict | None:
    resp = await fetch(url)
    if not resp.ok:
        return None
    product = find_product(resp.text)
    if product is None:
        return None
    host = urlparse(url).netloc.replace("www.", "")
    return {
        "strategy": NAME,
        "kind": "product",
        "name": product.get("name") or host,
        "brand": host,
        "url": url,
        "detected_via": "schema.org JSON-LD",
    }
