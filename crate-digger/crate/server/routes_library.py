"""Browsing what you've already pulled: samples, markers, crates."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..db import hydrate_all
from .deps import require_sample, state
from .schemas import CrateIn, CrateItemIn, MarkerIn, SampleUpdate

router = APIRouter(prefix="/api", tags=["library"])

SORTS = {
    "recent": "s.created_at DESC",
    "oldest": "s.created_at ASC",
    "bpm": "s.bpm ASC NULLS LAST",
    "bpm_desc": "s.bpm DESC NULLS LAST",
    "breaks": "s.breakiness DESC NULLS LAST",
    "smooth": "s.breakiness ASC NULLS LAST",
    "duration": "s.duration ASC NULLS LAST",
    "title": "s.title COLLATE NOCASE ASC",
    "year": "s.year ASC NULLS LAST",
    "loud": "s.loudness_db DESC NULLS LAST",
}


@router.get("/library")
async def library_list(
    request: Request,
    q: str = "",
    source: str | None = None,
    status: str | None = None,
    key: str | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    breaks_min: float | None = Query(default=None, ge=0, le=1),
    starred: bool | None = None,
    crate_id: int | None = None,
    sort: str = "recent",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    db = state(request).db
    where: list[str] = []
    # JOIN placeholders bind before WHERE placeholders, so they are kept apart
    # and concatenated in SQL order.
    join_params: list[Any] = []
    where_params: list[Any] = []
    joins = ""

    if crate_id is not None:
        joins += " JOIN crate_items ci ON ci.sample_id = s.id AND ci.crate_id = ?"
        join_params.append(crate_id)

    if q.strip():
        if db.has_fts:
            joins += " JOIN samples_fts f ON f.rowid = s.id"
            where.append("samples_fts MATCH ?")
            where_params.append(_fts_query(q))
        else:
            where.append(
                "(s.title LIKE ? OR s.artist LIKE ? OR s.album LIKE ?"
                " OR s.label LIKE ? OR s.genre LIKE ?)"
            )
            where_params += [f"%{q}%"] * 5

    for column, value in (
        ("s.source", source), ("s.status", status), ("s.musical_key", key)
    ):
        if value:
            where.append(f"{column} = ?")
            where_params.append(value)
    for clause, value in (
        ("s.bpm >= ?", bpm_min), ("s.bpm <= ?", bpm_max),
        ("s.year >= ?", year_min), ("s.year <= ?", year_max),
        ("s.breakiness >= ?", breaks_min),
    ):
        if value is not None:
            where.append(clause)
            where_params.append(value)
    if starred is not None:
        where.append("s.starred = ?")
        where_params.append(1 if starred else 0)

    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    order = SORTS.get(sort, SORTS["recent"])
    params = [*join_params, *where_params]

    total = db.one(
        f"SELECT COUNT(*) AS n FROM samples s{joins}{sql_where}", params
    )
    rows = db.query(
        f"SELECT s.* FROM samples s{joins}{sql_where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return {
        "total": int(total["n"]) if total else 0,
        "count": len(rows),
        "offset": offset,
        "results": [_strip_peaks(r) for r in hydrate_all(rows)],
    }


def _fts_query(q: str) -> str:
    """Turn user words into a forgiving prefix MATCH, quoting each term."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in q).split() if t]
    return " ".join(f'"{t}"*' for t in terms) or '""'


def _strip_peaks(row: dict) -> dict:
    """List views don't need 1600 floats per row."""
    row = dict(row)
    row["has_peaks"] = bool(row.get("peaks"))
    row.pop("peaks", None)
    return row


@router.get("/library/stats")
async def library_stats(request: Request) -> dict:
    db = state(request).db
    by_source = db.query(
        "SELECT source, COUNT(*) AS n FROM samples GROUP BY source ORDER BY n DESC"
    )
    by_status = db.query(
        "SELECT status, COUNT(*) AS n FROM samples GROUP BY status ORDER BY n DESC"
    )
    keys = db.query(
        "SELECT musical_key AS k, COUNT(*) AS n FROM samples"
        " WHERE musical_key IS NOT NULL GROUP BY k ORDER BY n DESC LIMIT 24"
    )
    totals = db.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(duration),0) AS secs,"
        " COALESCE(SUM(bytes),0) AS bytes FROM samples WHERE file_path IS NOT NULL"
    ) or {}
    return {
        "samples": int(totals.get("n", 0)),
        "seconds": float(totals.get("secs", 0.0)),
        "bytes": int(totals.get("bytes", 0)),
        "by_source": by_source,
        "by_status": by_status,
        "keys": keys,
    }


