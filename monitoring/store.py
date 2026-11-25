"""Monitor-owned state: incident history, latest check results, runs, outbox.

Kept in its own SQLite file, separate from every financial, HME and Revel table
and from the PostgreSQL database being monitored -- so the monitor keeps a
record (and can say "the database is unreachable") even when PostgreSQL is the
thing that failed, and nothing it writes can touch business data.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

OPEN, ACKNOWLEDGED, RESOLVED = "OPEN", "ACKNOWLEDGED", "RESOLVED"
ACTIVE = (OPEN, ACKNOWLEDGED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id               TEXT PRIMARY KEY,
    dedup_key        TEXT NOT NULL,
    source           TEXT NOT NULL,
    check_name       TEXT NOT NULL,
    store_id         INTEGER,
    store_name       TEXT,
    business_date    TEXT,
    qualifier        TEXT,
    severity         TEXT NOT NULL CHECK (severity IN ('CRITICAL','HIGH','WARNING')),
    status           TEXT NOT NULL CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
    title            TEXT NOT NULL,
    category         TEXT,
    expected         TEXT,
    actual           TEXT,
    difference       TEXT,
    evidence         TEXT NOT NULL DEFAULT '{}',
    actions          TEXT NOT NULL DEFAULT '[]',
    fingerprint      TEXT NOT NULL,
    first_detected_at TEXT NOT NULL,
    last_checked_at  TEXT NOT NULL,
    last_changed_at  TEXT NOT NULL,
    check_count      INTEGER NOT NULL DEFAULT 1,
    acknowledged_at  TEXT,
    acknowledged_by  TEXT,
    ack_note         TEXT,
    resolved_at      TEXT,
    resolution       TEXT,
    notified         INTEGER NOT NULL DEFAULT 0   -- an OPEN/ESCALATED alert was sent
);
-- At most ONE active incident per issue: the deduplication guarantee.
CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_active
    ON incidents(dedup_key) WHERE status <> 'RESOLVED';
CREATE INDEX IF NOT EXISTS incidents_status ON incidents(status, severity);
CREATE INDEX IF NOT EXISTS incidents_key ON incidents(dedup_key, first_detected_at);

CREATE TABLE IF NOT EXISTS incident_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    at          TEXT NOT NULL,
    event       TEXT NOT NULL,      -- OPENED UPDATED ESCALATED DEESCALATED ACKNOWLEDGED RESOLVED
    actor       TEXT NOT NULL DEFAULT 'monitor',
    detail      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS incident_events_inc ON incident_events(incident_id, id);

-- Latest verdict for every key any check evaluated (OK / PENDING / ACCEPTED
-- included), so the dashboard can show per-store status, not only failures.
CREATE TABLE IF NOT EXISTS latest_results (
    dedup_key     TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    check_name    TEXT NOT NULL,
    store_id      INTEGER,
    store_name    TEXT,
    business_date TEXT,
    qualifier     TEXT,
    state         TEXT NOT NULL,
    severity      TEXT NOT NULL,
    title         TEXT,
    category      TEXT,
    difference    TEXT,
    checked_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS latest_results_src ON latest_results(source, store_id, business_date);

CREATE TABLE IF NOT EXISTS source_status (
    source          TEXT PRIMARY KEY,
    last_success_at TEXT,
    detail          TEXT NOT NULL DEFAULT '{}',
    checked_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tier          TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    ok            INTEGER,
    checks_run    INTEGER,
    checks_failed INTEGER,
    results       INTEGER,
    error         TEXT,
    version       TEXT
);
CREATE INDEX IF NOT EXISTS monitor_runs_tier ON monitor_runs(tier, id);

-- Per-check bookkeeping for min_interval_minutes and crash reporting.
CREATE TABLE IF NOT EXISTS check_runs (
    check_name      TEXT PRIMARY KEY,
    last_started_at TEXT,
    last_ok_at      TEXT,
    last_error      TEXT,
    last_duration_s REAL
);

CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,     -- OPENED ESCALATED RESOLVED SUMMARY MONITOR
    severity     TEXT,
    incident_ids TEXT NOT NULL DEFAULT '[]',
    channel      TEXT NOT NULL,
    body         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('PENDING','SENT','DRY_RUN','FAILED')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    sent_at      TEXT
);
CREATE INDEX IF NOT EXISTS notifications_pending ON notifications(status, id);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def new_incident_id(now: datetime) -> str:
    return f"INC-{now.astimezone(timezone.utc):%Y%m%d}-{secrets.token_hex(3).upper()}"


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fresh = not os.path.exists(path)
        else:
            fresh = False
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout = 10000")
        self.db.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
            if fresh:
                os.chmod(path, 0o640)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # ── transactions ──────────────────────────────────────────────────────
    def begin(self):
        self.db.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.db.execute("COMMIT")

    def rollback(self):
        if self.db.in_transaction:
            self.db.execute("ROLLBACK")

    # ── incidents ─────────────────────────────────────────────────────────
    def active_incident(self, key: str):
        return self.db.execute(
            "SELECT * FROM incidents WHERE dedup_key = ? AND status <> 'RESOLVED'",
            (key,)).fetchone()

    def active_incidents(self, source: str | None = None, check: str | None = None):
        q, p = "SELECT * FROM incidents WHERE status <> 'RESOLVED'", []
        if source:
            q += " AND source = ?"
            p.append(source)
        if check:
            q += " AND check_name = ?"
            p.append(check)
        return self.db.execute(q + " ORDER BY first_detected_at", p).fetchall()

    def get(self, incident_id: str):
        return self.db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()

    def open_incident(self, r, now: datetime) -> str:
        iid = new_incident_id(now)
        t = iso(now)
        self.db.execute(
            """INSERT INTO incidents (id, dedup_key, source, check_name, store_id, store_name,
                 business_date, qualifier, severity, status, title, category, expected, actual,
                 difference, evidence, actions, fingerprint, first_detected_at, last_checked_at,
                 last_changed_at)
               VALUES (?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?)""",
            (iid, r.key, r.source, r.check, r.store_id, r.store_name, r.business_date,
             r.qualifier, r.severity, r.title, r.category, _j(r.expected), _j(r.actual),
             r.difference, _j(r.evidence), _j(r.actions), r.fingerprint(), t, t, t))
        self.event(iid, now, "OPENED", {"severity": r.severity, "difference": r.difference})
        return iid

    def touch(self, iid: str, now: datetime):
        self.db.execute("UPDATE incidents SET last_checked_at = ?, check_count = check_count + 1 "
                        "WHERE id = ?", (iso(now), iid))

    def update_incident(self, iid: str, r, now: datetime, event: str, prev: dict):
        t = iso(now)
        self.db.execute(
            """UPDATE incidents SET severity=?, title=?, category=?, expected=?, actual=?,
                 difference=?, evidence=?, actions=?, fingerprint=?, last_checked_at=?,
                 last_changed_at=?, check_count = check_count + 1, store_name=COALESCE(?, store_name)
               WHERE id=?""",
            (r.severity, r.title, r.category, _j(r.expected), _j(r.actual), r.difference,
             _j(r.evidence), _j(r.actions), r.fingerprint(), t, t, r.store_name, iid))
        self.event(iid, now, event, {"from": prev, "to": {"severity": r.severity,
                                                          "difference": r.difference,
                                                          "category": r.category}})

    def resolve(self, iid: str, now: datetime, resolution: str, actor: str = "monitor"):
        t = iso(now)
        self.db.execute("UPDATE incidents SET status='RESOLVED', resolved_at=?, resolution=?, "
                        "last_checked_at=?, last_changed_at=? WHERE id=? AND status <> 'RESOLVED'",
                        (t, resolution, t, t, iid))
        self.event(iid, now, "RESOLVED", {"resolution": resolution}, actor=actor)

    def acknowledge(self, iid: str, now: datetime, actor: str, note: str | None = None) -> bool:
        cur = self.db.execute(
            "UPDATE incidents SET status='ACKNOWLEDGED', acknowledged_at=?, acknowledged_by=?, "
            "ack_note=? WHERE id=? AND status='OPEN'", (iso(now), actor, note, iid))
        if cur.rowcount:
            self.event(iid, now, "ACKNOWLEDGED", {"note": note}, actor=actor)
        return bool(cur.rowcount)

    def event(self, iid: str, now: datetime, event: str, detail: dict, actor: str = "monitor"):
        self.db.execute("INSERT INTO incident_events (incident_id, at, event, actor, detail) "
                        "VALUES (?,?,?,?,?)", (iid, iso(now), event, actor, _j(detail)))

    def events(self, iid: str):
        return self.db.execute("SELECT * FROM incident_events WHERE incident_id = ? ORDER BY id",
                               (iid,)).fetchall()

    def history(self, limit: int = 200, status: str | None = None, source: str | None = None):
        q, p = "SELECT * FROM incidents WHERE 1=1", []
        if status:
            q += " AND status = ?"
            p.append(status)
        if source:
            q += " AND source = ?"
            p.append(source)
        q += " ORDER BY last_changed_at DESC LIMIT ?"
        p.append(int(limit))
        return self.db.execute(q, p).fetchall()

    # ── latest results ────────────────────────────────────────────────────
    def record_result(self, r, now: datetime):
        self.db.execute(
            """INSERT INTO latest_results (dedup_key, source, check_name, store_id, store_name,
                 business_date, qualifier, state, severity, title, category, difference, checked_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dedup_key) DO UPDATE SET state=excluded.state,
                 severity=excluded.severity, title=excluded.title, category=excluded.category,
                 difference=excluded.difference, checked_at=excluded.checked_at,
                 store_name=COALESCE(excluded.store_name, latest_results.store_name)""",
            (r.key, r.source, r.check, r.store_id, r.store_name, r.business_date, r.qualifier,
             r.state, r.severity, r.title, r.category, r.difference, iso(now)))

    def prune_results(self, now: datetime, days: int = 35):
        self.db.execute("DELETE FROM latest_results WHERE checked_at < ?",
                        (iso(now - timedelta(days=days)),))

    def latest_results(self, source: str | None = None):
        q, p = "SELECT * FROM latest_results", []
        if source:
            q += " WHERE source = ?"
            p.append(source)
        return self.db.execute(q + " ORDER BY source, store_id, business_date DESC", p).fetchall()

    def set_source_status(self, source: str, last_success_at: str | None, detail: dict,
                          now: datetime):
        self.db.execute(
            """INSERT INTO source_status (source, last_success_at, detail, checked_at)
               VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                 last_success_at=excluded.last_success_at, detail=excluded.detail,
                 checked_at=excluded.checked_at""",
            (source, last_success_at, _j(detail), iso(now)))

    def source_statuses(self):
        return self.db.execute("SELECT * FROM source_status ORDER BY source").fetchall()

    # ── runs ──────────────────────────────────────────────────────────────
    def start_run(self, tier: str, now: datetime, version: str) -> int:
        cur = self.db.execute("INSERT INTO monitor_runs (tier, started_at, version) VALUES (?,?,?)",
                              (tier, iso(now), version))
        return cur.lastrowid

    def finish_run(self, run_id: int, now: datetime, ok: bool, checks_run: int,
                   checks_failed: int, results: int, error: str | None = None):
        self.db.execute("UPDATE monitor_runs SET finished_at=?, ok=?, checks_run=?, "
                        "checks_failed=?, results=?, error=? WHERE id=?",
                        (iso(now), int(ok), checks_run, checks_failed, results, error, run_id))

    def last_run(self, tier: str, ok_only: bool = False):
        q = "SELECT * FROM monitor_runs WHERE tier = ? AND finished_at IS NOT NULL"
        if ok_only:
            q += " AND ok = 1"
        return self.db.execute(q + " ORDER BY id DESC LIMIT 1", (tier,)).fetchone()

    def recent_runs(self, limit: int = 20):
        return self.db.execute("SELECT * FROM monitor_runs ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()

    def mark_notified(self, iid: str):
        self.db.execute("UPDATE incidents SET notified = 1 WHERE id = ?", (iid,))

    # ── per-check bookkeeping ─────────────────────────────────────────────
    def check_run(self, name: str):
        return self.db.execute("SELECT * FROM check_runs WHERE check_name = ?", (name,)).fetchone()

    def record_check_run(self, name: str, started: datetime, ok: bool, duration_s: float,
                         error: str | None):
        self.db.execute(
            """INSERT INTO check_runs (check_name, last_started_at, last_ok_at, last_error,
                 last_duration_s) VALUES (?,?,?,?,?)
               ON CONFLICT(check_name) DO UPDATE SET last_started_at=excluded.last_started_at,
                 last_ok_at=COALESCE(excluded.last_ok_at, check_runs.last_ok_at),
                 last_error=excluded.last_error, last_duration_s=excluded.last_duration_s""",
            (name, iso(started), iso(started) if ok else None, error, round(duration_s, 3)))

    def check_runs(self):
        return self.db.execute("SELECT * FROM check_runs ORDER BY check_name").fetchall()

    # ── notification outbox ───────────────────────────────────────────────
    def enqueue(self, now: datetime, kind: str, severity: str | None, incident_ids: list[str],
                channel: str, body: str) -> int:
        cur = self.db.execute(
            "INSERT INTO notifications (created_at, kind, severity, incident_ids, channel, body, "
            "status) VALUES (?,?,?,?,?,?,'PENDING')",
            (iso(now), kind, severity, _j(incident_ids), channel, body))
        return cur.lastrowid

    def pending_notifications(self, max_attempts: int):
        return self.db.execute("SELECT * FROM notifications WHERE status = 'PENDING' "
                               "AND attempts < ? ORDER BY id", (max_attempts,)).fetchall()

    def mark_notification(self, nid: int, status: str, now: datetime, error: str | None = None):
        self.db.execute("UPDATE notifications SET status=?, attempts=attempts+1, last_error=?, "
                        "sent_at=CASE WHEN ? IN ('SENT','DRY_RUN') THEN ? ELSE sent_at END "
                        "WHERE id=?", (status, error, status, iso(now), nid))

    def bump_attempt(self, nid: int, error: str):
        self.db.execute("UPDATE notifications SET attempts=attempts+1, last_error=? WHERE id=?",
                        (error, nid))

    def notifications(self, limit: int = 50):
        return self.db.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT ?",
                               (limit,)).fetchall()


def _j(v) -> str:
    return json.dumps(v, default=str, sort_keys=True)


def row_dict(row) -> dict:
    if row is None:
        return None
    d = dict(row)
    for k in ("evidence", "actions", "expected", "actual", "detail", "incident_ids"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d
