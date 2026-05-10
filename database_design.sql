-- ============================================================
-- LAYNES CHICKEN FINGERS — PREDICTION PLATFORM
-- Database Design v1.0
-- PostgreSQL 15+
-- ============================================================
-- SCALE: 11 locations, ~580 orders/day/location
-- GROWTH: ~14M rows/year, ~3.5GB/year with indexes
-- SERVER: Standard PostgreSQL, $40-80/mo VPS handles 3+ years
-- ============================================================


-- ============================================================
-- LAYER 0 — REFERENCE / LOOKUP TABLES
-- Small, rarely changing. Loaded once, updated when menu changes.
-- ============================================================

-- Every Laynes location
CREATE TABLE establishments (
    id                  INTEGER PRIMARY KEY,        -- Revel ID e.g. 32
    name                VARCHAR(100) NOT NULL,      -- "Airtex", "Katy" etc.
    city                VARCHAR(100),
    state               VARCHAR(10),
    timezone            VARCHAR(50) DEFAULT 'US/Central',
    active              BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Product/menu item master list
-- Fetch once from /resources/Product/ and keep updated
CREATE TABLE products (
    id                  INTEGER PRIMARY KEY,        -- extracted from /resources/Product/18290/
    revel_uri           VARCHAR(100) UNIQUE,        -- /resources/Product/18290/
    name                VARCHAR(200) NOT NULL,      -- clean product name from Revel
    product_class       VARCHAR(100),               -- "1. Food", "2. Beverage", "Sides" etc.
    is_combo            BOOLEAN DEFAULT FALSE,
    is_modifier         BOOLEAN DEFAULT FALSE,
    base_price          NUMERIC(10,2),
    active              BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Modifier reference table — fetched from /resources/Modifier/ at pipeline start
-- 347 unique modifiers observed in sample data; refreshed nightly
CREATE TABLE modifiers (
    id                  INTEGER PRIMARY KEY,        -- extracted from /resources/Modifier/25002/
    revel_uri           VARCHAR(100) UNIQUE,        -- /resources/Modifier/25002/
    name                VARCHAR(200) NOT NULL,
    base_price          NUMERIC(10,2) DEFAULT 0,
    active              BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Dining channel lookup — decoded from dining_option integer
CREATE TABLE dining_channels (
    id                  SMALLINT PRIMARY KEY,       -- dining_option code from Revel
    name                VARCHAR(50) NOT NULL,       -- "Drive Through", "DD Marketplace" etc.
    channel_group       VARCHAR(50),                -- "drive_through", "third_party", "in_store", "online"
    is_third_party      BOOLEAN DEFAULT FALSE       -- TRUE for DoorDash, Uber Eats
);

-- Seed dining channels
INSERT INTO dining_channels VALUES
(0,   'To Go',           'in_store',      FALSE),
(1,   'Eat In',          'in_store',      FALSE),
(2,   'Delivery',        'delivery',      FALSE),
(3,   'Catering',        'catering',      FALSE),
(4,   'Drive Through',   'drive_through', FALSE),
(5,   'Online Ordering', 'online',        FALSE),
(6,   'Spirit Night',    'in_store',      FALSE),
(7,   'Shipping',        'delivery',      FALSE),
(8,   'Pickup',          'online',        FALSE),
(9,   'DoorDash Drive',  'third_party',   TRUE),
(100, 'DD Marketplace',  'third_party',   TRUE),
(101, 'Uber Eats',       'third_party',   TRUE),
(102, 'Eat In Fun',      'in_store',      FALSE),
(103, 'To Go Fun',       'in_store',      FALSE),
(104, 'Drive Thru Fun',  'drive_through', FALSE),
(105, 'Lane A',          'drive_through', FALSE),
(106, 'Lane B',          'drive_through', FALSE);


-- ============================================================
-- LAYER 1 — RAW INGESTION TABLES
-- Direct map from Revel API. Append-only. Never update rows.
-- Partitioned by month for fast queries and easy archival.
-- ============================================================

-- Orders — one row per Revel order
CREATE TABLE orders (
    -- Identity
    id                      BIGINT NOT NULL,            -- Revel order ID
    uuid                    UUID,                       -- Revel UUID
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    local_id                VARCHAR(50),                -- Revel local_id

    -- Timing (all stored in UTC, convert using establishment timezone)
    created_date            TIMESTAMPTZ NOT NULL,       -- exact order creation time
    updated_date            TIMESTAMPTZ,
    pickup_time             TIMESTAMPTZ,                -- for catering/scheduled orders

    -- Channel
    dining_option           SMALLINT REFERENCES dining_channels(id),
    pos_mode                VARCHAR(10),                -- "Q" = quick service

    -- Financial
    final_total             NUMERIC(10,2) DEFAULT 0,
    subtotal                NUMERIC(10,2) DEFAULT 0,
    tax                     NUMERIC(10,2) DEFAULT 0,
    gratuity                NUMERIC(10,2) DEFAULT 0,
    discount_total_amount   NUMERIC(10,2) DEFAULT 0,

    -- Status flags
    closed                  BOOLEAN DEFAULT FALSE,
    is_unpaid               BOOLEAN DEFAULT FALSE,
    deleted                 BOOLEAN DEFAULT FALSE,
    is_discounted           BOOLEAN DEFAULT FALSE,
    web_order               BOOLEAN DEFAULT FALSE,

    -- Customer linkage (nullable — many orders are anonymous)
    customer_id             INTEGER,                    -- extracted from /resources/Customer/123/

    -- Party info
    number_of_people        SMALLINT DEFAULT 0,

    -- Ingestion metadata
    ingested_at             TIMESTAMPTZ DEFAULT NOW(),  -- when WE pulled this record
    ingestion_date          DATE NOT NULL,              -- which daily run pulled it (for deduplication)

    PRIMARY KEY (id, created_date)                      -- composite for partitioning
) PARTITION BY RANGE (created_date);

-- Monthly partitions — create 1 per month, automate with a cron job
CREATE TABLE orders_2026_01 PARTITION OF orders FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE orders_2026_02 PARTITION OF orders FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE orders_2026_03 PARTITION OF orders FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE orders_2026_04 PARTITION OF orders FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE orders_2026_05 PARTITION OF orders FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE orders_2026_06 PARTITION OF orders FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE orders_2026_07 PARTITION OF orders FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE orders_2026_08 PARTITION OF orders FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE orders_2026_09 PARTITION OF orders FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE orders_2026_10 PARTITION OF orders FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE orders_2026_11 PARTITION OF orders FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE orders_2026_12 PARTITION OF orders FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- Indexes on orders (created on each partition automatically)
CREATE INDEX idx_orders_establishment_date ON orders (establishment_id, created_date);
CREATE INDEX idx_orders_dining_option ON orders (dining_option, created_date);
CREATE INDEX idx_orders_customer ON orders (customer_id) WHERE customer_id IS NOT NULL;
CREATE INDEX idx_orders_closed ON orders (closed, is_unpaid, created_date);


-- Order items — one row per item line in an order
-- ~4.5x more rows than orders (avg 5.8 items/order)
CREATE TABLE order_items (
    -- Identity
    id                      BIGINT NOT NULL,
    uuid                    UUID,
    order_id                BIGINT NOT NULL,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),

    -- What was ordered
    product_id              INTEGER REFERENCES products(id),   -- extracted from URI
    product_name            VARCHAR(200),                      -- product_name_override from Revel
    quantity                NUMERIC(8,2) DEFAULT 1,
    dining_option           SMALLINT REFERENCES dining_channels(id),

    -- Combo tracking
    combo_product_id        INTEGER REFERENCES products(id),   -- which combo this item belongs to
    combo_uuid              UUID,                              -- groups items in same combo

    -- Financial
    price                   NUMERIC(10,2) DEFAULT 0,           -- unit price
    pure_sales              NUMERIC(10,2) DEFAULT 0,           -- revenue excl tax
    tax_amount              NUMERIC(10,2) DEFAULT 0,
    modifier_amount         NUMERIC(10,2) DEFAULT 0,           -- upcharge from modifiers
    is_discounted           BOOLEAN DEFAULT FALSE,

    -- Kitchen performance — KEY for speed analysis
    created_date            TIMESTAMPTZ NOT NULL,              -- when item was ordered
    start_time              TIMESTAMPTZ,                       -- when KDS received it
    kitchen_completed       TIMESTAMPTZ,                       -- when KDS was bumped
    kitchen_seconds         INTEGER                            -- computed: kitchen_completed - start_time in seconds
                            GENERATED ALWAYS AS (
                                CASE WHEN kitchen_completed IS NOT NULL AND start_time IS NOT NULL
                                THEN EXTRACT(EPOCH FROM (kitchen_completed - start_time))::INTEGER
                                ELSE NULL END
                            ) STORED,

    -- Void tracking — KEY for waste analysis
    is_voided               BOOLEAN DEFAULT FALSE,
    voided_date             TIMESTAMPTZ,
    voided_by_user_id       INTEGER,                           -- extracted from /enterprise/User/123/
    voided_reason           VARCHAR(500),

    -- Status
    deleted                 BOOLEAN DEFAULT FALSE,
    deleted_date            TIMESTAMPTZ,

    -- Ingestion metadata
    ingested_at             TIMESTAMPTZ DEFAULT NOW(),
    ingestion_date          DATE NOT NULL,

    PRIMARY KEY (id, created_date)
) PARTITION BY RANGE (created_date);

-- Monthly partitions for order_items
CREATE TABLE order_items_2026_01 PARTITION OF order_items FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE order_items_2026_02 PARTITION OF order_items FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE order_items_2026_03 PARTITION OF order_items FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
CREATE TABLE order_items_2026_04 PARTITION OF order_items FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
CREATE TABLE order_items_2026_05 PARTITION OF order_items FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE order_items_2026_06 PARTITION OF order_items FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE order_items_2026_07 PARTITION OF order_items FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
CREATE TABLE order_items_2026_08 PARTITION OF order_items FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE order_items_2026_09 PARTITION OF order_items FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE TABLE order_items_2026_10 PARTITION OF order_items FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE order_items_2026_11 PARTITION OF order_items FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE order_items_2026_12 PARTITION OF order_items FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- Indexes on order_items
CREATE INDEX idx_oi_order_id ON order_items (order_id, created_date);
CREATE INDEX idx_oi_establishment_date ON order_items (establishment_id, created_date);
CREATE INDEX idx_oi_product_date ON order_items (product_id, created_date);
CREATE INDEX idx_oi_kitchen_speed ON order_items (establishment_id, product_id, kitchen_seconds)
    WHERE kitchen_seconds IS NOT NULL AND is_voided = FALSE;
CREATE INDEX idx_oi_voided ON order_items (establishment_id, voided_date)
    WHERE is_voided = TRUE;
CREATE INDEX idx_oi_combo ON order_items (combo_uuid) WHERE combo_uuid IS NOT NULL;


-- Modifier items — sauces, customizations on each order item
-- Smaller table, useful for preference prediction and upsell analysis
CREATE TABLE modifier_items (
    id                  BIGINT PRIMARY KEY,
    order_item_id       BIGINT NOT NULL,
    order_id            BIGINT NOT NULL,
    establishment_id    INTEGER NOT NULL REFERENCES establishments(id),
    modifier_id         INTEGER REFERENCES modifiers(id),
    modifier_name       VARCHAR(200),               -- denormalized from modifiers table for query convenience
    qty                 NUMERIC(8,2) DEFAULT 1,
    modifier_price      NUMERIC(10,2) DEFAULT 0,
    is_discounted       BOOLEAN DEFAULT FALSE,
    created_date        DATE NOT NULL,              -- date only (inherited from parent order)
    ingested_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_modifiers_order_item ON modifier_items (order_item_id);
CREATE INDEX idx_modifiers_establishment ON modifier_items (establishment_id, created_date);
CREATE INDEX idx_modifiers_modifier_id ON modifier_items (modifier_id, created_date);


-- ============================================================
-- LAYER 2 — FEATURE TABLES (Pre-aggregated for ML)
-- Computed nightly by aggregation job. TINY vs raw tables.
-- This is what the ML model reads — never the raw tables.
-- ============================================================

-- Hourly demand features — one row per location per hour per day
-- 24 hours × 11 locations × 365 days = 96,360 rows/year (tiny)
CREATE TABLE features_hourly (
    id                      BIGSERIAL PRIMARY KEY,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    date                    DATE NOT NULL,
    hour                    SMALLINT NOT NULL,              -- 0-23
    day_of_week             SMALLINT NOT NULL,              -- 0=Monday, 6=Sunday
    week_of_year            SMALLINT NOT NULL,
    month                   SMALLINT NOT NULL,
    is_weekend              BOOLEAN NOT NULL,

    -- Volume signals
    order_count             INTEGER DEFAULT 0,
    item_count              INTEGER DEFAULT 0,
    unique_customers        INTEGER DEFAULT 0,

    -- Revenue signals
    total_revenue           NUMERIC(12,2) DEFAULT 0,
    avg_order_value         NUMERIC(10,2) DEFAULT 0,
    avg_items_per_order     NUMERIC(6,2) DEFAULT 0,

    -- Channel breakdown
    orders_drive_through    INTEGER DEFAULT 0,
    orders_eat_in           INTEGER DEFAULT 0,
    orders_to_go            INTEGER DEFAULT 0,
    orders_doordash         INTEGER DEFAULT 0,
    orders_ubereats         INTEGER DEFAULT 0,
    orders_online           INTEGER DEFAULT 0,
    orders_lane_a           INTEGER DEFAULT 0,
    orders_lane_b           INTEGER DEFAULT 0,

    -- Operational signals
    avg_kitchen_seconds     NUMERIC(8,2),                  -- avg cook time this hour
    void_count              INTEGER DEFAULT 0,
    void_rate               NUMERIC(5,4) DEFAULT 0,        -- voided/total items

    -- Computed at
    computed_at             TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (establishment_id, date, hour)
);

CREATE INDEX idx_fh_est_date ON features_hourly (establishment_id, date, hour);
CREATE INDEX idx_fh_dow_hour ON features_hourly (day_of_week, hour, establishment_id);


-- Daily product features — one row per product per location per day
-- Drives the PREP SHEET prediction (Feature #1)
-- 120 products × 11 locations × 365 days = 481,800 rows/year
CREATE TABLE features_product_daily (
    id                      BIGSERIAL PRIMARY KEY,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    product_id              INTEGER NOT NULL REFERENCES products(id),
    product_name            VARCHAR(200),
    date                    DATE NOT NULL,
    day_of_week             SMALLINT NOT NULL,
    week_of_year            SMALLINT NOT NULL,
    month                   SMALLINT NOT NULL,
    is_weekend              BOOLEAN NOT NULL,

    -- Sales volume
    quantity_sold           NUMERIC(10,2) DEFAULT 0,
    order_count             INTEGER DEFAULT 0,             -- how many orders contained this item
    revenue                 NUMERIC(12,2) DEFAULT 0,

    -- Channel split for this product
    qty_drive_through       NUMERIC(10,2) DEFAULT 0,
    qty_eat_in              NUMERIC(10,2) DEFAULT 0,
    qty_third_party         NUMERIC(10,2) DEFAULT 0,

    -- Kitchen performance for this product this day
    avg_kitchen_seconds     NUMERIC(8,2),
    max_kitchen_seconds     INTEGER,
    min_kitchen_seconds     INTEGER,
    kitchen_outliers        INTEGER DEFAULT 0,             -- items > 3x avg kitchen time

    -- Combo context
    combo_attach_rate       NUMERIC(5,4),                  -- % of times sold as part of combo
    is_combo_item           BOOLEAN DEFAULT FALSE,

    -- Waste signals
    void_count              INTEGER DEFAULT 0,
    void_rate               NUMERIC(5,4) DEFAULT 0,

    computed_at             TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (establishment_id, product_id, date)
);

CREATE INDEX idx_fpd_est_date ON features_product_daily (establishment_id, date);
CREATE INDEX idx_fpd_product_date ON features_product_daily (product_id, date);
CREATE INDEX idx_fpd_dow ON features_product_daily (day_of_week, product_id, establishment_id);


-- Daily location summary — one row per location per day
-- Drives Location Health Score (Feature #7)
CREATE TABLE features_daily_summary (
    id                      BIGSERIAL PRIMARY KEY,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    date                    DATE NOT NULL,
    day_of_week             SMALLINT NOT NULL,
    week_of_year            SMALLINT NOT NULL,
    month                   SMALLINT NOT NULL,
    is_weekend              BOOLEAN NOT NULL,

    -- Volume
    total_orders            INTEGER DEFAULT 0,
    total_items             INTEGER DEFAULT 0,
    total_revenue           NUMERIC(12,2) DEFAULT 0,
    avg_order_value         NUMERIC(10,2) DEFAULT 0,
    avg_items_per_order     NUMERIC(6,2) DEFAULT 0,

    -- Channel mix
    pct_drive_through       NUMERIC(5,4) DEFAULT 0,
    pct_third_party         NUMERIC(5,4) DEFAULT 0,
    pct_in_store            NUMERIC(5,4) DEFAULT 0,
    revenue_drive_through   NUMERIC(12,2) DEFAULT 0,
    revenue_third_party     NUMERIC(12,2) DEFAULT 0,
    revenue_in_store        NUMERIC(12,2) DEFAULT 0,

    -- Kitchen performance
    avg_kitchen_seconds     NUMERIC(8,2),
    pct_orders_over_10min   NUMERIC(5,4) DEFAULT 0,        -- % of orders where any item > 10 min

    -- Waste & errors
    total_voids             INTEGER DEFAULT 0,
    void_rate               NUMERIC(5,4) DEFAULT 0,
    total_discounts         NUMERIC(12,2) DEFAULT 0,
    discount_rate           NUMERIC(5,4) DEFAULT 0,

    -- Peak hour
    peak_hour               SMALLINT,                      -- hour with most orders
    peak_hour_orders        INTEGER,

    computed_at             TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (establishment_id, date)
);

CREATE INDEX idx_fds_est_date ON features_daily_summary (establishment_id, date);
CREATE INDEX idx_fds_date ON features_daily_summary (date);


-- ============================================================
-- LAYER 3 — PREDICTION OUTPUT TABLES
-- What the ML model writes. What the dashboard reads.
-- ============================================================

-- Demand predictions — generated nightly for the next 7 days
CREATE TABLE predictions_hourly_demand (
    id                      BIGSERIAL PRIMARY KEY,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    target_date             DATE NOT NULL,              -- the day being predicted
    target_hour             SMALLINT NOT NULL,          -- 0-23
    predicted_orders        INTEGER,                    -- predicted transaction count
    predicted_revenue       NUMERIC(10,2),              -- predicted revenue
    confidence_low          INTEGER,                    -- lower bound (80% CI)
    confidence_high         INTEGER,                    -- upper bound (80% CI)
    model_version           VARCHAR(50),
    generated_at            TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (establishment_id, target_date, target_hour)
);

-- Prep sheet predictions — what to prep per item per shift
CREATE TABLE predictions_prep_sheet (
    id                      BIGSERIAL PRIMARY KEY,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    product_id              INTEGER NOT NULL REFERENCES products(id),
    product_name            VARCHAR(200),
    target_date             DATE NOT NULL,
    shift                   VARCHAR(20) NOT NULL,       -- 'morning', 'lunch', 'dinner', 'all_day'
    predicted_quantity      NUMERIC(10,2),              -- units to prep
    confidence_low          NUMERIC(10,2),
    confidence_high         NUMERIC(10,2),
    model_version           VARCHAR(50),
    generated_at            TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (establishment_id, product_id, target_date, shift)
);

-- Staffing recommendations
CREATE TABLE predictions_staffing (
    id                      BIGSERIAL PRIMARY KEY,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    target_date             DATE NOT NULL,
    shift                   VARCHAR(20) NOT NULL,       -- 'morning', 'lunch', 'dinner'
    shift_start_hour        SMALLINT,
    shift_end_hour          SMALLINT,
    recommended_staff       SMALLINT,                   -- headcount recommendation
    predicted_orders        INTEGER,                    -- basis for recommendation
    model_version           VARCHAR(50),
    generated_at            TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (establishment_id, target_date, shift)
);

-- Location health scores — daily composite score per location
CREATE TABLE location_health_scores (
    id                      BIGSERIAL PRIMARY KEY,
    establishment_id        INTEGER NOT NULL REFERENCES establishments(id),
    score_date              DATE NOT NULL,
    overall_score           NUMERIC(5,2),               -- 0-100
    revenue_score           NUMERIC(5,2),               -- vs forecast
    kitchen_score           NUMERIC(5,2),               -- speed vs baseline
    void_score              NUMERIC(5,2),               -- waste rate vs network avg
    volume_score            NUMERIC(5,2),               -- orders vs forecast
    network_rank            SMALLINT,                   -- 1 = best location today
    flags                   JSONB,                      -- {"slow_kitchen": true, "high_voids": false}
    generated_at            TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (establishment_id, score_date)
);

CREATE INDEX idx_health_date ON location_health_scores (score_date, overall_score);


-- ============================================================
-- LAYER 4 — OPERATIONAL / AUDIT TABLES
-- ============================================================

-- Track every ingestion run for debugging and monitoring
CREATE TABLE ingestion_log (
    id                  BIGSERIAL PRIMARY KEY,
    establishment_id    INTEGER REFERENCES establishments(id),
    run_date            DATE NOT NULL,              -- which day's data was fetched
    started_at          TIMESTAMPTZ NOT NULL,
    completed_at        TIMESTAMPTZ,
    status              VARCHAR(20),               -- 'running', 'success', 'failed', 'partial'
    orders_fetched      INTEGER DEFAULT 0,
    items_fetched       INTEGER DEFAULT 0,
    orders_inserted     INTEGER DEFAULT 0,
    items_inserted      INTEGER DEFAULT 0,
    orders_skipped      INTEGER DEFAULT 0,          -- duplicates
    error_message       TEXT,
    script_version      VARCHAR(50)
);

CREATE INDEX idx_ingestion_log_date ON ingestion_log (run_date, establishment_id);


-- ============================================================
-- DEDUPLICATION SAFETY
-- Daily runs may overlap (e.g. if script runs twice).
-- Use INSERT ... ON CONFLICT DO NOTHING everywhere.
-- ============================================================

-- Example safe insert pattern (use in Python ingestion script):
-- INSERT INTO orders (...) VALUES (...) ON CONFLICT (id, created_date) DO NOTHING;
-- INSERT INTO order_items (...) VALUES (...) ON CONFLICT (id, created_date) DO NOTHING;


-- ============================================================
-- USEFUL VIEWS FOR DASHBOARD QUERIES
-- ============================================================

-- Today's network overview (used by Location Health Score feature)
CREATE VIEW v_network_today AS
SELECT
    e.name AS location,
    s.total_orders,
    s.total_revenue,
    s.avg_order_value,
    s.void_rate,
    s.avg_kitchen_seconds,
    s.pct_third_party,
    s.overall_score
FROM features_daily_summary s
JOIN establishments e ON e.id = s.establishment_id
LEFT JOIN location_health_scores h
    ON h.establishment_id = s.establishment_id AND h.score_date = s.date
WHERE s.date = CURRENT_DATE - 1
ORDER BY h.overall_score DESC NULLS LAST;


-- Product velocity — last 30 days per location
CREATE VIEW v_product_velocity_30d AS
SELECT
    p.name AS product,
    f.establishment_id,
    e.name AS location,
    SUM(f.quantity_sold) AS total_qty,
    SUM(f.revenue) AS total_revenue,
    AVG(f.avg_kitchen_seconds) AS avg_kitchen_secs,
    AVG(f.void_rate) AS avg_void_rate,
    -- Week over week trend
    SUM(CASE WHEN f.date >= CURRENT_DATE - 7 THEN f.quantity_sold ELSE 0 END) AS qty_last_7d,
    SUM(CASE WHEN f.date BETWEEN CURRENT_DATE - 14 AND CURRENT_DATE - 8 THEN f.quantity_sold ELSE 0 END) AS qty_prev_7d
FROM features_product_daily f
JOIN products p ON p.id = f.product_id
JOIN establishments e ON e.id = f.establishment_id
WHERE f.date >= CURRENT_DATE - 30
GROUP BY p.name, f.establishment_id, e.name;


-- ============================================================
-- STORAGE ESTIMATES (from real data analysis)
-- ============================================================
-- orders table:         ~2.3M rows/year  ~460 MB/year with indexes
-- order_items table:    ~10.5M rows/year ~1.9 GB/year with indexes
-- modifier_items table: ~1.5M rows/year  ~120 MB/year
-- features_hourly:      ~96K rows/year   ~15 MB/year
-- features_product_daily: ~480K rows/year ~80 MB/year
-- features_daily_summary: ~4K rows/year  ~1 MB/year
-- prediction tables:    ~500K rows/year  ~50 MB/year
-- -------------------------------------------------------
-- TOTAL year 1:         ~3.5 GB (with indexes)
-- TOTAL year 3:         ~10.5 GB
-- Hardware needed:      $40-80/mo VPS with 50GB SSD
-- ============================================================
