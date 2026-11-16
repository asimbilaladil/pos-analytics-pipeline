-- Rollback for migrations 62 + 63 (financial reporting accuracy).
-- NOT a migration: apply only to undo 62/63, as the view owner, e.g.
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -d laynes -c 'SET ROLE laynes_user' -f - < this file
-- Restores v_r365_store_weekly_llm / v_r365_store_period_llm exactly as
-- migration 54 defined them (CREATE OR REPLACE cannot drop the appended
-- columns, so both are dropped and recreated; the period view is the weekly
-- view's only dependent), restores their laynes_ro grants (migration 55), and
-- removes v_revel_sales_metrics_daily_llm (its grant goes with it).
-- Code that reads the 62 columns must be rolled back FIRST.

BEGIN;

DROP VIEW IF EXISTS v_revel_sales_metrics_daily_llm;
DROP VIEW v_r365_store_period_llm;
DROP VIEW v_r365_store_weekly_llm;

-- verbatim from migrations/54_r365_canonical.sql
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
                 / w.forecasted_sales * 100 END                            AS sales_vs_forecast_pct
  FROM r365_store_weekly w
  JOIN r365_store_mapping m ON m.r365_store_id = w.r365_store_id
  JOIN establishments e     ON e.id = w.establishment_id
  LEFT JOIN v_r365_completeness_llm c ON c.week_start = w.week_start
  LEFT JOIN ayg_business_periods p
         ON w.week_start BETWEEN p.start_date AND p.end_date
 WHERE m.status = 'VERIFIED' AND m.active;

COMMENT ON VIEW v_r365_store_weekly_llm IS
  'OFFICIAL R365 weekly row per store (Thursday->Wednesday). Authoritative for '
  'weekly R365 questions; never replace it with summed daily rows. '
  'Percentages are POINTS. Forecast variance uses forecast-covered sales '
  '(forecast_sales_base), not all net sales. Never SUM a ratio column.';

-- Store x fiscal period rollup from the official weekly rows. Ratios are
-- RECOMPUTED from summed numerators/denominators, never averaged.
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
           sum(w.days_with_data)       AS days_with_data
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
       a.days_with_data
  FROM agg a
  -- the _llm calendar view carries period_status (Chicago-today derived)
  JOIN v_ayg_business_periods_llm p
    ON p.fiscal_year = a.fiscal_year AND p.period_number = a.period_number;

COMMENT ON VIEW v_r365_store_period_llm IS
  'Store x AYG fiscal period rollup of the OFFICIAL weekly rows (4 weeks per '
  'period). Ratios recomputed from summed numerators, never averaged. '
  'period_fully_covered = false means a current or partly loaded period: '
  'label it partial and never compare it against a full period''s totals.';

-- from migrations/55_r365_llm_grants.sql
GRANT SELECT ON v_r365_store_weekly_llm  TO laynes_ro;
GRANT SELECT ON v_r365_store_period_llm  TO laynes_ro;

COMMIT;
