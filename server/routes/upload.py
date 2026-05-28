from fastapi import APIRouter, UploadFile, File, HTTPException
from .. import db
from ..core.midi_analyzer import analyze

router = APIRouter()


@router.post("/upload-midi")
async def upload_midi(file: UploadFile = File(...)):
    if not file.filename.endswith(".mid") and not file.filename.endswith(".midi"):
        raise HTTPException(400, "File must be a .mid or .midi file")

    midi_bytes = await file.read()
    if len(midi_bytes) > 5 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 5MB)")

    try:
        features = analyze(midi_bytes)
    except Exception as e:
        raise HTTPException(422, f"Could not analyze MIDI: {e}")

    upload_id = db.insert_upload(file.filename, midi_bytes, features.model_dump())

    return {
        "id": upload_id,
        "filename": file.filename,
        "features": features.model_dump(),
        "message": "Upload analyzed and stored. Taste profile updated.",
    }
