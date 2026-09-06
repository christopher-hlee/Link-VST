"""SQLite storage for the crate.

One file, WAL mode, no ORM. Rows come back as dicts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,              -- ia | openverse | youtube | local
    source_id       TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    artist          TEXT NOT NULL DEFAULT '',
    album           TEXT NOT NULL DEFAULT '',
    label           TEXT NOT NULL DEFAULT '',
    year            INTEGER,
    genre           TEXT NOT NULL DEFAULT '',
    license         TEXT NOT NULL DEFAULT '',
    license_url     TEXT NOT NULL DEFAULT '',
    page_url        TEXT NOT NULL DEFAULT '',
    stream_url      TEXT NOT NULL DEFAULT '',
    file_path       TEXT,
    ext             TEXT NOT NULL DEFAULT '',
    bytes           INTEGER,
    duration        REAL,
    sample_rate     INTEGER,
    channels        INTEGER,
    bpm             REAL,
    bpm_confidence  REAL,
    musical_key     TEXT,
    key_confidence  REAL,
    loudness_db     REAL,
    breakiness      REAL,                       -- percussive/harmonic energy ratio
    peaks           TEXT,                       -- JSON array of waveform peaks
    status          TEXT NOT NULL DEFAULT 'lead', -- lead|queued|downloading|analyzing|ready|error
    error           TEXT,
    notes           TEXT NOT NULL DEFAULT '',
    starred         INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_samples_status ON samples(status);
CREATE INDEX IF NOT EXISTS idx_samples_bpm    ON samples(bpm);
CREATE INDEX IF NOT EXISTS idx_samples_key    ON samples(musical_key);
CREATE INDEX IF NOT EXISTS idx_samples_year   ON samples(year);

CREATE TABLE IF NOT EXISTS crates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    color      TEXT NOT NULL DEFAULT '#c9a227',
    notes      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS crate_items (
    crate_id  INTEGER NOT NULL REFERENCES crates(id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL DEFAULT 0,
    added_at  REAL NOT NULL,
    PRIMARY KEY (crate_id, sample_id)
);

-- A marker is a spot you liked: a loop region, a one-shot, or just a note.
CREATE TABLE IF NOT EXISTS markers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id  INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL DEFAULT 'loop',   -- loop|hit|note
    start_sec  REAL NOT NULL,
    end_sec    REAL,
    label      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markers_sample ON markers(sample_id);

CREATE TABLE IF NOT EXISTS slices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id  INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    start_sec  REAL NOT NULL,
    end_sec    REAL NOT NULL,
    file_path  TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slices_sample ON slices(sample_id);

-- Keep/pass decisions from Dig mode, so a record never comes back twice.
CREATE TABLE IF NOT EXISTS verdicts (
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    verdict    TEXT NOT NULL,                  -- keep|pass
    created_at REAL NOT NULL,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS samples_fts USING fts5(
    title, artist, album, label, genre, notes,
    content='samples', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS samples_ai AFTER INSERT ON samples BEGIN
    INSERT INTO samples_fts(rowid, title, artist, album, label, genre, notes)
    VALUES (new.id, new.title, new.artist, new.album, new.label, new.genre, new.notes);
END;
CREATE TRIGGER IF NOT EXISTS samples_ad AFTER DELETE ON samples BEGIN
    INSERT INTO samples_fts(samples_fts, rowid, title, artist, album, label, genre, notes)
    VALUES ('delete', old.id, old.title, old.artist, old.album, old.label, old.genre, old.notes);
END;
CREATE TRIGGER IF NOT EXISTS samples_au AFTER UPDATE ON samples BEGIN
    INSERT INTO samples_fts(samples_fts, rowid, title, artist, album, label, genre, notes)
    VALUES ('delete', old.id, old.title, old.artist, old.album, old.label, old.genre, old.notes);
    INSERT INTO samples_fts(rowid, title, artist, album, label, genre, notes)
    VALUES (new.id, new.title, new.artist, new.album, new.label, new.genre, new.notes);
END;
"""

_local = threading.local()


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


class Database:
    """Thread-local SQLite connections over one library file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.has_fts = False
        self._init()

    # -- connection -----------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        key = f"conn_{id(self)}"
        conn = getattr(_local, key, None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = _row_factory
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            setattr(_local, key, conn)
        return conn

    def close(self) -> None:
        key = f"conn_{id(self)}"
        conn = getattr(_local, key, None)
        if conn is not None:
            conn.close()
            setattr(_local, key, None)

    def _init(self) -> None:
        self.conn.executescript(SCHEMA)
        try:
            self.conn.executescript(FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:
            # SQLite built without FTS5 — search falls back to LIKE.
            self.has_fts = False
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    # -- helpers --------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.conn.execute(sql, params).fetchmany(1)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    # -- samples --------------------------------------------------------
    def upsert_sample(self, data: dict[str, Any]) -> int:
        """Insert a sample, or return the id of the existing (source, source_id)."""
        now = time.time()
        payload = dict(data)
        payload.setdefault("status", "lead")
        payload["created_at"] = now
        payload["updated_at"] = now
        if isinstance(payload.get("peaks"), (list, tuple)):
            payload["peaks"] = json.dumps(payload["peaks"])
        cols = ", ".join(payload)
        marks = ", ".join("?" for _ in payload)
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO samples ({cols}) VALUES ({marks})",
            list(payload.values()),
        )
        self.conn.commit()
        if cur.lastrowid and cur.rowcount:
            return int(cur.lastrowid)
        row = self.one(
            "SELECT id FROM samples WHERE source=? AND source_id=?",
            (payload["source"], payload["source_id"]),
        )
        return int(row["id"]) if row else 0

    def update_sample(self, sample_id: int, **fields: Any) -> None:
        if not fields:
            return
        if isinstance(fields.get("peaks"), (list, tuple)):
            fields["peaks"] = json.dumps(fields["peaks"])
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE samples SET {sets} WHERE id=?", [*fields.values(), sample_id]
        )

    def get_sample(self, sample_id: int) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM samples WHERE id=?", (sample_id,))
        return hydrate(row) if row else None

    def delete_sample(self, sample_id: int) -> None:
        self.execute("DELETE FROM samples WHERE id=?", (sample_id,))

    # -- verdicts -------------------------------------------------------
    def record_verdict(self, source: str, source_id: str, verdict: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO verdicts(source, source_id, verdict, created_at)"
            " VALUES (?,?,?,?)",
            (source, source_id, verdict, time.time()),
        )

    def seen_ids(self, source: str) -> set[str]:
        rows = self.query("SELECT source_id FROM verdicts WHERE source=?", (source,))
        rows += self.query("SELECT source_id FROM samples WHERE source=?", (source,))
        return {r["source_id"] for r in rows}


def hydrate(row: dict[str, Any]) -> dict[str, Any]:
    """Decode JSON columns for API responses."""
    out = dict(row)
    if out.get("peaks"):
        try:
            out["peaks"] = json.loads(out["peaks"])
        except (TypeError, ValueError):
            out["peaks"] = None
    out["starred"] = bool(out.get("starred"))
    return out


def hydrate_all(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [hydrate(r) for r in rows]
