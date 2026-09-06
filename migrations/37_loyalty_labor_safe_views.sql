-- Migration 37: LLM-facing views for loyalty evidence and labour.
--
-- The raw tables (order_loyalty_v2, timesheet_entries_v2) stay DENIED to
-- laynes_ro. They hold a hashed loyalty key and employee ids, which the
-- assistant has no analytical need for. Everything it can legitimately ask is
-- answerable from the aggregates below, so exposure happens through these
-- views and nothing else.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. LOYALTY -- one row per order
-- ---------------------------------------------------------------------------
-- TERMINOLOGY IS LOad-BEARING HERE.
--
-- has_loyalty_payload means Revel attached a loyalty structure to this order:
-- LOYALTY EVIDENCE PRESENT. Its absence means NO LOYALTY EVIDENCE OBSERVED --
-- it does NOT mean the guest is not a member. A member who does not identify
-- at the till produces an order with no payload, and that is indistinguishable
-- here from a non-member's order. Nothing in this data can separate them.
--
-- Coverage is a minority signal: 76,973 of 1,492,814 ingested orders (5.16%)
-- carry a payload, ranging 3.26% (Shepherd) to 9.07% (Cypress) by store.
--
-- loyalty_key_hash is deliberately absent. It is the one field that could link
-- orders to a single person, and per-person loyalty analysis is not something
-- the assistant should be doing from an unverified hash. customer_id is absent
-- too: identity is a SEPARATE concept from loyalty (A11) and joining them here
-- would invite exactly the conflation A11 forbids.
CREATE OR REPLACE VIEW v_order_loyalty_context AS
SELECT
    o.id                                       AS order_id,
    o.establishment_id,
    o.business_date,
    o.txn_class,
    o.final_total,
    COALESCE(l.has_loyalty_payload, FALSE)     AS has_loyalty_payload,
    l.loyalty_registered,
    l.has_applied_reward,
    COALESCE(l.applied_rewards_count, 0)       AS applied_rewards_count,
    COALESCE(l.has_reward_card, FALSE)         AS has_reward_card,
    -- Presence, not the balance. A point balance is a per-person running total
    -- that says nothing about this order, and exposing it invites summing it
    -- across orders, which is meaningless.
    (l.total_points_snapshot IS NOT NULL)      AS total_points_present
FROM v_orders_classified o
LEFT JOIN order_loyalty_v2 l ON l.order_id = o.id;

COMMENT ON VIEW v_order_loyalty_context IS
    'Order-level loyalty EVIDENCE. has_loyalty_payload = loyalty evidence '
    'present; its absence = no loyalty evidence observed, which does NOT prove '
    'non-membership. Carries no loyalty key, no customer_id, no PII. Loyalty '
    'evidence covers ~5% of orders -- a minority signal, never the whole guest '
    'base.';

