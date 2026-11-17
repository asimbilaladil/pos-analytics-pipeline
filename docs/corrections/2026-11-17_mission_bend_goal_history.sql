-- Mission Bend HME goal-history correction (HME 1158614 / establishment 25).
-- NOT a migration. DRY RUN BY DEFAULT: the transaction is rolled back unless
-- psql is started with  -v apply=1  (only after explicit approval).
--
--   dry run :  psql "$DSN" -v ON_ERROR_STOP=1 -f 2026-11-17_mission_bend_goal_history.sql
--   apply   :  psql "$DSN" -v ON_ERROR_STOP=1 -v apply=1 -f 2026-11-17_mission_bend_goal_history.sql
-- Run as the table owner (laynes_user). Undo: 2026-11-17_mission_bend_goal_history_rollback.sql
--
-- What is wrong. The store-scoped backfill loaded 2026-11-03 (run 572) and
-- 2026-11-06 (run 575) from captures whose Multi-report goals (A239/B251/C263/
-- D289) disagreed with the same day's Performance Analysis threshold (285); the
-- day's goal_d_matches_pa_threshold check FAILED, but loader 1.6.0 still folded
-- the observation into history (fixed in loader 1.7.0). Result, five rows:
--     09-08..11-02 285 (run 2) | 11-03 289 (572) | 11-04..11-05 285 (573)
--   | 11-06 289 (575) | 11-07..open 285 (576)
-- PA lane_total_goal_d_seconds is 285 on every Mission Bend row.
--
-- Target: one period, 09-08..open, A240/B255/C270/D285, observed_run_id 2 --
-- exactly what loader 1.7.0 produces for the same loads.
--
-- Audit evidence is preserved twice: the five original rows are copied verbatim
-- into hme_goal_history_correction_20261117 (also the rollback source), and
-- the two rejected observations are recorded in hme_goal_conflict with their
-- runs, the period they contradicted, and a resolution note.
-- Scope: only hme_store_number '1158614'. Every precondition is asserted and
-- any surprise aborts the whole transaction before anything is changed.

\set ON_ERROR_STOP 1
BEGIN;
SET LOCAL lock_timeout = '5s';
LOCK TABLE hme_goal_history IN SHARE ROW EXCLUSIVE MODE;   -- no concurrent loader write

-- ── 0. evidence for the other stores (compared again at the end) ─────────────
CREATE TEMP TABLE _others_before ON COMMIT DROP AS
SELECT md5(string_agg(row(g.*)::text, '|' ORDER BY hme_store_number, effective_from_date)) AS h,
       count(*) AS n
  FROM hme_goal_history g WHERE hme_store_number <> '1158614';

-- ── 1. preconditions: exactly the five rows described above ──────────────────
DO $$
DECLARE got text; want text :=
  '2026-09-08,2026-11-02,240,255,270,285,2|2026-11-03,2026-11-03,239,251,263,289,572|'
  '2026-11-04,2026-11-05,240,255,270,285,573|2026-11-06,2026-11-06,239,251,263,289,575|'
  '2026-11-07,,240,255,270,285,576';
BEGIN
  SELECT string_agg(concat_ws(',', effective_from_date, coalesce(effective_to_date::text, ''),
                              goal_a_seconds, goal_b_seconds, goal_c_seconds, goal_d_seconds,
                              observed_run_id), '|' ORDER BY effective_from_date)
    INTO got FROM hme_goal_history WHERE hme_store_number = '1158614';
  IF got IS DISTINCT FROM want THEN
    RAISE EXCEPTION 'precondition: Mission Bend goal history is not the expected 5 rows: %', got;
  END IF;
  IF EXISTS (SELECT 1 FROM hme_store_daily WHERE hme_store_number = '1158614'
              AND lane_total_goal_d_seconds IS DISTINCT FROM 285) THEN
    RAISE EXCEPTION 'precondition: a Mission Bend PA threshold is not 285';
  END IF;
  IF to_regclass('public.hme_goal_history_correction_20261117') IS NOT NULL THEN
    RAISE EXCEPTION 'precondition: backup table already exists (correction already applied?)';
  END IF;
  IF EXISTS (SELECT 1 FROM hme_goal_conflict WHERE hme_store_number = '1158614'
              AND observed_date IN ('2026-11-03', '2026-11-06')
              AND reason = 'observation_fails_pa_goal_d_check') THEN
    RAISE EXCEPTION 'precondition: audit conflict rows already exist';
  END IF;
END $$;

\echo '== BEFORE: v_hme_goal_history_llm, establishment 25'
SELECT effective_from_date, effective_to_date, goal_a_seconds, goal_b_seconds,
       goal_c_seconds, goal_d_seconds
  FROM v_hme_goal_history_llm WHERE establishment_id = 25 ORDER BY 1;

-- ── 2. verbatim backup of the rows being changed (rollback source) ───────────
CREATE TABLE hme_goal_history_correction_20261117 AS
SELECT g.*, now() AS backed_up_at,
       'Mission Bend goal-history correction 2026-11-17 (pre-correction rows)'::text AS note
  FROM hme_goal_history g WHERE hme_store_number = '1158614';
