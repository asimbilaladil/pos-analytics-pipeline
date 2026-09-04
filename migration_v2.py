"""
migration_v2.py
================
Task 07.3 execution — create and populate SHADOW tables orders_v2 /
order_items_v2 with a canonical, deduplicated (id)-only identity, then run
every reconciliation/referential/regression check BEFORE committing.

Does NOT touch orders / order_items / any other existing table. Does NOT
rename or cut over anything. Everything — DDL, population, indexes, FK,
and every validation query below, including the synthetic regression test
— runs inside ONE transaction and only commits if every check passes;
any failed assertion rolls the whole thing back, leaving orders_v2/
order_items_v2 not existing at all.

Background (Task 07.2/07.3): Revel's Order.created_date can be revised
while an order stays open. Since orders/order_items UPSERT on
(id, created_date), a later fetch with a revised created_date creates a
NEW row instead of updating the existing one — 51 orders and 15 items are
currently duplicated this way (all confirmed same uuid as their sibling
row, i.e. genuinely the same Revel record, not id reuse). orders_v2/
order_items_v2 use PRIMARY KEY (id) alone — created_date/updated_date
remain ordinary data columns, not identity.
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(".env")

import pipeline as P

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ORDERS_V2_DDL = """
CREATE TABLE orders_v2 (
    id                      BIGINT PRIMARY KEY,
    uuid                    UUID,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    local_id                VARCHAR,
    created_date            TIMESTAMPTZ NOT NULL,
    updated_date            TIMESTAMPTZ,
    pickup_time             TIMESTAMPTZ,
    dining_option           SMALLINT REFERENCES dining_channels(id),
    pos_mode                VARCHAR,
    final_total             NUMERIC DEFAULT 0,
    subtotal                NUMERIC DEFAULT 0,
    tax                     NUMERIC DEFAULT 0,
    gratuity                NUMERIC DEFAULT 0,
    discount_total_amount   NUMERIC DEFAULT 0,
    closed                  BOOLEAN DEFAULT FALSE,
    is_unpaid                BOOLEAN DEFAULT FALSE,
    deleted                 BOOLEAN DEFAULT FALSE,
    is_discounted            BOOLEAN DEFAULT FALSE,
    web_order               BOOLEAN DEFAULT FALSE,
    customer_id              INTEGER,
    number_of_people         SMALLINT DEFAULT 0,
    ingested_at              TIMESTAMPTZ DEFAULT NOW(),
    ingestion_date            DATE NOT NULL,
    created_by_user_id        INTEGER,
    updated_by_user_id        INTEGER,
    discount_amount           NUMERIC,
    discount_reason           TEXT,
    discounted_by_user_id     INTEGER,
    exchanged                 BOOLEAN,
    service_charge             NUMERIC,
    surcharge                 NUMERIC,
    remaining_due              NUMERIC,
    notes                      TEXT
)
"""

ORDER_ITEMS_V2_DDL = """
CREATE TABLE order_items_v2 (
    id                      BIGINT PRIMARY KEY,
    uuid                    UUID,
    order_id                BIGINT NOT NULL,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    product_id               INTEGER REFERENCES products(id),
    product_name             VARCHAR,
    quantity                  NUMERIC DEFAULT 1,
    dining_option              SMALLINT REFERENCES dining_channels(id),
    combo_product_id           INTEGER REFERENCES products(id),
    combo_uuid                 UUID,
    price                       NUMERIC DEFAULT 0,
    pure_sales                  NUMERIC DEFAULT 0,
    tax_amount                  NUMERIC DEFAULT 0,
    modifier_amount              NUMERIC DEFAULT 0,
    is_discounted                 BOOLEAN DEFAULT FALSE,
    created_date                  TIMESTAMPTZ NOT NULL,
    start_time                     TIMESTAMPTZ,
    kitchen_completed               TIMESTAMPTZ,
    kitchen_seconds INTEGER GENERATED ALWAYS AS (
        CASE WHEN kitchen_completed IS NOT NULL AND start_time IS NOT NULL
             THEN EXTRACT(epoch FROM (kitchen_completed - start_time))::integer
             ELSE NULL END
    ) STORED,
    is_voided                        BOOLEAN DEFAULT FALSE,
    voided_date                       TIMESTAMPTZ,
    voided_by_user_id                  INTEGER,
    voided_reason                       VARCHAR,
    deleted                              BOOLEAN DEFAULT FALSE,
    deleted_date                          TIMESTAMPTZ,
    ingested_at                            TIMESTAMPTZ DEFAULT NOW(),
    ingestion_date                          DATE NOT NULL,
    ervc_type                                SMALLINT,
    item_type                                 SMALLINT,
    initial_price                              NUMERIC,
    discount_amount                             NUMERIC,
    discount_reason                              TEXT,
    discounted_by_user_id                         INTEGER,
    cost                                            NUMERIC,
    exchanged                                        BOOLEAN,
    void_ref_uuid                                     UUID,
    updated_date                                       TIMESTAMPTZ
)
"""

ORDERS_COLS = [
    "id", "uuid", "establishment_id", "local_id", "created_date", "updated_date",
    "pickup_time", "dining_option", "pos_mode", "final_total", "subtotal", "tax",
    "gratuity", "discount_total_amount", "closed", "is_unpaid", "deleted",
    "is_discounted", "web_order", "customer_id", "number_of_people", "ingested_at",
    "ingestion_date", "created_by_user_id", "updated_by_user_id", "discount_amount",
    "discount_reason", "discounted_by_user_id", "exchanged", "service_charge",
    "surcharge", "remaining_due", "notes",
]

# order_items columns EXCLUDING kitchen_seconds (GENERATED ALWAYS columns
# cannot be targeted by an INSERT column list)
ITEM_COLS_V2 = [
    "id", "uuid", "order_id", "establishment_id", "product_id", "product_name",
    "quantity", "dining_option", "combo_product_id", "combo_uuid", "price",
    "pure_sales", "tax_amount", "modifier_amount", "is_discounted", "created_date",
    "start_time", "kitchen_completed", "is_voided", "voided_date",
    "voided_by_user_id", "voided_reason", "deleted", "deleted_date", "ingested_at",
    "ingestion_date", "ervc_type", "item_type", "initial_price", "discount_amount",
    "discount_reason", "discounted_by_user_id", "cost", "exchanged", "void_ref_uuid",
    "updated_date",
]

DEDUP_ORDER_BY = "id, updated_date DESC NULLS LAST, created_date DESC, ingested_at DESC"

# The two known "both closed, both $0-discrepancy-free but created_date
# shifted" duplicate orders from Task 07.2 — used for the affected-query
# double-count regression check.
EXCEPTION_ORDER_IDS = [16003344, 16028751]


def create_and_populate(cur, report):
    log.info("DROP IF EXISTS (idempotent for reruns of THIS shadow migration only)")
    cur.execute("DROP TABLE IF EXISTS order_items_v2 CASCADE")
    cur.execute("DROP TABLE IF EXISTS orders_v2 CASCADE")

    log.info("CREATE orders_v2 / order_items_v2")
    cur.execute(ORDERS_V2_DDL)
    cur.execute(ORDER_ITEMS_V2_DDL)

    log.info("Populate orders_v2 (DISTINCT ON dedup)")
    t0 = time.time()
    cur.execute(f"""
        INSERT INTO orders_v2 ({', '.join(ORDERS_COLS)})
        SELECT DISTINCT ON (id) {', '.join(ORDERS_COLS)}
        FROM orders
        ORDER BY {DEDUP_ORDER_BY}
    """)
    report["orders_v2_populate_seconds"] = round(time.time() - t0, 2)

    log.info("Populate order_items_v2 (DISTINCT ON dedup)")
    t1 = time.time()
    cur.execute(f"""
        INSERT INTO order_items_v2 ({', '.join(ITEM_COLS_V2)})
        SELECT DISTINCT ON (id) {', '.join(ITEM_COLS_V2)}
        FROM order_items
        ORDER BY {DEDUP_ORDER_BY}
    """)
    report["order_items_v2_populate_seconds"] = round(time.time() - t1, 2)

    log.info("Indexes")
    cur.execute("CREATE INDEX ix_orders_v2_est_created ON orders_v2(establishment_id, created_date)")
    cur.execute("CREATE INDEX ix_orders_v2_est_updated ON orders_v2(establishment_id, updated_date)")
    cur.execute("CREATE INDEX ix_orders_v2_created ON orders_v2(created_date)")
    cur.execute("CREATE INDEX ix_orders_v2_updated ON orders_v2(updated_date)")
    cur.execute("CREATE INDEX ix_orders_v2_closed ON orders_v2(closed)")
    cur.execute("CREATE INDEX ix_orders_v2_customer ON orders_v2(customer_id)")
    cur.execute("CREATE INDEX ix_orders_v2_dining_option ON orders_v2(dining_option)")
    cur.execute("CREATE INDEX ix_oiv2_order_id ON order_items_v2(order_id)")
    cur.execute("CREATE INDEX ix_oiv2_est_created ON order_items_v2(establishment_id, created_date)")
    cur.execute("CREATE INDEX ix_oiv2_product_created ON order_items_v2(product_id, created_date)")
    cur.execute("CREATE INDEX ix_oiv2_voided ON order_items_v2(is_voided)")
    cur.execute("CREATE INDEX ix_oiv2_combo ON order_items_v2(combo_product_id)")
    cur.execute("CREATE INDEX ix_oiv2_kitchen_speed ON order_items_v2(kitchen_seconds)")

    log.info("Evaluate order_items_v2.order_id -> orders_v2.id FK")
    cur.execute("""
        SELECT COUNT(DISTINCT oi.order_id) FROM order_items_v2 oi
        LEFT JOIN orders_v2 o ON o.id = oi.order_id
        WHERE o.id IS NULL
    """)
    orphans = cur.fetchone()[0]
    report["order_items_v2_order_id_orphans"] = orphans
    if orphans == 0:
        cur.execute("""
            ALTER TABLE order_items_v2
                ADD CONSTRAINT order_items_v2_order_id_fkey
                FOREIGN KEY (order_id) REFERENCES orders_v2(id)
        """)
        report["order_items_v2_order_id_fk_added"] = True
    else:
        report["order_items_v2_order_id_fk_added"] = False
        cur.execute("""
            SELECT DISTINCT oi.order_id FROM order_items_v2 oi
            LEFT JOIN orders_v2 o ON o.id = oi.order_id
            WHERE o.id IS NULL LIMIT 20
        """)
        report["order_items_v2_order_id_orphan_examples"] = [r[0] for r in cur.fetchall()]
        log.error("NOT adding FK: %d order_id values have no matching orders_v2.id", orphans)


def reconciliation(cur, report):
    log.info("Reconciliation: counts")
    counts = {}
    for t in ("orders_v2", "order_items_v2"):
        cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT id), COUNT(DISTINCT uuid) FROM {t}")
        cnt, dcnt, ucnt = cur.fetchone()
        counts[t] = {"count": cnt, "distinct_id": dcnt, "distinct_uuid": ucnt}
    report["counts"] = counts

    cur.execute("SELECT COUNT(*) FROM (SELECT id FROM orders_v2 GROUP BY id HAVING COUNT(*) > 1) x")
    report["orders_v2_duplicate_ids_remaining"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM (SELECT id FROM order_items_v2 GROUP BY id HAVING COUNT(*) > 1) x")
    report["order_items_v2_duplicate_ids_remaining"] = cur.fetchone()[0]

    log.info("Reconciliation: monetary totals (orders.final_total)")
    cur.execute("SELECT SUM(final_total) FROM orders")
    old_sum = cur.fetchone()[0]
    cur.execute("SELECT SUM(final_total) FROM orders_v2")
    new_sum = cur.fetchone()[0]
    cur.execute("""
        SELECT SUM(final_total) FROM orders
        WHERE (id, created_date) NOT IN (
            SELECT id, created_date FROM (
                SELECT DISTINCT ON (id) id, created_date FROM orders ORDER BY id, updated_date DESC NULLS LAST, created_date DESC, ingested_at DESC
            ) keep
        )
    """)
    discarded_sum = cur.fetchone()[0]
    report["monetary_reconciliation"] = {
        "orders_final_total_sum": str(old_sum),
        "orders_v2_final_total_sum": str(new_sum),
        "delta": str(old_sum - new_sum),
        "sum_of_discarded_phantom_rows_final_total": str(discarded_sum),
        "delta_matches_discarded_sum": (old_sum - new_sum) == discarded_sum,
    }

    log.info("Spot-check: all 51 duplicate orders now have exactly 1 row in orders_v2, matching the retained (latest) row")
    cur.execute("""
        WITH dup_ids AS (SELECT id FROM orders GROUP BY id HAVING COUNT(*) > 1),
        expected AS (
            SELECT DISTINCT ON (o.id) o.id, o.created_date, o.updated_date, o.closed, o.final_total
            FROM orders o JOIN dup_ids d ON d.id = o.id
            ORDER BY o.id, o.updated_date DESC NULLS LAST, o.created_date DESC, o.ingested_at DESC
        )
        SELECT COUNT(*) FROM expected e
        JOIN orders_v2 v ON v.id = e.id
        WHERE v.created_date = e.created_date AND v.updated_date IS NOT DISTINCT FROM e.updated_date
          AND v.closed = e.closed AND v.final_total = e.final_total
    """)
    matched = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM orders GROUP BY id HAVING COUNT(*) > 1")
    total_dup_orders = len(cur.fetchall())
    report["duplicate_orders_spot_check"] = {"total_duplicate_orders": total_dup_orders, "matched_expected_retained_row": matched}

    log.info("Spot-check: all 15 duplicate items now have exactly 1 row in order_items_v2, matching the retained (latest) row")
    cur.execute("""
        WITH dup_ids AS (SELECT id FROM order_items GROUP BY id HAVING COUNT(*) > 1),
        expected AS (
            SELECT DISTINCT ON (oi.id) oi.id, oi.created_date, oi.price
            FROM order_items oi JOIN dup_ids d ON d.id = oi.id
            ORDER BY oi.id, oi.updated_date DESC NULLS LAST, oi.created_date DESC, oi.ingested_at DESC
        )
        SELECT COUNT(*) FROM expected e
        JOIN order_items_v2 v ON v.id = e.id
        WHERE v.created_date = e.created_date AND v.price = e.price
    """)
    matched_items = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM order_items GROUP BY id HAVING COUNT(*) > 1")
    total_dup_items = len(cur.fetchall())
    report["duplicate_items_spot_check"] = {"total_duplicate_items": total_dup_items, "matched_expected_retained_row": matched_items}


def referential_checks(cur, report):
    log.info("Referential checks: order_items_v2.order_id -> orders_v2.id")
    cur.execute("""
        SELECT COUNT(*) FROM order_items_v2 oi LEFT JOIN orders_v2 o ON o.id = oi.order_id WHERE o.id IS NULL
    """)
    report["order_items_v2_unresolved_order_id_rows"] = cur.fetchone()[0]

    refs = {}
    for t in ("payments", "order_history", "modifier_items"):
        cur.execute(f"""
            SELECT COUNT(DISTINCT t.order_id) FROM {t} t
            LEFT JOIN orders_v2 o ON o.id = t.order_id
            WHERE o.id IS NULL AND t.order_id IS NOT NULL
        """)
        refs[t] = cur.fetchone()[0]
    report["existing_tables_unresolved_order_id_against_orders_v2"] = refs


def query_validation(cur, report):
    log.info("Query validation: affected-query comparisons (orders vs orders_v2)")
    results = {}

    # aggregate_features.py-style day aggregation for the 2 exception orders'
    # dates: does v1's plain join double-count vs v2?
    for oid in EXCEPTION_ORDER_IDS:
        cur.execute("SELECT created_date FROM orders_v2 WHERE id = %s", (oid,))
        created = cur.fetchone()[0]
        day_start = created.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        cur.execute("""
            SELECT COUNT(oi.id), SUM(oi.price) FROM orders o
            LEFT JOIN order_items oi ON oi.order_id = o.id
                AND oi.created_date >= %(s)s AND oi.created_date < %(e)s AND oi.deleted = FALSE
            WHERE o.id = %(oid)s AND o.closed = TRUE AND o.deleted = FALSE
                AND o.created_date >= %(s)s AND o.created_date < %(e)s
        """, {"s": day_start, "e": day_end, "oid": oid})
        v1_count, v1_sum = cur.fetchone()

        cur.execute("""
            SELECT COUNT(oi.id), SUM(oi.price) FROM orders_v2 o
            LEFT JOIN order_items_v2 oi ON oi.order_id = o.id
                AND oi.created_date >= %(s)s AND oi.created_date < %(e)s AND oi.deleted = FALSE
            WHERE o.id = %(oid)s AND o.closed = TRUE AND o.deleted = FALSE
                AND o.created_date >= %(s)s AND o.created_date < %(e)s
        """, {"s": day_start, "e": day_end, "oid": oid})
        v2_count, v2_sum = cur.fetchone()

        results[f"aggregate_features_style_order_{oid}"] = {
            "v1_item_count": v1_count, "v1_price_sum": str(v1_sum),
            "v2_item_count": v2_count, "v2_price_sum": str(v2_sum),
            "double_count_eliminated": v1_count == 2 * v2_count if v2_count else None,
        }

    # dashboard.py-style join (o.created_date BETWEEN oi.created_date +-1day, closed=TRUE)
    cur.execute("""
        SELECT COUNT(*) FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
            AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
            AND o.closed = TRUE AND o.deleted = FALSE
        WHERE oi.order_id = ANY(%s) AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
    """, (EXCEPTION_ORDER_IDS,))
    v1_dash = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM order_items_v2 oi
        JOIN orders_v2 o ON o.id = oi.order_id
            AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
            AND o.closed = TRUE AND o.deleted = FALSE
        WHERE oi.order_id = ANY(%s) AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
    """, (EXCEPTION_ORDER_IDS,))
    v2_dash = cur.fetchone()[0]
    results["dashboard_style_join_exception_orders"] = {"v1_rows": v1_dash, "v2_rows": v2_dash}

    # weather_analysis-style join, same shape as dashboard
    cur.execute("""
        SELECT COUNT(*) FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
            AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
            AND o.closed = TRUE AND o.deleted = FALSE
        WHERE oi.order_id = ANY(%s) AND oi.deleted = FALSE AND oi.is_voided = FALSE
    """, (EXCEPTION_ORDER_IDS,))
    v1_weather = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM order_items_v2 oi
        JOIN orders_v2 o ON o.id = oi.order_id
            AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
            AND o.closed = TRUE AND o.deleted = FALSE
        WHERE oi.order_id = ANY(%s) AND oi.deleted = FALSE AND oi.is_voided = FALSE
    """, (EXCEPTION_ORDER_IDS,))
    v2_weather = cur.fetchone()[0]
    results["weather_analysis_style_join_exception_orders"] = {"v1_rows": v1_weather, "v2_rows": v2_weather}

    # Strategy A monitor equivalent against v2 (plain join now safe -- id is unique)
    cur.execute("""
        SELECT COUNT(*) FROM order_items_v2 oi
        JOIN orders_v2 o ON o.id = oi.order_id
        WHERE oi.updated_date IS NOT NULL AND o.updated_date IS NOT NULL
          AND oi.updated_date > o.updated_date AND oi.updated_date < NOW() - INTERVAL '1 hour'
    """)
    results["strategy_a_monitor_v2_violations"] = cur.fetchone()[0]

    report["query_validation"] = results


def regression_test(cur, report):
    log.info("Regression test: same order arrives open (created_date=T1) then closed with a DIFFERENT created_date (T2) -- must UPDATE, not INSERT a duplicate")
    test_id = 999999999999
    t1 = datetime(2099, 1, 1, 4, 30, 0, tzinfo=timezone.utc)
    t2 = datetime(2099, 1, 1, 15, 25, 0, tzinfo=timezone.utc)

    cur.execute("DELETE FROM orders_v2 WHERE id = %s", (test_id,))  # in case of a prior failed run

    upsert_sql = f"""
        INSERT INTO orders_v2 ({', '.join(ORDERS_COLS)}) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            {', '.join(f"{c} = EXCLUDED.{c}" for c in ORDERS_COLS if c != "id")}
    """
    from psycopg2.extras import execute_values

    def row(created, updated, closed, total):
        return (test_id, None, 26, None, created, updated, None, None, None,
                total, total, 0, 0, 0, closed, False, False, False, False,
                None, 0, datetime.now(timezone.utc), created.date(),
                None, None, None, None, None, None, None, None, None, None)

    # phase 1: order arrives open, $0, created_date=T1
    execute_values(cur, upsert_sql, [row(t1, t1, False, 0)])
    cur.execute("SELECT COUNT(*), created_date, closed, final_total FROM orders_v2 WHERE id = %s GROUP BY created_date, closed, final_total", (test_id,))
    phase1 = cur.fetchall()

    # phase 2: same order arrives again, closed, with a DIFFERENT created_date=T2
    execute_values(cur, upsert_sql, [row(t2, t2, True, 42.00)])
    cur.execute("SELECT COUNT(*) FROM orders_v2 WHERE id = %s", (test_id,))
    row_count_after = cur.fetchone()[0]
    cur.execute("SELECT created_date, closed, final_total FROM orders_v2 WHERE id = %s", (test_id,))
    final_row = cur.fetchone()

    passed = (
        len(phase1) == 1 and phase1[0][0] == 1 and
        row_count_after == 1 and
        final_row[0] == t2 and final_row[1] is True and final_row[2] == 42
    )

    report["regression_test_open_then_closed_changed_created_date"] = {
        "phase1_rows_for_id": phase1[0][0] if phase1 else None,
        "row_count_after_phase2": row_count_after,
        "final_created_date": str(final_row[0]),
        "final_closed": final_row[1],
        "final_total": str(final_row[2]),
        "passed": passed,
    }

    # clean up the synthetic test row -- must not be part of the committed shadow table
    cur.execute("DELETE FROM orders_v2 WHERE id = %s", (test_id,))

    if not passed:
        raise AssertionError(f"regression test FAILED: {report['regression_test_open_then_closed_changed_created_date']}")


def run():
    conn = P.db_connect()
    cur = conn.cursor()
    t0 = time.time()
    report = {}

    try:
        create_and_populate(cur, report)
        reconciliation(cur, report)
        referential_checks(cur, report)
        query_validation(cur, report)
        regression_test(cur, report)

        # ── final gate before commit ──
        assert report["orders_v2_duplicate_ids_remaining"] == 0
        assert report["order_items_v2_duplicate_ids_remaining"] == 0
        assert report["order_items_v2_unresolved_order_id_rows"] == 0
        assert all(v == 0 for v in report["existing_tables_unresolved_order_id_against_orders_v2"].values())
        assert report["monetary_reconciliation"]["delta_matches_discarded_sum"]
        assert report["duplicate_orders_spot_check"]["matched_expected_retained_row"] == report["duplicate_orders_spot_check"]["total_duplicate_orders"]
        assert report["duplicate_items_spot_check"]["matched_expected_retained_row"] == report["duplicate_items_spot_check"]["total_duplicate_items"]
        assert report["regression_test_open_then_closed_changed_created_date"]["passed"]

        cur.execute("SELECT pg_size_pretty(pg_total_relation_size('orders_v2')), pg_size_pretty(pg_total_relation_size('order_items_v2'))")
        report["table_sizes"] = dict(zip(("orders_v2", "order_items_v2"), cur.fetchone()))

        conn.commit()
        report["committed"] = True
        log.info("=== ALL CHECKS PASSED — COMMITTED ===")

    except Exception:
        conn.rollback()
        report["committed"] = False
        log.exception("Migration FAILED validation — rolled back, orders_v2/order_items_v2 do NOT exist")
        raise
    finally:
        cur.close()
        conn.close()

    report["total_seconds"] = round(time.time() - t0, 2)
    return report


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, indent=2, default=str))
