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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import psycopg2
import re
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
  combo_sales numeric, standalone_sales numeric, identity_captured bool,
  entree_count numeric        -- maintained entrée count (see ENTRÉES below)
  unresolved_item_count int   -- lines whose product is not yet classified
  entree_fully_resolved bool  -- FALSE when entree_count may be understated
  USE THIS for any per-transaction figure (counts, AOV, per-check anything).
  Default to txn_class = 'REAL' and say so in the answer.

VIEW v_order_items_classified  -- order_items_v2 + combo structure + entrées
  every order_items_v2 column, plus in_combo bool, combo_group_id uuid,
  combo_seq int, business_date date, and:
  is_entree bool                    -- from the maintained classification
  product_form text                 -- combo_component | single_line_combo
                                    -- | standalone | unknown
  classification_confidence text    -- high | medium | unknown
  Excludes deleted rows.

VIEW v_orders_payment_classified  -- v_orders_classified + safe payment context
  every v_orders_classified column, plus:
  payment_record_count int, has_payment bool,
  payment_type_codes int[]      -- RAW Revel codes, no name mapping exists
  payment_type_single int       -- the code when the order has only one
  is_split_tender bool          -- more than one payment RECORD on the order
  refunded_payment_count int, has_refund bool,
  payment_amount_total numeric, tip_total numeric, gratuity_total numeric
  USE THIS for anything about payments, refunds, tenders or tips.
  has_payment = FALSE is normal for EMPTY and COMP orders, not lost revenue.
  NOTE: this view is slower than v_orders_classified, so prefer the latter when
  the question does not involve payments.

VIEW v_order_payment_summary  -- one row per order that HAS a payment
  order_id, establishment_id, payment_record_count, has_payment,
  payment_type_codes, payment_type_single, is_split_tender,
  refunded_payment_count, has_refund, payment_amount_total, tip_total,
  gratuity_total. An order absent from this view has no payment record.

VIEW v_orders_time_context  -- the time contract (migration 29)
  order_id, establishment_id,
  transaction_timestamp_local   -- created_date as America/Chicago wall-clock
  local_calendar_date, local_hour (0-23),
  local_weekday_iso             -- ISO-8601: Monday=1 .. Sunday=7
  local_weekday_name            -- 'Monday' ... 'Sunday'
  business_date                 -- currently EQUALS local_calendar_date
  business_date_method          -- 'local_calendar_date'
  business_date_confidence      -- 'limited' (no verified rollover rule exists)
  transaction_timestamp_source  -- 'created_date'
  USE local_weekday_iso for weekday questions. features_*_v2.day_of_week uses a
  DIFFERENT convention (Monday=0..Sunday=6) and EXTRACT(dow) a third (Sunday=0).

VIEW v_order_identity_context  -- per-order identity (migration 34)
  order_id, establishment_id, business_date, txn_class,
  has_customer_identity, anonymous_flag,
  safe_customer_key   -- opaque hash; no name/email/phone exists in this DB
  final_total

VIEW v_identity_profile  -- aggregate behaviour per opaque identity (WHOLE HISTORY)
  safe_customer_key, visits_all_time, distinct_establishments,
  pct_web_associated, first_seen, last_seen,
  suspected_non_individual  -- SUSPECTED marketplace account, not confirmed
  This view groups EVERY identified order, so do NOT join it row-wise to a
  scoped query -- the grouping cannot be pushed down and the join takes ~10s and
  can hit the statement timeout. Instead either (a) read
  identity.suspected_non_individual_customers from check_data, or (b) query this
  view on its own to list the suspected keys, then exclude them in a separate
  scoped aggregate over v_order_identity_context.

VIEW v_order_channel_context  -- channel context (migration 33)
  order_id, establishment_id, business_date,
  channel_code                      -- RAW orders_v2.dining_option integer
  channel_source_field
  channel_group                     -- 'web_associated' | 'non_web_associated'
                                    -- ORDERING PATTERN, not a service mode
  channel_group_confidence          -- 'verified_structural'
  channel_name_project_convention   -- drive_through/eat_in/to_go/doordash/...
  channel_name_confidence           -- 'project_convention_unverified'
  web_order, possible_code_source_mismatch
  Business data: needs store + period scope. The GROUP is verified but says
  only how the order REACHED the POS. There is NO verified drive-thru / dine-in
  / takeout / delivery mapping -- always give the raw code.

VIEW v_product_category_current  -- CURRENT category reference (migration 32)
  product_id, establishment_id, product_name, category_id, category_name,
  parent_category_id, parent_category_name, mapping_source,
  mapping_confidence, current_snapshot_at, category_stable_since
  REFERENCE DATA: needs NO store/period scope and NO reconciliation. Use it for
  "what category is X in" and "is there a Y category". It states TODAY's
  mapping only -- it proves nothing about a past period.

VIEW v_order_items_category_context  -- verified category dimension (migration 31)
  order_item_id, order_id, establishment_id, product_id, product_name,
  category_id, category_name, parent_category_id, parent_category_name,
  category_stable_since         -- last date the product record changed
  category_mapping_source       -- 'revel_product_api' | 'unresolved'
  category_mapping_confidence   -- 'verified_current' | 'unknown'
  historical_category_verified  -- TRUE only when the mapping predates THAT ROW's
                                -- own date. FALSE = only today's category known.
  Join this ONLY for category questions; it is not in the core views, so
  ordinary analytics stay fast. Category comes from Revel's explicit
  Product.category field -- NEVER from product_class, and never from a name.

VIEW v_category_review_queue  -- products lacking a verified category
  product_id, category_id, category_name_snapshot, category_stable_since,
  mapping_confidence, line_items_90d, revenue_90d

VIEW v_entree_coverage  -- per store/day entrée classification completeness
  establishment_id, business_date, real_orders, fully_resolved_orders,
  pct_orders_resolved, entrees, unresolved_items

VIEW v_entree_review_queue  -- products awaiting human entrée classification
  product_id, product_name_snapshot, confidence, classification_source,
  line_items_90d, quantity_90d, revenue_90d, avg_price, reviewer_hint

VIEW v_payments_daily_v2  -- per store/day payment totals (reconciliation)
  establishment_id, business_date, payment_count, paid_order_count,
  payment_amount, tip_amount, refunded_count

VIEW v_store_cohort  -- store age vs DATA history (migration 30)
  establishment_id, establishment_name,
  verified_open_date        -- NULL for ALL 12 stores today: no authoritative
                            -- source exists. NULL means unknown, never "new".
  open_date_source, open_date_confidence ('unknown' for all)
  revel_account_created_date -- Revel provisioning date, observed 20-49 days
                            -- BEFORE first trade. A lower bound, NOT an opening.
  first_seen_order_date, first_seen_real_order_date
  available_history_start / _end / _days / _weeks
  history_truncated bool    -- TRUE for 10 of 12: our data begins at the
                            -- 2026-01-01 backfill edge, so the store was
                            -- already trading before we could see it.
  weeks_since_open          -- NULL unless verified_open_date is set (so NULL
                            -- for every store). NEVER derived from first_seen.
  maturity_threshold_status -- 'no maintained threshold configured'
  DATA HISTORY IS NOT STORE AGE. "We have 8 months of data" is not "the store is
  8 months old". Say which one you mean.

VIEW v_orders_time_context  -- the time contract (migration 29)
  order_id, establishment_id,
  transaction_timestamp_local   -- created_date as America/Chicago wall-clock
  local_calendar_date, local_hour (0-23),
  local_weekday_iso             -- ISO-8601: Monday=1 .. Sunday=7
  local_weekday_name            -- 'Monday' ... 'Sunday'
  business_date                 -- currently EQUALS local_calendar_date
  business_date_method          -- 'local_calendar_date'
  business_date_confidence      -- 'limited' (no verified rollover rule exists)
  transaction_timestamp_source  -- 'created_date'
  USE local_weekday_iso for weekday questions. features_*_v2.day_of_week uses a
  DIFFERENT convention (Monday=0..Sunday=6) and EXTRACT(dow) a third (Sunday=0).

VIEW v_entree_coverage  -- per store/day entrée classification completeness
  establishment_id, business_date, real_orders, fully_resolved_orders,
  pct_orders_resolved, entrees, unresolved_items

VIEW v_entree_review_queue  -- products awaiting human entrée classification
  product_id, product_name_snapshot, confidence, classification_source,
  line_items_90d, quantity_90d, revenue_90d, avg_price, reviewer_hint

VIEW v_payments_daily_v2  -- per store/day payment totals (reconciliation)
  establishment_id, business_date, payment_count, paid_order_count,
  payment_amount, tip_amount, refunded_count

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
  * LCF Cypress (id 54) has NO observed sales history in this database before
    June 2026. That is a statement about our data, NOT about the store: its
    verified_open_date is NULL and its open_date_confidence is 'unknown', like
    every other store. Do NOT say Cypress opened in June, and do NOT treat the
    first date we can see as an opening date. Equally, do NOT report $0 for
    Cypress in earlier periods as a real decline — there is no history to
    decline from.
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
    so category mix and combo-attach-by-category remain unanswerable. Entrées
    ARE available: product_analysis_classification is a maintained, human-
    reviewed definition, surfaced as is_entree / product_form on
    v_order_items_classified and entree_count on v_orders_classified.
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
  * NOT LISTED HERE: category / product-category analysis. It IS available —
      Revel's Product.category is the authoritative source and the mapping is
      maintained. Do NOT refuse category questions; follow CATEGORY RULES for
      which relation to use and what coverage caveat to state.
  * weeks_since_open / store age for EVERY store
      No authoritative opening date exists for any of the 12 stores, so
      verified_open_date and weeks_since_open are NULL throughout. Cohort and
      store-age comparisons cannot be made. Do not substitute the first date in
      our data -- for 10 stores that is the backfill edge, and for the other 2 it
      is staff testing days before real trade.
"""

# Machine-readable mirror of UNAVAILABLE_METRICS, for the meta_extract payload.
# The prose above tells the model how to refuse; this list tells it what is
# missing in a form it can check against a request. Keep the two in step.
UNAVAILABLE_METRIC_KEYS = [
    "order_duration_or_service_time",
    "drive_thru_timing",
    "comp_vs_discount_separation",
    # "product_category_mix" was removed here: A5 established Revel's
    # Product.category as an authoritative source, so category analysis IS
    # available through the A5 views. Leaving it listed made the model's own
    # authoritative unavailable-list contradict what it could actually answer.
    "store_age_and_weeks_since_open_all_stores",
]


ENTREE_RULES = """\
ENTRÉES — use the maintained classification, never a product name:

  * is_entree, product_form and classification_confidence come from
    v_order_items_classified; entree_count comes from v_orders_classified.
    NEVER decide what an entrée is by pattern-matching a product name, and
    never write your own CASE over product_name to do it.
  * A product with no verified classification never counts as an entrée, so
    entree_count is a FLOOR, not an estimate.
  * check_data returns entree_classification.coverage_pct. Below the floor
    (80%) do not state entrées per check as fact: give the floor figure, say
    how many line items are unreviewed, and stop. Between 80% and 95%, quote
    the figure as a lower bound and say so.
  * product_form separates the two shapes a combo takes: combo_component (one
    line inside a combo) and single_line_combo (one row that IS the combo).
    Counting raw line items is NOT order size -- about 90% of lines sit inside
    a combo, so prefer entree_count or combo_count.
  * v_entree_review_queue lists what is unresolved and the revenue at stake.

ITEMS PER ORDER — never lead with it:
  * A raw line-item count is NOT order size and NOT products purchased. About
    90% of lines sit inside a combo, so one guest buying one combo produces
    roughly five rows (entrée, side, drink, sauce, toast). Leading with "about
    6 items per order" tells the user something false about their business.
  * When asked "how many items per order", answer with the maintained metrics
    FIRST: entree_count per REAL transaction, combo_count per REAL transaction,
    and the entrée distribution (0 / 1 / 2 / 3+). Explain that combos explode
    into component rows and that this is why the raw count misleads.
  * Report line items per order ONLY if the user explicitly asks for raw POS
    line density, and label it exactly: "raw Revel line items per REAL order —
    not products purchased and not order size."
  * Never say entrée classification is unavailable. It exists and is maintained.
