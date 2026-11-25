"""Daily readiness summary and the overview the dashboard shows.

Both are built ONLY from the monitor's own store (latest results, incidents,
runs) -- no extra production queries -- so the dashboard never loads the
production database and the summary reflects exactly what the checks saw.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .model import SEVERITY_RANK, SOURCE_LABELS
from .store import ACTIVE, Store, row_dict

STORE_SOURCES = ("revel", "hme", "r365")


# Machine-readable states (dashboard, CLI, and the future n8n API).
HEALTHY, PENDING, DEGRADED, FAILED, UNKNOWN, STALE = (
    "HEALTHY", "PENDING", "DEGRADED", "FAILED", "UNKNOWN", "STALE")
STATE_RANK = {HEALTHY: 0, PENDING: 1, DEGRADED: 2, UNKNOWN: 3, STALE: 4, FAILED: 5}
STALE_PERIODS = 3          # a check whose last success is > 3 of its periods old is STALE


def _ts(v):
    return datetime.fromisoformat(v) if v else None


def check_states(store: Store, now: datetime, cfg: dict) -> list[dict]:
    """Operational state of every registered check, from check_runs only.

    UNKNOWN   never completed successfully (not run yet, or always failed)
    STALE     last success older than STALE_PERIODS x its expected interval
    DEGRADED  ran successfully recently, but the LATEST attempt crashed or was
              skipped (its incidents are frozen at their last verdict)
    HEALTHY   latest attempt succeeded and is recent
    """
    from .checks import registry
    period = cfg["monitor"]["tier_period_minutes"]
    runs = {r["check_name"]: dict(r) for r in store.check_runs()}
    out = []
    for spec in registry():
        interval = max([spec.min_interval_minutes] + [period[t] for t in spec.tiers])
        stale_after = timedelta(minutes=interval * STALE_PERIODS)
        r = runs.get(spec.name) or {}
        last_ok, last_try = _ts(r.get("last_ok_at")), _ts(r.get("last_started_at"))
        if last_ok is None:
            state, why = UNKNOWN, ("never ran" if last_try is None else
                                   f"never succeeded: {(r.get('last_error') or '')[:160]}")
        elif now - last_ok > stale_after:
            state, why = STALE, f"last success older than {interval * STALE_PERIODS} min"
        elif r.get("last_error"):
            state, why = DEGRADED, f"latest attempt failed: {r['last_error'][:160]}"
        else:
            state, why = HEALTHY, None
        out.append({"check": spec.name, "source": spec.source, "tiers": list(spec.tiers),
                    "state": state, "reason": why,
                    "last_success_at": r.get("last_ok_at"),
                    "last_attempt_at": r.get("last_started_at"),
                    "last_error": r.get("last_error"),
                    "last_duration_s": r.get("last_duration_s"),
                    "stale_after_minutes": interval * STALE_PERIODS})
    return out


def _worst(states) -> str:
    states = list(states)
    return max(states, key=STATE_RANK.__getitem__) if states else UNKNOWN


def monitor_status(store: Store, now: datetime, cfg: dict) -> dict:
    """Operational health of the MONITOR itself (not of the business data)."""
    tiers = {}
    m = cfg["monitor"]
    for tier, max_min in m["heartbeat_max_minutes"].items():
        last_ok = store.last_run(tier, ok_only=True)
        last = store.last_attempt(tier)
        if tier in m["disabled_tiers"]:
            state = HEALTHY
        elif last_ok is None:
            state = UNKNOWN
        elif now - _ts(last_ok["finished_at"]) > timedelta(minutes=max_min):
            state = STALE
        elif last is not None and last["status"] in ("DEGRADED", "FAILED"):
            state = DEGRADED
        else:
            state = HEALTHY
        tiers[tier] = {"state": state, "disabled": tier in m["disabled_tiers"],
                       "max_minutes": max_min,
                       "last_success_at": last_ok["finished_at"] if last_ok else None,
                       "last_attempt_at": last["started_at"] if last else None,
                       "last_attempt_status": last["status"] if last else None,
                       "last_attempt_error": last["error"] if last else None,
                       "last_run_id": last["id"] if last else None}
    n = store.notification_counts(now)
    delivery = (FAILED if n["failed_24h"] else DEGRADED if n["RETRYING"] else HEALTHY)
    return {"state": _worst([t["state"] for t in tiers.values()] + [delivery]),
            "tiers": tiers, "notifications": {**n, "state": delivery}}


def overview(store: Store, now: datetime, cfg: dict | None = None) -> dict:
    from . import config as mcfg
    cfg = cfg or mcfg.DEFAULTS
    incidents = [row_dict(r) for r in store.active_incidents()]
    incidents.sort(key=lambda i: (-SEVERITY_RANK[i["severity"]], i["first_detected_at"]))
    results = [dict(r) for r in store.latest_results()]
    checks = check_states(store, now, cfg)

    counts: dict[str, Counter] = {}
    for r in results:
        counts.setdefault(r["source"], Counter())[r["state"]] += 1
    src_status = {r["source"]: row_dict(r) for r in store.source_statuses()}
    open_by_src = Counter(i["source"] for i in incidents)
    worst_by_src: dict[str, str] = {}
    for i in incidents:
        w = worst_by_src.get(i["source"])
        if w is None or SEVERITY_RANK[i["severity"]] > SEVERITY_RANK[w]:
            worst_by_src[i["source"]] = i["severity"]
    checks_by_src: dict[str, list] = defaultdict(list)
    for c in checks:
        checks_by_src[c["source"]].append(c)

    source_rows = []
    # EVERY known source is listed, even with no results: absence is UNKNOWN, never OK.
    for src in SOURCE_LABELS:
        st = src_status.get(src) or {}
        cs = checks_by_src.get(src, [])
        c = counts.get(src, Counter())
        mon_states = [x["state"] for x in cs]
        if not mon_states or all(x == UNKNOWN for x in mon_states):
            monitoring_state = UNKNOWN
        elif STALE in mon_states or UNKNOWN in mon_states:
            monitoring_state = STALE
        elif DEGRADED in mon_states:
            monitoring_state = DEGRADED
        else:
            monitoring_state = HEALTHY
        worst = worst_by_src.get(src)
        if worst in ("CRITICAL", "HIGH"):
            data_state = FAILED
        elif worst == "WARNING":
            data_state = DEGRADED
        elif not c:
            data_state = UNKNOWN
        elif c.get("PENDING"):
            data_state = PENDING
        else:
            data_state = HEALTHY
        if data_state == FAILED:
            state = FAILED
        elif monitoring_state in (UNKNOWN, STALE):
            state = monitoring_state        # no current evidence: never HEALTHY
        elif DEGRADED in (monitoring_state, data_state):
            state = DEGRADED
        else:
            state = data_state
        oks = [x["last_success_at"] for x in cs if x["last_success_at"]]
        tries = [x["last_attempt_at"] for x in cs if x["last_attempt_at"]]
        source_rows.append({
            "source": src, "label": SOURCE_LABELS[src],
            "state": state, "data_state": data_state, "monitoring_state": monitoring_state,
            "worst_open_severity": worst,
            "open_incidents": open_by_src.get(src, 0),
            "counts": dict(c),
            "last_success_at": st.get("last_success_at"),     # the SOURCE's own latest sync
            "detail": st.get("detail"),
            # The MONITOR's checks of this source:
            "last_successful_check_at": max(oks) if oks else None,
            "oldest_successful_check_at": min(oks) if len(oks) == len(cs) and oks else None,
            "last_attempted_check_at": max(tries) if tries else None,
            "checks": [x["check"] for x in cs],
        })

    # Per-store matrix: newest business date per (store, source), worst state.
    matrix: dict[int, dict] = defaultdict(lambda: {"store_name": None, "sources": {}})
    rank = {"FAIL": 3, "PENDING": 2, "ACCEPTED": 1, "OK": 0}
    for r in results:
        if r["source"] not in STORE_SOURCES or r["store_id"] is None:
            continue
        m = matrix[r["store_id"]]
        m["store_name"] = m["store_name"] or r["store_name"]
        cur = m["sources"].get(r["source"])
        day = r["business_date"] or ""
        if cur is None or day > cur["business_date"] or (
                day == cur["business_date"] and rank[r["state"]] > rank[cur["state"]]):
            m["sources"][r["source"]] = {"business_date": day, "state": r["state"],
                                         "severity": r["severity"], "title": r["title"],
                                         "difference": r["difference"], "check": r["check_name"]}
    # Any open incident for the store makes that cell show it, whatever the date.
    for i in incidents:
        if i["store_id"] is None or i["source"] not in STORE_SOURCES:
            continue
        cell = matrix[i["store_id"]]["sources"].setdefault(i["source"], {})
        cell["open_incidents"] = cell.get("open_incidents", 0) + 1
        prev = cell.get("worst_open")
        if prev is None or SEVERITY_RANK[i["severity"]] > SEVERITY_RANK[prev]:
            cell["worst_open"] = i["severity"]
        matrix[i["store_id"]]["store_name"] = matrix[i["store_id"]]["store_name"] or i["store_name"]

    recon = [{"id": i["id"], "source": i["source"], "store_name": i["store_name"],
              "business_date": i["business_date"], "difference": i["difference"],
              "category": i["category"], "severity": i["severity"], "status": i["status"]}
             for i in incidents if i["difference"]]
    runs = [dict(r) for r in store.recent_runs(30)]
    last_by_tier = {}
    for r in runs:
        last_by_tier.setdefault(r["tier"], r)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "sources": source_rows,
        "stores": [{"store_id": k, **v} for k, v in sorted(matrix.items())],
        "open_incidents": incidents,
        "open_counts": dict(Counter(i["severity"] for i in incidents)),
        "reconciliation_differences": recon,
        "last_runs": last_by_tier,
        "checks": checks,
        "monitor": monitor_status(store, now, cfg),
        "states": list(STATE_RANK),
    }


def daily_summary_text(store: Store, now: datetime, dashboard_url: str | None = None,
                       cfg: dict | None = None) -> str:
    ov = overview(store, now, cfg)
    oc = ov["open_counts"]
    lines = ["Laynes Intelligence — Daily data readiness", f"As of {now:%Y-%m-%d %H:%M} UTC", ""]
    for s in ov["sources"]:
        c = s["counts"]
        lines.append(f"{s['label']}: {s['state']} — ok {c.get('OK', 0)}, "
                     f"pending {c.get('PENDING', 0)}, failing {c.get('FAIL', 0)}"
                     + (f"; last sync {s['last_success_at'][:16]}" if s["last_success_at"] else ""))
    mon = ov["monitor"]
    lines.append(f"Monitor itself: {mon['state']}" + "".join(
        f"; {t} {v['state']}" for t, v in mon["tiers"].items() if v["state"] != "HEALTHY")
        + (f"; {mon['notifications']['failed_24h']} undelivered alert(s) in 24h"
           if mon["notifications"]["failed_24h"] else ""))
    lines.append("")
    total = sum(oc.values())
    lines.append(f"Open incidents: {total}" + (
        " (" + ", ".join(f"{k} {oc[k]}" for k in ("CRITICAL", "HIGH", "WARNING") if oc.get(k)) + ")"
        if total else ""))
    for i in ov["open_incidents"][:12]:
        where = i["store_name"] or i["qualifier"] or "network"
        lines.append(f"- [{i['severity']}] {where} {i['business_date'] or ''} — {i['title']}"
                     + (f" ({i['difference']})" if i["difference"] else "")
                     + (" [ack]" if i["status"] == "ACKNOWLEDGED" else ""))
    if total > 12:
        lines.append(f"… and {total - 12} more on the dashboard")
    if dashboard_url:
        lines += ["", f"Dashboard: {dashboard_url}"]
    return "\n".join(lines)[:3500]


def send_daily_summary(store: Store, cfg: dict, now: datetime) -> int:
    body = daily_summary_text(store, now, cfg["notify"].get("dashboard_url"), cfg)
    store.begin()
    n = 0
    for ch in cfg["notify"]["channels"]:
        store.enqueue(now, "SUMMARY", None, [i["id"] for i in store.active_incidents()], ch, body)
        n += 1
    store.commit()
    return n


__all__ = ["overview", "daily_summary_text", "send_daily_summary", "ACTIVE"]
