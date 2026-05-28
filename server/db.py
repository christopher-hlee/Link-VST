"""SQLite database — MIDI uploads, features, generated library."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "linkvst.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS uploads (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT NOT NULL,
            midi_bytes  BLOB NOT NULL,
            uploaded_at TEXT DEFAULT (datetime('now')),
            -- extracted features
            key         TEXT,
            mode        TEXT,
            tempo_bpm   REAL,
            bars        INTEGER,
            phrase_type TEXT,
            note_density    REAL,
            pitch_range     INTEGER,
            avg_interval    REAL,
            rhythmic_regularity REAL,
            chord_complexity    REAL,
            contour     TEXT,
            dominant_pitches TEXT  -- JSON array
        );

        CREATE TABLE IF NOT EXISTS library (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL DEFAULT 'generated',  -- 'generated' or 'uploaded'
            filename    TEXT NOT NULL,
            midi_bytes  BLOB NOT NULL,
            phrase_type TEXT,
            key         TEXT,
            mode        TEXT,
            tempo_bpm   REAL,
            bars        INTEGER,
            description TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            notes_json  TEXT  -- JSON array of note events
        );
        """)


def insert_upload(filename: str, midi_bytes: bytes, features: dict) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO uploads
               (filename, midi_bytes, key, mode, tempo_bpm, bars, phrase_type,
                note_density, pitch_range, avg_interval, rhythmic_regularity,
                chord_complexity, contour, dominant_pitches)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                filename, midi_bytes,
                features["key"], features["mode"], features["tempo_bpm"],
                features["bars"], features["phrase_type"], features["note_density"],
                features["pitch_range"], features["avg_interval"],
                features["rhythmic_regularity"], features["chord_complexity"],
                features["contour"], json.dumps(features["dominant_pitches"]),
            ),
        )
        return cur.lastrowid


def get_all_features() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT key, mode, tempo_bpm, bars, phrase_type, note_density,
                      pitch_range, avg_interval, rhythmic_regularity,
                      chord_complexity, contour
               FROM uploads ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_exemplar_notes(limit: int = 5) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT notes_json, phrase_type, key, mode FROM library ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            if r["notes_json"]:
                result.append({
                    "phrase_type": r["phrase_type"],
                    "key": r["key"],
                    "mode": r["mode"],
                    "notes": json.loads(r["notes_json"]),
                })
        return result


def insert_library(
    filename: str, midi_bytes: bytes, phrase: dict, source: str = "generated"
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO library
               (source, filename, midi_bytes, phrase_type, key, mode,
                tempo_bpm, bars, description, notes_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                source, filename, midi_bytes,
                phrase.get("phrase_type"), phrase.get("key"), phrase.get("mode"),
                phrase.get("tempo_bpm"), phrase.get("bars"), phrase.get("description"),
                json.dumps(phrase.get("notes", [])),
            ),
        )
        return cur.lastrowid


def list_library() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, source, filename, phrase_type, key, mode,
                      tempo_bpm, bars, description, created_at
               FROM library ORDER BY id DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_library_item(item_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM library WHERE id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row else None


def delete_library_item(item_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM library WHERE id = ?", (item_id,))
        return cur.rowcount > 0


def list_library_with_midi() -> list[dict]:
    """All library items including raw MIDI bytes — for export."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM library ORDER BY id").fetchall()
        return [dict(r) for r in rows]
