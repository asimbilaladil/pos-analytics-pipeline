-- Migration 39: canonical HME drive-thru facts + the HME->Revel store mapper
--
-- Source of truth is the Power BI `explore/querydata` endpoint behind HME Cloud,
-- captured by the SEPARATE downloader application at /opt/hme-report-downloader.
-- PDF exports are audit artefacts only and are never parsed for analytics.
--
-- Why a mapper instead of a direct join
-- -------------------------------------
-- HME store identity is `Store Number`, a TEXT code whose leading zeros are
-- significant ('00111', '002', '004', '01'). It is NOT the Revel
-- establishment_id: three numeric collisions exist and none agree semantically
-- (HME 14 = Tyler vs Revel 14 = LCF Beaumont; HME 15 = Nederland vs Revel 15 =
-- LCF Shepherd; HME 26 = Airtex vs Revel 26 = LCF Nederland). Display names are
-- also unstable -- six codes differ only by letter case between reports
-- (BEAUMONT/Beaumont, GREENS/Greens, BAUM/Baum, Parma/parma,
-- Missouri City/missouri city, TYLER/Tyler). So identity is maintained
-- explicitly here and cross-system analytics may join ONLY through this table,
-- ONLY on rows that a human has marked VERIFIED.
--
-- Tenant safety
-- -------------
-- HME's Power BI model is multi-tenant with no row-level security on the embed
-- token; isolation comes only from report filters. hme_store_mapping doubles as
-- the tenant allowlist: the extractor fails closed if a result contains a store
-- number absent from this table. See hme_ingest_run.tenant_scope_ok.
--
-- Grants: deliberately NONE. laynes_ro (the LLM-facing role) gets no access to
-- these relations. Safe LLM-facing views are a separate, later decision.

BEGIN;

-- ---------------------------------------------------------------------------
-- Store identity mapper
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hme_store_mapping (
    hme_store_number        text        PRIMARY KEY,
    hme_store_name          text        NOT NULL,
    hme_group_name          text        NULL,
    revel_establishment_id  bigint      NULL REFERENCES establishments(id),
    revel_store_name        text        NULL,
    mapping_status          text        NOT NULL,
    mapping_confidence      text        NULL,
    mapping_basis           text        NULL,
    verified_by             text        NULL,
    verified_at             timestamptz NULL,
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_seen_at            timestamptz NOT NULL DEFAULT now(),
    active                  boolean     NOT NULL DEFAULT true,

    CONSTRAINT hme_store_mapping_status_check CHECK (
        mapping_status IN ('VERIFIED','CANDIDATE','UNMAPPED','AMBIGUOUS','OUT_OF_SCOPE')),
    CONSTRAINT hme_store_mapping_confidence_check CHECK (
        mapping_confidence IS NULL OR mapping_confidence IN ('high','medium','low')),
    -- A VERIFIED row is structurally impossible without a human signature.
    CONSTRAINT hme_store_mapping_verified_requires_proof CHECK (
        mapping_status <> 'VERIFIED' OR (
            revel_establishment_id IS NOT NULL
            AND verified_by IS NOT NULL
            AND verified_at IS NOT NULL)),
    -- Store numbers are text, but must never be blank or padded.
    CONSTRAINT hme_store_mapping_number_shape CHECK (
        hme_store_number = btrim(hme_store_number) AND length(hme_store_number) > 0)
);

-- Partial unique index (NOT UNIQUE(revel_establishment_id, active)): keeps
-- historical inactive mappings while preventing two ACTIVE HME stores from
-- claiming the same Revel establishment.
CREATE UNIQUE INDEX IF NOT EXISTS hme_store_mapping_one_active_revel
    ON hme_store_mapping (revel_establishment_id)
    WHERE active = TRUE AND revel_establishment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS hme_store_mapping_status_idx
    ON hme_store_mapping (mapping_status) WHERE active = TRUE;

COMMENT ON TABLE hme_store_mapping IS
  'Maintained HME->Revel store identity. Also the tenant allowlist for HME extraction. '
  'PK is the HME Store Number as TEXT (leading zeros significant). Cross-system joins '
  'are permitted only via mapping_status = ''VERIFIED'' AND active.';
COMMENT ON COLUMN hme_store_mapping.hme_store_number IS
  'HME Device Event Statistics[Store Number]. TEXT: ''00111'' and ''111'' are different stores.';
COMMENT ON COLUMN hme_store_mapping.hme_store_name IS
  'Latest observed HME display name. Informational only -- casing is not stable across reports.';

