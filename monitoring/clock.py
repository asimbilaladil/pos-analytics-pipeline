"""Chicago business-day arithmetic, with an injectable "now" for tests."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .config import CHICAGO, parse_hhmm


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def chicago(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(CHICAGO)


def at_local(day: date, hhmm: str) -> datetime:
    """day at HH:MM America/Chicago, DST-correct."""
    return datetime.combine(day, parse_hhmm(hhmm), tzinfo=CHICAGO)


def ready_at(business_day: date, hhmm: str, days_after: int = 1) -> datetime:
    """When business_day's data is expected: HH:MM Chicago, `days_after` days later."""
    return at_local(business_day + timedelta(days=days_after), hhmm)


def closed_business_days(now: datetime, lookback: int) -> list[date]:
    """The `lookback` most recent Chicago business days that have ENDED, newest first."""
    today = chicago(now).date()
    return [today - timedelta(days=i) for i in range(1, lookback + 1)]
