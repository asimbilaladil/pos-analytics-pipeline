-- 56_r365_financial.sql
-- R365 FINANCIAL (GL / P&L expense) data, consumed from the R365 source VPS's
-- authenticated financial API -- NOT from Restaurant365 directly. The source
-- VPS owns GL ingestion, approval status, fiscal-period assignment, account ->
-- category mapping and its own daily (06:00) / weekly-repair (Mon 05:30)
-- syncs. This VPS only pulls the published results and stores them locally.
--
-- VIEWS ARE CREATED HERE BUT GRANTED NOTHING -- the laynes_ro GRANT lives in
-- 57_r365_financial_llm_grants.sql, the 43/44, 49/50, 54/55 convention.
--
-- Source endpoints (same host and bearer credential as /weekly-data):
--   GET /financial/gl/accounts             chart of accounts + mapping status
--   GET /financial/expenses/summary        store x fiscal period x category
--   GET /financial/expenses                approved GL detail lines (paged)
--
-- These tables are separate from the R365 OPERATIONAL tables of migration 54
-- (r365_store_daily / r365_store_weekly): different source, different grain,
-- different meaning. Nothing here is joined into those tables.
--
-- ---------------------------------------------------------------------------
-- SOURCE SEMANTICS (proven against the live API on 2026-10-16)
-- ---------------------------------------------------------------------------
--   * Only APPROVED transactions are summarised (includeUnapproved = false).
--   * A category total counts VERIFIED accounts only. Today exactly one account
--     is VERIFIED: 6200-03 Electric -> ELECTRICITY. Gas, Water, Telephone,
--     Cable/Internet and Waste Removal are CANDIDATE; ~380 accounts (rent,
--     repairs, insurance, food cost, ...) are UNMAPPED. Unverified activity is
--     reported by the source as category 'UNMAPPED' plus an account-level
--     unverifiedCandidates list. It is NEVER folded into a trusted category.
--   * locationsWithNoActivity lists store-periods with NO posting for the
--     requested category. That is NOT $0: utility bills post roughly monthly
--     and can land in an adjacent period or not be entered yet. Such a store-
--     period is stored as a NO_ACTIVITY row with expense_amount NULL.
--   * fiscalYear / periodNumber are assigned by the source only for dates
--     inside the official AYG calendar (FY2026 / FY2027). Earlier GL activity
--     (back to 2024-04-07) has NO fiscal period. It is kept at account x
--     calendar-month grain and is never given an invented period.
--   * MIGRATED_SUMMARY (HIST DATA IMPORT journal entries, through 2026-03-25)
--     is migrated P&L history with little transaction detail; transaction
--     counts there describe journal entries, not bills.
--
-- STORAGE CONVENTIONS (as migration 54)
--   * Money is unconstrained NUMERIC, stored exactly as sent (parsed with
--     Decimal). Counts are integer.
--   * Missing is absence or NULL, never zero.
--   * Store identity comes ONLY from r365_store_mapping (ACTIVE + VERIFIED,
--     keyed by the R365 locationId UUID). Never from a name.

BEGIN;

