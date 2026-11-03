"""Per-credential store isolation for chat_sql's data-access layer.

Why this module exists
----------------------
The assistant writes its own SQL. A prompt that says "only look at store 6" is
a request, not a control. For an MCP credential restricted to some stores, the
restriction has to hold even if the model (or text injected into a question)
writes SQL that tries to read another store.

How it works
------------
When a StorePolicy is active (a contextvar, so it is scoped to the one
request/thread that set it and invisible to every other caller), chat_sql.run_sql
hands the already-validated SELECT to ``apply_to_sql``. That function parses it
with pglast and replaces EVERY stored-relation reference with a filtered
subselect:

    FROM orders_v2 o      ->   FROM (SELECT * FROM public.orders_v2
                                      WHERE establishment_id IN (6, 7)) AS o

The filter is therefore applied by the database to every table the statement
reads, however it is joined, nested, aliased, unioned or hidden in a CTE or
sub-select. It does not depend on the query containing a WHERE clause.

Default-deny: a relation that has no store column and is not on the explicit
reference list is refused for a restricted credential (for example the review
queues, which carry network-wide revenue). A CTE that reuses the name of a real
relation is refused, so a name can never be used to dodge the rewrite.

When no policy is active (``current()`` is None -- every existing caller,
including the website chat) nothing here runs and behaviour is unchanged.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from pglast import ast, parse_sql
from pglast.stream import RawStream


class StoreScopeError(ValueError):
    """The request is outside the credential's store permissions."""


# Relations that carry establishment_id, keyed by the column to filter on.
_STORE_COLUMN = {
    "features_daily_summary_v2": "establishment_id",
    "features_hourly_v2": "establishment_id",
    "features_product_daily_v2": "establishment_id",
    "order_items_v2": "establishment_id",
    "orders_v2": "establishment_id",
    "weather_daily": "establishment_id",
    "v_entree_coverage": "establishment_id",
    "v_hme_goal_history_llm": "establishment_id",
    "v_hme_outliers_daily_llm": "establishment_id",
    "v_hme_store_daily_llm": "establishment_id",
    "v_labor_daily_context": "establishment_id",
    "v_labor_hourly_context": "establishment_id",
    "v_order_channel_context": "establishment_id",
    "v_order_identity_context": "establishment_id",
    "v_order_items_category_context": "establishment_id",
    "v_order_items_classified": "establishment_id",
    "v_order_loyalty_context": "establishment_id",
    "v_order_payment_summary": "establishment_id",
    "v_orders_classified": "establishment_id",
    "v_orders_payment_classified": "establishment_id",
    "v_orders_time_context": "establishment_id",
    "v_payments_daily_v2": "establishment_id",
    "v_product_category_current": "establishment_id",
    "v_r365_financial_period_llm": "establishment_id",
    "v_r365_financial_transaction_llm": "establishment_id",
    "v_r365_financial_unapproved_transaction_llm": "establishment_id",
    "v_r365_store_daily_llm": "establishment_id",
    "v_r365_store_period_llm": "establishment_id",
    "v_r365_store_weekly_llm": "establishment_id",
    "v_store_cohort": "establishment_id",
    "establishments": "id",
}

# Reference data with no per-store figures. Readable as-is.
_REFERENCE_OK = frozenset({
    "v_ayg_business_periods_llm",       # fiscal calendar
    "v_r365_completeness_llm",          # week coverage (counts, no money)
    "v_r365_financial_coverage_llm",    # period coverage (counts, no money)
    "v_r365_financial_account_llm",     # chart of accounts
    "v_hme_day_completeness_llm",       # day completeness (counts)
    "products",                         # product dimension
})
# Deliberately NOT listed (denied to restricted credentials): relations that
# aggregate across stores and expose network totals -- v_identity_profile,
# v_category_review_queue, v_entree_review_queue.


@dataclass(frozen=True)
class StorePolicy:
    allowed: frozenset  # frozenset[int]

    def __post_init__(self):
        if not self.allowed:
            raise ValueError("a StorePolicy needs at least one store")
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in self.allowed):
            raise ValueError("store ids must be integers")


_POLICY: contextvars.ContextVar = contextvars.ContextVar("store_policy", default=None)


def current() -> StorePolicy | None:
    return _POLICY.get()


def activate(allowed) -> contextvars.Token:
    return _POLICY.set(StorePolicy(frozenset(int(i) for i in allowed)))


