-- 49: the authoritative AYG fiscal period calendar (2026 + 2027).
--
-- PROVENANCE. These 26 rows are transcribed from the official AYG calendar by
-- period, supplied to Asim following Masroor's request to Brieanna Smith. They
-- are NOT derived from sales data and NOT computed from a 4-4-5 or 13x28 rule.
-- Before this file existed, fiscal-period resolution was deliberately BLOCKED
-- rather than guessed, because a wrong period boundary silently misstates every
-- period comparison built on top of it.
--
-- Do not extrapolate. 2025 and 2028 are absent on purpose: a period calendar is
-- an administrative decision, not arithmetic, so a year nobody has published is
-- a year this table must not claim to know. A question about an unlisted year
-- must fail to resolve rather than return an invented range.
--
-- The supplied data was checked before loading: each year is 13 periods of
-- exactly 28 days (364 days), fully contiguous with no gap or overlap, and both
-- fiscal years begin on a Thursday. Note FY2027 P1 starts 2026-12-31 -- a
-- fiscal year does not align to the calendar year, which is exactly the kind of
-- boundary that guessing would have got wrong.

CREATE TABLE IF NOT EXISTS ayg_business_periods (
    fiscal_year   integer     NOT NULL,
    period_number integer     NOT NULL,
    period_label  text        NOT NULL,
    start_date    date        NOT NULL,
    end_date      date        NOT NULL,
    source        text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ayg_business_periods_pkey
        PRIMARY KEY (fiscal_year, period_number),
    CONSTRAINT ayg_business_periods_number_range
        CHECK (period_number BETWEEN 1 AND 13),
    CONSTRAINT ayg_business_periods_date_order
        CHECK (end_date >= start_date)
);

-- A period's dates must be unique in their own right: two periods sharing a
-- start date would mean a transcription slip, and the composite key above would
-- not catch it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ayg_periods_start
    ON ayg_business_periods (start_date);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ayg_periods_end
    ON ayg_business_periods (end_date);
CREATE INDEX IF NOT EXISTS ix_ayg_periods_range
    ON ayg_business_periods (start_date, end_date);

INSERT INTO ayg_business_periods
    (fiscal_year, period_number, period_label, start_date, end_date, source)
VALUES
    -- FY2026
    (2026,  1, 'P1',  '2026-01-01', '2026-01-28', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026,  2, 'P2',  '2026-01-29', '2026-02-25', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026,  3, 'P3',  '2026-02-26', '2026-03-25', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026,  4, 'P4',  '2026-03-26', '2026-04-22', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026,  5, 'P5',  '2026-04-23', '2026-05-20', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026,  6, 'P6',  '2026-05-21', '2026-06-17', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026,  7, 'P7',  '2026-06-18', '2026-07-15', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026,  8, 'P8',  '2026-07-16', '2026-08-12', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026,  9, 'P9',  '2026-08-13', '2026-09-09', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026, 10, 'P10', '2026-09-10', '2026-10-07', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026, 11, 'P11', '2026-10-08', '2026-11-04', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026, 12, 'P12', '2026-11-05', '2026-12-02', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2026, 13, 'P13', '2026-12-03', '2026-12-30', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    -- FY2027 (P1 begins 2026-12-31, inside the 2026 calendar year)
    (2027,  1, 'P1',  '2026-12-31', '2027-01-27', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027,  2, 'P2',  '2027-01-28', '2027-02-24', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027,  3, 'P3',  '2027-02-25', '2027-03-24', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027,  4, 'P4',  '2027-03-25', '2027-04-21', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027,  5, 'P5',  '2027-04-22', '2027-05-19', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027,  6, 'P6',  '2027-05-20', '2027-06-16', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027,  7, 'P7',  '2027-06-17', '2027-07-14', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027,  8, 'P8',  '2027-07-15', '2027-08-11', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027,  9, 'P9',  '2027-08-12', '2027-09-08', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027, 10, 'P10', '2027-09-09', '2027-10-06', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027, 11, 'P11', '2027-10-07', '2027-11-03', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027, 12, 'P12', '2027-11-04', '2027-12-01', 'AYG_OFFICIAL_PERIOD_CALENDAR'),
    (2027, 13, 'P13', '2027-12-02', '2027-12-29', 'AYG_OFFICIAL_PERIOD_CALENDAR')
ON CONFLICT (fiscal_year, period_number) DO NOTHING;

-- The safe analytical view. Reference data only: period identity and dates,
-- plus a status and elapsed-day count derived from the Chicago business date.
-- No revenue, no store, no order or guest row -- so there is nothing here to
-- reconcile and nothing to leak.
--
-- days_elapsed counts COMPLETE days only. Today is in progress and has no
-- settled sales, so counting it would overstate a period-to-date comparison by
-- one partial day -- the same trap that failed a whole 276-day window on
-- 2026-10-03 when a not-yet-existing date entered the completeness check.
-- For a COMPLETED period days_elapsed = days_total; for a FUTURE period it is 0.
CREATE OR REPLACE VIEW v_ayg_business_periods_llm AS
WITH today AS (
    SELECT (now() AT TIME ZONE 'America/Chicago')::date AS chicago_today
)
SELECT
    p.fiscal_year,
    p.period_number,
    p.period_label,
    p.start_date,
    p.end_date,
    CASE
        WHEN t.chicago_today <  p.start_date THEN 'FUTURE'
        WHEN t.chicago_today >  p.end_date   THEN 'COMPLETED'
        ELSE 'CURRENT'
    END AS period_status,
    (p.end_date - p.start_date + 1)                       AS days_total,
    GREATEST(0, LEAST(p.end_date, t.chicago_today - 1)
                - p.start_date + 1)                       AS days_elapsed
FROM ayg_business_periods p
CROSS JOIN today t;

COMMENT ON TABLE ayg_business_periods IS
    'Authoritative AYG fiscal period calendar, transcribed from the official '
    'AYG calendar by period. Not derived, not extrapolated. Only published '
    'years are present; an absent year must fail to resolve, never be guessed.';
COMMENT ON VIEW v_ayg_business_periods_llm IS
    'Safe chat-facing period calendar. days_elapsed counts COMPLETE days only '
    '(today excluded, because today has no settled sales).';
