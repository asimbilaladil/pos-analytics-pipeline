# Revel Internal Data Completion & Backfill Plan

**Project:** Revel POS Analytics Pipeline  
**Objective:** Complete and harden the internal PostgreSQL dataset first, before any external reporting/export work.  
**Current ingestion coverage:** Daily ingestion from **2026-02-10 onward** across **12 locations**.  
**Business timezone:** **Central Time · Houston, TX** (`America/Chicago`).

---

## 1. Goal

Make our internal system the durable source of truth for Revel data.

The system should:

1. Capture the missing Revel resources and fields.
2. Store them correctly in PostgreSQL.
3. Backfill all currently covered history from **2026-02-10 to present**.
4. Fix the daily sync so later updates, refunds, voids, comps, payments, and other changes are not missed.
5. Preserve raw API responses so future fields can be recovered without re-pulling Revel.
6. Validate the completed database against live Revel.
7. Later extend the same architecture to historical data before **2026-02-10**.

---

# 2. Current Database

Current tables:

### Reference / dimension

- `establishments`
- `dining_channels`
- `products`
- `modifiers`

### Transactional

- `orders`
- `order_items`
- `modifier_items`

### Derived

- `features_daily_summary`
- `features_hourly`
- `features_product_daily`
- `location_health_scores`

### Operational

- `ingestion_log`
- `weather_daily`

Current approximate scale:

- `orders`: ~1.0M+
- `order_items`: ~3.8M+
- `modifier_items`: ~896K+
- `products`: ~35K
- `modifiers`: ~54K

`orders` and `order_items` are monthly partitioned by `created_date`.

---

# 3. Target Architecture

```text
                         REVEL API
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
        Order            OrderItem           Payment
          │                  │                  │
    OrderHistory        ModifierItem            │
          │                  │                  │
        Product ───── ProductCategory            │
          └──────────────────┼──────────────────┘
                             │
                             ▼
                   Raw API Archive (.json.gz)
                             │
                             ▼
                         Parser
                             │
                             ▼
                        PostgreSQL
                             │
             ┌───────────────┼───────────────┐
             │               │               │
          Features        Dashboard       Exports
```

Important rule:

> Every Revel API page should be archived before parsing and writing to PostgreSQL.

---

# 4. Core Implementation Rules

## 4.1 Preserve raw values

Store raw Revel fields where possible.

Examples:

- `ervc_type`
- `payment_type`
- `item_type`
- `source_type`
- `card_type`

Do not replace the raw integer with a label during ingestion.

Mapping should happen later in:

- queries
- views
- dashboards
- exports

---

## 4.2 Preserve monetary precision

Do not round raw Revel values.

Use:

```sql
NUMERIC
```

or:

```sql
NUMERIC(18,6)
```

for new monetary fields.

Avoid:

```sql
NUMERIC(10,2)
```

for raw API values.

---

## 4.3 Avoid unnecessary PII

Do not ingest unnecessary customer/cardholder fields such as:

- cardholder first name
- cardholder last name
- signature image
- receipt email
- payer ID
- first card digits

Only store fields with a clear operational need.

---

## 4.4 Backfills must be resumable

Every backfill must survive:

- process restart
- API quota
- HTTP errors
- DB failures
- network failure

Never design a backfill that must restart from zero.

---

# 5. Task Dependency Order

```text
TASK 01  Capture baseline
   ↓
TASK 02  Verify live Revel behavior
   ↓
TASK 03  Create new database tables
   ↓
TASK 04  Add missing fields to existing tables
   ↓
TASK 05  Fix UPSERT behavior
   ↓
TASK 06  Add raw API archive
   ↓
TASK 07  Improve daily incremental sync
   ↓
TASK 08  Backfill Product Categories
   ↓
TASK 09  Backfill Orders
   ↓
TASK 10  Backfill Order Items
   ↓
TASK 11  Backfill Payments
   ↓
TASK 12  Backfill Order History
   ↓
TASK 13  Validate / refresh Modifier Items
   ↓
TASK 14  Recompute affected feature tables
   ↓
TASK 15  Full reconciliation and validation
   ↓
TASK 16  Production cutover
   ↓
TASK 17  Historical backfill before 2026-02-10
```

---

# TASK 01 — Capture Current Baseline

## Goal

Create a snapshot before any schema or pipeline changes.

Run:

```sql
SELECT
    establishment_id,
    MIN(created_date) AS first_order,
    MAX(created_date) AS last_order,
    COUNT(*) AS order_count
FROM orders
GROUP BY establishment_id
ORDER BY establishment_id;
```

