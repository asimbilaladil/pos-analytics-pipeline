-- Undo 2026-11-17_mission_bend_goal_history.sql. DRY RUN BY DEFAULT
-- (psql -v apply=1 to commit). Run as laynes_user.
--
-- Restores Mission Bend's five original hme_goal_history rows verbatim from
-- hme_goal_history_correction_20261117 and marks the two audit conflict rows
-- as reverted (they are kept: they record that the observations failed their
-- check). The backup table is kept; drop it by hand once no longer needed.
-- Refuses if Mission Bend history has changed since the correction.

\set ON_ERROR_STOP 1
BEGIN;
SET LOCAL lock_timeout = '5s';
LOCK TABLE hme_goal_history IN SHARE ROW EXCLUSIVE MODE;

DO $$
DECLARE got text;
BEGIN
  IF to_regclass('public.hme_goal_history_correction_20261117') IS NULL THEN
    RAISE EXCEPTION 'no backup table: the correction was never applied';
  END IF;
  SELECT string_agg(concat_ws(',', effective_from_date, coalesce(effective_to_date::text, ''),
                              goal_d_seconds, observed_run_id), '|' ORDER BY effective_from_date)
    INTO got FROM hme_goal_history WHERE hme_store_number = '1158614';
  IF got IS DISTINCT FROM '2026-09-08,,285,2' THEN
    RAISE EXCEPTION 'Mission Bend history changed since the correction (%); resolve by hand', got;
  END IF;
END $$;

DELETE FROM hme_goal_history WHERE hme_store_number = '1158614';
INSERT INTO hme_goal_history (hme_store_number, effective_from_date, effective_to_date,
       goal_a_seconds, goal_b_seconds, goal_c_seconds, goal_d_seconds,
       observed_run_id, recorded_at)
SELECT hme_store_number, effective_from_date, effective_to_date,
       goal_a_seconds, goal_b_seconds, goal_c_seconds, goal_d_seconds,
       observed_run_id, recorded_at
  FROM hme_goal_history_correction_20261117;

UPDATE hme_goal_conflict
   SET resolution_note = resolution_note || ' | REVERTED ' || now()::text
 WHERE hme_store_number = '1158614' AND reason = 'observation_fails_pa_goal_d_check'
   AND observed_date IN ('2026-11-03', '2026-11-06');

SELECT effective_from_date, effective_to_date, goal_d_seconds, observed_run_id
  FROM hme_goal_history WHERE hme_store_number = '1158614' ORDER BY 1;

\if :{?apply}
  COMMIT;
\else
  \echo '== DRY RUN: rolling back'
  ROLLBACK;
\endif
