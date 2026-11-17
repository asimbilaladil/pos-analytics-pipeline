-- Read-only pre/post validation for the Mission Bend goal-history correction.
-- Safe to run at any time:  psql "$DSN" -X -f 2026-11-17_mission_bend_goal_history_validate.sql
SET default_transaction_read_only = on;
BEGIN READ ONLY;

\echo '1. Mission Bend goal history (pre: 5 rows; post: 1 open row 09-08, D285, run 2)'
SELECT effective_from_date, effective_to_date, goal_a_seconds, goal_b_seconds,
       goal_c_seconds, goal_d_seconds, observed_run_id
  FROM hme_goal_history WHERE hme_store_number = '1158614' ORDER BY 1;

\echo '2. PA thresholds on Mission Bend daily rows (expect one value: 285)'
SELECT lane_total_goal_d_seconds, count(*), min(business_date), max(business_date)
  FROM hme_store_daily WHERE hme_store_number = '1158614' GROUP BY 1;

\echo '3. Mission Bend dates whose goal in force differs from that day''s PA threshold'
\echo '   (pre: 2026-11-03, 2026-11-06; post: none)'
SELECT d.business_date, d.lane_total_goal_d_seconds AS pa_goal_d, g.goal_d_seconds AS history_goal_d
  FROM hme_store_daily d
  LEFT JOIN hme_goal_history g
         ON g.hme_store_number = d.hme_store_number
        AND d.business_date >= g.effective_from_date
        AND (g.effective_to_date IS NULL OR d.business_date <= g.effective_to_date)
 WHERE d.hme_store_number = '1158614'
   AND g.goal_d_seconds IS DISTINCT FROM d.lane_total_goal_d_seconds
 ORDER BY 1;

\echo '4. Audit rows (pre: none; post: 2 rows, observation_fails_pa_goal_d_check)'
SELECT observed_date, observed_goal_d, existing_from, existing_to, existing_goal_d,
       reason, run_id, resolved_at IS NOT NULL AS resolved
  FROM hme_goal_conflict WHERE hme_store_number = '1158614' ORDER BY 1;

\echo '5. Backup table (pre: absent; post: 5 rows)'
SELECT to_regclass('public.hme_goal_history_correction_20261117') AS backup_table;

\echo '6. Other stores: fingerprint must be identical before and after'
SELECT count(*) AS rows, count(*) FILTER (WHERE effective_to_date IS NULL) AS open_rows,
       md5(string_agg(row(g.*)::text, '|' ORDER BY hme_store_number, effective_from_date)) AS fingerprint
  FROM hme_goal_history g WHERE hme_store_number <> '1158614';

\echo '7. Network day verdicts (unaffected by goal history; fingerprint identical)'
SELECT count(*) AS days,
       md5(string_agg(row(c.*)::text, '|' ORDER BY business_date)) AS fingerprint
  FROM v_hme_day_completeness_llm c;

\echo '8. Runs the 289 periods came from'
SELECT run_id, business_date, status, reconciliation_ok, store_scope, loader_version
  FROM hme_ingest_run WHERE run_id IN (2, 572, 573, 575, 576) ORDER BY 1;

ROLLBACK;
