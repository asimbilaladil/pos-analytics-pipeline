# POS Analytics Pipeline

A production-grade data pipeline for **Laynes Chicken Fingers** that pulls daily order data from the Revel POS API across 12 locations, stores it in PostgreSQL, builds pre-aggregated feature tables, and powers two Streamlit dashboards: a kitchen-facing **Tender Planning** prep dashboard and a manager-facing **Sales Intelligence** dashboard. Daily weather per location (Open-Meteo) is layered in as an optional signal for the Tender Planning forecast.

> **Note (2026-07):** this project previously included a LightGBM 15-minute prediction model (`predict_daily_ml.py`) and a weighted-average baseline (`predict_daily.py`). Both were removed — nothing consumed their output anymore. The Tender Planning dashboard computes its own forecasts directly from same-weekday order history (see that section below). The old prediction output tables (`predictions_15min`, etc.) are no longer in the schema; if a database still has them from before, they can be dropped safely.

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
PostgreSQL (3 layers)
  ├── Layer 0: Reference tables  (establishments, products, modifiers, dining_channels)
  ├── Layer 1: Raw ingestion     (orders, order_items, modifier_items)  ← append-only, partitioned by month
  └── Layer 2: Feature tables    (features_hourly, features_product_daily, features_daily_summary, weather_daily)

weather_analysis/          ← fetches daily weather per location (Open-Meteo) into weather_daily
     (run nightly by run.sh, best-effort)

Both pipeline scripts run sequentially each morning via run.sh (cron), followed by the weather refresh.

Dashboards read from Postgres directly:
  • dashboard.py     (Tender Planning)     ← reads raw order_items for same-weekday medians + weather_daily
  • sales_report.py  (Sales Intelligence)  ← reads features_daily_summary (Layer 2)
