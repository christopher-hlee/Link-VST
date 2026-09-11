"""Pure state-transition logic. No I/O — this is the part that must be right.

The single most important rule: a failed check NEVER changes the recorded stock
state. A 403, a timeout, or a parse failure means "we don't know", not "sold
out". Conflating those is how monitors silently stop working while looking fine.
"""
from dataclasses import dataclass, field
from typing import Any, Literal

from .timeutil import parse, utcnow

# Stock states. `error` is deliberately NOT one of them — see module docstring.
UNKNOWN = "unknown"
IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"

# In stock, healthy, and correctly silent: what returned is not the size being
# watched. Held is deliberately NOT a variant of sold out. Collapsing the two
# would make a working watch indistinguishable from a dead one, which is the
# failure this whole state machine exists to prevent.
HELD = "held"

# A drop watch has no stock state of its own — it is watching a catalogue.
WATCHING = "watching"

# Event kinds
RESTOCK = "restock"
NEW_PRODUCT = "new_product"
HELD_NOTE = "held_note"
PRICE_DROP = "price_drop"
WATCH_FAILING = "watch_failing"
WATCH_RECOVERED = "watch_recovered"

WatchKind = Literal["product", "collection"]


@dataclass
class CheckResult:
    """Outcome of one fetch+parse attempt."""
    ok: bool
    state: str | None = None            # IN_STOCK / OUT_OF_STOCK; None when not ok
    price: float | None = None
    title: str | None = None
    product_url: str | None = None
    cart_url: str | None = None
    image: str | None = None
    handles: list[str] | None = None    # collection watches only
    http_status: int | None = None
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False          # HTTP 304 — nothing changed
    # The host asked us to slow down. Not a broken watch, and emphatically not
    # grounds for pausing one: complying means polling less, not stopping.
    rate_limited: bool = False
    retry_after: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Event:
    kind: str
    from_state: str | None = None
    to_state: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """What the caller should persist and send."""
    state: str                          # new last_state (unchanged on failure)
    failures: int
    events: list[Event] = field(default_factory=list)
    baseline: list[str] | None = None   # new handle baseline, collection watches
    price: float | None = None
    pause: bool = False                 # stop polling: too many failures in a row
    defer_seconds: float | None = None  # push the next poll out this far


def decide(
    *,
    kind: WatchKind,
    prev_state: str,
    prev_failures: int,
    prev_baseline: list[str] | None,
    prev_price: float | None,
    result: CheckResult,
    failure_threshold: int = 5,
    window_start=None,
) -> Decision:
    """Fold a check result into a new persisted state plus any events to fire."""
    if result.rate_limited:
        # Change nothing. A rate limit says nothing about stock, nothing about
        # the watch's health, and counting it as a failure would eventually
        # pause a perfectly good watch for the crime of being polled too often
        # — a self-inflicted outage from a recoverable condition.
        return Decision(state=prev_state, failures=prev_failures,
                        baseline=prev_baseline, price=prev_price,
                        defer_seconds=result.retry_after)

    if not result.ok:
        return _decide_failure(prev_state, prev_failures, prev_baseline,
                               prev_price, result, failure_threshold)

    events: list[Event] = []

    # A watch that was in the failing state and now works is worth reporting —
    # otherwise you never learn that the gap in coverage ended.
    if prev_failures >= failure_threshold:
        events.append(Event(kind=WATCH_RECOVERED, payload={
            "after_failures": prev_failures,
        }))

    # HTTP 304: the server told us nothing changed. Keep everything as-is.
    if result.not_modified:
        return Decision(state=prev_state, failures=0, events=events,
                        baseline=prev_baseline, price=prev_price)

    if kind == "collection":
        return _decide_collection(prev_state, prev_baseline, result, events,
                                  window_start)

    return _decide_product(prev_state, prev_price, result, events)


def _decide_failure(prev_state, prev_failures, prev_baseline, prev_price,
                    result, failure_threshold) -> Decision:
    failures = prev_failures + 1
    events: list[Event] = []
    # Alert exactly once, on the crossing, so a persistently broken watch does
    # not generate an alert every single cycle. At that point the watch is also
    # paused: a watch that has failed this many times consecutively is not
    # watching anything, and quietly retrying forever hides that.
    pause = failures >= failure_threshold
    if failures == failure_threshold:
        events.append(Event(kind=WATCH_FAILING, payload={
            "failures": failures,
            "error": result.error,
            "http_status": result.http_status,
            "paused": True,
        }))
    # prev_state is preserved verbatim. This is the rule that matters.
    return Decision(state=prev_state, failures=failures, events=events,
                    baseline=prev_baseline, price=prev_price, pause=pause)


