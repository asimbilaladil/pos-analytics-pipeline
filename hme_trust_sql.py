"""Deterministic HME store-day trust for the assistant's own SQL (migration 65).

Why this module exists
----------------------
The chat trust gate judges a SCOPE. For a network scope, an unverified
store-day (e.g. LCF Mission Bend 2026-11-03 / 11-06: PARTIAL /
RECONCILIATION_MISMATCH while the network day reconciled) is only a WARNING,
so the statement runs. A warning is a request to the model, not a control:
the model's SQL could still sum that row into a "network total", or filter to
that one store while declaring a network scope and read the row the
single-store gate would have refused.

How it works
------------
``confine_to_verified`` runs inside chat_sql.run_sql, on every statement, after
validation. It parses the SELECT with pglast and replaces EVERY reference to an
HME store-day fact view with a subselect that keeps only store-days graded
VERIFIED by v_hme_store_day_trust_llm:

    FROM v_hme_store_daily_llm d
      ->  FROM (SELECT _f.* FROM public.v_hme_store_daily_llm _f
                 WHERE EXISTS (SELECT 1 FROM public.v_hme_store_day_trust_llm _t
                                WHERE _t.establishment_id = _f.establishment_id
                                  AND _t.business_date = _f.business_date
                                  AND _t.trust_status = 'VERIFIED')) AS d

The filter is applied by the database however the view is joined, nested,
aliased, unioned or hidden in a CTE or sub-select, and it does not depend on
the statement having a WHERE clause or on the model following any instruction.

Fail closed:
  * the predicate is positive (EXISTS ... VERIFIED): a store-day with no trust
    row is excluded, never assumed verified;
  * if the trust view is missing or not granted, the statement errors and
    nothing is returned;
  * a WITH name that reuses a fact view's name is refused, and after the
    rewrite no bare reference to a fact view may remain.

Only the two store-day FACT views are rewritten. Status/reference views
(day completeness, store status, goal history, the trust view itself) carry no
store-day figures and are unchanged, and so are the server's own gate queries,
which do not go through run_sql.
"""

from __future__ import annotations

from pglast import ast, parse_sql
from pglast.stream import RawStream

TRUST_VIEW = "v_hme_store_day_trust_llm"
FACT_VIEWS = frozenset({"v_hme_store_daily_llm", "v_hme_outliers_daily_llm"})


class HmeTrustSqlError(ValueError):
    """The statement could not be proven confined to verified HME store-days."""


def _verified_subselect(rel: str, alias: str, colnames) -> ast.RangeSubselect:
    inner = parse_sql(
        f'SELECT _f.* FROM public."{rel}" _f WHERE EXISTS ('
        f'SELECT 1 FROM public."{TRUST_VIEW}" _t '
        f"WHERE _t.establishment_id = _f.establishment_id "
        f"AND _t.business_date = _f.business_date "
        f"AND _t.trust_status = 'VERIFIED')")[0].stmt
    return ast.RangeSubselect(subquery=inner,
                              alias=ast.Alias(aliasname=alias, colnames=colnames))


def _cte_names(node) -> set:
    out: set = set()

    def walk(n):
        if isinstance(n, (list, tuple)):
            for x in n:
                walk(x)
            return
        if not isinstance(n, ast.Node):
            return
        wc = getattr(n, "withClause", None)
        if wc is not None and wc.ctes:
            out.update(c.ctename.lower() for c in wc.ctes)
        for f in n:
            walk(getattr(n, f, None))
    walk(node)
    return out


def reads_fact_view(sql: str) -> bool:
    """True when the statement references an HME store-day fact view."""
    found = []

    def walk(n):
        if isinstance(n, (list, tuple)):
            for x in n:
                walk(x)
            return
        if not isinstance(n, ast.Node):
            return
        if isinstance(n, ast.RangeVar) and (n.relname or "").lower() in FACT_VIEWS:
            found.append(n)
        for f in n:
            walk(getattr(n, f, None))
    walk(parse_sql(sql)[0].stmt)
    return bool(found)


def confine_to_verified(sql: str) -> str:
    """Return `sql` with every HME fact-view read limited to VERIFIED store-days.

    Statements that do not read a fact view are returned unchanged. Raises
    HmeTrustSqlError rather than ever returning a partially rewritten statement.
    """
    stmts = parse_sql(sql)
    if len(stmts) != 1:
        raise HmeTrustSqlError("only one statement is allowed")
    root = stmts[0].stmt
    if not reads_fact_view(sql):
        return sql

    collide = sorted(_cte_names(root) & FACT_VIEWS)
    if collide:
        raise HmeTrustSqlError(
            "a WITH name may not reuse an HME data view's name: " + ", ".join(collide))

    built: list = []

    def rewrite(rv):
        if not isinstance(rv, ast.RangeVar):
            return rv
        rel = (rv.relname or "").lower()
        if rel not in FACT_VIEWS:
            return rv
        if getattr(rv, "sample", None) is not None:
            raise HmeTrustSqlError("TABLESAMPLE is not permitted on HME data views")
        sub = _verified_subselect(rel, rv.alias.aliasname if rv.alias else rel,
                                  rv.alias.colnames if rv.alias else None)
        built.append(sub)
        return sub

    def is_built(n) -> bool:
        return any(n is b for b in built)

    def visit(n):
        if isinstance(n, (list, tuple)):
            for x in n:
                visit(x)
            return
        if not isinstance(n, ast.Node) or is_built(n):
            return
        if isinstance(n, ast.SelectStmt) and n.fromClause:
            n.fromClause = tuple(rewrite(i) for i in n.fromClause)
        elif isinstance(n, ast.JoinExpr):
            n.larg = rewrite(n.larg)
            n.rarg = rewrite(n.rarg)
        for f in n:
            visit(getattr(n, f, None))

    visit(root)

    leftover = []

    def audit(n):
        if isinstance(n, (list, tuple)):
            for x in n:
                audit(x)
            return
        if not isinstance(n, ast.Node) or is_built(n):
            return
        if isinstance(n, ast.RangeVar) and (n.relname or "").lower() in FACT_VIEWS:
            leftover.append(n.relname)
        for f in n:
            audit(getattr(n, f, None))

    audit(root)
    if leftover:
        raise HmeTrustSqlError(
            "could not confine HME data view(s) to verified store-days: "
            + ", ".join(sorted(set(leftover))))
    return RawStream()(root)
