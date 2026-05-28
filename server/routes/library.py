import base64
from fastapi import APIRouter, HTTPException
from .. import db

router = APIRouter()


@router.get("/library")
def list_library():
    items = db.list_library()
    return {"items": items, "count": len(items)}


@router.get("/library/{item_id}/midi")
def download_midi(item_id: int):
    item = db.get_library_item(item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    return {
        "id": item_id,
        "filename": item["filename"],
        "midi_b64": base64.b64encode(item["midi_bytes"]).decode(),
    }


@router.post("/library/{item_id}/save")
def save_to_library(item_id: int):
    """Re-confirm a generated phrase into the library (already inserted on generate)."""
    item = db.get_library_item(item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    return {"message": "Already in library", "id": item_id}


@router.delete("/library/{item_id}")
def delete_from_library(item_id: int):
    deleted = db.delete_library_item(item_id)
    if not deleted:
        raise HTTPException(404, "Item not found")
    return {"message": "Deleted", "id": item_id}
