"""Alert feed and notification self-test."""
from fastapi import APIRouter, HTTPException, Query

from .. import db
from ..notify import telegram

router = APIRouter()


@router.get("/events")
def list_events(limit: int = Query(default=100, ge=1, le=500)):
    return {"events": db.list_events(limit=limit)}


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
