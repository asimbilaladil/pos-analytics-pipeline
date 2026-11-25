"""What a check reports, and how it is keyed.

A check returns one CheckResult per thing it EVALUATED -- passing ones
included. The engine needs the passes: an open incident is resolved only when
the same check re-evaluates the same key and finds it OK. A key a check did not
evaluate this run (out of its lookback, not yet ready, or the check crashed) is
left exactly as it was, so a monitor failure can never "resolve" a real issue.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Severities, most severe first.
CRITICAL, HIGH, WARNING = "CRITICAL", "HIGH", "WARNING"
SEVERITIES = (CRITICAL, HIGH, WARNING)
SEVERITY_RANK = {CRITICAL: 3, HIGH: 2, WARNING: 1}

# Result states.
FAIL = "FAIL"          # a genuine problem: open or keep an incident
OK = "OK"              # evaluated and fine: resolve an open incident
PENDING = "PENDING"    # inside the source's readiness / settling window: no verdict yet
ACCEPTED = "ACCEPTED"  # a known, accepted exclusion (configured): shown, never alerted

STATES = (FAIL, OK, PENDING, ACCEPTED)

# Sources (stable identifiers used in keys, the dashboard and notifications).
SRC_REVEL = "revel"
SRC_HME = "hme"
SRC_R365 = "r365"
SRC_R365_FIN = "r365_financial"
SRC_CHAT = "ai_chat"
SRC_MCP = "mcp"
SRC_INFRA = "infrastructure"
SRC_MONITOR = "monitor"

SOURCE_LABELS = {
    SRC_REVEL: "Revel POS",
    SRC_HME: "HME drive-thru",
    SRC_R365: "R365 operations",
    SRC_R365_FIN: "R365 financials",
    SRC_CHAT: "AI Chat",
    SRC_MCP: "MCP",
    SRC_INFRA: "Infrastructure",
    SRC_MONITOR: "Monitor",
}


def dedup_key(source: str, check: str, store_id: Any = None, business_date: Any = None,
              qualifier: str | None = None) -> str:
    """The identity of an issue. Same key = the same issue, alerted once.

    Built from what the problem IS (source, check, store, business date), never
    from what was measured, so a changing difference updates the open incident
    instead of opening a new one.
    """
    parts = [source, check, "-" if store_id is None else str(store_id),
             "-" if business_date is None else str(business_date)]
    if qualifier:
        parts.append(qualifier)
    return ":".join(parts)


@dataclass
class CheckResult:
    source: str
    check: str
    state: str
    severity: str = WARNING
    title: str = ""
    store_id: int | None = None
    store_name: str | None = None
    business_date: str | None = None        # ISO date, Chicago business day
    qualifier: str | None = None             # extra key part (unit name, week, period)
    expected: Any = None
    actual: Any = None
    difference: str | None = None            # display string, e.g. "+5.42% ($+334.51)"
    category: str | None = None              # e.g. payment_timing, settling, stale
    evidence: dict = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    notify: bool | None = None               # None = follow the severity policy

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"bad state {self.state!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"bad severity {self.severity!r}")

    @property
    def key(self) -> str:
        return dedup_key(self.source, self.check, self.store_id, self.business_date,
                         self.qualifier)

    def fingerprint(self) -> str:
        """Changes only when the issue materially changes (not on every re-check)."""
        material = {"severity": self.severity, "category": self.category,
                    "expected": _plain(self.expected), "actual": _plain(self.actual),
                    "difference": self.difference}
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str)
                              .encode()).hexdigest()[:16]


def _plain(v):
    if isinstance(v, float):
        return round(v, 4)
    return v