"""

COHORT_RULES = """\
STORE AGE — unknown for every store, and that is the answer:

  * verified_open_date is NULL for ALL 12 stores. No authoritative opening date
    exists. If asked when a store opened, say it is not recorded -- do NOT
    answer with the first date in our data.
  * DATA HISTORY IS NOT STORE AGE. Our data starts 2026-01-01 for 10 of 12
    stores because that is the backfill edge (history_truncated = TRUE), not
    because they opened then. Say "we have N months of data for this store",
    never "the store has been open N months".
  * The other 2 stores (Downtown Houston, Cypress) first appear later, but their
    earliest rows are staff testing -- Downtown Houston's first REAL day is 4
    orders totalling $14. Still not an opening date.
  * revel_account_created_date is when Revel provisioned the establishment,
    observed 20-49 days BEFORE first trade. It is a lower bound on the opening
    date, so "opened on or after <date>" is fair; "opened on <date>" is not.
  * NEVER compute weeks_since_open from first_seen_* dates. It is NULL and must
    stay NULL until a verified opening date is supplied.
  * NEVER call a store new, young, mature or in a honeymoon period. No
    maintained maturity threshold is configured (maturity_threshold_status), and
    without a verified opening date the label has no basis. If the user wants
    such a split, ask them for the threshold and the dates.
  * YEAR-OVER-YEAR IS IMPOSSIBLE HERE. This database contains nothing before
    2026-01-01. Any "vs last year" question must be refused with that fact, not
    answered with an empty comparison.
  * Before comparing one store to others, state the age/history context first:
    if a store has materially less data than its peers, say so before
    interpreting its numbers.
"""

IDENTITY_RULES = """\
CUSTOMER IDENTITY — a small, biased subset. Never speak for everyone:

  * identity_capture_rate = identified REAL transactions / ALL REAL transactions,
    on a 0-100 scale. Nederland June 2026 is 10.69% (488 of 4,565). NEVER use
    identified customers as the denominator.
  * Metrics from identified customers describe THE IDENTIFIED SUBSET, not "our
    customers". Say "among customers identifiable in Revel..." and give the
    capture rate. Below 30% capture, do NOT characterise anything as
    representative of customers generally; 30-80% needs a strong caveat; 80%+
    still needs the subset named.
  * ANONYMOUS MEANS UNKNOWN, NOT NON-MEMBER. Order-level loyalty EVIDENCE is
    now ingested (v_order_loyalty_context) and you CAN analyse it -- see the
    LOYALTY EVIDENCE section. What you still cannot do is call an
    unidentified customer a non-member: absence of identity proves nothing
    about membership.
  * CUSTOMER IDENTITY IS NOT LOYALTY MEMBERSHIP, and the two rates are
    SEPARATE NUMBERS. identity_capture_rate (customer_id present) and the
    loyalty evidence rate (a loyalty payload on the order) measure different
    things, come from different fields and must NEVER be merged, averaged or
    substituted for one another. An identified customer_id says an order was
    linked to a customer record; it says NOTHING about membership. Never treat
    identified/unidentified as a proxy for member/non-member.
  * OPERATOR IDS ARE NOT CUSTOMERS. created_by_user_id, updated_by_user_id,
    voided_by_user_id, discounted_by_user_id, opened_by_user_id and
    closed_by_user_id identify STAFF. Never treat them as customer identity.
  * SOME IDENTIFIED IDS ARE NOT PEOPLE, but that is a HEURISTIC, not a verified
    fact. 23 identified IDs hold 78.5% of all identified visits network-wide; at
    Nederland in June, 2 of the 88 IDs hold 389 of 488 visits.
    CHECK suspected_non_individual BEFORE any per-ID average -- read the count
    from check_data's identity block, or query v_identity_profile ON ITS OWN (it
    is ungated reference data) and exclude those keys in a separate scoped
    aggregate. Do NOT join that profile view row-wise into a scoped query: it
    groups every identified order in history and will hit the statement timeout.
  * ALWAYS SHOW BOTH, never the filtered figure alone:
        RAW IDENTIFIED IDS   88 IDs, 5.55 visits/ID, 10 with 2+ visits
        AFTER EXCLUDING SUSPECTED NON-INDIVIDUAL IDS
                             86 IDs, ~1.15 visits/ID, 8 with 2+ visits
    The filtered view never silently replaces the raw one. State the exclusion
    every time you apply it.
  * TERMINOLOGY, exactly:
      say "identified customer references" or "identified IDs"
      say "suspected non-individual IDs"
      say "after excluding suspected non-individual IDs"
      do NOT say "after removing platform accounts" or "marketplace accounts" --
          those IDs have NOT been manually verified as such
      do NOT call the remaining IDs proven, real or human customers.
          "human-like subset" is acceptable only if qualified as heuristic.
  * REPEAT CUSTOMER must state its window: ">=2 REAL transactions AT THIS STORE
    WITHIN THE ANALYSED PERIOD" is not "a repeat customer historically". Say
    which you computed.
  * UNIQUE CUSTOMERS CANNOT BE COUNTED. With ~11% identity capture, the number
    of distinct identifiable customers is NOT the number of unique guests. Say
    "N identifiable customers" and state plainly that total unique customers
    cannot be determined from this data.
  * NO CAUSAL CLAIMS. "Identified transactions averaged $X vs $Y" is allowed.
    "Loyalty makes customers spend more" is not -- identification correlates
    with channel (identified orders are overwhelmingly third-party/web), so the
    gap is confounded by channel, not shown to be caused by membership.
"""

LOYALTY_RULES = """\
LOYALTY EVIDENCE -- what the data proves, and what it cannot:

  * THE FIELD IS EVIDENCE, NOT MEMBERSHIP. v_order_loyalty_context.
    has_loyalty_payload = TRUE means Revel attached a loyalty structure to that
    order: LOYALTY EVIDENCE PRESENT. FALSE means NO LOYALTY EVIDENCE OBSERVED.
    It does NOT mean the guest is not a member. A member who does not identify
    at the till produces an order with no payload, and nothing in this data can
    tell that apart from a genuine non-member's order.
  * REQUIRED WORDING. Say "orders with loyalty evidence" and "orders with no
    loyalty evidence observed". NEVER say "loyalty customers" vs "non-loyalty
    customers", "members vs non-members", or "loyalty members spend X".
  * IF THE USER ASKS MEMBER VS NON-MEMBER, do not refuse -- answer the
    answerable half. Give the loyalty-evidence comparison, then state plainly
    that the non-member side cannot be established, because absence of evidence
    is not evidence of non-membership.
  * NEVER CLAIM CAUSATION. "Did loyalty cause customers to spend more" cannot be
    answered from this data. Report the observed difference and say explicitly
    that it is observational: guests who identify at the till are self-selected,
    and the comparison is confounded by that selection. No experiment exists
    here.
  * IT IS A MINORITY SIGNAL. Loyalty evidence appears on about 5% of orders
    network-wide (3.3%-9.1% depending on store). Always give the rate alongside
    any loyalty figure, and never present the loyalty-evidence subset as the
    behaviour of all guests.
  * DENOMINATOR IS REAL ORDERS. Compute rates over txn_class = 'REAL' only,
    exactly as elsewhere. State the denominator.
  * loyalty_registered = TRUE is "registered loyalty evidence on this order". It
    does NOT establish a person's full historical membership status.
  * total_points_present is a PRESENCE FLAG. The underlying balance is a
    per-person running total that says nothing about the order and is not
    exposed. Never sum or average points.
  * NO PER-PERSON LOYALTY ANALYSIS. There is no loyalty customer key in any
    view you can read. You cannot count loyalty members, measure repeat member
    visits, or compute per-member value. Say so plainly if asked.
"""


LABOR_RULES = """\
LABOUR -- v_labor_hourly_context (est x business_date x local_hour) and
v_labor_daily_context (est x business_date). Never join labour to individual
orders; relate it only by establishment_id + business_date (+ local_hour).

  * BREAKS ARE NOT RECORDED. break_data_status is always 'unavailable'.
    labor_hours is ELAPSED CLOCKED TIME (clock_out - clock_in) and INCLUDES any
    unpaid break actually taken, so it OVERSTATES paid-worked time by an unknown
    amount. Never call it verified paid-worked time. State this whenever you
    report labour hours or a labour percentage.
  * estimated_labor_cost IS NOT PAYROLL COST. It is hours x role wage, and
    excludes overtime premiums, employer taxes, burden and benefits. Always call
    it "estimated labour cost". Never call it payroll, never call it what the
    business actually paid.
  * SHIFTS ARE ALREADY SPLIT ACROSS HOURS. A 17:30-19:15 shift contributes 0.5h
    to hour 17, 1.0h to hour 18 and 0.25h to hour 19. Do not re-apportion, and
    do not assume a shift belongs to its clock-in hour.
  * COUNT COLUMNS THAT ARE NOT SUMMABLE. shift_day_count (daily) counts a
    midnight-crossing shift on both days; shift_overlap_count (hourly) is a
    staffing level, not a shift count. To count shifts worked, use
    shifts_started_count. Summing the other two overstates.
  * SALES VS LABOUR uses REAL orders and REAL sales only:
        labour % of sales   = estimated_labor_cost / REAL final_total x 100
        sales per labour hr = REAL final_total / labor_hours
        orders per labour hr= REAL order count  / labor_hours
    Name the denominator every time, and label the cost as estimated.
  * NEVER DECLARE OVER- OR UNDERSTAFFING. No staffing target, sales forecast or
    labour standard exists in this data, so "overstaffed" is not derivable.
    Quantify instead: labour hours and estimated cost against sales and orders
    for the same hour, and use wording like "hours with relatively high labour
    intensity (labour cost per sales dollar)".
    WHEN ASKED WHICH HOURS WERE OVERSTAFFED, DO NOT STOP AT THE REFUSAL AND DO
    NOT ASK WHETHER TO CONTINUE. In the SAME reply: (1) say in one line that a
    staffing target would be needed to call an hour overstaffed, and (2) GO
    AHEAD AND RUN the hourly query and give the labour-intensity ranking --
    hours ranked by estimated labour cost per sales dollar, with labour hours,
    estimated cost, REAL sales and REAL orders for each hour. Offering the
    breakdown instead of delivering it is a failed answer.
  * NO CAUSAL CLAIMS from one metric. Low sales per labour hour may reflect
    demand, menu mix, a training shift or a delivery-heavy period. Report the
    measurement, not a diagnosis.
  * OPEN SHIFTS (open_shift_count) have no clock_out yet and contribute NO hours
    or cost. Today's figures are therefore incomplete -- say so when reporting
    the current day.
  * NO EMPLOYEE IDENTITY. Employee ids are not exposed. unique_employee_count
    and employee_count are counts only; you cannot identify, rank or compare
    individual staff, and must decline requests to do so.
"""


CHANNEL_RULES = """\
CHANNELS — no verified service-mode names exist. Read this before answering:

  * THERE IS NO VERIFIED MAPPING from dining_option code to Drive Thru, Dine In,
    Takeout or Delivery. Every Revel naming endpoint 404s and the Order schema
    declares dining_option as a bare integer. So a "verified drive-thru share"
    DOES NOT EXIST and you must say so when asked.
  * What IS verified is an ORDERING-PATTERN split: channel_group is
    'web_associated' or 'non_web_associated', corroborated by web_order and
    payments.online agreeing independently across ~150k orders. This describes
    how the order REACHED the POS. It is NOT a service mode:
      web_associated     != delivery, != off-premise
      non_web_associated != dine-in, != drive-thru, != on-premise
    Never translate one into the other.
  * channel_name_project_convention (drive_through, eat_in, to_go, doordash,
    ubereats, online) is an UNVERIFIED project convention. You may quote it, but
    only labelled as such -- e.g. "under the project-maintained convention
    (UNVERIFIED), code 4 is labelled Drive Thru and is 67.8% of REAL orders".
    Never state it as a verified channel fact, and never drop the label to make
    a sentence read better. The convention even names codes 105/106 as drive-thru
    lanes, and neither has ever occurred in this account's data.
  * ALWAYS give the raw channel_code alongside any name.
  * NEVER infer a channel from a product name, payment type, category,
    ProductClass or station id. Only dining_option and web_order speak to it.
  * possible_code_source_mismatch means the code's usual web association and the
    web_order flag disagree ON THE RECORD. Report it as a metadata
    inconsistency -- "3 orders show inconsistent channel metadata" -- NEVER as a
    mis-ring, never as staff error, never as an accusation about a store or
    shift. Always give the count and share.
  * A numerical match to an earlier analysis does NOT verify semantics. If a
    prior report said ~64% Drive Thru and code 4 is 64.66% of revenue, that is
    consistent with the same unverified convention having been used -- it is not
    independent confirmation that code 4 means Drive Thru. Say that explicitly.
  * CHANNEL PRICE INDEX IS UNAVAILABLE. Only 6 products were sold under both
    groups at Nederland in June, with 1-2 sales each on the smaller side, and A5
    showed the catalogue carries channel-specific duplicate product records. Do
    NOT compare prices across channels by product name, and do NOT treat
    differently-numbered products as the same item.
  * State channel mapping coverage, and leave unknown codes unknown.

