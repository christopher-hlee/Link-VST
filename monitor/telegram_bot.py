"""Commands, so the app can be driven from the chat it already talks to.

Long polling rather than a webhook, deliberately. A webhook needs a public URL
registered with Telegram and a shared secret in the environment, and the whole
reason this exists is that editing the environment on the server is the thing
that is hard to reach. Polling needs nothing that is not already configured:
the bot token is here because the app already sends alerts with it.

Only the configured chat is obeyed. Anyone can message a bot whose username
they know, so every update from anywhere else is dropped without a reply —
silence rather than "not authorised", which would confirm the bot is live.
"""
import asyncio
import json
import logging

from . import db, filters, strategies
from .config import PUBLIC_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .fetcher import get_client
from .timeutil import EPOCH
from .notify.telegram import API, _esc, configured

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram_offset"
POLL_TIMEOUT = 25          # seconds Telegram holds the request open
_task: asyncio.Task | None = None

HELP = (
    "<b>Restock</b>\n"
    "/login — a one-tap sign-in link for the dashboard\n"
    "/check &lt;url&gt; — what the monitor can see at that address\n"
    "/status — watches, and when each was last swept\n"
    "/add &lt;url&gt; — start watching it\n"
    "/filter &lt;id&gt; &lt;saved-search-url&gt; — narrow a watch\n"
    "/filter &lt;id&gt; off — widen it back\n"
    "/help — this"
)


def origin() -> str | None:
    return PUBLIC_URL or db.kv_get("public_origin")


