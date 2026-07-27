# Daily + Thursday-Weekly Business Summary (Sales · Labor · Product Mix)

## Context

Today the pipeline pulls **orders and order items** from Revel nightly and surfaces them in two
Streamlit apps: `dashboard.py` (kitchen tender prep) and `sales_report.py` (network sales).
What you actually run the business on is broader than that, and three pieces are missing:

1. **Labor doesn't exist anywhere.** No employee, timecard, shift, or wage data is ingested —
   no table, no API call, nothing. Verified against the live Revel API: the data is there and
   reachable (see below), we just never pulled it.
2. **There is no product-mix layer.** `products.product_class` is `NULL` for all 35,381 rows, so
   "shakes", "spicy vs regular", "sides" don't exist as concepts — only raw product names do.
3. **Weeks start on Sunday, not Thursday.** `sales_report.py:134` and `:195` anchor weeks with
   `DATE_TRUNC('week', date + 1)::date - 1`. Every week-over-week number you look at is bucketed
   against the wrong week for your business.

Goal: a **Daily Summary** and a **Thursday-anchored Weekly Summary** covering sales, labor, and
product mix, as new pages in `sales_report.py`.

---

## What we verified against live Revel (read-only probes)

**Labor data exists and is clean.** `GET /resources/TimeSheetEntry/` — **487,591 records**,
filterable by `establishment` and `clock_in`. Real sample (LCF Katy, 2026-07-20):

```json
{"id": 585084, "employee": "/resources/Employee/1821/",
 "establishment": "/enterprise/Establishment/6/",
 "clock_in": "2026-07-20T09:03:11", "clock_out": "2026-07-20T14:06:04",
 "role_name": "Shift Manager", "role_wage": 15.5,
 "break_length": null, "break_type": null, "department_name": "Service Crew"}
```

Fields available: `clock_in`, `clock_out`, `role_name`, `role_wage`, `department_name`,
`break_length`, `break_type`, `is_auto_clock_out`, `exempt_salaried`, `employee`, `establishment`.
`role_wage` is populated — **labor cost and labor % of sales are computable**, not just hours.
`GET /resources/Employee/` (6,100 records) provides names/roles for the reference table.

Endpoints that do **not** exist (probed, all 404): `AttendanceEntry`, `EmployeeSchedule`,
`Timecard`, `Shift`, `PayrollEntry`, `LaborSchedule`, `TimeClock`, `Schedule`, `Payroll`.
**Consequence: Revel gives us hours *worked*, not hours *scheduled*.** Scheduled-vs-actual
variance is out of scope — we can only report actual labor.

**Product mix must be derived from names.** From the last 30 days:
- Heat is encoded **twice** — in item names (`** 3 Finger Regular **` 23,984 · `** 3 Finger Spicy **`
  17,895) *and* in modifiers (`Regular` 6,003 · `Spicy` 5,366 in 14d). Mixed items exist
  (`** 2 Regular 2 Spicy **`, 3,137). One canonical rule is needed.
- Shakes are fragmented across ~30 SKUs: size prefix (Small/Regular/Large) × flavor
  (Oreo/Strawberry/Chocolate/Vanilla/Orange Dream/Salted **Carmel** — note the misspelling,
  which coexists with correctly-spelled "Salted Caramel Cookie"), plus combo variants with a
  `*` suffix (`Oreo Shake*`), plus a catch-all product literally named `Shakes`.

**Data quality note:** 32 duplicate `orders.id` rows in the last 60 days (order_items are clean).
Small, but they double-count revenue. Summary queries must dedupe.

---

## API reference — which endpoint gives us what

Base URL `https://laynes.revelup.com`. All calls go through the existing Playwright session
(`/tmp/revel_session.json`) and the `fetch_all_pages()` limit/offset helper in `pipeline.py`.

### Endpoints we already use

