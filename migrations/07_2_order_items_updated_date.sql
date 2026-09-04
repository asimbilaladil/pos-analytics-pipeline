-- Task 07 — add order_items.updated_date
--
-- Needed for the Strategy A monitoring check requested alongside the
-- strategy decision: detect any OrderItem whose updated_date is later than
-- its parent Order's updated_date (a counterexample to the propagation
-- assumption Strategy A relies on). order_items had no updated_date column
-- at all before this -- the field exists live on Revel's OrderItem
-- resource (confirmed during the Section 5 investigation) but nothing
-- captured it. Nullable, no default -- matches every other Task 04/06/07
-- addition. Only sync_updated.py's opt-in updated-mode path populates it;
-- pipeline.py's default created-mode path is unchanged and leaves it NULL.

ALTER TABLE order_items
    ADD COLUMN updated_date TIMESTAMPTZ;