Capture row counts for:

```sql
SELECT COUNT(*) FROM orders;
SELECT COUNT(*) FROM order_items;
SELECT COUNT(*) FROM modifier_items;
SELECT COUNT(*) FROM products;
SELECT COUNT(*) FROM modifiers;
SELECT COUNT(*) FROM features_daily_summary;
SELECT COUNT(*) FROM features_hourly;
SELECT COUNT(*) FROM features_product_daily;
```

Also capture per location:

- first order date
- last order date
- order count
- order-item count
- modifier count

Inspect:

- `pipeline.py`
- current API date filtering
- timezone conversion
- `order_items.is_voided` derivation
- current `ON CONFLICT` behavior
- pagination
- retry logic
- partition handling

### Done when

- current counts are saved
- no schema changes have been made
- current ingestion behavior is documented

---

# TASK 02 — Verify Live Revel Behavior

Do this before migration or backfill.

## 2.1 Timezone

Take a known late-evening transaction, ideally:

```text
22:00–23:59 America/Chicago
```

Compare:

1. Revel POS UI
2. Revel API timestamp
3. PostgreSQL timestamp
4. Streamlit/dashboard timestamp

The same transaction must stay on the same local business date.

---

## 2.2 Verify OrderItem batch lookup

Test whether the live account supports querying multiple order IDs in one request for `OrderItem`.

Use around 10 orders first.

Do not assume the final batch size yet.

---

## 2.3 Verify OrderHistory behavior

For several orders inspect:

- `opened`
- `closed`
- `order_opened_by`
- `order_closed_by`
- `order_opened_at`
- `order_closed_at`

Determine:

- whether an order can have multiple history rows
- how the correct/latest close event should be selected

---

## 2.4 Verify Payment

Fetch several payments and confirm:

- `order`
- `establishment`
- `amount`
- `payment_type`
- `payment_date`
- `created_date`
- `updated_date`
- `refunded`
- `refund_transaction_id`
- `transaction_status`
- `card_type`
- `online`
- `deleted`
- `executed`

Also test a known refund if possible.

---

## 2.5 Verify comp / void / return examples

Find known examples and capture:

- `ervc_type`
- `initial_price`
- `price`
- `pure_sales`
- `quantity`
- `modifier_amount`
- `discount_amount`
- `discount_reason`
- `voided_date`

Do not derive comp amounts until verified against real examples.

---

## 2.6 Verify current `is_voided` logic

Inspect exactly how:

```text
order_items.is_voided
```

is currently derived.

Document the condition.

---

# TASK 03 — Create New Database Tables

## 3.1 Product Categories

```sql
CREATE TABLE product_categories (
    id              INTEGER PRIMARY KEY,
    name            VARCHAR(255),
    parent_id       INTEGER REFERENCES product_categories(id),
    active          BOOLEAN,
    sorting         INTEGER,
    description     TEXT,
    created_date    TIMESTAMPTZ,
    updated_date    TIMESTAMPTZ,
    ingested_at     TIMESTAMPTZ DEFAULT NOW()
);
```

Indexes:

```sql
CREATE INDEX ix_product_categories_parent
    ON product_categories(parent_id);

CREATE INDEX ix_product_categories_active
    ON product_categories(active);
```

---

## 3.2 Payments

```sql
CREATE TABLE payments (
    id                     BIGINT PRIMARY KEY,
    uuid                   UUID,

    order_id               BIGINT NOT NULL,
    establishment_id       INTEGER REFERENCES establishments(id),

    payment_type           SMALLINT,
    other_payment_type     VARCHAR(255),

    amount                 NUMERIC,
    amount_authorized      NUMERIC,
    tip                    NUMERIC,
    gratuity               NUMERIC,
    change                 NUMERIC,

    refunded               BOOLEAN,
    refund_transaction_id  VARCHAR(255),

    transaction_id         VARCHAR(255),
    transaction_status     VARCHAR(100),
    transaction_captured   BOOLEAN,
    processor_accepted     BOOLEAN,

    card_type              SMALLINT,
    online                 BOOLEAN,
    source_type            SMALLINT,

    executed               BOOLEAN,
    deleted                BOOLEAN,
    exchanged              BOOLEAN,

    station_id             INTEGER,

    payment_date           TIMESTAMPTZ,
    created_date           TIMESTAMPTZ,
    updated_date           TIMESTAMPTZ,

    created_by_user_id     INTEGER,
    updated_by_user_id     INTEGER,

    ingested_at            TIMESTAMPTZ DEFAULT NOW(),
    ingestion_date         DATE
);
```

