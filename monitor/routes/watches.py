"""Watch CRUD, arming, and forced checks."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, scheduler, strategies
from ..config import INTERVAL_BASE, INTERVAL_HOT, INTERVAL_SLOW
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
    store_ref: str | None = None
    size_pref: str | None = None
    tier: str | None = None
    alert_level: str | None = None
    enabled: bool | None = None


class ArmRequest(BaseModel):
    minutes: int = Field(default=60, ge=1, le=60 * 24 * 7)


@router.get("/watches")
def list_watches():
    return {"watches": db.list_watches(), "summary": db.summary()}


@router.get("/watches/{watch_id}")
def get_watch(watch_id: int):
    watch = db.get_watch(watch_id)
    if not watch:
        raise HTTPException(404, "No such watch")
    return {"watch": watch, "checks": db.list_checks(watch_id, limit=100)}


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

    fields.setdefault("kind", "product")
    fields.setdefault("name", body.url)
    fields["base_interval_s"] = TIERS[tier]
    fields["next_check_at"] = EPOCH

    watch_id = db.create_watch(**fields)
    return {"watch": db.get_watch(watch_id)}


@router.patch("/watches/{watch_id}")
def update_watch(watch_id: int, body: WatchUpdate):
    if not db.get_watch(watch_id):
        raise HTTPException(404, "No such watch")

    fields = body.model_dump(exclude_none=True)
    tier = fields.pop("tier", None)
    if tier:
        if tier not in TIERS:
            raise HTTPException(400, f"tier must be one of {sorted(TIERS)}")
        fields["base_interval_s"] = TIERS[tier]
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0

    db.update_watch(watch_id, **fields)
    return {"watch": db.get_watch(watch_id)}


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
