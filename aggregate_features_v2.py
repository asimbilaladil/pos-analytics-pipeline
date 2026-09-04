"""
aggregate_features_v2.py
=========================
Task 14 — authoritative historical feature recomputation, sourced from
orders_v2/order_items_v2 ONLY, writing to features_hourly_v2/
features_product_daily_v2/features_daily_summary_v2 ONLY.

This is a corrected-source recompute, not a metric-definition redesign: the
three SQL statements below are byte-for-byte aggregate_features.py's
HOURLY_SQL/PRODUCT_DAILY_SQL/DAILY_SUMMARY_SQL with FROM orders -> FROM
orders_v2, FROM/JOIN order_items -> FROM/JOIN order_items_v2, and the
INSERT INTO target table renamed to its _v2 shadow counterpart. No formula,
filter, GROUP BY, or FILTER clause is different. Never reads production
orders/order_items, payments(_v2), order_history(_v2), or modifier_items(_v2)
-- same as the live script, which never touched those either.

Never writes to features_hourly/features_product_daily/features_daily_summary
(the live tables any dashboard reads) -- INSERT target is always the _v2
table.

Unlike the live nightly script (one day, "yesterday", via cron), this loops
every day in an explicit date range against orders_v2/order_items_v2 --
matching the "1 establishment x 1 day" -> "increase only after testing"
philosophy used by every other Task 09-13 script, one persistent connection,
one commit per day, resumable in the sense that ON CONFLICT (establishment_id,
date[, hour|product_id]) DO UPDATE makes re-running any day idempotent.

Usage:
    python aggregate_features_v2.py --start 2026-02-09 --end 2026-08-11
"""

import os
import sys
import logging
import argparse
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

import pipeline as P

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")


