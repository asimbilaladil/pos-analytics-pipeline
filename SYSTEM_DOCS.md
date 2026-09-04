# POS Analytics Pipeline — Complete System Documentation

**Business:** Laynes Chicken Fingers — 12 locations
**Generated:** 2026-08-10
**Scope:** Full pipeline, database schema, dashboards, deployment, and every SQL query / API endpoint currently in use.

---

## 1. What this system does

A daily ETL pipeline pulls the previous day's order data from the **Revel POS API** for each of 12 Laynes locations, stores it in **PostgreSQL**, computes pre-aggregated feature tables, and powers three Streamlit web pages:

1. **Tender Planning** (`dashboard.py`) — kitchen-facing prep dashboard. Predicts chicken tenders needed per 15-minute slot (Spicy vs Regular) using an **empirical same-weekday median**, not a trained ML model. This is the dashboard the owner actually uses daily.
2. **Sales Intelligence** (`sales_report.py`) — manager-facing weekly performance dashboard across all locations.
3. **Revel Data Export** (`pages/Revel_Data_Export.py`) — self-serve CSV export UI, a sub-page of the Tender Planning app.

A supplementary **weather correlation package** (`weather_analysis/`) pulls daily weather (Open-Meteo) per location and feeds an optional weather-adjusted view inside Tender Planning. A separate one-off tool (`export_raw.py`) exports raw Revel data to Parquet for an unrelated competitor investigation.

> **Note:** the project previously included a LightGBM ML prediction model. It was removed in July 2026 — nothing consumed its output. All forecasting today is the empirical median method in `dashboard.py`.

---

## 2. Architecture

```
Revel POS API (https://laynes.revelup.com)
     │  (Playwright-driven login + REST calls)
     ▼
pipeline.py              ← fetch yesterday's orders + items + modifiers, upsert into DB
     │
     ▼
aggregate_features.py    ← compute hourly/daily feature tables from raw data
     │
     ▼
weather_analysis.cli backfill   ← best-effort, fetch daily weather (Open-Meteo) per location
     │
     ▼
PostgreSQL (4 layers — see §4)
  ├─ Layer 0: Reference   (establishments, products, modifiers, dining_channels)
  ├─ Layer 1: Raw         (orders, order_items, modifier_items) — append-only, partitioned by month
  ├─ Layer 2: Features    (features_hourly, features_product_daily, features_daily_summary, weather_daily)
  └─ Layer 3/4: Scoring / ops (location_health_scores, ingestion_log)

Dashboards read from Postgres directly:
  • dashboard.py             (Tender Planning)      ← reads raw order_items/orders + weather_daily
  • sales_report.py          (Sales Intelligence)   ← reads features_daily_summary (Layer 2)
  • pages/Revel_Data_Export.py (CSV export)         ← reads orders / order_items directly
```

All three steps (`pipeline.py` → `aggregate_features.py` → `weather_analysis.cli backfill`) run sequentially every morning via `run.sh`, triggered by cron. The weather step is best-effort — its failure does not fail the run.

---

## 3. Scripts reference

