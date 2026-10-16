-- 60_r365_financial_local_category_mapping.sql
-- Consumer-side (LOCAL) GL account -> analytics category mappings for R365
-- FINANCIAL, approved on this VPS from evidence in the stored GL lines.
--
-- WHY LOCAL. The R365 source VPS publishes its own account mapping (today one
-- VERIFIED account: 6200-03 Electric -> ELECTRICITY) and this VPS reloads it
-- on every ACCOUNTS pull, so a local edit of r365_financial_account would be
-- overwritten. The source's category summaries also cover its VERIFIED
-- categories only. Local mappings therefore live in their own table, and
-- their period figures are summed here from the Approved GL detail lines that
-- every PERIOD run already loads and reconciles (the source's UNMAPPED bucket
-- is checked line-for-line against that detail before a run can succeed).
--
-- RULES
--   * SOURCE WINS. A local mapping is ignored for an account the source has
--     VERIFIED, and for any category name the source itself VERIFIES, so a
--     line can never be counted under two categories, or twice in one.
--   * NAME GUARD. A local mapping applies only while the source still
--     publishes the account under the exact name it was approved under; a
--     renamed or repurposed account drops out (fails closed) until re-reviewed.
--   * LEAF ACCOUNTS ONLY. Parent accounts (6150, 6200, 7500) carry no lines and
--     are never mapped, so a category is each mapped line exactly once.
--   * Only mapping_status = 'VERIFIED' rows feed a category. 'CANDIDATE' rows
--     record accounts that were reviewed and deliberately left out.
--   * Electricity is untouched: it still comes from the source's summary.
--   * No transaction, summary or account row is modified by this migration.
--
-- VIEWS. The four LLM-safe views of 56/58 are replaced in place (same columns,
-- account view gains two at the end), so the laynes_ro grants of 57/59 carry
-- over. r365_financial_effective_mapping and the mapping table are internal
-- and granted nothing.

BEGIN;

CREATE TABLE IF NOT EXISTS r365_financial_local_category_mapping (
    account_number      text        PRIMARY KEY,
    account_name        text        NOT NULL,   -- must still match the source's name
    analytics_category  text        NOT NULL,
    mapping_status      text        NOT NULL,
    evidence            text        NOT NULL,
    decided_by          text        NOT NULL,
    decided_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT r365_fin_local_map_status_check
        CHECK (mapping_status IN ('VERIFIED', 'CANDIDATE')),
    CONSTRAINT r365_fin_local_map_category_check
        CHECK (analytics_category ~ '^[A-Z][A-Z_]*$')
);

COMMENT ON TABLE r365_financial_local_category_mapping IS
  'Locally approved R365 GL account -> analytics category decisions, with the '
  'evidence they rest on. VERIFIED rows feed a trusted category; CANDIDATE rows '
  'were reviewed and left out. Internal: never granted to laynes_ro.';

-- Evidence reviewed 2026-10-16 against the stored GL lines (Approved unless
-- stated; native TRANSACTION_DETAIL lines start 2026-03-26).
INSERT INTO r365_financial_local_category_mapping
       (account_number, account_name, analytics_category, mapping_status, evidence, decided_by)