-- ---------------------------------------------------------------------------
-- Ingest run provenance: ONE ROW PER (batch, target)
--   target_kind ACCOUNTS       the chart of accounts
--               PERIOD         one fiscal period: category summaries, the
--                              unverified-candidate list and the period's GL
--                              detail rolled up per account
--               HISTORY        GL detail with no fiscal period (pre-FY2026),
--                              rolled up per account x calendar month
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS r365_financial_ingest_run (
    id                       bigserial   PRIMARY KEY,
    batch_id                 text        NOT NULL,
    trigger                  text        NOT NULL,
    target_kind              text        NOT NULL,
    requested_fiscal_year    integer,
    requested_period_number  integer,
    requested_start          date,
    requested_end            date,
    started_at               timestamptz NOT NULL DEFAULT now(),
    completed_at             timestamptz,
    status                   text        NOT NULL DEFAULT 'RUNNING',
    rows_received            integer,
    rows_upserted            integer,
    rows_changed             integer,
    rows_deleted             integer,
    no_activity_rows         integer,
    candidate_rows           integer,
    detail_lines_received    integer,
    account_period_rows      integer,
    unmapped_locations       jsonb,
    unaccounted_stores       jsonb,
    reconciliation_ok        boolean,
    reconciliation           jsonb,
    source_data_status       jsonb,        -- {category: dataStatus} from the source
    source_status_reasons    jsonb,
    source_last_sync_at      timestamptz,  -- sync.lastSuccessfulSyncAt
    source_sync_stale        boolean,      -- sync.stale
    source_min_date          date,
    source_max_date          date,
    schema_warnings          jsonb,
    http_requests            integer,
    fetch_seconds            numeric,
    payload_sha256           text,
    raw_archive_path         text,
    loader_version           text        NOT NULL,
    error_code               text,
    error_message            text,
    CONSTRAINT r365_fin_run_status_check
        CHECK (status IN ('RUNNING', 'COMPLETE', 'PARTIAL', 'FAILED')),
    CONSTRAINT r365_fin_run_trigger_check
        CHECK (trigger IN ('scheduled', 'backfill', 'manual')),
    CONSTRAINT r365_fin_run_target_check
        CHECK (target_kind IN ('ACCOUNTS', 'PERIOD', 'HISTORY')),
    CONSTRAINT r365_fin_run_period_shape
        CHECK ((target_kind = 'PERIOD') =
               (requested_fiscal_year IS NOT NULL AND requested_period_number IS NOT NULL)),
    CONSTRAINT r365_fin_run_finished_has_time
        CHECK (status = 'RUNNING' OR completed_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS r365_fin_run_period_idx
    ON r365_financial_ingest_run (requested_fiscal_year, requested_period_number, id DESC);

COMMENT ON TABLE r365_financial_ingest_run IS
  'One row per (batch, target). Never holds credentials. Internal: '
  'error_message, raw_archive_path and payload_sha256 never cross the LLM '
  'boundary.';

-- ---------------------------------------------------------------------------
-- Chart of accounts, as published by the source (mapping decided THERE)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS r365_financial_account (
    account_id             text        PRIMARY KEY,   -- R365 GL account UUID
    account_number         text        NOT NULL,
    account_name           text        NOT NULL,
    account_type           text,
    parent_account_id      text,
    parent_account_number  text,
    analytics_category     text,                      -- NULL when UNMAPPED
    mapping_status         text        NOT NULL,
    ingest_run_id          bigint      NOT NULL REFERENCES r365_financial_ingest_run(id),
    first_loaded_at        timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT r365_fin_account_status_check
        CHECK (mapping_status IN ('VERIFIED', 'CANDIDATE', 'UNMAPPED')),
    CONSTRAINT r365_fin_account_verified_has_category
        CHECK (mapping_status <> 'VERIFIED' OR analytics_category IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS r365_fin_account_number_idx
    ON r365_financial_account (account_number);

COMMENT ON TABLE r365_financial_account IS
  'R365 chart of accounts with the SOURCE''s analytics_category and '
  'mapping_status. Only VERIFIED accounts make a category trusted.';

-- ---------------------------------------------------------------------------
-- Period expense fact: (r365_location_id, fiscal_year, period_number,
-- analytics_category). One row per store-period-category the source reported,
-- INCLUDING its 'UNMAPPED' bucket (kept, never exposed) and one NO_ACTIVITY
-- row per locationsWithNoActivity entry (expense_amount NULL).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS r365_financial_period_expense (
    r365_location_id          text        NOT NULL,
    fiscal_year               integer     NOT NULL,
    period_number             integer     NOT NULL,
    analytics_category        text        NOT NULL,
    -- VERIFIED-mapped establishment, or NULL for a non-store R365 entity
    -- (e.g. 'AYG Operation Inc'). Resolved from r365_store_mapping at load.
    establishment_id          integer     REFERENCES establishments(id),
    store_name                text        NOT NULL,   -- source locationName (audit only)
    period_start              date        NOT NULL,
    period_end                date        NOT NULL,
    row_kind                  text        NOT NULL,   -- ACTIVITY | NO_ACTIVITY
    expense_amount            numeric,                -- NULL <=> NO_ACTIVITY
    approved_amount           numeric,
    transaction_count         integer,
    account_count             integer,
    source_quality            text,                   -- TRANSACTION_DETAIL | MIGRATED_SUMMARY | ...
    transaction_detail_amount numeric,
    migrated_summary_amount   numeric,
    data_status               text        NOT NULL,   -- source dataStatus, or NO_ACTIVITY
    coverage_start            date,
    coverage_end              date,
    transaction_detail_start  date,
    source_updated_at         timestamptz,            -- source sync.lastSuccessfulSyncAt
    ingest_run_id             bigint      NOT NULL REFERENCES r365_financial_ingest_run(id),
    first_loaded_at           timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT r365_fin_period_expense_pkey
        PRIMARY KEY (r365_location_id, fiscal_year, period_number, analytics_category),
    -- The period must exist in the OFFICIAL calendar: no invented periods.
    CONSTRAINT r365_fin_period_expense_period_fk
        FOREIGN KEY (fiscal_year, period_number)
        REFERENCES ayg_business_periods (fiscal_year, period_number),
    CONSTRAINT r365_fin_period_expense_kind
        CHECK (row_kind IN ('ACTIVITY', 'NO_ACTIVITY')),
    CONSTRAINT r365_fin_period_expense_missing_is_null
        CHECK ((row_kind = 'NO_ACTIVITY') = (expense_amount IS NULL)),
    CONSTRAINT r365_fin_period_expense_no_activity_status
        CHECK ((row_kind = 'NO_ACTIVITY') = (data_status = 'NO_ACTIVITY')),
    CONSTRAINT r365_fin_period_expense_counts_nonneg
        CHECK (coalesce(transaction_count, 0) >= 0 AND coalesce(account_count, 0) >= 0),
    CONSTRAINT r365_fin_period_expense_dates
        CHECK (period_end >= period_start)
);
CREATE INDEX IF NOT EXISTS r365_fin_period_expense_period_idx
    ON r365_financial_period_expense (fiscal_year, period_number, analytics_category);
CREATE INDEX IF NOT EXISTS r365_fin_period_expense_est_idx
    ON r365_financial_period_expense (establishment_id);

COMMENT ON TABLE r365_financial_period_expense IS
  'Source summary rows per R365 location x fiscal period x analytics category, '
  'values verbatim. NO_ACTIVITY rows (expense_amount NULL) record that the '
  'source reported no posting -- unknown, never $0. The UNMAPPED bucket is '
  'kept for audit and is never exposed as a category.';

-- ---------------------------------------------------------------------------
-- Unverified candidates: account-level amounts the source refuses to fold into
-- a category (gas, water, telephone, cable/internet, waste removal).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS r365_financial_candidate_expense (
    r365_location_id     text        NOT NULL,
    fiscal_year          integer     NOT NULL,
    period_number        integer     NOT NULL,
    account_number       text        NOT NULL,
    account_name         text        NOT NULL,
    candidate_category   text,
    establishment_id     integer     REFERENCES establishments(id),
    expense_amount       numeric     NOT NULL,
    ingest_run_id        bigint      NOT NULL REFERENCES r365_financial_ingest_run(id),
    first_loaded_at      timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT r365_fin_candidate_pkey
        PRIMARY KEY (r365_location_id, fiscal_year, period_number, account_number),
    CONSTRAINT r365_fin_candidate_period_fk
        FOREIGN KEY (fiscal_year, period_number)
        REFERENCES ayg_business_periods (fiscal_year, period_number)
);

COMMENT ON TABLE r365_financial_candidate_expense IS
  'Source unverifiedCandidates: CANDIDATE-mapped account amounts per store-'
  'period. Unverified accounting data -- never a trusted category total.';

-- ---------------------------------------------------------------------------
-- Account-level roll-up of the approved GL detail lines, so every account
-- (rent, repairs, insurance, food cost, ...) is preserved locally without
-- copying individual transactions. Buckets:
--   FISCAL_PERIOD   lines the SOURCE assigned to a fiscal period
--   CALENDAR_MONTH  lines with no fiscal period (pre-FY2026 history); grouped
--                   by accounting-date month, with NO fiscal labels
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS r365_financial_account_period (
    r365_location_id         text        NOT NULL,
    gl_account_id            text        NOT NULL,
    bucket_key               text        NOT NULL,   -- 'FY2026-P10' | 'CM-2024-04'
    bucket_kind              text        NOT NULL,
    fiscal_year              integer,
    period_number            integer,
    bucket_start             date        NOT NULL,
    bucket_end               date        NOT NULL,
    establishment_id         integer     REFERENCES establishments(id),
    location_name            text        NOT NULL,
    account_number           text        NOT NULL,
    analytics_category       text,                   -- as stamped on the lines
    mapping_status           text,
    expense_amount           numeric     NOT NULL,
    debit_amount             numeric     NOT NULL,
    credit_amount            numeric     NOT NULL,
    migrated_summary_amount  numeric     NOT NULL,
    line_count               integer     NOT NULL,
    transaction_count        integer     NOT NULL,
    min_accounting_date      date        NOT NULL,
    max_accounting_date      date        NOT NULL,
    ingest_run_id            bigint      NOT NULL REFERENCES r365_financial_ingest_run(id),
    first_loaded_at          timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT r365_fin_account_period_pkey
        PRIMARY KEY (r365_location_id, gl_account_id, bucket_key),
    CONSTRAINT r365_fin_account_period_kind
        CHECK (bucket_kind IN ('FISCAL_PERIOD', 'CALENDAR_MONTH')),
    -- A calendar-month bucket must NOT carry a fiscal period, and a fiscal
    -- bucket must carry one from the official calendar.
    CONSTRAINT r365_fin_account_period_fiscal_shape
        CHECK ((bucket_kind = 'FISCAL_PERIOD') =
               (fiscal_year IS NOT NULL AND period_number IS NOT NULL)),
    CONSTRAINT r365_fin_account_period_fk
        FOREIGN KEY (fiscal_year, period_number)
        REFERENCES ayg_business_periods (fiscal_year, period_number),
    CONSTRAINT r365_fin_account_period_dates
        CHECK (min_accounting_date >= bucket_start AND max_accounting_date <= bucket_end),
    CONSTRAINT r365_fin_account_period_counts
        CHECK (line_count > 0 AND transaction_count > 0)
);
CREATE INDEX IF NOT EXISTS r365_fin_account_period_bucket_idx
    ON r365_financial_account_period (bucket_kind, fiscal_year, period_number);

COMMENT ON TABLE r365_financial_account_period IS
  'Approved GL detail rolled up per R365 location x GL account x bucket. Raw '
  'accounting data for every account, mapped or not. Internal only.';

-- ===========================================================================
-- LLM-SAFE VIEWS (granted in 57). VERIFIED store mappings and VERIFIED
-- categories only. No R365 location id, no source location name, no run id,
-- no error text, no transaction detail.
-- ===========================================================================

-- Categories the assistant may treat as trusted analytics categories: those
-- carried by at least one VERIFIED account. The source excludes non-VERIFIED
-- accounts from category totals, so a VERIFIED category total is VERIFIED
-- accounts only.
CREATE OR REPLACE VIEW v_r365_financial_period_llm AS
WITH verified_categories AS (
    SELECT DISTINCT analytics_category
      FROM r365_financial_account
     WHERE mapping_status = 'VERIFIED' AND analytics_category IS NOT NULL
)
SELECT f.establishment_id,
       e.name                                   AS store_name,
       f.fiscal_year,
       f.period_number,
       p.period_label,
       f.period_start,
       f.period_end,
       p.period_status,
       f.analytics_category,
       f.expense_amount,
       f.transaction_count,
       f.account_count,
       f.data_status,
       (f.row_kind = 'ACTIVITY')                AS has_posting,
       f.source_quality
  FROM r365_financial_period_expense f
  JOIN r365_store_mapping m     ON m.r365_store_id = f.r365_location_id
                               AND m.establishment_id = f.establishment_id
  JOIN establishments e         ON e.id = f.establishment_id
  JOIN verified_categories c    ON c.analytics_category = f.analytics_category
  JOIN v_ayg_business_periods_llm p
         ON p.fiscal_year = f.fiscal_year AND p.period_number = f.period_number
 WHERE m.status = 'VERIFIED' AND m.active;

COMMENT ON VIEW v_r365_financial_period_llm IS
  'R365 FINANCIAL (GL) expense per store x AYG fiscal period x VERIFIED '
  'analytics category. Approved transactions only. has_posting = false / '
  'data_status = NO_ACTIVITY means NO POSTING WAS FOUND: expense_amount is NULL '
  'and that is UNKNOWN, NEVER $0 (bills can post in an adjacent period). '
  'Period grain only: no daily or hourly financial data exists.';
COMMENT ON COLUMN v_r365_financial_period_llm.expense_amount IS
  'Approved expense in dollars for the period. NULL when no posting exists '
  '(never treat as 0). SUM() ignores NULLs; count(expense_amount) = stores '
  'with a posting.';
COMMENT ON COLUMN v_r365_financial_period_llm.source_quality IS
  'TRANSACTION_DETAIL = real GL transactions. MIGRATED_SUMMARY = migrated P&L '
  'history (through 2026-03-25) with little transaction detail.';

-- Per period x VERIFIED category coverage and freshness, from the latest
-- analytical (COMPLETE/PARTIAL) PERIOD run; FAILED runs never become truth.
CREATE OR REPLACE VIEW v_r365_financial_coverage_llm AS
WITH latest AS (
    SELECT DISTINCT ON (requested_fiscal_year, requested_period_number)
           requested_fiscal_year, requested_period_number, status,
           source_data_status, source_last_sync_at, source_sync_stale,
           reconciliation_ok, completed_at
      FROM r365_financial_ingest_run
     WHERE target_kind = 'PERIOD' AND status IN ('COMPLETE', 'PARTIAL')
     ORDER BY requested_fiscal_year, requested_period_number, id DESC
), last_attempt AS (
    SELECT DISTINCT ON (requested_fiscal_year, requested_period_number)
           requested_fiscal_year, requested_period_number, status
      FROM r365_financial_ingest_run
     WHERE target_kind = 'PERIOD' AND status <> 'RUNNING'
     ORDER BY requested_fiscal_year, requested_period_number, id DESC
), cats AS (
    SELECT DISTINCT analytics_category
      FROM r365_financial_account
     WHERE mapping_status = 'VERIFIED' AND analytics_category IS NOT NULL
), counts AS (
    SELECT fiscal_year, period_number, analytics_category,
           count(*) FILTER (WHERE has_posting)     AS stores_with_posting,
           count(*) FILTER (WHERE NOT has_posting) AS stores_without_posting
      FROM v_r365_financial_period_llm
     GROUP BY 1, 2, 3
)
SELECT l.requested_fiscal_year                  AS fiscal_year,
       l.requested_period_number                AS period_number,
       p.period_label, p.start_date AS period_start, p.end_date AS period_end,
       p.period_status,
       c.analytics_category,
       l.source_data_status ->> c.analytics_category AS source_data_status,
       (SELECT count(*) FROM r365_store_mapping
         WHERE status = 'VERIFIED' AND active)  AS expected_store_count,
       COALESCE(n.stores_with_posting, 0)       AS stores_with_posting,
       COALESCE(n.stores_without_posting, 0)    AS stores_without_posting,
       l.reconciliation_ok,
       l.source_last_sync_at,
       l.source_sync_stale,
       (a.status = 'FAILED')                    AS latest_refresh_failed,
       l.completed_at                           AS loaded_at
  FROM latest l
  CROSS JOIN cats c
  JOIN v_ayg_business_periods_llm p
    ON p.fiscal_year = l.requested_fiscal_year
   AND p.period_number = l.requested_period_number
  LEFT JOIN last_attempt a
    ON a.requested_fiscal_year = l.requested_fiscal_year
   AND a.requested_period_number = l.requested_period_number
  LEFT JOIN counts n
    ON n.fiscal_year = l.requested_fiscal_year
   AND n.period_number = l.requested_period_number
   AND n.analytics_category = c.analytics_category;

COMMENT ON VIEW v_r365_financial_coverage_llm IS
  'One row per loaded fiscal period x VERIFIED category. source_data_status '
  'COMPLETE = the source considers the period closed and fully loaded; PARTIAL '
  '= open period or not fully loaded. stores_without_posting are unknown, '
  'never $0. A period absent here is NOT loaded.';

-- The chart of accounts, so the assistant can say WHICH accounting data exists
-- and that it is not (yet) a trusted category. Metadata only: no amounts.
CREATE OR REPLACE VIEW v_r365_financial_account_llm AS
SELECT a.account_number,
       a.account_name,
       a.account_type,
       a.parent_account_number,
       a.analytics_category,
       a.mapping_status,
       (a.mapping_status = 'VERIFIED')          AS category_trusted
  FROM r365_financial_account a;

COMMENT ON VIEW v_r365_financial_account_llm IS
  'R365 chart of accounts with the source mapping status. Metadata only, no '
  'amounts. Only mapping_status = VERIFIED accounts feed a trusted category; '
  'CANDIDATE / UNMAPPED accounts are accounting data with no trusted category.';

COMMIT;
