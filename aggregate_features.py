"""
aggregate_features.py
=====================
Nightly job that reads yesterday's raw orders/order_items and populates
the three feature tables used by the ML prediction models:

  - features_hourly          (24 rows × 11 locations = 264 rows/day)
  - features_product_daily   (1 row per product × location × day)
  - features_daily_summary   (1 row per location per day)

Run after pipeline.py completes, e.g. at 3:00 AM:
    30 2 * * * python3 /opt/laynes/aggregate_features.py >> /var/log/laynes/features.log 2>&1

Usage:
    python aggregate_features.py               # aggregate yesterday
    python aggregate_features.py --date 2026-05-04   # specific date (backfill)

Environment variables (same as pipeline.py):
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS
"""

import os
import sys
import logging
import argparse
from datetime import date, timedelta

from dotenv import load_dotenv
load_dotenv()

import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def db_connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS"),
    )


# ─── features_hourly ─────────────────────────────────────────────────────────
HOURLY_SQL = """
INSERT INTO features_hourly (
    establishment_id, date, hour, day_of_week, week_of_year, month, is_weekend,
    order_count, item_count,
    total_revenue, avg_order_value, avg_items_per_order,
    orders_drive_through, orders_eat_in, orders_to_go,
    orders_doordash, orders_ubereats, orders_online,
    orders_lane_a, orders_lane_b,
    avg_kitchen_seconds, void_count, void_rate
)
SELECT
    o.establishment_id,
    DATE(o.created_date AT TIME ZONE 'America/Chicago')                          AS date,
    EXTRACT(HOUR FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT  AS hour,
    EXTRACT(ISODOW FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT - 1 AS day_of_week,
    EXTRACT(WEEK   FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT AS week_of_year,
    EXTRACT(MONTH  FROM o.created_date AT TIME ZONE 'America/Chicago')::SMALLINT AS month,
    EXTRACT(ISODOW FROM o.created_date AT TIME ZONE 'America/Chicago') IN (6,7) AS is_weekend,

    COUNT(DISTINCT o.id)                                AS order_count,
    COUNT(oi.id)                                        AS item_count,
    COALESCE(SUM(o.final_total), 0)                     AS total_revenue,
    COALESCE(AVG(o.final_total), 0)                     AS avg_order_value,
    CASE WHEN COUNT(DISTINCT o.id) > 0
         THEN COUNT(oi.id)::NUMERIC / COUNT(DISTINCT o.id)
         ELSE 0 END                                     AS avg_items_per_order,

    COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option = 4)    AS orders_drive_through,
    COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option = 1)    AS orders_eat_in,
    COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option = 0)    AS orders_to_go,
    COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option = 100)  AS orders_doordash,
    COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option = 101)  AS orders_ubereats,
    COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option IN (5,8)) AS orders_online,
    COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option = 105)  AS orders_lane_a,
    COUNT(DISTINCT o.id) FILTER (WHERE o.dining_option = 106)  AS orders_lane_b,

    AVG(oi.kitchen_seconds)                             AS avg_kitchen_seconds,
    COUNT(oi.id) FILTER (WHERE oi.is_voided = TRUE)     AS void_count,
    CASE WHEN COUNT(oi.id) > 0
         THEN COUNT(oi.id) FILTER (WHERE oi.is_voided = TRUE)::NUMERIC / COUNT(oi.id)
         ELSE 0 END                                     AS void_rate

FROM orders o
LEFT JOIN order_items oi
    ON oi.order_id = o.id
    AND oi.created_date >= %(day_start)s
    AND oi.created_date <  %(day_end)s
    AND oi.deleted = FALSE

WHERE
    o.closed      = TRUE
    AND o.deleted = FALSE
    AND o.created_date >= %(day_start)s
    AND o.created_date <  %(day_end)s

GROUP BY
    o.establishment_id,
    DATE(o.created_date AT TIME ZONE 'America/Chicago'),
    EXTRACT(HOUR FROM o.created_date AT TIME ZONE 'America/Chicago'),
    EXTRACT(ISODOW FROM o.created_date AT TIME ZONE 'America/Chicago'),
    EXTRACT(WEEK   FROM o.created_date AT TIME ZONE 'America/Chicago'),
    EXTRACT(MONTH  FROM o.created_date AT TIME ZONE 'America/Chicago')

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


# ─── features_product_daily ───────────────────────────────────────────────────
PRODUCT_DAILY_SQL = """
INSERT INTO features_product_daily (
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

    -- FILTER (not WHERE) throughout this query so voided rows stay in the
    -- group for void_count/void_rate below, while every actual-sales/volume
    -- metric excludes them. Order-level revenue (features_hourly.total_revenue,
    -- features_daily_summary.total_revenue) is untouched — Revel's
    -- order.final_total/subtotal already exclude voided items server-side
    -- (verified live 2026-08-10), only these item-level sums did not.
    SUM(oi.quantity) FILTER (WHERE oi.is_voided = FALSE)         AS quantity_sold,
    -- Orders containing a *sold* instance of this product — an order whose only
    -- instance of this product was voided should not count.
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
            FROM order_items sub
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

FROM order_items oi
JOIN orders o
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


# ─── features_daily_summary ───────────────────────────────────────────────────
DAILY_SUMMARY_SQL = """
INSERT INTO features_daily_summary (
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
    FROM orders o
    WHERE
        o.closed      = TRUE
        AND o.deleted = FALSE
        AND o.created_date >= %(day_start)s
        AND o.created_date <  %(day_end)s
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
    FROM order_items oi
    WHERE
        oi.deleted = FALSE
        AND oi.created_date >= %(day_start)s
        AND oi.created_date <  %(day_end)s
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
        -- Peak hour: the hour with the most orders
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


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Aggregate feature tables from raw data")
    parser.add_argument(
        "--date", type=str, default=None,
        help="Target date YYYY-MM-DD (default: yesterday)"
    )
    args = parser.parse_args()

    if not os.getenv("DB_PASS"):
        log.error("DB_PASS must be set")
        sys.exit(1)

    from datetime import datetime, timezone
    from datetime import date as date_type
    from zoneinfo import ZoneInfo

    if args.date:
        target_date = date_type.fromisoformat(args.date)
    else:
        target_date = date_type.today() - timedelta(days=1)

    # Boundaries in America/Chicago (the business day), converted to UTC for
    # the raw-table filter. created_date is stored as true UTC; a naive UTC
    # midnight-to-midnight window here would miss the last ~5-6 hours of the
    # Central business day and bleed into the neighboring day's row.
    _CT = ZoneInfo("America/Chicago")
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=_CT).isoformat()
    day_end   = datetime.combine(target_date + timedelta(days=1), datetime.min.time(), tzinfo=_CT).isoformat()

    params = {"day_start": day_start, "day_end": day_end}

    log.info("Aggregating features for %s", target_date)
    log.info("Window: %s → %s", day_start, day_end)

    conn = db_connect()

    try:
        with conn.cursor() as cur:
            log.info("Running features_hourly ...")
            cur.execute(HOURLY_SQL, params)
            log.info("  Done — %s rows affected", cur.rowcount if cur.rowcount >= 0 else "N/A")

            log.info("Running features_product_daily ...")
            cur.execute(PRODUCT_DAILY_SQL, params)
            log.info("  Done — %s rows affected", cur.rowcount if cur.rowcount >= 0 else "N/A")

            log.info("Running features_daily_summary ...")
            cur.execute(DAILY_SUMMARY_SQL, params)
            log.info("  Done — %s rows affected", cur.rowcount if cur.rowcount >= 0 else "N/A")

        conn.commit()
        log.info("Feature aggregation complete for %s", target_date)

    except Exception as exc:
        conn.rollback()
        log.error("Aggregation failed: %s", exc)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
