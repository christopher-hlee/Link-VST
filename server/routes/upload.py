from fastapi import APIRouter, UploadFile, File, HTTPException
from .. import db
from ..core.midi_analyzer import analyze

router = APIRouter()


@router.post("/upload-midi")
async def upload_midi(file: UploadFile = File(...)):
    if not (file.filename.endswith(".mid") or file.filename.endswith(".midi")):
        raise HTTPException(400, "File must be .mid or .midi")

    midi_bytes = await file.read()
    if len(midi_bytes) > 5 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 5MB)")

    try:
        features = analyze(midi_bytes)
    except Exception as e:
        raise HTTPException(422, f"Could not analyze MIDI: {e}")

    # Store for taste-profile training
    upload_id = db.insert_upload(file.filename, midi_bytes, features.model_dump())

    # Also add to library so it's playable and draggable
    library_id = db.insert_library(
        filename=file.filename,
        midi_bytes=midi_bytes,
        phrase={
            "phrase_type": features.phrase_type,
            "key":         features.key,
            "mode":        features.mode,
            "tempo_bpm":   features.tempo_bpm,
            "bars":        features.bars,
            "description": f"Uploaded: {file.filename}",
            "notes":       [],
        },
        source="uploaded",
    )

    return {
        "id":          upload_id,
        "library_id":  library_id,
        "filename":    file.filename,
        "features":    features.model_dump(),
        "message":     "Upload analyzed. Taste profile updated.",
    }
