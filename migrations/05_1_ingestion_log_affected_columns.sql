-- Task 05.1 — fix ingestion-log semantics
--
-- ingestion_log.orders_inserted/items_inserted were, since before Task 05,
-- always populated with len(rows) (rows attempted/fetched for upsert) — even
-- the legacy ingest_to_db.py's own comment admits "rowcount is -1 after
-- execute_values... return len(rows) as an upper bound". Task 05's UPSERT
-- refactor did not change that number (DO UPDATE with no DO NOTHING branch
-- means every row is "affected", so affected == fetched, same value as
-- always) — but continuing to write that value under the name "_inserted"
-- perpetuates a name that never matched what was actually computed.
--
-- Add honestly-named columns for what pipeline.py can actually report under
-- DO UPDATE semantics. orders_inserted/items_inserted are left as columns
-- (existing historical rows keep their values/meaning unchanged) but
-- pipeline.py stops writing to them going forward — see pipeline.py.

ALTER TABLE ingestion_log
    ADD COLUMN orders_affected INTEGER,
    ADD COLUMN items_affected  INTEGER;
