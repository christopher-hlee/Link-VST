"""Platform sniffing — powers the one-click add flow."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import strategies

router = APIRouter()


class DetectRequest(BaseModel):
    url: str


@router.post("/detect")
async def detect(body: DetectRequest):
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")

    found = await strategies.detect(url)
    if not found:
        raise HTTPException(
            422,
            "No supported platform detected. Shopify stores are identified by "
            "probing /products.json; the site may be gating it or running "
            "something else.",
        )
    return found
