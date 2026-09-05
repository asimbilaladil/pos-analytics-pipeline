"""
chat_sql.py — natural-language → SQL → conversational answer, for admin_chat.py.

Flow: the user asks a question in plain English ("last week Cypress sales?").
Claude is given the schema of the queryable (v2) tables and a single tool,
`run_sql`, that executes ONE read-only SELECT against the `laynes` database as a
restricted role. Claude writes the query, reads the rows back, and summarises
them in chat. A short agentic loop lets it run a follow-up query if the first
one wasn't enough.

Safety model:
  * queries run as DB_RO_USER (a LOGIN role with SELECT-only grants — see
    migrations/17 companion role setup); even a bad generated query can't write.
  * belt-and-suspenders: we reject anything that isn't a lone SELECT/WITH,
    force a read-only transaction, cap statement_timeout, and hard-LIMIT the
    result set.
"""

from __future__ import annotations

import decimal
import json
import os
import time
from datetime import date, datetime

import anthropic
import psycopg2
import psycopg2.extras
from pglast import ast, parse_sql
from pglast.parser import ParseError

MODEL = os.getenv("CHAT_MODEL", "claude-sonnet-5")

# Models selectable per-conversation from the chat UI. Keep in sync with
# whatever Claude models are actually available to this API key.
AVAILABLE_MODELS = [
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-haiku-4-5-20251001",
]

MAX_ROWS = 5000
STATEMENT_TIMEOUT_MS = 15000
MAX_LOOPS = 6  # Claude ↔ tool round-trips before we force a final answer

# Establishment id → name. Small and stable; embedding it saves Claude a lookup
# query for the common "location X" question. Kept in sync with the
# establishments table / .env ESTABLISHMENTS.
ESTABLISHMENTS = {
    6: "LCF Katy", 7: "LCF Ella", 14: "LCF Beaumont", 15: "LCF Shepherd",
    20: "LCF Pasadena", 25: "LCF Mission Bend", 26: "LCF Nederland",
    32: "LCF Airtex", 36: "LCF Missouri City", 40: "LCF Rosenberg",
    48: "LCF Downtown Houston", 54: "LCF Cypress",
}

