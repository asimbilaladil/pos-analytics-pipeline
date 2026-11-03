#!/usr/bin/env python3
"""Revel sync must never strand paid orders without their items.

2026-10 incident: sync_orders() committed the orders AND advanced the
orders_v2 watermark before sync_order_items_and_modifiers() ran. The process
was OOM-killed during the items fetch (Shepherd on Oct 25, Cypress on Oct
26), the next run's window started after those orders, and thousands of paid
orders were never given items. With money and no items they were
classified OTHER and dropped out of REAL sales, so the AI Chat reconciliation
gate failed for both stores.

These tests use an in-memory fake with real commit/rollback semantics (writes
are pending until conn.commit()) and pin:
  1. a kill after orders commit but before items leaves the watermark alone
  2. an items exception records failure and leaves the watermark alone
  3. partial items (chunk 2 of 3 fails) keep chunk 1, and a retry completes
  4. retries and duplicates are idempotent (upsert by id)
  5. on success the watermark advances only after items AND history commit
  6. the self-heal finds and repairs orders without items, never writes
     orders/payments, and is a no-op on the second run
  7. the repair CLI's checkpoint advances per chunk, resumes, and stops
     cleanly on low memory
  8. chunked fetches use distinct raw-archive window keys
"""
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)
os.environ.setdefault("ESTABLISHMENTS", "15,54")
import sync_updated as S  # noqa: E402
import repair_missing_items as R  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


class Killed(BaseException):
    """Stands in for SIGKILL from the OOM killer: no except-Exception handler
    runs, nothing after the raise executes."""


# ---------------------------------------------------------------------------
# In-memory fake of the DB + Revel, with transactional semantics
# ---------------------------------------------------------------------------
class Fake:
    def __init__(self, n_orders=7, items_per_order=2):
        self.revel_orders = {1000 + i: {"id": 1000 + i, "created_date": "2026-10-20T12:00:00"}
                             for i in range(n_orders)}
        self.items_per_order = items_per_order
        self.committed = {"orders": {}, "items": {}, "mods": {}, "history": {}, "payments": {},
                          "watermark": {}, "wm_status": {}}
        self.pending = []
        self.calls = []
        self.fail_items_on_call = None     # (n, exc) -> raise on n-th fetch_items call
        self.items_calls = 0
        self.window_keys = []

    # -- conn -------------------------------------------------------------
    @contextmanager
    def cursor(self):
        yield self

    def commit(self):
        for table, key, val in self.pending:
            self.committed[table][key] = val
        self.pending = []
        self.calls.append("commit")

    def rollback(self):
        self.pending = []
        self.calls.append("rollback")

    def close(self):
        pass

    def write(self, table, key, val):
        self.pending.append((table, key, val))

    # -- Revel + upsert fakes ------------------------------------------------
    def fetch_all_pages(self, context, endpoint, params, label="", **kw):
        return list(self.revel_orders.values())

    def fetch_items_for_orders(self, context, order_ids, **kw):
        self.items_calls += 1
        self.window_keys.append(kw.get("window_key"))
        if self.fail_items_on_call and self.items_calls == self.fail_items_on_call[0]:
            raise self.fail_items_on_call[1]
        out = []
        for oid in order_ids:
            for k in range(self.items_per_order):
                out.append({"id": oid * 10 + k, "order": f"/resources/Order/{oid}/",
                            "created_date": "2026-10-20T12:00:00",
                            "modifieritems": [{"id": oid * 100 + k}]})
        return out

    def fetch_history(self, context, order_ids, run_id, est_id, today, window_key=None):
        self.window_keys.append(window_key)
        return [{"id": oid * 7, "order": oid} for oid in order_ids]