| Script | Role | Scheduled? |
|---|---|---|
| `pipeline.py` | Main nightly ETL — logs into Revel, fetches orders/items/modifiers, upserts into Postgres | Yes — step 1 of `run.sh` |
| `aggregate_features.py` | Computes Layer 2 feature tables from raw data | Yes — step 2 of `run.sh` |
| `weather_analysis/` (`python -m weather_analysis.cli backfill`) | Fetches daily weather per location into `weather_daily` | Yes — step 3 of `run.sh`, best-effort |
| `run.sh` | Cron entry point — orchestrates the 3 steps above, logs to `/var/log/laynes/run_<date>.log`, prunes logs >30 days | Yes — `0 9 * * *` (see §7) |
| `ingest_to_db.py` | Manual/legacy — loads pre-downloaded JSON files into Postgres (no live Revel calls) | No |
| `order_fixed.py` | Manual/debug — fetches from Revel, saves JSON to disk | No |
| `backfill.sh` / `backfill3m.sh` / `backfill_local.sh` | Manual historical backfill runners (day-by-day loop over `pipeline.py`) | No |
| `seed_establishments.py` | One-off bootstrap — seeds `establishments` table from Revel | No (run once) |
| `export_raw.py` | Standalone Parquet exporter for the Nederland/Beaumont competitor investigation — never writes to Postgres | No |
| `dashboard.py` | Tender Planning Streamlit app | Long-running service (systemd) |
| `sales_report.py` | Sales Intelligence Streamlit app | Long-running service (systemd) |
| `pages/Revel_Data_Export.py` | CSV export Streamlit sub-page (auto-discovered by `dashboard.py`'s Streamlit process) | Runs inside the same service as `dashboard.py` |
| `database_design.sql` | Full schema DDL (run once on a fresh DB) | No |
| `setup.sh` | One-command Ubuntu server bootstrap — **stale**: doesn't install dashboards or weather package (see README) | No |

---

## 4. Database schema

PostgreSQL 15+. `orders` and `order_items` are **partitioned by month** on `created_date` (monthly partitions created through end of 2026). All inserts use `ON CONFLICT DO NOTHING` / `DO UPDATE` for idempotency — re-running a day's ingestion is always safe.

### Layer 0 — Reference / lookup tables

**`establishments`** — one row per Laynes location
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | Revel establishment ID |
| `name` | VARCHAR(100) | e.g. "Nederland" |
| `city` | VARCHAR(100) | |
| `state` | VARCHAR(10) | |
| `timezone` | VARCHAR(50) | default `US/Central` |
| `active` | BOOLEAN | default TRUE |
| `created_at` | TIMESTAMPTZ | default NOW() |

**`products`** — menu item master list
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | extracted from `/resources/Product/{id}/` |
| `revel_uri` | VARCHAR(100) UNIQUE | |
| `name` | VARCHAR(200) | |
| `product_class` | VARCHAR(100) | e.g. "1. Food", "2. Beverage" |
| `is_combo` | BOOLEAN | |
| `is_modifier` | BOOLEAN | |
| `base_price` | NUMERIC(10,2) | |
| `active` | BOOLEAN | |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

**`modifiers`** — modifier reference (~347 unique), refreshed nightly
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | extracted from `/resources/Modifier/{id}/` |
| `revel_uri` | VARCHAR(100) UNIQUE | |
| `name` | VARCHAR(200) | |
| `base_price` | NUMERIC(10,2) | default 0 |
| `active` | BOOLEAN | |
| `created_at` / `updated_at` | TIMESTAMPTZ | |

**`dining_channels`** — decoded `dining_option` codes (seeded, static)
| Column | Type | Notes |
|---|---|---|
| `id` | SMALLINT PK | Revel `dining_option` code |
| `name` | VARCHAR(50) | e.g. "Drive Through" |
| `channel_group` | VARCHAR(50) | `in_store` / `drive_through` / `delivery` / `catering` / `online` / `third_party` |
| `is_third_party` | BOOLEAN | TRUE for DoorDash / Uber Eats |

Seeded values: `0` To Go, `1` Eat In, `2` Delivery, `3` Catering, `4` Drive Through, `5` Online Ordering, `6` Spirit Night, `7` Shipping, `8` Pickup, `9` DoorDash Drive, `100` DD Marketplace, `101` Uber Eats, `102` Eat In Fun, `103` To Go Fun, `104` Drive Thru Fun, `105` Lane A, `106` Lane B.

### Layer 1 — Raw ingestion tables (append-only, partitioned by month on `created_date`)

**`orders`** — one row per Revel order (~5,400/day across all locations)
| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT | Revel order ID (part of composite PK) |
| `uuid` | UUID | |
| `establishment_id` | INTEGER FK → establishments | |
| `local_id` | VARCHAR(50) | |
| `created_date` | TIMESTAMPTZ NOT NULL | UTC; part of composite PK and partition key |
| `updated_date` | TIMESTAMPTZ | |
| `pickup_time` | TIMESTAMPTZ | catering/scheduled orders |
| `dining_option` | SMALLINT FK → dining_channels | |
| `pos_mode` | VARCHAR(10) | "Q" = quick service |
| `final_total` / `subtotal` / `tax` / `gratuity` / `discount_total_amount` | NUMERIC(10,2) | default 0 |
| `closed` / `is_unpaid` / `deleted` / `is_discounted` / `web_order` | BOOLEAN | |
| `customer_id` | INTEGER | nullable, extracted from `/resources/Customer/{id}/` |
| `number_of_people` | SMALLINT | |
| `ingested_at` | TIMESTAMPTZ | when this row was pulled |
| `ingestion_date` | DATE NOT NULL | which daily run pulled it |

PK: `(id, created_date)`. Indexes: `(establishment_id, created_date)`, `(dining_option, created_date)`, `(customer_id)` partial, `(closed, is_unpaid, created_date)`.

**`order_items`** — one row per item line (~4.5× orders row count)
| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT | part of composite PK |
| `uuid` | UUID | |
| `order_id` | BIGINT | |
| `establishment_id` | INTEGER FK → establishments | |
| `product_id` | INTEGER FK → products | |
| `product_name` | VARCHAR(200) | Revel's `product_name_override` |
| `quantity` | NUMERIC(8,2) | default 1 |
| `dining_option` | SMALLINT FK → dining_channels | |
| `combo_product_id` | INTEGER FK → products | which combo this item belongs to |
| `combo_uuid` | UUID | groups items in the same combo |
| `price` | NUMERIC(10,2) | unit price |
| `pure_sales` | NUMERIC(10,2) | revenue excl. tax |
| `tax_amount` / `modifier_amount` | NUMERIC(10,2) | |
| `is_discounted` | BOOLEAN | |
| `created_date` | TIMESTAMPTZ NOT NULL | when item was ordered; PK + partition key |
| `start_time` | TIMESTAMPTZ | when KDS received it |
| `kitchen_completed` | TIMESTAMPTZ | when KDS was bumped |
| `kitchen_seconds` | INTEGER, **GENERATED ALWAYS AS** `EXTRACT(EPOCH FROM (kitchen_completed - start_time))` | STORED |
| `is_voided` | BOOLEAN | |
| `voided_date` | TIMESTAMPTZ | |
| `voided_by_user_id` | INTEGER | extracted from `/enterprise/User/{id}/` |
| `voided_reason` | VARCHAR(500) | |
| `deleted` / `deleted_date` | BOOLEAN / TIMESTAMPTZ | |
| `ingested_at` / `ingestion_date` | TIMESTAMPTZ / DATE | |

PK: `(id, created_date)`. Indexes: `(order_id, created_date)`, `(establishment_id, created_date)`, `(product_id, created_date)`, kitchen-speed partial index, voided partial index, combo partial index.

**`modifier_items`** — sauces/customizations per order item (~1,050/day)
| Column | Type | Notes |
|---|---|---|
| `id` | BIGINT PK | |
| `order_item_id` / `order_id` | BIGINT | |
| `establishment_id` | INTEGER FK → establishments | |
| `modifier_id` | INTEGER FK → modifiers | |
| `modifier_name` | VARCHAR(200) | denormalized for query convenience |
| `qty` | NUMERIC(8,2) | default 1 |
| `modifier_price` | NUMERIC(10,2) | |
| `is_discounted` | BOOLEAN | |
| `created_date` | DATE NOT NULL | date only, inherited from parent order |
| `ingested_at` | TIMESTAMPTZ | |

Indexes: `(order_item_id)`, `(establishment_id, created_date)`, `(modifier_id, created_date)`.

### Layer 2 — Feature tables (computed nightly by `aggregate_features.py`)

**`features_hourly`** — one row per location per hour per day (~96K rows/yr)
| Column | Type |
|---|---|
| `id` | BIGSERIAL PK |
| `establishment_id` | INTEGER FK |
| `date` | DATE |
| `hour` | SMALLINT (0–23) |
| `day_of_week` | SMALLINT (0=Mon…6=Sun) |
| `week_of_year` / `month` | SMALLINT |
| `is_weekend` | BOOLEAN |
| `order_count` / `item_count` / `unique_customers` | INTEGER |
| `total_revenue` / `avg_order_value` | NUMERIC |
| `avg_items_per_order` | NUMERIC(6,2) |
| `orders_drive_through` / `orders_eat_in` / `orders_to_go` / `orders_doordash` / `orders_ubereats` / `orders_online` / `orders_lane_a` / `orders_lane_b` | INTEGER |
| `avg_kitchen_seconds` | NUMERIC(8,2) |
| `void_count` / `void_rate` | INTEGER / NUMERIC(5,4) |
| `computed_at` | TIMESTAMPTZ |

UNIQUE `(establishment_id, date, hour)`.

**`features_product_daily`** — one row per product per location per day (~480K rows/yr)
| Column | Type |
|---|---|
| `id` | BIGSERIAL PK |
| `establishment_id` / `product_id` | INTEGER FK |
| `product_name` | VARCHAR(200) |
| `date`, `day_of_week`, `week_of_year`, `month`, `is_weekend` | as above |
| `quantity_sold` | NUMERIC(10,2) |
| `order_count` | INTEGER |
| `revenue` | NUMERIC(12,2) |
| `qty_drive_through` / `qty_eat_in` / `qty_third_party` | NUMERIC(10,2) |
| `avg_kitchen_seconds` / `max_kitchen_seconds` / `min_kitchen_seconds` | NUMERIC / INTEGER |
| `kitchen_outliers` | INTEGER (items > 3× that product's own-day avg kitchen time) |
| `combo_attach_rate` / `is_combo_item` | NUMERIC(5,4) / BOOLEAN *(defined in schema, not currently populated by `aggregate_features.py`)* |
| `void_count` / `void_rate` | INTEGER / NUMERIC(5,4) |
| `computed_at` | TIMESTAMPTZ |

UNIQUE `(establishment_id, product_id, date)`.

**`features_daily_summary`** — one row per location per day. **This is what `sales_report.py` reads exclusively.**
| Column | Type |
|---|---|
| `id` | BIGSERIAL PK |
| `establishment_id` | INTEGER FK |
| `date`, `day_of_week`, `week_of_year`, `month`, `is_weekend` | as above |
| `total_orders` / `total_items` | INTEGER |
| `total_revenue` / `avg_order_value` | NUMERIC |
| `avg_items_per_order` | NUMERIC(6,2) |
| `pct_drive_through` / `pct_third_party` / `pct_in_store` | NUMERIC(5,4) |
| `revenue_drive_through` / `revenue_third_party` / `revenue_in_store` | NUMERIC(12,2) |
| `avg_kitchen_seconds` | NUMERIC(8,2) |
| `pct_orders_over_10min` | NUMERIC(5,4) (any item in the order took >600s) |
| `total_voids` / `void_rate` | INTEGER / NUMERIC(5,4) |
| `total_discounts` / `discount_rate` | NUMERIC(12,2) / NUMERIC(5,4) |
| `peak_hour` / `peak_hour_orders` | SMALLINT / INTEGER |
| `computed_at` | TIMESTAMPTZ |

UNIQUE `(establishment_id, date)`.

**`weather_daily`** — one row per location per day, from Open-Meteo (populated by `weather_analysis` package; also has its own `CREATE TABLE IF NOT EXISTS` in `weather_analysis/repositories.py` as a second authoritative source of this DDL)
| Column | Type |
|---|---|
| `establishment_id` | INTEGER FK |
| `observed_on` | DATE |
| `temp_max_c` / `temp_min_c` / `temp_mean_c` | NUMERIC(5,2) |
| `precipitation_mm` / `rain_mm` | NUMERIC(6,2) |
| `precipitation_hours` | NUMERIC(5,2) |
| `wind_max_kmh` | NUMERIC(6,2) |
| `weather_code` | SMALLINT |

PK `(establishment_id, observed_on)`.

### Layer 3 — Scoring output

**`location_health_scores`** — daily composite score per location (defined in schema; surfaced via `v_network_today` view, not currently populated by any script in the active pipeline)
| Column | Type |
|---|---|
| `id` | BIGSERIAL PK |
| `establishment_id` | INTEGER FK |
| `score_date` | DATE |
| `overall_score` / `revenue_score` / `kitchen_score` / `void_score` / `volume_score` | NUMERIC(5,2), 0–100 |
| `network_rank` | SMALLINT |
| `flags` | JSONB, e.g. `{"slow_kitchen": true}` |
| `generated_at` | TIMESTAMPTZ |

UNIQUE `(establishment_id, score_date)`.

### Layer 4 — Operational / audit

**`ingestion_log`** — one row per establishment per pipeline run
| Column | Type |
|---|---|
| `id` | BIGSERIAL PK |
| `establishment_id` | INTEGER FK |
| `run_date` | DATE |
| `started_at` / `completed_at` | TIMESTAMPTZ |
| `status` | VARCHAR(20) — `running` / `success` / `failed` / `partial` |
| `orders_fetched` / `items_fetched` / `orders_inserted` / `items_inserted` / `orders_skipped` | INTEGER |
| `error_message` | TEXT |
| `script_version` | VARCHAR(50) |

### Views

- **`v_network_today`** — joins `features_daily_summary` + `establishments` + `location_health_scores` for `CURRENT_DATE - 1`, ordered by health score.
- **`v_product_velocity_30d`** — 30-day product velocity per location with 7d-vs-prev-7d trend, from `features_product_daily` + `products` + `establishments`.

---

## 5. SQL queries by script

### `pipeline.py` (nightly ingestion)

Product upsert:
```sql
INSERT INTO products (id, revel_uri, name, product_class, is_combo, is_modifier, base_price, active)
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name, product_class = EXCLUDED.product_class,
    is_combo = EXCLUDED.is_combo, is_modifier = EXCLUDED.is_modifier,
    base_price = EXCLUDED.base_price, active = EXCLUDED.active, updated_at = NOW()
```

Modifier upsert:
```sql
INSERT INTO modifiers (id, revel_uri, name, base_price, active)
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name, base_price = EXCLUDED.base_price,
    active = EXCLUDED.active, updated_at = NOW()
```

Generic bulk upsert (used for `orders`, `order_items`, `modifier_items`):
```sql
INSERT INTO {table} ({columns}) VALUES %s ON CONFLICT DO NOTHING
```

Ingestion log lifecycle:
```sql
INSERT INTO ingestion_log (establishment_id, run_date, started_at, status, script_version)
VALUES (%s, %s, %s, 'running', %s) RETURNING id;

UPDATE ingestion_log SET completed_at=%s, status='success',
    orders_fetched=%s, items_fetched=%s, orders_inserted=%s, items_inserted=%s
WHERE id=%s;

UPDATE ingestion_log SET completed_at=%s, status='failed', error_message=%s WHERE id=%s;
```

Modifier cache load (used with `--skip-reference`):
```sql
SELECT id, name FROM modifiers
```

### `aggregate_features.py` (nightly feature computation)

Three large upsert statements, one per feature table — all windowed on `%(day_start)s` / `%(day_end)s` (America/Chicago business-day boundaries converted to UTC):

- **`features_hourly`**: aggregates `orders` LEFT JOIN `order_items` (bounded ±1 day, see §8 performance note) grouped by establishment/date/hour, with `FILTER` clauses splitting order counts by `dining_option` code, plus `AVG(kitchen_seconds)` and void rate. `ON CONFLICT (establishment_id, date, hour) DO UPDATE`.
- **`features_product_daily`**: aggregates `order_items JOIN orders` grouped by establishment/product/date, includes a correlated subquery computing `kitchen_outliers` (items where `kitchen_seconds > 3× that product-establishment-day's own average`). `ON CONFLICT (establishment_id, product_id, date) DO UPDATE`.
- **`features_daily_summary`**: three CTEs (`order_stats` — per-hour order rollup incl. peak-hour detection via `ARRAY_AGG ... ORDER BY hour_order_count DESC`, `item_stats` — item/kitchen/void rollup, `agg` — combines them) producing one row per establishment/date. `ON CONFLICT (establishment_id, date) DO UPDATE`.

*(Full verbatim SQL for all three is preserved in the script itself — reproduced here only in summary since each is 40–70 lines; see `aggregate_features.py` for exact text.)*

### `dashboard.py` (Tender Planning)

```sql
-- load_locations
SELECT id, name, city FROM establishments ORDER BY name;

-- load_actuals (one specific date+location, all products+flavor mods, bucketed into 15-min slots)
SELECT
    oi.product_id, MAX(oi.product_name) AS product_name, mf.flavor_mods,
    (EXTRACT(HOUR FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
     + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15))::SMALLINT AS slot_index,
    SUM(oi.quantity)::float AS actual_qty
FROM order_items oi
JOIN orders o ON o.id = oi.order_id
    AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
    AND o.closed = TRUE AND o.deleted = FALSE
LEFT JOIN LATERAL (
    SELECT string_agg(mi.modifier_name, ' | ') AS flavor_mods
    FROM modifier_items mi
    WHERE mi.order_item_id = oi.id
      AND ( lower(btrim(mi.modifier_name)) IN (
              'spicy','regular','crispy','extra crispy','chicken spicy','chicken crispy',
              'chicken grilled','chicken griiled','chicken spicy crispy','wrap spicy',
              'wrap crispy','kick it up spicy','kick it up regular')
            OR mi.modifier_name ~* '^\*+\s*\d+\s*(spicy|regular)' )
) mf ON true
WHERE DATE(oi.created_date AT TIME ZONE 'America/Chicago') = %s
  AND oi.establishment_id = %s
  AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
GROUP BY oi.product_id, mf.flavor_mods, slot_index;

-- load_dow_history (all history for one location+weekday — feeds the median forecast)
SELECT
    DATE(oi.created_date AT TIME ZONE 'America/Chicago') AS sale_date,
    (EXTRACT(HOUR FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
     + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15))::int AS slot_index,
    oi.product_name, mf.flavor_mods, SUM(oi.quantity)::float AS qty
FROM order_items oi
JOIN orders o ON o.id = oi.order_id
    AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
    AND o.closed = TRUE AND o.deleted = FALSE
[same flavor LEFT JOIN LATERAL as above]
WHERE oi.establishment_id = %s
  AND EXTRACT(DOW FROM oi.created_date AT TIME ZONE 'America/Chicago')::int = %s
  AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
GROUP BY sale_date, slot_index, oi.product_name, mf.flavor_mods;

-- load_order_date_range
SELECT MIN(DATE(created_date AT TIME ZONE 'America/Chicago')),
       MAX(DATE(created_date AT TIME ZONE 'America/Chicago'))
FROM orders WHERE closed = TRUE AND deleted = FALSE;

-- load_recent_daily (feeds the 7-day predicted-vs-actual scorecard)
SELECT
    DATE(oi.created_date AT TIME ZONE 'America/Chicago') AS sale_date,
    EXTRACT(DOW FROM oi.created_date AT TIME ZONE 'America/Chicago')::int AS dow,
    oi.product_name, mf.flavor_mods, SUM(oi.quantity)::float AS qty
FROM order_items oi
JOIN orders o ON o.id = oi.order_id
    AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
    AND o.closed = TRUE AND o.deleted = FALSE
[same flavor LEFT JOIN LATERAL]
WHERE oi.establishment_id = %s
  AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
GROUP BY sale_date, dow, oi.product_name, mf.flavor_mods;

-- load_weather_daily
SELECT observed_on, temp_max_c * 9.0/5.0 + 32 AS temp_max_f,
       temp_mean_c * 9.0/5.0 + 32 AS temp_mean_f, rain_mm, precipitation_hours
FROM weather_daily WHERE establishment_id = %s;

-- load_target_weather
SELECT temp_max_c * 9.0/5.0 + 32, rain_mm
FROM weather_daily WHERE establishment_id = %s AND observed_on = %s;
```

If the target date isn't yet in `weather_daily` (recent/future dates), falls back to the Open-Meteo **forecast** API (see §6).

### `sales_report.py` (Sales Intelligence)

```sql
-- load_available_weeks
SELECT DISTINCT DATE_TRUNC('week', date + 1)::date - 1 AS week_mon
FROM features_daily_summary ORDER BY week_mon DESC LIMIT 12;

-- load_week_summary (current week vs prior week, per location)
WITH cur AS (
    SELECT establishment_id, SUM(total_revenue) AS revenue, SUM(total_orders) AS orders,
        AVG(pct_drive_through) AS dt_pct, AVG(pct_third_party) AS tp_pct, AVG(pct_in_store) AS in_pct,
        SUM(total_discounts) AS discounts, COUNT(date) AS active_days
    FROM features_daily_summary WHERE date >= %s AND date < %s GROUP BY establishment_id
), prev AS (
    SELECT establishment_id, SUM(total_revenue) AS revenue, SUM(total_orders) AS orders
    FROM features_daily_summary WHERE date >= %s AND date < %s GROUP BY establishment_id
)
SELECT e.id, e.name, cur.revenue, cur.orders,
    CASE WHEN cur.orders > 0 THEN cur.revenue / cur.orders ELSE 0 END AS aov,
    cur.dt_pct, cur.tp_pct, cur.in_pct, cur.discounts, cur.active_days,
    COALESCE(prev.revenue, 0) AS prev_revenue, COALESCE(prev.orders, 0) AS prev_orders
FROM cur JOIN establishments e ON e.id = cur.establishment_id
LEFT JOIN prev ON prev.establishment_id = cur.establishment_id
ORDER BY cur.revenue DESC NULLS LAST;

-- load_trend (N-week trend per location)
SELECT e.name, (DATE_TRUNC('week', fds.date + 1) - INTERVAL '1 day')::date AS week_start,
    SUM(fds.total_revenue) AS revenue, SUM(fds.total_orders) AS orders
FROM features_daily_summary fds JOIN establishments e ON e.id = fds.establishment_id
WHERE fds.date >= CURRENT_DATE - (%s * 7)
GROUP BY e.name, week_start ORDER BY week_start, revenue DESC;

-- load_daily
SELECT e.name, fds.date, fds.day_of_week, fds.total_revenue, fds.total_orders
FROM features_daily_summary fds JOIN establishments e ON e.id = fds.establishment_id
WHERE fds.date >= %s AND fds.date < %s ORDER BY fds.date, fds.total_revenue DESC;

-- load_dow_heatmap (all-time)
SELECT e.name, fds.day_of_week, AVG(fds.total_revenue) AS avg_revenue, AVG(fds.total_orders) AS avg_orders
FROM features_daily_summary fds JOIN establishments e ON e.id = fds.establishment_id
GROUP BY e.name, fds.day_of_week ORDER BY e.name, fds.day_of_week;
```

> **Known discrepancy:** `load_trend`'s week bucketing is Monday-anchored (`DATE_TRUNC('week', ...)`), while the business actually runs a Thursday→Wednesday week. Not yet reconciled — flagged here, not fixed.

### `pages/Revel_Data_Export.py` (CSV export page)

Two dataset templates, each with a `COUNT(*)` variant and a full `SELECT ... ORDER BY` export variant:

```sql
-- orders dataset
SELECT o.id AS order_id, o.uuid AS order_uuid, o.local_id, o.establishment_id,
    e.name AS establishment_name,
    o.created_date AT TIME ZONE 'America/Chicago' AS created_date,
    o.updated_date AT TIME ZONE 'America/Chicago' AS updated_date,
    o.pickup_time  AT TIME ZONE 'America/Chicago' AS pickup_time,
    o.dining_option, dc.name AS dining_channel_name, dc.channel_group, dc.is_third_party,
    o.pos_mode, o.subtotal, o.discount_total_amount, o.tax, o.gratuity, o.final_total,
    o.closed, o.is_unpaid, o.deleted, o.is_discounted, o.web_order, o.customer_id,
    o.number_of_people, o.ingested_at AT TIME ZONE 'America/Chicago', o.ingestion_date
FROM orders o
LEFT JOIN establishments e ON e.id = o.establishment_id
LEFT JOIN dining_channels dc ON dc.id = o.dining_option
WHERE o.created_date >= %(start_ts)s AND o.created_date < %(end_ts)s
  [AND o.establishment_id = %(establishment_id)s]
ORDER BY o.created_date;

-- order_items dataset (no join to orders — order_items already carries establishment_id/dining_option)
SELECT oi.id AS order_item_id, oi.uuid AS order_item_uuid, oi.order_id, oi.establishment_id,
    e.name AS establishment_name, oi.product_id, oi.product_name, p.product_class,
    oi.quantity, oi.dining_option, oi.combo_product_id, oi.combo_uuid, oi.price, oi.pure_sales,
    oi.tax_amount, oi.modifier_amount, oi.is_discounted,
    oi.created_date AT TIME ZONE 'America/Chicago', oi.start_time AT TIME ZONE 'America/Chicago',
    oi.kitchen_completed AT TIME ZONE 'America/Chicago', oi.kitchen_seconds,
    oi.is_voided, oi.voided_date AT TIME ZONE 'America/Chicago', oi.voided_by_user_id, oi.voided_reason,
    oi.deleted, oi.deleted_date AT TIME ZONE 'America/Chicago', p.is_combo, p.is_modifier,
    oi.ingested_at AT TIME ZONE 'America/Chicago', oi.ingestion_date
FROM order_items oi
LEFT JOIN establishments e ON e.id = oi.establishment_id
LEFT JOIN products p ON p.id = oi.product_id
WHERE oi.created_date >= %(start_ts)s AND oi.created_date < %(end_ts)s
  [AND oi.establishment_id = %(establishment_id)s]
ORDER BY oi.created_date;
```

### `weather_analysis/` package

```sql
-- list_active (establishments with weather coordinates)
SELECT id, name, city FROM establishments WHERE active = TRUE AND city IS NOT NULL ORDER BY id;

-- ensure_schema (idempotent create, second authoritative source of this DDL besides database_design.sql)
CREATE TABLE IF NOT EXISTS weather_daily ( ... same columns as §4 ... );

-- upsert
INSERT INTO weather_daily (establishment_id, observed_on, temp_max_c, temp_min_c, temp_mean_c,
    precipitation_mm, rain_mm, precipitation_hours, wind_max_kmh, weather_code) VALUES %s
ON CONFLICT (establishment_id, observed_on) DO UPDATE SET ... ;

-- list_all
SELECT establishment_id, observed_on, temp_max_c, temp_min_c, temp_mean_c,
       precipitation_mm, rain_mm, precipitation_hours, wind_max_kmh, weather_code
FROM weather_daily;

-- daily_order_counts
SELECT establishment_id, DATE(created_date AT TIME ZONE 'America/Chicago') AS business_date,
       COUNT(*) AS order_count
FROM orders
WHERE closed = TRUE AND deleted = FALSE
  AND DATE(created_date AT TIME ZONE 'America/Chicago') BETWEEN %s AND %s
GROUP BY establishment_id, business_date ORDER BY establishment_id, business_date;

-- _fetch_line_quantities (tender demand for backtest)
SELECT oi.establishment_id, DATE(oi.created_date AT TIME ZONE 'America/Chicago') AS business_date,
       oi.product_name, SUM(oi.quantity)::int AS qty
FROM order_items oi
JOIN orders o ON o.id = oi.order_id
    AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
    AND o.closed = TRUE AND o.deleted = FALSE
WHERE oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_name IS NOT NULL
  AND DATE(oi.created_date AT TIME ZONE 'America/Chicago') BETWEEN %s AND %s
GROUP BY oi.establishment_id, business_date, oi.product_name;
```

### `export_raw.py` (Nederland/Beaumont competitor export tool)

Read-only, single query (all other data goes straight to Parquet, never to Postgres):
```sql
SELECT id, product_class FROM products;
```

### `ingest_to_db.py` / `seed_establishments.py`

Same upsert patterns as `pipeline.py` for `orders`/`order_items`/`modifier_items`/`ingestion_log`; `seed_establishments.py` additionally upserts `establishments`:
```sql
INSERT INTO establishments (id, name, city, state, timezone, active)
VALUES (%s, %s, %s, %s, %s, TRUE)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name, city = EXCLUDED.city, state = EXCLUDED.state, timezone = EXCLUDED.timezone;
```

---

## 6. External APIs

**Revel POS API** — base `https://laynes.revelup.com`. All access via a Playwright-driven headless Chromium session (login form automation, session cached to `/tmp/revel_session.json`) — not plain REST, since Revel requires an authenticated browser session.

| Endpoint | Used by | Notes |
|---|---|---|
| `GET /resources/Order/` | `pipeline.py`, `export_raw.py` | Filters: `establishment=`, `created_date__gte`/`__lte`. Paginated (`limit`/`offset`, check `meta.total_count`). |
| `GET /resources/OrderItem/` | `pipeline.py`, `export_raw.py` | `establishment=` filter is **broken** (silently returns all locations) — always scoped via `order__in=id1,id2,...` in batches of 200. |
| `GET /resources/Payment/` | `export_raw.py` | Also scoped via `order__in=`. |
| `GET /resources/Product/` | `pipeline.py` | Refreshed every pipeline run. |
| `GET /resources/Modifier/` | `pipeline.py`, `export_raw.py` | ~347 unique modifiers; refreshed nightly. |
| `GET /resources/Establishment/{id}/` | `seed_establishments.py` | One-off bootstrap. |

**Open-Meteo** (weather, no auth required):

| Endpoint | Used by | Notes |
|---|---|---|
| `GET https://archive-api.open-meteo.com/v1/archive` | `weather_analysis.weather_provider.OpenMeteoArchiveProvider` | Historical daily weather; params `latitude`, `longitude`, `start_date`, `end_date`, `daily=temperature_2m_max,temperature_2m_min,temperature_2m_mean,precipitation_sum,rain_sum,precipitation_hours,wind_speed_10m_max,weather_code`, `timezone=America/Chicago`. Has ~5-day reporting lag. |
| `GET https://api.open-meteo.com/v1/forecast` | `dashboard.py` (`load_target_weather` fallback) | Used when the target date isn't yet in the archive (recent/future dates); params `daily=temperature_2m_max,rain_sum`. |

Coordinates for both come from a hardcoded catalogue (`weather_analysis/config.py: CoordinateCatalogue`) since `establishments` has no lat/long columns.

---

## 7. Deployment & scheduling

**Cron** (root's crontab, `TZ=America/Chicago`):
```
0 9 * * * /root/pos-analytics-pipeline/run.sh >> /var/log/laynes/cron.log 2>&1
```
`run.sh` runs `pipeline.py` → `aggregate_features.py` (both hard-stop on failure) → `weather_analysis.cli backfill` (best-effort, logs WARN on failure but doesn't fail the run). Logs to `/var/log/laynes/run_<date>.log`, pruned after 30 days.

**Systemd services** (long-running dashboards, not timers):

| Service | Script | Port | Public URL | Logs |
|---|---|---|---|---|
| `laynes-dashboard.service` | `dashboard.py` (+ `pages/Revel_Data_Export.py` as a sub-page) | 8502 | `https://hr.aygfoods.com/daily-sales-prediction` | `/var/log/laynes-dashboard.log` |
| `laynes-sales-report.service` | `sales_report.py` | 8503 | `https://hr.aygfoods.com/sales-report` | `/var/log/laynes-sales-report.log` |

Both: `Restart=always`, `RestartSec=5`, `WantedBy=multi-user.target`. Nginx proxies each path to its port with WebSocket upgrade headers (required by Streamlit) in `/etc/nginx/sites-available/ayg-hr`.

The Revel Data Export page lives at `https://hr.aygfoods.com/daily-sales-prediction/Revel_Data_Export` — it's a Streamlit multipage sub-route of the same process as the Tender Planning dashboard, not a separate deployment. **Note:** Streamlit 1.58 builds its page registry at process start, so adding/changing files under `pages/` requires an `systemctl restart laynes-dashboard` to take effect.

Restart commands:
```bash
systemctl restart laynes-dashboard
systemctl restart laynes-sales-report
journalctl -u laynes-dashboard -f      # tail live logs
```

---

## 8. Key design decisions & known issues

- **Idempotent inserts** — every insert uses `ON CONFLICT`, so re-running a day's pipeline is always safe.
- **Modifier name resolution** — Revel's `modifieritems` only carry a URI; `pipeline.py` refreshes the full `modifiers` table each run and resolves names via an in-memory cache.
- **Batched item fetching** — `OrderItem`'s `establishment=` filter is broken in Revel's API, so items are always fetched via `order__in=` batching.
- **`order_items` ↔ `orders` join bound to ±1 day** — both tables are partitioned by their own `created_date`, and the two can differ by a few seconds for the same logical order. Every join in `dashboard.py`/`weather_analysis` bounds the join window to `BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'` so Postgres can still prune partitions (~2 instead of all 12) — a performance fix that is a correctness no-op since the real gap is only seconds.
- **Postgres JIT is disabled** (`ALTER DATABASE laynes SET jit = off;`) — JIT compilation overhead dominated query time for this dashboard's pattern of many small/medium repeated queries (~12× slower with JIT on in one measured case). Database-wide, performance-only, no correctness impact.
- **Tender Planning forecast is a full-history median, not a rolling window** — a slot that's been historically empty can take ~4–5 months of consistent new activity before its median turns positive and it reappears in the prep table. Not a bug; a known tradeoff of the current method.
- **Weather is a supplementary signal only** — backtesting showed it's often no better than the plain median for daily tender demand; it's surfaced as an exploration/drilldown feature, not folded into the headline prediction.
- **`sales_report.py`'s week trend is Monday-anchored**, while the business's actual operating week is Thursday→Wednesday — not yet reconciled.
- **`location_health_scores` and `combo_attach_rate`/`is_combo_item` on `features_product_daily`** are defined in the schema but not currently populated by any script in the active pipeline.
- **`setup.sh` is stale** — it only installs the core pipeline (not the dashboards or `weather_analysis` dependencies) and predates the current schema notes in the README.

---

## 9. Environment variables (`.env`)

```
REVEL_USER=<revel login email>
REVEL_PASS=<revel login password>
ESTABLISHMENTS=32,14,48,7,6,25,36,26,20,40,15,54
DB_HOST=localhost
DB_PORT=5432
DB_NAME=laynes
DB_USER=laynes_user       # pipeline scripts default to "postgres" if unset; dashboards default to "laynes_user"
DB_PASS=<db password>
```

---

*End of document.*