SCHEMA_DOC = """\
All monetary columns are USD numerics. All *_v2 tables are the live/current data
(post-2026-08-13 cutover); the non-v2 originals are frozen and MUST NOT be used.
Dates in feature tables are calendar dates in America/Chicago. Timestamps in
orders_v2/order_items_v2 are timezone-aware (UTC in the column, Central is
UTC-5/-6) — use (created_date AT TIME ZONE 'America/Chicago')::date to bucket by
local day.

TABLE establishments (id int, name text, city text, state text, timezone text, active bool)
  12 rows, one per location. Join target for names.

TABLE features_daily_summary_v2  -- ONE ROW PER establishment PER calendar day. Best table for "sales" questions.
  establishment_id int, date date, day_of_week smallint (0=Mon..6=Sun), is_weekend bool,
  total_orders int, total_items int, total_revenue numeric, avg_order_value numeric,
  avg_items_per_order numeric,
  pct_drive_through numeric, pct_third_party numeric, pct_in_store numeric,      -- fractions 0..1
  revenue_drive_through numeric, revenue_third_party numeric, revenue_in_store numeric,
  avg_kitchen_seconds numeric, pct_orders_over_10min numeric,
  total_voids int, void_rate numeric, total_discounts numeric, discount_rate numeric,
  peak_hour smallint, peak_hour_orders int

TABLE features_hourly_v2  -- one row per establishment per day per hour (0..23)
  establishment_id int, date date, hour smallint, day_of_week smallint, is_weekend bool,
  order_count int, item_count int, total_revenue numeric, avg_order_value numeric,
  orders_drive_through int, orders_eat_in int, orders_to_go int,
  orders_doordash int, orders_ubereats int, orders_online int,
  orders_lane_a int, orders_lane_b int, avg_kitchen_seconds numeric, void_count int, void_rate numeric

TABLE features_product_daily_v2  -- one row per establishment per product per day
  establishment_id int, product_id int, product_name varchar, date date, is_weekend bool,
  quantity_sold numeric, order_count int, revenue numeric,
  qty_drive_through numeric, qty_eat_in numeric, qty_third_party numeric,
  avg_kitchen_seconds numeric, combo_attach_rate numeric, is_combo_item bool, void_count int

TABLE orders_v2  -- raw order headers, use only when a feature table can't answer it
  id bigint, establishment_id int, created_date timestamptz, pickup_time timestamptz,
  dining_option smallint, pos_mode text, final_total numeric, subtotal numeric, tax numeric,
  gratuity numeric, discount_total_amount numeric, service_charge numeric, surcharge numeric,
  closed bool, deleted bool, is_unpaid bool, web_order bool, number_of_people smallint
  NOTE: exclude deleted rows: WHERE deleted IS NOT TRUE

TABLE order_items_v2  -- raw line items
  id bigint, order_id bigint, establishment_id int, product_id int, product_name varchar,
  quantity numeric, price numeric, pure_sales numeric, tax_amount numeric, modifier_amount numeric,
  dining_option smallint, created_date timestamptz, kitchen_seconds int, is_voided bool, deleted bool
  NOTE: exclude voided/deleted: WHERE is_voided IS NOT TRUE AND deleted IS NOT TRUE

TABLE weather_daily  -- one row per establishment per day. NOTE: date column is observed_on (not "date")
  establishment_id int, observed_on date, temp_max_c numeric, temp_min_c numeric,
  temp_mean_c numeric, precipitation_mm numeric, rain_mm numeric, wind_max_kmh numeric

TABLE products (id int, name varchar, product_class varchar, is_combo bool, category_id int)
  WARNING: category_id is NULL for ALL 36,645 product rows. Category analysis is
  NOT possible from this table. Do not group by it; do not infer a category from
  the product name.

VIEW v_orders_classified  -- orders_v2 + transaction classification (migration 21)
  every orders_v2 column, plus:
  txn_class text        REAL  = final_total > 0 AND has items   (a real sale)
                        EMPTY = $0 and no items                 (POS artefact)
                        COMP  = $0 WITH items                   (employee meal / de-facto void)
                        DELETED = deleted flag set
  business_date date (America/Chicago), item_count int, combo_count int,
  combo_sales numeric, standalone_sales numeric, identity_captured bool
  USE THIS for any per-transaction figure (counts, AOV, per-check anything).
  Default to txn_class = 'REAL' and say so in the answer.

VIEW v_order_items_classified  -- order_items_v2 + combo structure
  every order_items_v2 column, plus in_combo bool, combo_group_id uuid,
  combo_seq int, business_date date. Excludes deleted rows.

VIEW v_store_cohort  -- store age, for cross-store comparison
  establishment_id, store_name, open_date, open_date_source, weeks_since_open_int,
  store_age_bucket (honeymoon 1-8 wk | ramp 9-26 | maturing 27-52 | mature 53+ | unknown)
  open_date is only known for Cypress (54) and Downtown Houston (48), both
  INFERRED from the first order in the backfill window, so they are a floor, not
  a fact. The other 10 stores are 'unknown' — say so rather than guessing.

DINING_OPTION codes (orders_v2.dining_option, order_items_v2.dining_option):
  4   = drive-through            1 = eat-in / dine-in        0 = to-go / takeout
  100 = DoorDash                 101 = Uber Eats             5,8 = online / web
  105 = drive-thru lane A        106 = drive-thru lane B     2,3 = other in-store
  channel rollups: in-store = (0,1,2,3) · third-party = (100,101) · drive-through = 4
POS_MODE: 'Q' = normal quick-service register · 'K' = self-order kiosk
"""

