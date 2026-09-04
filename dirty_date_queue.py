"""
dirty_date_queue.py
=====================
Task 16 prerequisite — race-safe feature_recompute_queue enqueue primitive.
See migrations/16_feature_recompute_queue.sql for the schema and
reprocess_dirty_features_v2.py for the draining worker.

Race safety: a dirty event arriving while (establishment_id, business_date)
is already status='processing' must never be lost. enqueue_dirty_date()
ALWAYS advances dirty_at, even mid-processing; it only resets status back to
'pending' when the row wasn't already 'processing' (leaving an in-flight
claim's status alone, since the worker owns that transition). The worker
compares dirty_at to processing_started_at at completion time to decide
done vs. re-pending — see reprocess_dirty_features_v2.py's process_one().

Only called with target="shadow_v2" writes (sync_orders/
sync_order_items_and_modifiers) — there is no point queuing a recompute for
a date whose source data only changed in production, since
aggregate_features_v2.py never reads production orders/order_items.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

_REVEL_TZ = ZoneInfo("America/Chicago")


def business_date(created_date_dt: datetime):
    """Same America/Chicago conversion aggregate_features_v2.py uses."""
    if created_date_dt is None:
        return None
    return created_date_dt.astimezone(_REVEL_TZ).date()


def enqueue_dirty_date(cur, establishment_id: int, business_date_val, source_run_id: str = None) -> None:
    """Idempotent, dedup by (establishment_id, business_date). Safe to call
    once per changed row -- ON CONFLICT collapses repeats into one logical
    queue item, always advancing dirty_at."""
    if business_date_val is None or establishment_id is None:
        return
    cur.execute("""
        INSERT INTO feature_recompute_queue (establishment_id, business_date, dirty_at, status, source_run_id)
        VALUES (%s, %s, NOW(), 'pending', %s)
        ON CONFLICT (establishment_id, business_date) DO UPDATE SET
            dirty_at = NOW(),
            source_run_id = EXCLUDED.source_run_id,
            status = CASE WHEN feature_recompute_queue.status = 'processing'
                          THEN feature_recompute_queue.status
                          ELSE 'pending' END
    """, (establishment_id, business_date_val, source_run_id))


def enqueue_dirty_dates(cur, establishment_id: int, business_dates: set, source_run_id: str = None) -> None:
    """Bulk convenience wrapper — one enqueue_dirty_date() call per distinct
    date. Callers collect the set of business dates touched by a batch of
    UPSERTed rows and call this once per sync step."""
    for d in business_dates:
        enqueue_dirty_date(cur, establishment_id, d, source_run_id)
