# POS Analytics Pipeline

A production-grade data pipeline for **Laynes Chicken Fingers** that pulls daily order data from the Revel POS API across 11 locations, stores it in PostgreSQL, and builds pre-aggregated feature tables ready for ML demand prediction.

---

## Architecture

```
Revel POS API
     │
     ▼
pipeline.py          ← daily fetch + DB insert (runs 2:00 AM via cron)
     │
     ▼
PostgreSQL (4 layers)
  ├── Layer 0: Reference tables  (establishments, products, modifiers, dining_channels)
  ├── Layer 1: Raw ingestion     (orders, order_items, modifier_items)  ← append-only, partitioned by month
  ├── Layer 2: Feature tables    (features_hourly, features_product_daily, features_daily_summary)
  └── Layer 3: Prediction output (predictions_hourly_demand, predictions_prep_sheet, predictions_staffing)
     │
     ▼
aggregate_features.py  ← nightly aggregation job (runs 3:00 AM via cron)
```

---

## Scripts

| Script | Purpose |
|---|---|
| `pipeline.py` | Main daily pipeline — logs into Revel, fetches all orders + items + modifiers for each location, inserts into PostgreSQL |
| `aggregate_features.py` | Nightly aggregation — reads raw tables, populates feature tables for ML |
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
```

### 5. Backfill historical data

```bash
bash /opt/laynes/backfill.sh 2026-01-01 2026-04-30
```

Loops day by day, runs pipeline + aggregation for each date. Takes ~10–15 min per month of data.

---

## Cron Schedule

```
0 2 * * *   /opt/laynes/run.sh   # 2:00 AM — fetch from Revel + insert + aggregate
```

`run.sh` runs `pipeline.py` then `aggregate_features.py` and logs to `/var/log/laynes/run_YYYY-MM-DD.log`. Logs are retained for 30 days.

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
