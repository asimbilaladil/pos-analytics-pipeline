# Field Mapping — Nederland (est 26) June 2026 Export

Every field Masroor explicitly requested is mapped below to its exact Revel source (or marked
NOT AVAILABLE / NOT CONFIRMED where Revel does not provide it — nothing here is guessed). An
appendix at the bottom lists **every** raw field observed on Order, OrderItem, and Payment for
this dataset, with an explicit Included/Not-included note for each, so nothing is silently
dropped without a record of the decision.

## Requested fields

| Business field requested | Revel resource | Exact Revel field/path | Direct or derived | Transformation | Notes |
|---|---|---|---|---|---|
| Order ID | Order | id | direct | none | Primary Revel Order identifier. Column: order_id. |
| Store ID | Order | establishment (URI) / query param establishment=26 | direct | ID extracted from establishment URI / confirmed via establishments table | Column: establishment_id. Value is always 26 for this export. |
| Store name | Establishment (reference table) | establishments.name | reference lookup | joined by establishment_id=26 against read-only Postgres establishments table | Column: establishment_name = "LCF Nederland". Not present on the raw Order payload itself. |
| Order created timestamp | Order | created_date | direct + derived | kept exactly as returned in created_date_revel_raw; created_at_chicago = same value with America/Chicago tzinfo attached (naive Revel string already represents local time, not UTC — see README item 10) | Columns: created_date_revel_raw (raw) and created_at_chicago (normalized). Raw value never overwritten. |
| Order closed timestamp | Order | NOT AVAILABLE | NOT AVAILABLE / NOT CONFIRMED | none | Order does not expose a closed datetime field in this Revel configuration (confirmed against the full 117-field raw Order schema for this establishment — no field resembling a closed timestamp exists). A boolean `closed` flag is present and is included as-is (column: closed). No closed_date_revel_raw/closed_at_chicago columns were added, to avoid an always-null column that would look like "checked, found nothing" vs. a genuine absent field. |
| Order / channel / dining option | Order | dining_option | direct + derived name | dining_option is a raw integer code (0-106 range); dining_option_name is looked up from a project-maintained code->name table (not a Revel-provided field) | Columns: dining_option_code (raw int) and dining_option_name (project reference mapping — flag if a code appears that is not in the mapping; none observed in this dataset). |
| Subtotal | Order | subtotal | direct | parsed via json.loads(parse_float=Decimal), quantized to 6 decimals, no rounding | Column: subtotal. |
| Discount amount | Order | discount_amount / discount_total_amount | direct | both raw discount fields kept side by side; no single Revel field is authoritative for ""the"" discount amount | Columns: discount_amount, discount_total_amount (both raw, Decimal-safe). |
| Comp amount | Order | NOT AVAILABLE | NOT AVAILABLE / NOT CONFIRMED | none | Revel does not expose a distinct comp/complimentary-amount field on Order for this establishment. Discretionary comps appear to be recorded through discount_total_amount/discount_amount with a free-text discount_reason (raw string, column: discount_reason) — but there is no structured flag separating a comp from an ordinary discount, so no comp_amount column was derived. Classifying discount_reason text into comp-vs-discount would be a guess and was intentionally not done. |
| Tax | Order | tax | direct | parsed via json.loads(parse_float=Decimal), quantized to 6 decimals | Column: tax. |
| Total | Order | final_total | direct | parsed via json.loads(parse_float=Decimal), quantized to 6 decimals | Column: final_total. This is Revel's own settled/total field name. |
| Void yes/no | Order | NOT AVAILABLE (order-level) | derived proxy from OrderItem.is_voided | has_voided_items = true if ANY OrderItem belonging to this order has is_voided=true in File B; false otherwise | Column: has_voided_items (derived, clearly not a Revel field). Order itself has no void flag — only `deleted` (raw boolean, also included as column `deleted`). In this specific June 2026 Nederland dataset, 0 of 28,767 order items had is_voided=true, so has_voided_items is false for all 6,741 orders — a genuine finding, not a filtering artifact (verified directly against the raw archive). |
| Refund yes/no | Payment | refunded | derived to order-level flag | any_payment_refunded = true if ANY Payment record attached to this order has refunded=true; false if the order has payments and none are refunded; false if the order has zero payments | Column: any_payment_refunded. In this dataset, 0 of 4,659 archived Payment records had refunded=true, so this column is false for every order (verified directly against the raw archive — not a script defect). |
| Payment type | Payment | payment_type | derived (order-level aggregation — see PAYMENTS section of README) | payment_count = number of Payment records attached to the order; payment_type_single = that single payment's raw payment_type code IF payment_count==1, else null; payment_types_json = JSON list of every payment_type code on the order (any count); payment_records_json = full lossless JSON list of every attached Payment record's key fields (id, payment_type, amount, tip, gratuity, refunded, transaction_status, ...) | Columns: payment_count, payment_type_single, payment_types_json, payment_records_json. payment_type is a raw numeric code (e.g. 200) — Revel does not return a human-readable payment method name on Payment, and no code->name mapping table exists anywhere in this project's codebase, so payment_type NAME is NOT AVAILABLE / NOT CONFIRMED; only the raw numeric code is provided. 4,570 of 6,741 orders have >=1 payment; 71 orders have >1 payment (split tender: 59 with 2, 11 with 3, 1 with 4; max observed = 4); 2,171 orders have 0 attached payment records (mostly is_unpaid=true orders). |
| Employee ID | Order | created_by (URI) | derived (ID extracted from URI) | extract_id() parses the trailing numeric segment of the /enterprise/User/{id}/ URI | Columns: created_by_user_id (primary candidate — the user who created/placed the order) and updated_by_user_id (the user who last updated it) are both included since Revel has no field literally named employee_id and either could be the intended "employee" depending on use case. created_by_user_id is the closer analogue to "who rang this order up". |
| Guest count if available | Order | number_of_people | direct | none | Column: number_of_people. Present on every order (often 0 for to-go/pickup orders, which is expected, not missing data). |
| Order ID (File B) | OrderItem | order (URI) | derived (ID extracted from URI) | extract_id() parses the trailing numeric segment of the /resources/Order/{id}/ URI | Column: order_id. |
| OrderItem ID | OrderItem | id | direct | none | Column: order_item_id. |
| Product ID | OrderItem | product (URI) | derived (ID extracted from URI) | extract_id() parses the trailing numeric segment of the /resources/Product/{id}/ URI | Column: product_id. |
| Product name | OrderItem + Product (reference) | product_name_override (OrderItem) / products.name (V2 reference) | direct (primary) + reference lookup (secondary) | product_name_revel_raw = OrderItem.product_name_override, taken exactly as Revel returned it (100% populated in this dataset's sample — never null in Nederland June data). product_name_v2_reference = products.name joined by product_id, kept as a SEPARATE column for cross-checking only — never used to overwrite the raw value. | Columns: product_name_revel_raw (authoritative/raw) and product_name_v2_reference (reference/QA only). Per instructions, the raw value is never silently replaced by the V2 name. |
| Category ID | Product (raw archive reference — not present on raw OrderItem) | raw Product record field `category` (URI, e.g. /products/ProductCategory/2733/), from archived Product pages | reference lookup (raw-archive based, not DB) | OrderItem carries no category field at all. The Postgres `products.category_id` column is NULL for all 35,453 products account-wide (an ingestion gap, verified directly — not specific to this export), so this was NOT used. Instead, product_id was joined against a product_id->category_id map built directly from the raw archived Product pages (`/var/lib/laynes/raw_revel/products/2026/08/run_20260814T090001Z/`, verified complete: 35,453/35,453 records), which do carry a `category` URI on every record. | Column: category_id_v2_reference. Resolvable for all 161 distinct product_ids appearing in Nederland's June 2026 order items (161/161). This is an account-wide, PRESENT-DAY (archived 2026-08-14) product/category snapshot, not a June-2026-dated one — Revel has no historical category-assignment tracking, so today's categorization is used as the best available proxy for what a product's category was in June. If a product's category was reassigned between June and 2026-08-14, this column reflects the current assignment, not the June-2026 one. |
| Category name | ProductCategory (raw archive reference) | raw ProductCategory record field `name`, from archived ProductCategory pages (active + inactive) | reference lookup (raw-archive based, not DB) | category_id (above) joined against an id->name map built from the raw archived ProductCategory pages (`/var/lib/laynes/raw_revel/product_categories/2026/08/run_20260814T090001Z_active/` + `..._inactive/`, verified complete: 2,597 + 761 = 3,358 records, matching the DB product_categories row count exactly). ProductCategory records nest child categories under a `subcategories` field; the map was built by flattening that nesting so subcategory ids/names resolve correctly too, not just top-level categories. | Column: category_name_v2_reference. Same present-day-snapshot caveat as Category ID above. |
| Quantity | OrderItem | quantity | direct | parsed via json.loads(parse_float=Decimal), quantized to 3 decimals (supports fractional/weighted items), no rounding | Column: quantity. |
| Unit price | OrderItem | price | direct | parsed via json.loads(parse_float=Decimal), quantized to 6 decimals | Column: unit_price (source field name: price). |
| Line total | OrderItem | pure_sales | direct | parsed via json.loads(parse_float=Decimal), quantized to 6 decimals | Column: line_total (source field name: pure_sales — Revel's own extended-line-amount field; price * quantity does not always equal pure_sales exactly once modifiers/discounts are involved, so pure_sales was used as the more accurate "line total"). |
| Modifier / add-on indicator | OrderItem.modifieritems (nested) | modifieritems (array embedded on OrderItem) | direct (structural) | Modifiers are NOT separate OrderItem rows in Revel's payload — they are nested inside each OrderItem's modifieritems array. File B therefore contains ONLY main OrderItem rows (is_modifier_row is always false in File B, kept only as an explicit non-guessing marker). modifier_row_count on each File B row states how many nested modifier rows that item has. The nested modifier rows themselves are broken out separately into nederland_june_2026_modifiers.parquet (one row per modifier, referencing order_item_id/order_id) — this does not replace File B. | Columns: modifier_row_count (File B), plus the entire separate modifiers file. See README "MODIFIERS" section for full structural explanation. |
---

## Appendix: full raw field inventory

### Full raw Order field inventory (117 fields observed)

| Raw field | Included in export |
|---|---|
| applied_discounts | Yes |
| applied_service_fee | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| applied_taxes | Yes |
| asap | Yes |
| auto_grat_pct | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| bill_number | Yes |
| bill_parent | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| billing_address | Yes |
| billing_zip_code | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| bills_info | Yes |
| bills_type | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| call_name | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| call_number | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| check_sum | Yes |
| closed | Yes |
| created_at | No (this is a PosStation URI reference, NOT a timestamp, despite the name — a Revel naming quirk. Do not confuse with created_date.) |
| created_by | Yes |
| created_date | Yes |
| crv_taxed | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| crv_value | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| customer | Yes |
| customer_address_distance | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| customer_birthdate | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| deleted | Yes |
| deleted_discounts | Yes |
| delivery_address | Yes |
| delivery_clock_in | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| delivery_clock_out | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| delivery_distance | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| delivery_duration | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| delivery_employee | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| delivery_estimated_distance | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| device_id | Yes |
| dining_option | Yes |
| discount | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| discount_amount | Yes |
| discount_code | Yes |
| discount_nontaxable_surcharge_included | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| discount_reason | Yes |
| discount_rule_amount | Yes |
| discount_rule_type | Yes |
| discount_tax_amount | Yes |
| discount_tax_amount_included | Yes |
| discount_taxed | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| discount_total_amount | Yes |
| discounted_by | Yes |
| drive_through_data | Yes |
| establishment | Yes |
| exchange_discount | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| exchanged | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| external_sync | Yes |
| final_total | Yes |
| fleet_service_data | Yes |
| gift_reward_data | Yes |
| gratuity | Yes |
| gratuity_type | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| ha_applied | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| has_delivery_info | Yes |
| has_history | Yes |
| has_items | Yes |
| id | Yes |
| invoice_date | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| is_discounted | Yes |
| is_invoice | Yes |
| is_readonly | Yes |
| is_unpaid | Yes |
| kitchen_status | Yes |
| last_updated_at | No (this is a PosStation URI reference, NOT a timestamp, despite the name — same Revel naming quirk as created_at.) |
| local_id | Yes |
| loyalty_account_id | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| notes | Yes |
| notification_email_sent | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| notification_text_sent | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| number_of_people | Yes |
| orderhistory | Yes |
| package | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| pickup_data | Yes |
| pickup_time | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| points_added | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| points_redeemed | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| pos_mode | Yes |
| prevailing_surcharge | Yes |
| prevailing_tax | Yes |
| printed | Yes |
| registry_data | Yes |
| remaining_due | Yes |
| reporting_id | Yes |
| resource_uri | No (Revel self-link, e.g. /resources/Order/{id}/ — redundant with order_id + known endpoint pattern.) |
| rounding_delta | Yes |
| running_tax_number | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| sent | Yes |
| service_charge | Yes |
| service_fee_tax | Yes |
| service_fee_taxed | Yes |
| service_fee_untaxed | Yes |
| smart_order | Yes |
| smartpay_gratuity | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| smartpay_tip | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| subtotal | Yes |
| surcharge | Yes |
| surcharge_excluded | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| table | Yes |
| table_owner | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| tax | Yes |
| tax_country | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| tax_excluded_amount | Yes |
| tax_rebate | Yes |
| tax_rounding_model | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| taxable_surcharge | Yes |
| taxable_surcharge_excluded | Yes |
| updated_by | Yes |
| updated_date | Yes |
| uuid | Yes |
| vehicle | Yes |
| version | Yes |
| virtual_data | Yes |
| web_order | Yes |

### Full raw OrderItem field inventory (115 fields observed)

| Raw field | Included in export |
|---|---|
| applied_discounts | Yes |
| applied_service_fee | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| applied_taxes | Yes |
| appointment | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| appointment_ref_uuid | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| bill_parent | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| catering_complete | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| catering_delivery_date | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| combo_fraction_part | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| combo_product_set | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| combo_saving_amount | Yes |
| combo_type | Yes |
| combo_used | Yes |
| combo_uuid | Yes |
| commission_amount | Yes |
| commissions | Yes |
| cost | Yes |
| course_number | Yes |
| created_by | Yes |
| created_date | Yes |
| crv_value | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| cup_qty | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| cup_weight | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| date_paid | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| deleted | Yes |
| deleted_date | Yes |
| dining_option | Yes |
| discount | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| discount_amount | Yes |
| discount_code | Yes |
| discount_reason | Yes |
| discount_rule_amount | Yes |
| discount_rule_type | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| discount_tax_amount_included | Yes |
| discount_taxed | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| discounted_by | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| dynamic_combo | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| dynamic_combo_slot | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| ervc_type | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| event_date | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| exchange_discount | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| exchanged | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| exclude_from_discounts | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| expedited | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| external_shipping_address | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| gift_card_number | Yes |
| id | Yes |
| ingredientitems | Yes |
| initial_price | Yes |
| invoice_document_uuid | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| is_cold | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| is_discounted | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| is_layaway | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| is_store_credit | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| item_type | Yes |
| kitchen_completed | Yes |
| manual_unit_price_adjustment | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| modifier_amount | Yes |
| modifier_cost | Yes |
| modifieritems | Yes |
| not_returnable | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| on_hold | Yes |
| on_layaway | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| order | Yes |
| order_local_id | Yes |
| package | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| package_uuid | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| parent_combo_uuid | Yes |
| parent_uuid | Yes |
| price | Yes |
| price_to_display | Yes |
| printed | Yes |
| product | Yes |
| product_name_override | Yes |
| pump_date | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| pump_number | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| pure_sales | Yes |
| quantity | Yes |
| reference_discount | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| reference_discounts | Yes |
| resource_uri | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| returned_establishment | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| sales_tax_exemption_reason | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| scanned_barcode | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| seat_number | Yes |
| sent | Yes |
| serial_number | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| service_fee_tax | Yes |
| service_fee_taxed | Yes |
| service_fee_untaxed | Yes |
| service_provider | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| shared | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| sold_by_weight | Yes |
| special_request | Yes |
| split_parts | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| split_type | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| split_with_seat | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| start_time | Yes |
| station | Yes |
| tax_amount | Yes |
| tax_included | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| tax_rate | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| tax_rebate | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| taxed_flag | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| temp_sort | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| uom | Yes |
| updated_by | Yes |
| updated_date | Yes |
| uuid | Yes |
| void_ref_uuid | Yes |
| voided_by | Yes |
| voided_date | Yes |
| voided_reason | Yes |
| weight | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| wholesale_saving_amount | Yes |

### Full raw Payment (embedded in payment_records_json on each order) field inventory (51 fields observed)

| Raw field | Included in export |
|---|---|
| amount | Yes |
| amount_authorized | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| bill | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| card_surcharge | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| card_type | Yes |
| cash_drawer | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| cc_first_name | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| cc_last_name | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| change | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| created_by | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| created_date | Yes |
| currency_amount | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| currency_tip | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| currency_type | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| deleted | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| establishment | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| exchanged | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| executed | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| first_4_cc_digits | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| gratuity | Yes |
| house_account | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| id | Yes |
| invoice_transition_date | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| last_4_cc_digits | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| online | Yes |
| order | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| other_payment_type | Yes |
| payer_id | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| payment_date | Yes |
| payment_token | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| payment_type | Yes |
| processor_accepted | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| processor_response | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| rate | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| receipt_email | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| refund_transaction_id | Yes |
| refunded | Yes |
| resource_uri | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| rounding_delta | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| signature_img_url | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| source_type | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| station | Yes |
| till_owner | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| tip | Yes |
| transaction_captured | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| transaction_data | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| transaction_id | Yes |
| transaction_status | Yes |
| updated_by | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| updated_date | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |
| uuid | No (not analytically relevant for this export — low-usage/always-null-for-this-establishment or purely administrative field) |

### Category reference note

Category ID/name are not raw OrderItem or raw Order fields at all — they come from a
separate reference resolution against archived Product/ProductCategory pages. See the
"Category ID" / "Category name" rows in the requested-fields table above for the exact
archive paths and the present-day-snapshot caveat.