VALUES
 ('6200-01', 'Gas', 'GAS', 'VERIFIED',
  'Leaf under 6200 Utilities. 192 lines, 12 stores, 2025-01-10..2026-10-12; native lines are '
  'APInvoice/BankExpense bills, about one per store per month, no credits; 0 Unapproved. '
  'Source already classes it CANDIDATE GAS.', 'r365-category-review'),
 ('6200-02', 'Water', 'WATER', 'VERIFIED',
  'Leaf under 6200 Utilities. 353 lines, 11 stores (none for Downtown Houston), '
  '2025-01-02..2026-10-15; native lines are 1-2 bills per store per month, no credits; '
  'credits occur only in migrated history. 1 Unapproved line. Source classes it CANDIDATE '
  'WATER_SEWER.', 'r365-category-review'),
 ('7500-11', 'Rent - Restaurant', 'RENT', 'VERIFIED',
  'Leaf under 7500 Occupancy Expenses; the only rent account with activity. 230 lines, '
  '12 stores, 2025-01-02..2026-10-02; fixed monthly amounts per store, usually paid at the '
  'start of the month (April and August rent posted in late March / July). Some stores '
  'have a second recurring line (Shepherd $541.25, Mission Bend $3,000-3,007.50); the GL '
  'has no memo, so its nature is not asserted. 1 Unapproved line.', 'r365-category-review'),
 ('6000-01', 'R&M Electrical', 'REPAIRS_MAINTENANCE', 'VERIFIED',
  'Leaf under 6150 Repairs & Maintenance. 44 native lines, 11 stores, invoices/bank '
  'expenses, no credits.', 'r365-category-review'),
 ('6000-02', 'R&M Plumbing', 'REPAIRS_MAINTENANCE', 'VERIFIED',
  'Leaf under 6150 Repairs & Maintenance. 31 native lines, 11 stores; one $400 refund '
  '(BankDeposit credit).', 'r365-category-review'),
 ('6000-11', 'R&M - Building', 'REPAIRS_MAINTENANCE', 'VERIFIED',
  'Leaf under 6150 Repairs & Maintenance. 703 lines, 12 stores; carries migrated R&M '
  'history (511 HIST DATA IMPORT lines) plus native bills; refunds as credits.',
  'r365-category-review'),
 ('6000-13', 'R&M - Equipment', 'REPAIRS_MAINTENANCE', 'VERIFIED',
  'Leaf under 6150 Repairs & Maintenance. 512 lines, 12 stores; carries migrated R&M '
  'history (319 lines) plus native bills; refunds as credits.', 'r365-category-review'),
 ('6000-14', 'R&M HVAC', 'REPAIRS_MAINTENANCE', 'VERIFIED',
  'Leaf under 6150 Repairs & Maintenance. 136 native lines, 12 stores; 1 Unapproved.',
  'r365-category-review'),
 ('6000-20', 'R&M Landscaping', 'REPAIRS_MAINTENANCE', 'VERIFIED',
  'Leaf under 6150 Repairs & Maintenance. 129 native lines, 10 stores; refunds as credits.',
  'r365-category-review'),
 ('6000-31', 'R&M Signs and Lighting', 'REPAIRS_MAINTENANCE', 'VERIFIED',
  'Leaf under 6150 Repairs & Maintenance. 2 native lines, 1 store (Sep 2026).',
  'r365-category-review'),
 ('6000-09', 'R&M Parking Lot', 'REPAIRS_MAINTENANCE', 'CANDIDATE',
  'Leaf under 6150 but no line has ever posted to it: nothing to verify.',
  'r365-category-review'),
 ('7500-01', 'Rent', 'RENT', 'CANDIDATE',
  'No line has ever posted to it (rent posts to 7500-11).', 'r365-category-review'),
 ('7500-03', 'Rent - Percentage', 'RENT', 'CANDIDATE',
  'No line has ever posted to it.', 'r365-category-review'),
 ('7500-09', 'Rent - Storage', 'RENT', 'CANDIDATE',
  'Storage-unit rent, not restaurant occupancy: 9 lines, 2 stores, $118-$637. Semantically '
  'distinct; kept out of RENT.', 'r365-category-review'),
 ('7500-13', 'Rent - Equipment', 'RENT', 'CANDIDATE',
  'Equipment rental under 6500-01 Direct Operating Costs, not occupancy: 1 line. Kept out '
  'of RENT.', 'r365-category-review')
ON CONFLICT (account_number) DO NOTHING;

