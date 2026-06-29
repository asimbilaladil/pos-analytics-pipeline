"""
predict_daily_ml.py  (v7 — calibration + DOW baseline + 56-day recency)
========================================================================
LightGBM-based 15-min item quantity predictions.

Architecture:
  - Per-location LightGBM for each location's top-20 items by volume (~78% of sales).
  - Global LightGBM fallback for all remaining low-volume items.
  - DOW-average baseline for locations with insufficient training data.
  - Both models use Python-computed lag features for train/predict consistency.

New in v7 vs v6:
  - Post-hoc location calibration: scale each location's predictions by
    mean(actual/predicted) over the last 7 days (requires ≥3 dates, clamped 0.5–2.0).
  - DOW-average baseline: locations with < 200 training rows use roll_dow_4w
    directly instead of falling through to the global model.
  - Recency half-life extended 28 → 56 days (less aggressive, more stable).

Usage:
    python predict_daily_ml.py                            # all locations, today
    python predict_daily_ml.py --date 2026-05-13          # backtest all
    python predict_daily_ml.py --date 2026-05-13 --location Airtex   # single loc
    python predict_daily_ml.py --date 2026-05-13 --validate
"""

import os
import sys
import logging
import argparse
import datetime
from datetime import date, timedelta, time
from collections import defaultdict

import numpy as np
import pandas as pd
import lightgbm as lgb
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPT_VERSION    = "lgbm_v7"
DATA_START        = date(2026, 2, 10)
MIN_DAYS          = 3
TOP_N             = 20          # items per location for per-location models
RECENCY_HALF_LIFE = 56          # days — older rows down-weighted with this half-life
CAL_DAYS          = 7           # past dates used to compute calibration factors
CAL_MIN_DATES     = 3           # minimum past dates needed for calibration
DOW_NAMES         = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
LAG_OFFSETS       = [7, 14, 21, 28, 35, 42, 49, 56]

# Global model features (includes establishment_id)
FEATURE_COLS = [
    "establishment_id", "product_id",
    "slot_index", "day_of_week", "week_of_year", "month", "is_weekend",
    "lag_7d", "lag_14d", "lag_21d",
    "roll_dow_4w", "roll_dow_8w", "roll_dow_std",
    "activity_rate",
    "loc_dow_4w",   # avg total sales at this location on same DOW last 4 weeks
]
CAT_COLS     = ["establishment_id", "product_id"]
NUMERIC_COLS = [c for c in FEATURE_COLS if c not in CAT_COLS]

# Per-location model features (establishment_id is constant — drop it)
LOC_FEATURE_COLS = [c for c in FEATURE_COLS if c != "establishment_id"]
LOC_CAT_COLS     = ["product_id"]
LOC_NUMERIC_COLS = [c for c in LOC_FEATURE_COLS if c not in LOC_CAT_COLS]

# ── SQL ───────────────────────────────────────────────────────────────────────

