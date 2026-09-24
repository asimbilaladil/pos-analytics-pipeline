-- 43_hme_llm_views.sql
-- Safe, LLM-facing HME relations. VIEWS ONLY -- this migration grants nothing.
-- The laynes_ro GRANT is deliberately split into 44_hme_llm_grants.sql so that
-- creating the shape and opening the boundary are two separate, separately
-- reviewable decisions.
--
-- Design contract
-- ---------------
-- 1. Only ACTIVE VERIFIED HME->Revel mappings are visible. OUT_OF_SCOPE stores
--    (23 of 34, including Frisco/store 3) are structurally excluded: they have
--    no revel_establishment_id to join on and the WHERE clause removes them.
-- 2. hme_store_number is NEVER exposed. It is HME-internal identity, it is text
--    ('00111', '1158614') and it is NOT the Revel establishment_id. Withholding
--    it makes a numeric or name-based cross-system join unexpressible rather
--    than merely discouraged.
-- 3. No PII. No hme_* table holds an employee, customer, user, email or phone
--    column, so this layer is scoped, not redacted.
-- 4. No Power BI provenance: run_id, ingested_at, hme_store_name_seen,
--    query_hash, model_id, source_reports, tenant_scope_detail, freshness_detail
--    and warnings all stop here.
-- 5. Percentages are converted from the stored 0-1 fraction to 0-100, matching
--    the existing Laynes convention (v_entree_coverage.pct_orders_resolved,
--    chat_sql._pct_of). Storing 0-1 and presenting 0-100 is the single most
--    likely source of a silent 100x error, so the conversion lives in the view.
-- 6. Seconds stay seconds. Nothing is auto-converted to minutes.
-- 7. total_orders is renamed drive_thru_cars. HME counts LANE VEHICLE
--    OBSERVATIONS, not Revel POS orders; the column name is the first line of
--    defence against that conflation.
--
-- Not in this version (DECISION 1): Total Cars Goal A/B/C/D/E. Those values are
-- extracted into the facts file but never persisted canonically, and ingestion
-- is deliberately not reopened for them here. metric_availability advertises
-- total_cars_goal_a_e = false.

BEGIN;

-- ---------------------------------------------------------------------------
-- Daily drive-thru metrics
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_hme_store_daily_llm AS
SELECT m.revel_establishment_id                  AS establishment_id,
       m.revel_store_name                        AS store_name,
       d.business_date,
       d.total_orders                            AS drive_thru_cars,
       d.regular_orders                          AS regular_cars,
       d.disastrous_orders                       AS disastrous_cars,
       round(d.disastrous_pct * 100, 2)          AS disastrous_pct,
       d.lane_total_avg_seconds,
       d.lane_queue_avg_seconds,
       d.lane_total_2_avg_seconds,
       d.lane_total_goal_d_seconds,
       d.total_cars                              AS trend_total_cars,
       d.trend_avg_time_seconds
  FROM hme_store_daily d
  JOIN hme_store_mapping m ON m.hme_store_number = d.hme_store_number
 WHERE m.mapping_status = 'VERIFIED' AND m.active;

COMMENT ON VIEW v_hme_store_daily_llm IS
  'LLM-safe daily drive-thru metrics for ACTIVE VERIFIED HME->Revel stores only. '
  'Grain: establishment_id + business_date (America/Chicago calendar date). '
  'HME cars are lane vehicle observations and are NOT Revel POS orders.';
COMMENT ON COLUMN v_hme_store_daily_llm.drive_thru_cars IS
  'Lane vehicle observations for the day. NOT Revel POS orders -- never present '
  'this as an order or transaction count, and never sum it with Revel orders.';
COMMENT ON COLUMN v_hme_store_daily_llm.regular_cars IS
  'HME "regular" bucket. regular_cars + disastrous_cars does not always equal '
  'drive_thru_cars: the buckets are independent DAX measures and a small '
  'residual (7 on 2026-09-19) is a source property, not a load error.';
COMMENT ON COLUMN v_hme_store_daily_llm.disastrous_pct IS
  'Percent 0-100 (stored 0-1 upstream).';
COMMENT ON COLUMN v_hme_store_daily_llm.lane_total_avg_seconds IS
  'Mean total lane time per car, in SECONDS.';
COMMENT ON COLUMN v_hme_store_daily_llm.lane_queue_avg_seconds IS
  'Mean queue component, in SECONDS.';
COMMENT ON COLUMN v_hme_store_daily_llm.lane_total_2_avg_seconds IS
  'Second lane-total variant as reported by HME, in SECONDS.';
COMMENT ON COLUMN v_hme_store_daily_llm.lane_total_goal_d_seconds IS
  'Lane total measured against the Goal D threshold, in SECONDS.';
COMMENT ON COLUMN v_hme_store_daily_llm.trend_total_cars IS
  'Trend dashboard car count. Equals drive_thru_cars once the source has '
  'settled; a disagreement is the reconciliation signal, not a separate metric.';
COMMENT ON COLUMN v_hme_store_daily_llm.trend_avg_time_seconds IS
  'Trend average time, in SECONDS.';

-- ---------------------------------------------------------------------------
-- Daily outlier / data-quality metrics
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_hme_outliers_daily_llm AS
SELECT m.revel_establishment_id                  AS establishment_id,
       m.revel_store_name                        AS store_name,
       o.business_date,
       o.all_car_records,
       o.car_departures,
       o.total_outliers,
       round(o.outlier_pct         * 100, 2)     AS outlier_pct,
       round(o.pull_in_pct         * 100, 2)     AS pull_in_pct,
       round(o.pull_out_pct        * 100, 2)     AS pull_out_pct,
       round(o.max_over_delete_pct * 100, 2)     AS max_over_delete_pct,
       round(o.discard_pct         * 100, 2)     AS discard_pct,
       round(o.manual_delete_pct   * 100, 2)     AS manual_delete_pct
  FROM hme_outliers_daily o
  JOIN hme_store_mapping m ON m.hme_store_number = o.hme_store_number
 WHERE m.mapping_status = 'VERIFIED' AND m.active;