```

---

## Scripts

| Script | Purpose |
|---|---|
| `pipeline.py` | Main daily pipeline — logs into Revel, fetches all orders + items + modifiers for each location, inserts into PostgreSQL |
| `aggregate_features.py` | Nightly aggregation — reads raw tables, populates the Layer 2 feature tables (feeds the Sales Intelligence dashboard) |
| `run.sh` | Daily cron wrapper — runs `pipeline.py` → `aggregate_features.py` → weather refresh (`weather_analysis.cli backfill`, best-effort/non-fatal), logs to `/var/log/laynes/run_YYYY-MM-DD.log`, rotates logs after 30 days |
| `backfill.sh` | Simple date-range backfill — loops day by day, runs pipeline + aggregation for each date |
| `backfill3m.sh` | Smart 3-month backfill — auto-resume (skips completed dates), rate limiting, failure tracking, `--status` mode |
| `ingest_to_db.py` | Offline ingestion — reads JSON files produced by `order_fixed.py` and inserts into DB |
| `order_fixed.py` | Manual/debug tool — fetches from Revel and saves JSON files to disk |
| `database_design.sql` | Full PostgreSQL schema (run once on a fresh DB) |
| `setup.sh` | One-command server setup for Ubuntu 22.04 |
| `seed_establishments.py` | Seeds the `establishments` table from Revel API (run once after schema creation) |
| `backfill_local.sh` | Local backfill runner — uses project `venv/` instead of `/opt/laynes`; passes `--skip-reference` for fast runs |
| `dashboard.py` | **Tender Planning** — Streamlit kitchen prep dashboard (see dedicated section below). Deployed live via systemd. |
| `sales_report.py` | **Sales Intelligence** — Streamlit network-wide weekly performance dashboard (see dedicated section below). Deployed live via systemd. |
| `weather_analysis/` | Python package that fetches daily weather per location from the Open-Meteo archive/forecast API into the `weather_daily` table. Layered architecture (repositories/providers/services). CLI: `python -m weather_analysis.cli backfill [--start --end]`. Run nightly by `run.sh`; feeds the Tender Planning weather features. |

> **Note on `setup.sh`:** it copies `pipeline.py`, `aggregate_features.py`, `ingest_to_db.py`, and `database_design.sql` to `/opt/laynes`, installs `playwright`/`psycopg2-binary`/`python-dotenv`, and generates a cron `run.sh` that runs `pipeline.py` → `aggregate_features.py`. That matches the current nightly pipeline. It does **not** install or deploy the two Streamlit dashboards (`dashboard.py`, `sales_report.py`) or their `streamlit`/`pandas`/`plotly` dependencies — deploy those manually (see the dashboard sections). The repo-root `run.sh` currently has `APP_DIR` hardcoded to this dev box's checkout path rather than `/opt/laynes` — treat it as this environment's local runner, not a portable script.

---

## Database Schema

### Layer 0 — Reference Tables
- **`establishments`** — 12 Laynes locations with timezone info (seed with `seed_establishments.py`)

| Revel ID | Name |
|---|---|
| 32 | LCF Airtex |
| 14 | LCF Beaumont |
| 48 | LCF Downtown Houston |
| 7 | LCF Ella |
| 6 | LCF Katy |
| 25 | LCF Mission Bend |
| 36 | LCF Missouri City |
| 26 | LCF Nederland |
| 20 | LCF Pasadena |
| 40 | LCF Rosenberg |
| 15 | LCF Shepherd |
| 54 | LCF Cypress *(opened Jun 2026)* |
- **`products`** — menu item master list from `/resources/Product/`
- **`modifiers`** — modifier reference table from `/resources/Modifier/` (~347 unique modifiers), refreshed nightly
- **`dining_channels`** — dining option codes (Drive Through, DoorDash, Uber Eats, etc.)

### Layer 1 — Raw Ingestion (partitioned by month)
- **`orders`** — one row per Revel order (~5,400/day across all locations)
- **`order_items`** — one row per item line (~4,600/day), includes kitchen timing (`start_time`, `kitchen_completed`, `kitchen_seconds`)
- **`modifier_items`** — sauces and customizations (~1,050/day)

### Layer 2 — Feature Tables (pre-aggregated summaries)
- **`features_hourly`** — order count, revenue, channel breakdown, avg kitchen time per location per hour
- **`features_product_daily`** — quantity sold, revenue, kitchen performance, void rate per product per location per day
- **`features_daily_summary`** — daily rollup per location including peak hour, channel mix, void rate (read by the Sales Intelligence dashboard)
- **`weather_daily`** — one row per location per day (temp high/low/mean, rain, precipitation hours, wind, weather code) from Open-Meteo. Populated by the `weather_analysis` package; feeds the Tender Planning weather features. PK `(establishment_id, observed_on)`.

### Layer 3 — Scoring Output
- **`location_health_scores`** — composite daily score per location (0–100), surfaced by the `v_network_today` view

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

**Local / dev setup** (without `setup.sh`):

```bash
# Create venv and install dependencies
python3 -m venv venv
venv/bin/pip install psycopg2-binary python-dotenv playwright streamlit pandas plotly
venv/bin/playwright install chromium

# Create DB and apply schema
sudo -u postgres psql -c "CREATE USER laynes_user WITH PASSWORD 'yourpass';"
sudo -u postgres psql -c "CREATE DATABASE laynes OWNER laynes_user;"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE laynes TO laynes_user;"
sudo -u postgres psql -d laynes -f database_design.sql

# Seed location names (run once)
venv/bin/python3 seed_establishments.py
```

### 2. Environment variables

```bash
REVEL_USER=your_revel_email
REVEL_PASS=your_revel_password
ESTABLISHMENTS=32,14,48,7,6,25,36,26,20,40,15,54
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
venv/bin/python3 pipeline.py

# Fetch a specific date (for backfilling)
venv/bin/python3 pipeline.py --date 2026-05-04

# Run only specific locations
venv/bin/python3 pipeline.py --establishments 32,14

