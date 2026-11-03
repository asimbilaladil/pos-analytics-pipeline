"""Authorization, input validation and redaction shared by every tool."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import settings
from .keystore import Principal

CHICAGO = ZoneInfo("America/Chicago")


class ToolInputError(Exception):
    """Invalid or unauthorised request. The message is safe to return to a client."""


class ToolDenied(ToolInputError):
    pass


def chicago_today() -> date:
    return datetime.now(CHICAGO).date()


def require_tool(p: Principal, tool: str) -> None:
    if tool not in p.tools:                         # default deny
        raise ToolDenied("this credential is not permitted to use this tool")


def known_store_ids() -> dict[int, str]:
    import chat_sql
    return dict(chat_sql.ESTABLISHMENTS)


def resolve_stores(p: Principal, requested) -> list[int]:
    """The stores a call may read.

    requested None/[] -> every store the credential holds. Any requested id
    outside the credential's grant is refused outright (never silently dropped,
    never answered with a network total), and the error does not say whether the
    id exists.
    """
    known = known_store_ids()
    granted = set(known) if p.all_stores else set(p.stores)
    if requested in (None, [], ()):
        ids = sorted(granted)
    else:
        if not isinstance(requested, (list, tuple)):
            raise ToolInputError("store_ids must be a list of integers")
        try:
            ids = sorted({int(x) for x in requested})
        except (TypeError, ValueError):
            raise ToolInputError("store_ids must be a list of integers") from None
        if any(isinstance(x, bool) for x in requested):
            raise ToolInputError("store_ids must be a list of integers")
        denied = [i for i in ids if i not in granted]
        if denied:
            raise ToolDenied("one or more requested stores are not permitted for this "
                             "credential")
    if not ids:
        raise ToolDenied("this credential has no permitted stores")
    if len(ids) > settings.MAX_STORES_PER_CALL:
        raise ToolInputError(f"at most {settings.MAX_STORES_PER_CALL} stores per call")
    return ids


def parse_date(value, name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ToolInputError(f"{name} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolInputError(f"{name} is not a valid calendar date") from None


def parse_range(start, end, *, max_days: int | None = None) -> tuple[date, date]:
    """Inclusive [start, end]. Returns the dates; callers use end + 1 day as the
    exclusive bound for SQL."""
    s, e = parse_date(start, "start_date"), parse_date(end, "end_date")
    if e < s:
        raise ToolInputError("end_date is before start_date")
    limit = max_days or settings.MAX_RANGE_DAYS
    days = (e - s).days + 1
    if days > limit:
        raise ToolInputError(f"date range is {days} days; the maximum is {limit}")
    if e < date.fromisoformat(settings.DATA_START):
        raise ToolInputError(f"no source holds data before {settings.DATA_START}")
    if s > chicago_today():
        raise ToolInputError("start_date is in the future")
    return s, e


def ranges_of(dates: list[date]) -> list[str]:
    """Collapse a sorted date list into 'a..b' strings."""
    out, i = [], 0
    ds = sorted(dates)
    while i < len(ds):
        j = i
        while j + 1 < len(ds) and (ds[j + 1] - ds[j]).days == 1:
            j += 1
        out.append(ds[i].isoformat() if i == j else f"{ds[i]}..{ds[j]}")
        i = j + 1
    return out


def num(v, nd=2):
    """Decimal/None -> float/None. None stays None: missing is never zero."""
    if v is None:
        return None
    return round(float(v), nd)


_SECRET_RX = re.compile(
    r"(lmcp_[0-9a-f]{12}_[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{16,}|"
    r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}|(?i:password|passwd|pwd|api[_-]?key|secret)"
    r"\s*[=:]\s*\S+)")


def redact(text: str) -> str:
    return _SECRET_RX.sub("[redacted]", text or "")
