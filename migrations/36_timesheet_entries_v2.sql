-- Migration 36: labour timesheet facts (A-labor prep)
--
-- Source: Revel /resources/TimeSheetEntry/ -- 526,919 rows back to 2021-01-05,
-- 189,329 with clock_in >= 2026-01-01, current to the latest sync. Filters on
-- establishment, clock_in, created_date and updated_date all work, so both a
-- historical backfill and an updated_date incremental are supported.
--
-- BREAKS ARE NOT RECORDED. The resource exposes NO break-duration field at
-- all -- only break_type, which is NULL on every row observed (379/379 for
-- Nederland June). break_seconds below therefore has no upstream source and
-- stays NULL; it exists so the gap is visible rather than implied.
-- worked_seconds is simply
-- clock_out - clock_in, which INCLUDES any unpaid break actually taken. That
-- overstates paid-worked time by an unknown amount, so break_data_status is
-- stored as a column rather than left as tribal knowledge, and no consumer may
-- describe worked_seconds as verified paid time.
--
-- No employee PII is ingested. The API exposes only an employee URI, and the
-- free-text `remarks` field is deliberately not selected or stored.
--
-- ROLE NAMES: role_name_raw is kept verbatim. role_name_normalized folds only
-- pairs proven equivalent by inspection -- "Shift Manager"/"Shift MGR" are the
-- one observed case. No broader role taxonomy is invented.

BEGIN;

CREATE TABLE IF NOT EXISTS timesheet_entries_v2 (
    id                    BIGINT PRIMARY KEY,
    employee_id           INTEGER,
    establishment_id      INTEGER,
    clock_in              TIMESTAMPTZ,
    clock_out             TIMESTAMPTZ,
    worked_seconds        INTEGER,
    break_seconds         INTEGER,
    break_data_status     TEXT NOT NULL DEFAULT 'unavailable'
        CHECK (break_data_status IN ('unavailable', 'recorded')),
    department_name       TEXT,
    role_name_raw         TEXT,
    role_name_normalized  TEXT,
    role_wage             NUMERIC(10,4),
    estimated_labor_cost  NUMERIC(12,4),
    exempt_salaried       BOOLEAN,
    is_auto_clock_out     BOOLEAN,
    parent_id             BIGINT,
    source_created_date   TIMESTAMPTZ,
    source_updated_date   TIMESTAMPTZ,
    extracted_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_ts_v2_est_clockin
    ON timesheet_entries_v2 (establishment_id, clock_in);
CREATE INDEX IF NOT EXISTS ix_ts_v2_updated
    ON timesheet_entries_v2 (source_updated_date);
CREATE INDEX IF NOT EXISTS ix_ts_v2_employee
    ON timesheet_entries_v2 (employee_id);

COMMENT ON TABLE timesheet_entries_v2 IS
    'Labour timesheet entries. No employee name, email, phone or free-text '
    'remark is ingested -- employee_id only. worked_seconds = clock_out - '
    'clock_in and INCLUDES unpaid breaks, because Revel records no break data '
    'for this account (break_data_status = ''unavailable''). Do not present it '
    'as verified paid-worked time.';
COMMENT ON COLUMN timesheet_entries_v2.estimated_labor_cost IS
    'worked_seconds/3600 * role_wage. ESTIMATED: excludes overtime rules, '
    'burden, taxes and benefits, and is NULL-contributing where role_wage is '
    'absent (22 of 379 Nederland June entries had none).';
COMMENT ON COLUMN timesheet_entries_v2.role_name_normalized IS
    'Only proven-equivalent spellings are folded (Shift Manager / Shift MGR). '
    'No broader role taxonomy is asserted.';

COMMIT;
-- Grant deliberately NOT issued; expose via an aggregate view once agreed.

-- Rollback:
--   DROP TABLE IF EXISTS timesheet_entries_v2;
