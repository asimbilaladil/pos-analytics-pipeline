-- Migration 26: apply the human-approved entrée classification (A3)
--
-- Decisions supplied by Asim after reviewing the canonical candidate report.
-- These are RECORDED decisions, not inferences: classification_source is
-- 'verified_product_list' and reviewed_by names the person accountable for
-- them. Nothing here is derived from a product name at query time -- the
-- canonical-name mapping is used ONLY to find the per-store product_ids that
-- belong to each reviewed product, exactly as it was used in the review report.
--
-- Deliberately left unresolved, on instruction:
--   Family Pack, 10/25/50 Finger Party Pack, Party Pack 10/50/100,
--   Chicken Finger A la Carte
-- These have no agreed entrée-equivalent count. Guessing 1 would understate a
-- 50-finger pack and overstate a single a-la-carte finger, so they stay
-- is_entree = FALSE with confidence 'unknown' and continue to suppress entrée
-- coverage until a count is decided. That is the intended behaviour.

BEGIN;

ALTER TABLE product_analysis_classification
    ADD COLUMN IF NOT EXISTS reviewed_by TEXT;

COMMENT ON COLUMN product_analysis_classification.reviewed_by IS
    'Person accountable for a manual/verified decision. NULL for rows seeded '
    'from structural evidence alone.';

-- Canonical name mapping: identical normalisation to the review report --
-- strip asterisks, collapse whitespace, trim, lowercase. Used to locate
-- product_ids, never to decide classification.
CREATE OR REPLACE FUNCTION pg_temp.canon(txt TEXT) RETURNS TEXT AS $$
    SELECT lower(btrim(regexp_replace(regexp_replace($1, '\*+', '', 'g'),
                                      '\s+', ' ', 'g')));
$$ LANGUAGE sql IMMUTABLE;

-- ── A. single-line combo, 1 entrée ─────────────────────────────────────────
UPDATE product_analysis_classification c
SET is_entree = TRUE,
    product_form = 'single_line_combo',
    classification_source = 'verified_product_list',
    confidence = 'high',
    reviewed_by = 'Asim',
    reviewed_at = now(),
    review_note = 'Human-approved: one row that IS the whole combo (entrée plus '
                  'sides priced as one line). Counts as exactly 1 entrée.'
WHERE pg_temp.canon(c.product_name_snapshot) IN (
    '5 finger meal', '4 finger meal', '3 finger meal', 'kids finger meal',
    '$12 3 finger meal', 'chicken wrap combo', 'club sandwich meal', 'sandwich meal');

-- ── B. standalone entrée, 1 entrée ─────────────────────────────────────────
UPDATE product_analysis_classification c
SET is_entree = TRUE,
    product_form = 'standalone',
    classification_source = 'verified_product_list',
    confidence = 'high',
    reviewed_by = 'Asim',
    reviewed_at = now(),
    review_note = 'Human-approved: entrée sold on its own, no combo structure. '
                  'Counts as exactly 1 entrée.'
WHERE pg_temp.canon(c.product_name_snapshot) IN (
    'chicken wrap', 'chicken wraps', 'club sandwich', 'sandwich');

-- ── D. non-entrée ──────────────────────────────────────────────────────────
UPDATE product_analysis_classification c
SET is_entree = FALSE,
    product_form = 'non_entree',
    classification_source = 'verified_product_list',
    confidence = 'high',
    reviewed_by = 'Asim',
    reviewed_at = now(),
    review_note = 'Human-approved: side, sauce, dessert or drink. Never an entrée, '
                  'including when sold on its own.'
WHERE pg_temp.canon(c.product_name_snapshot) IN (
    'fries', 'crinkle-cut fries', 'toast', 'texas toast', 'cookie',
    'chocolate chunk cookie', 'oreo shake', 'laynes sauce', 'jalapeno ranch',
    'ranch', 'gravy', 'potato salad');

-- ── E. explicitly held at unknown ──────────────────────────────────────────
-- Recorded so the reason is visible in the table rather than only in this file.
UPDATE product_analysis_classification c
SET review_note = 'Reviewed and deliberately left unresolved: no agreed '
                  'entrée-equivalent count. A 50-finger party pack is not 1 entrée '
                  'and an individual a-la-carte finger is not 1 entrée, so any '
                  'assumed value would corrupt entrées-per-check. Revisit when a '
                  'count (or an exclusion rule) is agreed.',
    reviewed_by = 'Asim',
    reviewed_at = now()
WHERE pg_temp.canon(c.product_name_snapshot) IN (
    'family pack', '10 finger party pack', '25 finger party pack',
    '50 finger party pack', 'party pack 10', 'party pack 50', 'party pack 100',
    'chicken finger a la carte')
  AND c.confidence = 'unknown';

COMMIT;

-- Rollback: re-run migration 24 seeds after
--   DELETE FROM product_analysis_classification
--   WHERE classification_source = 'verified_product_list';
