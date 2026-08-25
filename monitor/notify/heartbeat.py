"""Dead-man's switch.

A monitor that dies looks exactly like a monitor reporting no drops, and you
will not notice on your own. healthchecks.io alerts by email when these pings
stop — deliberately a different delivery path from Telegram, so one outage
cannot silence both.
"""
import logging

from ..config import HEALTHCHECK_URL
from ..fetcher import get_client

log = logging.getLogger("monitor.heartbeat")


async def ping(suffix: str = "") -> None:
    """Best-effort; a failed heartbeat must never break a scheduler tick."""
    if not HEALTHCHECK_URL:
        return
    url = HEALTHCHECK_URL.rstrip("/") + suffix
    try:
        await get_client().get(url, timeout=10.0)
    except Exception as exc:
        log.warning("heartbeat ping failed: %s", exc)
