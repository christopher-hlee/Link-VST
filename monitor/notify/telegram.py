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
    NEW_PRODUCT, PRICE_DROP, RESTOCK, WATCH_FAILING, WATCH_RECOVERED,
)

log = logging.getLogger("monitor.telegram")

API = "https://api.telegram.org"

HEADLINE = {
    RESTOCK:         "🟢 BACK IN STOCK",
    NEW_PRODUCT:     "🔵 NEW DROP",
    PRICE_DROP:      "🏷️ PRICE DROP",
    WATCH_FAILING:   "🔴 WATCH FAILING",
    WATCH_RECOVERED: "✅ WATCH RECOVERED",
}


def configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _esc(value) -> str:
    return html.escape(str(value), quote=False)


def render(watch: dict, kind: str, payload: dict) -> str:
    """Build the HTML message body for one event."""
    lines = [f"<b>{HEADLINE.get(kind, kind.upper())}</b>"]

    label = payload.get("title") or watch.get("name") or "Watch"
    brand = watch.get("brand")
    lines.append(f"{_esc(brand)} — {_esc(label)}" if brand else _esc(label))

    if kind == WATCH_FAILING:
        lines.append("")
        lines.append(f"Failed {payload.get('failures')} checks in a row.")
        lines.append(f"<code>{_esc(payload.get('error') or 'unknown error')}</code>")
        lines.append("")
        lines.append("<i>Stock state is stale until this recovers.</i>")
        return "\n".join(lines)

    if kind == WATCH_RECOVERED:
        lines.append(f"Recovered after {payload.get('after_failures')} failures.")
        return "\n".join(lines)

    if kind == PRICE_DROP:
        lines.append(f"${payload.get('old_price')} → <b>${payload.get('new_price')}</b>")
    elif payload.get("price") is not None:
        lines.append(f"<b>${payload['price']}</b>")

    if kind == NEW_PRODUCT:
        handles = payload.get("handles") or []
        titles = payload.get("titles") or {}
        lines.append("")
        lines.append(f"<b>{len(handles)} new item(s):</b>")
        for handle in handles[:10]:
            lines.append(f"• {_esc(titles.get(handle) or handle)}")
        if len(handles) > 10:
            lines.append(f"• …and {len(handles) - 10} more")

    offers = payload.get("matched_offers") or payload.get("offers") or []
    sizes = [o.get("title") for o in offers
             if o.get("title") and o["title"].lower() not in ("default title", "default")]
    if sizes:
        lines.append("")
        prefs = payload.get("size_prefs") or []
        label = "Your sizes" if prefs and payload.get("matched_offers") else "Available"
        lines.append(f"<b>{label}:</b> {_esc(', '.join(sizes[:12]))}")

    stores = payload.get("pickup_stores") or payload.get("stores") or []
    if stores:
        lines.append("")
        lines.append("<b>Local pickup:</b>")
        for store in stores[:5]:
            qty = store.get("quantity")
            suffix = f" ×{qty}" if qty else ""
            lines.append(f"• {_esc(store.get('store') or store.get('city'))}{suffix}")

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

    offers = payload.get("matched_offers") or payload.get("offers") or []
    sized = [o for o in offers if o.get("cart_url") and o.get("title")
             and o["title"].lower() not in ("default title", "default")]

    if len(sized) > 1:
        row: list[dict] = []
        for offer in sized[:MAX_SIZE_BUTTONS]:
            row.append({"text": f"⚡ {offer['title']}", "url": offer["cart_url"]})
            if len(row) == BUTTONS_PER_ROW:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    elif payload.get("cart_url"):
        # One variant, or a product with no size axis at all.
        rows.append([{"text": "⚡ Add to cart", "url": payload["cart_url"]}])

    product_url = payload.get("product_url") or watch.get("url")
    if product_url:
        rows.append([{"text": "Open product page", "url": product_url}])

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