# ─── features_hourly_v2 (source: orders_v2/order_items_v2) ────────────────────
# Task 15 remediation (Issue B): the original single-query version LEFT JOINed
# order_items directly under SUM(o.final_total)/AVG(o.final_total) -- since an
# order joins to N items, each order's final_total was summed N times (a
# classic join fan-out). Fixed by pre-aggregating order-level metrics
# (order_agg, no item join at all -- no fan-out possible) and item-level
# metrics (item_agg, order_items as the driving table) SEPARATELY, then
# combining at the already-collapsed (establishment, date, hour) grain.
# order_count/total_revenue/avg_order_value/dining-option breakdowns are
# untouched in meaning -- COUNT(DISTINCT o.id) was already fan-out-immune and
# is now just COUNT(*) over undoubled rows (equivalent). item_count/
# avg_kitchen_seconds/void_count/void_rate remain genuinely item-level,
# computed from order_items exactly as before. No other formula changed.
HOURLY_SQL = """
INSERT INTO features_hourly_v2 (
    establishment_id, date, hour, day_of_week, week_of_year, month, is_weekend,
    order_count, item_count,
    total_revenue, avg_order_value, avg_items_per_order,
    orders_drive_through, orders_eat_in, orders_to_go,
    orders_doordash, orders_ubereats, orders_online,
    orders_lane_a, orders_lane_b,
    avg_kitchen_seconds, void_count, void_rate
)
WITH order_agg AS (
    SELECT
        o.establishment_id,
        DATE(o.created_date AT TIME ZONE 'America/Chicago')                          AS date,
        EXTRACT(HOUR FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT  AS hour,
        EXTRACT(ISODOW FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT - 1 AS day_of_week,
        EXTRACT(WEEK   FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT AS week_of_year,
        EXTRACT(MONTH  FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT AS month,
        EXTRACT(ISODOW FROM o.created_date AT TIME ZONE 'America/Chicago') IN (6,7) AS is_weekend,

        COUNT(*)                                            AS order_count,
        COALESCE(SUM(o.final_total), 0)                     AS total_revenue,
        COALESCE(AVG(o.final_total), 0)                     AS avg_order_value,

        COUNT(*) FILTER (WHERE o.dining_option = 4)    AS orders_drive_through,
        COUNT(*) FILTER (WHERE o.dining_option = 1)    AS orders_eat_in,
        COUNT(*) FILTER (WHERE o.dining_option = 0)    AS orders_to_go,
        COUNT(*) FILTER (WHERE o.dining_option = 100)  AS orders_doordash,
        COUNT(*) FILTER (WHERE o.dining_option = 101)  AS orders_ubereats,
        COUNT(*) FILTER (WHERE o.dining_option IN (5,8)) AS orders_online,
        COUNT(*) FILTER (WHERE o.dining_option = 105)  AS orders_lane_a,
        COUNT(*) FILTER (WHERE o.dining_option = 106)  AS orders_lane_b

    FROM orders_v2 o
    WHERE
        o.closed      = TRUE
        AND o.deleted = FALSE
        AND o.created_date >= %(day_start)s
        AND o.created_date <  %(day_end)s
        AND (%(est_id)s::int IS NULL OR o.establishment_id = %(est_id)s)
    GROUP BY
        o.establishment_id,
        DATE(o.created_date AT TIME ZONE 'America/Chicago'),
        EXTRACT(HOUR FROM o.created_date AT TIME ZONE 'America/Chicago'),
        EXTRACT(ISODOW FROM o.created_date AT TIME ZONE 'America/Chicago'),
        EXTRACT(WEEK   FROM o.created_date AT TIME ZONE 'America/Chicago'),
        EXTRACT(MONTH  FROM o.created_date AT TIME ZONE 'America/Chicago')
),
item_agg AS (
    SELECT
        o.establishment_id,
        DATE(o.created_date AT TIME ZONE 'America/Chicago')                          AS date,
        EXTRACT(HOUR FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT  AS hour,

        COUNT(oi.id)                                        AS item_count,
        AVG(oi.kitchen_seconds)                             AS avg_kitchen_seconds,
        COUNT(oi.id) FILTER (WHERE oi.is_voided = TRUE)     AS void_count

    FROM orders_v2 o
    JOIN order_items_v2 oi
        ON oi.order_id = o.id
        AND oi.created_date >= %(day_start)s
        AND oi.created_date <  %(day_end)s
        AND oi.deleted = FALSE
    WHERE
        o.closed      = TRUE
        AND o.deleted = FALSE
        AND o.created_date >= %(day_start)s
        AND o.created_date <  %(day_end)s
        AND (%(est_id)s::int IS NULL OR o.establishment_id = %(est_id)s)
    GROUP BY
        o.establishment_id,
        DATE(o.created_date AT TIME ZONE 'America/Chicago'),
        EXTRACT(HOUR FROM o.created_date AT TIME ZONE 'America/Chicago')
)
SELECT
    oa.establishment_id, oa.date, oa.hour, oa.day_of_week, oa.week_of_year, oa.month, oa.is_weekend,
    oa.order_count, COALESCE(ia.item_count, 0) AS item_count,
    oa.total_revenue, oa.avg_order_value,
    CASE WHEN oa.order_count > 0
         THEN COALESCE(ia.item_count, 0)::NUMERIC / oa.order_count
         ELSE 0 END                                     AS avg_items_per_order,
    oa.orders_drive_through, oa.orders_eat_in, oa.orders_to_go,
    oa.orders_doordash, oa.orders_ubereats, oa.orders_online,
    oa.orders_lane_a, oa.orders_lane_b,
    ia.avg_kitchen_seconds,
    COALESCE(ia.void_count, 0) AS void_count,
    CASE WHEN COALESCE(ia.item_count, 0) > 0
         THEN COALESCE(ia.void_count, 0)::NUMERIC / ia.item_count
         ELSE 0 END                                     AS void_rate
FROM order_agg oa
LEFT JOIN item_agg ia
    ON ia.establishment_id = oa.establishment_id
    AND ia.date = oa.date
    AND ia.hour = oa.hour

ON CONFLICT (establishment_id, date, hour) DO UPDATE SET
    order_count          = EXCLUDED.order_count,
    item_count           = EXCLUDED.item_count,
    total_revenue        = EXCLUDED.total_revenue,
    avg_order_value      = EXCLUDED.avg_order_value,
    avg_items_per_order  = EXCLUDED.avg_items_per_order,
    orders_drive_through = EXCLUDED.orders_drive_through,
    orders_eat_in        = EXCLUDED.orders_eat_in,
    orders_to_go         = EXCLUDED.orders_to_go,
    orders_doordash      = EXCLUDED.orders_doordash,
    orders_ubereats      = EXCLUDED.orders_ubereats,
    orders_online        = EXCLUDED.orders_online,
    orders_lane_a        = EXCLUDED.orders_lane_a,
    orders_lane_b        = EXCLUDED.orders_lane_b,
    avg_kitchen_seconds  = EXCLUDED.avg_kitchen_seconds,
    void_count           = EXCLUDED.void_count,
    void_rate            = EXCLUDED.void_rate,
    computed_at          = NOW();
"""


