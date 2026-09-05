#!/usr/bin/env python3
"""Regression tests for chat_sql._validate() -- the application half of the
LLM's SQL confinement.

Run:  venv/bin/python tests/test_sql_validator.py     (exit 0 = pass)

Two layers confine model-authored SQL: this validator, and the SELECT-only
grants held by the read-only role (migration 22). These tests cover the
validator only. KNOWN_BYPASS below lists inputs the validator currently lets
through -- each is verified to be denied by the database layer, which is why
they are tracked loudly rather than treated as a breach. They are NOT counted
as failures so that a real regression in fixed behaviour stays visible; move a
case into DENY once it is fixed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv

load_dotenv(".env", override=True)
from chat_sql import SqlError, _validate  # noqa: E402

# ── must be accepted ───────────────────────────────────────────────────────
ALLOW = [
    # Function-call syntax that spells an argument with FROM/IN. These are not
    # clauses, and reading them as one made the validator reject legitimate
    # date logic -- EXTRACT(dow FROM current_date) failed as "relation
    # current_date", which cost the live assistant its whole tool-loop budget
    # on an ordinary void-rate question.
    ("extract_dow_current_date",  "SELECT EXTRACT(dow FROM current_date)"),
    ("extract_hour_column",       "SELECT EXTRACT(hour FROM created_date) FROM v_orders_classified LIMIT 1"),
    ("trim_both_from",            "SELECT TRIM(BOTH ' ' FROM establishment_name) FROM v_orders_classified LIMIT 1"),
    ("extract_in_where",          "SELECT COUNT(*) FROM v_orders_classified WHERE EXTRACT(isodow FROM business_date) = 6"),
    ("extract_with_comma_join",   "SELECT EXTRACT(dow FROM o.business_date) FROM v_orders_classified o, establishments e"),
    ("substring_from_for",        "SELECT SUBSTRING(name FROM 1 FOR 3) FROM products"),
    ("position_in",               "SELECT POSITION('a' IN name) FROM products"),
    ("overlay_placing_from",      "SELECT OVERLAY(name PLACING 'x' FROM 2) FROM products"),
    ("date_part_unaffected",      "SELECT date_part('dow', current_date) FROM orders_v2"),
    # Ordinary query shapes.
    ("plain_view",                "SELECT * FROM v_orders_classified LIMIT 1"),
    ("join_allowed",              "SELECT e.name FROM v_orders_classified o JOIN establishments e ON e.id = o.establishment_id"),
    ("comma_join_allowed",        "SELECT * FROM orders_v2 o, products p WHERE o.id = p.id"),
    ("alias_as",                  "SELECT o.id FROM orders_v2 AS o"),
    ("alias_bare",                "SELECT o.id FROM orders_v2 o"),
    ("subquery",                  "SELECT (SELECT COUNT(*) FROM orders_v2) AS n"),
    ("union_allowed",             "SELECT id FROM orders_v2 UNION SELECT id FROM order_items_v2"),
    ("public_qualified",          "SELECT * FROM public.products LIMIT 1"),
    ("cte_simple",                "WITH x AS (SELECT 1 AS a FROM orders_v2) SELECT * FROM x"),
    ("cte_multiple",              "WITH a AS (SELECT 1 FROM orders_v2), b AS (SELECT 1 FROM products) SELECT * FROM a, b"),
    ("cte_recursive",             "WITH RECURSIVE a AS (SELECT 1 AS n FROM orders_v2) SELECT * FROM a"),
    ("cte_materialized",          "WITH a AS MATERIALIZED (SELECT 1 FROM orders_v2) SELECT * FROM a"),
    ("cte_not_materialized",      "WITH a AS NOT MATERIALIZED (SELECT 1 FROM orders_v2) SELECT * FROM a"),
    ("cte_column_list",           "WITH a(x) AS (SELECT 1 FROM orders_v2) SELECT * FROM a"),
    ("cte_nested_with",           "SELECT * FROM (WITH z AS (SELECT 1 FROM products) SELECT * FROM z) t"),
    # Commas that are NOT a from-list -- the comma walker must not misread them.
    ("in_value_list",             "SELECT * FROM orders_v2 WHERE establishment_id IN (26,27,28)"),
    ("group_order_by_commas",     "SELECT establishment_id, business_date FROM v_orders_classified GROUP BY 1, 2 ORDER BY 1, 2"),
    ("function_arg_commas",       "SELECT COALESCE(a, b, c) FROM orders_v2"),
    ("public_qualified_view",     "SELECT * FROM public.v_orders_classified LIMIT 1"),
    ("lateral_subquery",          "SELECT * FROM orders_v2 o, LATERAL (SELECT 1 FROM order_items_v2 i WHERE i.order_id = o.id) x"),
    ("scalar_subquery",           "SELECT (SELECT MAX(id) FROM orders_v2) AS m FROM products"),
    ("intersect_allowed",         "SELECT id FROM orders_v2 INTERSECT SELECT id FROM order_items_v2"),
    ("except_allowed",            "SELECT id FROM orders_v2 EXCEPT SELECT id FROM order_items_v2"),
    ("table_cmd_allowed",         "TABLE v_store_cohort"),
    ("aggregates_and_dates",      "SELECT date_trunc('month', business_date) AS m, COUNT(*), AVG(final_total) FROM v_orders_classified GROUP BY 1 ORDER BY 1"),
    ("window_function",           "SELECT id, ROW_NUMBER() OVER (PARTITION BY establishment_id ORDER BY id) FROM orders_v2"),
    ("case_and_filter",           "SELECT COUNT(*) FILTER (WHERE final_total > 0), CASE WHEN 1=1 THEN 'a' ELSE 'b' END FROM orders_v2"),
    ("generate_series",           "SELECT * FROM generate_series(1,10)"),
]

# ── must be rejected ───────────────────────────────────────────────────────
DENY = [
    ("app_users_direct",          "SELECT * FROM app_users"),
    ("app_users_columns",         "SELECT email, password_hash FROM app_users"),
    ("app_users_join",            "SELECT * FROM orders_v2 o JOIN app_users u ON TRUE"),
    ("app_users_inner_join",      "SELECT * FROM orders_v2 INNER JOIN app_users ON TRUE"),
    ("app_users_cross_join",      "SELECT * FROM orders_v2 CROSS JOIN app_users"),
    ("app_users_natural_join",    "SELECT * FROM orders_v2 NATURAL JOIN app_users"),
    # SQL-89 implicit join. The scan keyed off the FROM/JOIN keyword alone, so
    # every relation after the first comma was invisible to it. Pre-existing
    # bypass, found by testing rather than reported.
    ("comma_join_bare",           "SELECT * FROM orders_v2, app_users"),
    ("comma_join_alias_bare",     "SELECT * FROM orders_v2 o, app_users u"),
    ("comma_join_alias_as",       "SELECT * FROM orders_v2 AS o, app_users AS u"),
    ("comma_join_three_way",      "SELECT * FROM orders_v2 o, products p, chat_query_log c"),
    ("comma_join_qualified",      "SELECT * FROM orders_v2 o, public.app_users u"),
    ("comma_join_in_subquery",    "SELECT * FROM (SELECT 1) t WHERE 1 IN (SELECT 1 FROM orders_v2 o, app_users u)"),
    ("comma_join_in_cte_body",    "WITH a AS (SELECT * FROM orders_v2 o, app_users u) SELECT * FROM a"),
    ("app_users_subquery",        "SELECT (SELECT password_hash FROM app_users LIMIT 1)"),
    ("app_users_cte",             "WITH z AS (SELECT * FROM app_users) SELECT * FROM z"),
    ("app_users_cte_second",      "WITH a AS (SELECT 1 FROM orders_v2), b AS (SELECT * FROM app_users) SELECT * FROM a, b"),
    ("app_users_nested_with",     "SELECT * FROM (WITH z AS (SELECT * FROM app_users) SELECT * FROM z) t"),
    # A CTE named after a forbidden table must not launder a second one.
    ("cte_name_shadow",           "WITH app_users AS (SELECT 1 FROM orders_v2) SELECT * FROM app_users, chat_messages"),
    ("app_users_union",           "SELECT id FROM orders_v2 UNION SELECT id FROM app_users"),
    ("app_users_schema_public",   "SELECT * FROM public.app_users"),
    ("app_users_quoted",          'SELECT * FROM "app_users"'),
    ("app_users_quoted_qual",     'SELECT * FROM "public"."app_users"'),
    ("app_users_qual_spaces",     "SELECT * FROM public . app_users"),
    ("app_users_comment_gap",     "SELECT * FROM/**/app_users"),
    ("app_users_newline",         "SELECT * FROM\napp_users"),
    ("app_users_tab",             "SELECT * FROM\tapp_users"),
    ("app_users_from_only",       "SELECT * FROM ONLY app_users"),
    # Masking FROM inside a function must not hide a subquery nested in it.
    ("substring_hides_subquery",  "SELECT SUBSTRING((SELECT password_hash FROM app_users) FROM 1 FOR 5)"),
    ("trim_hides_subquery",       "SELECT TRIM(BOTH ' ' FROM (SELECT email FROM app_users LIMIT 1))"),
    ("extract_hides_subquery",    "SELECT EXTRACT(dow FROM (SELECT created_date FROM app_users LIMIT 1))"),
    # Other non-allowlisted relations.
    ("chat_query_log",            "SELECT question FROM chat_query_log"),
    ("chat_messages",             "SELECT content FROM chat_messages"),
    ("app_sessions",              "SELECT * FROM app_sessions"),
    ("payments_v2",               "SELECT * FROM payments_v2"),
    ("legacy_orders",             "SELECT * FROM orders"),
    ("catalog_pg_tables",         "SELECT * FROM pg_catalog.pg_tables"),
    ("catalog_pg_authid",         "SELECT * FROM pg_catalog.pg_authid"),
    ("information_schema",        "SELECT * FROM information_schema.tables"),
    # ── bypasses of the previous regex validator, kept permanently ─────────
    # Each string below is the exact SQL that passed the old validator. They
    # are the reason relation detection is now done from a parse tree.
    ("bypass_table_cmd_in_cte",   "WITH x AS (TABLE app_users) SELECT * FROM x"),
    ("bypass_table_cmd_union",    "SELECT id FROM orders_v2 UNION TABLE app_users"),
    ("bypass_quoted_no_space",    'SELECT * FROM"app_users"'),
    # ── Phase 3: constructs PostgreSQL accepts but policy must not ─────────
    ("select_into",               "SELECT * INTO x FROM orders_v2"),
    ("for_update",                "SELECT * FROM orders_v2 FOR UPDATE"),
    ("for_share",                 "SELECT * FROM orders_v2 FOR SHARE"),
    ("data_modifying_cte",        "WITH d AS (DELETE FROM orders_v2 RETURNING id) SELECT * FROM d"),
    ("data_modifying_cte_insert", "WITH i AS (INSERT INTO orders_v2 VALUES (1) RETURNING id) SELECT * FROM i"),
    ("copy",                      "COPY orders_v2 TO '/tmp/x.csv'"),
    ("copy_from_program",         "COPY orders_v2 FROM PROGRAM 'cat /etc/passwd'"),
    ("call",                      "CALL some_proc()"),
    ("do_block",                  "DO $$ BEGIN END $$"),
    ("set_var",                   "SET search_path = evil"),
    ("reset_var",                 "RESET search_path"),
    ("show_var",                  "SHOW all"),
    ("begin_txn",                 "BEGIN"),
    ("commit_txn",                "COMMIT"),
    ("explain",                   "EXPLAIN SELECT * FROM orders_v2"),
    ("vacuum",                    "VACUUM orders_v2"),
    ("truncate",                  "TRUNCATE orders_v2"),
    ("grant",                     "GRANT SELECT ON orders_v2 TO laynes_ro"),
    ("create_function",           "CREATE FUNCTION f() RETURNS int AS 'SELECT 1' LANGUAGE sql"),
    ("unparseable",               "SELECT FROM WHERE ((("),
    # Dangerous functions -- reachable with no FROM clause, so the relation
    # allowlist alone does not constrain them.
    ("fn_pg_read_file",           "SELECT pg_read_file('/etc/passwd')"),
    ("fn_pg_read_binary_file",    "SELECT pg_read_binary_file('/etc/passwd')"),
    ("fn_pg_ls_dir",              "SELECT pg_ls_dir('/')"),
    ("fn_pg_stat_file",           "SELECT pg_stat_file('/etc/passwd')"),
    ("fn_lo_import",              "SELECT lo_import('/etc/passwd')"),
    ("fn_dblink",                 "SELECT dblink('host=evil','SELECT 1')"),
    ("fn_current_setting",        "SELECT current_setting('some.secret')"),
    ("fn_set_config",             "SELECT set_config('search_path','evil',false)"),
    ("fn_pg_sleep",               "SELECT pg_sleep(60)"),
    ("fn_pg_terminate_backend",   "SELECT pg_terminate_backend(1)"),
    ("fn_query_to_xml",           "SELECT query_to_xml('SELECT * FROM app_users',true,true,'')"),
    ("fn_as_table_function",      "SELECT * FROM pg_ls_dir('/')"),
    ("fn_qualified",              "SELECT pg_catalog.pg_read_file('/etc/passwd')"),
    # Shape guards.
    ("insert",                    "INSERT INTO orders_v2 VALUES (1)"),
    ("update",                    "UPDATE orders_v2 SET id = 1"),
    ("delete",                    "DELETE FROM orders_v2"),
    ("drop",                      "DROP TABLE orders_v2"),
    ("alter",                     "ALTER TABLE orders_v2 ADD COLUMN x int"),
    ("create",                    "CREATE TABLE t (a int)"),
    ("stacked_statements",        "SELECT 1; DROP TABLE orders_v2"),
]

# ── known-open bypasses ────────────────────────────────────────────────────
# Each of these passes _validate() today and is stopped only by the read-only
# role's grants. They are listed so the gap is tracked in the open rather than
# rediscovered; see the note at the top of the file.
# Empty: the AST validator closed both entries that stood here. Kept as the
# place to record any future gap that the database layer alone is holding.
KNOWN_BYPASS: list[tuple[str, str, str]] = []


def main() -> int:
    failures = []
    print("=== MUST ALLOW ===")
    for name, sql in ALLOW:
        try:
            _validate(sql)
            print(f"  PASS  {name}")
        except SqlError as exc:
            failures.append(name)
            print(f"  FAIL  {name} -> {exc}")

    print("=== MUST DENY ===")
    for name, sql in DENY:
        try:
            _validate(sql)
            failures.append(name)
            print(f"  FAIL  {name} -> ACCEPTED, expected rejection")
        except SqlError:
            print(f"  PASS  {name}")

    print("=== KNOWN OPEN (validator only; database denies these) ===")
    for name, sql, why in KNOWN_BYPASS:
        try:
            _validate(sql)
            print(f"  OPEN  {name} -- {why}")
        except SqlError:
            print(f"  FIXED {name} -- move this case into DENY")

    total = len(ALLOW) + len(DENY)
    print(f"\n{total - len(failures)}/{total} passed, "
          f"{len(KNOWN_BYPASS)} known-open tracked")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
