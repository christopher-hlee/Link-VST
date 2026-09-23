"""Alert feed and notification self-test."""
from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..notify import telegram

router = APIRouter()


@router.get("/events")
def list_events(limit: int = Query(default=100, ge=1, le=500)):
    return {"events": db.list_events(limit=limit)}


@router.delete("/events/{event_id}")
def delete_event(event_id: int):
    """Dismiss one alert for good.

    Server-side rather than a browser flag: the dashboard gets read on a phone
    and on a laptop, and an alert cleared on one should be gone on the other.
    """
    if not db.delete_event(event_id):
        raise HTTPException(404, "No such event")
    return {"ok": True}


@router.delete("/events/{event_id}/items/{handle:path}")
def dismiss_item(event_id: int, handle: str):
    """Dismiss one product from an alert that found several.

    The dashboard lists products, not alerts, so this is what its per-item
    delete means; dismissing the whole event would take the other products
    found in the same sweep with it.
    """
    outcome = db.drop_event_item(event_id, handle)
    if outcome is None:
        raise HTTPException(404, "No such item in that alert")
    return {"ok": True, "event": outcome}


@router.delete("/events")
def clear_events():
    return {"ok": True, "deleted": db.clear_events()}


@router.post("/test-alert")
async def test_alert():
    """Prove the Telegram path works before relying on it."""
    if not telegram.configured():
        raise HTTPException(
            503,
            "Telegram is not configured. Set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID in monitor/.env.",
        )
    try:
        await telegram.send_text(
            "✅ <b>Restock monitor</b>\nTelegram is wired up correctly."
        )
    except Exception as exc:
        raise HTTPException(502, f"Telegram rejected the message: {exc}")
    return {"ok": True}
