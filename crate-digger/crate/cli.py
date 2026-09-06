"""Command line front end.

    crate serve                       start the web app
    crate dig soul-45s                rummage a seam and print what's down there
    crate digs                        list the seams
    crate search "upright bass"       search an archive directly
    crate pull <source> <id> <url>    download + analyse one thing
    crate import <path>               bring in a file you already have
    crate ls --bpm 80 100             what's in the crate
    crate chop <id> --export          slice a sample into one-shots
    crate loop <id> 12.0 19.5 --bpm 88
    crate analyze <id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import digs as digs_module
from . import library
from .audio import chop as chopper
from .audio.cache import CACHE
from .config import Settings, get_settings
from .db import Database
from .sources.base import Lead, SourceError
from .sources.registry import Registry


def _fmt(row: dict) -> str:
    bits = [
        f"[{row.get('id', '-')}]",
        (row.get("title") or "")[:46].ljust(46),
        (row.get("artist") or "")[:22].ljust(22),
        f"{row['bpm']:6.1f}bpm" if row.get("bpm") else " " * 9,
        (row.get("musical_key") or "").ljust(9),
        f"{row['duration']:6.1f}s" if row.get("duration") else " " * 7,
        (row.get("status") or ""),
    ]
    return "  ".join(bits)


async def _dig(settings: Settings, db: Database, args) -> int:
    dig = digs_module.get(args.slug)
    if not dig:
        print(f"No dig called {args.slug!r}. Try: crate digs", file=sys.stderr)
        return 1
    registry = Registry(settings)
    try:
        source = registry.get(dig.source)
        page = args.page or digs_module.random_page(dig)
        leads = await source.search(**{**dig.params, "rows": args.rows, "page": page})
        seen = db.seen_ids(dig.source)
        leads = [l for l in leads if l.source_id not in seen]
        print(f"{dig.name} — page {page} — {len(leads)} unseen\n")
        for lead in leads:
            print(f"  {lead.source_id[:38].ljust(38)}  {lead.title[:44]}")
            if args.verbose:
                print(f"      {lead.artist} · {lead.year or '?'} · {lead.license}")
        if args.pull:
            print(f"\nPulling {min(args.pull, len(leads))}…")
            for lead in leads[: args.pull]:
                tracks = await registry.ia.tracks(lead.source_id) if dig.source == "ia" else [lead]
                for track in tracks[:1]:
                    row = await library.ingest_lead(
                        db, registry.client, track,
                        audio_dir=settings.audio_dir, max_mb=settings.max_download_mb,
                    )
                    print("  " + _fmt(row))
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await registry.aclose()
    return 0


async def _search(settings: Settings, args) -> int:
    registry = Registry(settings)
    try:
        adapter = registry.get(args.source)
        kwargs = {"rows": args.rows, "page": args.page}
        if args.source == "ia":
            kwargs.update(
                year_from=args.year_from, year_to=args.year_to,
                collections=args.collections.split(",") if args.collections else None,
                subjects=args.subjects.split(",") if args.subjects else None,
            )
        leads = await adapter.search(args.query, **kwargs)
        if args.json:
            print(json.dumps([l.as_dict() for l in leads], indent=2))
        else:
            for lead in leads:
                print(f"  {lead.source_id[:38].ljust(38)}  {lead.title[:44]}  "
                      f"{lead.year or ''}")
        print(f"\n{len(leads)} results", file=sys.stderr)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await registry.aclose()
    return 0


async def _pull(settings: Settings, db: Database, args) -> int:
    registry = Registry(settings)
    try:
        if args.source == "ia" and not args.url:
            tracks = await registry.ia.tracks(args.source_id)
            if not tracks:
                print("No audio on that item", file=sys.stderr)
                return 1
            targets = tracks[: args.limit]
        else:
            targets = [Lead(source=args.source, source_id=args.source_id,
                            title=args.title or args.source_id, stream_url=args.url)]
        for lead in targets:
            row = await library.ingest_lead(
                db, registry.client, lead,
                audio_dir=settings.audio_dir, max_mb=settings.max_download_mb,
            )
            print(_fmt(row))
            if row.get("error"):
                print(f"  ! {row['error']}", file=sys.stderr)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        await registry.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crate", description="Crate Digger — a sample crate you can dig through."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--reload", action="store_true")

    sub.add_parser("digs", help="list the preset digs")

    p = sub.add_parser("dig", help="rummage through one seam")
    p.add_argument("slug")
    p.add_argument("--page", type=int, default=None, help="default: a random page")
    p.add_argument("--rows", type=int, default=25)
    p.add_argument("--pull", type=int, default=0, metavar="N", help="download the first N")
    p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("search", help="search an archive")
    p.add_argument("query")
    p.add_argument("--source", default="ia", choices=["ia", "openverse", "discogs"])
    p.add_argument("--rows", type=int, default=25)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--year-from", type=int, dest="year_from")
    p.add_argument("--year-to", type=int, dest="year_to")
    p.add_argument("--collections")
    p.add_argument("--subjects")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("pull", help="download and analyse one record")
    p.add_argument("source")
    p.add_argument("source_id")
    p.add_argument("url", nargs="?", default="")
    p.add_argument("--title", default="")
    p.add_argument("--limit", type=int, default=3, help="tracks per archive item")

    p = sub.add_parser("import", help="bring in a local file")
    p.add_argument("path")
    p.add_argument("--no-copy", action="store_true")

    p = sub.add_parser("ls", help="list the crate")
    p.add_argument("--bpm", nargs=2, type=float, metavar=("MIN", "MAX"))
    p.add_argument("--key")
    p.add_argument("--breaks", type=float, metavar="MIN", help="0-1 percussive ratio")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("analyze", help="re-analyse a sample")
    p.add_argument("sample_id", type=int)

    p = sub.add_parser("chop", help="slice a sample into one-shots")
    p.add_argument("sample_id", type=int)
    p.add_argument("--sensitivity", type=float, default=1.0)
    p.add_argument("--grid", type=float, default=None, metavar="BEATS")
    p.add_argument("--export", action="store_true", help="write WAVs")

    p = sub.add_parser("loop", help="render a loop, optionally varispeeded")
    p.add_argument("sample_id", type=int)
    p.add_argument("start", type=float)
    p.add_argument("end", type=float)
    p.add_argument("--bpm", type=float, default=None, help="target tempo")

    args = parser.parse_args(argv)
    settings = get_settings()
    db = Database(settings.db_path)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "crate.server.app:app",
            host=args.host or settings.host,
            port=args.port or settings.port,
            reload=args.reload,
        )
        return 0

    if args.cmd == "digs":
        for dig in digs_module.DIGS:
            print(f"  {dig.slug.ljust(24)} {dig.name.ljust(26)} {dig.blurb}")
        return 0

    if args.cmd == "dig":
        return asyncio.run(_dig(settings, db, args))
    if args.cmd == "search":
        return asyncio.run(_search(settings, args))
    if args.cmd == "pull":
        return asyncio.run(_pull(settings, db, args))

    if args.cmd == "import":
        row = library.import_local(
            db, args.path, audio_dir=settings.audio_dir, copy=not args.no_copy
        )
        print(_fmt(row))
        return 0

    if args.cmd == "ls":
        where, params = [], []
        if args.bpm:
            where.append("bpm BETWEEN ? AND ?")
            params += args.bpm
        if args.key:
            where.append("musical_key = ?")
            params.append(args.key)
        if args.breaks is not None:
            where.append("breakiness >= ?")
            params.append(args.breaks)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = db.query(
            f"SELECT * FROM samples{clause} ORDER BY created_at DESC LIMIT ?",
            [*params, args.limit],
        )
        for row in rows:
            print(_fmt(row))
        print(f"\n{len(rows)} of {db.one('SELECT COUNT(*) n FROM samples')['n']}",
              file=sys.stderr)
        return 0

    row = db.get_sample(args.sample_id)
    if not row:
        print(f"No sample {args.sample_id}", file=sys.stderr)
        return 1
    if not row.get("file_path") or not Path(row["file_path"]).is_file():
        print(f"Sample {args.sample_id} has no file on disk", file=sys.stderr)
        return 1
    path = Path(row["file_path"])

    if args.cmd == "analyze":
        db.update_sample(args.sample_id, **library.analyze_file(path), status="ready")
        print(_fmt(db.get_sample(args.sample_id) or {}))
        return 0

    y, sr = CACHE.load(path)

    if args.cmd == "chop":
        if args.grid:
            if not row.get("bpm"):
                print("Grid chopping needs a BPM — run `crate analyze` first", file=sys.stderr)
                return 1
            slices = chopper.chop_grid(y, sr, row["bpm"], division=args.grid)
        else:
            slices = chopper.chop_transient(y, sr, sensitivity=args.sensitivity)
        stem = library.slugify(row.get("title") or f"sample-{args.sample_id}")
        out_dir = settings.slices_dir / f"{args.sample_id:05d}-{stem}"
        for s in slices:
            line = f"  {s.idx:3d}  {s.start:8.3f} → {s.end:8.3f}  ({s.length:.3f}s)"
            if args.export:
                target = chopper.write_wav(
                    out_dir / f"{stem}-{s.idx:02d}.wav",
                    chopper.take(y, sr, s.start, s.end), sr,
                )
                line += f"  {target}"
            print(line)
        print(f"\n{len(slices)} slices" + (f" → {out_dir}" if args.export else ""),
              file=sys.stderr)
        return 0

    if args.cmd == "loop":
        seg, info = chopper.render_loop(
            y, sr, args.start, args.end,
            source_bpm=row.get("bpm"), target_bpm=args.bpm,
        )
        stem = library.slugify(row.get("title") or f"sample-{args.sample_id}")
        target = chopper.write_wav(
            settings.loops_dir / f"{stem}-{int(args.start)}s.wav", seg, sr
        )
        print(target)
        print(f"  {info['bars'] or '?'} bars · speed {info['speed']} · "
              f"{info['semitones']:+.2f} semitones", file=sys.stderr)
        return 0

    return 0
