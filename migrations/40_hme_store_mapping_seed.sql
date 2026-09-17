-- Migration 40: seed hme_store_mapping from the 2026-09-14 tenant-scoped inventory
--
-- 34 HME stores were observed in the correctly tenant-scoped querydata result
-- (the earlier 499-store result came from a lost scope filter and is NOT used).
--
-- 11 mappings were manually approved by Asim on 2026-09-16 on the basis of an
-- exact location-name match after stripping the Revel 'LCF ' prefix, corroborated
-- by city where Revel records one. They are seeded VERIFIED.
--
-- The HME store number is NOT the Revel establishment_id -- the mapping below is
-- explicit precisely because numeric equality is wrong here (HME 14 = Tyler but
-- Revel 14 = LCF Beaumont, etc).
--
-- The remaining 23 HME stores are outside the current 12-store Laynes scope and
-- are retained as OUT_OF_SCOPE so they serve as the tenant allowlist for the
-- extractor's fail-closed guard.
--
-- Revel 48 LCF Downtown Houston is intentionally NOT mapped: no HME candidate
-- exists and no HME key may be invented for it. It surfaces as
-- coverage_status = 'NO_HME_MAPPING' in v_hme_revel_coverage.
--
-- Idempotent: re-running refreshes names/last_seen_at without disturbing
-- verification metadata.

BEGIN;

INSERT INTO hme_store_mapping (
    hme_store_number, hme_store_name, revel_establishment_id, revel_store_name,
    mapping_status, mapping_confidence, mapping_basis, verified_by, verified_at
) VALUES
    ('1', 'Rosenberg', 40, 'LCF Rosenberg', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('3', 'Frisco', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('01', 'San Marcos', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('14', 'Tyler', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('15', 'Nederland', 26, 'LCF Nederland', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('16', 'Pasadena', 20, 'LCF Pasadena', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('19', 'Nacogdoches', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('26', 'Airtex', 32, 'LCF Airtex', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('37', 'Hot Springs', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('002', 'Lewisville', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('004', 'Roanoke', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('108', 'Katy', 6, 'LCF Katy', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('109', 'Ella', 7, 'LCF Ella', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('115', 'Shepherd', 15, 'LCF Shepherd', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('404', 'Lampasas', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('666', 'Allen', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('2711', 'Marble Falls', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('00111', 'Beaumont', 14, 'LCF Beaumont', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('1140043', 'Wellborn', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1140044', 'Walton', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1140045', 'Greens', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1142288', 'Corsicana', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1143588', 'Janesville', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1151958', 'Benton', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1151995', 'Roswell', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1153869', 'BAUM', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1153870', 'Parma', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1156859', 'Warrenton', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1158614', 'Mission Bend', 25, 'LCF Mission Bend', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('1158647', 'McCain', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1159902', 'Appleton', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1163022', 'Missouri City', 36, 'LCF Missouri City', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now()),
    ('1165112', 'Herriman', NULL, NULL, 'OUT_OF_SCOPE', NULL, 'not_in_current_12_store_laynes_scope', NULL, NULL),
    ('1171670', 'Cypress', 54, 'LCF Cypress', 'VERIFIED', 'high', 'manual_confirmation_exact_location_name', 'Asim', now())
ON CONFLICT (hme_store_number) DO UPDATE SET
    hme_store_name = EXCLUDED.hme_store_name,
    last_seen_at   = now();

-- Verification metadata is never overwritten by a re-run: the DO UPDATE above
-- deliberately touches only the observed name and last_seen_at.

COMMIT;
