"""Permanent regression suite: no PII may reach order_loyalty_v2 or
timesheet_entries_v2, and the documented data limitations must stay true.

These run against the live schema/data, so they also catch a future migration
or backfill change that reintroduces a forbidden field."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


import os
import re
import unittest

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
            override=True)

TABLES = ("order_loyalty_v2", "timesheet_entries_v2")

# Terms that must not appear as a column name in either table.
FORBIDDEN_COLUMNS = (
    "customername", "customer_name", "firstname", "first_name", "lastname",
    "last_name", "phonenumber", "phone_number", "phone", "birthday", "dob",
    "printedcardnumber", "printed_card_number", "card_number", "gift_reward_data",
    "remarks", "notes", "email", "address", "employee_name",
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

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()


class TestNoPIIColumns(Base):
    def test_no_forbidden_column_names(self):
        self.cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                            WHERE table_name IN %s""", (TABLES,))
        for table, col in self.cur.fetchall():
            for term in FORBIDDEN_COLUMNS:
                self.assertNotIn(term, col.lower(), f"{table}.{col} matches forbidden term {term!r}")

    def test_raw_payload_is_not_stored(self):
        """No column may hold the gift_reward_data blob under any name."""
        self.cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                            WHERE table_name = 'order_loyalty_v2'""")
        for col, dtype in self.cur.fetchall():
            self.assertNotIn(dtype, ("json", "jsonb"),
                             f"order_loyalty_v2.{col} is {dtype}; a JSON column here risks "
                             "storing the PII-bearing payload")


class TestNoPIIValues(Base):
    def _text_columns(self, table):
        self.cur.execute("""SELECT column_name FROM information_schema.columns
                            WHERE table_name=%s AND data_type IN ('text','character varying')""",
                         (table,))
        return [r[0] for r in self.cur.fetchall()]

    def test_no_email_shaped_values(self):
        for table in TABLES:
            for col in self._text_columns(table):
                self.cur.execute(f"SELECT count(*) FROM {table} WHERE {col} LIKE '%%@%%.%%'")
                self.assertEqual(self.cur.fetchone()[0], 0, f"{table}.{col} holds email-shaped text")

    def test_loyalty_key_hash_is_only_ever_a_digest(self):
        """The strongest guarantee that no raw externalId leaked: every stored
        value is exactly a 64-char sha256 hex digest and nothing else."""
        self.cur.execute("""SELECT count(*) FROM order_loyalty_v2
                            WHERE loyalty_key_hash IS NOT NULL
                              AND loyalty_key_hash !~ '^[0-9a-f]{64}$'""")
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_role_names_are_short_labels_not_people(self):
        self.cur.execute("""SELECT DISTINCT role_name_raw FROM timesheet_entries_v2
                            WHERE role_name_raw IS NOT NULL""")
        for (role,) in self.cur.fetchall():
            self.assertLess(len(role), 40, f"role_name_raw {role!r} is long enough to be free text")
            self.assertFalse(re.search(r"\d{5,}", role), f"role_name_raw {role!r} contains a long number")


class TestDocumentedLimitations(Base):
    def test_break_data_is_always_marked_unavailable(self):
        self.cur.execute("SELECT DISTINCT break_data_status FROM timesheet_entries_v2")
        statuses = {r[0] for r in self.cur.fetchall()}
        self.assertTrue(statuses <= {"unavailable", "recorded"})
        self.cur.execute("""SELECT count(*) FROM timesheet_entries_v2
                            WHERE break_data_status='recorded' AND break_seconds IS NULL""")
        self.assertEqual(self.cur.fetchone()[0], 0,
                         "a row claims recorded break data but stores no break_seconds")

    def test_break_seconds_null_while_status_unavailable(self):
        self.cur.execute("""SELECT count(*) FROM timesheet_entries_v2
                            WHERE break_data_status='unavailable' AND break_seconds IS NOT NULL""")
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_worked_seconds_never_negative(self):
        self.cur.execute("SELECT count(*) FROM timesheet_entries_v2 WHERE worked_seconds < 0")
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_open_shifts_have_no_fabricated_cost(self):
        self.cur.execute("""SELECT count(*) FROM timesheet_entries_v2
                            WHERE clock_out IS NULL AND
                                  (worked_seconds IS NOT NULL OR estimated_labor_cost IS NOT NULL)""")
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_role_normalization_folds_only_the_proven_alias(self):
        """Guards against a future broad taxonomy being invented."""
        self.cur.execute("""SELECT DISTINCT role_name_raw, role_name_normalized
                            FROM timesheet_entries_v2 WHERE role_name_raw IS NOT NULL""")
        for raw, norm in self.cur.fetchall():
            if raw != norm:
                self.assertEqual((raw, norm), ("Shift MGR", "Shift Manager"),
                                 f"unexpected role fold {raw!r} -> {norm!r}")

    def test_loyalty_coverage_stays_a_minority_signal(self):
        """If this ever flips to a majority the 'selection bias' framing in the
        assistant's prompt would need revisiting -- fail loudly rather than
        silently changing meaning."""
        self.cur.execute("""SELECT count(*), count(*) FILTER (WHERE has_loyalty_payload)
                            FROM order_loyalty_v2""")
        total, with_payload = self.cur.fetchone()
        if total:
            self.assertLess(with_payload / total, 0.5,
                            "loyalty payload coverage is no longer a minority of orders")

    def test_points_snapshot_is_non_negative(self):
        self.cur.execute("SELECT count(*) FROM order_loyalty_v2 WHERE total_points_snapshot < 0")
        self.assertEqual(self.cur.fetchone()[0], 0)


class TestNotExposedToAssistant(Base):
    def test_llm_role_cannot_read_either_table(self):
        """Neither table is granted to laynes_ro yet -- exposure happens later,
        through an agreed aggregate view, not by accident."""
        for table in TABLES:
            self.cur.execute("SELECT has_table_privilege('laynes_ro', %s, 'SELECT')", (table,))
            self.assertFalse(self.cur.fetchone()[0], f"laynes_ro can already read {table}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
