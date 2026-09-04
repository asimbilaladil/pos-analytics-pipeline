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
import re
import time
from datetime import date, datetime

import anthropic
import psycopg2
import psycopg2.extras

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

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|"
    r"create|comment|copy|call|do|vacuum|reindex|cluster|lock|"
    r"listen|notify|prepare|execute|set|reset|begin|commit|rollback)\b",
    re.IGNORECASE,
)


class SqlError(Exception):
    pass


def _validate(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise SqlError("empty query")
    # single statement only
    if ";" in s:
        raise SqlError("only one statement is allowed (no ';')")
    if not re.match(r"(?is)^\s*(with|select)\b", s):
        raise SqlError("query must be a SELECT (optionally starting with WITH)")
    # crude keyword guard — the read-only role is the real protection, this just
    # gives Claude a clearer error than a permission-denied deep in a CTE.
    if _FORBIDDEN.search(s):
        raise SqlError("only plain SELECT queries are permitted")
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