# Skip product/modifier re-fetch (use DB cache) — 7x faster, use for backfills
venv/bin/python3 pipeline.py --date 2026-05-04 --skip-reference

# Aggregate feature tables (run after pipeline.py)
venv/bin/python3 aggregate_features.py --date 2026-05-04
```

### 5. Backfill historical data

**Local backfill** (`backfill_local.sh`) — uses project `venv/`, passes `--skip-reference` for ~2.5 min/day (~7× faster than naive approach):

```bash
# Feb 10 2026 → yesterday (default)
nohup bash backfill_local.sh > /var/log/laynes/backfill3m.log 2>&1 &

# Custom date range
nohup bash backfill_local.sh 2026-01-01 2026-04-30 > /var/log/laynes/backfill3m.log 2>&1 &

# Watch live progress
tail -f /var/log/laynes/backfill3m.log

# Check status / see incomplete dates
bash backfill_local.sh --status
```

**Server backfill** (`backfill3m.sh`) — for `/opt/laynes` deployments, auto-resumes, tracks failures:

```bash
# Last 3 months → yesterday (default)
nohup bash /opt/laynes/backfill3m.sh > /var/log/laynes/backfill3m.log 2>&1 &

# Custom date range
nohup bash /opt/laynes/backfill3m.sh 2026-01-01 2026-04-30 > /var/log/laynes/backfill3m.log 2>&1 &

# Check status / see incomplete dates
bash /opt/laynes/backfill3m.sh --status
```

---

## Cron Schedule

```
TZ=America/Chicago
0 9 * * *   /root/pos-analytics-pipeline/run.sh   # 9:00 AM Central — fetch + insert + aggregate
```

`run.sh` runs `pipeline.py` → `aggregate_features.py` → weather refresh (`weather_analysis.cli backfill`) in sequence and logs everything to `/var/log/laynes/run_YYYY-MM-DD.log`. Logs are retained for 30 days. The weather step is best-effort: if it fails, the run is not marked failed (weather only feeds a supplementary dashboard feature, not the core forecast).

---

## Tender Planning Dashboard (`dashboard.py`)

A Streamlit page (🍗 Tender Planning) that tells kitchens how many chicken tenders to prep per 15-min slot, split into **Spicy** and **Regular** tabs. Its per-slot forecast is a simple **median over same-weekday history** — chosen because at 15-min granularity, volumes are low enough that "what actually happened the last N times this exact slot occurred on this weekday" is more useful to a kitchen than any trained model. This is the only forecasting in the project today.

**Layout:** a single-page dashboard with **no sidebar** — a top bar carries the brand plus the **Location** and **Day** pickers, a chip row shows the selected day / location / timezone (and a "Predicted only" flag for future dates), and a **US holiday notification bar** appears at the very top when the selected day is a holiday. Content sits in the Spicy / Regular tabs below.

**Live deployment:** runs as systemd service `laynes-dashboard`, Streamlit on port 8502, proxied by nginx at `https://hr.aygfoods.com/daily-sales-prediction`. Restart with `systemctl restart laynes-dashboard`; logs at `/var/log/laynes-dashboard.log`. **Do not** launch it with `nohup` — systemd owns port 8502 and will fight a manual instance.

### How the prediction is computed

For a given location, weekday, and 15-min slot:
1. Pull every past same-weekday date since backfill start (Feb 10, 2026) up to (not including) the selected date — this is the full `n_weeks` history, unbounded and growing over time.
2. Split every order line into regular/spicy tender counts (`get_finger_split`), so each tab only ever sums its own flavor.
3. **Zero-fill**: any past week with no matching order for that slot is treated as a real 0, not a missing data point — otherwise the median would only be computed over the rare weeks that happened to have an order there, wildly overstating a slot that's barely ever active.
4. Predicted quantity = median across **all** `n_weeks` (zero-filled) values. If that median is 0, the slot is dropped from the table entirely rather than showing a phantom prep number — see `dashboard.py` around `render_tender_flavor()` (the `pivot`/`fillna(0.0)` block).

