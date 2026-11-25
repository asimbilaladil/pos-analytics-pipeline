"""Check registry.

A check is ``fn(ctx) -> list[CheckResult]``. It reports every key it evaluated
(OK / PENDING / ACCEPTED as well as FAIL) so the engine can resolve incidents
safely. Checks never write anything except through ``ctx.store`` helpers the
engine provides for source status.

Tiers:
  health  every 5 minutes: is everything up?
  data    every 15 minutes: is the data fresh, complete, reconciled?
Heavy checks carry ``min_interval_minutes`` so the 15-minute tier does not
re-run them every time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CheckSpec:
    name: str
    tiers: tuple[str, ...]
    fn: Callable
    min_interval_minutes: int = 0
    needs_pg: bool = False

    @property
    def source(self) -> str:
        """The dashboard source this check reports on (operational state per source)."""
        return SPEC_SOURCES.get(self.name, SPEC_SOURCES.get(self.name.split(".")[0], "monitor"))


SPEC_SOURCES = {"revel": "revel", "hme": "hme", "r365.weekly": "r365",
                "r365.financial": "r365_financial", "chat": "ai_chat", "mcp": "mcp",
                "infra": "infrastructure", "services": "infrastructure", "monitor": "monitor"}


def registry() -> list[CheckSpec]:
    from . import chat, hme, infra, mcp, r365, revel, selfmon, services
    return [
        # health tier
        CheckSpec("infra.postgres", ("health",), infra.postgres, needs_pg=True),
        CheckSpec("services.units", ("health",), services.units),
        CheckSpec("services.http", ("health",), services.http),
        CheckSpec("infra.resources", ("health",), infra.resources),
        CheckSpec("mcp.health", ("health",), mcp.health),
        CheckSpec("chat.stuck_jobs", ("health",), chat.stuck_jobs, needs_pg=True),
        # Each tier checks the OTHER tiers (and the summary): a dead tier is
        # always reported by a live one.
        CheckSpec("monitor.heartbeat", ("health", "data"), selfmon.heartbeat),
        CheckSpec("monitor.notifications", ("health", "data"), selfmon.notifications),
        # data tier
        CheckSpec("services.jobs", ("data",), services.jobs),
        CheckSpec("services.timers", ("data",), services.timers),
        CheckSpec("revel.freshness", ("data",), revel.freshness, needs_pg=True),
        CheckSpec("revel.reconciliation", ("data",), revel.reconciliation,
                  min_interval_minutes=60, needs_pg=True),
        CheckSpec("hme.days", ("data",), hme.days, needs_pg=True),
        CheckSpec("r365.weekly", ("data",), r365.weekly, needs_pg=True),
        CheckSpec("r365.financial", ("data",), r365.financial, min_interval_minutes=360,
                  needs_pg=True),
        CheckSpec("chat.errors", ("data",), chat.errors, needs_pg=True),
        CheckSpec("mcp.tool_errors", ("data",), mcp.tool_errors),
        CheckSpec("infra.backups", ("data",), infra.backups, min_interval_minutes=60),
        CheckSpec("services.tls", ("data",), services.tls, min_interval_minutes=720),
    ]