Indexes:

```sql
CREATE INDEX ix_payments_order
    ON payments(order_id);

CREATE INDEX ix_payments_est_date
    ON payments(establishment_id, payment_date);

CREATE INDEX ix_payments_updated
    ON payments(updated_date);

CREATE INDEX ix_payments_refunded
    ON payments(refunded)
    WHERE refunded = TRUE;
```

---

## 3.3 Order History

```sql
CREATE TABLE order_history (
    id                     BIGINT PRIMARY KEY,
    uuid                   UUID,

    order_id               BIGINT NOT NULL,

    opened_at              TIMESTAMPTZ,
    closed_at              TIMESTAMPTZ,

    opened_by_user_id      INTEGER,
    closed_by_user_id      INTEGER,

    opened_at_station_id   INTEGER,
    closed_at_station_id   INTEGER,

    ingested_at            TIMESTAMPTZ DEFAULT NOW()
);
```

Index:

```sql
CREATE INDEX ix_order_history_order
    ON order_history(order_id);
```

---

# TASK 04 — Add Missing Fields to Existing Tables

## 4.1 Orders

Add:

```sql
ALTER TABLE orders
    ADD COLUMN created_by_user_id INTEGER,
    ADD COLUMN updated_by_user_id INTEGER,
    ADD COLUMN discount_amount NUMERIC,
    ADD COLUMN discount_reason TEXT,
    ADD COLUMN discounted_by_user_id INTEGER,
    ADD COLUMN exchanged BOOLEAN,
    ADD COLUMN service_charge NUMERIC,
    ADD COLUMN surcharge NUMERIC,
    ADD COLUMN remaining_due NUMERIC,
    ADD COLUMN notes TEXT;
```

---

## 4.2 Order Items

Add:

```sql
ALTER TABLE order_items
    ADD COLUMN ervc_type SMALLINT,
    ADD COLUMN item_type SMALLINT,
    ADD COLUMN initial_price NUMERIC,
    ADD COLUMN discount_amount NUMERIC,
    ADD COLUMN discount_reason TEXT,
    ADD COLUMN discounted_by_user_id INTEGER,
    ADD COLUMN cost NUMERIC,
    ADD COLUMN exchanged BOOLEAN,
    ADD COLUMN void_ref_uuid UUID;
```

`ervc_type` is the highest-priority field.

---

## 4.3 Products

Add:

```sql
ALTER TABLE products
    ADD COLUMN category_id INTEGER
        REFERENCES product_categories(id);
```

Keep:

```text
product_class
```

and:

```text
category
```

as separate concepts.

---

# TASK 05 — Fix UPSERT Behavior

## Problem

Current pattern:

```sql
ON CONFLICT DO NOTHING
```

silently discards newer versions of existing Revel rows.

That means later:

- refunds
- closed status
- corrected totals
- updated discounts
- updated items

may be fetched but never persisted.

---

## Required change

Implement proper `DO UPDATE` logic per table.

Example:

```sql
INSERT INTO orders (...)
VALUES (...)
ON CONFLICT (id, created_date)
DO UPDATE SET
    updated_date              = EXCLUDED.updated_date,
    pickup_time               = EXCLUDED.pickup_time,
    final_total               = EXCLUDED.final_total,
    subtotal                  = EXCLUDED.subtotal,
    tax                       = EXCLUDED.tax,
    gratuity                  = EXCLUDED.gratuity,
    discount_total_amount     = EXCLUDED.discount_total_amount,
    closed                    = EXCLUDED.closed,
    is_unpaid                 = EXCLUDED.is_unpaid,
    deleted                   = EXCLUDED.deleted,
    is_discounted             = EXCLUDED.is_discounted,
    created_by_user_id        = EXCLUDED.created_by_user_id,
    updated_by_user_id        = EXCLUDED.updated_by_user_id,
    discount_amount           = EXCLUDED.discount_amount,
    discount_reason           = EXCLUDED.discount_reason,
    discounted_by_user_id     = EXCLUDED.discounted_by_user_id,
    exchanged                 = EXCLUDED.exchanged,
    service_charge            = EXCLUDED.service_charge,
    surcharge                 = EXCLUDED.surcharge,
    remaining_due             = EXCLUDED.remaining_due,
    notes                     = EXCLUDED.notes,
    ingested_at               = NOW();
```