-- Effective account -> category mapping: the source's VERIFIED accounts, plus
-- local VERIFIED accounts subject to SOURCE WINS and the NAME GUARD. Internal.
CREATE OR REPLACE VIEW r365_financial_effective_mapping AS
WITH source_categories AS (
    SELECT DISTINCT analytics_category
      FROM r365_financial_account
     WHERE mapping_status = 'VERIFIED' AND analytics_category IS NOT NULL
)
SELECT a.account_id,
       a.account_number,
       a.account_name,
       CASE WHEN a.mapping_status = 'VERIFIED' THEN a.analytics_category
            ELSE l.analytics_category END                AS analytics_category,
       CASE WHEN a.mapping_status = 'VERIFIED' THEN 'SOURCE'
            ELSE 'LOCAL' END                             AS mapping_source
  FROM r365_financial_account a
  LEFT JOIN r365_financial_local_category_mapping l
         ON l.account_number = a.account_number
        AND l.account_name = a.account_name
        AND l.mapping_status = 'VERIFIED'
        AND a.mapping_status <> 'VERIFIED'
        AND l.analytics_category NOT IN (SELECT analytics_category FROM source_categories)
 WHERE (a.mapping_status = 'VERIFIED' AND a.analytics_category IS NOT NULL)
    OR l.account_number IS NOT NULL;

COMMENT ON VIEW r365_financial_effective_mapping IS
  'Every account that feeds a trusted category: SOURCE (VERIFIED by the R365 '
  'source) or LOCAL (r365_financial_local_category_mapping VERIFIED, name '
  'unchanged, account and category not VERIFIED by the source). Internal.';