SLOT_DATA_SQL = """
SELECT
    oi.establishment_id,
    oi.product_id,
    MAX(oi.product_name)                                                      AS product_name,
    (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
     + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15.0)
    )::SMALLINT                                                                AS slot_index,
    DATE(oi.created_date AT TIME ZONE 'America/Chicago')                      AS sale_date,
    SUM(oi.quantity)::float                                                    AS qty
FROM order_items oi
JOIN orders o ON o.id = oi.order_id AND o.closed = TRUE AND o.deleted = FALSE
WHERE
    oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
    AND DATE(oi.created_date AT TIME ZONE 'America/Chicago') >= %(data_start)s
    AND DATE(oi.created_date AT TIME ZONE 'America/Chicago') <  %(end_date)s
GROUP BY oi.establishment_id, oi.product_id, slot_index, sale_date
ORDER BY oi.establishment_id, oi.product_id, slot_index, sale_date
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def db_connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "laynes_user"),
        password=os.getenv("DB_PASS"),
    )


def slot_to_time(slot_index: int) -> time:
    return time(slot_index // 4, (slot_index % 4) * 15)


def df_from_query(conn, sql, params) -> pd.DataFrame:
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


def find_top_items(df_raw: pd.DataFrame, est_id: int, n: int = TOP_N) -> set:
    """Return set of top-N product_ids by total quantity for one establishment."""
    sub = df_raw[df_raw["establishment_id"] == est_id]
    return set(
        sub.groupby("product_id")["qty"]
        .sum()
        .sort_values(ascending=False)
        .head(n)
        .index.tolist()
    )


# ── Feature engineering ───────────────────────────────────────────────────────

def build_lag_features(df: pd.DataFrame, target_date: date, total_days: int) -> tuple:
    """
    Returns (df_train, df_val, df_pred):
      df_train: rows with all features, sale_date < target_date - 14
      df_val:   rows with all features, target_date-14 <= sale_date < target_date
      df_pred:  one row per active combo for target_date, with lag features
    """
    df = df.copy()
    df["sale_date"] = pd.to_datetime(df["sale_date"]).dt.date
    df["qty"]       = df["qty"].astype(float)

    combo_counts = (df.groupby(["establishment_id", "product_id", "slot_index"])
                      ["sale_date"].nunique())
    valid_set  = set(combo_counts[combo_counts >= MIN_DAYS].index)
    act_rate   = (combo_counts / float(total_days)).to_dict()

    pname_df = (df.sort_values("sale_date")
                  .groupby(["establishment_id", "product_id"])["product_name"]
                  .last()
                  .to_dict())

    # ── location daily total dict: (est_id, date) -> total_qty ──────────────
    loc_daily: dict = {}
    for (est, dt), grp in df.groupby(["establishment_id", "sale_date"]):
        loc_daily[(int(est), dt)] = float(grp["qty"].sum())

    log.info("  Building lookup dict (%d rows)...", len(df))
    lookup: dict = {}
    for r in df.itertuples(index=False):
        lookup[(int(r.establishment_id), int(r.product_id),
                int(r.slot_index), r.sale_date)] = float(r.qty)

    mask = np.array([
        (int(r.establishment_id), int(r.product_id), int(r.slot_index)) in valid_set
        for r in df.itertuples(index=False)
    ], dtype=bool)
    df_v = df[mask].reset_index(drop=True)
    log.info("  Valid training rows: %d  (filtered from %d)", len(df_v), len(df))

    log.info("  Computing lag features for training data...")
    lags_mat = np.zeros((len(df_v), len(LAG_OFFSETS)), dtype=np.float32)
    for idx, r in enumerate(df_v.itertuples(index=False)):
        base = (int(r.establishment_id), int(r.product_id), int(r.slot_index))
        for j, d in enumerate(LAG_OFFSETS):
            lags_mat[idx, j] = lookup.get(base + (r.sale_date - timedelta(days=d),), 0.0)

    df_v = df_v.copy()
    df_v["lag_7d"]        = lags_mat[:, 0]
    df_v["lag_14d"]       = lags_mat[:, 1]
    df_v["lag_21d"]       = lags_mat[:, 2]
    df_v["roll_dow_4w"]   = lags_mat[:, :4].mean(axis=1)
    df_v["roll_dow_8w"]   = lags_mat[:, :8].mean(axis=1)
    df_v["roll_dow_std"]  = lags_mat[:, :8].std(axis=1)
    df_v["day_of_week"]   = np.array([d.isoweekday() - 1 for d in df_v["sale_date"]], dtype=np.int32)
    df_v["week_of_year"]  = np.array([d.isocalendar()[1] for d in df_v["sale_date"]], dtype=np.int32)
    df_v["month"]         = np.array([d.month for d in df_v["sale_date"]], dtype=np.int32)
    df_v["is_weekend"]    = np.array([int(d.isoweekday() >= 6) for d in df_v["sale_date"]], dtype=np.int32)
    df_v["activity_rate"] = np.array([
        act_rate.get((int(r.establishment_id), int(r.product_id), int(r.slot_index)), 0.0)
        for r in df_v.itertuples(index=False)
    ], dtype=np.float32)

    # loc_dow_4w: avg location total on same DOW over last 4 same-DOW dates (vectorized)
    dates_arr = df_v["sale_date"].values          # numpy array of date objects
    ests_arr  = df_v["establishment_id"].values.astype(int)
    loc_mat   = np.zeros((len(df_v), 4), dtype=np.float32)
    for i in range(1, 5):
        loc_mat[:, i-1] = np.array([
            loc_daily.get((int(ests_arr[k]), dates_arr[k] - timedelta(days=7*i)), 0.0)
            for k in range(len(df_v))
        ], dtype=np.float32)
    df_v["loc_dow_4w"] = loc_mat.mean(axis=1)

    # recency weight: vectorized exponential decay
    decay = np.log(2) / RECENCY_HALF_LIFE
    days_old = np.array([(target_date - d).days for d in dates_arr], dtype=np.float32)
    df_v["sample_weight"] = np.exp(-decay * days_old).astype(np.float32)

    val_start = target_date - timedelta(days=14)
    df_train  = df_v[df_v["sale_date"] <  val_start].reset_index(drop=True)
    df_val    = df_v[df_v["sale_date"] >= val_start].reset_index(drop=True)

    log.info("  Building prediction features for %s...", target_date)
    last_8_dow_dates = {target_date - timedelta(days=7 * i) for i in range(1, 9)}

    active_combos: dict = defaultdict(int)
    df_recent = df[df["sale_date"].isin(last_8_dow_dates)]
    for r in df_recent.itertuples(index=False):
        key = (int(r.establishment_id), int(r.product_id), int(r.slot_index))
        if key in valid_set:
            active_combos[key] += 1

    log.info("  Active combos for %s: %d", target_date, len(active_combos))

    pred_rows = []
    for (est, pid, slot), rdow_count in active_combos.items():
        base     = (est, pid, slot)
        lag_vals = np.array([lookup.get(base + (target_date - timedelta(days=d),), 0.0)
                             for d in LAG_OFFSETS], dtype=np.float32)
        loc_dow_4w = np.mean([
            loc_daily.get((est, target_date - timedelta(days=7*i)), 0.0)
            for i in range(1, 5)
        ])
        pred_rows.append({
            "establishment_id": est,
            "product_id":       pid,
            "product_name":     pname_df.get((est, pid), "Unknown"),
            "slot_index":       slot,
            "day_of_week":      target_date.isoweekday() - 1,
            "week_of_year":     int(target_date.isocalendar()[1]),
            "month":            target_date.month,
            "is_weekend":       int(target_date.isoweekday() >= 6),
            "activity_rate":    float(act_rate.get(base, 0.0)),
            "lag_7d":           lag_vals[0],
            "lag_14d":          lag_vals[1],
            "lag_21d":          lag_vals[2],
            "roll_dow_4w":      float(lag_vals[:4].mean()),
            "roll_dow_8w":      float(lag_vals[:8].mean()),
            "roll_dow_std":     float(lag_vals[:8].std()),
            "loc_dow_4w":       float(loc_dow_4w),
            "recent_dow_count": rdow_count,
        })

    df_pred = pd.DataFrame(pred_rows).reset_index(drop=True)
    return df_train, df_val, df_pred


# ── Model training ────────────────────────────────────────────────────────────

def _prep_global(df):
    X = df[FEATURE_COLS].copy()
    for c in NUMERIC_COLS:
        X[c] = pd.to_numeric(X[c], errors="coerce").fillna(0).astype("float32")
    for c in CAT_COLS:
        X[c] = X[c].astype("category")
    return X, df["qty"].values.astype("float32")


def _prep_loc(df):
    X = df[LOC_FEATURE_COLS].copy()
    for c in LOC_NUMERIC_COLS:
        X[c] = pd.to_numeric(X[c], errors="coerce").fillna(0).astype("float32")
    for c in LOC_CAT_COLS:
        X[c] = X[c].astype("category")
    return X, df["qty"].values.astype("float32")


def train_global_model(df_train: pd.DataFrame, df_val: pd.DataFrame):
    X_tr, y_tr = _prep_global(df_train)
    X_va, y_va = _prep_global(df_val)
    w_tr = df_train["sample_weight"].values.astype("float32")
    tr_set = lgb.Dataset(X_tr, label=y_tr, weight=w_tr, categorical_feature=CAT_COLS, free_raw_data=False)
    va_set = lgb.Dataset(X_va, label=y_va, reference=tr_set,                          free_raw_data=False)
    params = {
        "objective":        "regression_l1",
        "metric":           "mae",
        "num_leaves":       63,
        "learning_rate":    0.05,
        "min_data_in_leaf": 20,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq":     5,
        "num_threads":      4,
        "verbose":          -1,
        "seed":             42,
    }
    model = lgb.train(
        params, tr_set,
        num_boost_round=1000,
        valid_sets=[va_set],
        callbacks=[
            lgb.early_stopping(stopping_rounds=50, verbose=False),
            lgb.log_evaluation(period=100),
        ],
    )
    val_mae = float(np.mean(np.abs(model.predict(X_va) - y_va)))
    return model, val_mae


def train_loc_model(df_train: pd.DataFrame, df_val: pd.DataFrame):
    X_tr, y_tr = _prep_loc(df_train)
    X_va, y_va = _prep_loc(df_val)
    w_tr = df_train["sample_weight"].values.astype("float32")
    tr_set = lgb.Dataset(X_tr, label=y_tr, weight=w_tr, categorical_feature=LOC_CAT_COLS, free_raw_data=False)
    va_set = lgb.Dataset(X_va, label=y_va, reference=tr_set,                              free_raw_data=False)
    params = {
        "objective":        "regression_l1",
        "metric":           "mae",
        "num_leaves":       31,      # smaller model for smaller dataset
        "learning_rate":    0.05,
        "min_data_in_leaf": 10,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq":     5,
        "num_threads":      4,
        "verbose":          -1,
        "seed":             42,
    }
    model = lgb.train(
        params, tr_set,
        num_boost_round=1000,
        valid_sets=[va_set],
        callbacks=[
            lgb.early_stopping(stopping_rounds=50, verbose=False),
            lgb.log_evaluation(period=100),
        ],
    )
    val_mae = float(np.mean(np.abs(model.predict(X_va) - y_va)))
    return model, val_mae


# ── DB write ──────────────────────────────────────────────────────────────────

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
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, predictions, page_size=500)


# ── Output ────────────────────────────────────────────────────────────────────

def print_summary(conn, target_date: date, predictions: list):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id, name FROM establishments ORDER BY name")
        est_names = {r["id"]: r["name"] for r in cur.fetchall()}

    by_est = defaultdict(list)
    for p in predictions:
        by_est[p["establishment_id"]].append(p)

    print(f"\n{'='*68}")
    print(f"  LAYNES 15-MIN PREDICTIONS  [{SCRIPT_VERSION}]")
    print(f"  {DOW_NAMES[target_date.weekday()]} {target_date.strftime('%B %d, %Y')}")
    print(f"{'='*68}")

    for est_id in sorted(by_est):
        preds  = by_est[est_id]
        pname  = {p["product_id"]: p["product_name"] for p in preds}

        prod_t = defaultdict(float)
        for p in preds:
            prod_t[p["product_id"]] += p["predicted_quantity"]
        top_prods = sorted(prod_t.items(), key=lambda x: x[1], reverse=True)

        slot_t = defaultdict(float)
        slot_i = defaultdict(list)
        for p in preds:
            slot_t[p["slot_index"]] += p["predicted_quantity"]
            slot_i[p["slot_index"]].append(p)
        peak = max(slot_t, key=slot_t.get)

        print(f"\n{'─'*68}")
        print(f"  {est_names.get(est_id, f'Est {est_id}').upper()}")
        print(f"{'─'*68}")
        print("  ALL-DAY PREP TOTALS (top 10):")
        for pid, qty in top_prods[:10]:
            bar = "█" * min(int(qty / 5), 28)
            print(f"    {pname[pid][:32]:<32} {qty:6.1f}  {bar}")

        print(f"\n  PEAK PERIOD — {slot_to_time(peak).strftime('%H:%M')}"
              f" ({slot_t[peak]:.1f} units/slot):")
        for s in range(max(0, peak - 2), min(96, peak + 3)):
            t    = slot_to_time(s)
            top4 = sorted(slot_i[s], key=lambda x: x["predicted_quantity"], reverse=True)[:4]
            items = "  ".join(f"{p['product_name'][:18]}: {p['predicted_quantity']:.1f}" for p in top4)
            mark = " ◄ PEAK" if s == peak else ""
            print(f"    {t.strftime('%H:%M')}{mark}  {items}")

        print(f"\n  SHIFT BREAKDOWN:")
        for sname, s0, s1 in [("Morning",6*4,11*4),("Lunch",11*4,14*4),
                               ("Afternoon",14*4,17*4),("Dinner",17*4,21*4)]:
            sp = [p for p in preds if s0 <= p["slot_index"] < s1]
            if not sp:
                continue
            st = defaultdict(float)
            for p in sp:
                st[p["product_id"]] += p["predicted_quantity"]
            total = sum(st.values())
            top5  = sorted(st.items(), key=lambda x: x[1], reverse=True)[:5]
            items = ", ".join(f"{pname[pid][:16]}: {qty:.0f}" for pid, qty in top5)
            print(f"    {sname:10}  {total:6.0f} items  [{items}]")

    print(f"\n{'='*68}\n")


def print_accuracy(conn, target_date: date):
    sql = """
        WITH actual AS (
            SELECT
                oi.establishment_id, oi.product_id,
                (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
                 + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15.0)
                )::SMALLINT AS slot_index,
                SUM(oi.quantity) AS actual_qty
            FROM order_items oi
            JOIN orders o ON o.id=oi.order_id AND o.closed=TRUE AND o.deleted=FALSE
            WHERE DATE(oi.created_date AT TIME ZONE 'America/Chicago') = %(dt)s
              AND oi.deleted=FALSE AND oi.is_voided=FALSE AND oi.product_id IS NOT NULL
            GROUP BY oi.establishment_id, oi.product_id, slot_index
        )
        SELECT
            COUNT(*)                                                               AS total_slots,
            ROUND(AVG(ABS(p.predicted_quantity - COALESCE(a.actual_qty,0))),3)    AS mae,
            ROUND(AVG(CASE WHEN p.predicted_quantity > 0
                THEN ABS(p.predicted_quantity-COALESCE(a.actual_qty,0))/p.predicted_quantity*100
                END),1)                                                            AS mape_pct,
            COUNT(*) FILTER (WHERE ABS(p.predicted_quantity-COALESCE(a.actual_qty,0))<=1) AS within_1,
            COUNT(*) FILTER (WHERE ABS(p.predicted_quantity-COALESCE(a.actual_qty,0))<=2) AS within_2,
            ROUND(SUM(p.predicted_quantity),0)        AS total_predicted,
            ROUND(SUM(COALESCE(a.actual_qty,0)),0)    AS total_actual
        FROM predictions_15min p
        LEFT JOIN actual a ON a.establishment_id=p.establishment_id
                          AND a.product_id=p.product_id AND a.slot_index=p.slot_index
        WHERE p.target_date=%(dt)s AND p.model_version=%(ver)s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, {"dt": target_date, "ver": SCRIPT_VERSION})
        r = cur.fetchone()

    w1 = int(100 * r["within_1"] // r["total_slots"]) if r["total_slots"] else 0
    w2 = int(100 * r["within_2"] // r["total_slots"]) if r["total_slots"] else 0

    print(f"\n{'='*68}")
    print(f"  ACCURACY  [{SCRIPT_VERSION}]  vs actual — {target_date}")
    print(f"{'='*68}")
    print(f"  Total prediction slots : {r['total_slots']}")
    print(f"  Total predicted items  : {r['total_predicted']}")
    print(f"  Total actual items     : {r['total_actual']}")
    print(f"  Mean Absolute Error    : {r['mae']} units/slot")
    print(f"  MAPE                   : {r['mape_pct']}%")
    print(f"  Slots within ±1 unit   : {r['within_1']}  ({w1}%)")
    print(f"  Slots within ±2 units  : {r['within_2']}  ({w2}%)")
    print(f"{'='*68}\n")


# ── Calibration ───────────────────────────────────────────────────────────────

def load_calibration_factors(conn, target_date: date) -> dict:
    """
    Per-location scale factor = mean(actual/predicted) over last CAL_DAYS past dates.
    Returns {est_id: factor} only for locations with >= CAL_MIN_DATES data points.
    Factor is clamped to [0.5, 2.0]; absent locations default to 1.0 at call site.
    """
    past_dates = [target_date - timedelta(days=i) for i in range(1, CAL_DAYS + 1)]
    sql = """
        WITH actuals AS (
            SELECT
                oi.establishment_id,
                DATE(oi.created_date AT TIME ZONE 'America/Chicago') AS sale_date,
                oi.product_id,
                (EXTRACT(HOUR FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
                 + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15.0)
                )::SMALLINT AS slot_index,
                SUM(oi.quantity) AS actual_qty
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id AND o.closed = TRUE AND o.deleted = FALSE
            WHERE oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
              AND DATE(oi.created_date AT TIME ZONE 'America/Chicago') = ANY(%(past_dates)s)
            GROUP BY oi.establishment_id, sale_date, oi.product_id, slot_index
        ),
        pred_latest AS (
            SELECT DISTINCT ON (establishment_id, target_date, product_id, slot_index)
                establishment_id, target_date, product_id, slot_index, predicted_quantity
            FROM predictions_15min
            WHERE target_date = ANY(%(past_dates)s) AND predicted_quantity > 0
            ORDER BY establishment_id, target_date, product_id, slot_index, generated_at DESC
        ),
        daily AS (
            SELECT
                p.establishment_id,
                p.target_date,
                SUM(p.predicted_quantity)      AS total_pred,
                SUM(COALESCE(a.actual_qty, 0)) AS total_actual
            FROM pred_latest p
            LEFT JOIN actuals a
                ON a.establishment_id = p.establishment_id
               AND a.sale_date        = p.target_date
               AND a.product_id       = p.product_id
               AND a.slot_index       = p.slot_index
            GROUP BY p.establishment_id, p.target_date
        )
        SELECT establishment_id, target_date, total_pred, total_actual
        FROM daily
        WHERE total_pred > 0
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, {"past_dates": past_dates})
        rows = cur.fetchall()

    by_est: dict = defaultdict(list)
    for r in rows:
        ratio = float(r["total_actual"]) / float(r["total_pred"])
        by_est[int(r["establishment_id"])].append(ratio)

    factors = {}
    for est_id, ratios in by_est.items():
        if len(ratios) >= CAL_MIN_DATES:
            factors[est_id] = float(np.clip(np.mean(ratios), 0.5, 2.0))
    return factors


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",     type=str, default=None)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--location", type=str, default=None,
                        help="Test a single location by name (e.g. 'Airtex'). "
                             "Trains and predicts only for that location — much faster.")
    args = parser.parse_args()

    if not os.getenv("DB_PASS"):
        log.error("DB_PASS must be set"); sys.exit(1)

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    total_days  = (target_date - DATA_START).days
    val_start   = target_date - timedelta(days=14)

    log.info("Target: %s (%s)", target_date, DOW_NAMES[target_date.weekday()])
    log.info("Train : %s → %s  |  Val: %s → %s  |  History: %d days",
             DATA_START, val_start, val_start, target_date, total_days)

    conn = db_connect()
    try:
        # ── 1. Load raw slot data ──────────────────────────────────────────────
        log.info("Loading slot data from DB...")
        df_raw = df_from_query(conn, SLOT_DATA_SQL, {
            "data_start": DATA_START,
            "end_date":   target_date,
        })
        log.info("  %d positive slot observations", len(df_raw))

        # ── Resolve single-location filter ─────────────────────────────────────
        single_est_id = None
        if args.location:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT id, name FROM establishments")
                est_map = {r["name"].lower(): r["id"] for r in cur.fetchall()}
            key = args.location.lower()
            if key not in est_map:
                log.error("Location '%s' not found. Available: %s",
                          args.location, ", ".join(sorted(est_map)))
                sys.exit(1)
            single_est_id = est_map[key]
            log.info("Single-location mode: %s (id=%s)", args.location, single_est_id)
            df_raw = df_raw[df_raw["establishment_id"] == single_est_id].reset_index(drop=True)

        # ── 2. Build lag features ──────────────────────────────────────────────
        df_train, df_val, df_pred = build_lag_features(df_raw, target_date, total_days)
        log.info("  Train: %d rows  |  Val: %d rows  |  Pred combos: %d",
                 len(df_train), len(df_val), len(df_pred))

        # ── 3. Per-location models (top-20 items each) ─────────────────────────
        establishments = sorted(df_raw["establishment_id"].unique())
        top20: dict       = {}   # est_id -> set of top-20 product_ids seen in training
        loc_models: dict  = {}   # est_id -> (model, val_mae)
        low_data_ests: set = set()  # ests that fall back to DOW-average baseline

        for est_id in establishments:
            top20_raw = find_top_items(df_raw, est_id, TOP_N)

            tr_loc = df_train[
                (df_train["establishment_id"] == est_id) &
                (df_train["product_id"].isin(top20_raw))
            ]
            va_loc = df_val[
                (df_val["establishment_id"] == est_id) &
                (df_val["product_id"].isin(top20_raw))
            ]

            if len(tr_loc) < 200 or len(va_loc) < 10:
                log.warning("  Est %s: too few data (train=%d, val=%d) — using DOW-average baseline",
                            est_id, len(tr_loc), len(va_loc))
                low_data_ests.add(est_id)
                continue

            log.info("  Training per-location model — est %s  (train=%d, val=%d, items=%d)...",
                     est_id, len(tr_loc), len(va_loc), tr_loc["product_id"].nunique())
            model, mae = train_loc_model(tr_loc, va_loc)
            loc_models[est_id] = (model, mae)
            # track which product_ids the loc model actually saw in training
            top20[est_id] = set(tr_loc["product_id"].unique())
            log.info("    Val MAE: %.4f", mae)

        log.info("Per-location models trained: %d / %d locations", len(loc_models), len(establishments))

        # ── 4. Global fallback model (skipped in single-location mode) ─────────
        global_model, global_mae = None, 0.0
        if single_est_id is None:
            log.info("Training global fallback model...")
            global_model, global_mae = train_global_model(df_train, df_val)
            log.info("  Global Val MAE: %.4f  |  Best iter: %d",
                     global_mae, global_model.best_iteration)
            imp = sorted(zip(FEATURE_COLS, global_model.feature_importance("gain")),
                         key=lambda x: x[1], reverse=True)
            log.info("  Top features: %s", ", ".join(f"{n}={v:.0f}" for n, v in imp[:6]))

        # ── 5. Predict ────────────────────────────────────────────────────────
        if df_pred.empty:
            log.warning("No combos to predict — nothing written.")
            return

        # Base predictions: global model (or zeros in single-location mode)
        if global_model is not None:
            X_global = df_pred[FEATURE_COLS].copy()
            for c in NUMERIC_COLS:
                X_global[c] = pd.to_numeric(X_global[c], errors="coerce").fillna(0).astype("float32")
            for c in CAT_COLS:
                X_global[c] = X_global[c].astype("category")
            raw = np.maximum(global_model.predict(X_global), 0.0)
        else:
            raw = np.zeros(len(df_pred), dtype=np.float64)

        mae_arr = np.full(len(df_pred), global_mae, dtype=np.float64)

        # Override with per-location predictions for top-20 items
        for est_id, (model, mae) in loc_models.items():
            mask = (
                (df_pred["establishment_id"] == est_id) &
                (df_pred["product_id"].isin(top20[est_id]))
            ).values

            if not mask.any():
                continue

            X_loc = df_pred.loc[mask, LOC_FEATURE_COLS].copy()
            for c in LOC_NUMERIC_COLS:
                X_loc[c] = pd.to_numeric(X_loc[c], errors="coerce").fillna(0).astype("float32")
            for c in LOC_CAT_COLS:
                X_loc[c] = X_loc[c].astype("category")

            raw[mask]     = np.maximum(model.predict(X_loc), 0.0)
            mae_arr[mask] = mae

        # DOW-average baseline for low-data locations
        dow_slots = 0
        for est_id in low_data_ests:
            mask = (df_pred["establishment_id"] == est_id).values
            if not mask.any():
                continue
            raw[mask]     = df_pred.loc[mask, "roll_dow_4w"].values.astype(np.float64)
            mae_arr[mask] = 0.0
            dow_slots    += int(mask.sum())
            log.info("  Est %s: applied DOW-average baseline (%d slots)", est_id, mask.sum())

        loc_slots    = int(sum(1 for v in mae_arr if v != global_mae))
        global_slots = int(sum(1 for v in mae_arr if v == global_mae))
        log.info("  Prediction routing — per-location: %d slots  |  DOW-baseline: %d slots  |  global fallback: %d slots",
                 loc_slots - dow_slots, dow_slots, global_slots)

        predictions = []
        for i, row in enumerate(df_pred.itertuples(index=False)):
            qty = float(raw[i]) * (row.recent_dow_count / 8.0)
            if qty < 0.05:
                continue
            used_mae = float(mae_arr[i])
            predictions.append({
                "establishment_id":   int(row.establishment_id),
                "product_id":         int(row.product_id),
                "product_name":       row.product_name or "Unknown",
                "target_date":        target_date,
                "slot_index":         int(row.slot_index),
                "slot_start":         slot_to_time(int(row.slot_index)),
                "predicted_quantity": round(qty, 2),
                "confidence_low":     round(max(0.0, qty - used_mae), 2),
                "confidence_high":    round(qty + used_mae, 2),
                "historical_points":  0,
                "model_version":      SCRIPT_VERSION,
            })

        # Post-hoc location calibration
        cal_factors = load_calibration_factors(conn, target_date)
        if cal_factors:
            log.info("  Calibration factors: %s",
                     ", ".join(f"est{k}={v:.3f}" for k, v in sorted(cal_factors.items())))
            for p in predictions:
                f = cal_factors.get(p["establishment_id"], 1.0)
                if f != 1.0:
                    p["predicted_quantity"] = round(p["predicted_quantity"] * f, 2)
                    p["confidence_low"]     = round(p["confidence_low"]     * f, 2)
                    p["confidence_high"]    = round(p["confidence_high"]    * f, 2)
        else:
            log.info("  No calibration factors found (first run or no past predictions).")

        log.info("Saving %d prediction rows...", len(predictions))
        with conn.cursor() as cur:
            if single_est_id is not None:
                cur.execute(
                    "DELETE FROM predictions_15min WHERE target_date = %s AND establishment_id = %s",
                    (target_date, single_est_id),
                )
            else:
                cur.execute("DELETE FROM predictions_15min WHERE target_date = %s", (target_date,))
            deleted = cur.rowcount
        log.info("  Cleared %d old prediction rows for %s", deleted, target_date)
        upsert_predictions(conn, predictions)
        conn.commit()
        log.info("Done.")

        print_summary(conn, target_date, predictions)

        if args.validate:
            print_accuracy(conn, target_date)

    except Exception as exc:
        conn.rollback()
        log.error("Failed: %s", exc)
        import traceback; traceback.print_exc()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
