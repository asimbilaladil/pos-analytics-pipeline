-- 54_r365_canonical.sql
-- R365 weekly-report API: canonical facts, store mapping, run provenance, and
-- the LLM-safe views. VIEWS ARE CREATED HERE BUT GRANTED NOTHING -- the
-- laynes_ro GRANT lives in 55_r365_llm_grants.sql, the 43/44 convention.
--
-- Source: GET https://weekly-report.aygchicken.com/weekly-data?week=<Thursday>
-- One response = one Thursday->Wednesday week: a weekly row per store plus a
-- `days` array with one row per store per day. There is no separate daily
-- endpoint and no finer grain: no items, no hours, no employees.
--
-- Canonical boundary: week_start >= 2026-08-13. Earlier weeks are reachable
-- live from R365 but are deliberately NOT ingested (approved scope decision);
-- the CHECK constraints below make that structural.
--
-- ---------------------------------------------------------------------------
-- SOURCE SEMANTICS (proven against every store-day and store-week from
-- 2026-08-13 to 2026-10-14: 756 daily rows, 108 weekly rows)
-- ---------------------------------------------------------------------------
--   laborPct            = totalLaborAmount / netSales   (0-1 fraction)
--                         weekly exact; DAILY IS ROUNDED TO 2 dp by the source
--   discountPctOfSales  = totalDiscounts / netSales     (0-1 fraction, exact)
--   avgCheck            = netSales / checkCount         (exact)
--   avgPerGuest         = netSales / guestCount         (exact)
--   salesPerLaborHour   = netSales / totalLaborHours    (weekly only, exact)
--   salesVsForecast     daily  = netSales - forecastedSales
--                       weekly = forecastSalesBase - forecastedSales
--   forecastSalesBase   = sum(netSales) over days with forecastedSales > 0
--   salesVsForecastPct  = salesVsForecast / forecastedSales (0-1 fraction)
--
--   forecastedSales = 0 is R365's "no forecast entered" placeholder: on every
--   such day salesVsForecast and salesVsForecastPct are null. A weekly row
--   with no forecast day has forecastedSales null and forecastSalesBase 0.
--
--   ZERO DENOMINATORS: the source sends 0 for an undefined ratio (Beaumont
--   2026-09-03: netSales 0, labour $42.50, laborPct 0). That 0 is NOT a real
--   0%. Facts keep it verbatim for audit; the views return NULL.
--
-- ---------------------------------------------------------------------------
-- STORAGE CONVENTIONS
-- ---------------------------------------------------------------------------
--   * Facts preserve SOURCE VALUES EXACTLY. Money, hours and ratios are
--     unconstrained NUMERIC: nothing is rounded on ingest. Counts are integer.
--   * Source ratios are stored as their 0-1 FRACTION in *_fraction columns.
--     The views expose percentage POINTS (fraction x 100, 20.5 = 20.5%).
--     Storing 0-1 and presenting 0-100 is the classic silent 100x error, so
--     the conversion lives in exactly one place: the views.
--   * A source day flagged missing (missingDays / noData) is NEVER inserted.
--     Missing is absence, not zero.
--   * day_status distinguishes genuine zero-trading source rows from normal
--     ones: NORMAL, CLOSED, PRE_OPENING. It is assigned by the loader.

BEGIN;