GLOSSARY = """\
METRIC DEFINITIONS — use these exact sources:
  "sales" / "revenue" / "net sales"  -> features_daily_summary_v2.total_revenue
        (raw equivalent: SUM(orders_v2.final_total) WHERE deleted IS NOT TRUE)
  "orders" / "transactions" / "tickets" -> total_orders
  "items sold" / "units"              -> total_items  (raw: SUM(order_items_v2.quantity))
  "AOV" / "average order value" / "average check" -> avg_order_value
  "check size in items"              -> avg_items_per_order
  "kitchen time" / "ticket time" / "speed of service" -> avg_kitchen_seconds (seconds)
  "% slow" / "orders over 10 min"    -> pct_orders_over_10min (fraction 0..1)
  "voids"                            -> total_voids / void_rate (fraction)
  "discounts"                        -> total_discounts ($) / discount_rate (fraction)
  "busiest hour"                     -> peak_hour (0..23 local) / peak_hour_orders
  "network" / "company-wide" / "all locations" -> SUM across all establishment_id

DATE LANGUAGE (America/Chicago, today given below):
  "yesterday"  -> date = current_date - 1
  "last week"  -> the most recently COMPLETED Thu..Wed business week
  "this week"  -> current Thu..Wed week to date
  "week to date" / "WTD" -> Thursday of the current business week .. today
  "MTD"        -> date_trunc('month', current_date) .. today
  "last month" -> the previous calendar month, full
  "last 7 days"/"past week" -> current_date - 7 .. current_date - 1 (rolling, NOT the business week)
  "WoW" -> compare the last completed business week to the one before it
  pct_* / *_rate columns are fractions — multiply by 100 when you say "percent".
"""

DATA_NOTES = """\
KNOWN DATA QUIRKS — account for these:
  * A handful of orders are left open for hours/days, which inflates
    avg_kitchen_seconds for that store/day into the thousands (e.g. a
    multi-hour average). If a kitchen-time figure looks physically
    implausible (> ~1800 s), say so and treat it as a data artefact rather
    than real prep time; offer to check order-level detail.
  * LCF Cypress (id 54) opened in June 2026 — it has no data before then;
    don't report $0 for Cypress in earlier periods as a real decline.
  * Historical order data currently goes back to 2026-01-01; feature tables
    (features_*_v2) are being backfilled and may start later than that —
    check the min(date) if a question reaches far back.
  * dining_option 8 appears in older data as "online"; keep it grouped with 5.
  * TRANSACTION COUNTS ARE CONTAMINATED. features_daily_summary_v2.total_orders
    and avg_order_value count every closed order, including COMP ($0 tickets
    that have items — the employee-meal / de-facto-void bucket) and a residual
    of EMPTY tickets. Measured over the last 30 days that is 3,487 COMP + 1,334
    EMPTY against 124,095 REAL: order count overstated ~3.9%, AOV understated
    ~3.8% ($19.43 reported vs $20.19 real). Revenue totals are NOT affected —
    the contaminating rows are all $0. For per-transaction figures prefer
    v_orders_classified WHERE txn_class = 'REAL'. If you use the feature table
    anyway (it is much faster), say the count includes comped tickets.
  * COMBOS EXPLODE INTO MULTIPLE ROWS. 89% of line items belong to a combo,
    joined by combo_uuid. One guest buying one combo produces ~5 order_items
    rows (entrée, side, drink, sauce, toast). Therefore: "items per order" is
    NOT order size, counting line items does NOT count products sold, and there
    is no combo price column — a combo's price is the SUM of its component
    rows. Use combo_count / in_combo from the classified views.
  * There is no product category anywhere (products.category_id is 100% NULL),
    and no maintained entrée classification. Questions about category mix or
    "entrées per check" cannot be answered from this database today.
  * Customer identity is captured on only ~14% of transactions, and enrollees
    are self-selected heavy users. Any customer-level figure must be reported
    with that capture rate and the selection-bias caveat. Never present
    identified-customer behaviour as representative of all guests, and never
    give a unique-customer count as a point estimate.
  * Uneven weekday counts: a month can hold five Mondays and four Thursdays.
    Never sum by day-of-week across such a period — use per-day averages.
"""

