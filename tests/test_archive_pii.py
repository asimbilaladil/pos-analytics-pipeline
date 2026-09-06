"""Permanent regression: Order raw archives must never contain gift_reward_data.

Every payload here is SYNTHETIC AND FAKE. No value came from the live account
or from the existing archive.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


import gzip
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone

import raw_archive


def fake_order(oid=1, with_loyalty=True):
    o = {
        "id": oid,
        "establishment": "/enterprise/Establishment/6/",
        "created_date": "2026-06-01T12:00:00",
        "updated_date": "2026-06-01T12:30:00",
        "final_total": 24.50,
        "dining_option": 0,
    }
    if with_loyalty:
        o["gift_reward_data"] = json.dumps({
            "isRegistered": True,
            "customerName": "Fake Testperson",
            "firstName": "Fake",
            "lastName": "Testperson",
            "phoneNumber": "+15550000000",
            "birthday": "1990-01-01",
            "printedCardNumber": "0000000000",
            "externalId": "fake-external-id-1",
            "totalPoints": 250,
        })
    return o


def fake_body(n=3, with_loyalty=True):
    return json.dumps({
        "meta": {"total_count": n, "limit": 1000, "offset": 0},
        "objects": [fake_order(i, with_loyalty) for i in range(1, n + 1)],
    })


FAKE_PII_STRINGS = ("Fake", "Testperson", "5550000000", "1990-01-01",
                    "0000000000", "fake-external-id-1", "gift_reward_data")


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="archive_pii_test_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def archive(self, resource, raw_text, **kw):
        return raw_archive.archive_response(
            resource=resource, raw_text=raw_text,
            endpoint="https://example.invalid/resources/X/",
            query_params={"establishment": 6}, establishment_id=6,
            date_window=("2026-06-01T00:00:00", "2026-06-08T00:00:00"),
            page=1, offset=0, attempt=1, run_id="testrun",
            archive_date=date(2026, 6, 1),
            fetch_time=datetime.now(timezone.utc),
            pipeline_version="test", base_dir=self.root, **kw)

    def read(self, result):
        with gzip.open(result["json_path"], "rt") as fh:
            return fh.read()

    def meta(self, result):
        with open(result["meta_path"]) as fh:
            return json.load(fh)


class TestOrderPIIStripped(ArchiveCase):
    def test_gift_reward_data_key_absent_from_archive(self):
        res = self.archive("orders", fake_body())
        for obj in json.loads(self.read(res))["objects"]:
            self.assertNotIn("gift_reward_data", obj)

    def test_no_fake_pii_string_survives_to_disk(self):
        res = self.archive("orders", fake_body())
        text = self.read(res)
        for needle in FAKE_PII_STRINGS:
            self.assertNotIn(needle, text, f"{needle!r} reached the archive")

    def test_compressed_bytes_contain_no_pii(self):
        """Check the raw file too, not just the decompressed view."""
        res = self.archive("orders", fake_body())
        with open(res["json_path"], "rb") as fh:
            blob = fh.read()
        for needle in FAKE_PII_STRINGS:
            self.assertNotIn(needle.encode(), blob)

    def test_meta_sidecar_contains_no_pii(self):
        res = self.archive("orders", fake_body())
        text = json.dumps(self.meta(res))
        for needle in FAKE_PII_STRINGS[:-1]:   # the field name itself is named in meta
            self.assertNotIn(needle, text)

    def test_all_other_order_fields_preserved(self):
        res = self.archive("orders", fake_body(n=2))
        got = json.loads(self.read(res))
        self.assertEqual(got["meta"], {"total_count": 2, "limit": 1000, "offset": 0})
        for i, obj in enumerate(got["objects"], start=1):
            expected = fake_order(i, with_loyalty=False)
            self.assertEqual(obj, expected)

    def test_record_count_unchanged(self):
        res = self.archive("orders", fake_body(n=5))
        self.assertEqual(len(json.loads(self.read(res))["objects"]), 5)
        self.assertEqual(self.meta(res)["object_count"], 5)

    def test_sanitation_recorded_in_meta(self):
        meta = self.meta(self.archive("orders", fake_body(n=3)))
        self.assertTrue(meta["pii_sanitized"])
        self.assertEqual(meta["pii_fields"], ["gift_reward_data"])
        self.assertEqual(meta["pii_fields_removed"], 3)
        self.assertFalse(meta["body_withheld_unparseable"])

    def test_orders_without_the_field_are_untouched(self):
        res = self.archive("orders", fake_body(n=2, with_loyalty=False))
        self.assertEqual(self.meta(res)["pii_fields_removed"], 0)
        self.assertEqual(len(json.loads(self.read(res))["objects"]), 2)

    def test_null_valued_field_still_removed(self):
        body = json.dumps({"meta": {}, "objects": [{"id": 1, "gift_reward_data": None}]})
        res = self.archive("orders", body)
        self.assertNotIn("gift_reward_data", json.loads(self.read(res))["objects"][0])


class TestUnparseableBodyWithheld(ArchiveCase):
    def test_unparseable_order_body_is_not_written(self):
        broken = '{"objects": [{"id": 1, "gift_reward_data": "Fake Testperson" broken'
        res = self.archive("orders", broken)
        text = self.read(res)
        for needle in ("Fake", "Testperson"):
            self.assertNotIn(needle, text)

    def test_withheld_body_is_recorded_not_silently_dropped(self):
        res = self.archive("orders", '{"objects": [ broken')
        meta = self.meta(res)
        self.assertTrue(meta["body_withheld_unparseable"])
        self.assertIn("pii_sanitize_parse_error", meta)
        self.assertIn("_archive_note", json.loads(self.read(res)))

    def test_withheld_archive_is_still_valid_json(self):
        res = self.archive("orders", "not json at all")
        json.loads(self.read(res))   # must not raise


class TestOtherResourcesUnaffected(ArchiveCase):
    def test_non_order_body_archived_byte_identical(self):
        body = json.dumps({"meta": {"total_count": 1},
                           "objects": [{"id": 9, "gift_reward_data": "kept-verbatim"}]})
        res = self.archive("payments", body)
        self.assertEqual(self.read(res), body)

    def test_malformed_non_order_body_still_archived_verbatim(self):
        """The existing contract for non-PII resources must not regress."""
        broken = '{"objects": [ this is not json'
        res = self.archive("timesheets", broken)
        self.assertEqual(self.read(res), broken)
        self.assertIsNotNone(self.meta(res)["parse_error"])

    def test_timesheets_declared_pii_free(self):
        self.assertNotIn("timesheets", raw_archive.PII_FIELDS_BY_RESOURCE)

    def test_orders_is_the_declared_pii_resource(self):
        self.assertEqual(raw_archive.PII_FIELDS_BY_RESOURCE["orders"], ("gift_reward_data",))


class TestReplayable(ArchiveCase):
    def test_sanitized_archive_reads_back_through_the_normal_reader(self):
        res = self.archive("orders", fake_body(n=4))
        got = raw_archive.read_archived_response(res["json_path"])
        self.assertEqual(len(got["objects"]), 4)
        self.assertNotIn("gift_reward_data", got["objects"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