# ─── features_product_daily_v2 (source: orders_v2/order_items_v2) ─────────────
PRODUCT_DAILY_SQL = """
INSERT INTO features_product_daily_v2 (
    establishment_id, product_id, product_name, date,
    day_of_week, week_of_year, month, is_weekend,
    quantity_sold, order_count, revenue,
    qty_drive_through, qty_eat_in, qty_third_party,
    avg_kitchen_seconds, max_kitchen_seconds, min_kitchen_seconds, kitchen_outliers,
    void_count, void_rate
)
SELECT
    oi.establishment_id,
    oi.product_id,
    MAX(oi.product_name)                                        AS product_name,
    DATE(oi.created_date AT TIME ZONE 'America/Chicago')             AS date,
    EXTRACT(ISODOW FROM oi.created_date AT TIME ZONE 'America/Chicago')::SMALLINT - 1 AS day_of_week,
    EXTRACT(WEEK   FROM oi.created_date AT TIME ZONE 'America/Chicago')::SMALLINT     AS week_of_year,
    EXTRACT(MONTH  FROM oi.created_date AT TIME ZONE 'America/Chicago')::SMALLINT     AS month,
    EXTRACT(ISODOW FROM oi.created_date AT TIME ZONE 'America/Chicago') IN (6,7)      AS is_weekend,

    SUM(oi.quantity) FILTER (WHERE oi.is_voided = FALSE)         AS quantity_sold,
    COUNT(DISTINCT oi.order_id) FILTER (WHERE oi.is_voided = FALSE) AS order_count,
    COALESCE(SUM(oi.pure_sales) FILTER (WHERE oi.is_voided = FALSE), 0) AS revenue,

    SUM(oi.quantity) FILTER (WHERE o.dining_option = 4 AND oi.is_voided = FALSE)         AS qty_drive_through,
    SUM(oi.quantity) FILTER (WHERE o.dining_option IN (0,1) AND oi.is_voided = FALSE)    AS qty_eat_in,
    SUM(oi.quantity) FILTER (WHERE o.dining_option IN (100,101) AND oi.is_voided = FALSE) AS qty_third_party,

    AVG(oi.kitchen_seconds)                                     AS avg_kitchen_seconds,
    MAX(oi.kitchen_seconds)                                     AS max_kitchen_seconds,
    MIN(oi.kitchen_seconds)                                     AS min_kitchen_seconds,
    COUNT(*) FILTER (
        WHERE oi.kitchen_seconds IS NOT NULL
        AND oi.kitchen_seconds > (
            SELECT AVG(kitchen_seconds) * 3
            FROM order_items_v2 sub
            WHERE sub.product_id = oi.product_id
            AND sub.establishment_id = oi.establishment_id
            AND sub.created_date >= %(day_start)s
            AND sub.created_date <  %(day_end)s
            AND sub.kitchen_seconds IS NOT NULL
        )
    )                                                           AS kitchen_outliers,

    COUNT(*) FILTER (WHERE oi.is_voided = TRUE)                 AS void_count,
    CASE WHEN COUNT(*) > 0
         THEN COUNT(*) FILTER (WHERE oi.is_voided = TRUE)::NUMERIC / COUNT(*)
         ELSE 0 END                                             AS void_rate

FROM order_items_v2 oi
JOIN orders_v2 o
    ON o.id = oi.order_id
    AND o.closed  = TRUE
    AND o.deleted = FALSE
    AND o.created_date >= %(day_start)s
    AND o.created_date <  %(day_end)s

WHERE
    oi.deleted   = FALSE
    AND oi.product_id IS NOT NULL
    AND oi.created_date >= %(day_start)s
    AND oi.created_date <  %(day_end)s
    AND (%(est_id)s::int IS NULL OR oi.establishment_id = %(est_id)s)

GROUP BY
    oi.establishment_id,
    oi.product_id,
    DATE(oi.created_date AT TIME ZONE 'America/Chicago'),
    EXTRACT(ISODOW FROM oi.created_date AT TIME ZONE 'America/Chicago'),
    EXTRACT(WEEK   FROM oi.created_date AT TIME ZONE 'America/Chicago'),
    EXTRACT(MONTH  FROM oi.created_date AT TIME ZONE 'America/Chicago')

ON CONFLICT (establishment_id, product_id, date) DO UPDATE SET
    product_name        = EXCLUDED.product_name,
    quantity_sold       = EXCLUDED.quantity_sold,
    order_count         = EXCLUDED.order_count,
    revenue             = EXCLUDED.revenue,
    qty_drive_through   = EXCLUDED.qty_drive_through,
    qty_eat_in          = EXCLUDED.qty_eat_in,
    qty_third_party     = EXCLUDED.qty_third_party,
    avg_kitchen_seconds = EXCLUDED.avg_kitchen_seconds,
    max_kitchen_seconds = EXCLUDED.max_kitchen_seconds,
    min_kitchen_seconds = EXCLUDED.min_kitchen_seconds,
    kitchen_outliers    = EXCLUDED.kitchen_outliers,
    void_count          = EXCLUDED.void_count,
    void_rate           = EXCLUDED.void_rate,
    computed_at         = NOW();
"""


