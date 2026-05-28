from fastapi import APIRouter
from ..core.presets import PRESETS

router = APIRouter()


@router.get("/presets")
def get_presets():
    return {"presets": PRESETS}
