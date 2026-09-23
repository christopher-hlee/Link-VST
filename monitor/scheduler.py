"""The polling loop.

One APScheduler job fires every TICK_SECONDS and claims whatever watches are
due. Per-watch intervals live in the database rather than in scheduler jobs, so
changing a watch's tier takes effect on the next tick with no job churn, and a
restart resumes exactly where it left off.
"""
import asyncio
import os
import json
import logging
import random
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db, strategies
from .strategies import shopify
from .config import (
    ALERT_COOLDOWN_SECONDS, FAILURE_ALERT_THRESHOLD, JITTER_FRACTION,
    MAX_CONCURRENT_CHECKS, TICK_SECONDS,
)
from .notify import heartbeat, ntfy, telegram
from .statemachine import (PRICE_DROP, WATCH_FAILING, adapt_interval, decide,
                           next_interval)
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


def _sweep_window(watch: dict):
    """Anything the store published after this instant is new to us.

    Measured from the last sweep that actually read the catalogue, not from the
    last check — a watch that failed for three days would otherwise have a
    window of seconds and hide every drop it missed.

    The slack is deliberately generous. Being too wide can only let through a
    product we genuinely had not recorded, because the baseline still
    de-duplicates; being too narrow silently drops a real release.
    """
    interval = watch.get("base_interval_s") or 300
    slack = max(2 * interval, 900)
    last = parse(watch.get("last_sweep_at"))
    if last is None:
        # No recorded sweep: trust only what the store published very recently,
        # so a first run cannot mistake an entire catalogue for a drop.
        return utcnow() - timedelta(seconds=slack)
    return last - timedelta(seconds=slack)


CURRENCY_RETRY_S = 24 * 3600


def currency_key(watch_id: int) -> str:
    return f"currency_probe:{watch_id}"


async def learn_currency(watch: dict) -> None:
    """Ask a Shopify store, once, what currency its prices are in.

    One request per watch for its whole life, recorded in kv so a restart does
    not ask again. A person's own choice (set from the dashboard) is recorded
    as "manual" and is never overridden. A store that could not be reached is
    asked again a day later, not on every tick.
    """
    if watch.get("strategy") != "shopify" or not watch.get("id"):
        return
    key = currency_key(watch["id"])
    seen = db.kv_get(key)
    if seen is not None:
        if not seen.startswith("retry@"):
            return
        waited = seconds_since(seen[len("retry@"):])
        if waited is not None and waited < CURRENCY_RETRY_S:
            return
    try:
        answered, code = await shopify.store_currency(watch["url"])
    except Exception:                               # never cost a check
        answered, code = False, None
    if not answered:
        db.kv_set(key, "retry@" + stamp())
        return
    db.kv_set(key, code or "unstated")
    if code and code != (watch.get("currency") or "USD").upper():
        db.update_watch(watch["id"], currency=code)
        watch["currency"] = code
        log.info("watch %s is priced in %s", watch["id"], code)


async def check_watch(watch: dict) -> bool:
    """Run one watch end to end: fetch, decide, persist, notify.

    Returns whether the check itself succeeded — not whether the item is in
    stock. The caller uses this to detect a systemic outage.
    """
    async with _gate:
        # Before the check, so the very first sweep is already valued in the
        # right currency rather than one sweep later.
        await learn_currency(watch)
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
        window_start=_sweep_window(watch),
        prev_availability=db.get_availability(watch),
        wanted=db.get_filter(watch),
        currency=watch.get("currency") or "USD",
    )

    interval = next_interval(
        watch.get("base_interval_s") or 300,
        watch.get("hot_interval_s") or 45,
        (parse(watch.get("hot_until")).timestamp()
         if is_future(watch.get("hot_until")) else None),
        utcnow().timestamp(),
    )

    # The controller learns `base_interval_s` — the watch's normal cadence.
    # It must never learn from the armed interval: while armed, `interval` is
    # the hot tier the person deliberately asked for, and writing that back
    # would make a temporary arming permanent.
    base = watch.get("base_interval_s") or 300
    armed = is_future(watch.get("hot_until"))

    if result.rate_limited:
        # Ease off, but proportionately. Doubling the interval turned one 429
        # into a ten-minute blind spot at the five-minute tier, which is how a
        # drop got missed by sixteen minutes — the store asked us to slow down,
        # not to stop. Retry-After wins when the store states its terms.
        learned = adapt_interval(base, rate_limited=True,
                                 retry_after=decision.defer_seconds)
        wait = max(learned, interval) if armed else learned
        db.update_watch(watch["id"], last_checked_at=stamp(),
                        base_interval_s=learned,
                        next_check_at=stamp_in(jittered(wait)),
                        last_error=f"rate limited — backing off to {int(wait)}s")
        db.record_check(watch["id"], ok=False, state=None, http_status=429,
                        latency_ms=None, error="HTTP 429 — rate limited")
        log.warning("watch %s rate limited; interval now %ds", watch["id"], int(wait))
        return result.ok

    # A clean check earns a little speed back, so a watch that was slowed by a
    # bad afternoon recovers on its own instead of staying slow forever.
    learned = adapt_interval(base, rate_limited=False)
    # While armed, keep polling at the armed cadence; only the learned base moves.
    wait = interval if armed else learned

    updates = {
        "last_state": decision.state,
        "consecutive_failures": decision.failures,
        "last_price": decision.price,
        "last_checked_at": stamp(),
        "base_interval_s": learned,
        "next_check_at": stamp_in(jittered(wait)),
        "last_error": result.error,
    }
    if decision.baseline is not None:
        updates["baseline_json"] = json.dumps(decision.baseline)
    if decision.availability is not None:
        updates["availability_json"] = json.dumps(decision.availability)
    # Only a sweep that actually READ the catalogue moves this, which is why it
    # is gated on result.ok rather than on the baseline: a failed check carries
    # the previous baseline through unchanged, so keying off that would stamp a
    # fresh sweep time on every failure and close the window over the very
    # drops the outage made us miss. A 304 counts — the validator matching is
    # the store confirming nothing changed.
    if (result.ok and (watch.get("kind") or "product") == "collection"
            and not result.extra.get("truncated")):
        # A truncated read is not a sweep. We stopped early — our own page cap,
        # or a store that asked us to slow down — so part of the catalogue was
        # never looked at. Stamping a fresh sweep would close the window over
        # exactly the products we failed to reach, and they would read as old
        # whenever they finally came into view. Leaving it be costs nothing:
        # the next complete read covers the gap.
        updates["last_sweep_at"] = stamp()
        seen = result.extra.get("product_count")
        if seen is not None:
            updates["last_seen_count"] = seen
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


def start() -> AsyncIOScheduler | None:
    """Begin polling, unless this process exists only to render the page.

    A layout check starts the whole app so the dashboard is real, and the
    whole app polls stores. Run from CI on every push that meant a GitHub
    runner making live requests to the shops this monitor watches — traffic
    the owner did not ask for, from an address they cannot vouch for, against
    hosts that rate-limit. A screenshot is not a reason to touch anyone's
    server.
    """
    global _scheduler
    if os.environ.get("MONITOR_NO_SCHEDULER"):
        log.info("scheduler disabled (MONITOR_NO_SCHEDULER)")
        return None
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