# ─── features_daily_summary_v2 (source: orders_v2/order_items_v2) ─────────────
DAILY_SUMMARY_SQL = """
INSERT INTO features_daily_summary_v2 (
    establishment_id, date, day_of_week, week_of_year, month, is_weekend,
    total_orders, total_items, total_revenue, avg_order_value, avg_items_per_order,
    pct_drive_through, pct_third_party, pct_in_store,
    revenue_drive_through, revenue_third_party, revenue_in_store,
    avg_kitchen_seconds, pct_orders_over_10min,
    total_voids, void_rate, total_discounts, discount_rate,
    peak_hour, peak_hour_orders
)
WITH order_stats AS (
    SELECT
        o.establishment_id,
        DATE(o.created_date AT TIME ZONE 'America/Chicago')             AS date,
        EXTRACT(ISODOW FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT - 1 AS day_of_week,
        EXTRACT(WEEK   FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT     AS week_of_year,
        EXTRACT(MONTH  FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT     AS month,
        EXTRACT(ISODOW FROM o.created_date AT TIME ZONE 'America/Chicago') IN (6,7)      AS is_weekend,
        COUNT(DISTINCT o.id)                                        AS total_orders,
        COALESCE(SUM(o.final_total), 0)                             AS total_revenue,
        AVG(o.final_total)                                          AS avg_order_value,
        SUM(CASE WHEN o.discount_total_amount > 0 THEN o.discount_total_amount ELSE 0 END) AS total_discounts,
        COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option = 4)     AS cnt_drive_through,
        COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option IN (100,101)) AS cnt_third_party,
        COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option IN (0,1,2,3)) AS cnt_in_store,
        SUM(o.final_total) FILTER (WHERE o.dining_option = 4)       AS rev_drive_through,
        SUM(o.final_total) FILTER (WHERE o.dining_option IN (100,101)) AS rev_third_party,
        SUM(o.final_total) FILTER (WHERE o.dining_option IN (0,1,2,3)) AS rev_in_store,
        EXTRACT(HOUR FROM o.created_date AT TIME ZONE 'America/Chicago') AS hour,
        COUNT(DISTINCT o.id)                                        AS hour_order_count
    FROM orders_v2 o
    WHERE
        o.closed      = TRUE
        AND o.deleted = FALSE
        AND o.created_date >= %(day_start)s
        AND o.created_date <  %(day_end)s
        AND (%(est_id)s::int IS NULL OR o.establishment_id = %(est_id)s)
    GROUP BY
        o.establishment_id,
        DATE(o.created_date AT TIME ZONE 'America/Chicago'),
        EXTRACT(ISODOW FROM o.created_date AT TIME ZONE 'America/Chicago'),
        EXTRACT(WEEK   FROM o.created_date AT TIME ZONE 'America/Chicago'),
        EXTRACT(MONTH  FROM o.created_date AT TIME ZONE 'America/Chicago'),
        EXTRACT(HOUR   FROM o.created_date AT TIME ZONE 'America/Chicago')
),
item_stats AS (
    SELECT
        oi.establishment_id,
        DATE(oi.created_date AT TIME ZONE 'America/Chicago')    AS date,
        COUNT(oi.id)                                        AS total_items,
        AVG(oi.kitchen_seconds)                             AS avg_kitchen_seconds,
        COUNT(DISTINCT oi.order_id) FILTER (
            WHERE oi.kitchen_seconds > 600
        )                                                   AS orders_over_10min,
        COUNT(oi.id) FILTER (WHERE oi.is_voided = TRUE)     AS total_voids
    FROM order_items_v2 oi
    WHERE
        oi.deleted = FALSE
        AND oi.created_date >= %(day_start)s
        AND oi.created_date <  %(day_end)s
        AND (%(est_id)s::int IS NULL OR oi.establishment_id = %(est_id)s)
    GROUP BY oi.establishment_id, DATE(oi.created_date AT TIME ZONE 'America/Chicago')
),
agg AS (
    SELECT
        os.establishment_id,
        os.date,
        MAX(os.day_of_week)     AS day_of_week,
        MAX(os.week_of_year)    AS week_of_year,
        MAX(os.month)           AS month,
        BOOL_OR(os.is_weekend)  AS is_weekend,
        SUM(os.total_orders)    AS total_orders,
        SUM(os.total_revenue)   AS total_revenue,
        AVG(os.avg_order_value) AS avg_order_value,
        SUM(os.total_discounts) AS total_discounts,
        SUM(os.cnt_drive_through) AS cnt_drive_through,
        SUM(os.cnt_third_party)   AS cnt_third_party,
        SUM(os.cnt_in_store)      AS cnt_in_store,
        SUM(os.rev_drive_through) AS rev_drive_through,
        SUM(os.rev_third_party)   AS rev_third_party,
        SUM(os.rev_in_store)      AS rev_in_store,
        (ARRAY_AGG(os.hour ORDER BY os.hour_order_count DESC))[1] AS peak_hour,
        MAX(os.hour_order_count)  AS peak_hour_orders
    FROM order_stats os
    GROUP BY os.establishment_id, os.date
)
SELECT
    a.establishment_id,
    a.date,
    a.day_of_week,
    a.week_of_year,
    a.month,
    a.is_weekend,
    a.total_orders,
    COALESCE(i.total_items, 0)                          AS total_items,
    a.total_revenue,
    COALESCE(a.avg_order_value, 0)                      AS avg_order_value,
    CASE WHEN a.total_orders > 0
         THEN COALESCE(i.total_items, 0)::NUMERIC / a.total_orders
         ELSE 0 END                                     AS avg_items_per_order,
    CASE WHEN a.total_orders > 0
         THEN a.cnt_drive_through::NUMERIC / a.total_orders ELSE 0 END AS pct_drive_through,
    CASE WHEN a.total_orders > 0
         THEN a.cnt_third_party::NUMERIC  / a.total_orders ELSE 0 END AS pct_third_party,
    CASE WHEN a.total_orders > 0
         THEN a.cnt_in_store::NUMERIC     / a.total_orders ELSE 0 END AS pct_in_store,
    COALESCE(a.rev_drive_through, 0)                    AS revenue_drive_through,
    COALESCE(a.rev_third_party, 0)                      AS revenue_third_party,
    COALESCE(a.rev_in_store, 0)                         AS revenue_in_store,
    i.avg_kitchen_seconds,
    CASE WHEN a.total_orders > 0
         THEN COALESCE(i.orders_over_10min, 0)::NUMERIC / a.total_orders
         ELSE 0 END                                     AS pct_orders_over_10min,
    COALESCE(i.total_voids, 0)                          AS total_voids,
    CASE WHEN COALESCE(i.total_items, 0) > 0
         THEN COALESCE(i.total_voids, 0)::NUMERIC / i.total_items
         ELSE 0 END                                     AS void_rate,
    COALESCE(a.total_discounts, 0)                      AS total_discounts,
    CASE WHEN a.total_revenue > 0
         THEN COALESCE(a.total_discounts, 0) / a.total_revenue
         ELSE 0 END                                     AS discount_rate,
    a.peak_hour::SMALLINT,
    a.peak_hour_orders
FROM agg a
LEFT JOIN item_stats i ON i.establishment_id = a.establishment_id AND i.date = a.date

ON CONFLICT (establishment_id, date) DO UPDATE SET
    total_orders        = EXCLUDED.total_orders,
    total_items         = EXCLUDED.total_items,
    total_revenue       = EXCLUDED.total_revenue,
    avg_order_value     = EXCLUDED.avg_order_value,
    avg_items_per_order = EXCLUDED.avg_items_per_order,
    pct_drive_through   = EXCLUDED.pct_drive_through,
    pct_third_party     = EXCLUDED.pct_third_party,
    pct_in_store        = EXCLUDED.pct_in_store,
    revenue_drive_through = EXCLUDED.revenue_drive_through,
    revenue_third_party = EXCLUDED.revenue_third_party,
    revenue_in_store    = EXCLUDED.revenue_in_store,
    avg_kitchen_seconds = EXCLUDED.avg_kitchen_seconds,
    pct_orders_over_10min = EXCLUDED.pct_orders_over_10min,
    total_voids         = EXCLUDED.total_voids,
    void_rate           = EXCLUDED.void_rate,
    total_discounts     = EXCLUDED.total_discounts,
    discount_rate       = EXCLUDED.discount_rate,
    peak_hour           = EXCLUDED.peak_hour,
    peak_hour_orders    = EXCLUDED.peak_hour_orders,
    computed_at         = NOW();
"""