Implement equivalent logic for:

- `order_items`
- `payments`
- `products`
- `product_categories`
- `order_history`
- `modifier_items` where appropriate

---

## Improve logging

Track:

```text
rows_fetched
rows_inserted
rows_updated
rows_skipped
rows_failed
```

---

# TASK 06 — Add Raw API Archive

## Goal

Preserve the original Revel payload before parsing.

Use:

```text
.json.gz
```

Example:

```text
raw_revel/
├── orders/
│   └── 2026/
│       └── 06/
│           └── establishment_26/
│               ├── page_000001.json.gz
│               └── page_000002.json.gz
├── order_items/
├── payments/
├── order_history/
├── products/
├── product_categories/
└── modifier_items/
```

Required flow:

```text
API
 ↓
raw .json.gz
 ↓
parser
 ↓
Postgres
```

Archive enough metadata to recover:

- resource
- establishment
- query date/window
- query parameters
- page/offset
- fetch timestamp
- pipeline version

---

# TASK 07 — Improve Daily Incremental Sync

## Problem

Created-date-only sync misses later changes.

Example:

```text
June 10
Order created

June 12
Refund processed
```

The June 10 record must be revisited.

---

## New strategy

Use an update lookback window.

Recommended starting point:

```text
last successful run - 48 hours
```

through:

```text
current run time
```

---

## Orders

Fetch using:

- `establishment`
- `updated_date`

UPSERT.

Collect changed order IDs.

---

## Payments

Fetch using:

- `establishment`
- `updated_date`

UPSERT.

---

## Order Items

For changed orders:

- fetch related OrderItems
- archive response
- UPSERT

---

## Order History

For changed orders:

- fetch histories
- archive response
- UPSERT

---

## Products / Product Categories

Use either:

- nightly updated-date sync
- or nightly full refresh if simpler and safe

---

## Modifier Items

Confirm whether modifier records can change after initial creation.

If yes, include them in changed-order refreshes.

---

# TASK 08 — Backfill Product Categories

Load the ProductCategory data first.

Then refresh Product records so:

```text
products.category_id
```

is populated.

Validation:

```sql
SELECT
    COUNT(*) AS total_products,
    COUNT(category_id) AS categorized_products,
    COUNT(*) FILTER (WHERE category_id IS NULL) AS uncategorized_products
FROM products;
```

Inspect category hierarchy:

```text
Product
  → Category
      → Parent Category
```

---

# TASK 09 — Backfill Orders

## Target window

Initial internal backfill:

```text
2026-02-10 → current date
```

Initial fixed run:

```text
2026-02-10 → 2026-08-10
```

---

## Recommended unit

Start with:

```text
1 establishment × 1 day
```

Increase only after testing.

---

## Backfill purpose

Refresh existing mutable fields and populate newly added fields.

Populate:

- `created_by_user_id`
- `updated_by_user_id`
- `discount_amount`
- `discount_reason`
- `discounted_by_user_id`
- `exchanged`
- `service_charge`
- `surcharge`
- `remaining_due`
- `notes`

Also refresh existing values such as:

- totals
- discounts
- tax
- gratuity
- closed
- unpaid
- deleted
- updated date

---

## Backfill log

Record every unit:

```text
resource
establishment_id
start_date
end_date
pages_fetched
rows_fetched
rows_inserted
rows_updated
rows_failed
started_at
completed_at
status
error_message
script_version
```

---

# TASK 10 — Backfill Order Items

## Target

Order items belonging to orders from:

```text
2026-02-10 → current date
```

---

## Fields to populate

- `ervc_type`
- `item_type`
- `initial_price`
- `discount_amount`
- `discount_reason`
- `discounted_by_user_id`
- `cost`
- `exchanged`
- `void_ref_uuid`

Also refresh existing mutable values.

---

## Strategy

Use order IDs already stored in Postgres.

Batch using the tested API behavior.

Example only:

```text
100–200 order IDs/request
```

Do not assume the final batch size until verified.

---

## Validation

```sql
SELECT
    COUNT(*) AS total,
    COUNT(ervc_type) AS with_ervc_type,
    COUNT(initial_price) AS with_initial_price
FROM order_items;
```

Distribution:

```sql
SELECT ervc_type, COUNT(*)
FROM order_items
GROUP BY ervc_type
ORDER BY ervc_type;
```

Manually inspect:

- normal items
- voids
- returns
- comps

---

