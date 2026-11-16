-- 62_financial_reporting_accuracy.sql
-- Forecast coverage on the R365 weekly / fiscal-period views, and explicit
-- Revel sales metrics. Found by the 2026-11-16 financial accuracy audit.
--
-- 1. FORECAST COVERAGE. R365 sends forecasted_sales = 0 for days nobody
--    forecast; the views already turn that into NULL, and R365's own variance
--    (forecast_sales_base - forecasted_sales) compares only forecast-covered
--    sales. But nothing said HOW MANY days were covered. In P11 FY2026 Airtex
--    and Ella had no forecast for 2026-10-29..11-04 and Beaumont none for
--    2026-10-15..10-21 (21 of 28 days each), and the assistant printed full-
--    period net_sales beside the 21-day forecast and variance -- a table that
--    does not add up -- and called it a full-period comparison.
--    New columns, APPENDED (existing columns, order and every existing
--    calculation are unchanged, so no reader breaks):
--      forecast_days                    days whose R365 forecast is > 0
--      days_in_week / days_in_period    calendar days in the week / period
--      forecast_coverage_status         FULL | PARTIAL | NONE
--      sales_on_forecast_days           = forecast_sales_base (R365's figure)
--      sales_on_days_without_forecast   = net_sales - sales_on_forecast_days
--    sales_vs_forecast_* are untouched: they remain R365's own calculation.
--
-- 2. REVEL SALES METRICS. "Sales" in this project has meant SUM(final_total)
--    over REAL tickets, which INCLUDES sales tax. v_revel_sales_metrics_daily_llm
--    names each figure explicitly, per store per Chicago calendar day:
--      gross_sales_before_discounts = SUM(subtotal), REAL + COMP tickets
--      discounts                    = SUM(discount_amount), REAL tickets
--      comps                        = SUM(subtotal), COMP tickets ($0 tickets
--                                     with items: fully comped)
--      net_sales_excluding_tax      = gross - discounts - comps
--      tax                          = SUM(tax), REAL tickets
--      total_including_tax          = SUM(final_total), REAL tickets (the
--                                     legacy "sales" figure, unchanged)
--      payments_received            = v_payments_daily_v2.payment_amount (the
--                                     reconciliation reference, by payment date)
--    Verified identity: final_total = subtotal + tax - discount_amount holds
--    for every COMP order and all but 5 of 122,248 REAL orders (2026-10-15 ..
--    11-14); identity_residual exposes any such difference instead of hiding it.
--    open_real_* count REAL tickets not marked closed (the cause of the
--    features_daily_summary_v2 differences, which counts closed tickets only).
--    This view does NOT change the default meaning of "sales" anywhere.
--
-- Additive only. Grants are in 63 (the 43/44, 55, 59 convention).

BEGIN;

CREATE OR REPLACE VIEW v_r365_store_weekly_llm AS
SELECT w.establishment_id,
       e.name                                                   AS store_name,
       w.week_start, w.week_end,
       p.fiscal_year, p.period_number, p.period_label,
       c.week_status,
       w.days_with_data,
       -- SALES
       w.net_sales, w.gross_sales, w.guest_count, w.check_count,
       CASE WHEN w.check_count > 0 THEN w.net_sales / w.check_count END AS avg_check,
       CASE WHEN w.guest_count > 0 THEN w.net_sales / w.guest_count END AS avg_per_guest,
       -- LABOUR
       w.labor_hours, w.labor_cost,
       CASE WHEN w.net_sales <> 0 THEN w.labor_cost / w.net_sales * 100 END AS labor_pct,
       CASE WHEN w.labor_hours > 0 THEN w.net_sales / w.labor_hours END   AS sales_per_labor_hour,
       -- CASH
       w.total_deposit, w.paid_in, w.paid_out, w.over_short,
       -- DISCOUNTS
       w.discounts, w.comps, w.coupons, w.total_discounts,
       CASE WHEN w.net_sales <> 0 THEN w.total_discounts / w.net_sales * 100 END AS discount_pct,
       -- GIFT CARDS / TIPS / VOIDS
       w.gift_cards_redeemed, w.gift_cards_sold,
       w.tips_declared, w.credit_card_tips,
       w.void_count, w.void_amount,
       -- FORECAST (forecast-covered sales only)
       CASE WHEN w.forecasted_sales > 0 THEN w.forecasted_sales END        AS forecasted_sales,
       CASE WHEN w.forecasted_sales > 0 THEN w.forecast_sales_base END     AS forecast_sales_base,
       CASE WHEN w.forecasted_sales > 0
            THEN w.forecast_sales_base - w.forecasted_sales END            AS sales_vs_forecast_amount,
       CASE WHEN w.forecasted_sales > 0
            THEN (w.forecast_sales_base - w.forecasted_sales)
                 / w.forecasted_sales * 100 END                            AS sales_vs_forecast_pct,
       -- FORECAST COVERAGE (migration 62; appended)
       COALESCE(fc.forecast_days, 0)                                       AS forecast_days,
       (w.week_end - w.week_start + 1)                                     AS days_in_week,
       CASE WHEN COALESCE(fc.forecast_days, 0) = 0 OR COALESCE(w.forecasted_sales, 0) <= 0
                 THEN 'NONE'
            WHEN fc.forecast_days >= (w.week_end - w.week_start + 1) THEN 'FULL'
            ELSE 'PARTIAL' END                                             AS forecast_coverage_status,
       CASE WHEN w.forecasted_sales > 0 THEN w.forecast_sales_base END     AS sales_on_forecast_days,
       w.net_sales - CASE WHEN w.forecasted_sales > 0
                          THEN w.forecast_sales_base ELSE 0 END           AS sales_on_days_without_forecast
  FROM r365_store_weekly w
  JOIN r365_store_mapping m ON m.r365_store_id = w.r365_store_id
  JOIN establishments e     ON e.id = w.establishment_id
  LEFT JOIN v_r365_completeness_llm c ON c.week_start = w.week_start
  LEFT JOIN ayg_business_periods p
         ON w.week_start BETWEEN p.start_date AND p.end_date
  LEFT JOIN LATERAL (
         SELECT count(*) FILTER (WHERE d.forecasted_sales > 0) AS forecast_days
           FROM r365_store_daily d
          WHERE d.establishment_id = w.establishment_id
            AND d.business_date BETWEEN w.week_start AND w.week_end
       ) fc ON true
 WHERE m.status = 'VERIFIED' AND m.active;

COMMENT ON VIEW v_r365_store_weekly_llm IS
  'OFFICIAL R365 weekly row per store (Thursday->Wednesday). Authoritative for '
  'weekly R365 questions; never replace it with summed daily rows. '
  'Percentages are POINTS. Forecast variance uses forecast-covered sales '
  '(forecast_sales_base = sales_on_forecast_days), not all net sales; '
  'forecast_days / forecast_coverage_status say how many days had a forecast. '
  'Never SUM a ratio column.';

CREATE OR REPLACE VIEW v_r365_store_period_llm AS
WITH agg AS (
    SELECT w.establishment_id, w.store_name, w.fiscal_year, w.period_number,
           w.period_label,
           count(*)                                         AS weeks_loaded,
           count(*) FILTER (WHERE w.week_status = 'COMPLETE') AS weeks_complete,
           min(w.week_start)                                AS first_week_start,
           max(w.week_end)                                  AS last_week_end,
           sum(w.net_sales)            AS net_sales,
           sum(w.gross_sales)          AS gross_sales,
           sum(w.guest_count)          AS guest_count,
           sum(w.check_count)          AS check_count,
           sum(w.labor_hours)          AS labor_hours,
           sum(w.labor_cost)           AS labor_cost,
           sum(w.total_deposit)        AS total_deposit,
           sum(w.paid_in)              AS paid_in,
           sum(w.paid_out)             AS paid_out,
           sum(w.over_short)           AS over_short,
           sum(w.discounts)            AS discounts,
           sum(w.comps)                AS comps,
           sum(w.coupons)              AS coupons,
           sum(w.total_discounts)      AS total_discounts,
           sum(w.gift_cards_redeemed)  AS gift_cards_redeemed,
           sum(w.gift_cards_sold)      AS gift_cards_sold,
           sum(w.tips_declared)        AS tips_declared,
           sum(w.credit_card_tips)     AS credit_card_tips,
           sum(w.void_count)           AS void_count,
           sum(w.void_amount)          AS void_amount,
           sum(w.forecasted_sales)     AS forecasted_sales,
           sum(w.forecast_sales_base)  AS forecast_sales_base,
           sum(w.days_with_data)       AS days_with_data,
           sum(w.forecast_days)        AS forecast_days
      FROM v_r365_store_weekly_llm w
     WHERE w.fiscal_year IS NOT NULL
     GROUP BY 1, 2, 3, 4, 5
)
SELECT a.establishment_id, a.store_name, a.fiscal_year, a.period_number,
       a.period_label, p.start_date, p.end_date, p.period_status,
       (p.end_date - p.start_date + 1) / 7                 AS weeks_in_period,
       a.weeks_loaded, a.weeks_complete,
       (a.weeks_loaded = (p.end_date - p.start_date + 1) / 7
        AND a.weeks_complete = a.weeks_loaded)             AS period_fully_covered,
       a.first_week_start, a.last_week_end,
       a.net_sales, a.gross_sales, a.guest_count, a.check_count,
       CASE WHEN a.check_count > 0 THEN a.net_sales / a.check_count END   AS avg_check,
       CASE WHEN a.guest_count > 0 THEN a.net_sales / a.guest_count END   AS avg_per_guest,
       a.labor_hours, a.labor_cost,
       CASE WHEN a.net_sales <> 0 THEN a.labor_cost / a.net_sales * 100 END AS labor_pct,
       CASE WHEN a.labor_hours > 0 THEN a.net_sales / a.labor_hours END   AS sales_per_labor_hour,
       a.total_deposit, a.paid_in, a.paid_out, a.over_short,
       a.discounts, a.comps, a.coupons, a.total_discounts,
       CASE WHEN a.net_sales <> 0 THEN a.total_discounts / a.net_sales * 100 END AS discount_pct,
       a.gift_cards_redeemed, a.gift_cards_sold,
       a.tips_declared, a.credit_card_tips,
       a.void_count, a.void_amount,
       a.forecasted_sales, a.forecast_sales_base,
       CASE WHEN a.forecasted_sales > 0
            THEN a.forecast_sales_base - a.forecasted_sales END            AS sales_vs_forecast_amount,
       CASE WHEN a.forecasted_sales > 0
            THEN (a.forecast_sales_base - a.forecasted_sales)
                 / a.forecasted_sales * 100 END                            AS sales_vs_forecast_pct,
       a.days_with_data,
       -- FORECAST COVERAGE (migration 62; appended). FULL only when every
       -- calendar day of the period is loaded AND forecast.
       COALESCE(a.forecast_days, 0)                                        AS forecast_days,
       (p.end_date - p.start_date + 1)                                     AS days_in_period,
       CASE WHEN COALESCE(a.forecast_days, 0) = 0 OR COALESCE(a.forecasted_sales, 0) <= 0
                 THEN 'NONE'
            WHEN a.forecast_days >= (p.end_date - p.start_date + 1) THEN 'FULL'
            ELSE 'PARTIAL' END                                             AS forecast_coverage_status,
       CASE WHEN a.forecasted_sales > 0 THEN a.forecast_sales_base END     AS sales_on_forecast_days,
       a.net_sales - COALESCE(CASE WHEN a.forecasted_sales > 0
                                   THEN a.forecast_sales_base END, 0)     AS sales_on_days_without_forecast
  FROM agg a
  -- the _llm calendar view carries period_status (Chicago-today derived)
  JOIN v_ayg_business_periods_llm p
    ON p.fiscal_year = a.fiscal_year AND p.period_number = a.period_number;

COMMENT ON VIEW v_r365_store_period_llm IS
  'Store x AYG fiscal period rollup of the OFFICIAL weekly rows (4 weeks per '
  'period). Ratios recomputed from summed numerators, never averaged. '
  'period_fully_covered = false means a current or partly loaded period: '
  'label it partial and never compare it against a full period''s totals. '
  'period_fully_covered says nothing about FORECAST coverage: use '
  'forecast_days / days_in_period / forecast_coverage_status, and compare '
  'forecast only with sales_on_forecast_days.';

CREATE OR REPLACE VIEW v_revel_sales_metrics_daily_llm AS
WITH o AS (
    SELECT establishment_id, business_date,
           count(*) FILTER (WHERE txn_class = 'REAL')                       AS real_transactions,
           count(*) FILTER (WHERE txn_class = 'COMP')                       AS comp_tickets,
           sum(subtotal) FILTER (WHERE txn_class IN ('REAL', 'COMP'))       AS gross_sales_before_discounts,
           sum(COALESCE(discount_amount, 0)) FILTER (WHERE txn_class = 'REAL') AS discounts,
           sum(subtotal) FILTER (WHERE txn_class = 'COMP')                  AS comps,
           sum(tax) FILTER (WHERE txn_class = 'REAL')                       AS tax,
           sum(final_total) FILTER (WHERE txn_class = 'REAL')               AS total_including_tax,
           count(*) FILTER (WHERE txn_class = 'REAL' AND closed IS NOT TRUE) AS open_real_tickets,
           sum(final_total) FILTER (WHERE txn_class = 'REAL' AND closed IS NOT TRUE)
                                                                            AS open_real_ticket_total
      FROM v_orders_classified
     GROUP BY establishment_id, business_date
)
SELECT o.establishment_id,
       e.name                                                       AS store_name,
       o.business_date,
       o.real_transactions,
       o.comp_tickets,
       COALESCE(o.gross_sales_before_discounts, 0)                  AS gross_sales_before_discounts,
       COALESCE(o.discounts, 0)                                     AS discounts,
       COALESCE(o.comps, 0)                                         AS comps,
       COALESCE(o.gross_sales_before_discounts, 0) - COALESCE(o.discounts, 0)
           - COALESCE(o.comps, 0)                                   AS net_sales_excluding_tax,
       COALESCE(o.tax, 0)                                           AS tax,
       COALESCE(o.total_including_tax, 0)                           AS total_including_tax,
       pay.payments_received,
       COALESCE(o.total_including_tax, 0)
         - (COALESCE(o.gross_sales_before_discounts, 0) - COALESCE(o.discounts, 0)
            - COALESCE(o.comps, 0) + COALESCE(o.tax, 0))            AS identity_residual,
       o.open_real_tickets,
       COALESCE(o.open_real_ticket_total, 0)                        AS open_real_ticket_total
  FROM o
  JOIN establishments e ON e.id = o.establishment_id
  LEFT JOIN LATERAL (
         SELECT sum(pm.amount) AS payments_received
           FROM payments_v2 pm
          WHERE pm.establishment_id = o.establishment_id
            AND pm.deleted IS NOT TRUE
            AND pm.created_date >= (o.business_date::timestamp AT TIME ZONE 'America/Chicago')
            AND pm.created_date <  ((o.business_date + 1)::timestamp AT TIME ZONE 'America/Chicago')
       ) pay ON true;

COMMENT ON VIEW v_revel_sales_metrics_daily_llm IS
  'Explicit Revel sales metrics per store per Chicago calendar day. '
  'gross_sales_before_discounts - discounts - comps = net_sales_excluding_tax; '
  'net + tax = total_including_tax (+ identity_residual). total_including_tax '
  'is the legacy "sales" figure (REAL final_total, includes tax). '
  'payments_received matches v_payments_daily_v2 (payment date). None of these '
  'is verified against a Revel UI report definition.';

-- Same owner as every other analytics view, whoever applies this file (the
-- replaced R365 views keep their existing owner automatically).
ALTER VIEW v_revel_sales_metrics_daily_llm OWNER TO laynes_user;

COMMIT;