| Endpoint | Volume | Fields we take | What it powers |
|---|---|---|---|
| `/resources/Order/` | 15.9M all-time | `final_total`, `subtotal`, `tax`, `gratuity`, `discount_total_amount`, `dining_option`, `created_date`, `closed`, `is_unpaid`, `deleted`, `number_of_people`, `web_order`, `pos_mode`, `customer` | Revenue, order counts, AOV, channel mix, discount rate |
| `/resources/OrderItem/` | fetched per order via `order__in` batches of 200 | `product`, `product_name_override`, `quantity`, `price`, `pure_sales`, `tax_amount`, `modifier_amount`, `start_time`, `kitchen_completed`, `is_voided`/`voided_*`, `combo_uuid`, `dining_option`, **`modifieritems` (embedded)** | Product mix, tender counts, kitchen speed, voids. `modifieritems` is embedded here — that's where `modifier_items` rows come from, resolved against the Modifier cache |
| `/resources/Product/` | 35,381 | `name`, `productclass`, `is_combo`, `price`, `active` | Product master |
| `/resources/Modifier/` | 114 distinct names in use | `name`, `price`, `active` | Resolves modifier IDs to names — **carries the Spicy/Regular heat signal** |
| `/resources/Establishment/{id}/` | 12 | `name`, timezone | Location master (`seed_establishments.py`) |

`Order` exposes 117 fields and `OrderItem` 113; we ingest roughly 20 and 25 respectively. The rest
are available without any new endpoint if a summary needs them later.

### Endpoints to add for this work

| Endpoint | Volume | Fields | What it powers | Priority |
|---|---|---|---|---|
| `/resources/TimeSheetEntry/` | 487,591 | `clock_in`, `clock_out`, `role_name`, `role_wage`, `department_name`, `break_length`, `break_type`, `is_auto_clock_out`, `exempt_salaried`, `employee`, `establishment` | **All labor**: hours, cost, labor % of sales, SPLH, cost per order, role/department split | Required |
| `/resources/Employee/` | 6,100 | `first_name`, `last_name`, `active`, `employee_start`, `employee_end`, `weekly_wage`, `pin` | Employee names for labor reporting | Required |
| `/resources/Role/` | 23 | `name`, `department`, `rank` | Role reference (GM, AGM, Shift Manager, Cook, Cashier, Porter, Trainer…) | Optional — `role_name` is already denormalized onto TimeSheetEntry |
| `/products/ProductClass/` | 11 | `name` | Coarse spine: `1. Food`, `2. Beverage`, `3. Merchandise`, `Sides`, `Meals`, `4. Other Sales` | Optional — useful as a cross-check, see below |

`TimeSheetEntry` filters on `establishment` and `clock_in` (`clock_in__gte` / `clock_in__lte`),
which is exactly the access pattern the nightly run needs.

### Evaluated and rejected

| Endpoint | Why not |
|---|---|
| `/products/ProductCategory/` | 2,595 rows, but only coarse per-establishment buckets — `Main Menu`, `Food`, `Catering Food`, `Merch`. Sandwiches, fries, sauce and lemonade all share one category ID |
| `/resources/ProductGroup/` | 1,780 rows. Does contain `3 Fingers` / `4 Fingers` / `5 Fingers` / `Kid Fingers`, but duplicated per establishment and polluted with `active items` and `Untaxed Group`. Usable as a validation signal at best |