def _decide_product(prev_state, prev_price, result, events) -> Decision:
    new_state = result.state or UNKNOWN

    # Only a genuine transition into stock is a restock. Coming from UNKNOWN just
    # establishes the baseline: adding a watch for something already in stock
    # should not immediately ping you. HELD counts as a source, because the
    # preferred size arriving later is exactly the moment worth waking for.
    if prev_state in (OUT_OF_STOCK, HELD) and new_state == IN_STOCK:
        events.append(Event(kind=RESTOCK, from_state=prev_state,
                            to_state=new_state, payload=_payload(result)))

    # Something came back, but not in the watched size. Say so once, as a note
    # rather than an alert — silence with a stated reason, so the person never
    # has to wonder whether the watch is broken. Never repeats while held holds.
    if prev_state == OUT_OF_STOCK and new_state == HELD:
        events.append(Event(kind=HELD_NOTE, from_state=prev_state,
                            to_state=new_state, payload=_payload(result)))

    if (prev_price is not None and result.price is not None
            and result.price < prev_price):
        events.append(Event(kind=PRICE_DROP, from_state=prev_state,
                            to_state=new_state,
                            payload={**_payload(result),
                                     "old_price": prev_price,
                                     "new_price": result.price}))

    return Decision(state=new_state, failures=0, events=events,
                    baseline=None,
                    price=result.price if result.price is not None else prev_price)


def _decide_collection(prev_state, prev_baseline, result, events,
                       window_start=None) -> Decision:
    handles = list(result.handles or [])

    # First successful check just records what's there.
    if prev_baseline is None:
        return Decision(state=WATCHING, failures=0, events=events,
                        baseline=handles)

    known = set(prev_baseline)
    unseen = [h for h in handles if h not in known]

    # The baseline is a dedupe ledger, not the trigger. Being absent from it
    # only means we have not seen the product; whether the STORE considers it
    # new is a separate question, and the only one worth alerting on.
    items = (result.extra or {}).get("items") or {}
    now = utcnow()
    start = window_start if window_start is not None else now

    buckets: dict[str, list[str]] = {}
    for handle in unseen:
        arrival = classify_arrival(items.get(handle) or {},
                                   window_start=start, now=now)
        buckets.setdefault(arrival, []).append(handle)

    # ARRIVAL_KNOWN is absorbed in silence: it was already on the shelf, we
    # just had not looked at it. That single line is the whole fix.
    for kind in (ARRIVAL_NEW, ARRIVAL_RELISTED, ARRIVAL_UNCONFIRMED):
        fresh = buckets.get(kind)
        if not fresh:
            continue
        payload = {**_prune(_payload(result), fresh),
                   "handles": fresh,
                   "arrival": kind,
                   # What the catalogue held before this sweep, so the alert
                   # can say "214 -> 217".
                   "baseline_count": len(known)}
        lag = _detection_lag(payload, fresh)
        if lag is not None:
            payload["listed_ago_s"] = lag
        events.append(Event(kind=NEW_PRODUCT, from_state=prev_state,
                            to_state=WATCHING, payload=payload))

    # Union, so a product briefly dropping out of the feed doesn't re-alert
    # when it comes back.
    return Decision(state=WATCHING, failures=0, events=events,
                    baseline=sorted(known | set(handles)))


# How a product that is new *to us* relates to the store's own record of it.
ARRIVAL_NEW = "new"                # the store published it since we last looked
ARRIVAL_RELISTED = "relisted"      # published since, but created long ago
ARRIVAL_UNCONFIRMED = "unconfirmed"  # the store gave us no dates to judge by
ARRIVAL_KNOWN = "known"            # already on the shelf; we simply hadn't seen it

# A brand may stage a product days before a launch, so created_at being older
# than published_at is normal and not evidence of a relist. Only a gap this
# large means the item genuinely existed in a previous life.
RELIST_GAP_S = 30 * 86400


def classify_arrival(item: dict, *, window_start, now) -> str:
    """Why is this product new to us — because the store published it, or
    because we only just looked in the right place?

    The bug this exists to kill: "new" used to mean "not in our baseline",
    which is a fact about our own memory and nothing to do with the store. A
    catalogue bigger than one page, a collection that re-sorts, or a widened
    sweep all put previously-unseen products in front of us, and every one of
    them fired an alert. One such item had been on sale for 329 days.
    """
    published = parse(item.get("published_at"))
    created = parse(item.get("created_at"))

    if published is None and created is None:
        # Say so rather than guess. An alert that admits it could not verify is
        # worth more than one that quietly pretends.
        return ARRIVAL_UNCONFIRMED

    appeared = published or created
    if appeared < window_start:
        return ARRIVAL_KNOWN

    if created is not None and published is not None:
        if (published - created).total_seconds() > RELIST_GAP_S:
            # Shopify resets published_at when an item is unpublished and put
            # back. created_at does not move, so this gap is the only thing
            # separating a genuine release from an old item returning.
            return ARRIVAL_RELISTED

    return ARRIVAL_NEW