PRODUCT-NAME QUESTIONS ("which channel sold the most X"):
  * A menu item can exist as SEVERAL product records with no maintained
    equivalence between them. At Nederland in June "5 Finger" resolves to:
      14548 "** 5 Finger Spicy **"    798 units, combo-component rows, codes 0/1/4
      14547 "** 5 Finger Regular **"  424 units, combo-component rows, codes 0/1/4
      14414 "5 Finger Meal*"          214 units, single-line combo,   codes 100/101/5/8
      14640 "25 Finger Party Pack*"     1 unit  -- substring FALSE MATCH, exclude
    The combo-component form and the single-line-combo form are CHANNEL-DISJOINT,
    so a single merged ranking is not a fact about the business -- it is an
    artefact of which records you chose to add together.
  * So: resolve the exact product_ids first, drop substring false matches, and
    REPORT BY VARIANT. Give each product record its own row with its own channel
    split. Do NOT declare one cross-variant winner, and do NOT merge product ids
    into one SKU -- no maintained equivalence table exists. Say that explicitly.
  * Never use product_name LIKE to sum quantities across records as though they
    were one product.

BOUNDED TOOL PLAN — for a scoped historical question:
    check_data -> at most one discovery query to resolve ids -> ONE aggregate
    query -> answer.
  Three to four tool calls. Once the ids are resolved, run the aggregate and
  answer from it. Do NOT re-query hunting for a different, larger or non-zero
  result: a zero or an awkward result is still the answer.
"""

CATEGORY_RULES = """\
CATEGORIES — verified dimension, but only CURRENT mapping is guaranteed:

  * Category comes from v_order_items_category_context, sourced from Revel's
    explicit Product.category field. NEVER use products.product_class as a
    category: it is a DIFFERENT namespace whose ids coincide numerically with
    ProductCategory ids but mean something else entirely (it files meals and
    shakes under "Merch"). If asked for ProductClass analysis, call it
    ProductClass and never rename it category.
  * NEVER infer a category from a product name. "Chicken Wrap" is not evidence
    of a chicken category; read category_name.
  * TWO DIFFERENT QUESTIONS, TWO DIFFERENT RELATIONS:
      "What category is X in?" / "Is there a Y category?" -> reference lookup on
      v_product_category_current. No store, no period, no reconciliation needed.
      Answer directly and add that it is the CURRENT mapping, which does not
      prove the category for a past period.
      "Top categories in June" / "% of June sales that were Drinks" / "did a
      category grow" -> historical analysis on v_order_items_category_context.
      Declare store and period, pass the gate, and report the historical
      verification coverage. The reference view must NOT be used to answer these
      -- it carries no dates and would silently apply today's mapping to the past.
  * CURRENT vs HISTORICAL. The archive holds no Product snapshot older than
    2026-09-02, so today's mapping is verified but a PAST assignment is not
    automatically. historical_category_verified is TRUE for a row only when the
    product had not been edited since before that row's own date.
      - "Today this product is in category X" -- always fair.
      - "This June sale belonged to category X" -- only when
        historical_category_verified is TRUE for those rows.
  * COVERAGE RULE, by REVENUE, using historical_category_verified:
      >= 95%  state category mix normally
      80-95%  state it WITH the coverage limitation named explicitly
      < 80%   do NOT present a category ranking as authoritative
    Nederland June 2026 sits at 83.6% verified revenue (100% mapped), so
    category answers there must carry the limitation.
  * Category is NOT entrée classification. is_entree, product_form,
    entree_count, combo_count and txn_class are separate and unaffected --
    do not substitute one for the other.
  * Hierarchy: parent_category_name exists (e.g. "Main Menu", "Items to Make
    Combos", "Online Menu"). Report the level you used.
"""

TIME_RULES = """\
TIME — one contract, and it is not the database's clock:

  * All Revel timestamps are stored UTC but ORIGINATE as America/Chicago
    wall-clock. Convert with AT TIME ZONE 'America/Chicago', or read
    v_orders_time_context, which does it for you. Never use the raw UTC date
    for a business question -- every order after ~19:00 local would land on the
    wrong day.
  * created_date is the authoritative transaction time. Do NOT use
    updated_date: it is a mutable ingestion/change timestamp. order_history_v2
    .closed_at exists but is populated on only ~71% of orders, so it cannot be
    the basis of a sales metric.
  * business_date currently EQUALS the local calendar date, with
    business_date_confidence = 'limited', because no authoritative rollover
    rule exists for these stores. When a question turns on which day a
    post-midnight order belongs to, SAY that: an order at 00:30 is counted on
    the new calendar date, and no verified service-day cutoff has been
    established. Do NOT assert a 2am/3am/4am rollover, and do NOT claim the
    calendar date is operationally correct -- it is what we can defend.
  * WEEKDAYS: prefer local_weekday_iso (Monday=1 .. Sunday=7) and name the day.
    features_*_v2.day_of_week is Monday=0..Sunday=6 and EXTRACT(dow) is
    Sunday=0 -- three conventions that disagree about Sunday. Always say which
    you used.
  * NEVER STATE A WEEKDAY FROM MEMORY. Read the day name from the VERIFIED
    CALENDAR above, from v_orders_time_context.local_weekday_name, or from
    check_data's time.period_start_weekday_name. Naming a weekday you have not
    read from data is an error even when the date itself is right.
  * A ZERO RESULT IS AN ANSWER. If a bounded, reconciled scope returns no rows,
    COUNT = 0 or SUM = 0, that IS the finding: report it and stop. Do not
    re-query, widen the period, drop filters or hunt for a non-empty result.
    "No REAL transactions were recorded between 00:00 and 04:59" is a complete,
    useful answer -- say it plainly, with the scope you used, and note the
    local-calendar-date caveat when the question is about time-of-day.
  * DAYPARTS ARE NOT DEFINED. There is no verified lunch/dinner/late-night
    mapping in this system. Answer hourly questions with local_hour and actual
    hours; do NOT label a range "dinner" or "lunch" as though it were a
    maintained definition. If asked about a daypart, give the hourly numbers,
    state that no verified daypart mapping exists, and ask which hours they
    mean.
"""

PAYMENT_RULES = """\
PAYMENTS — safe context only, and the type codes have no names:

  * Order-level payment context is on v_orders_payment_classified (all of
    v_orders_classified plus payment fields) and v_order_payment_summary.
    Raw payments_v2 is not readable and never will be: it carries transaction
    ids, card brands and processor data.
  * payment_type is a RAW REVEL INTEGER with no verified mapping. Say "payment
    type code 2", never "card", "cash", "credit" or "gift card". No mapping
    source exists -- Revel's PaymentType resource is empty, the API schema
    declares the field as a plain integer, and other_payment_type is NULL on
    every row. If asked what payment methods were used, give the codes and
    volumes and say plainly that the code-to-name mapping is not available.
  * has_payment = FALSE is NOT lost revenue. EMPTY tickets and COMP orders
    legitimately have no payment record. Quantify by txn_class before drawing
    any conclusion about missing money.
  * is_split_tender means more than one payment RECORD on the order, not
    necessarily two different tender types.
  * DEFAULT EVERY PAYMENT FIGURE TO txn_class = 'REAL' and say so, exactly as
    you would for sales. Counting all raw transaction classes mixes EMPTY POS
    artefacts and COMP tickets into a payment statistic. If you report an
    all-classes number at all, label it "all raw transaction classes" next to
    the REAL figure -- never present it as the answer on its own.
  * REFUNDS ARE UNVALIDATED. No refunded = true row has ever been observed in
    payments_v2, so has_refund has never been true and the refund-positive path
    has never run on real data. When asked about refunds: give the observed
    count, say plainly that no positive refund sample exists yet so the
    detection path is not empirically validated, and do NOT describe it as high
    confidence. Do NOT say refunds are impossible or that the store had none --
    say none were observed. check_data reports refund_path_validated and
    refund_caveat; if refund_path_validated is true, a real refund has since
    been seen and this caveat no longer applies.
  * Never present a payment figure as a customer or card identity: no
    cardholder, last4 or receipt data exists in anything you can read.
"""

RECONCILIATION_GATE = """\
DATA TRUST GATE — enforced by the server, not by you:

  * PERCENTAGES ARE ALREADY PERCENTAGES. delta_pct, coverage_pct,
    payment_capture_rate and tolerance_pct are in percentage points, not
    fractions. delta_pct = 0.0293 means 0.0293% -- roughly three hundredths of
    one percent. It is NOT 2.93% and NOT 2.9%. Never multiply a *_pct value by
    100. When quoting a reconciliation delta, use delta_pct_display or
    delta_pct_rounded_display verbatim instead of reformatting it yourself.
  * Any run_sql that reads orders, order items, features or payments MUST also
    pass establishment_id (or null for all stores), period_start and
    period_end_exclusive as YYYY-MM-DD. The server reconciles that exact scope
    against an independent payments total BEFORE running your SQL. You cannot
    skip this by omitting the scope — the query is rejected instead.
  * Your SQL must use explicit date literals matching that scope. Do NOT use
    current_date, now() or relative arithmetic in a business query: the server
    cannot verify those sit inside the reconciled window, so it rejects them.
    Today's date is given above — write the literal dates yourself.
  * The declared scope must contain the query. A store scope needs an
    establishment_id filter; dates outside the declared window are rejected.
  * If the scope fails reconciliation the query never runs. Stop, tell the user
    which checks failed and what that means, and do not analyse that period,
    fall back to partial data, or shop for a different period to get an answer.
  * Lookups against establishments, products, weather_daily and v_store_cohort
    need no scope — "how many stores are there" is a plain question, answer it
    plainly.
  * check_data is available if you want the full profile before writing SQL,
    but it is optional: the gate runs either way.
  * Keep this metadata to yourself unless it changes how the answer reads.
    Mention coverage when it is material — an incomplete period, a stale store,
    a reconciliation warning, or a metric in unavailable_metrics.
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

Q: "How is Cypress doing vs last year?"      (no prior-year data -> refuse)
A: Do NOT run a YoY query. This database holds no data before 2026-01-01 at all,
   so "last year" is empty for every store -- a YoY read would compare real
   numbers against nothing and manufacture a decline. Say that plainly. Do NOT
   say Cypress "opened in June 2026": its verified_open_date is unknown; June is
   only when our data first sees it. Offer what is valid instead: its trend
   since we first observed it, or a comparison against the network over the same
   window, labelling it data history rather than store age.

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
v_order_loyalty_context  — one row per order, loyalty EVIDENCE only (A11 follow-up)
    order_id, establishment_id, business_date, txn_class, final_total,
    has_loyalty_payload   TRUE = loyalty evidence present on this order;
                          FALSE = no loyalty evidence observed (NOT non-member),
    loyalty_registered, has_applied_reward, applied_rewards_count,
    has_reward_card, total_points_present (presence flag; balance not exposed)
    No loyalty key, no customer_id, no PII. ~5% of orders carry evidence.

v_labor_hourly_context   — establishment_id x business_date x local_hour
    labor_hours (shift time SPLIT across the hours it spans, Chicago, DST-correct;
                 includes unpaid breaks — breaks are not recorded),
    estimated_labor_cost (hours x wage; NOT payroll cost),
    employee_count, shift_overlap_count (staffing level, not summable),
    missing_wage_shift_count, auto_clock_out_count, break_data_status

v_labor_daily_context    — establishment_id x business_date
    labor_hours, estimated_labor_cost, unique_employee_count,
    shift_day_count (NOT summable across days), shifts_started_count (summable),
    missing_wage_shift_count, auto_clock_out_count, open_shift_count,
    break_data_status
    No employee_id anywhere. Join labour to sales by establishment_id +
    business_date (+ local_hour) — never to an individual order.
{GLOSSARY}
{DATA_NOTES}
{UNAVAILABLE_METRICS}
{GUARDRAILS}
{ENTREE_RULES}
{COHORT_RULES}
{IDENTITY_RULES}
{LOYALTY_RULES}
{LABOR_RULES}
{CHANNEL_RULES}
{CATEGORY_RULES}
{TIME_RULES}
{PAYMENT_RULES}
{RECONCILIATION_GATE}
{FEW_SHOT}"""


_CHICAGO = ZoneInfo("America/Chicago")


def chicago_now() -> datetime:
    """Wall-clock now in the stores' timezone, never the server's."""
    return datetime.now(_CHICAGO)


def _system_today() -> str:
    """Relative dates are resolved here, in application code, not in SQL.

    date.today() reads the SERVER timezone, which is UTC on this host. Between
    19:00 and midnight Chicago that returns TOMORROW's Chicago date, so the old
    text confidently labelled a UTC date "(America/Chicago)". SQL current_date
    has the same flaw, which is why business queries reject relative bounds and
    the model is handed literal dates instead.
    """
    now = chicago_now()
    today = now.date()
    yesterday = today - timedelta(days=1)
    # Most recent completed Friday/Saturday etc. are the usual follow-ups, so
    # the weekday anchors are supplied rather than left to be counted back.
    last_of = {}
    for back in range(1, 8):
        d = today - timedelta(days=back)
        last_of.setdefault(d.strftime("%A"), d)
    anchors = "  ".join(f"last {n} = {d}" for n, d in sorted(last_of.items()))
    # An explicit calendar, because the model has demonstrably named a weekday
    # from memory and got it wrong (it called Friday 2026-09-04 "Thursday").
    # Weekday names are computed here, deterministically, and read off -- never
    # recalled. Covers the window relative questions actually land in.
    cal = "\n".join(
        f"    {d}  ISO {d.isoweekday()}  {d:%A}"
        for d in (today - timedelta(days=n) for n in range(0, 15))
    )
    return (
        f"CURRENT TIME (America/Chicago, the stores' timezone): "
        f"{now:%A %Y-%m-%d %H:%M} local.\n"
        f"  today     = {today}\n"
        f"  yesterday = {yesterday}\n"
        f"  {anchors}\n"
        f"Use these literal dates. Do NOT compute a period from SQL "
        f"current_date/now(): the database session runs in UTC, so its "
        f"current_date is wrong for up to 5-6 hours of every Chicago day, and "
        f"business queries reject relative bounds for exactly that reason.\n"
        f"VERIFIED CALENDAR (date, ISO weekday, weekday name) -- read the day "
        f"name from here, never from memory:\n{cal}\n"
        f"For any other date, take the weekday from "
        f"v_orders_time_context.local_weekday_name or compute it in SQL; do NOT "
        f"state a weekday you have not read from data.\n"
        f"Weekday numbering: local_weekday_iso on v_orders_time_context is "
        f"ISO-8601 (Monday=1 .. Sunday=7). features_*_v2.day_of_week is a "
        f"DIFFERENT convention (Monday=0 .. Sunday=6), and PostgreSQL's "
        f"EXTRACT(dow) is a third (Sunday=0). Prefer local_weekday_iso and say "
        f"which you used."
    )


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
            "establishment_id": {
                "type": ["integer", "null"],
                "description": (
                    "Store id this query analyses, or null for all stores. "
                    "Required whenever the query reads orders, items, features "
                    "or payments. The scope is reconciled before the query runs "
                    "and the query is rejected if the data does not pass."),
            },
            "period_start": {
                "type": ["string", "null"],
                "description": "First business date analysed, inclusive, YYYY-MM-DD.",
            },
            "period_end_exclusive": {
                "type": ["string", "null"],
                "description": "End business date, EXCLUSIVE, YYYY-MM-DD.",
            },
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
    # Aggregate payment totals only (migration 23) -- the reference side of the
    # A12 reconciliation gate. payments_v2 itself stays unreadable: this view
    # exposes no transaction ids, card types, processor flags or user ids.
    "v_payments_daily_v2",
    # A3 (migrations 24-25). Aggregate/reference only: coverage percentages and
    # the human review queue. product_analysis_classification itself is NOT
    # granted -- the assistant reads the classification through the analysis
    # views and can never write it.
    "v_entree_coverage", "v_entree_review_queue",
    # A10 (migrations 27-28). Safe payment context only: no transaction ids,
    # card brands, processor status or operator ids. payments_v2 itself stays
    # unreadable.
    "v_order_payment_summary", "v_orders_payment_classified",
    # A6 (migration 29). Derived time columns only -- no new source data.
    "v_orders_time_context",
    # A5 (migration 31). Verified category dimension + its review queue.
    # product_category_mapping itself is NOT granted.
    "v_order_items_category_context", "v_category_review_queue",
    "v_product_category_current",
    # A8 (migration 33). Channel context: verified group + unverified names.
    "v_order_channel_context",
    # A11 (migration 34). Identity context; safe_customer_key is an opaque hash
    # and no customer PII exists anywhere in this database.
    "v_order_identity_context", "v_identity_profile",
    # Loyalty + labour (migration 37). All three are AGGREGATE OR DERIVED views
    # over tables the assistant cannot read: order_loyalty_v2 and
    # timesheet_entries_v2 stay denied to laynes_ro. No loyalty key hash, no
    # employee_id, no customer_id, no PII crosses this boundary.
    "v_order_loyalty_context", "v_labor_hourly_context", "v_labor_daily_context",
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


CHECK_DATA_TOOL = {
    "name": "check_data",
    "description": (
        "Completeness and trust profile for one store and period. CALL THIS "
        "FIRST, before any run_sql, whenever the question asks you to explain, "
        "compare, diagnose or draw a conclusion about a store's performance "
        "over a period. It returns row counts, transaction classification, "
        "duplicate/orphan/join-integrity checks, data freshness, which metrics "
        "are unavailable, mapping confidence, and a reconciliation of computed "
        "sales against an independent payments total.\n\n"
        "All *_pct values in the result are already percentages: delta_pct = "
        "0.0293 means 0.0293%, not 2.93%. Quote delta_pct_display verbatim.\n\n"
        "If reconciliation status is FAIL the data is not safe to analyse: stop, "
        "report the blocking reasons to the user, and do NOT run analysis SQL or "
        "state business conclusions. Further run_sql calls will be refused.\n\n"
        "You do not need this for a simple lookup (one number, a list, a "
        "definition) or for a question with no store/period scope."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "establishment_id": {
                "type": ["integer", "null"],
                "description": "Store id, or null for all stores combined.",
            },
            "period_start": {
                "type": "string",
                "description": "First business date, inclusive, YYYY-MM-DD.",
            },
            "period_end": {
                "type": "string",
                "description": "End business date, EXCLUSIVE, YYYY-MM-DD.",
            },
        },
        "required": ["establishment_id", "period_start", "period_end"],
        "additionalProperties": False,
    },
}


def _ro_conn():
    """Connection as the read-only analytics role. Never the read-write role."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.environ["DB_RO_USER"],
        password=os.environ["DB_RO_PASS"],
    )