**Do not exist on this account** (probed, all 404 — don't re-probe): `AttendanceEntry`,
`EmployeeSchedule`, `TimeSheet`, `Timesheet`, `TimesheetEntry`, `Timecard`, `Shift`,
`EmployeeShift`, `ScheduledShift`, `PayrollEntry`, `Payroll`, `LaborSchedule`, `TimeClock`,
`PaySchedule`, `Schedule`, `ShiftSchedule`, `Attendance`, `AttendanceLog`, `EmployeeHours`,
`LaborHours`, `Punch`, `Break`, `TimeEntry`, `EmployeeJob`, `JobRole`, `enterprise/Employee`.
There is also no `/resources/` index endpoint — resources must be probed by name.

---

## Approach

### 1. Product taxonomy layer

**First, a one-word bug to fix.** `products.product_class` is NULL for all 35,381 rows because
`pipeline.py:265` reads `p.get("product_class")` — but Revel's field is spelled **`productclass`**
(no underscore), and it *is* populated (37 of 40 sampled products). Fixing that line and re-running
the product refresh gives us Revel's own classification for free.

That classification is **too coarse to be the answer, but worth having as a spine**:
`/products/ProductClass/` has 11 values — `1. Food`, `2. Beverage`, `3. Merchandise`, `Sides`,
`Meals`, `4. Other Sales`, `Donation`, `Gift`. It separates food from drinks from sides, but it
cannot distinguish spicy from regular, or a Small Oreo Shake from a Large Vanilla Shake.
**So name-parsing is still required** — we use `productclass` as a cross-check and as a tripwire
for newly added SKUs that the name rules haven't seen.

New module `product_taxonomy.py`, modelled directly on the existing
`weather_analysis/tender_counting.py` — a dependency-free, rule-ordered name classifier.
Reuse `TenderCounter` from that module for finger counts rather than reimplementing.

Resolves a product name to: `category` (Tenders / Shakes / Drinks / Sides / Desserts / Sauces),
`subcategory`, `size` (Small/Regular/Large), `flavor`, `fingers`, and — because of mixed combos —
**`regular_units` and `spicy_units` as counts, not a single heat label**, so `2 Regular 2 Spicy`
splits correctly instead of being forced into one bucket.

Materialize it as a table rather than re-parsing in every query:

```
product_taxonomy(product_name_norm PK, category, subcategory, size, flavor,
                 fingers, regular_units, spicy_units, is_combo_variant,
                 is_override BOOLEAN, classified_at)
```

Built by a script over `SELECT DISTINCT product_name FROM order_items` (a few hundred real names,
not all 35k products). `is_override` protects manual corrections from being clobbered on re-run —
this is how "Salted Carmel" and the bare `Shakes` SKU get fixed once and stay fixed.

Heat resolution rule: **item name wins; fall back to the `modifier_items` join only when the name
is heat-neutral.** This avoids double-counting, since most spicy items encode heat in both places.

- Files: new `product_taxonomy.py`; new table in `database_design.sql`; new
  `build_taxonomy.py` (or a `--taxonomy` subcommand on `aggregate_features.py`).

### 2. Labor ingestion

- **`pipeline.py`** — add `fetch_timesheets()` using the existing `fetch_all_pages()` helper
  against `/resources/TimeSheetEntry/` with `establishment=` + `clock_in__gte/__lt`, and
  `fetch_employees()` against `/resources/Employee/` (reference upsert, same shape as the
  existing `fetch_products()` / `fetch_modifiers()` pattern, and skippable via `--skip-reference`).
- **Timestamps: use the existing `parse_dt()`.** Revel returns naive `America/Chicago` for
  timesheets exactly as it does for orders — this is the same trap as the timezone bug already
  fixed in this repo. Do not treat them as UTC.
- **Ingest with a rolling re-fetch window, not append-only.** `clock_out` is written hours after
  `clock_in`, and shifts get edited after the fact, so a strict "yesterday only" append leaves
  permanently open shifts. Re-fetch the trailing ~7 days and **upsert on `id`**. This also
  sidesteps the duplicate-row class of bug seen in `orders`.
- New tables (plain, not partitioned — volume is ~150 rows/day network-wide):
  ```
  employees(id PK, first_name, last_name, active, employee_start, employee_end, ...)
  labor_shifts(id PK, establishment_id, employee_id, clock_in, clock_out,
               role_name, role_wage, department_name, break_length, break_type,
               is_auto_clock_out, exempt_salaried, hours GENERATED, ingested_at)
  ```
  `hours` as a generated column mirrors the `kitchen_seconds` pattern already in `order_items`.

### 3. Labor aggregation

- **`aggregate_features.py`** — add `features_labor_daily(establishment_id, date, labor_hours,
  labor_cost, shift_count, headcount, hours_by_role JSONB, cost_by_role JSONB, ot_hours)`.
- Derived metrics computed at read time by joining `features_daily_summary`:
  **labor % of sales**, **sales per labor hour (SPLH)**, **labor cost per order**.
- Attribute a shift to the **business date of its `clock_in`** (Chicago-local), consistent with how
  `features_daily_summary.date` is already derived. Closing shifts crossing midnight stay on the
  opening day — worth confirming that matches how you read the numbers.

### 4. Thursday week anchor

Single shared helper, used everywhere, replacing the Sunday anchor:

```sql
-- Thursday-start week (Postgres DOW: Thu=4)
(date - ((EXTRACT(DOW FROM date)::int + 3) % 7))::date AS week_start
```
```python
# Python: Mon=0, Thu=3
week_start = d - timedelta(days=(d.weekday() - 3) % 7)
```

- Fix `sales_report.py:134` (`load_available_weeks`) and `:195` (`load_trend`) — these hardcode the
  Sunday anchor. `load_week_summary` and `load_daily` take `week_start` as a parameter and need no
  change beyond receiving the corrected anchor.
- **Do not use `features_daily_summary.week_of_year`** for any of this — `aggregate_features.py:67`
  populates it with `EXTRACT(WEEK)`, which is ISO/Monday-based. Derive `week_start` from `date`.
- Put the helper in a small shared module so both dashboards use one definition.

### 5. Summary pages in `sales_report.py`

**Daily Summary** — pick a date; per location and network total:
sales (revenue, orders, AOV, channel mix) · labor (hours, cost, labor %, SPLH) ·
product mix (category breakdown, spicy vs regular split, shake units by flavor and size) ·
exceptions (voids, discounts, kitchen-time outliers).

**Weekly Summary** — Thursday→Wednesday, with week-over-week deltas on every headline number,
day-by-day breakdown within the week, and location ranking. Reuse the existing `kpi_card()`,
`wow_parts()`, `fmt_k()`, and `rank_emoji()` helpers in `sales_report.py` rather than new ones.

Charts: load the `dataviz` skill before writing any chart code.

**Deployment note:** `setup.sh` does not deploy either Streamlit app, and repo-root `run.sh` has
`APP_DIR` hardcoded to this dev box. Adding the labor step to `run.sh` covers the local nightly run;
the deployed service will need the same change applied wherever it actually runs.

---

## Suggested order

1. Thursday week anchor (small, immediately corrects numbers you already look at)
2. Product taxonomy + mix rollup
3. Labor ingestion + backfill
4. Labor aggregation
5. Daily + Weekly summary pages

Steps 1–2 deliver a usable summary on data already in the DB; 3–4 add the labor half.

---

## Verification

- **Taxonomy:** dump every distinct `product_name` with its assigned category/heat/size/flavor and
  eyeball the tail; assert no sold product lands in an "unclassified" bucket. Cross-check that
  `regular_units + spicy_units` summed over a day matches total tender units from `TenderCounter`.
- **Labor:** backfill one location for one known day and reconcile hours and cost against Revel's
  own labor report in the UI. Confirm `clock_in` in the DB matches the wall-clock time Revel shows
  (the timezone regression test). Verify a re-run changes no row counts (upsert idempotency).
- **Week anchor:** assert every generated `week_start` has `EXTRACT(DOW) = 4`, and that a known
  Wednesday and the following Thursday land in different weeks.
- **Summaries:** for one date, reconcile the Daily Summary revenue against
  `SELECT SUM(final_total) FROM orders` (deduped by `id`) for that Chicago-local day. Confirm the
  weekly total equals the sum of its seven daily totals.
- **End to end:** run `./run.sh` for a single date and confirm pipeline → aggregation → labor →
  taxonomy all complete, then load both new pages.

---

## Out of scope / flagged

- **Scheduled labor and schedule-vs-actual variance** — Revel exposes no scheduling endpoint on
  this account (probed, 404). Actual hours only.
- **The 32 duplicate `orders.id` rows** will be deduped in summary queries. Fixing the underlying
  ingestion cause is a separate task from this one; say the word and I'll fold it in.