COMMENT ON VIEW v_hme_outliers_daily_llm IS
  'LLM-safe daily drive-thru outlier/data-quality metrics for ACTIVE VERIFIED '
  'stores only. All *_pct columns are 0-100.';
COMMENT ON COLUMN v_hme_outliers_daily_llm.car_departures IS
  'Departure count from the Outliers report. Reconciles to '
  'v_hme_store_daily_llm.drive_thru_cars once the source has settled; the '
  'Outliers report can lag by hours after close.';

-- ---------------------------------------------------------------------------
-- Effective-dated goal thresholds (HISTORY, not current-only)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_hme_goal_history_llm AS
SELECT m.revel_establishment_id                  AS establishment_id,
       m.revel_store_name                        AS store_name,
       g.effective_from_date,
       g.effective_to_date,
       g.goal_a_seconds,
       g.goal_b_seconds,
       g.goal_c_seconds,
       g.goal_d_seconds
  FROM hme_goal_history g
  JOIN hme_store_mapping m ON m.hme_store_number = g.hme_store_number
 WHERE m.mapping_status = 'VERIFIED' AND m.active;

COMMENT ON VIEW v_hme_goal_history_llm IS
  'Effective-dated drive-thru goal thresholds (SECONDS) for ACTIVE VERIFIED '
  'stores. This is HISTORY, not current-only: a date-scoped question must pick '
  'the row in force on that business_date using '
  'business_date >= effective_from_date AND (effective_to_date IS NULL OR '
  'business_date <= effective_to_date). effective_to_date IS NULL means the row '
  'is currently in force. Goals are NEVER backdated before the first observed '
  'period: no goal is evidenced before the earliest effective_from_date, and a '
  'question about an earlier date must be answered "unavailable", not with the '
  'oldest known goal.';
COMMENT ON COLUMN v_hme_goal_history_llm.effective_to_date IS
  'Inclusive end of the period. NULL = still in force.';

-- ---------------------------------------------------------------------------
-- Per-day completeness / reconciliation metadata
-- ---------------------------------------------------------------------------
-- Only 'succeeded' and 'partial' are analytical outcomes. 'failed' and
-- 'aborted_tenant_scope' describe a run that must never become LLM truth, and
-- an UNRECOGNISED future status is excluded too: the IN list fails closed,
-- whereas status <> 'failed' would silently admit any new security status.
CREATE OR REPLACE VIEW v_hme_day_completeness_llm AS
WITH current_run AS (
    SELECT DISTINCT ON (business_date)
           business_date, status, reconciliation_ok, store_count,
           verified_store_count, out_of_scope_store_count, freshness_detail
      FROM hme_ingest_run
     WHERE status IN ('succeeded', 'partial')
     ORDER BY business_date, run_id DESC
), expectation AS (
    SELECT count(*)                                              AS expected_total,
           count(*) FILTER (WHERE mapping_status = 'VERIFIED')   AS expected_verified
      FROM hme_store_mapping WHERE active
)
SELECT r.business_date,
       r.status,
       -- Recorded truth when the loader stored it; otherwise derived by
       -- comparing the observed count against today's active allowlist. Runs
       -- before hme_load/1.4.0 did not persist the completeness blob, so the
       -- fallback is what makes historical days answerable. The caveat: a
       -- derived value is evaluated against the CURRENT allowlist, so changing
       -- the store set re-characterises old days.
       COALESCE((r.freshness_detail->'completeness'->>'complete')::boolean,
                r.store_count = e.expected_total)                AS source_complete,
       r.reconciliation_ok,
       r.store_count                                             AS observed_store_count,
       e.expected_total                                          AS expected_store_count,
       r.verified_store_count                                    AS verified_observed_count,
       e.expected_verified                                       AS verified_expected_count,
       (r.store_count = r.verified_store_count
                      + r.out_of_scope_store_count)              AS counts_consistent
  FROM current_run r CROSS JOIN expectation e;

COMMENT ON VIEW v_hme_day_completeness_llm IS
  'Per-business_date HME completeness and reconciliation metadata, from the '
  'latest ANALYTICAL run (status succeeded or partial) for that date. Failed '
  'and security-aborted runs are excluded and can never become LLM truth. '
  'source_complete and reconciliation_ok are INDEPENDENT axes: '
  'reconciliation_ok = true never implies the day is complete, and a day can be '
  'complete but unreconciled. A missing store is UNKNOWN, never zero.';
COMMENT ON COLUMN v_hme_day_completeness_llm.source_complete IS
  'FALSE means at least one expected store is absent from the source for this '
  'date. Absent stores must be reported as unavailable, never as 0 cars.';
COMMENT ON COLUMN v_hme_day_completeness_llm.reconciliation_ok IS
  'FALSE means cross-source checks disagreed (e.g. the Outliers report had not '
  'settled). Disclose this in any answer that uses the date.';
COMMENT ON COLUMN v_hme_day_completeness_llm.verified_observed_count IS
  'VERIFIED stores actually present. When this is below '
  'verified_expected_count, store-level HME answers for the affected date are '
  'not trustworthy and must be declined rather than estimated.';

COMMIT;