# Metrics the model must refuse rather than approximate. Each entry was checked
# against this database, NOT copied from the source spec — the spec's claim that
# voids are absent does NOT hold here (18,385 voided line items in 90 days, with
# void_ref_uuid populated on 17,052), so void rate is deliberately absent from
# this list and remains answerable.
UNAVAILABLE_METRICS = """\
METRICS THAT CANNOT BE COMPUTED — refuse, name the missing field, do not
substitute a proxy or derive an estimate:

  * order duration / drive-thru time / service speed / throughput timing
      orders_v2 has a `closed` boolean but NO order-closed timestamp, so an
      order's elapsed time is not derivable. NOTE: order_items_v2.kitchen_seconds
      IS available and is a real per-item kitchen measure — use it, and call it
      kitchen time, never "service speed" or "drive-thru time".
  * comp vs discount separation
      Both flow through discount_total_amount; discount_reason is free text and
      is empty on effectively all orders. You can report total discounts, not
      the comp share of them.
  * entrées per check / category mix / combo attach by category
      No maintained product-classification or category table exists
      (products.category_id is NULL for every row).
  * weeks_since_open for the 10 stores whose open_date is unknown
      Any cohort or store-age comparison covering them is not available. Cypress
      and Downtown Houston have INFERRED dates only.
"""

GUARDRAILS = """\
ANALYTICAL GUARDRAILS — these override the user's framing when they conflict:

  1. DENOMINATORS. State the denominator with every rate, average or
     per-transaction figure. Default to real sales (txn_class = 'REAL'). If you
     used a different denominator, say so in the same sentence.
  2. COMBOS. Never present "items per order" as order size. Never count line
     items as products purchased.
  3. NO YEAR-OVER-YEAR FOR YOUNG STORES. For any store under ~18 months old,
     a YoY comparison measures against the opening honeymoon and will show a
     decline every time, at every store, in every brand. That is not a finding.
     Cypress (54) and Downtown Houston (48) are both well under that. Compare
     them on weeks-since-open against other stores instead, or decline.
  4. EXCLUDE THE HONEYMOON. Weeks 1-8 after opening are not a baseline. Drop
     them from the "before" side of any pre/post comparison.
  5. BENCHMARK TO BRAND AUV, NOT BUDGET. Budget variance is not performance —
     a budget set from prior actuals will rank a badly underperforming store as
     the best relative performer. Prefer: % of brand system AUV (Layne's ~$2.2M
     per the 2026 FDD, traditional restaurants), then the store's own trailing
     13 weeks, then dollars at stake.
  6. NORMALIZE DAY OF WEEK. Per-day averages only, never raw sums over a period
     with uneven weekday counts.
  7. REFUSE THE UNAVAILABLE. See the list above. Name the missing field.
  8. QUANTIFY BEFORE ATTRIBUTING. Before accepting any causal story, check
     whether the proposed cause is arithmetically big enough to produce the
     effect. A competitor doing $29.5K/week cannot explain a $60K/week decline
     even at 100% cannibalization. This test is cheap and often ends the
     analysis — apply it first.
  9. MEASUREMENT VS BUSINESS CHANGE. When a metric moves, ask whether the
     definition, capture method or system behaviour changed before concluding
     the business changed.
 10. CONFIDENCE IS MANDATORY on every quantitative claim: high (straight from
     the data), moderate (derived, assumptions stated), low (proxy estimate),
     or unknown (say so and stop). Never give an estimate in the same voice as
     a measurement.
 11. SELECTION BIAS. State it whenever the sample is not the population —
     identified customers, delivery orders, any self-selected subset.
 12. LEAD WITH THE COUNTERARGUMENT. Before landing a conclusion, state the best
     case against it. If that case survives, the conclusion is not ready.
"""

