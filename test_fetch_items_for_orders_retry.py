"""
test_fetch_items_for_orders_retry.py
=====================================
Task 10 retry hardening: proves pipeline.fetch_items_for_orders' with_retries
wrap (attempts=3, delay=5, same helper fetch_all_pages already uses) actually
retries a transient failure instead of aborting on the first one, and still
raises after a genuinely persistent failure exhausts all 3 attempts rather
than silently returning partial/empty results.

Regression trigger: a live Task 10 chunk (est=15, 2026-04) failed twice with
"APIRequestContext.get: Timeout 30000ms exceeded" because the pre-fix
fetch_items_for_orders had no retry around its single context.request.get()
call at all -- one slow response killed the whole order_ids batch.

No pytest in this repo yet -- unittest (stdlib) only.
"""
import json
import unittest
from unittest.mock import MagicMock

import pipeline as P


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def text(self):
        return self._body


def _ok_response(objects, total_count=None):
    total_count = len(objects) if total_count is None else total_count
    return FakeResponse(200, json.dumps({"objects": objects, "meta": {"total_count": total_count}}))


class FetchItemsForOrdersRetryTests(unittest.TestCase):
    def setUp(self):
        # with_retries sleeps `delay` seconds between attempts -- patch to keep this test fast.
        self._orig_sleep = P.time.sleep
        P.time.sleep = lambda *a, **k: None

    def tearDown(self):
        P.time.sleep = self._orig_sleep

    def test_timeout_on_first_attempt_succeeds_on_retry(self):
        """A transient failure on attempt 1 must not abort the fetch -- attempt 2
        succeeds and its data is returned. Proves the retry wrap actually retries."""
        item = {"id": 1, "order": "/resources/Order/1/", "product": "/resources/Product/1/"}
        get_mock = MagicMock(side_effect=[
            TimeoutError("APIRequestContext.get: Timeout 30000ms exceeded."),
            _ok_response([item]),
        ])
        context = MagicMock()
        context.request.get = get_mock

        items = P.fetch_items_for_orders(context, order_ids=[1], run_id=None)

        self.assertEqual(get_mock.call_count, 2, "should retry exactly once after the first failure")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 1)

    def test_persistent_failure_raises_after_exactly_three_attempts(self):
        """A failure on every attempt must exhaust with_retries' 3 attempts and then
        raise -- never silently return partial/empty results for a batch that never
        actually succeeded."""
        get_mock = MagicMock(side_effect=[
            TimeoutError("timeout 1"),
            TimeoutError("timeout 2"),
            TimeoutError("timeout 3"),
        ])
        context = MagicMock()
        context.request.get = get_mock

        with self.assertRaises(TimeoutError):
            P.fetch_items_for_orders(context, order_ids=[1], run_id=None)

        self.assertEqual(get_mock.call_count, 3, "must attempt exactly 3 times, no more, no fewer")

    def test_non_200_status_is_also_retried(self):
        """Mirrors fetch_all_pages: a bad HTTP status (not just a raised exception)
        is converted to a retryable error inside the closure, same as before."""
        item = {"id": 2, "order": "/resources/Order/2/", "product": "/resources/Product/2/"}
        get_mock = MagicMock(side_effect=[
            FakeResponse(503, "Service Unavailable"),
            _ok_response([item]),
        ])
        context = MagicMock()
        context.request.get = get_mock

        items = P.fetch_items_for_orders(context, order_ids=[2], run_id=None)

        self.assertEqual(get_mock.call_count, 2)
        self.assertEqual(items[0]["id"], 2)


if __name__ == "__main__":
    unittest.main()
