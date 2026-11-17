"""Run a view migration as session-local TEMP views, for tests.

CREATE TEMP VIEW objects live in pg_temp, which PostgreSQL searches before
public, so inside ONE connection they shadow the real views of the same name
-- without touching, locking or altering the production objects, and without
any grant. Everything disappears when the connection closes (and the caller
rolls back as well). Only CREATE OR REPLACE VIEW statements are executed;
BEGIN/COMMIT, COMMENT and GRANT statements are dropped.
"""
import re

_STMT = re.compile(r"CREATE OR REPLACE VIEW\s+(\w+)\s+AS(.*?);\s*(?=\n(?:COMMENT|CREATE|COMMIT|GRANT|BEGIN|--|$))",
                   re.S | re.I)


def temp_view_statements(sql_text: str) -> list[tuple[str, str]]:
    body = re.sub(r"--[^\n]*", "", sql_text)
    out = []
    for name, select in _STMT.findall(body + "\n"):
        out.append((name, f"CREATE TEMP VIEW {name} AS{select}"))
    return out


def install(cur, sql_text: str) -> list[str]:
    names = []
    for name, stmt in temp_view_statements(sql_text):
        cur.execute(stmt)
        names.append(name)
    return names
