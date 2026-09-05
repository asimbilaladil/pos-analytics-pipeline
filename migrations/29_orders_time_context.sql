-- Migration 29: explicit time contract for analytics (A6)
--
-- One documented answer to "what day/hour is this order", so nothing has to
-- infer it from a UTC timestamp or guess a weekday convention.
--
-- TIMEZONE, VERIFIED NOT ASSUMED ---------------------------------------------
-- Revel's API returns NAIVE local wall-clock strings in America/Chicago, and
-- pipeline.parse_dt localises them as Chicago before storing UTC timestamptz.
-- Checked against the live API rather than trusting the docstring: four
-- Nederland orders fetched from Revel returned created_date "2026-06-14T19:07:56"
-- etc., and the stored rows convert back to exactly 2026-06-14 19:07:56 Chicago
-- (00:07:56 UTC the following day). 4/4 exact. So
--     created_date AT TIME ZONE 'America/Chicago'
-- recovers the true wall-clock, and using the UTC date would misfile every
-- evening order after 19:00 CDT onto the following day.
--
-- BUSINESS DATE = LOCAL CALENDAR DATE, and that is a limitation ---------------
-- No authoritative rollover rule could be established, so none is invented:
--   1. orders_v2 has NO business-date column of any kind.
--   2. Revel exposes a BusinessDay resource, and it looked promising -- rows
--      carry date/establishment/opened/closed, and est 7 shows a 10:00 -> 10:00
--      window. But coverage kills it: 1,440 of its 1,541 rows belong to
--      establishment 7 alone and every other establishment has exactly ONE
--      stale row, mostly from 2021 with closed = NULL. Its opened times also
--      vary (10:00, 11:00, 05:00, 04:00), which reads as when a terminal was
--      cycled rather than a configured service-day boundary -- est 7's own
--      orders stop by 01:xx, nowhere near its 10:00 "close".
--   3. No operating-hours configuration is available for the other 11 stores.
-- Post-midnight trade is real but small and store-specific: 00:00-04:59 holds
-- 3.15% of REAL orders and 3.0% of revenue in June 2026 network-wide, with
-- Shepherd trading to 03:xx and Nederland stopping before midnight entirely.
-- That is evidence a rollover WOULD matter for some stores; it is NOT evidence
-- of where the cutoff sits, so business_date stays the local calendar date and
-- says so through business_date_method / business_date_confidence.
--
-- WEEKDAY -- three conventions already exist in this database ------------------
--   PostgreSQL dow     Sunday = 0 .. Saturday = 6
--   PostgreSQL isodow  Monday = 1 .. Sunday = 7
--   features_*_v2.day_of_week   Monday = 0 .. Sunday = 6  (Python weekday())
-- All three disagree about Sunday, and two disagree about every day. This view
-- publishes ISO explicitly alongside the name so no consumer has to guess.

BEGIN;

CREATE OR REPLACE VIEW v_orders_time_context AS
SELECT
    o.id                                                        AS order_id,
    o.establishment_id,
    (o.created_date AT TIME ZONE 'America/Chicago')             AS transaction_timestamp_local,
    (o.created_date AT TIME ZONE 'America/Chicago')::date       AS local_calendar_date,
    EXTRACT(hour FROM (o.created_date AT TIME ZONE 'America/Chicago'))::int
                                                                AS local_hour,
    EXTRACT(isodow FROM (o.created_date AT TIME ZONE 'America/Chicago'))::int
                                                                AS local_weekday_iso,
    to_char((o.created_date AT TIME ZONE 'America/Chicago'), 'FMDay')
                                                                AS local_weekday_name,
    -- Identical to local_calendar_date by design, kept as its own column so a
    -- future verified rollover rule changes one definition here rather than
    -- every query that ever asked for a business date.
    (o.created_date AT TIME ZONE 'America/Chicago')::date        AS business_date,
    'local_calendar_date'::text                                  AS business_date_method,
    'limited'::text                                              AS business_date_confidence,
    'created_date'::text                                         AS transaction_timestamp_source,
    'America/Chicago'::text                                      AS source_timezone
FROM orders_v2 o;

COMMENT ON VIEW v_orders_time_context IS
    'The time contract for order analytics. transaction_timestamp_local is '
    'orders_v2.created_date rendered as America/Chicago wall-clock -- verified '
    'against the Revel API. business_date currently EQUALS local_calendar_date '
    'because no authoritative rollover rule exists for these stores; '
    'business_date_confidence = ''limited'' says so. local_weekday_iso is '
    'ISO-8601 (Monday=1..Sunday=7), which is NOT the convention used by '
    'features_*_v2.day_of_week (Monday=0..Sunday=6).';

COMMIT;

GRANT SELECT ON v_orders_time_context TO laynes_ro;

-- Rollback:
--   DROP VIEW IF EXISTS v_orders_time_context;
