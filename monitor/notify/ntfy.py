"""High-priority channel for watches marked critical.

Telegram is the default because it is rich and free, but its notifications obey
Do Not Disturb — which is exactly wrong for a 3am restock you have been waiting
months for. ntfy delivers at a priority that pierces it, and costs nothing.

Only used for `alert_level="critical"` watches and for watch failures.
"""
import logging

from ..config import NTFY_SERVER, NTFY_TOPIC
from ..fetcher import get_client

log = logging.getLogger("monitor.ntfy")


def configured() -> bool:
    return bool(NTFY_TOPIC)


async def send(title: str, body: str, *, url: str | None = None,
               priority: str = "high", tags: str = "bell") -> None:
    """Raises on failure so the caller can record it against the event."""
    if not configured():
        raise RuntimeError("ntfy is not configured (NTFY_TOPIC)")

    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if url:
        # Renders as a tappable action in the ntfy client.
        headers["Actions"] = f"view, Open, {url}"

    resp = await get_client().post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        content=body.encode("utf-8"),
        headers=headers,
        timeout=15.0,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"ntfy HTTP {resp.status_code}: {resp.text[:200]}")
