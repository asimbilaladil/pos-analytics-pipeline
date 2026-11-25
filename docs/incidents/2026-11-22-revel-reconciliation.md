# 2026-11-22 Revel sales reconciliation alert (Shepherd, Missouri City)

Investigated 2026-11-25, read-only, on production `c6d743c`.

## What AI Chat showed

AI Chat runs a per-store check (`chat_sql.revel_store_breakdown`, the same
function the monitor now uses). It compares each store's REAL order totals
(`v_orders_classified`) with its payments for the same Chicago business day
(`v_payments_daily_v2`). The tolerance is ±0.5%, and it was not changed.

For 2026-11-22, 10 of 12 stores reconciled. Two did not:

| Store | Delta | AI Chat category |
|---|---|---|
| LCF Missouri City | **+5.42%** (+$334.51) | payment_timing |
| LCF Shepherd | **+0.66%** (+$59.15) | reconciliation_mismatch |

Neither case is data corruption, and the sync did not fail. Every store's
`orders_v2` and `payments_v2` synced successfully on 2026-11-24 at 15:09–15:18 UTC.

## Missouri City: a prepaid scheduled order

One REAL order (id 19387154, $334.48, Revel created-date 2026-11-22 03:00,
pickup 2026-11-22 14:00) was **paid on 2026-11-19 at 13:00**. The order counts
toward 11-22, but its payment counts toward 11-19. That accounts for $334.48 of
the $334.51 gap; the other $0.03 is a payment correction dated the next day.

The same pattern explains 2026-11-21 (+3.60%): order 19432433, $234.88, was
prepaid on 11-20.

Both sides are complete: payments linked to the 11-22 orders total $6,502.38,
within tolerance of the order total. This is a **payment-timing difference on
advance (scheduled or catering) orders**. Nothing is missing.

## Shepherd: open tickets with unpaid balances

Five REAL orders on 11-22 were never closed in Revel (`closed = false`):

- 3 orders (16:33, 17:44, 01:28 local) with **no payment at all**: $48.53.
- 2 orders with a **remaining balance** ($5.41 and $0.24): $5.65.

Revel still showed all five as open when they were re-ingested on 11-24 at
15:16 UTC. So the gap is in Revel itself, not something we missed. Together they
explain $54.18 of the $59.15. The remaining $4.97 is the net of two
cross-midnight payments ($13.39 for an 11-22 order paid on 11-23, and $8.42 for
an 11-21 order paid on 11-22).

This keeps happening at Shepherd. In the last 30 days it had 52 such orders
on 14 days ($558). The next highest store had 4 orders.

**Cause:** a store-operations issue, with tickets left open or unpaid in Revel.

**Action:** the store manager should close, settle or void those tickets in
Revel. The next daily sync picks up the change, and the day reconciles on its
own.

2026-11-23 shows the same at Shepherd (+0.63%, 3 open tickets, $55.96).

## What the monitor does with these

The monitor classifies each failing store-day:

- **Missouri City → `payment_timing`, WARNING.** It is visible on the dashboard
  and in the daily summary, but no alert is pushed. The page explains that this
  is not data loss.
- **Shepherd → `open_unpaid_tickets`, WARNING.** It applies when open tickets
  explain at least 90% of the gap. Its guidance is to ask the store to settle the
  tickets.
- **Unexplained mismatch → HIGH.** A mismatch that neither explains (orders
  versus payments, missing days, a stale sync) is pushed as an alert.

AI Chat's behaviour is unchanged: it still withholds figures for any store-day
outside ±0.5%. That is why users see these days excluded.
