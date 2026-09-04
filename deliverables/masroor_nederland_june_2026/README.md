# Nederland — June 2026 Raw Data Export

Export/investigation package only. No production database table, sync_state, cron, `.env`, or
dashboard was modified to produce this. No new Revel API calls were made — everything below is
built from preserved raw archives already on disk plus read-only reference lookups against
Postgres (`establishments`, `products`, `product_categories`).

## 1. Nederland — exact Revel establishment ID and name

**Establishment ID 26, name "LCF Nederland"** — confirmed fresh against the `establishments`
table (`select id, name from establishments where name ilike '%nederland%'`), not assumed from
memory. This is the only establishment in scope for this export.

## 2. Period

**2026-06-01 00:00:00 America/Chicago (inclusive) through 2026-07-01 00:00:00 America/Chicago
(exclusive)** — i.e. all of local June 2026 for the Nederland store. See item 10 for the
timezone finding and item 2's boundary proof below.

**Boundary proof (not assumed):** `backfill_progress` shows the three consecutive monthly
Order-fetch windows for establishment 26 as UTC instants:

| Window | window_start (UTC) | window_end (UTC) |
|---|---|---|
| May 2026 | 2026-05-01 05:00 | 2026-06-01 05:00 |
| **June 2026** | **2026-06-01 05:00** | **2026-07-01 05:00** |
| July 2026 | 2026-07-01 05:00 | 2026-08-01 05:00 |

