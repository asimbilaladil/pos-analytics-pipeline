# POS Analytics Pipeline

A production-grade data pipeline for **Laynes Chicken Fingers** that pulls daily order data from the Revel POS API across 11 locations, stores it in PostgreSQL, builds pre-aggregated feature tables, and generates 15-minute interval item quantity predictions for each location.

---

## Architecture

```
Revel POS API
     │
     ▼
pipeline.py              ← fetch yesterday's orders + items + modifiers, insert into DB
     │
     ▼
aggregate_features.py    ← compute hourly/daily feature tables from raw data
     │
     ▼
predict_daily.py         ← generate 15-min item predictions for today (all 11 locations)
     │
     ▼
PostgreSQL (4 layers)
  ├── Layer 0: Reference tables  (establishments, products, modifiers, dining_channels)
  ├── Layer 1: Raw ingestion     (orders, order_items, modifier_items)  ← append-only, partitioned by month
  ├── Layer 2: Feature tables    (features_hourly, features_product_daily, features_daily_summary)
  └── Layer 3: Prediction output (predictions_15min, predictions_hourly_demand, predictions_prep_sheet, predictions_staffing)

All three scripts run sequentially at 2:00 AM via run.sh.
```

---

## Scripts

| Script | Purpose |
|---|---|
| `pipeline.py` | Main daily pipeline — logs into Revel, fetches all orders + items + modifiers for each location, inserts into PostgreSQL |
| `aggregate_features.py` | Nightly aggregation — reads raw tables, populates feature tables for ML |
| `predict_daily.py` | 15-min item prediction — generates per-product, per-slot quantity forecasts for each location; supports backtesting with `--validate` |
| `run.sh` | Daily cron wrapper — runs `pipeline.py` → `aggregate_features.py` → `predict_daily.py`, logs to `/var/log/laynes/run_YYYY-MM-DD.log`, rotates logs after 30 days |
| `backfill.sh` | Simple date-range backfill — loops day by day, runs pipeline + aggregation for each date |
| `backfill3m.sh` | Smart 3-month backfill — auto-resume (skips completed dates), rate limiting, failure tracking, `--status` mode |
| `ingest_to_db.py` | Offline ingestion — reads JSON files produced by `order_fixed.py` and inserts into DB |
| `order_fixed.py` | Manual/debug tool — fetches from Revel and saves JSON files to disk |
| `database_design.sql` | Full PostgreSQL schema (run once on a fresh DB) |
| `setup.sh` | One-command server setup for Ubuntu 22.04 |

---

## Database Schema

### Layer 0 — Reference Tables
- **`establishments`** — 11 Laynes locations with timezone info
- **`products`** — menu item master list from `/resources/Product/`
- **`modifiers`** — modifier reference table from `/resources/Modifier/` (~347 unique modifiers), refreshed nightly
- **`dining_channels`** — dining option codes (Drive Through, DoorDash, Uber Eats, etc.)

### Layer 1 — Raw Ingestion (partitioned by month)
- **`orders`** — one row per Revel order (~5,400/day across all locations)
- **`order_items`** — one row per item line (~4,600/day), includes kitchen timing (`start_time`, `kitchen_completed`, `kitchen_seconds`)
- **`modifier_items`** — sauces and customizations (~1,050/day)

### Layer 2 — Feature Tables (pre-aggregated for ML)
- **`features_hourly`** — order count, revenue, channel breakdown, avg kitchen time per location per hour
- **`features_product_daily`** — quantity sold, revenue, kitchen performance, void rate per product per location per day
- **`features_daily_summary`** — daily rollup per location including peak hour, channel mix, void rate

### Layer 3 — Prediction Output
- **`predictions_15min`** — predicted item quantities per product per 15-min slot per location (96 slots/day); written by `predict_daily.py`
- **`predictions_hourly_demand`** — predicted order count and revenue per hour
- **`predictions_prep_sheet`** — predicted prep quantities per item per shift
- **`predictions_staffing`** — recommended headcount per shift
- **`location_health_scores`** — composite daily score per location (0–100)

---

## Scale

| Table | Rows/day | Rows/year |
|---|---|---|
| orders | ~5,400 | ~2M |
| order_items | ~4,600 | ~1.7M |
| modifier_items | ~1,050 | ~380k |
| **Total** | **~11,100** | **~4M** |

Storage: ~1 GB/year including indexes. A $40–80/mo VPS with 50 GB SSD handles 3+ years.

---

## Setup

### 1. Server setup (Ubuntu 22.04)

```bash
chmod +x setup.sh && sudo bash setup.sh
```

This installs PostgreSQL, Python, Playwright + Chromium, creates the DB, writes `/opt/laynes/.env`, and sets up cron.

### 2. Environment variables