def aggregate_day(conn, target_date: date, sections=("hourly", "product_daily", "daily_summary"),
                   establishment_id: int = None) -> dict:
    """sections: which of the three SQL statements to run for this date.
    Defaults to all three (unchanged behavior for any full recompute). Task 15
    remediation uses sections=("hourly",) to regenerate ONLY features_hourly_v2
    without touching features_product_daily_v2/features_daily_summary_v2,
    whose formulas were verified unaffected by the join fan-out fix.

    establishment_id (Task 16 prerequisite): None (default) recomputes every
    establishment for this date, byte-for-byte the pre-existing behavior --
    every existing caller (the full-range recompute, the Task 15 hourly-only
    regeneration) leaves this unset. A specific id scopes to just that
    establishment via `AND (%(est_id)s::int IS NULL OR ... = %(est_id)s)` in
    each query -- used by reprocess_dirty_features_v2.py so a single dirty
    (establishment, date) never triggers a network-wide day recompute."""
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=_CT).isoformat()
    day_end   = datetime.combine(target_date + timedelta(days=1), datetime.min.time(), tzinfo=_CT).isoformat()
    params = {"day_start": day_start, "day_end": day_end, "est_id": establishment_id}

    stats = {"hourly": 0, "product_daily": 0, "daily_summary": 0}
    with conn.cursor() as cur:
        if "hourly" in sections:
            cur.execute(HOURLY_SQL, params)
            stats["hourly"] = cur.rowcount
        if "product_daily" in sections:
            cur.execute(PRODUCT_DAILY_SQL, params)
            stats["product_daily"] = cur.rowcount
        if "daily_summary" in sections:
            cur.execute(DAILY_SUMMARY_SQL, params)
            stats["daily_summary"] = cur.rowcount
    conn.commit()
    return stats


