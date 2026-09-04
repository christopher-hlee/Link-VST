"""Telegram bot delivery.

Chosen over email because IONOS blocks outbound port 25, and over SMS because
it's free and supports inline buttons — including a Shopify cart permalink,
which removes the slowest step between the alert and the checkout page.
"""
import html
import logging

from ..config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from ..fetcher import get_client
from ..statemachine import (
    HELD_NOTE, NEW_PRODUCT, PRICE_DROP, RESTOCK, WATCH_FAILING,
    WATCH_RECOVERED,
)

log = logging.getLogger("monitor.telegram")

API = "https://api.telegram.org"

# Telegram renders only bold, italic, code and links. Emoji are legitimate
# here — this is the one surface where they are genuine message content.
HEADLINE = {
    NEW_PRODUCT:     "🆕",
    PRICE_DROP:      "🏷️",
    WATCH_FAILING:   "⚠️",
    WATCH_RECOVERED: "✅",
}


def configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _esc(value) -> str:
    return html.escape(str(value), quote=False)


def _money(value) -> str:
    """$185, not $185.0 — Shopify prices are whole numbers far more often than not."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"


def _ago(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{round(seconds / 3600)}h"


def _sizes(payload: dict) -> list[str]:
    return [o["title"] for o in (payload.get("offers") or [])
            if o.get("title") and o["title"].lower() not in ("default title", "default")]


def render(watch: dict, kind: str, payload: dict) -> str:
    """Build one message body per event kind.

    Each shape is deliberately different. A restock in your size, a restock with
    no sizes, a drop, a broken watch and a held note are five different pieces
    of news, and flattening them into one template makes the important one
    (something is buyable right now) read like the routine ones.
    """
    brand = watch.get("brand") or ""
    title = payload.get("title") or watch.get("name") or "Watch"
    price = payload.get("price")
    lines: list[str] = []

    if kind == WATCH_FAILING:
        lines.append(f"<b>⚠️ Watch broken — {_esc(title)}</b>")
        if brand:
            lines.append(f"<b>{_esc(brand)}</b>")
        lines.append(f"<code>{_esc(payload.get('error') or 'unknown error')}</code>")
        lines.append("")
        n = payload.get("failures")
        stale = watch.get("last_state")
        sentence = f"{n} failed checks in a row."
        if stale and stale != "unknown":
            sentence += (f" The state you last saw (<i>{_esc(stale.replace('_', ' '))}</i>)"
                         " is stale and should not be trusted.")
        lines.append(sentence)
        if payload.get("paused"):
            lines.append("")
            lines.append(f"<code>watch paused after {n} failures</code>")
        return "\n".join(lines)

    if kind == HELD_NOTE:
        # Informational, sent once, and explicitly not an alert.
        got = ", ".join(payload.get("available_sizes") or []) or "another size"
        want = ", ".join(payload.get("watched_sizes") or []) or "your size"
        lines.append(f"<b>Held — {_esc(title)}</b>")
        head = f"{_esc(brand)} · " if brand else ""
        if price is not None:
            head += f"{_money(price)} · "
        lines.append(f"{head}restocked in <b>{_esc(got)}</b> only. "
                     f"You watch <b>{_esc(want)}</b>, so this is not an alert — "
                     "just a note, sent once.")
        return "\n".join(lines)

    if kind == WATCH_RECOVERED:
        lines.append(f"<b>✅ Watch recovered — {_esc(title)}</b>")
        lines.append(f"Back to normal after {payload.get('after_failures')} failures.")
        return "\n".join(lines)

    if kind == NEW_PRODUCT:
        handles = payload.get("handles") or []
        titles = payload.get("titles") or {}
        n = len(handles)

        # A feed match and a storefront drop are both "something new appeared",
        # but calling a news article a product — and counting a feed's entries
        # as a catalogue — reads as a bug even when the alert is correct.
        if payload.get("is_announcement"):
            terms = payload.get("keywords") or []
            joined = (" + " if payload.get("keyword_mode") == "all" else ", ").join(terms)
            lines.append(f"<b>🆕 {n} match{'' if n == 1 else 'es'}"
                         f"{' — ' + _esc(joined) if joined else ''}</b>")
            if brand:
                lines.append(f"<b>{_esc(brand)}</b>")
            for h in handles[:10]:
                lines.append(f"· {_esc(titles.get(h) or h)}")
            if n > 10:
                lines.append(f"· …and {n - 10} more")
            return "\n".join(lines)

        lines.append(f"<b>🆕 {n} new from {_esc(brand or title)}</b>")
        # Not "an hour ago": the poll interval adapts per store and is usually
        # far shorter, so naming a duration we do not know is a small lie in a
        # message whose whole value is being trusted about timing.
        lines.append("Not in the catalogue on the previous sweep:")
        for h in handles[:10]:
            lines.append(f"· {_esc(titles.get(h) or h)}")
        if n > 10:
            lines.append(f"· …and {n - 10} more")
        before = payload.get("baseline_count")
        if before is not None:
            lines.append("")
            lines.append(f"<code>{before} → {before + n} products</code>")
        # How far behind the store we were. Without this, "the alert was late"
        # can only be argued about; with it, the lag is a number you can read
        # off your phone.
        lag = payload.get("listed_ago_s")
        if isinstance(lag, int):
            lines.append(f"<code>listed {_ago(lag)} before this alert</code>")
        return "\n".join(lines)

    if kind == PRICE_DROP:
        lines.append(f"<b>🏷️ Price drop — {_esc(title)}</b>")
        if brand:
            lines.append(f"<b>{_esc(brand)}</b>")
        lines.append(f"{_money(payload.get('old_price'))} → "
                 f"<b>{_money(payload.get('new_price'))}</b>")
        return "\n".join(lines)

    # RESTOCK — two shapes, sized and single-variant.
    matched = [o["title"] for o in (payload.get("matched_offers") or [])
               if o.get("title")]
    sizes = _sizes(payload)

    if matched:
        lines.append(f"<b>⚡ Back in your size — {_esc(title)}</b>")
    else:
        lines.append(f"<b>⚡ Back in stock — {_esc(title)}</b>")

    head = f"<b>{_esc(brand)}</b>" if brand else ""
    if price is not None:
        head = f"{head} · {_money(price)}" if head else _money(price)
    if head:
        lines.append(head)

    if matched:
        lines.append(f"<b>Your sizes: {_esc(', '.join(matched))}</b>")
        also = [s for s in sizes if s not in matched]
        if also:
            lines.append(f"Also back: {_esc(', '.join(also))}")
        lines.append("")
        lines.append("<i>Tap a size to put it in the cart.</i>")
    elif len(sizes) > 1:
        lines.append(f"Available: {_esc(', '.join(sizes))}")
        lines.append("")
        lines.append("<i>Tap a size to put it in the cart.</i>")
    else:
        lines.append("One variant, no sizes.")

    stores = payload.get("pickup_stores") or payload.get("stores") or []
    if stores:
        lines.append("")
        lines.append("<b>Local pickup:</b>")
        for store in stores[:5]:
            qty = store.get("quantity")
            lines.append(f"· {_esc(store.get('store') or store.get('city'))}"
                         f"{f' ×{qty}' if qty else ''}")

    return "\n".join(lines)


MAX_SIZE_BUTTONS = 8
BUTTONS_PER_ROW = 4


def _keyboard(watch: dict, payload: dict) -> dict | None:
    """Inline keyboard: one cart button per size, then the product page.

    Apparel is the reason this is not a single button. A lone "Add to cart"
    silently picks some variant, and being dropped into checkout holding the
    wrong size is worse than no shortcut at all — so every available size gets
    its own permalink and the choice stays with the person.
    """
    rows: list[list[dict]] = []

    # An announcement's useful link is the matched article, not the feed the
    # match came from. Linking to the feed makes the alert unactionable.
    if payload.get("is_announcement"):
        links = payload.get("links") or {}
        titles = payload.get("titles") or {}
        for h in (payload.get("handles") or [])[:4]:
            url = links.get(h)
            if url:
                label = (titles.get(h) or "Read it")[:40]
                rows.append([{"text": f"📰 {label}", "url": url}])
        if not rows and watch.get("url"):
            rows.append([{"text": "Open the feed", "url": watch["url"]}])
        return {"inline_keyboard": rows} if rows else None

    if payload.get("handles"):
        return _drop_keyboard(watch, payload)

    offers = payload.get("matched_offers") or payload.get("offers") or []
    rows.extend(_size_rows(offers))
    if not rows and payload.get("cart_url"):
        # One variant, or a product with no size axis at all.
        rows.append([{"text": "⚡ Add to cart", "url": payload["cart_url"]}])

    product_url = payload.get("product_url") or watch.get("url")
    if product_url:
        rows.append([{"text": "Open product page", "url": product_url}])

    return {"inline_keyboard": rows} if rows else None


def _size_rows(offers: list[dict]) -> list[list[dict]]:
    """One cart button per named size, wrapped into rows. [] when unsized."""
    sized = [o for o in offers if o.get("cart_url") and o.get("title")
             and o["title"].lower() not in ("default title", "default")]
    if len(sized) < 2:
        return []
    rows, row = [], []
    for offer in sized[:MAX_SIZE_BUTTONS]:
        row.append({"text": f"⚡ {offer['title']}", "url": offer["cart_url"]})
        if len(row) == BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


MAX_ITEM_BUTTONS = 8


def _drop_keyboard(watch: dict, payload: dict) -> dict | None:
    """Buttons for a drop: one per newly-listed product, not one for the store.

    The collection is what we poll, not what the person wants to open. Sending
    them to the shop-all page to re-find the item we just named is the alert
    doing the easy half of its job.
    """
    handles = payload.get("handles") or []
    items = payload.get("items") or {}
    links = payload.get("links") or {}
    titles = payload.get("titles") or {}
    rows: list[list[dict]] = []

    def url_for(handle):
        return (items.get(handle) or {}).get("url") or links.get(handle)

    # A single new product is just a restock with no prior state: it deserves
    # the same per-size cart buttons rather than a lone link.
    if len(handles) == 1:
        handle = handles[0]
        item = items.get(handle) or {}
        rows.extend(_size_rows(item.get("offers") or []))
        if not rows:
            offers = item.get("offers") or []
            if len(offers) == 1 and offers[0].get("cart_url"):
                rows.append([{"text": "⚡ Add to cart", "url": offers[0]["cart_url"]}])
        target = url_for(handle)
        if target:
            rows.append([{"text": "Open product page", "url": target}])
        elif watch.get("url"):
            rows.append([{"text": "Open product page", "url": watch["url"]}])
        return {"inline_keyboard": rows} if rows else None

    # Several at once: one link each. Sizes across many products would need a
    # keyboard nobody can read, so the product page takes the last step.
    for handle in handles[:MAX_ITEM_BUTTONS]:
        target = url_for(handle)
        if target:
            label = (titles.get(handle) or handle)[:40]
            rows.append([{"text": f"🆕 {label}", "url": target}])

    store_url = watch.get("url") or payload.get("product_url")
    if store_url:
        # Only worth a row once the per-item buttons exist or are incomplete.
        if not rows:
            rows.append([{"text": "Open product page", "url": store_url}])
        elif len(handles) > len(rows):
            rows.append([{"text": f"See all {len(handles)} at "
                                  f"{watch.get('brand') or 'the store'}",
                          "url": store_url}])

    return {"inline_keyboard": rows} if rows else None


async def send_event(watch: dict, kind: str, payload: dict) -> None:
    """Raises on failure so the caller can record notify_error."""
    if not configured():
        raise RuntimeError("Telegram is not configured "
                           "(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")

    body = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": render(watch, kind, payload),
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    markup = _keyboard(watch, payload)
    if markup:
        body["reply_markup"] = markup

    resp = await get_client().post(
        f"{API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=body, timeout=15.0
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")


async def send_text(text: str) -> None:
    """Plain message — used by the connectivity test in the dashboard."""
    if not configured():
        raise RuntimeError("Telegram is not configured")
    resp = await get_client().post(
        f"{API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")