async def _send(text: str) -> None:
    await get_client().post(
        f"{API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=15.0)


# ----------------------------------------------------------------- commands

async def _login() -> str:
    from . import security

    if not security.configured():
        return ("Dashboard auth is not configured, so there is no session to "
                "hand out. Run <code>python -m monitor.hashpw</code> on the "
                "server first.")
    base = origin()
    if not base:
        return ("I do not know this app's public address yet — open the "
                "dashboard once from any device and ask again, or set "
                "PUBLIC_URL in monitor/.env.")
    token = security.issue_login_token()
    return (f'<a href="{base}/api/auth/telegram?t={token}">Tap to sign in</a>\n'
            "Good once, for ten minutes. Asking again cancels this one.")


async def _check(url: str) -> str:
    """What would a watch on this address actually see?

    The question that otherwise needs a shell on the server, which is exactly
    what is unavailable when a network sits between you and the dashboard.
    """
    if not url.startswith(("http://", "https://")):
        return "Give me a full URL, starting with https://"

    spec = filters.parse_boost_url(url)
    found = await strategies.detect(url)
    if not found:
        return (f"Nothing I recognise at {_esc(url)}.\n"
                "Not Shopify, or the catalogue is gated.")

    lines = [f"<b>{_esc(found.get('name') or url)}</b>",
             f"platform: {found.get('strategy')} · {found.get('kind')}",
             f"read via: <code>{_esc(found.get('detected_via') or '?')}</code>"]

    if found.get("kind") == "collection":
        result = await strategies.check(
            {"id": 0, "url": url, "kind": "collection",
             "strategy": found.get("strategy"),
             "target_ref": found.get("target_ref")})
        if result.ok:
            count = (result.extra or {}).get("product_count")
            lines.append(f"products: {count}")
            if (result.extra or {}).get("truncated"):
                lines.append("⚠️ more than I can read in one sweep — "
                             "watch a narrower collection")
        else:
            lines.append(f"⚠️ could not read it: {_esc(result.error or '?')}")

    if spec:
        lines.append("")
        lines.append(f"saved search: {_esc(filters.describe(spec))}")
        lines.append("<i>those parameters do not reach the API — I would "
                     "apply them myself</i>")
    return "\n".join(lines)


async def _status() -> str:
    watches = [w for w in db.list_watches() if w.get("enabled")]
    if not watches:
        return "No watches."
    lines = [f"<b>{len(watches)} watch(es)</b>"]
    for w in watches[:20]:
        baseline = db.get_baseline(w) or []
        live = w.get("last_seen_count")
        seen = f"{len(baseline)} tracked"
        if live is not None and live != len(baseline):
            seen += f" · {live} live"
        line = f"<code>{w['id']}</code> {_esc(w['name'])} — {seen}"
        spec = db.get_filter(w)
        if spec:
            line += f"\n    ↳ {_esc(filters.describe(spec))}"
        lines.append(line)
    return "\n".join(lines)


async def _add(url: str) -> str:
    """Start watching an address, applying any saved search it carries."""
    if not url.startswith(("http://", "https://")):
        return "Give me a full URL, starting with https://"

    found = await strategies.detect(url)
    if not found:
        return f"Nothing I recognise at {_esc(url)}."

    spec = filters.parse_boost_url(url)
    watch_id = db.create_watch(
        name=found.get("name") or url, brand=found.get("brand"), url=url,
        strategy=found.get("strategy"), kind=found.get("kind") or "product",
        target_ref=found.get("target_ref"),
        filter_json=json.dumps(spec) if spec else None,
        next_check_at=EPOCH)
    reply = [f"Watching <b>{_esc(found.get('name') or url)}</b> "
             f"(<code>{watch_id}</code>)."]
    if spec:
        reply.append(f"Filter: {_esc(filters.describe(spec))}")
    reply.append("<i>The first sweep adopts the catalogue in silence — "
                 "you hear about what arrives after it.</i>")
    return "\n".join(reply)


async def _filter(rest: str) -> str:
    """Narrow an existing watch to a saved search, or widen it back."""
    raw_id, _, arg = rest.strip().partition(" ")
    arg = arg.strip()
    if not raw_id.isdigit() or not arg:
        return "Usage: /filter &lt;id&gt; &lt;saved-search-url&gt;  ·  /filter &lt;id&gt; off"

    watch = db.get_watch(int(raw_id))
    if not watch:
        return f"No watch {raw_id}. /status lists them."

    if arg.lower() in ("off", "none", "clear"):
        db.update_watch(watch["id"], filter_json=None)
        return f"<b>{_esc(watch['name'])}</b> — filter cleared."

    spec = filters.parse_boost_url(arg)
    if not spec:
        return ("No filter parameters in that URL. Paste the storefront "
                "address with its facets still on it.")
    db.update_watch(watch["id"], filter_json=json.dumps(spec))
    # Deliberately does NOT rebaseline. The filter decides what is said, not
    # what has been seen, and replaying a catalogue already shown is what the
    # baseline exists to prevent.
    return (f"<b>{_esc(watch['name'])}</b>\n"
            f"↳ {_esc(filters.describe(spec))}\n"
            "<i>Applies to what arrives from now on.</i>")


async def handle(text: str) -> str | None:
    command, _, rest = text.strip().partition(" ")
    command = command.split("@")[0].lower()      # /check@mybot in a group
    if command == "/login":
        return await _login()
    if command == "/check":
        return await _check(rest.strip())
    if command == "/status":
        return await _status()
    if command == "/add":
        return await _add(rest.strip())
    if command == "/filter":
        return await _filter(rest)
    if command in ("/help", "/start"):
        return HELP
    return None


# ------------------------------------------------------------------ polling

async def _poll_once() -> None:
    offset = db.kv_get(OFFSET_KEY)
    params = {"timeout": POLL_TIMEOUT, "allowed_updates": '["message"]'}
    if offset:
        params["offset"] = int(offset)

    resp = await get_client().get(
        f"{API}/bot{TELEGRAM_BOT_TOKEN}/getUpdates", params=params,
        timeout=POLL_TIMEOUT + 10)
    if resp.status_code != 200:
        raise RuntimeError(f"getUpdates HTTP {resp.status_code}")

    for update in (resp.json().get("result") or []):
        # Advance the offset FIRST. A command that throws must not be replayed
        # on the next poll for the rest of the process's life.
        db.kv_set(OFFSET_KEY, str(update["update_id"] + 1))
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id")) != str(TELEGRAM_CHAT_ID):
            continue
        text = message.get("text") or ""
        if not text.startswith("/"):
            continue
        try:
            reply = await handle(text)
        except Exception as exc:
            log.exception("command failed: %s", text)
            reply = f"That failed: {_esc(f'{type(exc).__name__}: {exc}')}"
        if reply:
            await _send(reply)


async def _loop() -> None:
    backoff = 1
    while True:
        try:
            await _poll_once()
            backoff = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A bad token or no network should not become a hot loop against
            # someone else's API.
            log.warning("telegram poll failed (%s); retrying in %ss", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


def start() -> None:
    global _task
    if _task is not None or not configured():
        return
    _task = asyncio.get_event_loop().create_task(_loop())
    log.info("telegram commands listening")


async def shutdown() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    _task = None
