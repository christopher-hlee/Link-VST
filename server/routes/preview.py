from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from .. import db
from ..core.audio_renderer import render_to_wav

router = APIRouter()


@router.get("/preview/{item_id}")
async def preview(item_id: int):
    """Render a library phrase to WAV audio for playback."""
    item = db.get_library_item(item_id)
    if not item:
        raise HTTPException(404, "Phrase not found")

    midi_bytes = item.get("midi_bytes")
    if not midi_bytes:
        raise HTTPException(422, "No MIDI data stored for this phrase")

    try:
        wav_bytes = render_to_wav(bytes(midi_bytes))
    except Exception as e:
        raise HTTPException(500, f"Audio render failed: {e}")

    return Response(
        content=wav_bytes,
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=3600"},
    )
