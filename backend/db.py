"""Database access for the API.

Same connection contract and the same SQL the Streamlit app used -- these
helpers were lifted out of admin_chat.py rather than reinvented, so behaviour
is identical. They live here because admin_chat.py imports Streamlit at module
scope and therefore cannot be imported by a web process.
"""

from __future__ import annotations

import os

import psycopg2
import psycopg2.extras


def rw_conn():
    """Read/write connection used for auth and conversation persistence.

    NOTE this is deliberately NOT the analytics role. Business questions are
    answered by chat_sql, which opens its own read-only laynes_ro connection
    and is bound by the relation allowlist and grants. Nothing in this module
    executes user-supplied SQL.
    """
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "laynes_user"),
        password=os.environ["DB_PASS"],
    )


def query(sql, params=None, *, fetch="all", commit=False):
    conn = rw_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            row = None
            if fetch == "all":
                row = cur.fetchall()
            elif fetch == "one":
                row = cur.fetchone()
        if commit:
            conn.commit()
        return row
    finally:
        conn.close()
