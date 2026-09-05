-- Migration 30: honest store-age / cohort context (A7)
--
-- Replaces open dates that were inferred from our own data with an explicit
-- "unknown", and separates STORE AGE from DATA HISTORY AGE.
--
-- WHY THE PREVIOUS INFERRED DATES ARE REMOVED ---------------------------------
-- Migration 21 seeded establishments.open_date from each store's first observed
-- order, guarded only by excluding dates at the backfill edge. Two stores got
-- dates that way, and v_store_cohort then derived weeks_since_open and a
-- honeymoon/ramp/mature bucket from them. Checked against the data:
--   * est 48 (Downtown Houston) was given open_date 2026-04-21 -- a day with
--     ZERO real transactions. Its first REAL order is 2026-04-29 and consists
--     of 4 orders totalling $14, followed by several near-empty days, with
--     recognisable trade only from 2026-05-07 ($1,513 / 91 orders). The early
--     rows are staff testing, not an opening.
--   * est 54 (Cypress) first appears 2026-06-02 on the same pattern.
-- A first row in a POS is not an opening, so both are cleared to NULL.
--
-- WHAT REVEL ACTUALLY OFFERS ---------------------------------------------------
-- /enterprise/Establishment/<id>/ exists and carries created_date,
-- on_boarding_started_date, on_boarding_completed_date and cancellation_date.
-- Both on_boarding fields are NULL for all 12 stores, so the only populated
-- candidate is created_date -- and it does NOT mean "opened". Measured on the
-- two stores whose history is NOT truncated, created_date precedes the first
-- real sale by 20 and 49 days:
--     est 48  created 2026-03-11  first REAL sale 2026-04-29  (+49d)
--     est 54  created 2026-05-13  first REAL sale 2026-06-02  (+20d)
-- That is an account/provisioning date recorded weeks before trading. It is
-- retained as revel_account_created_date because it is a genuine LOWER BOUND --
-- a store cannot have opened before it was provisioned -- but it is never
-- presented as the opening date.
--
-- RESULT: no authoritative opening date exists for ANY of the 12 stores, so
-- verified_open_date is NULL for all of them and weeks_since_open is NULL for
-- all of them. That is the honest state; it is not a gap to be filled by
-- guessing.
--
-- MATURITY THRESHOLDS ARE NOT ASSERTED -----------------------------------------
-- Migration 21's store_age_bucket hardcoded honeymoon <=8 weeks, ramp <=26,
-- maturing <=52. Those numbers came from the planning spec, not from a
-- maintained, verifiable configuration, and with every open date unknown they
-- could only ever have returned 'unknown' anyway. The bucket is dropped and
-- maturity_threshold_status says so.

BEGIN;

ALTER TABLE establishments
    ADD COLUMN IF NOT EXISTS open_date_confidence      TEXT,
    ADD COLUMN IF NOT EXISTS revel_account_created_date DATE;

COMMENT ON COLUMN establishments.open_date IS
    'VERIFIED store opening date. NULL means unknown -- it must never be '
    'back-filled from the first row in our own data. Set it only from a '
    'company source that states when the store opened to the public.';
COMMENT ON COLUMN establishments.open_date_confidence IS
    'verified = from an authoritative company source. unknown = no such source '
    'exists yet. Never "inferred".';
COMMENT ON COLUMN establishments.revel_account_created_date IS
    'Revel /enterprise/Establishment/<id>/ created_date: when the establishment '
    'was provisioned in Revel, observed 20-49 days BEFORE first trade. A lower '
    'bound on the opening date, NOT the opening date.';

-- Clear the inferred dates: they describe our data, not the business.
UPDATE establishments
SET open_date = NULL,
    open_date_source = NULL,
    open_date_confidence = 'unknown'
WHERE open_date_source = 'inferred_first_order' OR open_date IS NULL;

-- Revel provisioning dates, read from /enterprise/Establishment/<id>/.
UPDATE establishments SET revel_account_created_date = v.d
FROM (VALUES
    (6,  DATE '2021-04-05'), (7,  DATE '2022-09-26'), (14, DATE '2023-09-05'),
    (15, DATE '2023-09-05'), (20, DATE '2024-10-21'), (25, DATE '2024-12-10'),
    (26, DATE '2025-01-17'), (32, DATE '2025-05-21'), (36, DATE '2025-09-19'),
    (40, DATE '2025-10-10'), (48, DATE '2026-03-11'), (54, DATE '2026-05-13')
) AS v(id, d)
WHERE establishments.id = v.id;

COMMIT;

-- The view gains and loses columns, so CREATE OR REPLACE cannot be used.
DROP VIEW IF EXISTS v_store_cohort;

CREATE VIEW v_store_cohort AS
SELECT
    e.id                                   AS establishment_id,
    e.name                                 AS establishment_name,
    e.open_date                            AS verified_open_date,
    e.open_date_source,
    COALESCE(e.open_date_confidence, 'unknown') AS open_date_confidence,
    e.revel_account_created_date,
    h.first_seen_order_date,
    h.first_seen_real_order_date,
    h.first_seen_real_order_date           AS available_history_start,
    h.last_seen_order_date                 AS available_history_end,
    (h.last_seen_order_date - h.first_seen_real_order_date)        AS available_history_days,
    ((h.last_seen_order_date - h.first_seen_real_order_date) / 7)  AS available_history_weeks,
    -- TRUE when our history begins at the backfill edge, so the store was
    -- already trading before we could see it and its age cannot be read off
    -- our own data at all.
    (h.first_seen_order_date <= DATE '2026-01-02')                 AS history_truncated,
    -- weeks_since_open is NULL unless the opening date is VERIFIED. It is
    -- never computed from first_seen_*: that would turn a backfill boundary
    -- into a business fact.
    CASE WHEN e.open_date IS NOT NULL
         THEN (CURRENT_DATE - e.open_date) / 7 + 1 END             AS weeks_since_open,
    'no maintained threshold configured'::text                      AS maturity_threshold_status
FROM establishments e
LEFT JOIN (
    SELECT establishment_id,
           MIN(business_date)                                      AS first_seen_order_date,
           MIN(business_date) FILTER (WHERE txn_class = 'REAL')    AS first_seen_real_order_date,
           MAX(business_date)                                      AS last_seen_order_date
    FROM v_orders_classified
    GROUP BY establishment_id
) h ON h.establishment_id = e.id;

COMMENT ON VIEW v_store_cohort IS
    'Store age and data-history context. verified_open_date is NULL for every '
    'store today -- no authoritative source exists -- so weeks_since_open is '
    'also NULL and no store may be called new, young or mature. '
    'first_seen_real_order_date is when OUR DATA starts, which for 10 of 12 '
    'stores is the backfill edge (history_truncated = TRUE), not an opening. '
    'revel_account_created_date is a provisioning date observed 20-49 days '
    'before first trade: a lower bound, not an opening date.';

GRANT SELECT ON v_store_cohort TO laynes_ro;

-- Rollback: re-run migration 21's v_store_cohort definition, then
--   ALTER TABLE establishments DROP COLUMN IF EXISTS open_date_confidence,
--                              DROP COLUMN IF EXISTS revel_account_created_date;