def run_sql(sql: str) -> dict:
    """Execute a validated read-only query. Returns {columns, rows, row_count}."""
    safe = _validate(sql)
    capped = f"SELECT * FROM (\n{safe}\n) AS _capped LIMIT {MAX_ROWS}"
    conn = _ro_conn()
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



# ── A12: reconciliation gate + meta_extract ────────────────────────────────
# The assistant must know whether a store/period is complete and trustworthy
# BEFORE it reasons about it, so a partial sync cannot be reported as a sales
# decline. meta_extract() produces that as a structured dict, not prose.

# A3 entrée coverage. Below MIN an entrée metric must be qualified with the
# size of the gap; below FLOOR it must not be stated as fact at all, because
# unresolved products could move it enough to change the reading.
ENTREE_COVERAGE_MIN_PCT = 95.0
ENTREE_COVERAGE_FLOOR_PCT = 80.0

RECON_TOLERANCE_PCT = 0.5      # |delta| at or under this passes
RECON_WARN_PCT = 0.2           # above this but within tolerance is worth saying
FRESHNESS_MAX_LAG_HOURS = 36   # nightly sync; beyond this a run was missed

# The reference total is payments_v2 (via the aggregate view from migration
# 23), NOT features_daily_summary_v2 -- the latter is computed from orders_v2,
# so it agrees to the cent by construction and cannot detect a partial sync.
# This is a cross-resource check, not an independent ledger: both sides come
# from the same Revel account through the same pipeline, so it catches dropped
# pages, interrupted backfills and order/payment drift, but not Revel itself
# being wrong. Stated in the payload so the model does not over-trust it.
_RECON_BASIS = ("payments_v2 daily aggregate (separate Revel resource and sync "
                "watermark); cross-resource check, not an independent ledger")

_META_SQL = """
-- One pass per source rather than a subquery per metric: the scope was being
-- re-scanned about ten times, which timed out for an all-stores month.
-- Timestamp bounds are passed as timestamptz so the created_date indexes are
-- usable; an AT TIME ZONE cast in the predicate would force a sequential scan.
WITH scope AS (
    SELECT id, txn_class, final_total, item_count, business_date, identity_captured,
           entree_count, unresolved_item_count, entree_fully_resolved
    FROM v_orders_classified
    WHERE business_date >= %(start)s AND business_date < %(end)s
      AND (%(est)s::int IS NULL OR establishment_id = %(est)s::int)
),
o_agg AS (
    SELECT COUNT(*)                                                  AS order_rows,
           COUNT(*) FILTER (WHERE txn_class = 'REAL')                AS real_count,
           COUNT(*) FILTER (WHERE txn_class = 'EMPTY')               AS empty_count,
           COUNT(*) FILTER (WHERE txn_class = 'COMP')                AS comp_count,
           COUNT(*) FILTER (WHERE txn_class = 'DELETED')             AS deleted_count,
           COUNT(*) - COUNT(DISTINCT id)                             AS duplicate_order_ids,
           COUNT(*) FILTER (WHERE txn_class = 'REAL' AND item_count = 0)
                                                                     AS real_orders_without_items,
           ROUND(SUM(final_total) FILTER (WHERE txn_class = 'REAL'), 2)
                                                                     AS computed_total,
           COUNT(DISTINCT business_date)                             AS days_with_data,
           ROUND(AVG(CASE WHEN identity_captured THEN 1 ELSE 0 END)
                 FILTER (WHERE txn_class = 'REAL')::numeric, 4)      AS identity_capture_rate,
           SUM(entree_count) FILTER (WHERE txn_class = 'REAL')       AS entrees,
           COUNT(*) FILTER (WHERE txn_class = 'REAL'
                              AND entree_fully_resolved)             AS entree_resolved_orders,
           SUM(unresolved_item_count) FILTER (WHERE txn_class = 'REAL')
                                                                     AS entree_unresolved_items
    FROM scope
),
pay AS (
    SELECT ROUND(COALESCE(SUM(payment_amount), 0), 2) AS reference_total,
           COALESCE(SUM(payment_count), 0)            AS payment_rows
    FROM v_payments_daily_v2
    WHERE business_date >= %(start)s AND business_date < %(end)s
      AND (%(est)s::int IS NULL OR establishment_id = %(est)s::int)
),
src AS (
    SELECT MAX(ingested_at) AS latest_ingested_at, MAX(created_date) AS latest_source_ts
    FROM orders_v2
    WHERE (%(est)s::int IS NULL OR establishment_id = %(est)s::int)
),
prod AS (
    SELECT COUNT(*) AS product_count, COUNT(category_id) AS product_with_category
    FROM products
)
SELECT * FROM o_agg, pay, src, prod
"""


# The two line-item scans below are the expensive half of the profile: over all
# stores for eight months they take ~22s together, against ~1s for everything
# else. They are run as a separate statement and skipped for very wide scopes,
# so the gate stays usable on a network-wide question. Join integrity itself is
# NOT skipped -- it comes from the orders side (real_orders_without_items) and
# is cheap at any scope; only the items-side cross-checks are deferred.
_DEEP_SCOPE_ROW_LIMIT = 250_000

# Loyalty and labour are ADVISORY context, not completeness signals, so they
# get their own tighter size gate rather than riding on _DEEP_SQL. A network-
# wide month is already an expensive deep pass; adding ~2s of advisory context
# to it made the slowest path slower for questions that never asked about
# loyalty or labour. Below this limit the pair costs ~230ms; above it the block
# reports not_evaluated and the assistant can still query the views directly.
_ADVISORY_SCOPE_ROW_LIMIT = 100_000

