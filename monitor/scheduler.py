"""The polling loop.

One APScheduler job fires every TICK_SECONDS and claims whatever watches are
due. Per-watch intervals live in the database rather than in scheduler jobs, so
changing a watch's tier takes effect on the next tick with no job churn, and a
restart resumes exactly where it left off.
"""
import asyncio
import json
import logging
import random

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, strategies
from .config import (
    ALERT_COOLDOWN_SECONDS, FAILURE_ALERT_THRESHOLD, JITTER_FRACTION,
    MAX_CONCURRENT_CHECKS, TICK_SECONDS,
)
from .notify import heartbeat, ntfy, telegram
from .statemachine import PRICE_DROP, WATCH_FAILING, decide, next_interval
from .timeutil import is_future, parse, seconds_since, stamp, stamp_in, utcnow

log = logging.getLogger("monitor.scheduler")

_scheduler: AsyncIOScheduler | None = None
_gate = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
_tick_lock = asyncio.Lock()


def jittered(seconds: int) -> float:
    """Spread polls so they never land on a round boundary.

    A perfectly periodic request pattern is a trivially detectable signature;
    the jitter costs nothing and makes the traffic look ordinary.
    """
    return seconds * (1.0 + random.uniform(-JITTER_FRACTION, JITTER_FRACTION))


async def check_watch(watch: dict) -> bool:
    """Run one watch end to end: fetch, decide, persist, notify.

    Returns whether the check itself succeeded — not whether the item is in
    stock. The caller uses this to detect a systemic outage.
    """
    async with _gate:
        try:
            result = await strategies.check(watch)
        except Exception as exc:                      # never kill the tick
            log.exception("watch %s raised", watch["id"])
            from .statemachine import CheckResult
            result = CheckResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    decision = decide(
        kind=watch.get("kind") or "product",
        prev_state=watch.get("last_state") or "unknown",
        prev_failures=watch.get("consecutive_failures") or 0,
        prev_baseline=db.get_baseline(watch),
        prev_price=watch.get("last_price"),
        result=result,
        failure_threshold=FAILURE_ALERT_THRESHOLD,
    )

    interval = next_interval(
        watch.get("base_interval_s") or 300,
        watch.get("hot_interval_s") or 45,
        (parse(watch.get("hot_until")).timestamp()
         if is_future(watch.get("hot_until")) else None),
        utcnow().timestamp(),
    )

    updates = {
        "last_state": decision.state,
        "consecutive_failures": decision.failures,
        "last_price": decision.price,
        "last_checked_at": stamp(),
        "next_check_at": stamp_in(jittered(interval)),
        "last_error": result.error,
    }
    if decision.baseline is not None:
        updates["baseline_json"] = json.dumps(decision.baseline)
    if decision.pause and watch.get("enabled"):
        # A watch that has failed this many times running is not watching
        # anything. Stop polling rather than retrying into the void forever.
        updates["enabled"] = 0
        log.warning("watch %s paused after %d consecutive failures",
                    watch["id"], decision.failures)
    if result.ok and not result.not_modified:
        # Only refresh cache validators on a real 200; a 304 keeps the old ones.
        updates["etag"] = result.etag
        updates["last_modified"] = result.last_modified
        if result.title:
            updates["last_title"] = result.title
        if result.image:
            updates["last_image"] = result.image
        offers = result.extra.get("offers")
        if offers is not None:
            updates["last_offers_json"] = json.dumps(offers)

    db.update_watch(watch["id"], **updates)
    db.record_check(
        watch["id"], ok=result.ok,
        state=decision.state if result.ok else None,
        http_status=result.http_status,
        latency_ms=result.extra.get("latency_ms"),
        error=result.error,
    )

    for event in decision.events:
        await _emit(watch, event)

    return result.ok


def _effective_level(watch: dict, kind: str) -> str:
    """A broken watch is always urgent, whatever the watch is set to.

    Everything else honours the watch's own alert_level.
    """
    if kind == WATCH_FAILING:
        return "critical"
    return "critical" if watch.get("alert_level") == "critical" else "info"


async def _emit(watch: dict, event) -> None:
    if _suppressed(watch, event.kind):
        log.info("watch %s: %s suppressed by cooldown", watch["id"], event.kind)
        return

    level = _effective_level(watch, event.kind)
    event_id = db.insert_event(watch["id"], event.kind, event.from_state,
                               event.to_state, event.payload)

    delivered = False
    errors: list[str] = []

    try:
        await telegram.send_event(watch, event.kind, event.payload)
        delivered = True
    except Exception as exc:
        errors.append(f"telegram: {exc}")

    # Critical watches also go out on a channel that ignores Do Not Disturb.
    if level == "critical" and ntfy.configured():
        try:
            await ntfy.send(
                title=f"{watch.get('brand') or 'Restock'} — {event.kind.replace('_', ' ')}",
                body=(event.payload.get("title") or watch.get("name") or "")[:200],
                url=event.payload.get("cart_url") or event.payload.get("product_url"),
                priority="urgent" if event.kind != WATCH_FAILING else "high",
            )
            delivered = True
        except Exception as exc:
            errors.append(f"ntfy: {exc}")

    # The event row survives either way, so the dashboard shows that something
    # fired even when every channel failed.
    db.mark_notified(event_id, error="; ".join(errors)[:500] if errors else None)
    if delivered:
        db.update_watch(watch["id"], last_alert_at=stamp())
        log.info("watch %s: %s notified (%s)", watch["id"], event.kind, level)
    else:
        log.error("watch %s: %s undelivered: %s", watch["id"], event.kind,
                  "; ".join(errors))


def _suppressed(watch: dict, kind: str) -> bool:
    """Cooldown deliberately applies only to price drops.

    Restocks and new drops are already deduplicated by the state machine — they
    fire on a transition, not on a condition — so rate-limiting them could only
    ever swallow the one alert the whole app exists to deliver.
    """
    if kind != PRICE_DROP:
        return False
    elapsed = seconds_since(watch.get("last_alert_at"))
    return elapsed is not None and elapsed < ALERT_COOLDOWN_SECONDS


async def tick() -> None:
    """Claim due watches and check them concurrently."""
    if _tick_lock.locked():
        log.warning("previous tick still running; skipping this one")
        return
    async with _tick_lock:
        try:
            due = db.due_watches(limit=50)
        except Exception:
            log.exception("could not read due watches")
            return

        if not due:
            await heartbeat.ok()
            return

        log.info("tick: %d watch(es) due", len(due))
        results = await asyncio.gather(*(check_watch(w) for w in due),
                                       return_exceptions=True)

        succeeded = sum(1 for r in results if r is True)
        if succeeded == 0:
            # Every single check failed. That is not a quiet market — it is an
            # outage, most likely this host being refused. Trip the same alarm a
            # crash would, because a green heartbeat here would be a lie.
            await heartbeat.fail(
                f"all {len(results)} check(s) failed this tick")
        else:
            await heartbeat.ok()


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(tick, "interval", seconds=TICK_SECONDS,
                       id="tick", max_instances=1, coalesce=True,
                       misfire_grace_time=TICK_SECONDS)
    _scheduler.start()
    log.info("scheduler started (tick every %ss)", TICK_SECONDS)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