def install(f: Fake):
    P = S.P
    P.fetch_all_pages = f.fetch_all_pages
    P.fetch_items_for_orders = f.fetch_items_for_orders
    P.build_order_row = lambda o, est, today: (o["id"],)
    P.build_item_row = lambda item, est, today, created: tuple([item["id"]] + [None] * 14 + [created])
    P.build_modifier_row = lambda mod, item_id, order_id, est, d, cache: (mod["id"],)

    def up_orders(cur, rows, target="production"):
        f.calls.append("upsert_orders")
        for r in rows:
            f.write("orders", r[0], {"final_total": 10})
        return {"affected": len(rows)}

    def up_items(cur, rows, target="production"):
        f.calls.append("upsert_items")
        for r in rows:
            f.write("items", r[0], r[0] // 10)
        return {"affected": len(rows)}

    def up_mods(cur, rows, target="production"):
        for r in rows:
            f.write("mods", r[0], 1)
        return {"affected": len(rows)}

    def up_hist(cur, rows, target="production"):
        f.calls.append("upsert_history")
        for r in rows:
            f.write("history", r[0], 1)
        return {"affected": len(rows)}

    P.upsert_orders, P.upsert_order_items = up_orders, up_items
    P.upsert_modifier_items, P.upsert_order_history = up_mods, up_hist
    P.upsert_payments = lambda *a, **k: (_ for _ in ()).throw(AssertionError("payments written"))
    S.build_order_history_row = lambda h: (h["id"],)
    S.fetch_order_history_for_orders = f.fetch_history
    S._update_item_updated_dates = lambda cur, items, target="production": None
    S.dirty_date_queue.enqueue_dirty_dates = lambda cur, est, dates, source_run_id=None: None
    S.sync_payments = lambda *a, **k: S._empty_stats()

    def get_window(cur, resource, est):
        start = f.committed["watermark"].get((resource, est), datetime(2026, 10, 18, tzinfo=timezone.utc))
        return start, start + timedelta(days=1)

    def rec_success(cur, resource, est, start, end, run_id):
        f.calls.append("watermark_success")
        f.write("watermark", (resource, est), end)
        f.write("wm_status", (resource, est), "success")

    def rec_failure(cur, resource, est, run_id):
        f.calls.append("watermark_failure")
        f.write("wm_status", (resource, est), "failed")

    S.get_sync_window, S.record_sync_success, S.record_sync_failure = get_window, rec_success, rec_failure

    def find_missing(cur, est, since, until, target="shadow_v2", limit=S.SELF_HEAL_MAX_ORDERS):
        have = set(f.committed["items"].values())
        return sorted(o for o in f.committed["orders"] if o not in have)[:limit]

    S.find_orders_missing_items = find_missing


def run_est(f):
    return S.run_establishment_updated(None, f, 54, "run-x", {}, target="shadow_v2")


START = datetime(2026, 10, 18, tzinfo=timezone.utc)
S.ITEM_CHUNK_ORDERS = 3          # 7 orders -> 3 chunks: [3, 3, 1]

# ---------------------------------------------------------------------------
print("1 -- kill after orders commit, before items")
f = Fake(); install(f)
f.fail_items_on_call = (1, Killed())
try:
    run_est(f)
    killed = False
except Killed:
    killed = True
check("process died in the items fetch", killed)
check("orders were committed", len(f.committed["orders"]) == 7)
check("watermark NOT advanced", ("orders_v2", 54) not in f.committed["watermark"],
      str(f.committed["watermark"]))
check("no items stored", not f.committed["items"])
# recovery: next run, same window
f.fail_items_on_call = None
rep = run_est(f)
check("recovery run re-covers the same window and stores every item",
      len(f.committed["items"]) == 14, str(len(f.committed["items"])))
check("watermark advanced after recovery",
      f.committed["watermark"].get(("orders_v2", 54)) == START + timedelta(days=1))
check("history stored for every order", len(f.committed["history"]) == 7)

# ---------------------------------------------------------------------------
print("2 -- items exception")
f = Fake(); install(f)
f.fail_items_on_call = (1, RuntimeError("HTTP 502"))
rep = run_est(f)
check("reported as failed", rep["order_items"]["rows_failed"] == -1)
check("watermark NOT advanced", ("orders_v2", 54) not in f.committed["watermark"])
check("watermark marked failed", f.committed["wm_status"].get(("orders_v2", 54)) == "failed")
check("self-heal in the same run then repaired the items",
      rep["items_self_heal"].get("missing_after") == 0 and len(f.committed["items"]) == 14,
      str(rep["items_self_heal"]))

# ---------------------------------------------------------------------------
print("3/4 -- partial items, retry, duplicates")
f = Fake(); install(f)
f.fail_items_on_call = (2, Killed())            # chunk 1 commits, chunk 2 dies
try:
    run_est(f)
except Killed:
    pass
check("chunk 1 (3 orders) persisted before the kill", len(f.committed["items"]) == 6,
      str(len(f.committed["items"])))
check("watermark NOT advanced after partial items", ("orders_v2", 54) not in f.committed["watermark"])
f.fail_items_on_call = None
f.items_calls = 0
run_est(f)
check("retry completes all items", len(f.committed["items"]) == 14)
before = dict(f.committed["items"])
run_est(f)                                       # duplicate run, same data
check("duplicate run is idempotent (no extra rows, same keys)", f.committed["items"] == before)

# ---------------------------------------------------------------------------
print("5 -- ordering on success")
f = Fake(); install(f)
run_est(f)
c = f.calls
wm = c.index("watermark_success")
check("watermark written after the last item upsert",
      wm > max(i for i, x in enumerate(c) if x == "upsert_items"))
check("watermark written after the history upsert",
      wm > max(i for i, x in enumerate(c) if x == "upsert_history"))
check("watermark committed", ("orders_v2", 54) in f.committed["watermark"])
check("internal _watermark key not leaked into the report",
      "_watermark" not in run_est(f)["orders"])
# default (non-deferred) callers keep the old behaviour
f = Fake(); install(f)
S.sync_orders(None, f, 54, "r", target="shadow_v2")
check("sync_orders() default still advances its own watermark (catch-up scripts)",
      ("orders_v2", 54) in f.committed["watermark"])

# ---------------------------------------------------------------------------
print("6 -- self-heal")
f = Fake(); install(f)
for oid in (2001, 2002, 2003, 2004):
    f.committed["orders"][oid] = {"final_total": 10}
f.calls.clear()
res = S.repair_missing_items(None, f, 54, "r", {}, since=START, until=START + timedelta(days=1))
check("found 4 orders without items", res["missing_before"] == 4)
check("all repaired", res["missing_after"] == 0 and len(f.committed["items"]) == 8)
check("history re-fetched for repaired orders", len(f.committed["history"]) == 4)
check("never upserts orders", "upsert_orders" not in f.calls)
res2 = S.repair_missing_items(None, f, 54, "r", {}, since=START, until=START + timedelta(days=1))
check("second run is a no-op", res2 == {"missing_before": 0, "missing_after": 0})

# ---------------------------------------------------------------------------
print("7 -- repair CLI checkpoint")
chunks = R.plan_chunks([15, 54], datetime(2026, 10, 18).date(), datetime(2026, 10, 20).date())
check("one chunk per store per Chicago day", [k for k, *_ in chunks] ==
      ["15:2026-10-18", "54:2026-10-18", "15:2026-10-19", "54:2026-10-19"])
k, e, s, u = chunks[0]
check("Chicago day boundaries (CDT, UTC-5)",
      s == datetime(2026, 10, 18, 5, tzinfo=timezone.utc) and u == datetime(2026, 10, 19, 5, tzinfo=timezone.utc))
seen = []


def fake_repair(ctx, conn, est, run_id, cache, target, since, until, window_prefix):
    seen.append((est, since))
    if len(seen) == 3:
        raise Killed()
    return {"missing_before": 5, "missing_after": 0}


ck = {"done": {}}
try:
    R.run_chunks(None, None, chunks, {}, "r", ck, None, 0, repair=fake_repair, mem=lambda: 9999)
except Killed:
    pass
check("checkpoint holds exactly the 2 chunks that completed",
      list(ck["done"]) == ["15:2026-10-18", "54:2026-10-18"], str(list(ck["done"])))
seen.clear()
status, _ = R.run_chunks(None, None, chunks, {}, "r", ck, None, 0,
                         repair=lambda *a, **k: {"missing_before": 1, "missing_after": 0}, mem=lambda: 9999)
check("resume processes only the remaining chunks", status == "ok" and len(ck["done"]) == 4)
ck2 = {"done": {}}
status, _ = R.run_chunks(None, None, chunks, {}, "r", ck2, None, 500,
                         repair=fake_repair, mem=lambda: 100)
check("low memory stops before any chunk, checkpoint unchanged", status == "low_memory" and not ck2["done"])
sh = R.plan_chunks([15, 54])
check("self-heal mode: one chunk per store over the lookback", [k for k, *_ in sh] == ["15:selfheal", "54:selfheal"])

# ---------------------------------------------------------------------------
print("8 -- raw archive window keys")
f = Fake(); install(f)
S.sync_order_items_and_modifiers(None, f, 54, "r", list(f.revel_orders), {}, target="shadow_v2")
check("un-prefixed chunks: None, chunk_001, chunk_002", f.window_keys == [None, "chunk_001", "chunk_002"],
      str(f.window_keys))
f.window_keys.clear()
S.sync_order_history(None, f, 54, "r", list(f.revel_orders), target="shadow_v2", window_prefix="selfheal")
check("prefixed history chunks are distinct", len(set(f.window_keys)) == 3 and all(f.window_keys))

src = open("pipeline.py").read()
check("saved Revel session file is chmod 0600", "os.chmod(STATE_FILE, 0o600)" in src)

passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nRevel items self-heal: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n} -- {d}")
sys.exit(1 if failed else 0)