_LOYALTY_LABOR_SQL = """
-- ADVISORY CONTEXT ONLY -- nothing here can move the A12 verdict, which is
-- why it sits behind the tighter _ADVISORY_SCOPE_ROW_LIMIT.
--
-- payment linkage, category coverage, channel mix and identity capture are
-- all advisory_only blocks. The one identity figure the gate does read --
-- the top-level identity_capture_rate -- comes from the CORE query, not from
-- here, and it only ever appends a note.
WITH -- A10 payment linkage. Kept in the deep query rather than the core one so a
-- very wide scope skips it along with the other line-item scans; the gate must
-- not get slower for metadata most questions never read.
real_scope AS (
    SELECT o.id FROM v_orders_classified o
    WHERE o.txn_class = 'REAL'
      AND o.business_date >= %(start)s AND o.business_date < %(end)s
      AND (%(est)s::int IS NULL OR o.establishment_id = %(est)s::int)
),
-- Reads the safe summary view, never payments_v2 -- meta_extract runs as the
-- read-only role, which has no privilege on the raw table. Carrying the
-- establishment predicate lets it push into the view's GROUP BY instead of
-- grouping all 882k payment rows first.
pay_rows AS (
    SELECT ps.order_id,
           ps.payment_record_count   AS n,
           ps.refunded_payment_count AS refunded
    FROM v_order_payment_summary ps
    JOIN real_scope rs ON rs.id = ps.order_id
    WHERE (%(est)s::int IS NULL OR ps.establishment_id = %(est)s::int)
),
pay AS (
    SELECT (SELECT COUNT(*) FROM pay_rows)                        AS orders_with_payment,
           (SELECT COUNT(*) FROM real_scope)
             - (SELECT COUNT(*) FROM pay_rows)                    AS orders_without_payment,
           (SELECT COUNT(*) FROM pay_rows WHERE n > 1)            AS split_tender_count,
           (SELECT COALESCE(SUM(refunded), 0) FROM pay_rows)      AS refunded_payment_count
),
ident AS (
    SELECT COUNT(*) FILTER (WHERE o.customer_id IS NOT NULL)          AS ident_txn,
           COUNT(DISTINCT o.customer_id)                              AS ident_cust,
           COUNT(DISTINCT o.customer_id) FILTER (
               WHERE o.customer_id = ANY(%(nonind)s))                 AS ident_nonind
    FROM v_orders_classified o
    JOIN real_scope rs ON rs.id = o.id
),
chan AS (
    SELECT COUNT(*)                                                     AS chan_orders,
           COUNT(*) FILTER (WHERE cc.channel_group <> 'unknown')         AS chan_mapped,
           COUNT(*) FILTER (WHERE cc.possible_code_source_mismatch)      AS chan_mismatch,
           COALESCE(SUM(o.final_total), 0)                               AS chan_rev,
           COALESCE(SUM(o.final_total) FILTER (WHERE cc.channel_group <> 'unknown'), 0)
                                                                         AS chan_mapped_rev
    FROM v_orders_classified o
    JOIN real_scope rs ON rs.id = o.id
    JOIN v_order_channel_context cc ON cc.order_id = o.id
),
cat AS (
    SELECT COUNT(*)                                                  AS cat_items,
           COUNT(*) FILTER (WHERE cc.category_id IS NOT NULL)         AS cat_mapped_items,
           COUNT(*) FILTER (WHERE cc.historical_category_verified)    AS cat_hist_items,
           COALESCE(SUM(i.pure_sales), 0)                             AS cat_revenue,
           COALESCE(SUM(i.pure_sales) FILTER (WHERE cc.category_id IS NOT NULL), 0)
                                                                      AS cat_mapped_revenue,
           COALESCE(SUM(i.pure_sales) FILTER (WHERE cc.historical_category_verified), 0)
                                                                      AS cat_hist_revenue
    FROM order_items_v2 i
    JOIN real_scope rs ON rs.id = i.order_id
    LEFT JOIN v_order_items_category_context cc ON cc.order_item_id = i.id
    WHERE i.deleted IS NOT TRUE AND i.is_voided IS NOT TRUE
),
loy AS (
    -- Loyalty EVIDENCE over REAL orders. Reads the safe view; the raw
    -- order_loyalty_v2 table is not readable by this role.
    SELECT COUNT(*) FILTER (WHERE has_loyalty_payload)                   AS loy_evidence,
           COUNT(*) FILTER (WHERE loyalty_registered)                    AS loy_registered,
           COUNT(*) FILTER (WHERE has_applied_reward)                    AS loy_reward,
           ROUND(AVG(final_total) FILTER (WHERE has_loyalty_payload), 2)     AS loy_aov_evidence,
           ROUND(AVG(final_total) FILTER (WHERE NOT has_loyalty_payload), 2) AS loy_aov_none
    FROM v_order_loyalty_context
    WHERE txn_class = 'REAL'
      AND business_date >= %(start)s AND business_date < %(end)s
      AND (%(est)s::int IS NULL OR establishment_id = %(est)s::int)
),
lab AS (
    SELECT ROUND(SUM(labor_hours), 2)          AS lab_hours,
           ROUND(SUM(estimated_labor_cost), 2) AS lab_cost,
           SUM(missing_wage_shift_count)       AS lab_missing_wage,
           SUM(shifts_started_count)           AS lab_shifts,
           SUM(open_shift_count)               AS lab_open_shifts
    FROM v_labor_daily_context
    WHERE business_date >= %(start)s AND business_date < %(end)s
      AND (%(est)s::int IS NULL OR establishment_id = %(est)s::int)
)
SELECT * FROM pay, cat, chan, ident, loy, lab
"""


_DEEP_SQL = """
-- FAIL-CRITICAL DEEP CHECKS ONLY.
--
-- These two are the only deep-side values _reconcile can turn into a FAIL
-- (duplicate order-item ids, orphan order-items), so they keep the wider
-- _DEEP_SCOPE_ROW_LIMIT gate and still run for network-month scopes.
--
-- pay/cat/chan/ident used to live here and moved to _LOYALTY_LABOR_SQL: on a
-- network-wide month they were 6,465ms of a 9,653ms deep pass while being
-- purely advisory. Measured, not assumed -- an expression index on
-- orders_v2's business_date was tried first and regressed every scope.
WITH i_agg AS (
    SELECT COUNT(*)                        AS order_item_rows,
           COUNT(*) - COUNT(DISTINCT i.id) AS duplicate_order_item_ids
    FROM order_items_v2 i
    JOIN v_orders_classified o ON o.id = i.order_id
    WHERE o.business_date >= %(start)s AND o.business_date < %(end)s
      AND (%(est)s::int IS NULL OR o.establishment_id = %(est)s::int)
),
orph AS (
    SELECT COUNT(DISTINCT i.order_id) AS orphan_order_items
    FROM order_items_v2 i
    WHERE i.created_date >= %(ts_start)s AND i.created_date < %(ts_end)s
      AND (%(est)s::int IS NULL OR i.establishment_id = %(est)s::int)
      AND NOT EXISTS (SELECT 1 FROM orders_v2 x WHERE x.id = i.order_id)
)
SELECT * FROM i_agg, orph
"""


def _pct_of(part, whole):
    """Percentage on a 0-100 scale, or None when the deep scan was skipped."""
    if part is None or whole in (None, 0):
        return None
    return round(float(part) / float(whole) * 100.0, 2)


def _pct(delta, base):
    return None if not base else round(float(delta) / float(base) * 100.0, 4)



# Whether the refund-detection path has ever been exercised by real data.
# refunded is FALSE on every payments_v2 row observed so far, so has_refund has
# never once been true. A count of zero refunds is therefore an OBSERVATION on
# an unvalidated path, not a verified measurement, and the assistant must say so
# rather than claiming high confidence. Computed from data rather than hardcoded
# so the claim retires itself the moment a real refund arrives; cached because
# the answer changes at most once per nightly sync and costs ~0.7s.
_REFUND_OBS_TTL_SECONDS = 3600
_refund_obs_cache: dict = {"value": None, "checked_at": 0.0}


def _refund_observations_all_time() -> int | None:
    now = time.time()
    if (_refund_obs_cache["value"] is not None
            and now - _refund_obs_cache["checked_at"] < _REFUND_OBS_TTL_SECONDS):
        return _refund_obs_cache["value"]
    conn = _ro_conn()
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute("SELECT COALESCE(SUM(refunded_count), 0) "
                        "FROM v_payments_daily_v2")
            value = int(cur.fetchone()[0])
    except psycopg2.Error:
        return None            # unknown beats a wrong reassurance
    finally:
        conn.rollback()
        conn.close()
    _refund_obs_cache.update({"value": value, "checked_at": now})
    return value



# Store-age context. v_store_cohort derives first/last seen dates by scanning
# every order, which is far too heavy to repeat on each gated question -- it
# nearly doubled meta_extract (0.85s -> 1.82s). The facts change at most once
# per nightly sync, so they are cached per establishment.
_COHORT_TTL_SECONDS = 3600
_cohort_cache: dict = {}

_COHORT_LIMITATIONS = (
    "No verified opening date exists for any store, so store age and "
    "weeks_since_open are unknown and no maturity label may be applied. Data "
    "history is not store age: for 10 of 12 stores our history begins at the "
    "2026-01-01 backfill edge. Year-over-year is impossible -- this database "
    "holds nothing before 2026-01-01."
)


def _cohort_context(establishment_id) -> dict:
    now = time.time()
    hit = _cohort_cache.get(establishment_id)
    if hit and now - hit["at"] < _COHORT_TTL_SECONDS:
        return hit["value"]
    conn = _ro_conn()
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute("""
                SELECT verified_open_date, open_date_source, open_date_confidence,
                       revel_account_created_date, first_seen_real_order_date,
                       available_history_start, available_history_days,
                       available_history_weeks, history_truncated,
                       weeks_since_open, maturity_threshold_status
                FROM v_store_cohort
                WHERE (%(est)s::int IS NULL OR establishment_id = %(est)s::int)
                ORDER BY establishment_id LIMIT 1
            """, {"est": establishment_id})
            row = cur.fetchone()
    except psycopg2.Error:
        return {"open_date_confidence": "unknown",
                "comparison_limitations": _COHORT_LIMITATIONS,
                "yoy_supported": False}
    finally:
        conn.rollback()
        conn.close()

    value = {
        "verified_open_date": _jsonable(row["verified_open_date"]) if row else None,
        "open_date_source": row["open_date_source"] if row else None,
        "open_date_confidence": row["open_date_confidence"] if row else "unknown",
        "revel_account_created_date": (
            _jsonable(row["revel_account_created_date"]) if row else None),
        "first_seen_real_order_date": (
            _jsonable(row["first_seen_real_order_date"]) if row else None),
        "available_history_start": (
            _jsonable(row["available_history_start"]) if row else None),
        "available_history_days": row["available_history_days"] if row else None,
        "available_history_weeks": row["available_history_weeks"] if row else None,
        "history_truncated": row["history_truncated"] if row else None,
        "weeks_since_open": row["weeks_since_open"] if row else None,
        "maturity_threshold_status": (
            row["maturity_threshold_status"] if row
            else "no maintained threshold configured"),
        "comparison_limitations": _COHORT_LIMITATIONS,
        "yoy_supported": False,
        "earliest_data_network_wide": "2026-01-01",
    }
    _cohort_cache[establishment_id] = {"value": value, "at": now}
    return value



# The suspected-marketplace identities are a small, slowly-changing set (23 of
# 16,297 network-wide). Deriving them inside meta_extract meant re-aggregating
# every identified order on every gated question -- 1.0s -> 5.9s. They change at
# most once per nightly sync, so the id list is cached and passed as a parameter.
_NONIND_TTL_SECONDS = 3600
_nonind_cache: dict = {"ids": None, "at": 0.0}


