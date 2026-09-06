-- Migration 38: A3 classification for five products first sold 2026-09-05/06.
--
-- ADDITIVE. Migration 26 is not modified. These five product_ids had no row in
-- product_analysis_classification at all (they are INSERTs, not UPDATEs --
-- migration 26 updated rows that already existed), and no other product is
-- touched: every statement targets an explicit id, never a name pattern or a
-- category, so no other newly unresolved product is swept in.
--
-- These products first sold in September 2026, so no historical A3 figure
-- moves. The Nederland June 2026 golden numbers (4,565 REAL orders, 6,323
-- entrées, 1.3851 entrées/check, 64.10% exactly-one-entrée) are unaffected by
-- construction and are re-verified after this migration.
--
-- NOTHING HERE CHANGES A GLOBAL RULE. No entrée rule is derived from product
-- names, from the Merch category or from the Service Fee category. Each row
-- below is a decision about one product id.

BEGIN;

-- ── A. Three first-row combo entrée markers ────────────────────────────────
-- Evidence is POS STRUCTURE, not the product name. Each of these heads its own
-- multi-item combo group in 100% of its appearances, followed by the sides and
-- drink of that combo -- the same structural slot occupied by ** 3 Finger
-- Regular ** (9071) and ** 5 Finger Spicy ** (21541) in the very same orders:
--
--   45271  order 17050523  combo 0627b6bf…  -> Ranch, Fries, Regular Dr Pepper
--   45293  orders 17043924 / 17071365       -> Laynes Sauce, Fries, drink  (3 groups)
--   45297  order 17067411                   -> Laynes Sauce, Fries, Large Lemonade
--
-- first-row combo count / later-component count / standalone count:
--   45271 = 1 / 0 / 0      45293 = 3 / 0 / 0      45297 = 1 / 0 / 0
--
-- These ids are part of one contiguous rollout block
-- (45271, 45272, 45289, 45293, 45297, 45305, 45311) -- the same menu item
-- deployed per store. The four already carrying rows were seeded by the same
-- verified_structure rule at 13:22 on 2026-09-05; these three simply sold
-- later that day and missed that pass. They are recorded with the identical
-- source, form and confidence as their structural siblings.
INSERT INTO product_analysis_classification
    (product_id, product_name_snapshot, is_entree, product_form,
     classification_source, confidence, effective_from, effective_to,
     reviewed_at, reviewed_by, review_note)
VALUES
    (45271, '** 1 Regular and 1 Spicy **', TRUE, 'combo_component',
     'verified_structure', 'high', DATE '2026-01-01', NULL, now(), NULL,
     'Verified first-row combo structure: heads a multi-item combo group '
     'followed by that combo''s sides and drink, in 1 of 1 appearances. '
     'Product name was not used as evidence.'),
    (45293, '** 1 Regular and 1 Spicy **', TRUE, 'combo_component',
     'verified_structure', 'high', DATE '2026-01-01', NULL, now(), NULL,
     'Verified first-row combo structure: heads a multi-item combo group '
     'followed by that combo''s sides and drink, in 3 of 3 appearances. '
     'Product name was not used as evidence.'),
    (45297, '** 1 Regular and 1 Spicy **', TRUE, 'combo_component',
     'verified_structure', 'high', DATE '2026-01-01', NULL, now(), NULL,
     'Verified first-row combo structure: heads a multi-item combo group '
     'followed by that combo''s sides and drink, in 1 of 1 appearances. '
     'Product name was not used as evidence.');

-- ── B. Two human-reviewed non-entrées ──────────────────────────────────────
-- Same human-review convention as migration 26: classification_source =
-- 'verified_product_list', reviewed_by = 'Asim'. These are recorded as
-- explicit human decisions precisely because the automated evidence was NOT
-- sufficient -- neither product has a reviewed equivalent, and the pre-existing
-- same-name rows for Cotten T Shirt are classification_source 'unresolved'
-- whose is_entree = FALSE is the unresolved default, not a decision. That
-- default was NOT treated as a prior classification.
INSERT INTO product_analysis_classification
    (product_id, product_name_snapshot, is_entree, product_form,
     classification_source, confidence, effective_from, effective_to,
     reviewed_at, reviewed_by, review_note)
VALUES
    (32840, 'Cotten T Shirt', FALSE, 'non_entree',
     'verified_product_list', 'high', DATE '2026-01-01', NULL, now(), 'Asim',
     'Human reviewed as non-entrée. Revel category Merch was historically '
     'valid for the sale and Merch has no observed combo participation. '
     'Decision is maintained explicitly rather than inferred from name.'),
    (32809, 'Delivery Service Fee - Uber Eats', FALSE, 'non_entree',
     'verified_product_list', 'high', DATE '2026-01-01', NULL, now(), 'Asim',
     'Human reviewed as non-entrée. Historically valid Revel category is '
     'Service Fee. Service fees are not food entrées. Earlier automated '
     'signals (kitchen_seconds, tax, standalone position) were explicitly '
     'rejected and are not used as evidence.');

COMMIT;

-- Rollback:
--   DELETE FROM product_analysis_classification
--    WHERE product_id IN (45271, 45293, 45297, 32840, 32809);
