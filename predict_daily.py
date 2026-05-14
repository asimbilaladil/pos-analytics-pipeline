"""
predict_daily.py
================
Generates 15-minute interval item quantity predictions for a target date.
Uses a weighted historical average of same-day-of-week data from raw order_items.
Most recent week gets the highest weight (linear decay).

Run after aggregate_features.py in the nightly pipeline (see run.sh).

Usage:
    python predict_daily.py                       # predict for today
    python predict_daily.py --date 2026-05-13     # specific date (backtest)
    python predict_daily.py --date 2026-05-13 --validate  # predict + accuracy report
    python predict_daily.py --lookback 12         # use 12 same-dow historical weeks
"""

import os
import sys
import logging
import argparse
import statistics
from datetime import date, datetime, timedelta, time
from collections import defaultdict

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPT_VERSION = "weighted_avg_v1"
DATA_START = date(2026, 2, 10)
DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def db_connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS"),
    )


def slot_to_time(slot_index: int) -> time:
    return time(slot_index // 4, (slot_index % 4) * 15)


def get_historical_dates(target_date: date, lookback: int) -> list:
    """Return up to `lookback` same-day-of-week dates strictly before target_date."""
    dates = []
    d = target_date - timedelta(days=7)
    while len(dates) < lookback and d >= DATA_START:
        dates.append(d)
        d -= timedelta(days=7)
    return dates


def fetch_slot_data(conn, dates: list) -> tuple:
    """
    Pull actual per-product per-15min-slot quantities for the given dates.
    Returns (data, product_names).
      data key: (est_id, product_id, slot_index, sale_date) -> qty (float)
      product_names key: (est_id, product_id) -> name
    """
    if not dates:
        return {}, {}

    placeholders = ",".join(["%s"] * len(dates))
    sql = f"""
        SELECT
            oi.establishment_id,
            oi.product_id,
            MAX(oi.product_name)                                                    AS product_name,
            (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
             + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15.0)
            )::SMALLINT                                                              AS slot_index,
            DATE(oi.created_date AT TIME ZONE 'America/Chicago')                    AS sale_date,
            SUM(oi.quantity)                                                         AS qty
        FROM order_items oi
        JOIN orders o
            ON  o.id      = oi.order_id
            AND o.closed  = TRUE
            AND o.deleted = FALSE
        WHERE
            oi.deleted        = FALSE
            AND oi.is_voided  = FALSE
            AND oi.product_id IS NOT NULL
            AND DATE(oi.created_date AT TIME ZONE 'America/Chicago') IN ({placeholders})
        GROUP BY
            oi.establishment_id, oi.product_id, slot_index, sale_date
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, dates)
        rows = cur.fetchall()

    data, product_names = {}, {}
    for row in rows:
        key = (row["establishment_id"], row["product_id"],
               int(row["slot_index"]), row["sale_date"])
        data[key] = float(row["qty"])
        product_names[(row["establishment_id"], row["product_id"])] = row["product_name"]

    return data, product_names


def filter_outlier_dates(data: dict, historical_dates: list) -> dict:
    """
    Per establishment, drop any historical date whose total daily volume
    exceeds mean + 2*std of that location's daily volumes.
    Returns {est_id: [clean_dates]} — outlier dates removed.
    """
    est_date_totals: dict = defaultdict(lambda: defaultdict(float))
    for (est_id, _prod, _slot, sale_date), qty in data.items():
        est_date_totals[est_id][sale_date] += qty

    filtered = {}
    for est_id, date_totals in est_date_totals.items():
        totals = [date_totals.get(d, 0.0) for d in historical_dates]
        non_zero = [t for t in totals if t > 0]
        if len(non_zero) >= 3:
            mean  = statistics.mean(non_zero)
            std   = statistics.stdev(non_zero)
            threshold = mean + 2 * std
            outliers = [d for d, t in zip(historical_dates, totals) if t > threshold]
            if outliers:
                log.info("  Est %d: dropping outlier date(s) %s (threshold %.0f units/day)",
                         est_id, [str(o) for o in outliers], threshold)
            filtered[est_id] = [d for d, t in zip(historical_dates, totals) if t <= threshold]
        else:
            filtered[est_id] = list(historical_dates)

    # Ensure every est that appears in historical_dates is present
    all_ests = {est_id for (est_id, *_) in data}
    for est_id in all_ests:
        if est_id not in filtered:
            filtered[est_id] = list(historical_dates)

    return filtered


def compute_predictions(data: dict, product_names: dict,
                        historical_dates: list, filtered_dates_by_est: dict,
                        target_date: date) -> list:
    """
    For each (est_id, product_id, slot_index), compute a recency-weighted
    average using that establishment's outlier-filtered date list.
    Weight[0] = n (most recent), Weight[n-1] = 1 (oldest).
    Confidence interval: predicted ± 1 std dev of the non-zero historical values.
    """
    grouped: dict = defaultdict(dict)
    for (est_id, prod_id, slot_idx, sale_date), qty in data.items():
        grouped[(est_id, prod_id, slot_idx)][sale_date] = qty

    predictions = []
    for (est_id, prod_id, slot_idx), date_qty in grouped.items():
        dates = filtered_dates_by_est.get(est_id, historical_dates)
        if not dates:
            continue

        n       = len(dates)
        weights = [n - i for i in range(n)]
        qtys    = [date_qty.get(d, 0.0) for d in dates]
        pred    = sum(q * w for q, w in zip(qtys, weights)) / sum(weights)

        if pred < 0.05:
            continue

        non_zero = [q for q in qtys if q > 0]
        std = statistics.stdev(non_zero) if len(non_zero) >= 2 else pred * 0.25

        predictions.append({
            "establishment_id":   est_id,
            "product_id":         prod_id,
            "product_name":       product_names.get((est_id, prod_id), "Unknown"),
            "target_date":        target_date,
            "slot_index":         slot_idx,
            "slot_start":         slot_to_time(slot_idx),
            "predicted_quantity": round(pred, 2),
            "confidence_low":     round(max(0.0, pred - std), 2),
            "confidence_high":    round(pred + std, 2),
            "historical_points":  sum(1 for q in qtys if q > 0),
        })

    return predictions


def upsert_predictions(conn, predictions: list):
    sql = """
        INSERT INTO predictions_15min (
            establishment_id, product_id, product_name,
            target_date, slot_index, slot_start,
            predicted_quantity, confidence_low, confidence_high,
            historical_points, model_version
        ) VALUES (
            %(establishment_id)s, %(product_id)s, %(product_name)s,
            %(target_date)s, %(slot_index)s, %(slot_start)s,
            %(predicted_quantity)s, %(confidence_low)s, %(confidence_high)s,
            %(historical_points)s, %(model_version)s
        )
        ON CONFLICT (establishment_id, product_id, target_date, slot_index) DO UPDATE SET
            product_name       = EXCLUDED.product_name,
            predicted_quantity = EXCLUDED.predicted_quantity,
            confidence_low     = EXCLUDED.confidence_low,
            confidence_high    = EXCLUDED.confidence_high,
            historical_points  = EXCLUDED.historical_points,
            model_version      = EXCLUDED.model_version,
            generated_at       = NOW()
    """
    rows = [{**p, "model_version": SCRIPT_VERSION} for p in predictions]
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)


def print_summary(conn, target_date: date, predictions: list):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id, name FROM establishments ORDER BY name")
        est_names = {r["id"]: r["name"] for r in cur.fetchall()}

    by_est = defaultdict(list)
    for p in predictions:
        by_est[p["establishment_id"]].append(p)

    print(f"\n{'='*68}")
    print(f"  LAYNES 15-MIN ITEM PREDICTIONS")
    print(f"  {DOW_NAMES[target_date.weekday()]} {target_date.strftime('%B %d, %Y')}"
          f"  |  {SCRIPT_VERSION}")
    print(f"{'='*68}")

    for est_id in sorted(by_est):
        preds    = by_est[est_id]
        pname    = {p["product_id"]: p["product_name"] for p in preds}

        prod_totals = defaultdict(float)
        for p in preds:
            prod_totals[p["product_id"]] += p["predicted_quantity"]
        top_prods = sorted(prod_totals.items(), key=lambda x: x[1], reverse=True)

        slot_totals = defaultdict(float)
        slot_items  = defaultdict(list)
        for p in preds:
            slot_totals[p["slot_index"]] += p["predicted_quantity"]
            slot_items[p["slot_index"]].append(p)
        peak_slot = max(slot_totals, key=slot_totals.get)

        print(f"\n{'─'*68}")
        print(f"  {est_names.get(est_id, f'Est {est_id}').upper()}")
        print(f"{'─'*68}")

        print("  ALL-DAY PREP TOTALS (top 10):")
        for pid, qty in top_prods[:10]:
            bar = "█" * min(int(qty / 5), 28)
            print(f"    {pname[pid][:32]:<32} {qty:6.1f}  {bar}")

        print(f"\n  PEAK PERIOD — around {slot_to_time(peak_slot).strftime('%H:%M')}"
              f" ({slot_totals[peak_slot]:.1f} units/slot):")
        for s in range(max(0, peak_slot - 2), min(96, peak_slot + 3)):
            t    = slot_to_time(s)
            top4 = sorted(slot_items[s], key=lambda x: x["predicted_quantity"], reverse=True)[:4]
            items = "  ".join(f"{p['product_name'][:18]}: {p['predicted_quantity']:.1f}" for p in top4)
            mark = " ◄ PEAK" if s == peak_slot else ""
            print(f"    {t.strftime('%H:%M')}{mark}  {items}")

        print(f"\n  SHIFT BREAKDOWN:")
        for shift_name, s0, s1 in [("Morning", 6*4, 11*4), ("Lunch", 11*4, 14*4),
                                    ("Afternoon", 14*4, 17*4), ("Dinner", 17*4, 21*4)]:
            sp = [p for p in preds if s0 <= p["slot_index"] < s1]
            if not sp:
                continue
            st = defaultdict(float)
            for p in sp:
                st[p["product_id"]] += p["predicted_quantity"]
            total = sum(st.values())
            top5  = sorted(st.items(), key=lambda x: x[1], reverse=True)[:5]
            items = ", ".join(f"{pname[pid][:16]}: {qty:.0f}" for pid, qty in top5)
            print(f"    {shift_name:10}  {total:6.0f} items  [{items}]")

    print(f"\n{'='*68}\n")


def print_accuracy(conn, target_date: date):
    """Compare stored predictions vs actual order_items for target_date."""
    sql = """
        WITH actual AS (
            SELECT
                oi.establishment_id,
                oi.product_id,
                (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
                 + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15.0)
                )::SMALLINT        AS slot_index,
                SUM(oi.quantity)   AS actual_qty
            FROM order_items oi
            JOIN orders o
                ON  o.id      = oi.order_id
                AND o.closed  = TRUE
                AND o.deleted = FALSE
            WHERE
                DATE(oi.created_date AT TIME ZONE 'America/Chicago') = %(dt)s
                AND oi.deleted        = FALSE
                AND oi.is_voided      = FALSE
                AND oi.product_id    IS NOT NULL
            GROUP BY oi.establishment_id, oi.product_id, slot_index
        )
        SELECT
            COUNT(*)                                                            AS total_slots,
            ROUND(AVG(ABS(p.predicted_quantity - COALESCE(a.actual_qty, 0))), 3) AS mae,
            ROUND(AVG(
                CASE WHEN p.predicted_quantity > 0
                THEN ABS(p.predicted_quantity - COALESCE(a.actual_qty, 0))
                     / p.predicted_quantity * 100
                END
            ), 1)                                                               AS mape_pct,
            COUNT(*) FILTER (
                WHERE ABS(p.predicted_quantity - COALESCE(a.actual_qty, 0)) <= 1
            )                                                                   AS within_1_unit,
            COUNT(*) FILTER (
                WHERE ABS(p.predicted_quantity - COALESCE(a.actual_qty, 0)) <= 2
            )                                                                   AS within_2_units,
            ROUND(SUM(p.predicted_quantity), 0)                                AS total_predicted,
            ROUND(SUM(COALESCE(a.actual_qty, 0)), 0)                           AS total_actual
        FROM predictions_15min p
        LEFT JOIN actual a
            ON  a.establishment_id = p.establishment_id
            AND a.product_id       = p.product_id
            AND a.slot_index       = p.slot_index
        WHERE p.target_date = %(dt)s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, {"dt": target_date})
        row = cur.fetchone()

    within_1_pct = int(100 * row["within_1_unit"] // row["total_slots"]) if row["total_slots"] else 0
    within_2_pct = int(100 * row["within_2_units"] // row["total_slots"]) if row["total_slots"] else 0

    print(f"\n{'='*68}")
    print(f"  ACCURACY REPORT — predictions vs actual for {target_date}")
    print(f"{'='*68}")
    print(f"  Total prediction slots : {row['total_slots']}")
    print(f"  Total predicted items  : {row['total_predicted']}")
    print(f"  Total actual items     : {row['total_actual']}")
    print(f"  Mean Absolute Error    : {row['mae']} units/slot")
    print(f"  MAPE                   : {row['mape_pct']}%")
    print(f"  Slots within ±1 unit   : {row['within_1_unit']}  ({within_1_pct}%)")
    print(f"  Slots within ±2 units  : {row['within_2_units']}  ({within_2_pct}%)")
    print(f"{'='*68}\n")


def main():
    parser = argparse.ArgumentParser(description="15-min item predictions for a target date")
    parser.add_argument("--date",     type=str,           default=None,
                        help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--lookback", type=int,           default=8,
                        help="Same-dow historical weeks to use (default: 8)")
    parser.add_argument("--validate", action="store_true",
                        help="Print accuracy report comparing predictions vs actual")
    args = parser.parse_args()

    if not os.getenv("DB_PASS"):
        log.error("DB_PASS must be set")
        sys.exit(1)

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    log.info("Target: %s (%s)", target_date, DOW_NAMES[target_date.weekday()])

    conn = db_connect()
    try:
        hist_dates = get_historical_dates(target_date, args.lookback)
        if not hist_dates:
            log.error("No historical dates found before %s — cannot predict", target_date)
            sys.exit(1)

        log.info("Historical dates (%d): %s … %s",
                 len(hist_dates), hist_dates[0], hist_dates[-1])

        raw_data, product_names = fetch_slot_data(conn, hist_dates)
        log.info("Fetched %d (est, product, slot, date) data points", len(raw_data))

        log.info("Filtering outlier dates (>mean+2σ daily volume) per establishment...")
        filtered_dates = filter_outlier_dates(raw_data, hist_dates)

        preds = compute_predictions(raw_data, product_names, hist_dates, filtered_dates, target_date)
        log.info("Computed %d prediction rows", len(preds))

        upsert_predictions(conn, preds)
        conn.commit()
        log.info("Saved to predictions_15min")

        print_summary(conn, target_date, preds)

        if args.validate:
            print_accuracy(conn, target_date)

    except Exception as exc:
        conn.rollback()
        log.error("Failed: %s", exc)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
