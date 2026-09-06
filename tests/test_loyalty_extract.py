"""Loyalty extraction tests. Every payload here is SYNTHETIC AND FAKE -- no
value in this file came from the live account or the raw archive."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


import json
import os
import unittest

os.environ.setdefault("LOYALTY_HASH_SECRET", "test-secret-not-production")

import loyalty_extract as lx


def fake_payload(**over):
    """A synthetic payload shaped like the real one, with invented PII."""
    p = {
        "isRegistered": True,
        "customerName": "Fake Testperson",
        "firstName": "Fake",
        "lastName": "Testperson",
        "phoneNumber": "+15550000000",
        "birthday": "1990-01-01",
        "printedCardNumber": "0000000000",
        "externalId": "fake-external-id-1",
        "totalPoints": 250,
        "appliedRewards": [{"name": "Fake Reward"}],
        "availableRewards": [],
        "applicableRewards": [],
        "appliedPaymentReward": None,
        "tags": [], "consent": True, "receiptText": "fake",
        "orderTotal": 12.34, "user": None, "orderVoided": False,
    }
    p.update(over)
    return p


class TestNoPIIEscapes(unittest.TestCase):
    def test_no_pii_key_in_output(self):
        out = lx.extract(fake_payload())
        for key in lx.PII_KEYS:
            self.assertNotIn(key, out)

    def test_no_pii_value_in_serialised_output(self):
        out = json.dumps(lx.extract(fake_payload()))
        for value in ("Fake", "Testperson", "5550000000", "1990-01-01", "0000000000"):
            self.assertNotIn(value, out)

    def test_output_keys_are_exactly_the_safe_set(self):
        self.assertEqual(set(lx.extract(fake_payload())), set(lx.SAFE_FIELDS))

    def test_empty_result_has_same_keys(self):
        self.assertEqual(set(lx.extract(None)), set(lx.SAFE_FIELDS))

    def test_card_number_reduced_to_a_boolean(self):
        out = lx.extract(fake_payload(printedCardNumber="1234567890"))
        self.assertIs(out["has_reward_card"], True)
        self.assertNotIn("1234567890", json.dumps(out))


class TestHashing(unittest.TestCase):
    def test_hash_is_not_the_raw_id(self):
        out = lx.extract(fake_payload(externalId="fake-abc"))
        self.assertNotIn("fake-abc", out["loyalty_key_hash"])

    def test_hash_is_stable(self):
        a = lx.extract(fake_payload(externalId="fake-x"))["loyalty_key_hash"]
        b = lx.extract(fake_payload(externalId="fake-x"))["loyalty_key_hash"]
        self.assertEqual(a, b)

    def test_different_ids_hash_differently(self):
        a = lx.extract(fake_payload(externalId="fake-x"))["loyalty_key_hash"]
        b = lx.extract(fake_payload(externalId="fake-y"))["loyalty_key_hash"]
        self.assertNotEqual(a, b)

    def test_hash_depends_on_secret(self):
        a = lx.hash_external_id("fake-x")
        os.environ["LOYALTY_HASH_SECRET"] = "a-different-secret"
        try:
            self.assertNotEqual(a, lx.hash_external_id("fake-x"))
        finally:
            os.environ["LOYALTY_HASH_SECRET"] = "test-secret-not-production"

    def test_missing_id_gives_none(self):
        self.assertIsNone(lx.extract(fake_payload(externalId=None))["loyalty_key_hash"])
        self.assertIsNone(lx.extract(fake_payload(externalId=""))["loyalty_key_hash"])

    def test_empty_secret_refused(self):
        saved = os.environ.pop("LOYALTY_HASH_SECRET")
        try:
            with self.assertRaises(RuntimeError):
                lx.hash_external_id("fake-x")
        finally:
            os.environ["LOYALTY_HASH_SECRET"] = saved


class TestAbsentAndMalformed(unittest.TestCase):
    def test_absent_forms(self):
        for raw in (None, "", "   ", "{}", "null", "[]", {}, []):
            self.assertFalse(lx.extract(raw)["has_loyalty_payload"], raw)

    def test_malformed_json_is_absent_not_an_error(self):
        for raw in ("{not json", '{"a":', "\x00\x01", "12345"):
            self.assertFalse(lx.extract(raw)["has_loyalty_payload"], raw)

    def test_malformed_payload_never_appears_in_output(self):
        out = json.dumps(lx.extract('{"firstName": "Fake", broken'))
        self.assertNotIn("Fake", out)

    def test_json_string_input_matches_dict_input(self):
        p = fake_payload()
        self.assertEqual(lx.extract(p), lx.extract(json.dumps(p)))

    def test_bytes_input(self):
        p = fake_payload()
        self.assertEqual(lx.extract(json.dumps(p).encode()), lx.extract(p))

    def test_undecodable_bytes(self):
        self.assertFalse(lx.extract(b"\xff\xfe\x00")["has_loyalty_payload"])


class TestFieldSemantics(unittest.TestCase):
    def test_registered_flag(self):
        self.assertIs(lx.extract(fake_payload(isRegistered=True))["loyalty_registered"], True)
        self.assertIs(lx.extract(fake_payload(isRegistered=False))["loyalty_registered"], False)

    def test_non_boolean_registered_becomes_none(self):
        self.assertIsNone(lx.extract(fake_payload(isRegistered="yes"))["loyalty_registered"])
        self.assertIsNone(lx.extract(fake_payload(isRegistered=None))["loyalty_registered"])

    def test_applied_rewards_counted(self):
        out = lx.extract(fake_payload(appliedRewards=[{"a": 1}, {"b": 2}]))
        self.assertEqual(out["applied_rewards_count"], 2)
        self.assertIs(out["has_applied_reward"], True)

    def test_no_rewards(self):
        out = lx.extract(fake_payload(appliedRewards=[], appliedPaymentReward=None))
        self.assertEqual(out["applied_rewards_count"], 0)
        self.assertIs(out["has_applied_reward"], False)

    def test_payment_reward_alone_counts_as_applied(self):
        out = lx.extract(fake_payload(appliedRewards=[], appliedPaymentReward={"x": 1}))
        self.assertIs(out["has_applied_reward"], True)

    def test_points_coercion(self):
        self.assertEqual(lx.extract(fake_payload(totalPoints="300"))["total_points_snapshot"], 300)
        self.assertEqual(lx.extract(fake_payload(totalPoints=12.9))["total_points_snapshot"], 12)
        self.assertIsNone(lx.extract(fake_payload(totalPoints=None))["total_points_snapshot"])
        self.assertIsNone(lx.extract(fake_payload(totalPoints="abc"))["total_points_snapshot"])

    def test_zero_points_is_kept_not_nulled(self):
        self.assertEqual(lx.extract(fake_payload(totalPoints=0))["total_points_snapshot"], 0)

    def test_missing_keys_do_not_raise(self):
        self.assertTrue(lx.extract({"isRegistered": True})["has_loyalty_payload"])

    def test_unknown_extra_keys_ignored(self):
        out = lx.extract(fake_payload(someFutureField={"secret": "Fake"}))
        self.assertNotIn("Fake", json.dumps(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
