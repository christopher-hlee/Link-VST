import json as _json
import base64
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .. import db
from ..core.taste_profile import build_profile
from ..core.claude_client import generate_batch, generate_phrase
from ..core.midi_renderer import phrase_to_midi
from ..core.humanizer import humanize
from ..core.presets import PRESET_MAP

router = APIRouter()


class GenerateRequest(BaseModel):
    count:      int   = 4
    phrase_type: str | None = None
    key:        str | None = None
    mode:       str | None = None
    bars:       int   = 4
    hint:       str   = ""
    variety:    bool  = True
    # Humanization
    swing:             float = Field(0.0, ge=0.0, le=1.0)
    velocity_variance: int   = Field(0,   ge=0,   le=30)
    timing_variance:   float = Field(0.0, ge=0.0, le=0.05)
    # Optional preset ID to merge params from
    preset: str | None = None


def _merge_preset(req: GenerateRequest) -> GenerateRequest:
    """If preset is set, fill in any None fields from preset defaults."""
    if not req.preset or req.preset not in PRESET_MAP:
        return req
    p = PRESET_MAP[req.preset]
    data = req.model_dump()
    if data["phrase_type"] is None: data["phrase_type"] = p["phrase_type"]
    if data["key"]         is None: data["key"]         = p["key"]
    if data["mode"]        is None: data["mode"]        = p["mode"]
    if data["bars"]        == 4:    data["bars"]        = p.get("bars", 4)
    if not data["hint"]:            data["hint"]        = p.get("hint", "")
    return GenerateRequest(**data)


@router.post("/generate")
async def generate(req: GenerateRequest):
    req = _merge_preset(req)

    features  = db.get_all_features()
    exemplars = db.get_exemplar_notes()
    profile   = build_profile(features, exemplars)

    try:
        if req.count == 1:
            phrases = [generate_phrase(
                profile,
                phrase_type=req.phrase_type,
                key=req.key,
                mode=req.mode,
                bars=req.bars,
                extra_hint=req.hint,
            )]
        else:
            phrases = generate_batch(profile, count=req.count, variety=req.variety)
            # Override phrase-level key/type if explicitly set
            if req.phrase_type or req.key or req.mode:
                from ..core.claude_client import generate_phrase as gp
                phrases = [gp(profile, phrase_type=req.phrase_type, key=req.key,
                               mode=req.mode, bars=req.bars, extra_hint=req.hint)
                           for _ in range(req.count)]
    except Exception as e:
        raise HTTPException(500, f"Generation failed: {e}")

    results = []
    for phrase in phrases:
        # Apply humanization
        phrase = humanize(phrase,
                          swing=req.swing,
                          velocity_variance=req.velocity_variance,
                          timing_variance=req.timing_variance)

        midi_bytes = phrase_to_midi(phrase)
        filename   = f"{phrase.key}_{phrase.mode}_{phrase.phrase_type}.mid"

        library_id = db.insert_library(
            filename=filename,
            midi_bytes=midi_bytes,
            phrase=phrase.model_dump(),
            source="generated",
        )

        results.append({
            "id":          library_id,
            "phrase_type": phrase.phrase_type,
            "key":         phrase.key,
            "mode":        phrase.mode,
            "tempo_bpm":   phrase.tempo_bpm,
            "bars":        phrase.bars,
            "description": phrase.description,
            "midi_b64":    base64.b64encode(midi_bytes).decode(),
        })

    return {
        "phrases": results,
        "profile_summary": {
            "total_uploads":   profile.total_uploads,
            "preferred_keys":  profile.preferred_keys,
            "preferred_modes": profile.preferred_modes,
            "preferred_types": profile.preferred_phrase_types,
        },
    }


@router.post("/generate/variation/{library_id}")
async def generate_variation(library_id: int):
    """Generate 2 fresh phrases inspired by an existing library item."""
    item = db.get_library_item(library_id)
    if not item:
        raise HTTPException(404, "Library item not found")

    features  = db.get_all_features()
    exemplars = db.get_exemplar_notes()
    profile   = build_profile(features, exemplars)

    # Inject the original phrase as the sole exemplar so Claude studies it
    if item.get("notes_json"):
        notes = _json.loads(item["notes_json"])
        profile = profile.model_copy(update={"exemplar_phrases": [{
            "phrase_type": item["phrase_type"],
            "key":         item["key"],
            "mode":        item["mode"],
            "notes":       notes,
        }]})

    hint = (
        f"Create a fresh variation — same key/mode/character as: \"{item['description']}\". "
        "Write completely different notes and rhythms while keeping the same emotional feel."
    )

    try:
        phrases = [
            generate_phrase(profile,
                phrase_type=item["phrase_type"],
                key=item["key"],
                mode=item["mode"],
                bars=item["bars"] or 4,
                extra_hint=hint)
            for _ in range(2)
        ]
    except Exception as e:
        raise HTTPException(500, f"Variation generation failed: {e}")

    results = []
    for phrase in phrases:
        midi_bytes = phrase_to_midi(phrase)
        filename   = f"{phrase.key}_{phrase.mode}_{phrase.phrase_type}_var.mid"
        lid = db.insert_library(filename=filename, midi_bytes=midi_bytes,
                                phrase=phrase.model_dump(), source="generated")
        results.append({
            "id":          lid,
            "phrase_type": phrase.phrase_type,
            "key":         phrase.key,
            "mode":        phrase.mode,
            "tempo_bpm":   phrase.tempo_bpm,
            "bars":        phrase.bars,
            "description": phrase.description,
            "midi_b64":    base64.b64encode(midi_bytes).decode(),
        })

    return {"phrases": results}
