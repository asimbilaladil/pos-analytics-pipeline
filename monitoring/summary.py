"""Daily readiness summary and the overview the dashboard shows.

Both are built ONLY from the monitor's own store (latest results, incidents,
runs) -- no extra production queries -- so the dashboard never loads the
production database and the summary reflects exactly what the checks saw.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from .model import SEVERITY_RANK, SOURCE_LABELS
from .store import ACTIVE, Store, row_dict

STORE_SOURCES = ("revel", "hme", "r365")


def overview(store: Store, now: datetime) -> dict:
    incidents = [row_dict(r) for r in store.active_incidents()]
    incidents.sort(key=lambda i: (-SEVERITY_RANK[i["severity"]], i["first_detected_at"]))
    results = [dict(r) for r in store.latest_results()]

    sources = {}
    for r in results:
        s = sources.setdefault(r["source"], Counter())
        s[r["state"]] += 1
    src_status = {r["source"]: row_dict(r) for r in store.source_statuses()}
    open_by_src = Counter(i["source"] for i in incidents)
    worst_by_src: dict[str, str] = {}
    for i in incidents:
        w = worst_by_src.get(i["source"])
        if w is None or SEVERITY_RANK[i["severity"]] > SEVERITY_RANK[w]:
            worst_by_src[i["source"]] = i["severity"]
    source_rows = []
    for src in sorted(set(sources) | set(src_status) | set(open_by_src)):
        if src not in SOURCE_LABELS:
            continue
        st = src_status.get(src) or {}
        source_rows.append({
            "source": src, "label": SOURCE_LABELS[src],
            "health": worst_by_src.get(src) or ("PENDING" if sources.get(src, {}).get("PENDING")
                                                and not sources.get(src, {}).get("OK") else "OK"),
            "open_incidents": open_by_src.get(src, 0),
            "counts": dict(sources.get(src, {})),
            "last_success_at": st.get("last_success_at"),
            "detail": st.get("detail"),
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
        "checks": [dict(r) for r in store.check_runs()],
    }


def daily_summary_text(store: Store, now: datetime, dashboard_url: str | None = None) -> str:
    ov = overview(store, now)
    oc = ov["open_counts"]
    lines = ["Laynes Intelligence — Daily data readiness", f"As of {now:%Y-%m-%d %H:%M} UTC", ""]
    for s in ov["sources"]:
        c = s["counts"]
        lines.append(f"{s['label']}: {s['health']} — ok {c.get('OK', 0)}, "
                     f"pending {c.get('PENDING', 0)}, failing {c.get('FAIL', 0)}"
                     + (f"; last sync {s['last_success_at'][:16]}" if s["last_success_at"] else ""))
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
    body = daily_summary_text(store, now, cfg["notify"].get("dashboard_url"))
    store.begin()
    n = 0
    for ch in cfg["notify"]["channels"]:
        store.enqueue(now, "SUMMARY", None, [i["id"] for i in store.active_incidents()], ch, body)
        n += 1
    store.commit()
    return n


__all__ = ["overview", "daily_summary_text", "send_daily_summary", "ACTIVE"]