-- ---------------------------------------------------------------------------
-- 2. LABOUR -- one row per establishment x business_date x local hour
-- ---------------------------------------------------------------------------
-- SHIFTS ARE SPLIT ACROSS THE HOURS THEY SPAN, not dumped into the clock-in
-- hour. A 17:30-19:15 shift contributes 0.5h to hour 17, 1.0h to hour 18 and
-- 0.25h to hour 19. Assigning the whole shift to hour 17 would make opening
-- hours look catastrophically overstaffed and closing hours unstaffed.
--
-- DST: the hour series is generated in timestamptz (absolute time) and only
-- LABELLED with the local hour. America/Chicago offsets move by whole hours,
-- so buckets stay aligned. Spring forward simply has no 02:00 bucket; fall
-- back produces two 01:00 buckets whose hours sum together under local_hour=1.
-- Durations are computed as timestamptz differences, so they stay true across
-- both transitions -- a shift running through the fall-back hour correctly
-- yields 25 hours of elapsed time, not 24.
--
-- business_date is taken from the BUCKET, not the shift, so a shift crossing
-- midnight lands its hours on the days they were actually worked. It uses the
-- same local-calendar-date contract as A6 (v_orders_time_context).
--
-- BREAKS ARE NOT RECORDED (break_data_status = 'unavailable'). labor_hours is
-- elapsed clocked time and INCLUDES any unpaid break taken. It is not verified
-- paid-worked time.
--
-- Open shifts (clock_out IS NULL) are EXCLUDED from hours and cost -- their
-- duration is not yet known -- and counted separately so they are visible
-- rather than silently missing.
CREATE OR REPLACE VIEW v_labor_hourly_context AS
WITH closed AS (
    SELECT id, establishment_id, employee_id, clock_in, clock_out,
           role_wage, estimated_labor_cost, is_auto_clock_out, role_wage IS NULL AS missing_wage,
           EXTRACT(epoch FROM (clock_out - clock_in)) AS shift_seconds
    FROM timesheet_entries_v2
    WHERE clock_out IS NOT NULL
      AND clock_out > clock_in
), buckets AS (
    SELECT
        c.*,
        b AS bucket_start,
        -- Overlap of the shift with this one-hour bucket.
        EXTRACT(epoch FROM (
            LEAST(c.clock_out, b + interval '1 hour') - GREATEST(c.clock_in, b)
        )) AS bucket_seconds
    FROM closed c
    CROSS JOIN LATERAL generate_series(
        -- Truncate to the local hour, then return to absolute time.
        (date_trunc('hour', c.clock_in AT TIME ZONE 'America/Chicago'))
            AT TIME ZONE 'America/Chicago',
        c.clock_out,
        interval '1 hour'
    ) AS b
)
SELECT
    establishment_id,
    (bucket_start AT TIME ZONE 'America/Chicago')::date          AS business_date,
    EXTRACT(hour FROM (bucket_start AT TIME ZONE 'America/Chicago'))::int
                                                                 AS local_hour,
    ROUND((SUM(bucket_seconds) / 3600.0)::numeric, 4)            AS labor_hours,
    -- Allocated by the same fraction of shift duration, so the hourly rows sum
    -- back to the shift's stored estimated_labor_cost.
    ROUND(SUM(estimated_labor_cost * (bucket_seconds / NULLIF(shift_seconds, 0)))::numeric, 4)
                                                                 AS estimated_labor_cost,
    COUNT(DISTINCT employee_id)                                  AS employee_count,
    -- Shifts overlapping this hour. NOT summable across hours -- one shift
    -- spans many hours. Use v_labor_daily_context.shifts_started_count for a
    -- count of shifts worked.
    COUNT(DISTINCT id)                                           AS shift_overlap_count,
    COUNT(DISTINCT id) FILTER (WHERE missing_wage)               AS missing_wage_shift_count,
    COUNT(DISTINCT id) FILTER (WHERE is_auto_clock_out)          AS auto_clock_out_count,
    'unavailable'::text                                          AS break_data_status
FROM buckets
WHERE bucket_seconds > 0
GROUP BY 1, 2, 3;

COMMENT ON VIEW v_labor_hourly_context IS
    'Labour by establishment x business_date x local hour, with each shift '
    'SPLIT proportionally across every hour it spans (not assigned to its '
    'clock-in hour). Chicago local time, DST-correct. labor_hours is elapsed '
    'clocked time and INCLUDES unpaid breaks -- breaks are not recorded for '
    'this account. estimated_labor_cost excludes overtime, burden, taxes and '
    'benefits: it is NOT payroll cost. No employee_id is exposed.';

