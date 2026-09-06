"""Finding records: sources, preset digs, search, and pulling them down."""
from __future__ import annotations

import random
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .. import digs as digs_module
from .. import library, ripper
from ..sources.base import Lead, SourceError
from .deps import state
from .schemas import ImportIn, IngestIn, LeadIn, VerdictIn, YouTubeIn

router = APIRouter(prefix="/api", tags=["dig"])


def _lead_from(model: LeadIn) -> Lead:
    return Lead(**model.model_dump())


def _decorate(app_state, leads: list[Lead]) -> list[dict[str, Any]]:
    """Mark leads already in the crate so the UI can grey them out."""
    out = []
    for lead in leads:
        row = app_state.db.one(
            "SELECT id, status FROM samples WHERE source=? AND source_id=?",
            (lead.source, lead.source_id),
        )
        payload = lead.as_dict()
        payload["sample_id"] = row["id"] if row else None
        payload["sample_status"] = row["status"] if row else None
        out.append(payload)
    return out


@router.get("/sources")
async def list_sources(request: Request) -> dict:
    app_state = state(request)
    return {
        "sources": app_state.registry.describe(),
        "ripper": ripper.status(app_state.settings),
    }


@router.get("/digs")
async def list_digs() -> dict:
    return {"digs": [d.as_dict() for d in digs_module.DIGS]}


@router.get("/dig/{slug}")
async def run_dig(
    request: Request,
    slug: str,
    page: int | None = Query(default=None, ge=1),
    rows: int = Query(default=30, ge=1, le=100),
    hide_seen: bool = True,
    seed: int | None = None,
) -> dict:
    """Rummage through one seam.

    With no page given we jump to a random one — page 1 of any archive is the
    records everybody already owns.
    """
    dig = digs_module.get(slug)
    if not dig:
        raise HTTPException(404, f"No dig called {slug!r}")

    app_state = state(request)
    rng = random.Random(seed)
    chosen_page = page or digs_module.random_page(dig, rng)
    source = app_state.registry.get(dig.source)

    try:
        leads = await source.search(**{**dig.params, "rows": rows, "page": chosen_page})
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc

    if hide_seen:
        seen = app_state.db.seen_ids(dig.source)
        leads = [l for l in leads if l.source_id not in seen]

    return {
        "dig": dig.as_dict(),
        "page": chosen_page,
        "count": len(leads),
        "results": _decorate(app_state, leads),
    }


@router.get("/search")
async def search(
    request: Request,
    q: str = "",
    source: str = "ia",
    rows: int = Query(default=40, ge=1, le=100),
    page: int = Query(default=1, ge=1),
    year_from: int | None = None,
    year_to: int | None = None,
    collections: str | None = None,
    subjects: str | None = None,
    style: str | None = None,
    label: str | None = None,
    sort: str | None = None,
) -> dict:
    app_state = state(request)
    try:
        adapter = app_state.registry.get(source)
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc

    kwargs: dict[str, Any] = {"rows": rows, "page": page}
    if source == "ia":
        kwargs.update(
            year_from=year_from,
            year_to=year_to,
            collections=[c for c in (collections or "").split(",") if c],
            subjects=[s for s in (subjects or "").split(",") if s],
            sort=sort or "downloads desc",
        )
    elif source == "discogs":
        kwargs.update(style=style, label=label, year=str(year_from) if year_from else None)

    try:
        leads = await adapter.search(q, **kwargs)
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc

    return {"source": source, "page": page, "count": len(leads),
            "results": _decorate(app_state, leads)}


@router.get("/ia/item/{identifier:path}")
async def ia_tracks(request: Request, identifier: str) -> dict:
    """Expand one Archive item into its playable tracks."""
    app_state = state(request)
    try:
        leads = await app_state.registry.ia.tracks(identifier)
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"identifier": identifier, "count": len(leads),
            "results": _decorate(app_state, leads)}


@router.post("/verdict")
async def record_verdict(request: Request, body: VerdictIn) -> dict:
    state(request).db.record_verdict(body.source, body.source_id, body.verdict)
    return {"ok": True}


@router.post("/ingest")
async def ingest(request: Request, body: IngestIn) -> dict:
    """Queue one or more leads for download and analysis."""
    app_state = state(request)
    settings = app_state.settings
    submitted = []

    for model in body.leads:
        lead = _lead_from(model)

        async def run(lead: Lead = lead) -> dict:
            row = await library.ingest_lead(
                app_state.db,
                app_state.registry.client,
                lead,
                audio_dir=settings.audio_dir,
                max_mb=settings.max_download_mb,
                analyse=body.analyse,
            )
            return {"sample_id": row.get("id"), "status": row.get("status"),
                    "error": row.get("error")}

        job = app_state.jobs.submit(
            "ingest", lead.title or lead.source_id, run
        )
        app_state.db.record_verdict(lead.source, lead.source_id, "keep")
        submitted.append(job.as_dict())

    return {"jobs": submitted}


@router.post("/youtube")
async def add_youtube(request: Request, body: YouTubeIn) -> dict:
    """File a YouTube video as a lead, optionally pulling the audio down."""
    app_state = state(request)
    settings = app_state.settings
    try:
        lead = await app_state.registry.youtube.lead_for(body.url)
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc

    row = lead.as_sample_row()
    row["notes"] = body.note
    sample_id = app_state.db.upsert_sample(row)
    timestamp = lead.extra.get("timestamp")
    if timestamp:
        app_state.db.execute(
            "INSERT INTO markers(sample_id, kind, start_sec, label, created_at)"
            " VALUES (?,?,?,?,strftime('%s','now'))",
            (sample_id, "note", float(timestamp), "from URL timestamp"),
        )

    result: dict[str, Any] = {
        "sample_id": sample_id,
        "lead": lead.as_dict(),
        "ripped": False,
    }

    if body.rip:
        if not settings.enable_ripper:
            result["rip_error"] = (
                "The ripper is off. Set CRATE_ENABLE_RIPPER=true, or download "
                "the audio yourself and use Import."
            )
            return result

        async def run() -> dict:
            dest = library.sample_dir(settings.audio_dir, "youtube", lead.source_id)
            app_state.db.update_sample(sample_id, status="downloading", error=None)
            try:
                path = await ripper.rip(
                    settings, lead.page_url, dest,
                    stem=library.slugify(lead.title or lead.source_id),
                )
            except ripper.RipperError as exc:
                app_state.db.update_sample(sample_id, status="error", error=str(exc))
                raise
            app_state.db.update_sample(
                sample_id, file_path=str(path), status="analyzing"
            )
            app_state.db.update_sample(
                sample_id, **library.analyze_file(path), status="ready", error=None
            )
            return {"sample_id": sample_id, "path": str(path)}

        job = app_state.jobs.submit("rip", lead.title or lead.source_id, run)
        result["ripped"] = True
        result["job"] = job.as_dict()

    return result


@router.post("/import")
async def import_file(request: Request, body: ImportIn) -> dict:
    """Bring in a file you already have on disk."""
    app_state = state(request)
    try:
        row = library.import_local(
            app_state.db, body.path,
            audio_dir=app_state.settings.audio_dir, copy=body.copy_file,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, f"No such file: {exc}") from exc
    return row


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> dict:
    app_state = state(request)
    return {"active": app_state.jobs.active, "jobs": app_state.jobs.listing(limit)}
