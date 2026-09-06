"""Permanent regression: loyalty/labour analytics semantics and safety.

Covers the claims the assistant must never make (membership from evidence,
payroll from estimated cost, staffing verdicts from labour alone), the
structural guarantees (no identifiers in the safe views, raw tables denied),
and the arithmetic that is easy to get silently wrong (hourly shift splitting,
DST, non-summable counts).
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import unittest

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
            override=True)

import chat_sql as cs

SAFE_VIEWS = ("v_order_loyalty_context", "v_labor_hourly_context", "v_labor_daily_context")
RAW_TABLES = ("order_loyalty_v2", "timesheet_entries_v2")

FORBIDDEN_COLUMNS = (
    "loyalty_key_hash", "customer_id", "employee_id", "externalid", "external_id",
    "printedcardnumber", "printed_card_number", "gift_reward_data", "safe_customer_key",
    "customername", "firstname", "lastname", "phonenumber", "birthday", "email",
)


def connect():
    return psycopg2.connect(host=os.getenv("DB_HOST", "localhost"),
                            dbname=os.getenv("DB_NAME", "laynes"),
                            user=os.environ["DB_USER"], password=os.environ["DB_PASS"])


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect()
        cls.cur = cls.conn.cursor()
        # Flatten: the prompt is hard-wrapped, so a phrase that reads as one
        # sentence spans a newline plus indentation in the source.
        raw = (cs.LOYALTY_RULES + cs.LABOR_RULES + cs.IDENTITY_RULES
               + cs.SCHEMA_DOC + cs.GUARDRAILS)
        cls.prompt = re.sub(r"\s+", " ", raw)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def q(self, sql, params=None):
        self.cur.execute(sql, params)
        return self.cur.fetchall()


class TestSafeViewsExposeNoIdentifiers(Base):
    def test_no_forbidden_columns(self):
        for view in SAFE_VIEWS:
            self.cur.execute("""SELECT column_name FROM information_schema.columns
                                WHERE table_name = %s""", (view,))
            for (col,) in self.cur.fetchall():
                for term in FORBIDDEN_COLUMNS:
                    self.assertNotIn(term, col.lower(), f"{view}.{col} exposes {term!r}")

    def test_no_employee_id_in_labor_views(self):
        for view in ("v_labor_hourly_context", "v_labor_daily_context"):
            self.cur.execute("""SELECT count(*) FROM information_schema.columns
                                WHERE table_name=%s AND column_name LIKE %s""",
                             (view, "%employee%id%"))
            self.assertEqual(self.cur.fetchone()[0], 0)

    def test_labor_cannot_be_joined_to_an_order(self):
        """No order column anywhere in the labour views."""
        for view in ("v_labor_hourly_context", "v_labor_daily_context"):
            self.cur.execute("""SELECT count(*) FROM information_schema.columns
                                WHERE table_name=%s AND column_name LIKE %s""",
                             (view, "%order%"))
            self.assertEqual(self.cur.fetchone()[0], 0,
                             f"{view} has an order column; labour must not be "
                             "attributable to individual orders")

    def test_loyalty_view_has_no_points_balance(self):
        self.cur.execute("""SELECT count(*) FROM information_schema.columns
                            WHERE table_name='v_order_loyalty_context'
                              AND column_name IN ('total_points_snapshot','total_points')""")
        self.assertEqual(self.cur.fetchone()[0], 0)


class TestPrivilegeSeparation(Base):
    def test_llm_role_can_read_safe_views(self):
        for view in SAFE_VIEWS:
            self.cur.execute("SELECT has_table_privilege('laynes_ro', %s, 'SELECT')", (view,))
            self.assertTrue(self.cur.fetchone()[0], f"laynes_ro cannot read {view}")

    def test_llm_role_cannot_read_raw_tables(self):
        for table in RAW_TABLES:
            self.cur.execute("SELECT has_table_privilege('laynes_ro', %s, 'SELECT')", (table,))
            self.assertFalse(self.cur.fetchone()[0], f"laynes_ro CAN read raw {table}")

    def test_allowlist_matches_grants(self):
        for view in SAFE_VIEWS:
            self.assertIn(view, cs._ALLOWED_RELATIONS)
        for table in RAW_TABLES:
            self.assertNotIn(table, cs._ALLOWED_RELATIONS)

    def test_safe_views_are_scope_gated_not_reference(self):
        """They carry per-order/per-day rows, so they need period scope."""
        for view in SAFE_VIEWS:
            self.assertNotIn(view, cs._REFERENCE_RELATIONS)


class TestHourlySplitting(Base):
    def test_shift_split_across_spanned_hours(self):
        rows = self.q("""
            WITH c AS (SELECT timestamptz '2026-06-15 17:30:00-05' AS ci,
                              timestamptz '2026-06-15 19:15:00-05' AS co)
            SELECT EXTRACT(hour FROM (b AT TIME ZONE 'America/Chicago'))::int,
                   round(EXTRACT(epoch FROM (LEAST(c.co,b+interval '1 hour')
                                             - GREATEST(c.ci,b)))/3600.0, 4)
            FROM c CROSS JOIN LATERAL generate_series(
                (date_trunc('hour', c.ci AT TIME ZONE 'America/Chicago'))
                    AT TIME ZONE 'America/Chicago', c.co, interval '1 hour') b
            WHERE LEAST(c.co,b+interval '1 hour') > GREATEST(c.ci,b) ORDER BY 1""")
        self.assertEqual([(h, float(v)) for h, v in rows],
                         [(17, 0.5), (18, 1.0), (19, 0.25)])

    def test_dst_spring_forward_has_no_2am_bucket(self):
        rows = self.q("""SELECT DISTINCT local_hour FROM v_labor_hourly_context
                         WHERE business_date = '2026-03-08' AND local_hour = 2""")
        self.assertEqual(rows, [], "02:00 exists on a spring-forward date")

    def test_dst_fall_back_counts_the_repeated_hour_twice(self):
        rows = self.q("""
            WITH c AS (SELECT timestamptz '2026-11-01 00:30:00-05' AS ci,
                              timestamptz '2026-11-01 03:00:00-06' AS co)
            SELECT round(SUM(EXTRACT(epoch FROM (LEAST(c.co,b+interval '1 hour')
                                                 - GREATEST(c.ci,b))))/3600.0, 4)
            FROM c CROSS JOIN LATERAL generate_series(
                (date_trunc('hour', c.ci AT TIME ZONE 'America/Chicago'))
                    AT TIME ZONE 'America/Chicago', c.co, interval '1 hour') b
            WHERE LEAST(c.co,b+interval '1 hour') > GREATEST(c.ci,b)""")
        # 00:30 CDT -> 03:00 CST is 3.5 elapsed hours, not 2.5.
        self.assertEqual(float(rows[0][0]), 3.5)

    def test_hourly_sums_to_daily(self):
        h = self.q("""SELECT round(sum(labor_hours),2) FROM v_labor_hourly_context
                      WHERE establishment_id=26 AND business_date >= '2026-06-01'
                        AND business_date < '2026-07-01'""")[0][0]
        d = self.q("""SELECT round(sum(labor_hours),2) FROM v_labor_daily_context
                      WHERE establishment_id=26 AND business_date >= '2026-06-01'
                        AND business_date < '2026-07-01'""")[0][0]
        self.assertEqual(h, d)

    def test_no_shift_dumped_into_its_clock_in_hour(self):
        """A long shift must touch more than one hour bucket."""
        n = self.q("""SELECT count(DISTINCT local_hour) FROM v_labor_hourly_context
                      WHERE establishment_id=26 AND business_date='2026-06-15'""")[0][0]
        self.assertGreater(n, 4)


class TestNonSummableCounts(Base):
    def test_daily_shift_day_count_is_named_to_prevent_summing(self):
        cols = [r[0] for r in self.q("""SELECT column_name FROM information_schema.columns
                                        WHERE table_name='v_labor_daily_context'""")]
        self.assertIn("shift_day_count", cols)
        self.assertIn("shifts_started_count", cols)
        self.assertNotIn("shift_count", cols)

    def test_shifts_started_matches_reality(self):
        started = self.q("""SELECT sum(shifts_started_count) FROM v_labor_daily_context
                            WHERE establishment_id=26 AND business_date>='2026-06-01'
                              AND business_date<'2026-07-01'""")[0][0]
        actual = self.q("""SELECT count(*) FROM timesheet_entries_v2
                           WHERE establishment_id=26 AND clock_out IS NOT NULL
                             AND (clock_in AT TIME ZONE 'America/Chicago')::date >= '2026-06-01'
                             AND (clock_in AT TIME ZONE 'America/Chicago')::date <  '2026-07-01'""")[0][0]
        self.assertEqual(int(started), int(actual))

    def test_hourly_count_is_named_as_an_overlap(self):
        cols = [r[0] for r in self.q("""SELECT column_name FROM information_schema.columns
                                        WHERE table_name='v_labor_hourly_context'""")]
        self.assertIn("shift_overlap_count", cols)


class TestPromptSemantics(Base):
    def test_evidence_is_not_membership(self):
        self.assertIn("LOYALTY EVIDENCE PRESENT", self.prompt)
        self.assertIn("NO LOYALTY EVIDENCE OBSERVED", self.prompt)
        self.assertIn("orders with loyalty evidence", self.prompt)
        self.assertIn("does NOT mean the guest is not a member", self.prompt)

    def test_forbids_loyalty_vs_non_loyalty_wording(self):
        self.assertIn('NEVER say "loyalty customers" vs "non-loyalty customers"', self.prompt)

    def test_absence_is_not_non_membership(self):
        self.assertIn("absence of evidence is not evidence of non-membership", self.prompt)

    def test_forbids_causal_loyalty_claims(self):
        self.assertIn("NEVER CLAIM CAUSATION", self.prompt)
        self.assertIn("confounded by that selection", self.prompt)

    def test_identity_is_not_loyalty(self):
        self.assertIn("CUSTOMER IDENTITY IS NOT LOYALTY MEMBERSHIP", self.prompt)
        self.assertIn("must NEVER be merged", self.prompt)

    def test_identity_capture_rate_still_stated(self):
        self.assertIn("10.69%", self.prompt)

    def test_breaks_unavailable_caveat(self):
        self.assertIn("BREAKS ARE NOT RECORDED", self.prompt)
        self.assertIn("INCLUDES any unpaid break", self.prompt)

    def test_estimated_cost_is_not_payroll(self):
        self.assertIn("IS NOT PAYROLL COST", self.prompt)
        self.assertIn("Never call it payroll", self.prompt)

    def test_forbids_staffing_verdicts(self):
        self.assertIn("NEVER DECLARE OVER- OR UNDERSTAFFING", self.prompt)
        self.assertIn("relatively high labour intensity", self.prompt)

    def test_labor_not_joined_to_orders(self):
        self.assertIn("Never join labour to individual orders", self.prompt)

    def test_real_denominator_required(self):
        self.assertIn("REAL orders and REAL sales only", self.prompt)

    def test_no_per_person_loyalty_analysis(self):
        self.assertIn("NO PER-PERSON LOYALTY ANALYSIS", self.prompt)


class TestAdvisoriesDoNotFailA12(Base):
    def test_meta_blocks_are_advisory(self):
        meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
        self.assertTrue(meta["loyalty"]["advisory_only"])
        self.assertTrue(meta["labor"]["advisory_only"])
        self.assertIn("never affects the A12", meta["loyalty"]["limitations"])
        self.assertIn("never affects the A12", meta["labor"]["limitations"])

    def test_low_loyalty_rate_does_not_fail_the_gate(self):
        meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
        self.assertLess(meta["loyalty"]["evidence_rate_pct"], 10.0)
        self.assertEqual(meta["reconciliation"]["status"], "PASS")

    def test_unavailable_break_data_does_not_fail_the_gate(self):
        meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
        self.assertEqual(meta["labor"]["break_data_status"], "unavailable")
        self.assertEqual(meta["reconciliation"]["status"], "PASS")

    def test_meta_states_the_limitations(self):
        meta = cs.meta_extract(26, "2026-06-01", "2026-07-01")
        self.assertIn("NOT payroll cost", meta["labor"]["limitations"])
        self.assertIn("does NOT prove non-membership", meta["loyalty"]["limitations"])


class TestIncrementalSyncIdempotent(Base):
    def test_upserts_are_idempotent_by_key(self):
        import backfill_loyalty_v2 as BL
        import backfill_timesheets_v2 as BT
        self.assertIn("ON CONFLICT (order_id) DO UPDATE", BL.UPSERT)
        self.assertIn("ON CONFLICT (id) DO UPDATE", BT.UPSERT)

    def test_sync_uses_updated_date_and_overlap(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "sync_loyalty_labor.py")).read()
        self.assertIn("updated_date__gte", src)
        self.assertIn("OVERLAP_HOURS = 48", src)

    def test_sync_never_archives_loyalty(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "sync_loyalty_labor.py")).read()
        self.assertIn("resource=None", src)

    def test_daily_checkpoints_are_separate_from_backfill(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "sync_loyalty_labor.py")).read()
        self.assertIn("order_loyalty_v2_daily", src)
        self.assertIn("timesheet_entries_v2_daily", src)

    def test_reapplying_a_row_changes_nothing(self):
        """Re-UPSERT an existing loyalty row and confirm the values are stable."""
        before = self.q("""SELECT order_id, has_loyalty_payload, loyalty_registered,
                                  applied_rewards_count, loyalty_key_hash
                           FROM order_loyalty_v2 WHERE has_loyalty_payload LIMIT 1""")[0]
        self.cur.execute("""
            INSERT INTO order_loyalty_v2 (order_id, has_loyalty_payload, loyalty_registered,
                                          applied_rewards_count, loyalty_key_hash)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (order_id) DO UPDATE SET
                has_loyalty_payload = EXCLUDED.has_loyalty_payload,
                loyalty_registered  = EXCLUDED.loyalty_registered,
                applied_rewards_count = EXCLUDED.applied_rewards_count,
                loyalty_key_hash    = EXCLUDED.loyalty_key_hash""", before)
        self.conn.commit()
        after = self.q("""SELECT order_id, has_loyalty_payload, loyalty_registered,
                                 applied_rewards_count, loyalty_key_hash
                          FROM order_loyalty_v2 WHERE order_id = %s""", (before[0],))[0]
        self.assertEqual(tuple(before), tuple(after))


if __name__ == "__main__":
    unittest.main(verbosity=1)