FEW_SHOT = """\
EXAMPLES (question -> the query to run -> the style of answer):

Q: "Last week's sales for Cypress"
SQL:
  SELECT ROUND(SUM(total_revenue),2) AS revenue, SUM(total_orders) AS orders
  FROM features_daily_summary_v2
  WHERE establishment_id = 54
    AND date >= date_trunc('week', current_date - 7 - ((EXTRACT(dow FROM current_date)::int + 3) % 7) * 0)  -- see note
A: "Last week (Thu Aug 21 – Wed Aug 27) LCF Cypress did **$88,412.55** across **4,390 orders** (AOV $20.14)."
  -- NOTE: the reliable way to get the last completed Thu..Wed week is:
  --   WITH w AS (SELECT (current_date - ((EXTRACT(isodow FROM current_date)::int + 3) % 7) - 7)::date AS wk_start)
  --   SELECT ... WHERE date >= (SELECT wk_start FROM w) AND date < (SELECT wk_start FROM w) + 7

Q: "Which location had the highest revenue yesterday?"
SQL:
  SELECT e.name, ROUND(f.total_revenue,2) AS revenue
  FROM features_daily_summary_v2 f JOIN establishments e ON e.id = f.establishment_id
  WHERE f.date = current_date - 1
  ORDER BY f.total_revenue DESC
A: Lead with the winner and its number, then a short ranked list.

Q: "Network drive-through vs in-store split this month"
SQL:
  SELECT
    ROUND(SUM(revenue_drive_through),2) AS drive_through,
    ROUND(SUM(revenue_in_store),2)      AS in_store,
    ROUND(SUM(revenue_third_party),2)   AS third_party,
    ROUND(SUM(total_revenue),2)         AS total
  FROM features_daily_summary_v2
  WHERE date >= date_trunc('month', current_date)
A: Give each channel's $ and its share of total.

Q: "What's our average ticket this month?"   (per-transaction -> classified view)
SQL:
  SELECT COUNT(*)                              AS real_txns,
         ROUND(AVG(final_total), 2)            AS avg_ticket,
         ROUND(SUM(final_total), 2)            AS revenue
  FROM v_orders_classified
  WHERE txn_class = 'REAL' AND closed
    AND business_date >= date_trunc('month', current_date)::date
A: "Month to date the average ticket is **$20.19** across **124,095 real
   transactions** ($2.51M). That excludes comped $0 tickets — including them
   would report $19.43, which is the figure the daily summary table gives."
   -- confidence: high. Denominator stated, as guardrail 1 requires.

Q: "How is Cypress doing vs last year?"      (young store -> refuse the framing)
A: Do NOT run a YoY query. Cypress opened June 2026, so "last year" is either
   empty or the pre-opening period; a YoY read would compare against the
   opening honeymoon and manufacture a decline. Say that, then offer the valid
   alternative: its weeks-since-open curve against other stores (v_store_cohort),
   noting its open_date is inferred.

Q: "Top 5 products at Airtex last week"
SQL:
  SELECT product_name, SUM(quantity_sold) AS qty, ROUND(SUM(revenue),2) AS revenue
  FROM features_product_daily_v2
  WHERE establishment_id = 32 AND date >= current_date - 11 AND date < current_date - 4
  GROUP BY product_name ORDER BY qty DESC LIMIT 5
A: Numbered list, qty and revenue each.
"""


