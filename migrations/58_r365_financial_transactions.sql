-- 58_r365_financial_transactions.sql
-- Transaction-level R365 FINANCIAL drill-down: every GL DETAIL LINE published
-- by the source financial API (GET /financial/expenses?include_unapproved=true),
-- one row per source detail line. Extends migration 56; the period summary
-- tables and views are unchanged.
--
-- VIEWS ARE CREATED HERE BUT GRANTED NOTHING -- the laynes_ro GRANT lives in
-- 59_r365_financial_transaction_grants.sql.
--
-- ---------------------------------------------------------------------------
-- SOURCE SEMANTICS (proven on all 144,776 lines, 2026-10-16)
-- ---------------------------------------------------------------------------
--   * detailId is unique per line (0 duplicates) and is the natural key. Lines
--     are NEVER deduplicated by date, amount, account or store.
--   * expenseAmount = debitAmount - creditAmount on every line (0 exceptions),
--     so credits and reversals reduce expense. No line has both sides > 0.
--   * The LINE carries its own location: 20 transactions span more than one
--     store, so the transaction header's location is never used.
--   * status is 'Approved' (128,151) or 'Unapproved' (16,625). Period
--     summaries count Approved lines only.
--   * Every line dated 2026-01-01 or later carries a source fiscal period; no
--     earlier line does. Fiscal periods are never assigned locally.
--   * sourceClass MIGRATED_SUMMARY = 'HIST DATA IMPORT' journal entries
--     (history through 2026-03-25): migrated P&L lines, not original bills.
--   * The source publishes NO description, memo, reference, posting date or
--     modified timestamp. transactionNumber (document number) is the only
--     reference; nothing else is stored because nothing else exists.

BEGIN;

-- Run provenance for the transaction rows each PERIOD / HISTORY run writes.
ALTER TABLE r365_financial_ingest_run
    ADD COLUMN IF NOT EXISTS transaction_rows_upserted integer,
    ADD COLUMN IF NOT EXISTS transaction_rows_changed  integer,
    ADD COLUMN IF NOT EXISTS transaction_rows_deleted  integer,
    ADD COLUMN IF NOT EXISTS transaction_stats         jsonb;

CREATE TABLE IF NOT EXISTS r365_financial_transaction (
    source_transaction_detail_id  text        PRIMARY KEY,   -- detailId
    source_transaction_id         text        NOT NULL,      -- transactionId (header)
    transaction_number            text,                      -- document number
    transaction_type              text        NOT NULL,      -- APInvoice, JournalEntry, ...
    transaction_status            text        NOT NULL,      -- Approved | Unapproved
    r365_location_id              text        NOT NULL,      -- the DETAIL LINE's location
    location_name                 text        NOT NULL,      -- source name (audit only)
    establishment_id              integer     REFERENCES establishments(id),
    accounting_date               date        NOT NULL,
    fiscal_year                   integer,
    period_number                 integer,
    gl_account_id                 text        NOT NULL,
    gl_account_number             text        NOT NULL,
    gl_account_name               text        NOT NULL,
    gl_account_type               text,
    analytics_category            text,                      -- as stamped by the source
    mapping_status                text        NOT NULL,
    debit_amount                  numeric     NOT NULL,
    credit_amount                 numeric     NOT NULL,
    expense_amount                numeric     NOT NULL,
    source_class                  text        NOT NULL,      -- TRANSACTION_DETAIL | MIGRATED_SUMMARY
    source_fetched_at             timestamptz NOT NULL,
    ingest_run_id                 bigint      NOT NULL REFERENCES r365_financial_ingest_run(id),
    first_loaded_at               timestamptz NOT NULL DEFAULT now(),
    updated_at                    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT r365_fin_txn_status_check
        CHECK (transaction_status IN ('Approved', 'Unapproved')),
    CONSTRAINT r365_fin_txn_mapping_check
        CHECK (mapping_status IN ('VERIFIED', 'CANDIDATE', 'UNMAPPED')),
    CONSTRAINT r365_fin_txn_sign
        CHECK (expense_amount = debit_amount - credit_amount),
    CONSTRAINT r365_fin_txn_fiscal_shape
        CHECK ((fiscal_year IS NULL) = (period_number IS NULL)),
    -- A fiscal period must be one of the official calendar's; none invented.
    CONSTRAINT r365_fin_txn_period_fk
        FOREIGN KEY (fiscal_year, period_number)
        REFERENCES ayg_business_periods (fiscal_year, period_number)
);
CREATE INDEX IF NOT EXISTS r365_fin_txn_period_idx
    ON r365_financial_transaction (fiscal_year, period_number, transaction_status);
CREATE INDEX IF NOT EXISTS r365_fin_txn_date_idx
    ON r365_financial_transaction (accounting_date);
CREATE INDEX IF NOT EXISTS r365_fin_txn_account_idx
    ON r365_financial_transaction (gl_account_number);
CREATE INDEX IF NOT EXISTS r365_fin_txn_est_idx
    ON r365_financial_transaction (establishment_id, accounting_date);

COMMENT ON TABLE r365_financial_transaction IS
  'Every R365 GL detail line from the source financial API, Approved and '
  'Unapproved, keyed by the source detailId. Location is the detail line''s. '
  'expense_amount = debit_amount - credit_amount. Internal: R365 ids, source '
  'names and run ids never cross the LLM boundary.';

-- ===========================================================================
-- LLM-SAFE VIEWS (granted in 59). VERIFIED store mappings only. No R365 ids
-- (detail, transaction, location, account), no run id, no fetch time.
-- analytics_category is shown ONLY for VERIFIED accounts: a CANDIDATE
-- account's provisional category (e.g. GAS) is never presented as trusted.
-- Approved and Unapproved lines are separate views, so the default view
-- cannot mix unapproved entries into an analysis by accident.
-- ===========================================================================
CREATE OR REPLACE VIEW v_r365_financial_transaction_llm AS
SELECT t.establishment_id,
       e.name                                            AS store_name,
       t.accounting_date,
       t.fiscal_year,
       t.period_number,
       p.period_label,
       t.gl_account_number,
       t.gl_account_name,
       CASE WHEN t.mapping_status = 'VERIFIED' THEN t.analytics_category END
                                                         AS analytics_category,
       t.mapping_status,
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
SELECT t.establishment_id,
       e.name                                            AS store_name,
       t.accounting_date,
       t.fiscal_year,
       t.period_number,
       p.period_label,
       t.gl_account_number,
       t.gl_account_name,
       CASE WHEN t.mapping_status = 'VERIFIED' THEN t.analytics_category END
                                                         AS analytics_category,
       t.mapping_status,
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
 WHERE m.status = 'VERIFIED' AND m.active
   AND t.transaction_status = 'Unapproved';

COMMENT ON VIEW v_r365_financial_unapproved_transaction_llm IS
  'UNAPPROVED R365 GL detail lines. Not part of any period total. Use ONLY '
  'when the user explicitly asks for unapproved entries, and label them as '
  'unapproved.';

COMMIT;
