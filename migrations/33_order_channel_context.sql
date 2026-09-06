-- Migration 33: channel context with verified and unverified layers kept apart (A8)
--
-- NO AUTHORITATIVE CHANNEL NAME SOURCE EXISTS. All four evidence routes checked:
--   1. Revel reference resource -- DiningOption, DiningOptions, OrderType,
--      ServiceType, Channel, OrderSource, DeliveryService, ThirdPartyService
--      and /enterprise/DiningOption/ all return 404.
--   2. Explicit URI on Order -- there is none. Order.dining_option is a bare
--      integer, and the Order schema endpoint declares it "Integer data. Ex:
--      2673" with no choices, exactly like payment_type in A10.
--   3. A maintained account mapping -- what exists is a PROJECT convention
--      (aggregate_features_v2.py maps 4 -> drive_through, 1 -> eat_in,
--      0 -> to_go, 100 -> DoorDash, 101 -> UberEats, 5/8 -> online), and the
--      earlier export documented its own names as coming from "a
--      project-maintained code->name table (not a Revel-provided field)".
--      It is also demonstrably not derived from this account's data: it defines
--      105 and 106 as drive-thru lanes A and B, and NEITHER CODE HAS EVER
--      OCCURRED -- the only codes present are 0,1,2,3,4,5,6,8,100,101.
--   4. Otherwise unknown -- which is where this lands for the NAMES.
--
-- WHAT *IS* VERIFIABLE: an ORDERING-PATTERN split, corroborated by two
-- independent fields that nobody hand-mapped. Measured over June 2026:
--     codes 4, 1        0.0% web_order, 0.0% online payments   avg ticket  $8-10
--     code  0           1.4% web_order, 1.5% online payments   avg ticket  $16.52
--     codes 100,101,8  99.4-100% web_order AND 99.4-100% online payments,
--                       avg ticket $25.81-$30.45
-- orders_v2.web_order and payments_v2.online agreeing to within a rounding
-- error, across 150k orders, is evidence -- not a naming guess.
--
-- BUT IT PROVES ONLY WHAT IT PROVES. This evidence shows that certain codes are
-- associated with web ordering and online payment. It does NOT establish a
-- physical service mode. "web_associated" is a statement about how the order
-- reached the POS; "off-premise", "delivery", "drive-thru", "dine-in" and
-- "takeout" are claims about where the guest was and how they were served, and
-- nothing here evidences those. An earlier draft of this view called the groups
-- on_premise / off_premise_digital, which quietly smuggled that claim back in
-- after the whole point was that it is unverified. The groups are therefore
-- named for the evidence -- web_associated / non_web_associated -- and the raw
-- code is always carried so nothing is lost.
--
-- The view reads orders_v2 only: no payment or item join, so it stays cheap and
-- is deliberately NOT folded into v_orders_classified.

BEGIN;

CREATE OR REPLACE VIEW v_order_channel_context AS
SELECT
    o.id                                   AS order_id,
    o.establishment_id,
    (o.created_date AT TIME ZONE 'America/Chicago')::date AS business_date,
    o.dining_option                        AS channel_code,
    'orders_v2.dining_option'::text        AS channel_source_field,
    -- VERIFIED layer: corroborated by web_order and payment.online. Named for
    -- the evidence (ordering pattern), NOT for a service mode.
    CASE
        WHEN o.dining_option IN (100, 101, 8, 5) THEN 'web_associated'
        WHEN o.dining_option IN (0, 1, 2, 3, 4, 6) THEN 'non_web_associated'
        ELSE 'unknown'
    END                                    AS channel_group,
    CASE
        WHEN o.dining_option IN (100, 101, 8, 5, 0, 1, 2, 3, 4, 6)
        THEN 'verified_structural' ELSE 'unknown'
    END                                    AS channel_group_confidence,
    -- UNVERIFIED layer: the project convention, carried so figures reconcile
    -- with features_*_v2 and the earlier export, and labelled as unverified so
    -- it is never mistaken for a Revel fact.
    CASE o.dining_option
        WHEN 4   THEN 'drive_through'  WHEN 1 THEN 'eat_in'
        WHEN 0   THEN 'to_go'          WHEN 100 THEN 'doordash'
        WHEN 101 THEN 'ubereats'       WHEN 8 THEN 'online'
        WHEN 5   THEN 'online'
        ELSE NULL
    END                                    AS channel_name_project_convention,
    'project_convention_unverified'::text  AS channel_name_confidence,
    o.web_order,
    -- METADATA inconsistency: the code's usual web association and the
    -- web_order flag disagree on this order. A SIGNAL about record-keeping, not
    -- a finding about how food was served, and not an accusation. 0.162% of
    -- REAL orders network-wide in June 2026.
    ((o.dining_option IN (100, 101, 8, 5) AND o.web_order IS NOT TRUE)
     OR (o.dining_option IN (0, 1, 2, 3, 4, 6) AND o.web_order IS TRUE))
                                           AS possible_code_source_mismatch
FROM orders_v2 o;

COMMENT ON VIEW v_order_channel_context IS
    'Channel context. channel_group (web_associated / non_web_associated) is '
    'VERIFIED structurally -- web_order and payments.online independently agree '
    '-- but it describes ORDERING PATTERN only. It is NOT a service mode: it '
    'does not establish drive-thru, dine-in, takeout or delivery. '
    'channel_name_project_convention is NOT verified either: no Revel source '
    'names these codes, and the convention defines codes 105/106 that have never '
    'occurred. Always report the raw channel_code. '
    'possible_code_source_mismatch is a metadata inconsistency, never a '
    'confirmed mis-ring.';

COMMIT;

GRANT SELECT ON v_order_channel_context TO laynes_ro;

-- Rollback:
--   DROP VIEW IF EXISTS v_order_channel_context;