-- Period view: the source summary rows exactly as before (electricity), plus
-- one row per loaded period x VERIFIED store x LOCAL category summed from the
-- Approved GL lines. A store-period with no line is a NO_ACTIVITY row with
-- expense_amount NULL -- never 0.
CREATE OR REPLACE VIEW v_r365_financial_period_llm AS
WITH verified_categories AS (
    SELECT DISTINCT analytics_category
      FROM r365_financial_account
     WHERE mapping_status = 'VERIFIED' AND analytics_category IS NOT NULL
), local_map AS (
    SELECT account_id, analytics_category
      FROM r365_financial_effective_mapping
     WHERE mapping_source = 'LOCAL'
), local_categories AS (
    SELECT DISTINCT analytics_category FROM local_map
), loaded_periods AS (
    -- the latest analytical run per period: FAILED runs never become truth
    SELECT DISTINCT ON (requested_fiscal_year, requested_period_number)
           requested_fiscal_year AS fiscal_year,
           requested_period_number AS period_number,
           status
      FROM r365_financial_ingest_run
     WHERE target_kind = 'PERIOD' AND status IN ('COMPLETE', 'PARTIAL')
     ORDER BY requested_fiscal_year, requested_period_number, id DESC
), local_lines AS (
    SELECT t.r365_location_id, t.establishment_id, t.fiscal_year, t.period_number,
           lm.analytics_category,
           sum(t.expense_amount)                        AS expense_amount,
           count(*)::integer                            AS transaction_count,
           count(DISTINCT t.gl_account_id)::integer     AS account_count,
           CASE WHEN bool_and(t.source_class = 'TRANSACTION_DETAIL') THEN 'TRANSACTION_DETAIL'
                WHEN bool_and(t.source_class = 'MIGRATED_SUMMARY') THEN 'MIGRATED_SUMMARY'
                ELSE 'MIXED' END                        AS source_quality
      FROM r365_financial_transaction t
      JOIN local_map lm ON lm.account_id = t.gl_account_id
     WHERE t.transaction_status = 'Approved' AND t.fiscal_year IS NOT NULL
     GROUP BY 1, 2, 3, 4, 5
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
 WHERE m.status = 'VERIFIED' AND m.active
UNION ALL
SELECT m.establishment_id,
       e.name                                   AS store_name,
       lp.fiscal_year,
       lp.period_number,
       p.period_label,
       p.start_date                             AS period_start,
       p.end_date                               AS period_end,
       p.period_status,
       c.analytics_category,
       ll.expense_amount,
       ll.transaction_count,
       ll.account_count,
       CASE WHEN ll.r365_location_id IS NULL THEN 'NO_ACTIVITY'
            ELSE lp.status END                  AS data_status,
       (ll.r365_location_id IS NOT NULL)        AS has_posting,
       ll.source_quality
  FROM loaded_periods lp
  JOIN v_ayg_business_periods_llm p
         ON p.fiscal_year = lp.fiscal_year AND p.period_number = lp.period_number
 CROSS JOIN local_categories c
  JOIN r365_store_mapping m     ON m.status = 'VERIFIED' AND m.active
  JOIN establishments e         ON e.id = m.establishment_id
  LEFT JOIN local_lines ll
         ON ll.r365_location_id = m.r365_store_id
        AND ll.establishment_id = m.establishment_id
        AND ll.fiscal_year = lp.fiscal_year
        AND ll.period_number = lp.period_number
        AND ll.analytics_category = c.analytics_category;

COMMENT ON VIEW v_r365_financial_period_llm IS
  'R365 FINANCIAL (GL) expense per store x AYG fiscal period x VERIFIED '
  'analytics category. Approved transactions only. has_posting = false / '
  'data_status = NO_ACTIVITY means NO POSTING WAS FOUND: expense_amount is NULL '
  'and that is UNKNOWN, NEVER $0 (bills can post in an adjacent period). '
  'Period grain only: no daily or hourly financial data exists. ELECTRICITY '
  'comes from the source summary; GAS, WATER, RENT and REPAIRS_MAINTENANCE are '
  'summed from the Approved lines of their VERIFIED GL accounts.';
COMMENT ON COLUMN v_r365_financial_period_llm.expense_amount IS
  'Approved expense in dollars for the period. NULL when no posting exists '
  '(never treat as 0). SUM() ignores NULLs; count(expense_amount) = stores '
  'with a posting.';
COMMENT ON COLUMN v_r365_financial_period_llm.source_quality IS
  'TRANSACTION_DETAIL = real GL transactions. MIGRATED_SUMMARY = migrated P&L '
  'history (through 2026-03-25) with little transaction detail.';

-- Coverage: one row per loaded period x VERIFIED category. A LOCAL category
-- takes the period run's status: a run is COMPLETE only when the source calls
-- the period closed and every summary bucket, including the one holding the
-- locally mapped accounts, reconciled to the GL lines.
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
    SELECT DISTINCT analytics_category, mapping_source
      FROM r365_financial_effective_mapping
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
       COALESCE(l.source_data_status ->> c.analytics_category,
                CASE WHEN c.mapping_source = 'LOCAL' THEN l.status END)
                                                AS source_data_status,
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

-- Chart of accounts with the EFFECTIVE mapping (metadata only, no amounts).
CREATE OR REPLACE VIEW v_r365_financial_account_llm AS
SELECT a.account_number,
       a.account_name,
       a.account_type,
       a.parent_account_number,
       CASE WHEN em.account_id IS NOT NULL THEN em.analytics_category
            WHEN a.mapping_status <> 'UNMAPPED' THEN a.analytics_category
            ELSE lc.analytics_category END      AS analytics_category,
       CASE WHEN em.account_id IS NOT NULL THEN 'VERIFIED'
            WHEN a.mapping_status <> 'UNMAPPED' THEN a.mapping_status
            WHEN lc.account_number IS NOT NULL THEN 'CANDIDATE'
            ELSE 'UNMAPPED' END                 AS mapping_status,
       (em.account_id IS NOT NULL)              AS category_trusted,
       em.mapping_source,
       a.mapping_status                         AS source_mapping_status
  FROM r365_financial_account a
  LEFT JOIN r365_financial_effective_mapping em ON em.account_id = a.account_id
  LEFT JOIN r365_financial_local_category_mapping lc
         ON lc.account_number = a.account_number
        AND lc.account_name = a.account_name
        AND lc.mapping_status = 'CANDIDATE';

COMMENT ON VIEW v_r365_financial_account_llm IS
  'R365 chart of accounts with the effective mapping status. Metadata only, no '
  'amounts. Only mapping_status = VERIFIED accounts feed a trusted category '
  '(mapping_source SOURCE = verified by the R365 source, LOCAL = verified here '
  'from the GL evidence); CANDIDATE / UNMAPPED accounts are accounting data '
  'with no trusted category.';

-- Transaction views: analytics_category / mapping_status follow the effective
-- mapping, so a VERIFIED category's lines add up to its period figure.
CREATE OR REPLACE VIEW v_r365_financial_transaction_llm AS
WITH local_map AS (
    SELECT account_id, analytics_category
      FROM r365_financial_effective_mapping
     WHERE mapping_source = 'LOCAL'
)
SELECT t.establishment_id,
       e.name                                            AS store_name,
       t.accounting_date,
       t.fiscal_year,
       t.period_number,
       p.period_label,
       t.gl_account_number,
       t.gl_account_name,
       CASE WHEN t.mapping_status = 'VERIFIED' THEN t.analytics_category
            ELSE lm.analytics_category END               AS analytics_category,
       CASE WHEN t.mapping_status <> 'VERIFIED' AND lm.account_id IS NOT NULL
            THEN 'VERIFIED' ELSE t.mapping_status END    AS mapping_status,
       t.transaction_type,
       t.transaction_status,
       t.transaction_number,
       t.debit_amount,
       t.credit_amount,
       t.expense_amount,
       t.source_class                                    AS source_quality
  FROM r365_financial_transaction t
  JOIN r365_store_mapping m ON m.r365_store_id = t.r365_location_id
                           AND m.establishment_id = t.establishment_id
  JOIN establishments e     ON e.id = t.establishment_id
  LEFT JOIN ayg_business_periods p
         ON p.fiscal_year = t.fiscal_year AND p.period_number = t.period_number
  LEFT JOIN local_map lm    ON lm.account_id = t.gl_account_id
 WHERE m.status = 'VERIFIED' AND m.active
   AND t.transaction_status = 'Approved';

COMMENT ON VIEW v_r365_financial_transaction_llm IS
  'APPROVED R365 GL detail lines (one row per source detail line) for the 12 '
  'VERIFIED stores. expense_amount = debit_amount - credit_amount: credits and '
  'reversals are negative. SUM(expense_amount) for a VERIFIED category, store '
  'and period equals v_r365_financial_period_llm.expense_amount. '
  'analytics_category is NULL unless the account is VERIFIED. accounting_date '
  'is an accounting date, not a time of use: there is no hour or time of day. '
  'fiscal_year/period_number are NULL before FY2026 (no official calendar). '
  'source_quality MIGRATED_SUMMARY = migrated P&L history lines, not bills.';

CREATE OR REPLACE VIEW v_r365_financial_unapproved_transaction_llm AS
WITH local_map AS (
    SELECT account_id, analytics_category
      FROM r365_financial_effective_mapping
     WHERE mapping_source = 'LOCAL'
)
SELECT t.establishment_id,
       e.name                                            AS store_name,
       t.accounting_date,
       t.fiscal_year,
       t.period_number,
       p.period_label,
       t.gl_account_number,
       t.gl_account_name,
       CASE WHEN t.mapping_status = 'VERIFIED' THEN t.analytics_category
            ELSE lm.analytics_category END               AS analytics_category,
       CASE WHEN t.mapping_status <> 'VERIFIED' AND lm.account_id IS NOT NULL
            THEN 'VERIFIED' ELSE t.mapping_status END    AS mapping_status,
       t.transaction_type,
       t.transaction_status,
       t.transaction_number,
       t.debit_amount,
       t.credit_amount,
       t.expense_amount,
       t.source_class                                    AS source_quality
  FROM r365_financial_transaction t
  JOIN r365_store_mapping m ON m.r365_store_id = t.r365_location_id
                           AND m.establishment_id = t.establishment_id
  JOIN establishments e     ON e.id = t.establishment_id
  LEFT JOIN ayg_business_periods p
         ON p.fiscal_year = t.fiscal_year AND p.period_number = t.period_number
  LEFT JOIN local_map lm    ON lm.account_id = t.gl_account_id
 WHERE m.status = 'VERIFIED' AND m.active
   AND t.transaction_status = 'Unapproved';

COMMENT ON VIEW v_r365_financial_unapproved_transaction_llm IS
  'UNAPPROVED R365 GL detail lines. Not part of any period total. Use ONLY '
  'when the user explicitly asks for unapproved entries, and label them as '
  'unapproved.';

COMMIT;