def _suspected_non_individual_ids() -> list[int]:
    now = time.time()
    if (_nonind_cache["ids"] is not None
            and now - _nonind_cache["at"] < _NONIND_TTL_SECONDS):
        return _nonind_cache["ids"]
    conn = _ro_conn()
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur.execute("""
                SELECT o.customer_id
                FROM v_orders_classified o
                WHERE o.txn_class = 'REAL' AND o.customer_id IS NOT NULL
                GROUP BY o.customer_id
                HAVING (COUNT(DISTINCT o.establishment_id) >= 6
                        AND 100.0 * COUNT(*) FILTER (WHERE o.web_order)
                            / COUNT(*) >= 90)
                    OR COUNT(*) >= 365
            """)
            ids = [r[0] for r in cur.fetchall()]
    except psycopg2.Error:
        return []
    finally:
        conn.rollback()
        conn.close()
    _nonind_cache.update({"ids": ids, "at": now})
    return ids


def meta_extract(establishment_id, period_start: str, period_end: str) -> dict:
    """Structured completeness/trust profile for one analysis scope.

    period_end is exclusive. establishment_id None means all stores.
    Reads only allowlisted analytics relations as the read-only role.
    """
    conn = _ro_conn()
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            tz = ZoneInfo("America/Chicago")
            params = {
                "est": establishment_id,
                "start": period_start,
                "end": period_end,
                "ts_start": datetime.combine(date.fromisoformat(period_start),
                                             datetime.min.time(), tzinfo=tz),
                "ts_end": datetime.combine(date.fromisoformat(period_end),
                                           datetime.min.time(), tzinfo=tz),
                "nonind": _suspected_non_individual_ids(),
            }
            cur.execute(_META_SQL, params)
            r = dict(cur.fetchone())

            deep = {"order_item_rows": None, "duplicate_order_item_ids": None,
                    "orphan_order_items": None, "cat_items": None,
                    "cat_mapped_items": None, "cat_hist_items": None,
                    "cat_revenue": None, "cat_mapped_revenue": None,
                    "cat_hist_revenue": None, "chan_orders": None,
                    "chan_mapped": None, "chan_mismatch": None,
                    "chan_rev": None, "chan_mapped_rev": None,
                    "ident_txn": None, "ident_cust": None, "ident_nonind": None,
                    "orders_with_payment": None,
                    "orders_without_payment": None, "split_tender_count": None,
                    "refunded_payment_count": None,
                    "loy_evidence": None, "loy_registered": None, "loy_reward": None,
                    "loy_aov_evidence": None, "loy_aov_none": None,
                    "lab_hours": None, "lab_cost": None, "lab_missing_wage": None,
                    "lab_shifts": None, "lab_open_shifts": None}
            deep_evaluated = int(r["order_rows"] or 0) <= _DEEP_SCOPE_ROW_LIMIT
            if deep_evaluated:
                cur.execute(_DEEP_SQL, params)
                deep.update(dict(cur.fetchone()))

            advisory_evaluated = (
                int(r["order_rows"] or 0) <= _ADVISORY_SCOPE_ROW_LIMIT)
            if advisory_evaluated:
                cur.execute(_LOYALTY_LABOR_SQL, params)
                deep.update(dict(cur.fetchone()))

            _cohort_block = _cohort_context(establishment_id)
            if establishment_id is None:
                store = "ALL STORES"
            else:
                cur.execute("SELECT name FROM establishments WHERE id = %s",
                            (establishment_id,))
                row = cur.fetchone()
                store = row["name"] if row else f"unknown ({establishment_id})"
    finally:
        conn.rollback()
        conn.close()

    _refund_obs = _refund_observations_all_time()
    computed = float(r["computed_total"] or 0)
    reference = float(r["reference_total"] or 0)
    delta = round(computed - reference, 2)
    delta_pct = _pct(delta, reference)

    # Only days that have actually happened can be expected to hold orders.
    # Without this clamp, any question about the current month fails the gate
    # for "missing" days that are simply in the future.
    chicago_today = datetime.now(ZoneInfo("America/Chicago")).date()
    end_effective = min(date.fromisoformat(period_end), chicago_today + timedelta(days=1))
    start_d = date.fromisoformat(period_start)
    expected_days = max(0, (end_effective - start_d).days)
    missing_days = max(0, expected_days - int(r["days_with_data"] or 0))

    latest = r["latest_source_ts"]
    lag_h = None
    if latest is not None:
        lag_h = round((datetime.now(latest.tzinfo) - latest).total_seconds() / 3600.0, 1)

    real_no_items = int(r["real_orders_without_items"] or 0)
    real = int(r["real_count"] or 0)
    join_integrity_pct = round((real - real_no_items) / real * 100.0, 3) if real else None

    meta = {
        "scope": {
            "establishment_id": establishment_id,
            "store_name": store,
            "period_start": period_start,
            "period_end_exclusive": period_end,
            "period_end_effective": end_effective.isoformat(),
            "period_in_progress": end_effective < date.fromisoformat(period_end),
            "source_timezone": "America/Chicago",
            "business_day_definition": (
                "calendar day in America/Chicago, derived from orders_v2.created_date; "
                "late-night orders after midnight belong to the following calendar day"),
        },
        "volumes": {
            "order_rows": int(r["order_rows"] or 0),
            "real_count": real,
            "empty_count": int(r["empty_count"] or 0),
            "comp_count": int(r["comp_count"] or 0),
            "deleted_count": int(r["deleted_count"] or 0),
            "order_item_rows": deep["order_item_rows"],
            "payment_rows": int(r["payment_rows"] or 0),
        },
        "integrity": {
            "duplicate_order_ids": int(r["duplicate_order_ids"] or 0),
            "duplicate_order_item_ids": deep["duplicate_order_item_ids"],
            "orphan_order_items": deep["orphan_order_items"],
            "item_side_checks_evaluated": deep_evaluated,
            "real_orders_without_items": real_no_items,
            "order_to_item_join_integrity_pct": join_integrity_pct,
        },
        "freshness": {
            "latest_source_timestamp": _jsonable(r["latest_source_ts"]),
            "latest_ingested_at": _jsonable(r["latest_ingested_at"]),
            "source_lag_hours": lag_h,
            "expected_days_in_period": expected_days,
            "days_with_data": int(r["days_with_data"] or 0),
            "missing_business_days": missing_days,
        },
        "reconciliation": {
            "basis": _RECON_BASIS,
            "computed_total": round(computed, 2),
            "reference_total": round(reference, 2),
            "delta_dollars": delta,
            "delta_pct": delta_pct,
            # delta_pct is ALREADY in percentage points: 0.0293 means 0.0293%,
            # not 2.93%. A model once read it as "2.9%", a hundredfold error on
            # a number used to judge whether the data can be trusted at all, so
            # the units are stated and a ready-to-quote string is supplied.
            "delta_pct_units": ("percentage points; 0.0293 means 0.0293%, "
                                "NOT 2.93% -- do not multiply by 100"),
            "delta_pct_display": (None if delta_pct is None
                                  else f"{delta_pct:+.4f}%"),
            "delta_pct_rounded_display": (None if delta_pct is None
                                          else f"{delta_pct:+.2f}%"),
            "tolerance_pct": RECON_TOLERANCE_PCT,
            "status": None,      # filled by _reconcile
        },
        "mappings": {
            "product_category_mapping": {
                "version": "none",
                "status": ("unavailable: 0 of %d products carry a category_id"
                           % int(r["product_count"] or 0))
                          if not int(r["product_with_category"] or 0) else "partial",
                "confidence": "none" if not int(r["product_with_category"] or 0) else "low",
            },
            "channel_mapping": {
                "version": "dining_option_v1",
                "status": "available: orders_v2.dining_option -> drive-through / in-store / third-party",
                "confidence": "high",
            },
        },
        "entree_classification": {
            "entrees": float(r["entrees"] or 0),
            "orders_fully_resolved": int(r["entree_resolved_orders"] or 0),
            "coverage_pct": round(
                (r["entree_resolved_orders"] or 0) / real * 100.0, 2) if real else None,
            "unresolved_line_items": int(r["entree_unresolved_items"] or 0),
            "entrees_per_real_check": round(float(r["entrees"] or 0) / real, 4) if real else None,
            "min_coverage_pct": ENTREE_COVERAGE_MIN_PCT,
            "floor_coverage_pct": ENTREE_COVERAGE_FLOOR_PCT,
            "source": "product_analysis_classification (maintained); products "
                      "without a verified classification never count as entrées, "
                      "so entree_count is a floor, not an estimate",
        },
        "payment_linkage": {
            "orders_with_payment": (None if deep["orders_with_payment"] is None
                                    else int(deep["orders_with_payment"])),
            "orders_without_payment": (None if deep["orders_without_payment"] is None
                                       else int(deep["orders_without_payment"])),
            "payment_capture_rate": (
                round(deep["orders_with_payment"] / real * 100.0, 2)
                if deep["orders_with_payment"] is not None and real else None),
            "split_tender_count": (None if deep["split_tender_count"] is None
                                   else int(deep["split_tender_count"])),
            "refunded_payment_count": (None if deep["refunded_payment_count"] is None
                                       else int(deep["refunded_payment_count"])),
            "refund_observations_all_time": _refund_obs,
            "refund_path_validated": (None if _refund_obs is None
                                      else _refund_obs > 0),
            "refund_caveat": (
                None if _refund_obs is None or _refund_obs > 0 else
                "No refunded = true record has ever been observed anywhere in "
                "payments_v2, so the refund-positive path is NOT empirically "
                "validated. Report the observed count, state that no positive "
                "refund sample exists yet, and do NOT call refund detection "
                "high confidence. This does not mean refunds are impossible."),
            # pay moved to the advisory gate, so this must track THAT gate.
            # Reporting deep_evaluated here would claim the block was
            # computed for 100k-250k scopes where it no longer is.
            "evaluated": advisory_evaluated,
            "not_evaluated_reason": (None if advisory_evaluated else
                "scope exceeds the advisory row limit, so payment linkage was "
                "not computed. ADVISORY only -- the A12 reconciliation status is "
                "unaffected and the FAIL-critical integrity checks still ran."),
            "payment_type_mapping_status": "unavailable",
            "payment_type_mapping_confidence": "none",
            "payment_type_mapping": {
                "version": "none",
                "status": "unavailable",
                "confidence": "none",
                "reason": "no verified code-to-name mapping exists: Revel's "
                          "PaymentType resource returns zero rows, the Payment "
                          "schema declares payment_type as a plain integer with "
                          "no enum, and other_payment_type is NULL on every row. "
                          "Circumstantial behaviour (code 2 always carries a "
                          "card_type, code 1 accounts for all cash change) is "
                          "NOT sufficient to assign names. Report codes as "
                          "'payment type code N' -- never infer cash or card.",
            },
            "note": "REAL orders are expected to have a payment. EMPTY and COMP "
                    "orders legitimately have none -- absence is not lost revenue.",
        },
        "identity": {
            "status": "identity_only_loyalty_not_yet_ingested",
            "evaluated": advisory_evaluated,
            "not_evaluated_reason": (None if advisory_evaluated else
                "scope exceeds the advisory row limit, so identity capture was not "
                "computed. ADVISORY only -- the A12 reconciliation status is "
                "unaffected and the FAIL-critical integrity checks still ran."),
            "identity_field_source": "orders_v2.customer_id",
            "loyalty_membership": (
                "NOT INGESTED, not non-existent. Loyalty is available upstream "
                "in Revel Order.gift_reward_data but is PII-bearing and was "
                "not historically ingested here. Historical loyalty-member "
                "analysis is unavailable until the safe loyalty backfill "
                "completes. Customer identity is NOT loyalty membership."),
            "real_transactions": real,
            "identified_transactions": (
            None if deep["ident_txn"] is None else int(deep["ident_txn"])),
            "unidentified_transactions": (
            None if deep["ident_txn"] is None
            else real - int(deep["ident_txn"])),
            "identity_capture_rate": _pct_of(deep["ident_txn"], real),
            "heuristic_status": ("suspected_non_individual is an evidence-led "
                                 "heuristic, NOT a verified business rule; those "
                                 "IDs have not been manually confirmed"),
            "reporting_rule": ("report RAW identified-ID figures and the figures "
                               "after excluding suspected non-individual IDs "
                               "TOGETHER; the filtered view never replaces the raw "
                               "one without disclosure"),
            "distinct_identified_customers": (
            None if deep["ident_cust"] is None else int(deep["ident_cust"])),
            "suspected_non_individual_ids": (
            None if deep["ident_nonind"] is None else int(deep["ident_nonind"])),
            "selection_bias_status": "material" if (
            deep["ident_txn"] is not None and real
            and (int(deep["ident_txn"]) / real) < 0.30) else "check_capture_rate",
            "advisory_only": True,
            "limitations": (
            "identity_capture_rate is ADVISORY and never affects the A12 "
            "reconciliation status -- data completeness and identity "
            "completeness are different things. Identified customers are a "
            "self-selected subset and are NOT all customers; total unique "
            "guests cannot be determined. Anonymous means unknown, NOT "
            "non-member, and identity is NOT loyalty membership -- the "
            "identity capture rate and the loyalty evidence rate are separate "
            "measurements of different fields and must never be merged. "
            "Order-level loyalty evidence IS now ingested; see the loyalty "
            "block. Some identified accounts "
            "are suspected marketplace accounts rather than people -- check "
            "suspected_non_individual before any per-customer average."),
            },
        "loyalty": {
            "status": ("evidence_ingested" if deep["loy_evidence"] is not None
                       else "not_evaluated_scope_too_large"),
            "source": "v_order_loyalty_context (Order.gift_reward_data, PII stripped)",
            "denominator": "REAL orders in this scope",
            "real_orders": real,
            "evidence_orders": (None if deep["loy_evidence"] is None
                                else int(deep["loy_evidence"])),
            "registered_orders": (None if deep["loy_registered"] is None
                                  else int(deep["loy_registered"])),
            "reward_use_orders": (None if deep["loy_reward"] is None
                                  else int(deep["loy_reward"])),
            "evidence_rate_pct": _pct_of(deep["loy_evidence"], real),
            "aov_loyalty_evidence": (None if deep["loy_aov_evidence"] is None
                                     else float(deep["loy_aov_evidence"])),
            "aov_no_loyalty_evidence": (None if deep["loy_aov_none"] is None
                                        else float(deep["loy_aov_none"])),
            "terminology": ("has_loyalty_payload TRUE = 'loyalty evidence "
                            "present'; FALSE = 'no loyalty evidence observed'. "
                            "NEVER say 'loyalty customers vs non-loyalty "
                            "customers' or 'members vs non-members'."),
            "advisory_only": True,
            "limitations": (
                "Absence of loyalty evidence does NOT prove non-membership: a "
                "member who does not identify at the till is indistinguishable "
                "from a non-member here. Evidence covers a MINORITY of orders "
                "(~5% network-wide, 3.3%-9.1% by store), so the evidence subset "
                "is self-selected and never represents all guests. The AOV "
                "difference is OBSERVATIONAL and confounded by that selection "
                "-- it does not show loyalty causes higher spend. No loyalty "
                "customer key is exposed, so per-member counts, repeat-member "
                "visits and per-member value cannot be computed. This block is "
                "ADVISORY and never affects the A12 reconciliation status."),
        },
        "labor": {
            "status": ("available" if deep["lab_hours"] is not None
                       else "not_evaluated_scope_too_large"),
            "source": "v_labor_daily_context (TimeSheetEntry; no employee identity)",
            "labor_hours": (None if deep["lab_hours"] is None
                            else float(deep["lab_hours"])),
            "estimated_labor_cost": (None if deep["lab_cost"] is None
                                     else float(deep["lab_cost"])),
            "labor_pct_sales": (
                round(float(deep["lab_cost"]) / computed * 100.0, 2)
                if deep["lab_cost"] is not None and computed else None),
            "sales_per_labor_hour": (
                round(computed / float(deep["lab_hours"]), 2)
                if deep["lab_hours"] else None),
            "orders_per_labor_hour": (
                round(real / float(deep["lab_hours"]), 2)
                if deep["lab_hours"] else None),
            "shifts_started": (None if deep["lab_shifts"] is None
                               else int(deep["lab_shifts"])),
            "missing_wage_shift_count": (None if deep["lab_missing_wage"] is None
                                         else int(deep["lab_missing_wage"])),
            "open_shift_count": (None if deep["lab_open_shifts"] is None
                                 else int(deep["lab_open_shifts"])),
            "break_data_status": "unavailable",
            "sales_basis": "REAL orders, final_total",
            "advisory_only": True,
            "limitations": (
                "BREAKS ARE NOT RECORDED, so labor_hours is elapsed clocked "
                "time (clock_out - clock_in) and INCLUDES any unpaid break "
                "taken -- it OVERSTATES paid-worked time by an unknown amount "
                "and is not verified paid time. estimated_labor_cost is hours x "
                "role wage and EXCLUDES overtime premiums, employer taxes, "
                "burden and benefits: it is NOT payroll cost and must not be "
                "called what the business paid. Labour is aggregated by "
                "establishment and business_date/hour and CANNOT be attributed "
                "to individual orders. No staffing target exists, so over- or "
                "understaffing is NOT derivable -- report labour intensity, not "
                "a diagnosis. Open shifts contribute no hours, so the current "
                "day is incomplete. This block is ADVISORY and never affects "
                "the A12 reconciliation status."),
        },
        "channel_mapping": {
            "status": "ordering_pattern_verified_service_mode_unverified",
            "evaluated": advisory_evaluated,
            "not_evaluated_reason": (None if advisory_evaluated else
                "scope exceeds the advisory row limit, so channel mapping was not "
                "computed. ADVISORY only -- the A12 reconciliation status is "
                "unaffected and the FAIL-critical integrity checks still ran."),
            "source": "orders_v2.dining_option (+ web_order corroboration)",
            "group_meaning": ("web_associated / non_web_associated describes how "
                              "the order reached the POS. It is NOT a service "
                              "mode: it does not establish drive-thru, dine-in, "
                              "takeout or delivery."),
            "service_mode_mapping": "unavailable",
            "group_confidence": "verified_structural",
            "name_confidence": "project_convention_unverified",
            "historical_verified": True,
            "mapped_order_pct": _pct_of(deep["chan_mapped"], deep["chan_orders"]),
            "mapped_revenue_pct": _pct_of(deep["chan_mapped_rev"], deep["chan_rev"]),
            "unknown_order_count": (
                None if deep["chan_orders"] is None
                else int(deep["chan_orders"]) - int(deep["chan_mapped"])),
            "unknown_revenue": (
                None if deep["chan_rev"] is None
                else round(float(deep["chan_rev"]) - float(deep["chan_mapped_rev"]), 2)),
            "limitations": (
                "No Revel source names these codes -- every candidate endpoint "
                "404s and the Order schema declares dining_option as a bare "
                "integer. The on/off-premise GROUP is verified by web_order and "
                "payments.online agreeing independently; the NAMES are this "
                "project's own convention, which even defines codes 105/106 that "
                "have never occurred. Report the raw code. A numerical match to "
                "an earlier report does not verify the semantics."),
        },
        "misring_detection": {
            "status": "signal_only",
            "methodology": ("orders where the code's usual web association and "
                            "the web_order flag disagree on the record"),
            "finding_type": "metadata_inconsistency",
            "confidence": "suspected_only",
            "suspected_count": deep["chan_mismatch"],
            "suspected_pct": _pct_of(deep["chan_mismatch"], deep["chan_orders"]),
            "limitations": ("A single contradicting field is NOT proof of a "
                            "mis-ring. It is a record-keeping inconsistency, not "
                            "an established fact about how food was served. "
                            "Report it quantified, never as a confirmed "
                            "operational finding or an accusation."),
        },
        "channel_price_index": {
            "status": "unavailable",
            "methodology": "same product_id across channels, realised unit price",
            "comparability_coverage": (
                "insufficient: only 6 products were sold under both channel "
                "groups at Nederland in June 2026, with 1-2 sales each on the "
                "smaller side"),
            "limitations": (
                "The catalogue carries channel-specific duplicate product "
                "records (A5), so differently-numbered products must not be "
                "treated as the same item. price is a UNIT price and "
                "pure_sales = price x quantity excluding tax, but 8% of "
                "standalone lines diverge (discounts), so realised price needs "
                "care even within one channel."),
        },
        "category_mapping": {
            "status": "current_only",
            "evaluated": advisory_evaluated,
            "not_evaluated_reason": (None if advisory_evaluated else
                "scope exceeds the advisory row limit, so category coverage was not "
                "computed. ADVISORY only -- the A12 reconciliation status is "
                "unaffected and the FAIL-critical integrity checks still ran."),
            "source": "revel_product_api (Product.category)",
            "confidence": "verified_current",
            "historical_verified": False,
            "mapped_item_pct": _pct_of(deep["cat_mapped_items"], deep["cat_items"]),
            "mapped_revenue_pct": _pct_of(deep["cat_mapped_revenue"], deep["cat_revenue"]),
            "historically_verified_item_pct": _pct_of(deep["cat_hist_items"], deep["cat_items"]),
            "historically_verified_revenue_pct": _pct_of(
                deep["cat_hist_revenue"], deep["cat_revenue"]),
            "unmapped_item_count": (
                None if deep["cat_items"] is None
                else int(deep["cat_items"]) - int(deep["cat_mapped_items"])),
            "unmapped_revenue": (
                None if deep["cat_revenue"] is None
                else round(float(deep["cat_revenue"])
                           - float(deep["cat_mapped_revenue"]), 2)),
            "hierarchy_status": "parent_child available from product_categories",
            "coverage_thresholds_pct": {"state_normally": 95, "state_with_caveat": 80},
            "limitations": (
                "Category is verified for TODAY. The raw archive holds no Product "
                "snapshot older than 2026-09-02, so a past assignment is only "
                "verified where the product was not edited after the sale "
                "(historical_category_verified). Never derived from "
                "product_class, which is a different namespace."),
        },
        "store_cohort": _cohort_block,
        "time": {
            "source_timezone": "America/Chicago",
            "timestamp_semantics": (
                "Revel returns naive America/Chicago wall-clock; stored as UTC "
                "timestamptz. Verified against the live Revel API, 4/4 exact."),
            "transaction_timestamp": "orders_v2.created_date",
            "business_date_method": "local_calendar_date",
            "business_date_confidence": "limited",
            "business_date_note": (
                "No authoritative rollover rule exists: orders_v2 has no "
                "business-date column, and Revel's BusinessDay resource covers "
                "only 1 of 12 stores usefully. Post-midnight trade is 3.15% of "
                "REAL orders network-wide, so the choice matters for late-night "
                "stores -- disclose it rather than asserting a cutoff."),
            "weekday_convention": "ISO-8601 Monday=1..Sunday=7 (local_weekday_iso)",
            "weekday_convention_warning": (
                "features_*_v2.day_of_week is Monday=0..Sunday=6; "
                "PostgreSQL EXTRACT(dow) is Sunday=0. Three conventions."),
            "daypart_mapping_status": "unavailable",
            "relative_date_resolution": "application-side America/Chicago",
            "today_local": chicago_now().date().isoformat(),
            # Supplied so an answer never has to recall what day a scope
            # boundary fell on.
            "period_start_weekday_iso": date.fromisoformat(period_start).isoweekday(),
            "period_start_weekday_name": date.fromisoformat(period_start).strftime("%A"),
            "period_end_inclusive": (date.fromisoformat(period_end)
                                     - timedelta(days=1)).isoformat(),
            "period_end_inclusive_weekday_name": (
                (date.fromisoformat(period_end) - timedelta(days=1)).strftime("%A")),
        },
        # 0-100 scale, matching identity.identity_capture_rate and every other
        # rate in this payload. It was a 0-1 fraction while the A11 block used
        # 0-100, i.e. the same name meaning two different things -- exactly the
        # scale hazard that produced a hundredfold reporting error before.
        "identity_capture_rate": round(float(r["identity_capture_rate"] or 0) * 100.0, 2),
        "unavailable_metrics": UNAVAILABLE_METRIC_KEYS,
        "warnings": [],
    }
    return _reconcile(meta)


