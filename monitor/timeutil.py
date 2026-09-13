"""Timestamp helpers.

Everything is stored in SQLite's own `datetime('now')` format — UTC, second
resolution, space-separated — so that plain string comparison in SQL is a valid
chronological comparison.
"""
from datetime import datetime, timedelta, timezone

SQL_FORMAT = "%Y-%m-%d %H:%M:%S"
EPOCH = "1970-01-01 00:00:00"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stamp(dt: datetime | None = None) -> str:
    return (dt or utcnow()).strftime(SQL_FORMAT)


def stamp_in(seconds: float) -> str:
    return stamp(utcnow() + timedelta(seconds=seconds))


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in (SQL_FORMAT, "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def parse_instant(value: str | None) -> datetime | None:
    """Parse an EXTERNAL timestamp, normalised to naive UTC.

    `parse` is for our own SQLite stamps and truncates at 19 characters, which
    silently discards a timezone offset. Shopify serves `published_at` in the
    shop's own timezone with the offset attached, so truncating "…T22:36:47-04:00"
    reads a product published this minute as four hours old — and a four-hour-old
    product is outside the sweep window, so a real drop is absorbed in silence.
    Anything arriving from a store has to come through here.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return parse(value)          # fall back to our own stamp format
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def is_future(value: str | None) -> bool:
    parsed = parse(value)
    return parsed is not None and parsed > utcnow()


def seconds_since(value: str | None) -> float | None:
    parsed = parse(value)
    return None if parsed is None else (utcnow() - parsed).total_seconds()