def _system_static() -> str:
    """The large, stable part of the system prompt — cached across requests."""
    est_lines = "\n".join(f"  {i} = {n}" for i, n in sorted(ESTABLISHMENTS.items()))
    return f"""\
You are the data assistant for Laynes Chicken Fingers, a 12-location restaurant
group ("LCF" = each store). You answer questions about sales and operations by
querying a PostgreSQL database and summarising the result conversationally.

The business week runs Thursday → Wednesday. If a question is ambiguous about the
date range, pick the sensible reading and state the range you used in the answer.

Establishments (establishment_id = name):
{est_lines}

TOOL — run_sql. Rules for the SQL:
  * exactly one statement, a SELECT (a leading WITH is fine). The connection is
    read-only; writes/DDL are rejected.
  * PostgreSQL dialect. Filter by establishment_id for location questions; join
    establishments only to get the name.
  * Prefer features_daily_summary_v2 / features_hourly_v2 / features_product_daily_v2
    for revenue/order/product questions — they are pre-aggregated and fast. Use
    orders_v2 / order_items_v2 only for things the feature tables don't carry
    (individual orders, sub-hour timing, payment/discount detail).
  * Round money to cents in the SELECT. Aggregate in SQL — never pull thousands
    of raw rows to sum them yourself.
  * If you're unsure a column or value exists, run a tiny exploratory query
    first (e.g. SELECT DISTINCT ... LIMIT 20) — that's cheaper than a wrong answer.
  * On an error, read it and retry once with a fix.

ANSWER STYLE: lead with the headline number, then a short breakdown if it helps.
Use $ and thousands separators; convert fractions to %. State the date range you
used. Don't print SQL unless asked. Keep it tight — this is a chat, not a report.
Flag anything that looks like a data anomaly rather than reporting it as fact.

{SCHEMA_DOC}
{GLOSSARY}
{DATA_NOTES}
{UNAVAILABLE_METRICS}
{GUARDRAILS}
{FEW_SHOT}"""


def _system_today() -> str:
    return (f"Today is {date.today():%A, %Y-%m-%d} (America/Chicago). "
            f"current_date in SQL resolves to this date.")