COMMENT ON TABLE hme_goal_history_correction_20261117 IS
  'Verbatim pre-correction hme_goal_history rows for HME 1158614 (Mission Bend). Audit + rollback source.';

-- ── 3. audit: the two rejected observations, as loader 1.7.0 would record them
INSERT INTO hme_goal_conflict (hme_store_number, observed_date,
       observed_goal_a, observed_goal_b, observed_goal_c, observed_goal_d,
       existing_from, existing_to,
       existing_goal_a, existing_goal_b, existing_goal_c, existing_goal_d,
       reason, run_id, resolved_at, resolved_by, resolution_note)
SELECT g.hme_store_number, g.effective_from_date,
       g.goal_a_seconds, g.goal_b_seconds, g.goal_c_seconds, g.goal_d_seconds,
       DATE '2026-09-08', NULL, 240, 255, 270, 285,
       'observation_fails_pa_goal_d_check', g.observed_run_id, now(),
       'approved goal-history correction 2026-11-17',
       'Observation failed goal_d_matches_pa_threshold (PA threshold 285) but was applied '
       'by loader 1.6.0; removed from hme_goal_history. Original row preserved in '
       'hme_goal_history_correction_20261117.'
  FROM hme_goal_history g
 WHERE g.hme_store_number = '1158614'
   AND g.effective_from_date IN ('2026-11-03', '2026-11-06');

-- ── 4. restore one continuous 285 period ─────────────────────────────────────
-- Delete first: the open row (11-07) must go before 09-08 can be reopened
-- (hme_goal_history_one_open allows one open period per store).
DELETE FROM hme_goal_history
 WHERE hme_store_number = '1158614'
   AND effective_from_date IN ('2026-11-03', '2026-11-04', '2026-11-06', '2026-11-07');
UPDATE hme_goal_history SET effective_to_date = NULL
 WHERE hme_store_number = '1158614' AND effective_from_date = '2026-09-08'
   AND effective_to_date = '2026-11-02';

-- ── 5. postconditions ────────────────────────────────────────────────────────
DO $$
DECLARE got text;
BEGIN
  SELECT string_agg(concat_ws(',', effective_from_date, coalesce(effective_to_date::text, ''),
                              goal_a_seconds, goal_b_seconds, goal_c_seconds, goal_d_seconds,
                              observed_run_id), '|' ORDER BY effective_from_date)
    INTO got FROM hme_goal_history WHERE hme_store_number = '1158614';
  IF got IS DISTINCT FROM '2026-09-08,,240,255,270,285,2' THEN
    RAISE EXCEPTION 'postcondition: unexpected Mission Bend history: %', got;
  END IF;
  IF (SELECT count(*) FROM hme_goal_history_correction_20261117) <> 5 THEN
    RAISE EXCEPTION 'postcondition: backup does not hold 5 rows';
  END IF;
  IF (SELECT count(*) FROM hme_goal_conflict WHERE hme_store_number = '1158614'
        AND reason = 'observation_fails_pa_goal_d_check'
        AND observed_date IN ('2026-11-03', '2026-11-06') AND observed_goal_d = 289) <> 2 THEN
    RAISE EXCEPTION 'postcondition: audit conflict rows missing';
  END IF;
  IF (SELECT h FROM _others_before) IS DISTINCT FROM
     (SELECT md5(string_agg(row(g.*)::text, '|' ORDER BY hme_store_number, effective_from_date))
        FROM hme_goal_history g WHERE hme_store_number <> '1158614') THEN
    RAISE EXCEPTION 'postcondition: another store''s goal history changed';
  END IF;
  -- Every Mission Bend business date resolves to D=285.
  IF EXISTS (SELECT 1 FROM hme_store_daily d
              WHERE d.hme_store_number = '1158614'
                AND NOT EXISTS (SELECT 1 FROM hme_goal_history g
                                 WHERE g.hme_store_number = d.hme_store_number
                                   AND d.business_date >= g.effective_from_date
                                   AND (g.effective_to_date IS NULL
                                        OR d.business_date <= g.effective_to_date)
                                   AND g.goal_d_seconds = d.lane_total_goal_d_seconds)) THEN
    RAISE EXCEPTION 'postcondition: a Mission Bend date does not resolve to its PA threshold';
  END IF;
END $$;

\echo '== AFTER: v_hme_goal_history_llm, establishment 25'
SELECT effective_from_date, effective_to_date, goal_a_seconds, goal_b_seconds,
       goal_c_seconds, goal_d_seconds
  FROM v_hme_goal_history_llm WHERE establishment_id = 25 ORDER BY 1;
\echo '== AFTER: audit rows'
SELECT observed_date, observed_goal_d, existing_from, existing_goal_d, reason, run_id
  FROM hme_goal_conflict WHERE hme_store_number = '1158614' ORDER BY observed_date;

\if :{?apply}
  \echo '== APPLY: committing'
  COMMIT;
\else
  \echo '== DRY RUN: rolling back (start psql with -v apply=1 to commit)'
  ROLLBACK;
\endif
