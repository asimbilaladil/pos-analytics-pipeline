-- Rollback for migration 65. NOT a migration; apply only to undo 65, as the
-- view owner (laynes_user), and only AFTER the code is rolled back to 73ef62a.
--
-- Order matters: code from this branch FAILS CLOSED without the view (the
-- HME gate refuses with "HME store-level trust could not be determined"), so
-- dropping the view while that code is live would block every HME answer.
-- Leaving the view in place after a code-only rollback is harmless: it is a
-- read-only, status-only view that nothing else references.
--
-- The loader change (hme_load 1.7.0) needs no schema rollback: it writes only
-- to the existing hme_goal_conflict table. Rolling the code back restores the
-- 1.6.0 behaviour; conflict rows already written stay as audit records.

BEGIN;
REVOKE SELECT ON v_hme_store_day_trust_llm FROM laynes_ro;
DROP VIEW IF EXISTS v_hme_store_day_trust_llm;
COMMIT;
