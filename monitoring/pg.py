"""Bounded, read-only PostgreSQL access.

Every session the monitor opens is read-only three ways:
  * the server option default_transaction_read_only=on (set at connect time),
  * psycopg2 set_session(readonly=True),
  * a startup assertion that SHOW transaction_read_only is 'on' -- if any of
    that ever stops holding, the monitor refuses to run a query.
Every statement is bounded by statement_timeout and lock_timeout, and the
session by idle_in_transaction_session_timeout, so a slow check can never hold
locks against ingestion or the API. Every transaction is rolled back.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras


class MonitorDbError(RuntimeError):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def connect(cfg: dict):
    p = cfg["postgres"]
    user = _env(p["user_env"])
    password = _env(p["password_env"])
    if not user:
        raise MonitorDbError(f"{p['user_env']} is not set")
    opts = (f"-c default_transaction_read_only=on "
            f"-c statement_timeout={int(p['statement_timeout_ms'])} "
            f"-c lock_timeout={int(p['lock_timeout_ms'])} "
            f"-c idle_in_transaction_session_timeout=60000 "
            f"-c application_name=laynes_monitor")
    conn = psycopg2.connect(host=_env(p["host_env"], "localhost"),
                            port=_env(p["port_env"], "5432"),
                            dbname=_env(p["dbname_env"], "laynes"),
                            user=user, password=password,
                            connect_timeout=int(p["connect_timeout_s"]),
                            options=opts)
    conn.set_session(readonly=True, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        if cur.fetchone()[0] != "on":
            conn.close()
            raise MonitorDbError("session is not read-only; refusing to query")
    conn.rollback()
    return conn


@contextmanager
def session(cfg: dict):
    conn = connect(cfg)
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def rows(conn, sql: str, params=None) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or {})
        out = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    return out