# TASK 11 — Backfill Payments

## Target

```text
2026-02-10 → current date
all 12 locations
```

Use location/date/update filters where supported.

Archive every response.

UPSERT all Payment rows.

Validation:

```sql
SELECT
    establishment_id,
    COUNT(*) AS payment_count,
    COUNT(*) FILTER (WHERE refunded = TRUE) AS refunded_count
FROM payments
GROUP BY establishment_id
ORDER BY establishment_id;
```

Also:

```sql
SELECT COUNT(DISTINCT order_id)
FROM payments;
```

And:

```sql
SELECT payment_type, COUNT(*)
FROM payments
GROUP BY payment_type
ORDER BY payment_type;
```

Inspect split-tender orders manually.

---

# TASK 12 — Backfill Order History

## Target

All stored orders from:

```text
2026-02-10 → current date
```

Use stored order IDs.

Process in resumable batches.

Do not assume one history record per order.

---

## Progress state

Create a generic backfill state mechanism.

Example:

```sql
CREATE TABLE backfill_runs (
    id                  BIGSERIAL PRIMARY KEY,
    backfill_name       VARCHAR(100) NOT NULL,
    resource_name       VARCHAR(100) NOT NULL,

    establishment_id    INTEGER,
    start_date          DATE,
    end_date            DATE,

    current_date        DATE,
    current_offset      BIGINT,
    last_order_id       BIGINT,

    rows_fetched        BIGINT DEFAULT 0,
    rows_inserted       BIGINT DEFAULT 0,
    rows_updated        BIGINT DEFAULT 0,
    rows_failed         BIGINT DEFAULT 0,

    status              VARCHAR(30),
    error_message       TEXT,

    started_at          TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,

    script_version      VARCHAR(100)
);
```

Statuses:

```text
pending
running
paused
failed
completed
```

---

## Validation

```sql
SELECT COUNT(DISTINCT order_id)
FROM order_history;
```

Compare with:

```sql
SELECT COUNT(*)
FROM orders
WHERE closed = TRUE;
```

Differences must be reviewed, not automatically treated as errors.

---

# TASK 13 — Validate / Refresh Modifier Items

Current relationship:

```text
Order
  └── OrderItem
        └── ModifierItem
```

Validate:

- `order_item_id`
- `order_id`
- modifier ID
- modifier name
- quantity
- modifier price
- discount state

Check orphan records:

```sql
SELECT COUNT(*)
FROM modifier_items mi
LEFT JOIN order_items oi
    ON oi.id = mi.order_item_id
WHERE oi.id IS NULL;
```

Investigate any non-zero result.

If live testing proves modifier rows can change after order creation, add modifier refresh to the daily changed-order sync.

---

# TASK 14 — Review Derived Feature Tables

Existing feature tables:

- `features_daily_summary`
- `features_hourly`
- `features_product_daily`

Check whether the backfilled fields can affect feature calculations.

Potentially affected values:

- void status
- discount amounts
- totals
- closed status
- product/category information
- kitchen timing
- channel information

If affected:

```text
recompute 2026-02-10 → current date
```

for all locations.

If not affected:

do not rebuild unnecessarily.

---

# TASK 15 — Full Reconciliation & Validation

## 15.1 Count checks

For several locations and dates compare:

```text
Revel API
vs
PostgreSQL
```

Check:

- Orders
- OrderItems
- ModifierItems
- Payments
- Refunds
- OrderHistory

---

## 15.2 Known regression checkpoint

Preserve:

```text
Nederland
2026-06-15

Revel Order total_count = 204
Postgres order count     = 204
```

Use this as a permanent test.

---

## 15.3 Record-level validation

Select at least 10 individual orders across multiple locations.

Compare:

- order ID
- created date
- updated date
- subtotal
- tax
- discount
- total
- closed
- employee/user IDs
- payment records
- refund status
- item prices
- quantity
- `ervc_type`
- modifiers
- category
- OrderHistory open/close time

---

## 15.4 Timezone regression tests

Include late evening transactions:

```text
22:00–23:59 America/Chicago
```

Ensure the local business date stays correct.

---

# TASK 16 — Production Cutover

After the backfill passes validation:

1. Enable update-aware daily ingestion.
2. Keep rollback capability.
3. Compare old/new behavior briefly if practical.
4. Switch production to the new logic.
5. Monitor closely.

Track:

- API calls
- 429 responses
- runtime
- rows fetched
- inserted
- updated
- failed
- missing locations
- missing dates
- raw archive failures
- DB failures