def main():
    ap = argparse.ArgumentParser(description="Task 14: recompute feature tables from orders_v2/order_items_v2")
    ap.add_argument("--start", type=str, required=True, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--end", type=str, required=True, help="YYYY-MM-DD, inclusive")
    ap.add_argument("--only", type=str, default=None,
                     help="comma-separated subset of hourly,product_daily,daily_summary; default all three")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    sections = tuple(args.only.split(",")) if args.only else ("hourly", "product_daily", "daily_summary")

    conn = P.db_connect()
    n_days = (end - start).days + 1
    log.info("=== Task 14 recompute (orders_v2/order_items_v2 -> *_v2 feature tables) — %s -> %s (%d days) — sections=%s ===",
              start, end, n_days, sections)

    totals = {"hourly": 0, "product_daily": 0, "daily_summary": 0}
    d = start
    n = 0
    try:
        while d <= end:
            n += 1
            stats = aggregate_day(conn, d, sections=sections)
            for k in totals:
                totals[k] += stats[k]
            if n % 20 == 0 or d == end:
                log.info("  [%d/%d] %s done — hourly=%d product_daily=%d daily_summary=%d (cumulative: %s)",
                          n, n_days, d, stats["hourly"], stats["product_daily"], stats["daily_summary"], totals)
            d += timedelta(days=1)
    except Exception as exc:
        conn.rollback()
        log.error("Aggregation failed at date=%s: %s", d, exc)
        import traceback
        traceback.print_exc()
        conn.close()
        sys.exit(1)

    conn.close()
    log.info("=== Task 14 recompute complete — %d days, totals=%s ===", n, totals)


if __name__ == "__main__":
    main()