RUN_SQL_TOOL = {
    "name": "run_sql",
    "description": (
        "Execute one read-only SELECT against the Laynes PostgreSQL database and "
        "return the rows as JSON. One statement only. Results are capped at "
        f"{MAX_ROWS} rows."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single PostgreSQL SELECT statement."},
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
}


# Relations the assistant may read. This is the application half of a
# defence-in-depth pair: migration 22 grants laynes_ro SELECT on exactly this
# set and nothing else, so a bypass here still hits a permission error at the
# database. Keep the two lists identical — if you add a relation to SCHEMA_DOC,
# it must be added here AND granted in a migration, or queries will fail.
#
# Deliberately absent: app_users, app_sessions, chat_messages, chat_query_log,
# chat_conversations (credentials, session tokens, and other users' chat text),
# every legacy non-v2 table and its monthly partitions, and anything the prompt
# does not document. Fail closed.
_ALLOWED_RELATIONS = frozenset({
    "v_orders_classified", "v_order_items_classified", "v_store_cohort",
    "features_daily_summary_v2", "features_hourly_v2", "features_product_daily_v2",
    "orders_v2", "order_items_v2",
    "establishments", "products", "weather_daily",
})

# ── SQL validation ─────────────────────────────────────────────────────────
# Every relation reference is taken from a real PostgreSQL parse tree, not from
# pattern matching. The previous regex validator was replaced after testing
# turned up four separate bypasses, each invisible to the patch before it:
#
#   FROM orders_v2 o, app_users u        SQL-89 implicit join -- only the first
#                                        relation followed a FROM/JOIN keyword
#   FROM orders_v2 AS o, app_users AS u  CTE detection matched "<name> AS" and
#                                        booked app_users as a query-local name
#   WITH x AS (TABLE app_users) ...      TABLE <rel> is SELECT * FROM <rel>,
#                                        with no keyword to key off at all
#   FROM"app_users"                      a quote is a valid separator, but the
#                                        pattern required whitespace
#
# The lesson was not that those four patterns needed fixing but that a regex
# recognises spellings while the policy is about grammar, so it can never tell
# us when it is complete. pglast embeds libpg_query -- the actual PostgreSQL
# parser -- so a relation is whatever the server would resolve as one,
# including forms nobody here anticipated.

_ALLOWED_SCHEMAS = frozenset({"public"})

# Statement types permitted at top level. Anything else -- INSERT, UPDATE,
# DELETE, CREATE, ALTER, DROP, COPY, CALL, DO, SET/RESET, SHOW, BEGIN/COMMIT,
# EXPLAIN, VACUUM -- is rejected by type, so no keyword list has to stay
# exhaustive; an unrecognised utility statement fails closed by default.
_ALLOWED_STMT = (ast.SelectStmt,)

# Functions the read-only role must never call, regardless of which relations
# the query touches. The relation allowlist does not constrain these: several
# read the filesystem or reach the network with no FROM clause at all.
_BLOCKED_FUNCTIONS = frozenset({
    # filesystem
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_ls_logdir", "pg_ls_waldir", "pg_ls_tmpdir", "pg_ls_archivestatusdir",
    "pg_ls_logicalmapdir", "pg_ls_logicalsnapdir", "pg_ls_replslotdir",
    # large objects (file import/export)
    "lo_import", "lo_export", "lo_get", "lo_put", "lo_from_bytea",
    "loread", "lowrite", "lo_open", "lo_create", "lo_unlink",
    # outbound network / foreign data
    "dblink", "dblink_connect", "dblink_connect_u", "dblink_exec",
    "dblink_open", "dblink_fetch", "dblink_send_query", "dblink_get_result",
    "postgres_fdw_handler", "postgres_fdw_get_connections",
    # configuration and secrets
    "current_setting", "set_config", "pg_read_all_settings",
    # server control / denial of service
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "pg_rotate_logfile", "pg_promote", "pg_switch_wal",
    "pg_create_physical_replication_slot", "pg_create_logical_replication_slot",
    "pg_drop_replication_slot", "pg_logical_slot_get_changes",
    "pg_logical_slot_peek_changes",
    # arbitrary-query reflection
    "query_to_xml", "query_to_xmlschema", "query_to_xml_and_xmlschema",
    "database_to_xml", "schema_to_xml",
})


class SqlError(Exception):
    pass


def _fn_name(node: ast.FuncCall) -> str:
    """Bare (unqualified) function name, lowercased."""
    parts = [p.sval for p in (node.funcname or ()) if isinstance(p, ast.String)]
    return parts[-1].lower() if parts else ""


def _check_relation(node: ast.RangeVar, ctes: frozenset[str], found: set[str]) -> None:
    schema = (node.schemaname or "").lower()
    rel = (node.relname or "").lower()
    if schema:
        # A qualified name can never be a CTE, so this is always a stored
        # relation. Checking the schema separately keeps public.app_users and
        # pg_catalog.pg_authid from being reduced to a bare name.
        if schema not in _ALLOWED_SCHEMAS:
            raise SqlError(f"schema '{schema}' is not permitted")
        found.add(rel)
    elif rel not in ctes:            # unqualified and not query-local
        found.add(rel)


def _walk(node, ctes: frozenset[str], found: set[str]) -> None:
    """Depth-first over the parse tree, carrying the CTE names in scope."""
    if isinstance(node, (list, tuple)):
        for item in node:
            _walk(item, ctes, found)
        return
    if not isinstance(node, ast.Node):
        return

    # A WITH clause binds names for its own subtree only, so an inner CTE
    # cannot launder a forbidden name for an outer query. Recursive CTEs need
    # the names visible inside their own bodies, hence binding before descent.
    with_clause = getattr(node, "withClause", None)
    if with_clause is not None and with_clause.ctes:
        for cte in with_clause.ctes:
            if not isinstance(cte.ctequery, ast.SelectStmt):
                raise SqlError("data-modifying WITH clauses are not permitted")
        ctes = ctes | {c.ctename.lower() for c in with_clause.ctes}

    if isinstance(node, ast.SelectStmt):
        if node.intoClause is not None:
            raise SqlError("SELECT INTO is not permitted")
        if node.lockingClause:
            raise SqlError("row locking clauses (FOR UPDATE/SHARE) are not permitted")
    elif isinstance(node, ast.RangeVar):
        _check_relation(node, ctes, found)
    elif isinstance(node, ast.FuncCall):
        name = _fn_name(node)
        if name in _BLOCKED_FUNCTIONS:
            raise SqlError(f"function '{name}' is not permitted")

    for field in node:
        _walk(getattr(node, field, None), ctes, found)


def _relations_in(sql: str) -> set[str]:
    """Every stored relation the statement reads, lowercased and unqualified."""
    found: set[str] = set()
    _walk(_parse_one(sql), frozenset(), found)
    return found


def _parse_one(sql: str) -> ast.Node:
    """Parse to a single SELECT statement or raise. Fails closed."""
    try:
        stmts = parse_sql(sql)
    except ParseError as exc:
        raise SqlError(f"could not parse SQL: {exc}") from exc
    if len(stmts) != 1:
        raise SqlError("only one statement is allowed")
    stmt = stmts[0].stmt
    if not isinstance(stmt, _ALLOWED_STMT):
        kind = type(stmt).__name__.replace("Stmt", "").upper()
        raise SqlError(f"only SELECT queries are permitted (got {kind})")
    return stmt


def _validate(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise SqlError("empty query")
    bad = sorted(_relations_in(s) - _ALLOWED_RELATIONS)
    if bad:
        raise SqlError(
            "query references relation(s) that are not available to this "
            f"assistant: {', '.join(bad)}. Readable relations are: "
            + ", ".join(sorted(_ALLOWED_RELATIONS))
        )
    return s


def _jsonable(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def run_sql(sql: str) -> dict:
    """Execute a validated read-only query. Returns {columns, rows, row_count}."""
    safe = _validate(sql)
    capped = f"SELECT * FROM (\n{safe}\n) AS _capped LIMIT {MAX_ROWS}"
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.environ["DB_RO_USER"],
        password=os.environ["DB_RO_PASS"],
    )
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute(capped)
            rows = cur.fetchall()
        cols = list(rows[0].keys()) if rows else []
        data = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
        return {"columns": cols, "rows": data, "row_count": len(data)}
    finally:
        conn.rollback()
        conn.close()


class ChatResult:
    def __init__(self, answer: str, steps: list[dict]):
        self.answer = answer
        self.steps = steps  # [{sql, row_count, error}]


def answer_question(history: list[dict], question: str, model: str | None = None) -> ChatResult:
    """
    history: prior turns as [{"role": "user"|"assistant", "content": str}, ...]
    model: override the default MODEL for this call (e.g. a per-conversation choice).
    Returns ChatResult(answer, steps). Raises anthropic.APIError on API failure.
    """
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env
    model = model or MODEL
    messages: list[dict] = [
        {"role": m["role"], "content": m["content"]} for m in history
    ]
    messages.append({"role": "user", "content": question})

    steps: list[dict] = []
    final_text = ""

    system = [
        {"type": "text", "text": _system_static(),
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _system_today()},
    ]
    for _ in range(MAX_LOOPS):
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            tools=[RUN_SQL_TOOL],
            messages=messages,
        )

        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            break

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use" or block.name != "run_sql":
                continue
            sql = block.input.get("sql", "")
            step = {"sql": sql, "row_count": None, "error": None}
            try:
                out = run_sql(sql)
                step["row_count"] = out["row_count"]
                payload = json.dumps(out, separators=(",", ":"))
                # keep tool payload bounded
                if len(payload) > 60000:
                    payload = json.dumps(
                        {"columns": out["columns"], "rows": out["rows"][:200],
                         "row_count": out["row_count"], "note": "truncated to 200 rows"},
                        separators=(",", ":"),
                    )
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id, "content": payload,
                })
            except (SqlError, psycopg2.Error) as e:
                step["error"] = str(e).strip()
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": f"ERROR: {step['error']}", "is_error": True,
                })
            steps.append(step)
        messages.append({"role": "user", "content": tool_results})
    else:
        final_text = ("I wasn't able to get to a clean answer within a few tries. "
                      "Try rephrasing, or narrow the question to one location / date range.")

    return ChatResult(final_text.strip(), steps)