-- ---------------------------------------------------------------------------
-- Store mapping: R365 locationId -> canonical Revel establishment_id
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS r365_store_mapping (
    r365_store_id     text        PRIMARY KEY,
    r365_store_name   text        NOT NULL,
    establishment_id  integer     REFERENCES establishments(id),
    status            text        NOT NULL,
    active            boolean     NOT NULL DEFAULT true,
    -- Confirmed first trading date. NULL = not confirmed. PRE_OPENING is only
    -- ever assigned against a confirmed date, never inferred from zero sales.
    opening_date      date,
    verified_by       text,
    verified_at       timestamptz,
    notes             text,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT r365_store_mapping_id_shape
        CHECK (r365_store_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    CONSTRAINT r365_store_mapping_status_check
        CHECK (status IN ('VERIFIED', 'CANDIDATE', 'UNMAPPED', 'AMBIGUOUS', 'RETIRED')),
    CONSTRAINT r365_store_mapping_verified_requires_proof
        CHECK (status <> 'VERIFIED'
               OR (establishment_id IS NOT NULL AND verified_by IS NOT NULL
                   AND verified_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS r365_store_mapping_one_active_establishment
    ON r365_store_mapping (establishment_id)
    WHERE active AND establishment_id IS NOT NULL;

COMMENT ON TABLE r365_store_mapping IS
  'R365 locationId (stable UUID) -> canonical establishment_id. Explicit and '
  'evidence-based: never fuzzy-matched. Only ACTIVE VERIFIED rows are loaded '
  'or exposed. Also the loader''s allowlist: an R365 store absent from this '
  'table is surfaced as unmapped and its rows are not loaded.';

-- Seed: 12 VERIFIED mappings, approved 2026-10-16.
-- Evidence: Pearson correlation of R365 daily netSales against Revel REAL
-- daily subtotal (v_orders_classified) over 63 days, 2026-08-13..2026-10-14,
-- tested against all 12 Revel establishments. Every R365 store pairs 1:1 with
-- exactly one establishment; the runner-up never exceeds 0.83. R365 net sales
-- run 0.7-3.4% above Revel REAL subtotal for every pair.
INSERT INTO r365_store_mapping
    (r365_store_id, r365_store_name, establishment_id, status,
     verified_by, verified_at, notes)
VALUES
 ('83a5757d-6ffc-41c1-875a-5adb6e693e99', 'LCF Airtex',        32, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'Exact name match. Daily sales corr 0.9842 vs est 32; runner-up 0.615. R365/Revel sales ratio 1.0164.'),
 ('fe62609a-7f59-40e5-a023-fce3636191f3', 'LCF Beaumont',      14, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'Exact name match. Daily sales corr 0.9638 vs est 14; runner-up 0.342. Ratio 1.0067.'),
 ('45ae2ddd-0ffe-4ed8-af37-ae7e0573954e', 'LCF Cypress',       54, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'Exact name match. Daily sales corr 0.9997 vs est 54; runner-up 0.810. Ratio 1.0275.'),
 ('eaecfc87-c58e-414c-99e2-883516d3d53b', 'LCF Downtown',      48, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'NAME DIFFERS (Revel: LCF Downtown Houston). Approved on sales evidence: corr 0.9986 vs est 48; runner-up 0.291. Ratio 1.0300.'),
 ('fc51a73b-ae99-458d-9e46-081290d760d9', 'LCF Fairmont',      20, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'NAME DIFFERS (Revel: LCF Pasadena). Approved on sales evidence: corr 0.9850 vs est 20; runner-up 0.826. Ratio 1.0145.'),
 ('4efdd0d7-1cfd-4b26-ac43-bf7559d10fec', 'LCF Garden Oaks',    7, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'NAME DIFFERS (Revel: LCF Ella). Approved on sales evidence: corr 0.9702 vs est 7; runner-up 0.827. Ratio 1.0127.'),
 ('fe4877f0-3e5b-47b0-9b12-ccfdc9fe6cde', 'LCF Katy',           6, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'Exact name match. Daily sales corr 0.9963 vs est 6; runner-up 0.808. Ratio 1.0192.'),
 ('ddaec52c-402f-4143-993d-e1fe5c61fbfe', 'LCF Mission Bend',  25, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'Exact name match. Daily sales corr 0.9727 vs est 25; runner-up 0.799. Ratio 1.0263.'),
 ('5c85a935-2e63-4065-b4d4-c1582fea5485', 'LCF Missouri City', 36, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'Exact name match. Daily sales corr 0.9267 vs est 36; runner-up 0.812. Ratio 1.0342.'),
 ('9ce105d5-e8c3-4f70-85f1-0639ccd2625f', 'LCF Nederland',     26, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'Exact name match. Daily sales corr 0.9931 vs est 26; runner-up 0.368. Ratio 1.0236.'),
 ('1ba72f8f-4525-4c0c-8053-ae2eb651f090', 'LCF Rosenberg',     40, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'Exact name match. Daily sales corr 0.9927 vs est 40; runner-up 0.829. Ratio 1.0252.'),
 ('fed10630-4b13-480b-afe2-8c27b86cb6f7', 'LCF Shepherd',      15, 'VERIFIED', 'asim', '2026-10-16 00:00:00+00',
  'Exact name match. Daily sales corr 0.9510 vs est 15; runner-up 0.312. Ratio 1.0068.')
ON CONFLICT (r365_store_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Ingest run provenance: ONE ROW PER (run, requested week)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS r365_ingest_run (
    id                     bigserial   PRIMARY KEY,
    batch_id               text        NOT NULL,
    trigger                text        NOT NULL,
    started_at             timestamptz NOT NULL DEFAULT now(),
    completed_at           timestamptz,
    status                 text        NOT NULL DEFAULT 'RUNNING',
    requested_week_start   date        NOT NULL,
    requested_week_end     date        NOT NULL,
    expected_store_count   integer,
    observed_store_count   integer,
    mapped_observed_count  integer,
    stores_with_full_week  integer,
    missing_store_days     integer,
    daily_rows_received    integer,
    weekly_rows_received   integer,
    daily_rows_upserted    integer,
    weekly_rows_upserted   integer,
    daily_rows_changed     integer,
    weekly_rows_changed    integer,
    source_complete        boolean,
    source_min_date        date,
    source_max_date        date,
    reconciliation_ok      boolean,
    unmapped_stores        jsonb,
    missing_stores         jsonb,
    source_missing_days    jsonb,
    reconciliation         jsonb,
    schema_warnings        jsonb,
    source_warning         text,
    http_status            integer,
    fetch_seconds          numeric,
    payload_sha256         text,
    raw_archive_path       text,
    loader_version         text        NOT NULL,
    error_code             text,
    error_message          text,
    CONSTRAINT r365_ingest_run_status_check
        CHECK (status IN ('RUNNING', 'COMPLETE', 'PARTIAL', 'FAILED')),
    CONSTRAINT r365_ingest_run_trigger_check
        CHECK (trigger IN ('scheduled', 'backfill', 'manual')),
    CONSTRAINT r365_ingest_run_week_shape
        CHECK (EXTRACT(isodow FROM requested_week_start) = 4
               AND requested_week_end = requested_week_start + 6),
    CONSTRAINT r365_ingest_run_finished_has_time
        CHECK (status = 'RUNNING' OR completed_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS r365_ingest_run_week_idx
    ON r365_ingest_run (requested_week_start, id DESC);

COMMENT ON TABLE r365_ingest_run IS
  'One row per requested week per run. Never holds credentials. Internal: '
  'error_message, raw_archive_path and payload_sha256 never cross the LLM '
  'boundary.';

-- ---------------------------------------------------------------------------
-- Daily fact: (establishment_id, business_date)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS r365_store_daily (
    establishment_id               integer     NOT NULL REFERENCES establishments(id),
    business_date                  date        NOT NULL,
    week_start                     date        NOT NULL,
    r365_store_id                  text        NOT NULL REFERENCES r365_store_mapping(r365_store_id),
    day_status                     text        NOT NULL,
    -- SALES
    net_sales                      numeric     NOT NULL,   -- netSales
    gross_sales                    numeric     NOT NULL,   -- grossSales
    guest_count                    integer     NOT NULL,   -- guestCount
    check_count                    integer     NOT NULL,   -- checkCount
    avg_check                      numeric     NOT NULL,   -- avgCheck (source; 0 when undefined)
    avg_per_guest                  numeric     NOT NULL,   -- avgPerGuest (source; 0 when undefined)
    -- LABOUR
    labor_hours                    numeric     NOT NULL,   -- totalLaborHours
    labor_cost                     numeric     NOT NULL,   -- totalLaborAmount
    labor_pct_fraction             numeric     NOT NULL,   -- laborPct (0-1, ROUNDED to 2 dp by source)
    -- CASH
    total_deposit                  numeric     NOT NULL,   -- totalDeposit
    expected_cash                  numeric     NOT NULL,   -- expectedCash (daily only)
    paid_in                        numeric     NOT NULL,   -- paidInTotal
    paid_out                       numeric     NOT NULL,   -- paidOutTotal
    over_short                     numeric     NOT NULL,   -- overShort (= totalDeposit - expectedCash)
    -- DISCOUNTS
    discounts                      numeric     NOT NULL,   -- discounts
    comps                          numeric     NOT NULL,   -- comps
    coupons                        numeric     NOT NULL,   -- coupons
    total_discounts                numeric     NOT NULL,   -- totalDiscounts
    discount_pct_fraction          numeric     NOT NULL,   -- discountPctOfSales (0-1)
    -- GIFT CARDS
    gift_cards_redeemed            numeric     NOT NULL,   -- gcardRedeem
    gift_cards_sold                numeric     NOT NULL,   -- gcardSold
    -- TIPS
    tips_declared                  numeric     NOT NULL,   -- declaredTips
    credit_card_tips               numeric     NOT NULL,   -- creditCardTips
    -- VOIDS
    void_count                     integer     NOT NULL,   -- voidCount
    void_amount                    numeric     NOT NULL,   -- voidAmount
    -- FORECAST
    forecasted_sales               numeric,                -- forecastedSales (0 = no forecast)
    sales_vs_forecast_amount       numeric,                -- salesVsForecast (null when no forecast)
    sales_vs_forecast_pct_fraction numeric,                -- salesVsForecastPct (0-1; null when no forecast)
    -- provenance
    source_store_name              text        NOT NULL,
    ingest_run_id                  bigint      NOT NULL REFERENCES r365_ingest_run(id),
    source_fetched_at              timestamptz NOT NULL,
    first_loaded_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT r365_store_daily_pkey PRIMARY KEY (establishment_id, business_date),
    CONSTRAINT r365_store_daily_boundary CHECK (business_date >= DATE '2026-08-13'),
    CONSTRAINT r365_store_daily_week_start
        CHECK (week_start = business_date
                            - ((EXTRACT(isodow FROM business_date)::int + 3) % 7)),
    CONSTRAINT r365_store_daily_day_status
        CHECK (day_status IN ('NORMAL', 'CLOSED', 'PRE_OPENING')),
    CONSTRAINT r365_store_daily_counts_nonneg
        CHECK (guest_count >= 0 AND check_count >= 0 AND void_count >= 0)
);
CREATE INDEX IF NOT EXISTS r365_store_daily_date_idx  ON r365_store_daily (business_date);
CREATE INDEX IF NOT EXISTS r365_store_daily_week_idx  ON r365_store_daily (week_start);

-- ---------------------------------------------------------------------------
-- Weekly fact: (establishment_id, week_start) -- the OFFICIAL R365 weekly row
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS r365_store_weekly (
    establishment_id               integer     NOT NULL REFERENCES establishments(id),
    week_start                     date        NOT NULL,
    week_end                       date        NOT NULL,
    r365_store_id                  text        NOT NULL REFERENCES r365_store_mapping(r365_store_id),
    -- SALES
    net_sales                      numeric     NOT NULL,
    gross_sales                    numeric     NOT NULL,
    guest_count                    integer     NOT NULL,
    check_count                    integer     NOT NULL,
    avg_check                      numeric     NOT NULL,
    avg_per_guest                  numeric     NOT NULL,
    -- LABOUR
    labor_hours                    numeric     NOT NULL,
    labor_cost                     numeric     NOT NULL,
    labor_pct_fraction             numeric     NOT NULL,   -- laborPct (0-1, exact)
    sales_per_labor_hour           numeric     NOT NULL,   -- salesPerLaborHour (weekly only)
    -- CASH
    total_deposit                  numeric     NOT NULL,
    paid_in                        numeric     NOT NULL,
    paid_out                       numeric     NOT NULL,
    over_short                     numeric     NOT NULL,
    -- DISCOUNTS
    discounts                      numeric     NOT NULL,
    comps                          numeric     NOT NULL,
    coupons                        numeric     NOT NULL,
    total_discounts                numeric     NOT NULL,
    discount_pct_fraction          numeric     NOT NULL,
    -- GIFT CARDS
    gift_cards_redeemed            numeric     NOT NULL,
    gift_cards_sold                numeric     NOT NULL,
    -- TIPS
    tips_declared                  numeric     NOT NULL,
    credit_card_tips               numeric     NOT NULL,
    -- VOIDS
    void_count                     integer     NOT NULL,
    void_amount                    numeric     NOT NULL,
    -- FORECAST
    forecasted_sales               numeric,                -- null = no forecast day in the week
    forecast_sales_base            numeric     NOT NULL,   -- forecastSalesBase (weekly only)
    sales_vs_forecast_amount       numeric,
    sales_vs_forecast_pct_fraction numeric,
    days_with_data                 integer     NOT NULL,   -- daysWithData (weekly only)
    -- provenance
    source_store_name              text        NOT NULL,
    ingest_run_id                  bigint      NOT NULL REFERENCES r365_ingest_run(id),
    source_fetched_at              timestamptz NOT NULL,
    first_loaded_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT r365_store_weekly_pkey PRIMARY KEY (establishment_id, week_start),
    CONSTRAINT r365_store_weekly_boundary CHECK (week_start >= DATE '2026-08-13'),
    CONSTRAINT r365_store_weekly_thursday CHECK (EXTRACT(isodow FROM week_start) = 4),
    CONSTRAINT r365_store_weekly_wednesday CHECK (EXTRACT(isodow FROM week_end) = 3),
    CONSTRAINT r365_store_weekly_seven_days CHECK (week_end = week_start + 6),
    CONSTRAINT r365_store_weekly_days_range CHECK (days_with_data BETWEEN 1 AND 7),
    CONSTRAINT r365_store_weekly_counts_nonneg
        CHECK (guest_count >= 0 AND check_count >= 0 AND void_count >= 0)
);
CREATE INDEX IF NOT EXISTS r365_store_weekly_week_idx ON r365_store_weekly (week_start);

COMMENT ON TABLE r365_store_daily IS
  'Canonical R365 daily facts, source values verbatim. Ratios are 0-1 '
  'fractions in *_fraction columns; laborPct is rounded to 2 dp by the source '
  'at daily grain. Missing source days are absent, never zero rows.';
COMMENT ON TABLE r365_store_weekly IS
  'Official R365 weekly row per store (authoritative for weekly questions). '
  'Never derived from daily rows; daily sums are only a reconciliation check.';

-- ===========================================================================
-- LLM-SAFE VIEWS (granted in 55). ACTIVE VERIFIED mappings only. No R365
-- store id, no source name, no run id, no error text, no archive path.
-- Ratios are percentage POINTS. NULL = mathematically undefined or no forecast.
-- ===========================================================================

-- Per-week completeness, from the latest ANALYTICAL run for each week.
-- FAILED runs never become truth; an unrecognised status is excluded by the
-- IN list rather than admitted by a <> test.
CREATE OR REPLACE VIEW v_r365_completeness_llm AS
WITH latest AS (
    SELECT DISTINCT ON (requested_week_start)
           requested_week_start, requested_week_end, status, source_complete,
           reconciliation_ok, expected_store_count, mapped_observed_count,
           stores_with_full_week, missing_store_days, completed_at
      FROM r365_ingest_run
     WHERE status IN ('COMPLETE', 'PARTIAL')
     ORDER BY requested_week_start, id DESC
), last_attempt AS (
    SELECT DISTINCT ON (requested_week_start)
           requested_week_start, status
      FROM r365_ingest_run
     WHERE status <> 'RUNNING'
     ORDER BY requested_week_start, id DESC
), day_flags AS (
    SELECT d.week_start,
           count(*) FILTER (WHERE d.day_status = 'CLOSED')      AS closed_store_days,
           count(*) FILTER (WHERE d.day_status = 'PRE_OPENING') AS pre_opening_store_days
      FROM r365_store_daily d
      JOIN r365_store_mapping m ON m.r365_store_id = d.r365_store_id
     WHERE m.status = 'VERIFIED' AND m.active
     GROUP BY d.week_start
)
SELECT l.requested_week_start                         AS week_start,
       l.requested_week_end                           AS week_end,
       p.fiscal_year, p.period_number, p.period_label,
       l.status                                       AS week_status,
       l.source_complete,
       l.reconciliation_ok,
       l.expected_store_count,
       l.mapped_observed_count                        AS observed_store_count,
       l.stores_with_full_week,
       l.missing_store_days,
       COALESCE(f.closed_store_days, 0)               AS closed_store_days,
       COALESCE(f.pre_opening_store_days, 0)          AS pre_opening_store_days,
       (a.status = 'FAILED')                          AS latest_refresh_failed,
       l.completed_at                                 AS loaded_at
  FROM latest l
  LEFT JOIN last_attempt a ON a.requested_week_start = l.requested_week_start
  LEFT JOIN day_flags f    ON f.week_start = l.requested_week_start
  LEFT JOIN ayg_business_periods p
         ON l.requested_week_start BETWEEN p.start_date AND p.end_date;

COMMENT ON VIEW v_r365_completeness_llm IS
  'One row per loaded R365 week (Thursday->Wednesday, America/Chicago). '
  'week_status COMPLETE = every expected mapped store present for all 7 days, '
  'source flagged complete, and daily sums reconcile to the official weekly '
  'row. PARTIAL = any of those failed. A week absent from this view is NOT '
  'loaded -- never zero. latest_refresh_failed = a later re-fetch failed and '
  'the loaded data is from the last successful run.';

CREATE OR REPLACE VIEW v_r365_store_daily_llm AS
SELECT d.establishment_id,
       e.name                                                   AS store_name,
       d.business_date,
       d.week_start,
       p.fiscal_year, p.period_number, p.period_label,
       d.day_status,
       -- SALES
       d.net_sales, d.gross_sales, d.guest_count, d.check_count,
       CASE WHEN d.check_count > 0 THEN d.net_sales / d.check_count END AS avg_check,
       CASE WHEN d.guest_count > 0 THEN d.net_sales / d.guest_count END AS avg_per_guest,
       -- LABOUR
       d.labor_hours, d.labor_cost,
       CASE WHEN d.net_sales <> 0 THEN d.labor_cost / d.net_sales * 100 END AS labor_pct,
       CASE WHEN d.labor_hours > 0 THEN d.net_sales / d.labor_hours END   AS sales_per_labor_hour,
       -- CASH
       d.total_deposit, d.expected_cash, d.paid_in, d.paid_out, d.over_short,
       -- DISCOUNTS
       d.discounts, d.comps, d.coupons, d.total_discounts,
       CASE WHEN d.net_sales <> 0 THEN d.total_discounts / d.net_sales * 100 END AS discount_pct,
       -- GIFT CARDS / TIPS / VOIDS
       d.gift_cards_redeemed, d.gift_cards_sold,
       d.tips_declared, d.credit_card_tips,
       d.void_count, d.void_amount,
       -- FORECAST: 0 is the source's no-forecast placeholder -> NULL here
       CASE WHEN d.forecasted_sales > 0 THEN d.forecasted_sales END        AS forecasted_sales,
       CASE WHEN d.forecasted_sales > 0 THEN d.net_sales END               AS forecast_sales_base,
       CASE WHEN d.forecasted_sales > 0 THEN d.sales_vs_forecast_amount END AS sales_vs_forecast_amount,
       CASE WHEN d.forecasted_sales > 0
            THEN d.sales_vs_forecast_amount / d.forecasted_sales * 100 END AS sales_vs_forecast_pct
  FROM r365_store_daily d
  JOIN r365_store_mapping m ON m.r365_store_id = d.r365_store_id
  JOIN establishments e     ON e.id = d.establishment_id
  LEFT JOIN ayg_business_periods p
         ON d.business_date BETWEEN p.start_date AND p.end_date
 WHERE m.status = 'VERIFIED' AND m.active;

COMMENT ON VIEW v_r365_store_daily_llm IS
  'R365 daily store metrics. Grain establishment_id + business_date (America/'
  'Chicago). Percentages are POINTS (20.5 = 20.5%). NULL ratio = undefined '
  '(zero denominator) or no forecast. A missing row is NOT zero. day_status '
  'CLOSED/PRE_OPENING rows are genuine source rows with zero trading. Finest '
  'R365 grain is daily: no hours, items or employees exist.';
COMMENT ON COLUMN v_r365_store_daily_llm.labor_pct IS
  'labor_cost / net_sales x 100, recomputed from source numerators (the '
  'source''s own daily laborPct is rounded to whole percent). NULL when '
  'net_sales = 0.';
COMMENT ON COLUMN v_r365_store_daily_llm.forecasted_sales IS
  'NULL when no forecast was entered (source placeholder 0). Such a day is '
  'neither a forecast hit nor a miss.';
COMMENT ON COLUMN v_r365_store_daily_llm.forecast_sales_base IS
  'net_sales on forecast-covered days only, else NULL. Roll up forecast '
  'variance as SUM(forecast_sales_base) - SUM(forecasted_sales).';

CREATE OR REPLACE VIEW v_r365_store_weekly_llm AS
SELECT w.establishment_id,
       e.name                                                   AS store_name,
       w.week_start, w.week_end,
       p.fiscal_year, p.period_number, p.period_label,
       c.week_status,
       w.days_with_data,
       -- SALES
       w.net_sales, w.gross_sales, w.guest_count, w.check_count,
       CASE WHEN w.check_count > 0 THEN w.net_sales / w.check_count END AS avg_check,
       CASE WHEN w.guest_count > 0 THEN w.net_sales / w.guest_count END AS avg_per_guest,
       -- LABOUR
       w.labor_hours, w.labor_cost,
       CASE WHEN w.net_sales <> 0 THEN w.labor_cost / w.net_sales * 100 END AS labor_pct,
       CASE WHEN w.labor_hours > 0 THEN w.net_sales / w.labor_hours END   AS sales_per_labor_hour,
       -- CASH
       w.total_deposit, w.paid_in, w.paid_out, w.over_short,
       -- DISCOUNTS
       w.discounts, w.comps, w.coupons, w.total_discounts,
       CASE WHEN w.net_sales <> 0 THEN w.total_discounts / w.net_sales * 100 END AS discount_pct,
       -- GIFT CARDS / TIPS / VOIDS
       w.gift_cards_redeemed, w.gift_cards_sold,
       w.tips_declared, w.credit_card_tips,
       w.void_count, w.void_amount,
       -- FORECAST (forecast-covered sales only)
       CASE WHEN w.forecasted_sales > 0 THEN w.forecasted_sales END        AS forecasted_sales,
       CASE WHEN w.forecasted_sales > 0 THEN w.forecast_sales_base END     AS forecast_sales_base,
       CASE WHEN w.forecasted_sales > 0
            THEN w.forecast_sales_base - w.forecasted_sales END            AS sales_vs_forecast_amount,
       CASE WHEN w.forecasted_sales > 0
            THEN (w.forecast_sales_base - w.forecasted_sales)
                 / w.forecasted_sales * 100 END                            AS sales_vs_forecast_pct
  FROM r365_store_weekly w
  JOIN r365_store_mapping m ON m.r365_store_id = w.r365_store_id
  JOIN establishments e     ON e.id = w.establishment_id
  LEFT JOIN v_r365_completeness_llm c ON c.week_start = w.week_start
  LEFT JOIN ayg_business_periods p
         ON w.week_start BETWEEN p.start_date AND p.end_date
 WHERE m.status = 'VERIFIED' AND m.active;

COMMENT ON VIEW v_r365_store_weekly_llm IS
  'OFFICIAL R365 weekly row per store (Thursday->Wednesday). Authoritative for '
  'weekly R365 questions; never replace it with summed daily rows. '
  'Percentages are POINTS. Forecast variance uses forecast-covered sales '
  '(forecast_sales_base), not all net sales. Never SUM a ratio column.';

-- Store x fiscal period rollup from the official weekly rows. Ratios are
-- RECOMPUTED from summed numerators/denominators, never averaged.
CREATE OR REPLACE VIEW v_r365_store_period_llm AS
WITH agg AS (
    SELECT w.establishment_id, w.store_name, w.fiscal_year, w.period_number,
           w.period_label,
           count(*)                                         AS weeks_loaded,
           count(*) FILTER (WHERE w.week_status = 'COMPLETE') AS weeks_complete,
           min(w.week_start)                                AS first_week_start,
           max(w.week_end)                                  AS last_week_end,
           sum(w.net_sales)            AS net_sales,
           sum(w.gross_sales)          AS gross_sales,
           sum(w.guest_count)          AS guest_count,
           sum(w.check_count)          AS check_count,
           sum(w.labor_hours)          AS labor_hours,
           sum(w.labor_cost)           AS labor_cost,
           sum(w.total_deposit)        AS total_deposit,
           sum(w.paid_in)              AS paid_in,
           sum(w.paid_out)             AS paid_out,
           sum(w.over_short)           AS over_short,
           sum(w.discounts)            AS discounts,
           sum(w.comps)                AS comps,
           sum(w.coupons)              AS coupons,
           sum(w.total_discounts)      AS total_discounts,
           sum(w.gift_cards_redeemed)  AS gift_cards_redeemed,
           sum(w.gift_cards_sold)      AS gift_cards_sold,
           sum(w.tips_declared)        AS tips_declared,
           sum(w.credit_card_tips)     AS credit_card_tips,
           sum(w.void_count)           AS void_count,
           sum(w.void_amount)          AS void_amount,
           sum(w.forecasted_sales)     AS forecasted_sales,
           sum(w.forecast_sales_base)  AS forecast_sales_base,
           sum(w.days_with_data)       AS days_with_data
      FROM v_r365_store_weekly_llm w
     WHERE w.fiscal_year IS NOT NULL
     GROUP BY 1, 2, 3, 4, 5
)
SELECT a.establishment_id, a.store_name, a.fiscal_year, a.period_number,
       a.period_label, p.start_date, p.end_date, p.period_status,
       (p.end_date - p.start_date + 1) / 7                 AS weeks_in_period,
       a.weeks_loaded, a.weeks_complete,
       (a.weeks_loaded = (p.end_date - p.start_date + 1) / 7
        AND a.weeks_complete = a.weeks_loaded)             AS period_fully_covered,
       a.first_week_start, a.last_week_end,
       a.net_sales, a.gross_sales, a.guest_count, a.check_count,
       CASE WHEN a.check_count > 0 THEN a.net_sales / a.check_count END   AS avg_check,
       CASE WHEN a.guest_count > 0 THEN a.net_sales / a.guest_count END   AS avg_per_guest,
       a.labor_hours, a.labor_cost,
       CASE WHEN a.net_sales <> 0 THEN a.labor_cost / a.net_sales * 100 END AS labor_pct,
       CASE WHEN a.labor_hours > 0 THEN a.net_sales / a.labor_hours END   AS sales_per_labor_hour,
       a.total_deposit, a.paid_in, a.paid_out, a.over_short,
       a.discounts, a.comps, a.coupons, a.total_discounts,
       CASE WHEN a.net_sales <> 0 THEN a.total_discounts / a.net_sales * 100 END AS discount_pct,
       a.gift_cards_redeemed, a.gift_cards_sold,
       a.tips_declared, a.credit_card_tips,
       a.void_count, a.void_amount,
       a.forecasted_sales, a.forecast_sales_base,
       CASE WHEN a.forecasted_sales > 0
            THEN a.forecast_sales_base - a.forecasted_sales END            AS sales_vs_forecast_amount,
       CASE WHEN a.forecasted_sales > 0
            THEN (a.forecast_sales_base - a.forecasted_sales)
                 / a.forecasted_sales * 100 END                            AS sales_vs_forecast_pct,
       a.days_with_data
  FROM agg a
  -- the _llm calendar view carries period_status (Chicago-today derived)
  JOIN v_ayg_business_periods_llm p
    ON p.fiscal_year = a.fiscal_year AND p.period_number = a.period_number;

COMMENT ON VIEW v_r365_store_period_llm IS
  'Store x AYG fiscal period rollup of the OFFICIAL weekly rows (4 weeks per '
  'period). Ratios recomputed from summed numerators, never averaged. '
  'period_fully_covered = false means a current or partly loaded period: '
  'label it partial and never compare it against a full period''s totals.';

COMMIT;
