"""Refresh state of one R365 financial fiscal period. PURE: shared by AI Chat / MCP
(chat_sql R365 FINANCIAL gate) and the data monitor, so every surface tells the same
story about how current a period's figures are.

R365 keeps changing a CLOSED period for ~5 weeks (late postings, approvals,
re-classification). The scheduled ingest re-pulls every period overlapping the last
42 days (r365_financial_ingest.OVERLAP_DAYS). A period is therefore only FINAL once a
COMPLETE, reconciled load postdates that repair window.

States
  FINAL        closed; a COMPLETE, reconciled load on or after the repair window's last day
  PROVISIONAL  current (open) period refreshed recently, or a closed period inside its
               repair window refreshed recently -- figures may still change
  STALE        not refreshed when it should have been: loaded before close, refresh
               overdue inside the repair window, never re-pulled after the window, or a
               current period whose last load is too old. NEVER present as current.
  UNVERIFIED   never loaded, last load did not reconcile, latest refresh failed, or the
               source reports a closed period incomplete
"""
from datetime import date, datetime, timedelta

REPAIR_WINDOW_DAYS = 42           # == r365_financial_ingest.OVERLAP_DAYS
MAX_REFRESH_AGE_HOURS = 30        # one missed daily pull (07:00 Chicago) is tolerated

FINAL, PROVISIONAL, STALE, UNVERIFIED = "FINAL", "PROVISIONAL", "STALE", "UNVERIFIED"


def period_refresh_state(*, period_end: date, today: date, now: datetime,
                         loaded_at: datetime | None, period_open: bool,
                         source_complete: bool | None, reconciliation_ok: bool | None,
                         refresh_failed: bool = False,
                         repair_window_days: int = REPAIR_WINDOW_DAYS,
                         max_age_hours: float = MAX_REFRESH_AGE_HOURS) -> tuple[str, str, str]:
    """-> (state, code, reason). `code` is stable for programs (monitor categories, tests). `now` and `loaded_at` must be timezone-aware; `today` is the
    America/Chicago calendar date (fiscal periods are Chicago dates)."""
    if loaded_at is None:
        return UNVERIFIED, "never_loaded", "never loaded"
    if reconciliation_ok is False:
        return UNVERIFIED, "not_reconciled", "last load did not reconcile summary totals with line detail"
    age_h = (now - loaded_at).total_seconds() / 3600
    loaded_day = loaded_at.date() if loaded_at.tzinfo is None else _chicago_date(loaded_at)
    if period_open:
        if age_h > max_age_hours:
            return STALE, "current_stale", (f"current period last loaded {age_h:.0f} h ago "
                           f"(expected at most {max_age_hours:.0f} h)")
        return PROVISIONAL, "current_ok", "current period: postings still arriving"
    if refresh_failed:
        return UNVERIFIED, "refresh_failed", "latest refresh failed; figures are from an earlier load"
    if loaded_day <= period_end:
        return STALE, "loaded_before_close", f"last loaded {loaded_day} -- before the period closed on {period_end}"
    if source_complete is False:
        return UNVERIFIED, "source_incomplete", "source reports the closed period incomplete"
    window_end = period_end + timedelta(days=repair_window_days)
    if today <= window_end:
        if age_h > max_age_hours:
            return STALE, "window_overdue", (f"inside the source's correction window (until {window_end}) but "
                           f"last refreshed {age_h:.0f} h ago")
        return PROVISIONAL, "window_ok", f"closed; source may still correct it until {window_end}"
    # The scheduled ingest's LAST daily pull of a period happens on window_end itself
    # (it targets periods with end >= today - 42); the source's own repair window is
    # 35 days, so a load on window_end already postdates every source correction.
    if loaded_day < window_end:
        return STALE, "not_refreshed_after_window", (f"last loaded {loaded_day}, before the source's correction window "
                       f"ended on {window_end}; late postings may be missing")
    return FINAL, "final", f"loaded {loaded_day}, after the correction window ended on {window_end}"


def _chicago_date(ts: datetime) -> date:
    from zoneinfo import ZoneInfo
    return ts.astimezone(ZoneInfo("America/Chicago")).date()