def _reconcile(meta: dict) -> dict:
    """Apply the gate. Sets reconciliation.status and appends warnings.

    FAIL stops analysis. WARN is reserved for conditions that are real but do
    not compromise the requested figures -- it never means "probably fine".
    """
    rec = meta["reconciliation"]
    vol, integ, fresh = meta["volumes"], meta["integrity"], meta["freshness"]
    fails: list[str] = []       # FAIL: analysis must not proceed
    warns: list[str] = []       # WARN: real, scope-specific, non-material
    notes: list[str] = []       # advisories: permanent dataset traits, not status

    if vol["order_rows"] == 0:
        fails.append("no orders exist for this store and period")

    d = rec["delta_pct"]
    if rec["reference_total"] == 0 and rec["computed_total"] > 0:
        fails.append("no payment records exist for this period, so the sales "
                     "total cannot be reconciled against any reference")
    elif d is not None and abs(d) > RECON_TOLERANCE_PCT:
        fails.append(
            f"sales reconciliation is off by {d:+.2f}% "
            f"(${rec['delta_dollars']:+,.2f}); tolerance is "
            f"±{RECON_TOLERANCE_PCT}%. Orders and payments disagree by more "
            "than rounding, which usually means a partial or interrupted sync")
    elif d is not None and abs(d) > RECON_WARN_PCT:
        warns.append(f"reconciliation delta {d:+.2f}% is within tolerance but "
                     "larger than typical; treat totals as approximate")

    if fresh["missing_business_days"] > 0:
        fails.append(
            f"{fresh['missing_business_days']} of "
            f"{fresh['expected_days_in_period']} business days have no orders "
            "at all; the period is incomplete")

    lag = fresh["source_lag_hours"]
    if lag is not None and lag > FRESHNESS_MAX_LAG_HOURS:
        fails.append(
            f"newest order in this store's data is {lag:.0f}h old, beyond the "
            f"{FRESHNESS_MAX_LAG_HOURS}h nightly-sync limit; a sync run was "
            "missed and recent activity is absent")

    if not integ.get("item_side_checks_evaluated", True):
        warns.append(
            "scope too large to cross-check line items; duplicate and orphan "
            "item scans were skipped. Order-level figures are still reconciled "
            "and join integrity is still measured -- narrow to one store or a "
            "shorter period for the full item-level check")

    if integ["duplicate_order_ids"]:
        fails.append(f"{integ['duplicate_order_ids']} duplicate order ids -- "
                     "every count and total would be inflated")
    if integ["duplicate_order_item_ids"]:
        fails.append(f"{integ['duplicate_order_item_ids']} duplicate order-item "
                     "ids -- item counts and product mix would be inflated")
    if integ["orphan_order_items"]:
        fails.append(f"{integ['orphan_order_items']} order-items reference "
                     "orders that are not present; item-level analysis would "
                     "silently under-count")

    ji = integ["order_to_item_join_integrity_pct"]
    if ji is not None and ji < 99.0:
        fails.append(f"only {ji:.2f}% of REAL orders have line items; "
                     "basket and product analysis is not safe")

    # The two below are properties of this dataset in every period, not defects
    # in this scope. They belong in the payload so the model reasons correctly,
    # but they must not move the gate -- if they did, every scope would be WARN
    # and the status would stop meaning anything.
    if meta["identity_capture_rate"] < 25.0:
        notes.append(
            f"identity captured on only "
            f"{meta['identity_capture_rate']:.1f}% of REAL transactions -- "
            "customer-level conclusions are selection-biased, not representative")

    # A3 entrée coverage. An advisory, not a gate condition: unreviewed products
    # make entree_count a floor, which changes how an entrée answer must be
    # worded, but it does not make the period's sales figures untrustworthy.
    ec = meta.get("entree_classification") or {}
    cov = ec.get("coverage_pct")
    if cov is not None and vol["real_count"]:
        if cov < ENTREE_COVERAGE_FLOOR_PCT:
            notes.append(
                f"entree classification covers only {cov:.1f}% of REAL orders "
                f"(floor {ENTREE_COVERAGE_FLOOR_PCT}%); "
                f"{ec['unresolved_line_items']} line items are unreviewed, so "
                "entree_count is a FLOOR and is understated by an unknown "
                "amount. Do not state entrees per check as fact for this scope "
                "-- give the floor, say what is unresolved, and stop there")
        elif cov < ENTREE_COVERAGE_MIN_PCT:
            notes.append(
                f"entree classification covers {cov:.1f}% of REAL orders "
                f"(target {ENTREE_COVERAGE_MIN_PCT}%); entree figures are a "
                "lower bound, say so when quoting them")

    empty_share = vol["empty_count"] / vol["order_rows"] if vol["order_rows"] else 0
    if empty_share > 0.10:
        notes.append(
            f"{empty_share * 100:.0f}% of raw order rows are EMPTY tickets "
            "(POS artifacts). Counts must use txn_class='REAL' or they overstate "
            "transactions")

    rec["status"] = "FAIL" if fails else ("WARN" if warns else "PASS")
    meta["blocking_reasons"] = fails
    meta["warnings"] = warns
    meta["advisories"] = notes
    meta["analysis_permitted"] = not fails
    return meta