-- ---------------------------------------------------------------------------
-- Ingestion provenance
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hme_ingest_run (
    run_id                  bigserial   PRIMARY KEY,
    business_date           date        NOT NULL,
    requested_date          date        NOT NULL,   -- the date literal actually sent to HME
    source                  text        NOT NULL,   -- 'powerbi_querydata'
    source_reports          text[]      NOT NULL,
    model_id                text        NULL,
    query_hash              text        NULL,       -- sha256 of the semantic queries used
    started_at              timestamptz NOT NULL,
    finished_at             timestamptz NULL,
    tenant_scope_ok         boolean     NULL,
    tenant_scope_detail     jsonb       NULL,
    store_count             integer     NULL,
    rows_received           integer     NULL,
    rows_written            integer     NULL,
    reconciliation_ok       boolean     NULL,
    reconciliation_detail   jsonb       NULL,
    status                  text        NOT NULL DEFAULT 'running',
    warnings                text[]      NOT NULL DEFAULT '{}',

    CONSTRAINT hme_ingest_run_status_check CHECK (
        status IN ('running','succeeded','failed','aborted_tenant_scope')),
    -- business_date must be the date we explicitly asked HME for; never derived
    -- from a UTC folder name, an export filename, or a PDF's printed range.
    CONSTRAINT hme_ingest_run_date_contract CHECK (business_date = requested_date)
);
CREATE INDEX IF NOT EXISTS hme_ingest_run_date_idx ON hme_ingest_run (business_date DESC);

COMMENT ON TABLE hme_ingest_run IS
  'One row per HME extraction attempt. Never stores embed tokens, id_token, ctx_token, '
  'cookies or passwords. business_date is the date literal sent to Power BI.';

-- ---------------------------------------------------------------------------
-- Daily store fact
--   grain : one row per HME store per HME business date
--   PK    : (hme_store_number, business_date)
--   source: Performance Analysis store pivot (Detector Event Data Hour),
--           plus total_cars/avg from the Trend Dashboard store query
--   units : *_seconds are whole seconds; *_pct are RATIOS (0.1544 = 15.44%)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hme_store_daily (
    hme_store_number            text    NOT NULL REFERENCES hme_store_mapping(hme_store_number),
    business_date               date    NOT NULL,
    -- HME lane-visit counts. NOT Revel POS orders: source terminology kept.
    total_orders                integer NULL,
    regular_orders              integer NULL,
    disastrous_orders           integer NULL,
    disastrous_pct              numeric(9,6) NULL,
    lane_total_avg_seconds      integer NULL,
    lane_queue_avg_seconds      integer NULL,
    lane_total_2_avg_seconds    integer NULL,
    lane_total_goal_d_seconds   integer NULL,
    -- Independent Trend Dashboard measures for the same grain; kept separately
    -- so agreement with total_orders stays verifiable rather than assumed.
    total_cars                  integer NULL,
    trend_avg_time_seconds      integer NULL,
    hme_store_name_seen         text    NULL,
    run_id                      bigint  NULL REFERENCES hme_ingest_run(run_id),
    ingested_at                 timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (hme_store_number, business_date),
    CONSTRAINT hme_store_daily_pct_ratio CHECK (
        disastrous_pct IS NULL OR (disastrous_pct >= 0 AND disastrous_pct <= 1)),
    CONSTRAINT hme_store_daily_nonneg CHECK (
        coalesce(total_orders,0) >= 0 AND coalesce(regular_orders,0) >= 0
        AND coalesce(disastrous_orders,0) >= 0 AND coalesce(total_cars,0) >= 0)
);
CREATE INDEX IF NOT EXISTS hme_store_daily_date_idx ON hme_store_daily (business_date DESC);

COMMENT ON TABLE hme_store_daily IS
  'Grain: HME store x HME business date. Upsert on (hme_store_number, business_date). '
  'disastrous_pct is a ratio, not a percentage. NOTE: regular_orders + disastrous_orders '
  'does not necessarily equal total_orders (independent DAX buckets; observed delta 5 of '
  '5283 on 2026-09-14). This is recorded, not corrected.';
COMMENT ON COLUMN hme_store_daily.total_orders IS
  'HME "Total Order" = drive-thru lane visits. Deliberately NOT renamed to match Revel orders.';
COMMENT ON COLUMN hme_store_daily.lane_total_goal_d_seconds IS
  'HME "Threshold" -- the Lane Total Goal D used to classify a visit as disastrous, as '
  'reported for this date''s query. Goal history lives in hme_goal_history.';