def deactivate(token: contextvars.Token) -> None:
    _POLICY.reset(token)


def check_declared_scope(establishment_id) -> None:
    """Gate/metadata entry points: a declared scope must be one permitted store.

    A NULL (all stores) scope is refused because reconciling it reports
    network-wide status and totals the credential may not see.
    """
    pol = current()
    if pol is None:
        return
    if establishment_id is None:
        raise StoreScopeError(
            "this credential is restricted to specific stores: declare a single "
            "permitted establishment_id instead of null (all stores)")
    if int(establishment_id) not in pol.allowed:
        raise StoreScopeError(
            f"establishment_id {establishment_id} is not permitted for this credential")


def _in_list(col: str, ids) -> str:
    return f"{col} IN ({', '.join(str(int(i)) for i in sorted(ids))})"


def _scoped_subselect(rel: str, col: str, ids, alias: str,
                      colnames) -> ast.RangeSubselect:
    inner = parse_sql(
        f'SELECT * FROM public."{rel}" WHERE {_in_list(col, ids)}')[0].stmt
    sub = ast.RangeSubselect(subquery=inner,
                             alias=ast.Alias(aliasname=alias, colnames=colnames))
    return sub


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


def apply_to_sql(sql: str) -> str:
    """Return `sql` with every relation reference confined to the active policy.

    A no-op without a policy. Raises StoreScopeError for anything that cannot be
    proven confined -- it never returns a partially rewritten statement.
    """
    pol = current()
    if pol is None:
        return sql
    stmts = parse_sql(sql)
    if len(stmts) != 1:
        raise StoreScopeError("only one statement is allowed")
    root = stmts[0].stmt

    collide = sorted(_cte_names(root) & (set(_STORE_COLUMN) | _REFERENCE_OK))
    if collide:
        raise StoreScopeError(
            "a WITH name may not reuse a data relation's name: " + ", ".join(collide))
    ctes_all = _cte_names(root)
    built: list = []            # wrappers we create; compared by identity

    def rewrite_rangevar(rv: ast.RangeVar):
        rel = (rv.relname or "").lower()
        if not rv.schemaname and rel in ctes_all:
            return rv                              # query-local CTE: already confined
        if rel in _REFERENCE_OK:
            return rv
        col = _STORE_COLUMN.get(rel)
        if col is None:
            raise StoreScopeError(
                f"relation '{rel}' is not available to a store-restricted credential")
        if getattr(rv, "sample", None) is not None:
            raise StoreScopeError("TABLESAMPLE is not permitted for this credential")
        alias = rv.alias.aliasname if rv.alias else rel
        colnames = rv.alias.colnames if rv.alias else None
        sub = _scoped_subselect(rel, col, pol.allowed, alias, colnames)
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
            return              # never descend into a wrapper we just built
        if isinstance(n, ast.SelectStmt) and n.fromClause:
            n.fromClause = tuple(
                rewrite_rangevar(i) if isinstance(i, ast.RangeVar) else i
                for i in n.fromClause)
        elif isinstance(n, ast.JoinExpr):
            if isinstance(n.larg, ast.RangeVar):
                n.larg = rewrite_rangevar(n.larg)
            if isinstance(n.rarg, ast.RangeVar):
                n.rarg = rewrite_rangevar(n.rarg)
        for f in n:
            visit(getattr(n, f, None))

    visit(root)

    # Fail closed: the rewritten tree must contain no bare stored relation that
    # is not either a CTE, a reference relation, or the public.<rel> inside one
    # of the filtered subselects we just built.
    leftover = []

    def audit(n, guarded):
        if isinstance(n, (list, tuple)):
            for x in n:
                audit(x, guarded)
            return
        if not isinstance(n, ast.Node):
            return
        if is_built(n):
            return          # a filtered wrapper we built; its inner scan is intended
        if isinstance(n, ast.RangeVar):
            rel = (n.relname or "").lower()
            if (n.schemaname or n.relname) and not (
                    (not n.schemaname and rel in ctes_all) or rel in _REFERENCE_OK):
                leftover.append(rel)
        for f in n:
            audit(getattr(n, f, None), guarded)

    audit(root, False)
    if leftover:
        raise StoreScopeError("could not confine relation(s): " + ", ".join(sorted(set(leftover))))
    return RawStream()(root)