# ── A12 hard enforcement ───────────────────────────────────────────────────
# The gate cannot depend on the model choosing to call check_data first: a
# model that skips it would analyse ungated. So every run_sql that touches
# business data must declare its scope, and the server reconciles that scope
# itself before the SQL reaches the database. check_data remains available for
# inspection but is no longer the trust boundary.

# Relations that carry no transactional history: store list, product catalogue,
# store cohort metadata, external weather. Reading these cannot produce a
# business conclusion about a period, so they need no reconciliation. This is a
# named list, never a category or a prefix rule -- a new fact table must be
# added deliberately, and defaults to gated.
_REFERENCE_RELATIONS = frozenset({
    "establishments", "products", "weather_daily", "v_store_cohort",
    # Current product -> category reference. Safe to leave ungated because it
    # reads only the mapping table and the product catalogue -- no order, item
    # or payment row -- so there is no period to reconcile. The HISTORICAL view
    # (v_order_items_category_context) joins order_items_v2 and stays gated.
    "v_product_category_current",
    # Whole-history aggregate behaviour per OPAQUE key: no per-order rows, no
    # dates to scope, no PII, no revenue. Gating it as business data was a
    # mistake -- the model was told to query it standalone, then rejected for
    # not declaring a period it has no column for. The per-order view
    # (v_order_identity_context) stays gated.
    "v_identity_profile",
})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _scope_facts(stmt) -> dict:
    """Date literals, establishment_id literals and relative-date use in a query."""
    dates: set[str] = set()
    ests: set[int] = set()
    relative = False

    def const_int(node):
        v = getattr(node, "val", None)
        return v.ival if isinstance(v, ast.Integer) else None

    def walk(n):
        nonlocal relative
        if isinstance(n, (list, tuple)):
            for x in n:
                walk(x)
            return
        if not isinstance(n, ast.Node):
            return
        if isinstance(n, ast.A_Const):
            v = getattr(n, "val", None)
            if isinstance(v, ast.String) and _DATE_RE.match(v.sval or ""):
                dates.add(v.sval)
        elif isinstance(n, ast.SQLValueFunction):
            relative = True          # current_date / current_timestamp
        elif isinstance(n, ast.FuncCall):
            if _fn_name(n) in ("now", "current_date", "current_timestamp", "localtimestamp"):
                relative = True
        elif isinstance(n, ast.A_Expr):
            lex = n.lexpr
            if (isinstance(lex, ast.ColumnRef) and lex.fields
                    and isinstance(lex.fields[-1], ast.String)
                    and lex.fields[-1].sval == "establishment_id"):
                for cand in (n.rexpr if isinstance(n.rexpr, (list, tuple)) else [n.rexpr]):
                    if isinstance(cand, ast.A_Const):
                        iv = const_int(cand)
                        if iv is not None:
                            ests.add(iv)
        for f in n:
            walk(getattr(n, f, None))

    walk(stmt)
    return {"dates": dates, "establishment_ids": ests, "relative_dates": relative}


class ScopeError(SqlError):
    """SQL rejected because its scope is unproven or its data failed the gate."""


def enforce_scope(sql: str, establishment_id, period_start, period_end,
                  cache: dict | None = None) -> dict | None:
    """Reconcile before execution. Returns the meta payload, or None if ungated.

    Raises ScopeError -- so nothing reaches the database -- when the query
    touches business data and either its scope cannot be proven to sit inside
    the declared scope, or that scope fails reconciliation.
    """
    stmt = _parse_one(sql.strip().rstrip(";").strip())
    relations = set()
    _walk(stmt, frozenset(), relations)
    business = relations - _REFERENCE_RELATIONS
    if not business:
        return None                      # reference-only lookup, no gate

    if not period_start or not period_end:
        raise ScopeError(
            "this query reads business data (" + ", ".join(sorted(business)) +
            ") so it must declare period_start and period_end_exclusive "
            "(YYYY-MM-DD), and establishment_id or null for all stores. The "
            "scope is reconciled before the query runs.")

    facts = _scope_facts(stmt)

    # Relative dates cannot be checked against the declared window, so a query
    # using them is not provably inside it.
    if facts["relative_dates"]:
        raise ScopeError(
            "business queries must use explicit date literals, not current_date "
            f"or now(). Rewrite the bounds as '{period_start}' and "
            f"'{period_end}' so the scope can be verified.")

    if not facts["dates"]:
        raise ScopeError(
            "business queries must carry explicit date bounds matching the "
            f"declared scope ('{period_start}' to '{period_end}'), otherwise "
            "the query could read outside the period that was reconciled.")

    outside = sorted(d for d in facts["dates"]
                     if not (period_start <= d <= period_end))
    if outside:
        raise ScopeError(
            f"query references date(s) {', '.join(outside)} outside the declared "
            f"scope {period_start}..{period_end}. Declare the scope you actually "
            "intend to analyse so it can be reconciled first.")

    if establishment_id is not None:
        wrong = sorted(e for e in facts["establishment_ids"] if e != establishment_id)
        if wrong:
            raise ScopeError(
                f"query filters establishment_id {wrong} but the declared scope "
                f"is store {establishment_id}. Declare the store you intend to "
                "analyse.")
        if not facts["establishment_ids"]:
            raise ScopeError(
                f"declared scope is store {establishment_id}, but the query has "
                "no establishment_id filter, so it would read every store. Add "
                "the filter, or declare establishment_id null for a network query.")

    key = (establishment_id, period_start, period_end)
    if cache is not None and key in cache:
        meta = cache[key]
    else:
        meta = meta_extract(establishment_id, period_start, period_end)
        if cache is not None:
            cache[key] = meta

    if not meta["analysis_permitted"]:
        raise ScopeError(
            "the data for this scope did not pass the reconciliation gate, so "
            "the query was not run: " + "; ".join(meta["blocking_reasons"]) +
            ". Report this to the user and stop -- do not analyse this period.")
    return meta


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
    scope_cache: dict = {}                  # one meta_extract per scope per turn
    last_meta: list[dict] = []              # first gated scope, for the answer

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
            tools=[CHECK_DATA_TOOL, RUN_SQL_TOOL],
            messages=messages,
        )

        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            break

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue

            if block.name == "check_data":
                step = {"sql": None, "row_count": None, "error": None,
                        "check": dict(block.input)}
                try:
                    ck = (block.input.get("establishment_id"),
                          block.input["period_start"], block.input["period_end"])
                    meta = scope_cache.get(ck) or meta_extract(*ck)
                    scope_cache[ck] = meta
                    step["meta"] = meta
                    step["status"] = meta["reconciliation"]["status"]
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": json.dumps(meta, default=str,
                                              separators=(",", ":")),
                    })
                except (psycopg2.Error, KeyError, ValueError) as e:
                    step["error"] = str(e).strip()
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": f"ERROR: {step['error']}", "is_error": True,
                    })
                steps.append(step)
                continue

            if block.name != "run_sql":
                continue

            sql = block.input.get("sql", "")
            step = {"sql": sql, "row_count": None, "error": None}
            try:
                # A12: the server reconciles the declared scope itself, before
                # the SQL reaches the database. This does not depend on the
                # model having called check_data.
                gate_meta = enforce_scope(
                    sql,
                    block.input.get("establishment_id"),
                    block.input.get("period_start"),
                    block.input.get("period_end_exclusive"),
                    cache=scope_cache,
                )
                if gate_meta is not None:
                    step["gate"] = gate_meta["reconciliation"]["status"]
                    if not last_meta:
                        last_meta.append(gate_meta)
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
            except ScopeError as e:
                # Per-scope, not turn-wide: a failed scope stays failed (the
                # result is cached), but a DIFFERENT scope is free to be
                # reconciled on its own merits. Pivoting from a broken period to
                # a sound one is legitimate; it cannot skip the gate, because
                # enforce_scope runs again for the new scope.
                step["error"] = str(e).strip()
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": f"REJECTED BEFORE EXECUTION: {step['error']}",
                    "is_error": True,
                })
                steps.append(step)
                continue
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
