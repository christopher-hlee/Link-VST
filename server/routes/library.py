import io
import zipfile
import base64
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from .. import db

router = APIRouter()


@router.get("/library/export")
def export_library():
    """Download all library MIDI files as a zip archive."""
    items = db.list_library_with_midi()
    if not items:
        raise HTTPException(404, "Library is empty")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            raw = item.get("midi_bytes")
            if not raw:
                continue
            safe_key  = (item.get("key")  or "X").replace("/", "-").replace("#", "s")
            safe_mode = (item.get("mode") or "unknown")
            safe_type = (item.get("phrase_type") or "phrase").replace("_", "-")
            fname = f"{safe_key}_{safe_mode}_{safe_type}_id{item['id']}.mid"
            zf.writestr(fname, bytes(raw))

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=linkvst_library.zip"},
    )


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
        "id":       item_id,
        "filename": item["filename"],
        "midi_b64": base64.b64encode(item["midi_bytes"]).decode(),
    }


@router.post("/library/{item_id}/save")
def save_to_library(item_id: int):
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
