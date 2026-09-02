"""Watch CRUD, arming, and forced checks."""
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, scheduler, strategies
from ..config import (BESTBUY_API_KEY, INTERVAL_BASE, INTERVAL_HOT,
                      INTERVAL_SLOW)
from ..statemachine import UNKNOWN
from ..timeutil import EPOCH, stamp_in

router = APIRouter()

TIERS = {"slow": INTERVAL_SLOW, "base": INTERVAL_BASE, "fast": INTERVAL_HOT}


class WatchCreate(BaseModel):
    url: str
    name: str | None = None
    brand: str | None = None
    strategy: str | None = None
    kind: str | None = None
    target_ref: str | None = None
    store_ref: str | None = None
    size_pref: str | None = None
    tier: str = "base"
    alert_level: str = "info"


class WatchUpdate(BaseModel):
    name: str | None = None
    brand: str | None = None
    url: str | None = None
    target_ref: str | None = None      # keywords for announce, handle otherwise
    store_ref: str | None = None
    size_pref: str | None = None
    tier: str | None = None
    alert_level: str | None = None
    enabled: bool | None = None


# Changing any of these changes what the watch is looking at, which invalidates
# the baseline it has been comparing against.
REBASELINE_FIELDS = {"url", "target_ref"}
# Changing these changes what counts as a match, so the current state is stale.
RECHECK_FIELDS = REBASELINE_FIELDS | {"size_pref"}


class ArmRequest(BaseModel):
    minutes: int = Field(default=60, ge=1, le=60 * 24 * 7)


def _decorate(watch: dict) -> dict:
    """Attach derived fields the dashboard needs to render a row."""
    offers = db.get_offers(watch)
    watch = dict(watch)
    watch["offers"] = offers
    prefs = {p.strip().lower() for p in (watch.get("size_pref") or "").split(",") if p.strip()}
    watch["matched_offers"] = [o for o in offers if o.get("preferred")] if prefs else []
    watch["available_sizes"] = [o.get("title") for o in offers if o.get("title")]
    watch["paused"] = not watch.get("enabled")
    return watch


@router.get("/watches")
def list_watches():
    return {"watches": [_decorate(w) for w in db.list_watches()],
            "summary": db.summary()}


@router.get("/watches/{watch_id}")
def get_watch(watch_id: int):
    watch = db.get_watch(watch_id)
    if not watch:
        raise HTTPException(404, "No such watch")
    return {"watch": watch, "checks": db.list_checks(watch_id, limit=100)}


def _host(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "") or url


def _fallback_name(url: str, target_ref: str | None) -> str:
    """A readable name for a watch that never went through detection."""
    host = _host(url)
    return f"{host} · {target_ref}" if target_ref else host


@router.post("/watches", status_code=201)
async def create_watch(body: WatchCreate):
    fields = body.model_dump(exclude_none=True)
    tier = fields.pop("tier", "base")
    if tier not in TIERS:
        raise HTTPException(400, f"tier must be one of {sorted(TIERS)}")

    # Sniff the platform unless the caller pinned one explicitly.
    if not body.strategy:
        detected = await strategies.detect(body.url)
        if not detected:
            raise HTTPException(
                422,
                "Could not identify this site. It may not be Shopify, or it may "
                "be blocking automated requests. Pass `strategy` explicitly to "
                "override.",
            )
        for key in ("strategy", "kind", "name", "brand", "target_ref"):
            fields.setdefault(key, detected.get(key))

    # A missing key is not a transient failure, so let it fail here rather than
    # five checks later when the watch auto-pauses and looks broken instead of
    # unconfigured.
    if fields.get("strategy") == "bestbuy" and not BESTBUY_API_KEY:
        raise HTTPException(
            422,
            "Best Buy needs a free API key. Get one at https://developer.bestbuy.com, "
            "add BESTBUY_API_KEY to monitor/.env, and restart the service.",
        )

    fields.setdefault("kind", "product")
    # Pinning `strategy` skips detection, which is where a name normally comes
    # from — so a feed watch would otherwise render its raw URL as its title.
    fields.setdefault("brand", _host(body.url))
    fields.setdefault("name", _fallback_name(body.url, fields.get("target_ref")))
    fields["base_interval_s"] = TIERS[tier]
    fields["next_check_at"] = EPOCH

    watch_id = db.create_watch(**fields)
    return {"watch": db.get_watch(watch_id)}


@router.patch("/watches/{watch_id}")
def update_watch(watch_id: int, body: WatchUpdate):
    current = db.get_watch(watch_id)
    if not current:
        raise HTTPException(404, "No such watch")

    # exclude_unset, not exclude_none: clearing a size preference back to "any
    # size" means sending null, and exclude_none would silently drop it.
    fields = body.model_dump(exclude_unset=True)

    tier = fields.pop("tier", None)
    if tier is not None:
        if tier not in TIERS:
            raise HTTPException(400, f"tier must be one of {sorted(TIERS)}")
        fields["base_interval_s"] = TIERS[tier]
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
        if fields["enabled"]:
            # Resuming a watch that auto-paused after repeated failures has to
            # clear the counter, or it re-pauses on the very next tick.
            fields["consecutive_failures"] = 0
    if fields.get("url") is not None and not str(fields["url"]).startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")

    changed = {k for k in fields if k in RECHECK_FIELDS
               and (fields[k] or None) != (current.get(k) or None)}

    if changed & REBASELINE_FIELDS:
        # The watch is now looking at something else. Comparing against the old
        # baseline would either miss real arrivals or, on a broadened keyword,
        # report every previously-ignored entry as new.
        fields["baseline_json"] = None
        fields["last_state"] = UNKNOWN
        fields["consecutive_failures"] = 0
        fields["last_error"] = None
    if changed:
        fields["next_check_at"] = EPOCH

    db.update_watch(watch_id, **fields)
    return {"watch": db.get_watch(watch_id), "rebaselined": bool(changed & REBASELINE_FIELDS)}


@router.delete("/watches/{watch_id}")
def delete_watch(watch_id: int):
    if not db.delete_watch(watch_id):
        raise HTTPException(404, "No such watch")
    return {"ok": True}


@router.post("/watches/{watch_id}/arm")
def arm(watch_id: int, body: ArmRequest):
    """Switch to the hot interval for a known drop window."""
    if not db.get_watch(watch_id):
        raise HTTPException(404, "No such watch")
    db.update_watch(watch_id, hot_until=stamp_in(body.minutes * 60),
                    next_check_at=EPOCH)
    return {"watch": db.get_watch(watch_id)}


@router.post("/watches/{watch_id}/disarm")
def disarm(watch_id: int):
    if not db.get_watch(watch_id):
        raise HTTPException(404, "No such watch")
    db.update_watch(watch_id, hot_until=None)
    return {"watch": db.get_watch(watch_id)}


@router.post("/watches/{watch_id}/check")
async def force_check(watch_id: int):
    watch = db.get_watch(watch_id)
    if not watch:
        raise HTTPException(404, "No such watch")
    await scheduler.check_watch(watch)
    return {"watch": db.get_watch(watch_id),
            "checks": db.list_checks(watch_id, limit=5)}