These are exactly contiguous (May's end == June's start == July's start's predecessor) with no
gap and no overlap, and 05:00 UTC is exactly midnight America/Chicago in June/July (CDT, UTC-5).
This export additionally re-applies its own independent America/Chicago filter to every raw
order (rather than trusting the archive's own fetch boundary) — 0 of 6,741 archived orders were
dropped by that independent filter, confirming the archive is already exactly and only the
June-Chicago window.

## 3. Source resources used

All three from preserved raw archives (no live API calls):

| Resource | Archive run_id | Location |
|---|---|---|
| Order | `20260811T062636Z` | `/var/lib/laynes/raw_revel/orders/2026/08/establishment_26/run_20260811T062636Z/` |
| OrderItem | `20260811T195437Z` | `/var/lib/laynes/raw_revel/order_items/2026/08/establishment_26/run_20260811T195437Z/window_20260601_20260701/` |
| Payment | `20260812T153723Z` | `/var/lib/laynes/raw_revel/payments/2026/08/establishment_26/run_20260812T153723Z/window_20260601_20260701/` |
| Product (reference, category only) | `20260814T090001Z` | `/var/lib/laynes/raw_revel/products/2026/08/run_20260814T090001Z/` |
| ProductCategory (reference, category names) | `20260814T090001Z_active` + `..._inactive` | `/var/lib/laynes/raw_revel/product_categories/2026/08/` |

Each Order/OrderItem/Payment run_id was located via `backfill_progress` as the canonical,
verified-complete archive for this establishment/window/resource combination — not an arbitrary
or most-recent archive folder. The Product/ProductCategory archives are account-wide reference
data (not establishment- or June-scoped); the most recent complete archive was used and its page
counts verified against each archive's own `total_count` before use — see item 15 for the
present-day-snapshot caveat this implies for Category. `establishments` and `products` were also
read read-only (`conn.set_session(readonly=True)`) for establishment/product name resolution —
see field_mapping.md.

## 4. Order row count

**6,741 raw Order rows**, all written to File A. Matches Revel's own `total_count: 6741` for
this window exactly (proven both in the archive's own `.meta.json` sidecar and independently by
summing all 7 archived pages).

## 5. OrderItem row count

**28,767 raw OrderItem rows**, all written to File B. (Note: the archive's own per-batch
`meta.total_count` is not a single usable total for OrderItem — it was fetched in
order_id-batches of 200 via `order__in`, so each page's `total_count` reflects only that batch,
not the whole month. 28,767 is the actual summed record count across all 37 archived pages, and
100% of it falls inside the June-Chicago order set — see item 14.)

## 6. Payment row count involved

**4,659 raw Payment records** in the archive (Payment is windowed by `Payment.created_date`, not
by the parent Order's date — see item 6a). Of those, **4,654** attach to a June-2026 Nederland
order; **5** do not (item 6a). **4,570** of the 6,741 June orders have at least one attached
payment; **2,171** have zero (see item 9). Split-tender distribution among orders that do have
payments:

| Payments per order | Order count |
|---|---|
| 1 | 4,499 |
| 2 | 59 |
| 3 | 11 |
| 4 | 1 |

**71 orders (1.05%) are genuine split-tender** (>1 payment). No collapsing/discarding was done —
File A carries `payment_count`, `payment_type_single` (only when count==1),
`payment_types_json`, and a fully lossless `payment_records_json` per order. See
field_mapping.md's "Payment type" row for the exact column semantics.

**6a. The 5 orphan payments**, all genuine, none a script defect (verified against raw values
directly): 4 are payments posted in the first ~4 minutes of 2026-06-01 for orders created in the
last minutes of 2026-05-31 (a real midnight-boundary interaction between Order.created_date and
Payment.created_date, not an error) — those orders correctly belong to May's File A, not this
one. The 5th is a single **-$28.53 refund transaction** posted 2026-06-08 against a much older
order from an earlier period, unrelated to the boundary. These 5 payment records are not in
either exported file's aggregation (their order is out of scope) but are fully visible in the
raw Payment archive if needed.

## 7. Modifier handling

Modifiers are **not a separate top-level Revel resource** and are **not separate OrderItem
rows**. They live nested inside each OrderItem's own `modifieritems` array in the raw payload —
confirmed directly against the raw archive (e.g. one sampled OrderItem, id 47462380, "4 Finger
Meal*", carries 5 nested `modifieritems` entries for its included sides/drink choices, each with
its own `modifier` product reference, `modifier_price`, `qty`, and `uuid`).

Consequently:
- **File B (`nederland_order_items_2026-06.parquet`) contains only true OrderItem rows** —
  6,741 orders' worth of line items, 28,767 rows — each carrying a `modifier_row_count` column
  stating how many nested modifiers that item has. `is_modifier_row` is always `false` in File B
  (kept as an explicit non-guessing marker, not omitted).
- **A separate `nederland_june_2026_modifiers.parquet` (5,688 rows)** unpacks every nested
  `modifieritems` entry into its own row, referencing `order_item_id`/`order_id`, for anyone who
  wants modifier-level detail. **This does not replace File B.**

## 8. Duplicate findings

**Zero duplicates found**, checked directly against the raw archive (not assumed):
- Order: 6,741 raw rows, 6,741 distinct `id` values — 0 duplicates.
- OrderItem: 28,767 raw rows, 28,767 distinct `id` values — 0 duplicates.

Had duplicates existed, the export script (`build_export.py`) was written to keep every raw row
and flag them via a `duplicate_order_id_flag` column rather than resolving/collapsing them — that
logic path exists but was not exercised because this dataset happens to be clean.

## 9. Void / refund / $0 counts (QA only — nothing filtered out)

| Metric | Count | Note |
|---|---|---|
| Orders with `deleted=true` | 0 | |
| Orders with `is_unpaid=true` | 2,007 | kept in File A, e.g. order 14673193 ($0, no items, unpaid) |
| Orders with `final_total = 0.00` | 2,176 | kept in File A |
| Orders with any voided OrderItem (derived `has_voided_items`) | 0 | see below |
| Orders with any refunded Payment (derived `any_payment_refunded`) | 0 | see below |
| OrderItems with `is_voided=true` | 0 | |

The zero counts for voids/refunds were double-checked directly against the raw archive
(`Counter(is_voided)` / `Counter(refunded)` on every raw record) to rule out a script bug — this
Nederland/June-2026 dataset genuinely has no voided items and no refunded payments. `is_unpaid`
varies normally (2,007 of 6,741), confirming boolean fields are being read correctly elsewhere.
No row of any kind (void/refund/$0/deleted) was filtered out of either file.

## 10. Timezone finding

**Revel returns naive datetime strings (no UTC offset, no "Z" suffix) that represent
America/Chicago local time — not UTC.** Confirmed two independent ways, not assumed:

1. **Request-side proof:** the archive's own `.meta.json` sidecar shows the exact query sent to
   Revel: `created_date__gte: "2026-06-01T00:00:00"`, `created_date__lt: "2026-07-01T00:00:00"`
   (naive, no offset). `backfill_progress` records this same window as
   `2026-06-01 05:00 UTC` → `2026-07-01 05:00 UTC` — a consistent 5-hour offset, i.e. CDT
   (UTC-5), applied by whatever system translated the naive request string to a UTC watermark.
2. **Response-side proof:** the first order in the June archive has
   `created_date: "2026-06-01T00:21:19"` — 21 minutes past the requested window start, with
   zero orders before it in the window. A genuinely-UTC value here would put Nederland's day
   start at ~19:00 the prior evening Chicago time, which does not match a QSR's real opening
   pattern; a Chicago-local value matching almost exactly to the requested boundary does.

Both raw timestamps (`created_date_revel_raw`, `updated_date_revel_raw`, per-payment
`payment_date`/`created_date` inside `payment_records_json`) are preserved **exactly as Revel
returned them, unmodified**, alongside a derived `*_at_chicago` column (the same value with
`America/Chicago` tzinfo attached — not converted, just labeled). The raw column is never
overwritten.

## 11. Original API extraction timing (from `backfill_progress`, not this export's replay)

| Resource | Original fetch pages | Original fetch duration | Original fetch rows |
|---|---|---|---|
| Order | 7 | **~31 seconds** (06:26:37.640 → 06:27:08.689 UTC, 2026-08-11) | 6,741 |
| OrderItem | 34 (37 archived incl. retries) | **~169 seconds / 2.8 min** (19:54:37.970 → 19:57:26.508 UTC, 2026-08-11) | 28,767 |
| Payment | 5 | **~30 seconds** (15:37:24.487 → 15:37:54.626 UTC, 2026-08-12) | 4,659 |

These three fetches happened on different dates/times because they were originally pulled as
part of separate backfill/freshness passes (Tasks 09–16), not a single dedicated Nederland pull —
but each duration above is that resource's real, original, live-Revel wall-clock extraction time
for exactly this June-2026/establishment-26 window, read directly from `backfill_progress`'s
`started_at`/`completed_at` columns.

## 12. Current export-generation timing (this script, replaying the archive — NOT an API call)

- **Archive read (decompress + parse all 49 pages across 3 resources): ~1.75 seconds.**
- **Full generation (archive read + join/derive + write all Parquet/CSV.gz/sample files):
  ~11.3 seconds.**

This is a **local replay of already-downloaded data** and must not be confused with the original
API extraction time in item 11 — no network calls were made to produce this package.

## 13. Rate limit — documented vs. observed

**DOCUMENTED LIMIT: NOT AVAILABLE / NOT CONFIRMED.** Nothing in this project's Revel
documentation (`revel_internal_data_backfill_plan.md` §7 "API Rate-Limit Handling") states a
specific numeric limit (e.g. "N requests/minute") — it only specifies how to *react* to an HTTP
429 if one occurs (persist progress, back off, resume, never restart from zero). No other
project document states a published Revel numeric rate-limit figure.

**OBSERVED BEHAVIOR:** from a prior live probe against this same production account (documented
in `10-aug.md`, §Task 1c): 80 sequential requests, zero explicit delay, all HTTP 200, zero 429s,
no `Retry-After`/`X-RateLimit-*` headers observed at any point. No hard ceiling was found within
that safe probe range; natural per-request latency was ~500ms. This export itself made **zero**
new API calls (archive-based), so it adds no new observation to this figure.

## 14. Order-to-OrderItem join integrity

**0 orphan OrderItems — 100% join integrity, verified directly, not assumed.** All 28,767
OrderItems in File B reference an `order_id` present in File A (checked by set-difference between
`{order_id for every OrderItem}` and `{order_id for every Order}`; result was empty). This
matches the expected-zero-orphan outcome for a correctly-scoped single-month, single-store
export; nothing was modified to force this result.

(Payment-to-Order join has 5 orphans by design — see item 6a; this is a Payment-date-window
edge case, not a File A/File B integrity problem.)

## 15. Limitations / fields not available

Everything below is also captured per-field in `field_mapping.md`; summarized here:

- **Order closed timestamp: NOT AVAILABLE.** Only a boolean `closed` flag exists on Order in
  this Revel configuration — no closed-datetime field of any kind.
- **Order-level void flag: NOT AVAILABLE.** Only OrderItem-level `is_voided` exists. A derived
  `has_voided_items` proxy is included in File A, clearly labeled as derived, not a Revel field.
  It is `false` for all 6,741 June orders (item 9).
- **Comp amount: NOT AVAILABLE as a distinct field.** Comps are not separated from ordinary
  discounts anywhere in the raw Order payload; `discount_amount`/`discount_total_amount` plus
  free-text `discount_reason` are the closest available fields, but classifying that text into
  "comp vs. discount" would be a guess, so no `comp_amount` column was derived.
  discount_reason was empty ('') on every sampled order in this dataset — no populated
  comp-indicating text was observed to classify from in the first place.
- **Payment type NAME: NOT AVAILABLE.** Revel returns only a numeric `payment_type` code (e.g.
  200); no code-to-name lookup table exists anywhere in this Revel account's data or in this
  project's codebase. Only the raw numeric code is exported.
- **Employee ID: no field literally named this.** `created_by_user_id` (order creator) and
  `updated_by_user_id` (last updater) are both included as the closest Revel analogues — see
  field_mapping.md.
- **Category ID / Category name: not present on raw OrderItem at all.** These are resolved by
  `product_id` from the raw archived **Product**/**ProductCategory** pages (not the Postgres
  `products.category_id` column — that column is `NULL` for all 35,453 products account-wide, an
  ingestion gap discovered while fixing this field; it was not used). All 161 distinct products
  appearing in Nederland's June 2026 order items resolve to a category (161/161, 28,767/28,767
  rows populated). **Caveat:** the Product/ProductCategory archive used is an account-wide,
  present-day snapshot (archived 2026-08-14) — Revel does not track historical category
  assignment, so this reflects each product's *current* category, not necessarily its category
  exactly as of June 2026. If a product was recategorized between June and 2026-08-14, this
  column shows the newer category. See field_mapping.md for the exact archive paths/record counts.
- **`created_at` / `last_updated_at` on Order are PosStation URI references, not timestamps** —
  a Revel naming quirk worth flagging explicitly so they aren't mistaken for date fields.
- Not every one of the 117 raw Order fields / 115 raw OrderItem fields was carried into the
  export (administrative/always-empty-for-this-establishment fields like `billing_address`,
  `vehicle`, `delivery_clock_in/out` were left out) — the full inventory with an explicit
  included/not-included note for every single field is in `field_mapping.md`'s appendix, so
  nothing was dropped silently.

## 16. Files generated

All under `deliverables/masroor_nederland_june_2026/`:

| File | Rows | Size |
|---|---|---|
| `nederland_orders_2026-06.parquet` | 6,741 | ~1.01 MB |
| `nederland_orders_2026-06.csv.gz` | 6,741 | ~1.11 MB |
| `nederland_order_items_2026-06.parquet` | 28,767 | ~2.84 MB |
| `nederland_order_items_2026-06.csv.gz` | 28,767 | ~3.23 MB |
| `nederland_june_2026_modifiers.parquet` | 5,688 | ~0.18 MB |
| `sample_orders_200.csv` | 200 (deterministic, chronological) | ~0.32 MB |
| `sample_order_items_200.csv` | 200 (deterministic, chronological) | ~0.27 MB |
| `field_mapping.csv` | 26 requested-field rows | — |
| `field_mapping.md` | same + full raw-field-inventory appendix | — |
| `README.md` | this file | — |
| `email_to_masroor.txt` | draft delivery email | — |
| `build_export.py` / `generate_deliverables.py` | scripts that produced this package (archive-based, no live API calls) | — |

Samples are a **deterministic, evenly-spaced chronological subsample** (sorted by
`created_at_chicago`, then ID; every Nth row selected) of the corresponding full file, using the
same columns — reproducible on re-run, not random.
