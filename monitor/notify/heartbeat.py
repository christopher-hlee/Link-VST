"""Dead-man's switch.

A monitor that dies looks exactly like a monitor reporting no drops, and you
will not notice on your own. healthchecks.io alerts by email when these pings
stop — deliberately a different delivery path from Telegram, so one outage
cannot silence both.

Process death is the easy case. The dangerous one is a monitor that is running
happily while every request it makes is refused — a banned IP looks identical to
a quiet market. So a tick where every check failed reports `/fail` rather than
success, and trips the same alarm as a crash.
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


async def ok() -> None:
    await ping()


async def fail(reason: str = "") -> None:
    """Trip the alarm. Same signal a crash produces."""
    if reason:
        log.error("heartbeat reporting failure: %s", reason)
    await ping("/fail")