-- ---------------------------------------------------------------------------
-- Daily outliers fact
--   grain : one row per HME store per business date
--   source: Outliers (Device Event Statistics)
--   note  : outlier TYPES stay separate; never collapsed into a "bad order" total
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hme_outliers_daily (
    hme_store_number    text    NOT NULL REFERENCES hme_store_mapping(hme_store_number),
    business_date       date    NOT NULL,
    all_car_records     integer NULL,
    car_departures      integer NULL,
    total_outliers      integer NULL,
    outlier_pct         numeric(9,6) NULL,
    pull_in_pct         numeric(9,6) NULL,
    pull_out_pct        numeric(9,6) NULL,
    max_over_delete_pct numeric(9,6) NULL,
    discard_pct         numeric(9,6) NULL,
    manual_delete_pct   numeric(9,6) NULL,
    hme_store_name_seen text    NULL,
    run_id              bigint  NULL REFERENCES hme_ingest_run(run_id),
    ingested_at         timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (hme_store_number, business_date),
    CONSTRAINT hme_outliers_daily_ratios CHECK (
        (outlier_pct         IS NULL OR outlier_pct         BETWEEN 0 AND 1) AND
        (pull_in_pct         IS NULL OR pull_in_pct         BETWEEN 0 AND 1) AND
        (pull_out_pct        IS NULL OR pull_out_pct        BETWEEN 0 AND 1) AND
        (max_over_delete_pct IS NULL OR max_over_delete_pct BETWEEN 0 AND 1) AND
        (discard_pct         IS NULL OR discard_pct         BETWEEN 0 AND 1) AND
        (manual_delete_pct   IS NULL OR manual_delete_pct   BETWEEN 0 AND 1))
);
CREATE INDEX IF NOT EXISTS hme_outliers_daily_date_idx ON hme_outliers_daily (business_date DESC);

COMMENT ON TABLE hme_outliers_daily IS
  'Grain: HME store x business date. NULL in a *_pct breakdown column means HME returned no '
  'breakdown row for that store on that date (typically zero outliers), NOT zero percent.';

-- ---------------------------------------------------------------------------
-- Goal history (observation-effective, NOT backdated)
--   The querydata goal projection exposes NO date column, so goals can only be
--   observed as-of an extraction. Rows are opened when a value changes and
--   closed when it changes again. History before the first observation is
--   genuinely unavailable and is not fabricated.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hme_goal_history (
    hme_store_number    text    NOT NULL REFERENCES hme_store_mapping(hme_store_number),
    effective_from_date date    NOT NULL,   -- first business_date this value was OBSERVED
    effective_to_date   date    NULL,       -- NULL = current
    goal_a_seconds      integer NULL,
    goal_b_seconds      integer NULL,
    goal_c_seconds      integer NULL,
    goal_d_seconds      integer NULL,
    observed_run_id     bigint  NULL REFERENCES hme_ingest_run(run_id),
    recorded_at         timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (hme_store_number, effective_from_date),
    CONSTRAINT hme_goal_history_range CHECK (
        effective_to_date IS NULL OR effective_to_date >= effective_from_date)
);
CREATE UNIQUE INDEX IF NOT EXISTS hme_goal_history_one_open
    ON hme_goal_history (hme_store_number) WHERE effective_to_date IS NULL;

COMMENT ON TABLE hme_goal_history IS
  'Effective-dated HME goal thresholds. effective_from_date is the business_date on which the '
  'value was first OBSERVED -- the source exposes no per-date goal, so earlier history cannot '
  'be reconstructed and is never backdated.';

-- ---------------------------------------------------------------------------
-- Cross-system access: the ONLY sanctioned join path
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_hme_store_daily_verified AS
SELECT m.revel_establishment_id,
       m.revel_store_name,
       d.hme_store_number,
       d.business_date,
       d.total_orders,
       d.regular_orders,
       d.disastrous_orders,
       d.disastrous_pct,
       d.lane_total_avg_seconds,
       d.lane_queue_avg_seconds,
       d.lane_total_2_avg_seconds,
       d.lane_total_goal_d_seconds,
       d.total_cars,
       d.trend_avg_time_seconds
FROM hme_store_daily d
JOIN hme_store_mapping m ON m.hme_store_number = d.hme_store_number
WHERE m.mapping_status = 'VERIFIED' AND m.active = TRUE;

COMMENT ON VIEW v_hme_store_daily_verified IS
  'The only sanctioned HME<->Revel join path. Excludes CANDIDATE/UNMAPPED/AMBIGUOUS/'
  'OUT_OF_SCOPE stores. Never join HME to Revel by numeric equality, display name, fuzzy '
  'name, or city at query time.';

-- Revel-side coverage gap. The mapper is keyed on the HME store number, so a
-- Revel establishment with no HME timer cannot be represented as a row there
-- without inventing a fake HME key. It is expressed here instead.
CREATE OR REPLACE VIEW v_hme_revel_coverage AS
SELECT e.id   AS revel_establishment_id,
       e.name AS revel_store_name,
       e.city,
       e.active AS revel_active,
       m.hme_store_number,
       m.hme_store_name,
       COALESCE(m.mapping_status, 'NO_HME_MAPPING') AS coverage_status
FROM establishments e
LEFT JOIN hme_store_mapping m
       ON m.revel_establishment_id = e.id AND m.active = TRUE;

COMMENT ON VIEW v_hme_revel_coverage IS
  'Revel-side view of HME coverage. coverage_status = NO_HME_MAPPING marks a Revel '
  'establishment with no active HME mapping (e.g. Revel 48 LCF Downtown Houston, pending '
  'confirmation of whether that location has an HME timer at all).';

COMMIT;
