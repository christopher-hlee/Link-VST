import base64
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from .. import db
from ..core.taste_profile import build_profile
from ..core.claude_client import generate_batch, generate_phrase
from ..core.midi_renderer import phrase_to_midi

router = APIRouter()


class GenerateRequest(BaseModel):
    count: int = 4
    phrase_type: str | None = None  # override; None = use profile preference
    key: str | None = None
    mode: str | None = None
    bars: int = 4
    hint: str = ""
    variety: bool = True


@router.post("/generate")
async def generate(req: GenerateRequest):
    features = db.get_all_features()
    exemplars = db.get_exemplar_notes()
    profile = build_profile(features, exemplars)

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
    except Exception as e:
        raise HTTPException(500, f"Generation failed: {e}")

    results = []
    for phrase in phrases:
        midi_bytes = phrase_to_midi(phrase)
        results.append({
            "phrase_type": phrase.phrase_type,
            "key": phrase.key,
            "mode": phrase.mode,
            "tempo_bpm": phrase.tempo_bpm,
            "bars": phrase.bars,
            "description": phrase.description,
            "midi_b64": base64.b64encode(midi_bytes).decode(),
        })

    return {
        "phrases": results,
        "profile_summary": {
            "total_uploads": profile.total_uploads,
            "preferred_keys": profile.preferred_keys,
            "preferred_modes": profile.preferred_modes,
            "preferred_types": profile.preferred_phrase_types,
        },
    }