```bash
REVEL_USER=your_revel_email
REVEL_PASS=your_revel_password
ESTABLISHMENTS=32,14,48,7,6,25,36,26,20,40,15
DB_HOST=localhost
DB_PORT=5432
DB_NAME=laynes
DB_USER=laynes_user
DB_PASS=your_db_password
```

### 3. Database schema

```bash
psql -d laynes -f database_design.sql
```

### 4. Run manually

```bash
# Fetch yesterday's data and insert into DB
python3 pipeline.py

# Fetch a specific date (for backfilling)
python3 pipeline.py --date 2026-05-04

# Run only specific locations
python3 pipeline.py --establishments 32,14

# Aggregate feature tables (run after pipeline.py)
python3 aggregate_features.py --date 2026-05-04

# Generate 15-min predictions for today
python3 predict_daily.py

# Generate predictions for a specific date (backtest)
python3 predict_daily.py --date 2026-05-13

# Backtest + accuracy report vs actual data
python3 predict_daily.py --date 2026-05-13 --validate

# Use more historical weeks (default: 8)
python3 predict_daily.py --lookback 12
```

### 5. Backfill historical data

**Simple backfill** — loops day by day with a 2-second pause between dates:

```bash
bash /opt/laynes/backfill.sh 2026-01-01 2026-04-30
```

**Smart backfill** — auto-resumes (skips already-completed dates), 30-minute sleep between dates to avoid API rate limits, tracks failures:

```bash
# Last 3 months → yesterday (default)
nohup bash /opt/laynes/backfill3m.sh > /var/log/laynes/backfill3m.log 2>&1 &

# Custom date range
nohup bash /opt/laynes/backfill3m.sh 2026-01-01 2026-04-30 > /var/log/laynes/backfill3m.log 2>&1 &

# Watch live progress
tail -f /var/log/laynes/backfill3m.log

# Check status / see incomplete dates
bash /opt/laynes/backfill3m.sh --status
```

---

## Cron Schedule

```
0 2 * * *   /opt/laynes/run.sh   # 2:00 AM — fetch + insert + aggregate + predict
```

`run.sh` runs all three scripts in sequence and logs everything to `/var/log/laynes/run_YYYY-MM-DD.log`. Logs are retained for 30 days.

---

## 15-Min Item Prediction Model

`predict_daily.py` generates a daily forecast before the business day starts. For each location, product, and 15-minute slot it:

1. Looks back at the last 8 same-day-of-week dates (e.g. the last 8 Wednesdays)
2. Drops any date where that location's total daily volume exceeded mean + 2σ (outlier filter)
3. Computes a recency-weighted average — the most recent week gets weight `n`, the oldest gets weight `1`
4. Outputs predicted quantity ± 1 std dev as the confidence interval

The daily summary printed to the log shows:
- **All-day prep totals** — top 10 items ranked by predicted daily quantity
- **Peak period** — the 5-slot window around the busiest 15-min slot with per-item breakdown
- **Shift breakdown** — Morning / Lunch / Afternoon / Dinner totals with top items

**Backtested accuracy (May 13, 2026 — 8 weeks of history):**
- Mean Absolute Error: 0.67 units/slot
- 86% of slots within ±1 unit of actual
- 94% of slots within ±2 units of actual

Results are stored in `predictions_15min` and can be queried per location, product, or time slot.

---

## Key Design Decisions

**Partitioned tables** — `orders` and `order_items` are partitioned by month (`PARTITION BY RANGE (created_date)`). This keeps queries fast and allows easy archival of old data.

**Idempotent inserts** — every insert uses `ON CONFLICT DO NOTHING`, so re-running the pipeline for the same date is safe.

**Modifier name resolution** — Revel's `modifieritems` records contain only a URI (e.g. `/resources/Modifier/25002/`). `pipeline.py` fetches the full modifier list from `/resources/Modifier/` once per run, upserts into the `modifiers` reference table, and uses an in-memory cache to populate `modifier_name` on every row.

**Batched item fetching** — the `OrderItem` establishment filter is broken in Revel's API (silently returns all locations). Items are fetched using `order__in=id1,id2,...` in batches of 200, which is the only reliable way to get per-location items.

**Feature separation** — raw tables are append-only and never updated. Feature tables are computed nightly by `aggregate_features.py` and are the only tables the ML model reads.

---

## Ingestion Log

Every pipeline run is tracked in the `ingestion_log` table:

```sql
SELECT establishment_id, run_date, status, orders_inserted, items_inserted, completed_at
FROM ingestion_log
ORDER BY started_at DESC
LIMIT 20;
```

---

## Requirements

```
playwright
psycopg2-binary
python-dotenv
```

```bash
pip install playwright psycopg2-binary python-dotenv
playwright install chromium
```

PostgreSQL 15+
