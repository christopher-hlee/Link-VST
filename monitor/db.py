"""SQLite storage — watches, check history, events.

Raw sqlite3 with WAL and `row_factory=Row`; no ORM, since the query surface is
small and fixed. Connections are closed explicitly rather than relying on
`with sqlite3.connect(...)`, which commits but does not close — the scheduler
opens one every tick, so that pattern would leak file descriptors over days of
uptime.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH, CHECK_HISTORY_LIMIT
from .timeutil import EPOCH
from .statemachine import UNKNOWN


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Transactional connection that always closes."""
    conn = get_conn()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    brand        TEXT,
    url          TEXT NOT NULL,
    strategy     TEXT NOT NULL,                    -- shopify | target | bestbuy
    kind         TEXT NOT NULL DEFAULT 'product',  -- product | collection
    target_ref   TEXT,                             -- handle | variant_id | tcin | sku
    store_ref    TEXT,                             -- zip / store id for pickup checks
    size_pref    TEXT,                             -- ordered sizes, e.g. "m,l". Empty = any.
    base_interval_s INTEGER NOT NULL DEFAULT 300,
    hot_interval_s  INTEGER NOT NULL DEFAULT 45,
    hot_until    TEXT,
    alert_level  TEXT NOT NULL DEFAULT 'info',     -- info | critical
    enabled      INTEGER NOT NULL DEFAULT 1,
    etag         TEXT,
    last_modified TEXT,
    last_state   TEXT NOT NULL DEFAULT 'unknown',
    last_price   REAL,
    last_title   TEXT,
    last_image   TEXT,
    baseline_json TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    last_checked_at TEXT,
    next_check_at   TEXT,
    last_alert_at   TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS checks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id   INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    http_status INTEGER,
    state      TEXT,
    ok         INTEGER NOT NULL DEFAULT 1,
    latency_ms INTEGER,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id   INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT,
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    notified_at TEXT,
    notify_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_watches_due   ON watches(enabled, next_check_at);
CREATE INDEX IF NOT EXISTS idx_checks_watch  ON checks(watch_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_recent ON events(id DESC);
"""


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- watches

WATCH_WRITABLE = {
    "name", "brand", "url", "strategy", "kind", "target_ref", "store_ref", "size_pref",
    "base_interval_s", "hot_interval_s", "hot_until", "alert_level", "enabled",
    "etag", "last_modified", "last_state", "last_price", "last_title",
    "last_image", "baseline_json", "consecutive_failures", "last_error",
    "last_checked_at", "next_check_at", "last_alert_at",
}


def create_watch(**fields: Any) -> int:
    cols = {k: v for k, v in fields.items() if k in WATCH_WRITABLE}
    cols.setdefault("last_state", UNKNOWN)
    cols.setdefault("next_check_at", EPOCH)  # due immediately
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    with tx() as conn:
        cur = conn.execute(
            f"INSERT INTO watches ({names}) VALUES ({marks})", tuple(cols.values())
        )
        return int(cur.lastrowid)


def update_watch(watch_id: int, **fields: Any) -> None:
    cols = {k: v for k, v in fields.items() if k in WATCH_WRITABLE}
    if not cols:
        return
    assigns = ", ".join(f"{k}=?" for k in cols)
    with tx() as conn:
        conn.execute(f"UPDATE watches SET {assigns} WHERE id=?",
                     (*cols.values(), watch_id))


def delete_watch(watch_id: int) -> bool:
    with tx() as conn:
        cur = conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))
        return cur.rowcount > 0


def get_watch(watch_id: int) -> dict | None:
    with tx() as conn:
        row = conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
        return dict(row) if row else None


def list_watches() -> list[dict]:
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM watches ORDER BY enabled DESC, brand, name"
        ).fetchall()
        return [dict(r) for r in rows]


def due_watches(limit: int = 50) -> list[dict]:
    """Enabled watches whose next_check_at has passed."""
    with tx() as conn:
        rows = conn.execute(
            """SELECT * FROM watches
               WHERE enabled=1
                 AND (next_check_at IS NULL OR next_check_at <= datetime('now'))
               ORDER BY next_check_at IS NULL DESC, next_check_at
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_baseline(watch: dict) -> list[str] | None:
    raw = watch.get("baseline_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- checks

def record_check(watch_id: int, *, ok: bool, state: str | None,
                 http_status: int | None, latency_ms: int | None,
                 error: str | None) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO checks (watch_id, ok, state, http_status, latency_ms, error)
               VALUES (?,?,?,?,?,?)""",
            (watch_id, 1 if ok else 0, state, http_status, latency_ms, error),
        )
        conn.execute(
            """DELETE FROM checks
               WHERE watch_id=? AND id NOT IN (
                   SELECT id FROM checks WHERE watch_id=? ORDER BY id DESC LIMIT ?
               )""",
            (watch_id, watch_id, CHECK_HISTORY_LIMIT),
        )


def list_checks(watch_id: int, limit: int = 100) -> list[dict]:
    with tx() as conn:
        rows = conn.execute(
            "SELECT * FROM checks WHERE watch_id=? ORDER BY id DESC LIMIT ?",
            (watch_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- events

def insert_event(watch_id: int, kind: str, from_state: str | None,
                 to_state: str | None, payload: dict) -> int:
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO events (watch_id, kind, from_state, to_state, payload_json)
               VALUES (?,?,?,?,?)""",
            (watch_id, kind, from_state, to_state, json.dumps(payload)),
        )
        return int(cur.lastrowid)


def mark_notified(event_id: int, error: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE events SET notified_at=datetime('now'), notify_error=? WHERE id=?",
            (error, event_id),
        )


def list_events(limit: int = 100) -> list[dict]:
    with tx() as conn:
        rows = conn.execute(
            """SELECT e.*, w.name AS watch_name, w.brand AS watch_brand
               FROM events e LEFT JOIN watches w ON w.id = e.watch_id
               ORDER BY e.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json") or "{}")
            except (ValueError, TypeError):
                d["payload"] = {}
            out.append(d)
        return out


def summary() -> dict:
    with tx() as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*)                                        AS total,
                 SUM(enabled=1)                                  AS enabled,
                 SUM(last_state='in_stock' AND enabled=1)        AS in_stock,
                 SUM(consecutive_failures >= 5)                  AS failing,
                 SUM(hot_until IS NOT NULL
                     AND hot_until > datetime('now'))            AS armed
               FROM watches"""
        ).fetchone()
        return {k: (row[k] or 0) for k in row.keys()}
