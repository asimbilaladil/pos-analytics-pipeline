-- Task 14 — shadow analytics/feature tables, sourced from orders_v2/order_items_v2
-- ONLY. Structural mirror of features_hourly/features_product_daily/
-- features_daily_summary (same columns, constraints, indexes) so a future
-- cutover (Task 16, not part of this migration) can swap tables directly.
--
-- aggregate_features_v2.py populates these using the exact same SQL/formulas
-- as aggregate_features.py, just retargeted at orders_v2/order_items_v2 as
-- source and these _v2 tables as destination. Three pre-existing live
-- columns (features_hourly.unique_customers, features_product_daily.
-- combo_attach_rate, features_product_daily.is_combo_item) are not populated
-- by the live script either -- mirrored here at the same defaults, not
-- newly wired up, to keep this a corrected-source recompute, not a
-- metric-definition change.
--
-- Never read by any live dashboard/view until an explicit future cutover.

CREATE TABLE features_hourly_v2 (
    id                    BIGSERIAL PRIMARY KEY,
    establishment_id      INTEGER NOT NULL REFERENCES establishments(id),
    date                  DATE NOT NULL,
    hour                  SMALLINT NOT NULL,
    day_of_week           SMALLINT NOT NULL,
    week_of_year          SMALLINT NOT NULL,
    month                 SMALLINT NOT NULL,
    is_weekend            BOOLEAN NOT NULL,
    order_count           INTEGER DEFAULT 0,
    item_count            INTEGER DEFAULT 0,
    unique_customers      INTEGER DEFAULT 0,
    total_revenue         NUMERIC DEFAULT 0,
    avg_order_value       NUMERIC DEFAULT 0,
    avg_items_per_order   NUMERIC DEFAULT 0,
    orders_drive_through  INTEGER DEFAULT 0,
    orders_eat_in         INTEGER DEFAULT 0,
    orders_to_go          INTEGER DEFAULT 0,
    orders_doordash       INTEGER DEFAULT 0,
    orders_ubereats       INTEGER DEFAULT 0,
    orders_online         INTEGER DEFAULT 0,
    orders_lane_a         INTEGER DEFAULT 0,
    orders_lane_b         INTEGER DEFAULT 0,
    avg_kitchen_seconds   NUMERIC,
    void_count            INTEGER DEFAULT 0,
    void_rate             NUMERIC DEFAULT 0,
    computed_at           TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (establishment_id, date, hour)
);

CREATE INDEX idx_fhv2_est_date ON features_hourly_v2 (establishment_id, date, hour);
CREATE INDEX idx_fhv2_dow_hour ON features_hourly_v2 (day_of_week, hour, establishment_id);


CREATE TABLE features_product_daily_v2 (
    id                    BIGSERIAL PRIMARY KEY,
    establishment_id      INTEGER NOT NULL REFERENCES establishments(id),
    product_id            INTEGER NOT NULL REFERENCES products(id),
    product_name          VARCHAR,
    date                  DATE NOT NULL,
    day_of_week           SMALLINT NOT NULL,
    week_of_year          SMALLINT NOT NULL,
    month                 SMALLINT NOT NULL,
    is_weekend            BOOLEAN NOT NULL,
    quantity_sold         NUMERIC DEFAULT 0,
    order_count           INTEGER DEFAULT 0,
    revenue               NUMERIC DEFAULT 0,
    qty_drive_through     NUMERIC DEFAULT 0,
    qty_eat_in            NUMERIC DEFAULT 0,
    qty_third_party       NUMERIC DEFAULT 0,
    avg_kitchen_seconds   NUMERIC,
    max_kitchen_seconds   INTEGER,
    min_kitchen_seconds   INTEGER,
    kitchen_outliers      INTEGER DEFAULT 0,
    combo_attach_rate     NUMERIC,
    is_combo_item         BOOLEAN DEFAULT FALSE,
    void_count            INTEGER DEFAULT 0,
    void_rate             NUMERIC DEFAULT 0,
    computed_at           TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (establishment_id, product_id, date)
);

CREATE INDEX idx_fpdv2_est_date ON features_product_daily_v2 (establishment_id, date);
CREATE INDEX idx_fpdv2_product_date ON features_product_daily_v2 (product_id, date);
CREATE INDEX idx_fpdv2_dow ON features_product_daily_v2 (day_of_week, product_id, establishment_id);


CREATE TABLE features_daily_summary_v2 (
    id                      BIGSERIAL PRIMARY KEY,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    date                    DATE NOT NULL,
    day_of_week             SMALLINT NOT NULL,
    week_of_year            SMALLINT NOT NULL,
    month                   SMALLINT NOT NULL,
    is_weekend              BOOLEAN NOT NULL,
    total_orders            INTEGER DEFAULT 0,
    total_items             INTEGER DEFAULT 0,
    total_revenue           NUMERIC DEFAULT 0,
    avg_order_value         NUMERIC DEFAULT 0,
    avg_items_per_order     NUMERIC DEFAULT 0,
    pct_drive_through       NUMERIC DEFAULT 0,
    pct_third_party         NUMERIC DEFAULT 0,
    pct_in_store            NUMERIC DEFAULT 0,
    revenue_drive_through   NUMERIC DEFAULT 0,
    revenue_third_party     NUMERIC DEFAULT 0,
    revenue_in_store        NUMERIC DEFAULT 0,
    avg_kitchen_seconds     NUMERIC,
    pct_orders_over_10min   NUMERIC DEFAULT 0,
    total_voids             INTEGER DEFAULT 0,
    void_rate               NUMERIC DEFAULT 0,
    total_discounts         NUMERIC DEFAULT 0,
    discount_rate           NUMERIC DEFAULT 0,
    peak_hour               SMALLINT,
    peak_hour_orders        INTEGER,
    computed_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (establishment_id, date)
);

CREATE INDEX idx_fdsv2_date ON features_daily_summary_v2 (date);
CREATE INDEX idx_fdsv2_est_date ON features_daily_summary_v2 (establishment_id, date);