-- ---------------------------------------------------------------------------
-- 3. LABOUR -- one row per establishment x business_date
-- ---------------------------------------------------------------------------
-- employee_count is computed from the raw table, NOT summed from the hourly
-- view: summing hourly distinct counts would count one person once per hour
-- they worked. Counted here before the ids are dropped, and only the count
-- leaves.
--
-- A shift is attributed to a day by the hours actually worked on that day
-- (consistent with the hourly view), so a shift crossing midnight contributes
-- to both dates.
CREATE OR REPLACE VIEW v_labor_daily_context AS
WITH hourly AS (
    SELECT establishment_id, business_date,
           SUM(labor_hours)             AS labor_hours,
           SUM(estimated_labor_cost)    AS estimated_labor_cost,
           SUM(auto_clock_out_count)    AS auto_clock_out_count
    FROM v_labor_hourly_context
    GROUP BY 1, 2
), per_day AS (
    SELECT
        t.establishment_id,
        (b AT TIME ZONE 'America/Chicago')::date        AS business_date,
        t.employee_id,
        t.id,
        t.role_wage IS NULL                             AS missing_wage
    FROM timesheet_entries_v2 t
    CROSS JOIN LATERAL generate_series(
        (date_trunc('hour', t.clock_in AT TIME ZONE 'America/Chicago'))
            AT TIME ZONE 'America/Chicago',
        t.clock_out,
        interval '1 hour'
    ) AS b
    WHERE t.clock_out IS NOT NULL AND t.clock_out > t.clock_in
      AND LEAST(t.clock_out, b + interval '1 hour') > GREATEST(t.clock_in, b)
), ids AS (
    SELECT establishment_id, business_date,
           COUNT(DISTINCT employee_id)                       AS unique_employee_count,
           COUNT(DISTINCT id)                                AS shift_day_count,
           COUNT(DISTINCT id) FILTER (WHERE missing_wage)    AS missing_wage_shift_count
    FROM per_day GROUP BY 1, 2
), started AS (
    -- Shifts whose clock_in falls on this day. This one IS safely summable
    -- across days and is the right count for "how many shifts were worked".
    SELECT establishment_id,
           (clock_in AT TIME ZONE 'America/Chicago')::date AS business_date,
           COUNT(*) AS shifts_started_count
    FROM timesheet_entries_v2 WHERE clock_out IS NOT NULL GROUP BY 1, 2
), open_shifts AS (
    SELECT establishment_id,
           (clock_in AT TIME ZONE 'America/Chicago')::date AS business_date,
           COUNT(*) AS open_shift_count
    FROM timesheet_entries_v2 WHERE clock_out IS NULL GROUP BY 1, 2
)
SELECT
    h.establishment_id,
    h.business_date,
    ROUND(h.labor_hours, 4)                     AS labor_hours,
    ROUND(h.estimated_labor_cost, 4)            AS estimated_labor_cost,
    i.unique_employee_count,
    -- NOT summable across days: a shift crossing midnight contributes hours to
    -- two dates and so counts on both. Named shift_day_count rather than
    -- shift_count precisely so nobody sums it and reports more shifts than were
    -- worked (Nederland June: 484 shift-days vs 380 actual shifts).
    i.shift_day_count,
    COALESCE(st.shifts_started_count, 0)        AS shifts_started_count,
    i.missing_wage_shift_count,
    h.auto_clock_out_count,
    COALESCE(o.open_shift_count, 0)             AS open_shift_count,
    'unavailable'::text                         AS break_data_status
FROM hourly h
JOIN ids i  ON i.establishment_id = h.establishment_id AND i.business_date = h.business_date
LEFT JOIN started st ON st.establishment_id = h.establishment_id AND st.business_date = h.business_date
LEFT JOIN open_shifts o ON o.establishment_id = h.establishment_id AND o.business_date = h.business_date;

COMMENT ON COLUMN v_labor_daily_context.shift_day_count IS
    'Shifts contributing hours to this day. NOT summable across days -- a '
    'midnight-crossing shift counts on both. Use shifts_started_count instead.';
COMMENT ON COLUMN v_labor_hourly_context.shift_overlap_count IS
    'Shifts overlapping this hour -- a staffing level, NOT a shift count. Not '
    'summable across hours.';

COMMENT ON VIEW v_labor_daily_context IS
    'Labour by establishment x business_date, aggregated from the hourly split '
    'so shifts crossing midnight land on the days actually worked. '
    'unique_employee_count is a COUNT computed before ids are dropped -- no '
    'employee_id is exposed. Same break and cost limitations as the hourly '
    'view: labor_hours includes unpaid breaks; estimated_labor_cost is NOT '
    'payroll cost.';

-- ---------------------------------------------------------------------------
-- Grants: the three safe views only. The raw tables stay denied.
-- ---------------------------------------------------------------------------
GRANT SELECT ON v_order_loyalty_context TO laynes_ro;
GRANT SELECT ON v_labor_hourly_context  TO laynes_ro;
GRANT SELECT ON v_labor_daily_context   TO laynes_ro;

REVOKE ALL ON order_loyalty_v2     FROM laynes_ro;
REVOKE ALL ON timesheet_entries_v2 FROM laynes_ro;

COMMIT;

-- Rollback:
--   DROP VIEW IF EXISTS v_labor_daily_context;
--   DROP VIEW IF EXISTS v_labor_hourly_context;
--   DROP VIEW IF EXISTS v_order_loyalty_context;