---

# TASK 17 — Historical Backfill Before 2026-02-10

This is Phase 2.

Do not mix it with the current backfill.

---

## 17.1 Determine historical start date

For each establishment define:

```text
historical start
→
2026-02-09
```

---

## 17.2 Create historical partitions first

Before inserting older records, create all missing monthly partitions for:

- `orders`
- `order_items`

Example:

```text
orders_2024_01
orders_2024_02
...
order_items_2024_01
order_items_2024_02
...
```

---

## 17.3 Estimate workload

Before starting:

- estimate order volume
- estimate item volume
- estimate payment volume
- estimate OrderHistory work
- confirm Revel API quota
- estimate expected calendar runtime

---

## 17.4 Use the same architecture

Do not build a separate throwaway historical importer.

Use:

```text
Revel
  ↓
raw JSON.gz
  ↓
parser
  ↓
UPSERT
  ↓
validation
```

---

# 6. Recommended Backfill Resource Order

```text
1. ProductCategory
2. Product
3. Order
4. OrderItem
5. ModifierItem validation/refresh
6. Payment
7. OrderHistory
8. Feature recomputation
9. Final reconciliation
```

Reason:

- metadata first
- Orders establish the main order ID universe
- OrderItems and histories can then be fetched against known orders
- Payments link back to orders
- feature rebuild should happen after raw data is complete

---

# 7. API Rate-Limit Handling

Handle:

```text
HTTP 429
```

safely.

Required behavior:

- persist progress
- stop safely
- do not lose current position
- do not restart from zero
- resume later
- log last successful batch/page

Track:

```text
API calls
429 count
last successful page
current establishment
current date
current resource
```

---

# 8. Error Handling

A single failed record or page must not destroy the whole run.

For every failure:

1. Keep the raw archive.
2. Log the error.
3. Continue if safe.
4. Save retry information.
5. Never silently discard data.

---

# 9. Completion Checklist

The internal Revel data project is complete when:

- [ ] Baseline captured.
- [ ] Timezone verified.
- [ ] OrderItem batch behavior verified.
- [ ] OrderHistory behavior verified.
- [ ] Payment behavior verified.
- [ ] Comp / void / return behavior verified.
- [ ] `product_categories` exists.
- [ ] `payments` exists.
- [ ] `order_history` exists.
- [ ] Missing `orders` fields exist.
- [ ] Missing `order_items` fields exist.
- [ ] `products.category_id` exists.
- [ ] Monetary precision is preserved.
- [ ] `ON CONFLICT DO NOTHING` no longer discards updates.
- [ ] Raw API responses are archived.
- [ ] Daily ingestion uses update-aware logic.
- [ ] Product categories are backfilled.
- [ ] Orders are backfilled from 2026-02-10 onward.
- [ ] OrderItems are backfilled from 2026-02-10 onward.
- [ ] Payments are backfilled from 2026-02-10 onward.
- [ ] OrderHistory is backfilled from 2026-02-10 onward.
- [ ] Modifier relationships are validated.
- [ ] Affected feature tables are recomputed.
- [ ] Sample counts match Revel.
- [ ] Individual records match Revel.
- [ ] Late-night timezone tests pass.
- [ ] Backfills resume safely after interruption.
- [ ] Production daily runs correctly update old records.
- [ ] Existing dashboards continue to work.

---

# 10. Recommended Commit Structure

Keep implementation in small, reviewable commits:

```text
01-baseline-and-live-api-verification
02-schema-product-categories
03-schema-payments
04-schema-order-history
05-add-order-fields
06-add-order-item-fields
07-upsert-refactor
08-raw-response-archive
09-updated-date-daily-sync
10-product-category-backfill
11-orders-backfill
12-order-items-backfill
13-payments-backfill
14-order-history-backfill
15-modifier-validation
16-feature-rebuild
17-validation-and-production-cutover
18-historical-partitions-and-backfill
```

Avoid one giant migration or one giant code change.

---

# 11. Immediate Next Step

Start only with:

```text
TASK 01 — Capture Current Baseline
TASK 02 — Verify Live Revel Behavior
```

Do not start the schema migration or the backfill until those checks are complete.

Highest-risk correctness items:

1. timezone handling
2. existing `is_voided` derivation
3. OrderItem batching behavior
4. OrderHistory cardinality/filtering
5. current `ON CONFLICT` behavior

Once those are confirmed, proceed to schema changes and controlled backfill.