@router.get("/samples/{sample_id}")
async def get_sample(request: Request, sample_id: int) -> dict:
    row = require_sample(request, sample_id)
    db = state(request).db
    row["markers"] = db.query(
        "SELECT * FROM markers WHERE sample_id=? ORDER BY start_sec", (sample_id,)
    )
    row["slices"] = db.query(
        "SELECT * FROM slices WHERE sample_id=? ORDER BY idx", (sample_id,)
    )
    row["crates"] = db.query(
        "SELECT c.id, c.name, c.color FROM crates c"
        " JOIN crate_items ci ON ci.crate_id = c.id WHERE ci.sample_id=?",
        (sample_id,),
    )
    return row


@router.patch("/samples/{sample_id}")
async def update_sample(request: Request, sample_id: int, body: SampleUpdate) -> dict:
    require_sample(request, sample_id)
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "starred" in fields:
        fields["starred"] = 1 if fields["starred"] else 0
    if fields:
        state(request).db.update_sample(sample_id, **fields)
    return require_sample(request, sample_id)


@router.delete("/samples/{sample_id}")
async def delete_sample(
    request: Request, sample_id: int, delete_file: bool = False
) -> dict:
    from pathlib import Path

    app_state = state(request)
    row = require_sample(request, sample_id)
    removed = []
    if delete_file:
        for path in [row.get("file_path")] + [
            s["file_path"] for s in app_state.db.query(
                "SELECT file_path FROM slices WHERE sample_id=?", (sample_id,))
        ]:
            if not path:
                continue
            candidate = Path(path)
            try:
                inside = candidate.resolve().is_relative_to(
                    app_state.settings.library_dir.resolve()
                )
            except OSError:
                inside = False
            if inside and candidate.is_file():
                candidate.unlink()
                removed.append(str(candidate))
    app_state.db.delete_sample(sample_id)
    return {"deleted": sample_id, "files_removed": removed}


# -- markers ------------------------------------------------------------
@router.post("/samples/{sample_id}/markers")
async def add_marker(request: Request, sample_id: int, body: MarkerIn) -> dict:
    require_sample(request, sample_id)
    db = state(request).db
    cur = db.execute(
        "INSERT INTO markers(sample_id, kind, start_sec, end_sec, label, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (sample_id, body.kind, body.start_sec, body.end_sec, body.label, time.time()),
    )
    return db.one("SELECT * FROM markers WHERE id=?", (cur.lastrowid,)) or {}


@router.delete("/markers/{marker_id}")
async def delete_marker(request: Request, marker_id: int) -> dict:
    state(request).db.execute("DELETE FROM markers WHERE id=?", (marker_id,))
    return {"deleted": marker_id}


# -- crates -------------------------------------------------------------
@router.get("/crates")
async def list_crates(request: Request) -> dict:
    rows = state(request).db.query(
        "SELECT c.*, (SELECT COUNT(*) FROM crate_items ci WHERE ci.crate_id=c.id)"
        " AS item_count FROM crates c ORDER BY c.name COLLATE NOCASE"
    )
    return {"crates": rows}


@router.post("/crates")
async def create_crate(request: Request, body: CrateIn) -> dict:
    db = state(request).db
    existing = db.one("SELECT * FROM crates WHERE name=?", (body.name,))
    if existing:
        return existing
    cur = db.execute(
        "INSERT INTO crates(name, color, notes, created_at) VALUES (?,?,?,?)",
        (body.name, body.color, body.notes, time.time()),
    )
    return db.one("SELECT * FROM crates WHERE id=?", (cur.lastrowid,)) or {}


@router.delete("/crates/{crate_id}")
async def delete_crate(request: Request, crate_id: int) -> dict:
    state(request).db.execute("DELETE FROM crates WHERE id=?", (crate_id,))
    return {"deleted": crate_id}


@router.post("/crates/{crate_id}/items")
async def add_to_crate(request: Request, crate_id: int, body: CrateItemIn) -> dict:
    db = state(request).db
    if not db.one("SELECT id FROM crates WHERE id=?", (crate_id,)):
        raise HTTPException(404, f"No crate {crate_id}")
    require_sample(request, body.sample_id)
    position = db.one(
        "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM crate_items WHERE crate_id=?",
        (crate_id,),
    )
    db.execute(
        "INSERT OR IGNORE INTO crate_items(crate_id, sample_id, position, added_at)"
        " VALUES (?,?,?,?)",
        (crate_id, body.sample_id, int(position["p"]) if position else 0, time.time()),
    )
    return {"crate_id": crate_id, "sample_id": body.sample_id}


@router.delete("/crates/{crate_id}/items/{sample_id}")
async def remove_from_crate(request: Request, crate_id: int, sample_id: int) -> dict:
    state(request).db.execute(
        "DELETE FROM crate_items WHERE crate_id=? AND sample_id=?", (crate_id, sample_id)
    )
    return {"removed": sample_id}