### Weather signal (supplementary)

Daily weather per location (`weather_daily`, from Open-Meteo) is layered onto the forecast as an **exploration aid, not a change to the headline number**. Two places surface it:

1. **Slot drilldown → "Weather-adj" metric.** Clicking a 15-min row shows that slot's history; alongside Records/Average/Median/Max there's a **weather-weighted median** — each past same-weekday is weighted by how closely its weather (temp high + rain) matched the target day, then the weighted median is taken. Weighted median (not mean) so a freak high-volume day can't drag it. Falls back to "—" when there are &lt;6 weathered same-weekdays.
2. **"Weather forecast vs actual — day total" section.** Per flavor tab: the selected day's Median vs Weather vs Actual day totals, plus a leave-one-out backtest bar chart over recent same-weekdays with a verdict on which forecast tracked actual more closely.

The target day's weather comes from `weather_daily` (archive) or, for recent/future dates the ~5-day-lagged archive lacks, the Open-Meteo **forecast** API. **Caveat:** backtesting showed weather is a weak signal for daily tender demand (often no better than the plain median) — this is a display/exploration feature, and the main "Predicted" number is unchanged. See the `weather_analysis` package for the offline analysis and backtest that informed this.

### Holiday awareness

When the selected day is a **US holiday**, a notification bar renders at the top of the page (e.g. "🎆 Today is Independence Day — demand often differs from a typical Saturday"). Backed by the `holidays` library (`holidays.US`, includes observed dates), imported defensively so a missing package never breaks the page.

### Known limitation: slow to pick up new patterns

Because step 4 uses the median over **all** history (no rolling window, no recency weighting), a slot that has historically been empty will **not** reappear the moment real orders start showing up there. More than half of all recorded same-weekdays need to be non-zero before the median flips positive.

Concretely: if a slot has 19 zero-weeks and 1 non-zero week on record, and orders start appearing there every week going forward, it takes roughly **19 more consecutive weeks** (~4-5 months) before that slot's median turns positive and it reappears in the table — the old zero-weeks keep outnumbering the new ones until they don't.

**If this needs to be more responsive** (e.g. a location extends hours or a new pattern emerges and you want it reflected within a few weeks, not months), the fix is to compute the median over a **rolling recent window** (e.g. last 10-12 same-weekdays) instead of full history, or add recency weighting. Not implemented as of this writing — flagged here so a missing/slow-to-appear slot isn't mistaken for a bug.

### Performance: why date/location switches used to be slow

Switching the date or location dropdown used to take 10+ seconds. Root causes, found via `EXPLAIN (ANALYZE, BUFFERS)`:

