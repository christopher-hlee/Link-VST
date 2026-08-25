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


def is_future(value: str | None) -> bool:
    parsed = parse(value)
    return parsed is not None and parsed > utcnow()


def seconds_since(value: str | None) -> float | None:
    parsed = parse(value)
    return None if parsed is None else (utcnow() - parsed).total_seconds()