def _detection_lag(payload: dict, fresh: list[str]) -> int | None:
    """Seconds between the store listing the item and us noticing.

    This is the number that decomposes "the alert was late" into our polling
    lag versus a stale CDN document. Returns None when the store gives no
    usable timestamp — an unknown lag must read as unknown, never as zero.
    """
    from datetime import datetime, timezone

    items = payload.get("items") or {}
    newest = None
    for handle in fresh:
        raw = (items.get(handle) or {}).get("published_at")
        if not isinstance(raw, str):
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if newest is None or when > newest:
            newest = when
    if newest is None:
        return None
    lag = (datetime.now(timezone.utc) - newest).total_seconds()
    # A future timestamp means clock skew, not a negative lag.
    return int(lag) if lag >= 0 else None


# Per-handle maps a strategy returns for the whole catalogue. Only the handles
# that actually fired belong in the stored event.
_PER_HANDLE_KEYS = ("titles", "links", "items")


def _prune(payload: dict[str, Any], fresh: list[str]) -> dict[str, Any]:
    """Narrow the catalogue-wide maps to the handles this event is about.

    A collection sweep carries a title, link and item record for all 250-odd
    products. Storing that whole catalogue on every alert bloats the row by two
    orders of magnitude to say something about two items.
    """
    keep = set(fresh)
    return {
        key: ({h: v for h, v in value.items() if h in keep}
              if key in _PER_HANDLE_KEYS and isinstance(value, dict) else value)
        for key, value in payload.items()
    }


def _payload(result: CheckResult) -> dict[str, Any]:
    return {
        "title": result.title,
        "price": result.price,
        "product_url": result.product_url,
        "cart_url": result.cart_url,
        "image": result.image,
        # extra carries per-variant offers, so the notifier can build one cart
        # button per size instead of choosing a variant on the person's behalf.
        **result.extra,
    }


def next_interval(base_s: int, hot_s: int, hot_until_ts: float | None,
                  now_ts: float) -> int:
    """Hot tier while armed, base tier otherwise."""
    if hot_until_ts is not None and hot_until_ts > now_ts:
        return hot_s
    return base_s


# Bounds for the learned polling interval. The floor is the existing hot tier,
# which is the fastest this project has ever asked a store for; the ceiling is
# the slow tier, so a hostile afternoon can never silence a watch entirely.
INTERVAL_FLOOR = 45
INTERVAL_CEILING = 900
SPEEDUP_STEP = 15          # additive increase in rate, per clean check
SLOWDOWN_FACTOR = 1.5      # multiplicative decrease on a 429
# Multiplying an already-slow interval overshoots: 1.5x on five minutes is
# another four and a half minutes blind, which is the problem this set out to
# fix rather than a fix for it. Cap what a single 429 may cost.
MAX_BACKOFF_STEP = 120


def adapt_interval(current_s: int, *, rate_limited: bool,
                   retry_after: float | None = None,
                   floor_s: int = INTERVAL_FLOOR,
                   ceiling_s: int = INTERVAL_CEILING) -> int:
    """Poll as fast as this particular store tolerates.

    Additive-increase / multiplicative-decrease, the same shape TCP uses for
    congestion. Every clean check earns a little more speed; a 429 gives a lot
    of it back at once, because being wrong in the fast direction gets the watch
    banned while being wrong in the slow direction only costs latency.

    This exists because fixed tiers force a guess that is wrong for some store.
    Hardcoding 45s is what triggered the rate-limit storm that auto-paused a
    healthy watch; leaving it at five minutes is how a drop is missed by sixteen
    minutes. A controller measures instead of guessing.
    """
    current = max(int(current_s or floor_s), 1)

    if rate_limited:
        # Retry-After is the store stating its terms. Prefer it over our guess,
        # but never let it drop us below the interval we were already using.
        if retry_after and retry_after > 0:
            proposed = max(int(retry_after), current)
        else:
            proposed = min(int(current * SLOWDOWN_FACTOR),
                           current + MAX_BACKOFF_STEP)
    else:
        proposed = current - SPEEDUP_STEP

    return max(floor_s, min(ceiling_s, proposed))