1. **Postgres JIT compilation overhead dominated query time** — the same query ran in 4.6s with `jit=on` vs 0.37s with `jit=off` (~12x). JIT is built for huge batch scans; for this dashboard's pattern of many small/medium repeated queries it was pure overhead with no benefit. Fixed at the database level: `ALTER DATABASE laynes SET jit = off;` — a performance-only flag, applies to all new connections, no correctness impact on the pipeline scripts.
2. **The `order_items JOIN orders` join fanned out across all 12 monthly partitions per row.** Both tables are partitioned by their own `created_date`, but `order_items.created_date` can differ from its parent order's `created_date` by a few seconds — so Postgres had no partition key to prune on and checked every partition per row. Fixed by bounding the join to `o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'` (all query sites in `dashboard.py`) — the ±1 day window is far wider than the actual few-second gap, so this is a correctness no-op that lets Postgres prune to ~2 partitions instead of 12.
3. **`load_dow_history` and `load_recent_daily` were cached keyed on the exact selected date**, so every date change was a guaranteed cache miss — even when browsing between two dates of the *same weekday*, the most common workflow. Both now cache on the stable dimension only (location, and weekday for `load_dow_history`) and fetch the full underlying history once; callers filter the date-dependent window in pandas afterward. Browsing dates within an already-cached weekday is now served from cache instead of re-querying Postgres.

Net effect: worst case (a location/weekday never viewed this session) dropped from ~14s (3 sequential ~4.6s queries) to under ~1s; same-weekday date browsing after the first hit is now a cache hit (~0.5s, pure Streamlit rerun, no DB round trip).

**If a future query added here feels slow**, check `EXPLAIN (ANALYZE, BUFFERS)` with `jit` on and off before reaching for an index — on this database, JIT overhead has repeatedly been the bigger factor. Also: any new `@st.cache_data` function parameterized by the currently-selected date will miss cache on every date change — prefer caching on the stable dimension and filtering the date-dependent slice in pandas, as done here.

---

## Sales Intelligence Dashboard (`sales_report.py`)

A separate, network-wide Streamlit page ("Laynes | Sales Intelligence") for weekly performance across all 12 locations — a manager-facing view, distinct from the kitchen-facing Tender Planning dashboard above. Reads from `features_daily_summary` (Layer 2), not raw order data, so it's fast regardless of history size.

**Sections:**
- **Sidebar week selector** — pick any past week (Mon–Sun) from all weeks with data.
- **KPI header** — network totals for the selected week vs. the prior week (WoW deltas).
- **Leaderboard** — all 12 locations ranked for the selected week.
- **8-Week Trend** — Grouped / Stacked / Table tabs comparing locations over the last 8 weeks.
- **Day-of-week heatmap** — which weekdays run hottest, per location.

**Live deployment:** runs as systemd service `laynes-sales-report`, Streamlit on port 8503, proxied by nginx at `https://hr.aygfoods.com/sales-report`. Restart with `systemctl restart laynes-sales-report`; logs at `/var/log/laynes-sales-report.log`.

---

## Key Design Decisions

**Partitioned tables** — `orders` and `order_items` are partitioned by month (`PARTITION BY RANGE (created_date)`). This keeps queries fast and allows easy archival of old data.

**Idempotent inserts** — every insert uses `ON CONFLICT DO NOTHING`, so re-running the pipeline for the same date is safe.

**Modifier name resolution** — Revel's `modifieritems` records contain only a URI (e.g. `/resources/Modifier/25002/`). `pipeline.py` fetches the full modifier list from `/resources/Modifier/` once per run, upserts into the `modifiers` reference table, and uses an in-memory cache to populate `modifier_name` on every row.

**Batched item fetching** — the `OrderItem` establishment filter is broken in Revel's API (silently returns all locations). Items are fetched using `order__in=id1,id2,...` in batches of 200, which is the only reliable way to get per-location items.

**Feature separation** — raw tables are append-only and never updated. Feature tables are computed nightly by `aggregate_features.py` and are what the Sales Intelligence dashboard reads (never the raw tables).

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
requests
streamlit
pandas
numpy
plotly
holidays
```

```bash
venv/bin/pip install playwright psycopg2-binary python-dotenv requests streamlit pandas numpy plotly holidays
venv/bin/playwright install chromium
```

- `playwright`/`psycopg2-binary`/`python-dotenv` — pipeline scripts.
- `requests` — the `weather_analysis` package (Open-Meteo API) and the dashboard's weather-forecast fallback.
- `streamlit`/`pandas`/`numpy`/`plotly` — the two dashboards. `numpy` also backs the weather-weighted-median math in `dashboard.py`.
- `holidays` — the Tender Planning US holiday banner. **Optional**: `dashboard.py` imports it defensively, so if it's missing the page still runs, just without the holiday bar.

Skip the dashboard-only packages if you're running only the nightly pipeline.

PostgreSQL 16 tested (15+ should work)

> All scripts load `.env` automatically via `python-dotenv` — no need to `source` it manually before running.
