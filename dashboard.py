"""
dashboard.py — Laynes Daily Prediction Dashboard
Streamlit app serving 15-min item forecasts for all 11 locations.
"""

import os
import re
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
import psycopg2.extras
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Laynes Predictions",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

:root {
  --brand:#6366F1; --brand-2:#8B5CF6; --ink:#1a1c23; --muted:#6b7280;
  --bg:#f6f7fb; --card:#ffffff; --line:#eceef3;
}

html, body, [class*="css"], .stApp, button, input, textarea, select {
  font-family:'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif !important;
}
.stApp { background:var(--bg); }
#MainMenu, footer, header [data-testid="stToolbar"] { visibility:hidden; }
.block-container { padding-top:1.4rem; padding-bottom:3rem; max-width:1500px; }

/* Hero header */
.hero {
  background:linear-gradient(120deg, var(--brand) 0%, var(--brand-2) 100%);
  border-radius:20px; padding:22px 28px; margin-bottom:18px; color:#fff;
  box-shadow:0 12px 30px -10px rgba(99,102,241,.5);
  display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px;
}
.hero h1 { font-size:1.7rem; font-weight:800; margin:0; line-height:1.15; letter-spacing:-.02em; }
.hero .sub { opacity:.92; font-size:.92rem; font-weight:500; margin-top:4px; }
.hero .badge {
  background:rgba(255,255,255,.18); border:1px solid rgba(255,255,255,.35);
  padding:8px 16px; border-radius:999px; font-weight:700; font-size:.95rem; white-space:nowrap;
}

/* KPI cards */
.kpi-row { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:6px; }
.kpi {
  flex:1; min-width:150px; background:var(--card); border:1px solid var(--line);
  border-radius:16px; padding:16px 18px; box-shadow:0 4px 14px -8px rgba(20,22,40,.18);
}
.kpi .label { color:var(--muted); font-size:.78rem; font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
.kpi .value { color:var(--ink); font-size:1.7rem; font-weight:800; line-height:1.1; margin-top:3px; }
.kpi .foot { font-size:.8rem; font-weight:600; margin-top:2px; }
.kpi.accent { border-top:3px solid var(--brand); }
.kpi { transition: transform .18s ease, box-shadow .18s ease; }
.kpi:hover { transform:translateY(-3px); box-shadow:0 10px 22px -10px rgba(99,102,241,.4); }

/* Tabs as pills */
.stTabs [data-baseweb="tab-list"] { gap:6px; flex-wrap:wrap; border-bottom:none; }
.stTabs [data-baseweb="tab"] {
  background:#fff; border:1px solid var(--line); border-radius:999px;
  padding:6px 16px; font-weight:600; font-size:.85rem; color:var(--muted);
}
.stTabs [aria-selected="true"] {
  background:var(--brand) !important; color:#fff !important; border-color:var(--brand) !important;
}
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display:none; }

/* Location picker (Tender Planning) — st.selectbox */
[data-baseweb="select"] > div {
  border-radius:12px; border-color:var(--line); box-shadow:0 2px 8px -6px rgba(20,22,40,.15);
  font-weight:600;
}
[data-baseweb="select"] > div:hover { border-color:var(--brand); }

/* Section titles */
.sec { font-weight:800; font-size:1.05rem; color:var(--ink); margin:14px 0 2px; letter-spacing:-.01em; }
.sec-sub { color:var(--muted); font-size:.86rem; margin-bottom:8px; }

/* Dataframe polish */
[data-testid="stDataFrame"] {
  border-radius:14px; overflow:hidden; border:1px solid var(--line);
  box-shadow:0 4px 14px -10px rgba(20,22,40,.2);
  animation: fadeInUp .35s ease;
}
@keyframes fadeInUp { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }

/* Sidebar */
[data-testid="stSidebar"] {
  background:linear-gradient(180deg,#0f172a 0%, #1e293b 100%);
  border-right:none;
}
[data-testid="stSidebar"] * { color:#e9eaf0; }
[data-testid="stSidebar"] .block-container { padding-top:1.2rem; }

.sb-brand { display:flex; align-items:center; gap:12px; margin-bottom:4px; }
.sb-logo {
  width:46px; height:46px; border-radius:14px; flex:0 0 auto;
  background:linear-gradient(135deg, var(--brand), var(--brand-2));
  display:flex; align-items:center; justify-content:center; font-size:1.5rem;
  box-shadow:0 6px 16px -6px rgba(99,102,241,.7);
}
.sb-name { font-weight:800; font-size:1.05rem; line-height:1.1; color:#fff; }
.sb-tag { font-size:.74rem; color:#9aa0b3; font-weight:500; }
.sb-div { height:1px; background:rgba(255,255,255,.08); margin:16px 0; }
.sb-nav-item {
  display:flex; align-items:center; gap:11px; padding:11px 13px; border-radius:12px;
  font-weight:600; font-size:.92rem; margin-bottom:6px;
  background:linear-gradient(135deg, rgba(99,102,241,.95), rgba(139,92,246,.95));
  box-shadow:0 6px 16px -8px rgba(99,102,241,.7); color:#fff;
}
.sb-nav-item.ghost { background:transparent; box-shadow:none; color:#aeb3c6; }
.sb-label { font-size:.72rem; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
  color:#7c8197; margin:4px 0 6px; }
.sb-foot { font-size:.76rem; color:#8b90a3; line-height:1.5; }
.sb-foot b { color:#c9cdda; }

/* Modern loading overlay */
.loadwrap { display:flex; justify-content:center; padding:54px 0 44px; }
.loadcard {
  background:var(--card); border:1px solid var(--line); border-radius:22px;
  padding:34px 46px; text-align:center; min-width:340px;
  box-shadow:0 16px 40px -18px rgba(99,102,241,.35);
}
.spin-ring {
  width:46px; height:46px; border-radius:50%; margin:0 auto 18px;
  border:4px solid #eef0fe; border-top-color:var(--brand);
  border-right-color:var(--brand-2);
  animation: dspin .8s cubic-bezier(.5,0,.5,1) infinite;
}
@keyframes dspin { to { transform:rotate(360deg); } }
.load-title { font-weight:800; font-size:1.05rem; color:var(--ink); letter-spacing:-.01em; }
.load-sub { font-size:.82rem; color:var(--muted); margin-top:3px; margin-bottom:20px; }
.skel-row { height:10px; border-radius:6px; background:#eef0f4; margin:9px auto; overflow:hidden; }
.skel-bar {
  height:100%; border-radius:6px; width:100%;
  background:linear-gradient(90deg, #eef0f4 25%, #dcdffa 50%, #eef0f4 75%);
  background-size:300% 100%; animation:shimmer 1.4s ease-in-out infinite;
}
@keyframes shimmer { 0% { background-position:200% 0; } 100% { background-position:-200% 0; } }

/* Sidebar date input → dark field */
[data-testid="stSidebar"] [data-baseweb="input"],
[data-testid="stSidebar"] input {
  background:rgba(255,255,255,.06) !important; border-radius:10px !important;
  border:1px solid rgba(255,255,255,.12) !important; color:#fff !important;
}
[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p { color:#9aa0b3 !important; font-weight:600; }

/* Alerts rounded */
[data-testid="stAlert"] { border-radius:14px; }

/* Panel wrapper */
.panel { background:var(--card); border:1px solid var(--line); border-radius:18px;
  padding:16px 18px; box-shadow:0 6px 20px -12px rgba(20,22,40,.22); margin-bottom:14px; }

/* Custom data table */
.tw { max-height:560px; overflow:auto; border-radius:14px; border:1px solid var(--line); }
.tw::-webkit-scrollbar { width:8px; height:8px; }
.tw::-webkit-scrollbar-thumb { background:#d7dae2; border-radius:8px; }
table.dt { width:100%; border-collapse:separate; border-spacing:0; font-size:.85rem; }
table.dt th { position:sticky; top:0; z-index:1; background:#fbfbfd; color:var(--muted);
  font-weight:700; font-size:.7rem; text-transform:uppercase; letter-spacing:.05em;
  text-align:left; padding:11px 14px; border-bottom:1px solid var(--line); }
table.dt th.num, table.dt td.num { text-align:right; }
table.dt td { padding:9px 14px; border-bottom:1px solid #f3f4f8; color:var(--ink); font-weight:600; }
table.dt tr:last-child td { border-bottom:none; }
table.dt tbody tr:hover td { background:#fafaff; }
.bnum { font-variant-numeric:tabular-nums; }
.track { display:inline-block; width:90px; height:8px; background:#eef0f4;
  border-radius:6px; overflow:hidden; vertical-align:middle; margin-left:9px; }
.track > i { display:block; height:100%; border-radius:6px; }
.fp { background:linear-gradient(90deg,#c4b5fd,#6366F1); }
.fa { background:linear-gradient(90deg,#99f6e4,#14b8a6); }
.pill { padding:3px 11px; border-radius:999px; font-weight:700; font-size:.76rem;
  display:inline-block; min-width:64px; text-align:center; }
.muted { color:#aeb3c0; font-weight:600; }

/* KPI icon */
.kpi .top { display:flex; align-items:center; gap:8px; }
.kpi .ic { width:30px; height:30px; border-radius:9px; display:flex; align-items:center;
  justify-content:center; font-size:1rem; background:#eef0fe; }
</style>
""", unsafe_allow_html=True)

# ── DB helpers ────────────────────────────────────────────────────────────────

# Some tender/wrap/sandwich product names carry no spicy/regular signal at all
# (older POS naming, e.g. "3 Finger Meal*", "Chicken Finger A la Carte",
# "Chicken Wrap*") — the flavor is only recorded as a linked modifier. This
# LATERAL pulls the relevant modifier text (if any) per order_item so Python
# can resolve flavor the same way it resolves it from the product name.
# Restricted to an exact-match whitelist (not a loose ILIKE '%regular%') so
# unrelated modifiers like "Regular Coke"/"Regular Lemonade" don't get pulled in.
_FLAVOR_MOD_JOIN = r"""
    LEFT JOIN LATERAL (
        SELECT string_agg(mi.modifier_name, ' | ') AS flavor_mods
        FROM modifier_items mi
        WHERE mi.order_item_id = oi.id
          AND (
            lower(btrim(mi.modifier_name)) IN (
                'spicy', 'regular', 'crispy', 'extra crispy',
                'chicken spicy', 'chicken crispy', 'chicken grilled', 'chicken griiled',
                'chicken spicy crispy', 'wrap spicy', 'wrap crispy',
                'kick it up spicy', 'kick it up regular'
            )
            OR mi.modifier_name ~* '^\*+\s*\d+\s*(spicy|regular)'
          )
    ) mf ON true
"""

def _connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "laynes"),
        user=os.getenv("DB_USER", "laynes_user"),
        password=os.getenv("DB_PASS"),
    )

@st.cache_data(ttl=60)
def load_locations():
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id, name FROM establishments ORDER BY name")
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@st.cache_data(ttl=60)
def load_prediction_dates():
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT target_date FROM predictions_15min
            ORDER BY target_date DESC LIMIT 60
        """)
        rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows

@st.cache_data(ttl=120)
def load_predictions(target_date, establishment_id):
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT slot_index, product_id, product_name,
                   predicted_quantity, confidence_low, confidence_high
            FROM predictions_15min
            WHERE target_date = %s AND establishment_id = %s
            ORDER BY slot_index, predicted_quantity DESC
        """, (target_date, establishment_id))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@st.cache_data(ttl=120)
def load_accuracy_metrics(target_date, establishment_id):
    """Slot-level join of predictions vs actuals for accuracy report."""
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT
                p.product_id,
                p.product_name,
                p.slot_index,
                p.predicted_quantity,
                COALESCE(a.actual_qty, 0) AS actual_qty
            FROM predictions_15min p
            LEFT JOIN (
                SELECT
                    oi.product_id,
                    (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
                     + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15)
                    )::SMALLINT AS slot_index,
                    SUM(oi.quantity)::float AS actual_qty
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                    AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
                    AND o.closed = TRUE AND o.deleted = FALSE
                WHERE DATE(oi.created_date AT TIME ZONE 'America/Chicago') = %s
                  AND oi.establishment_id = %s
                  AND oi.deleted = FALSE AND oi.is_voided = FALSE
                  AND oi.product_id IS NOT NULL
                GROUP BY oi.product_id, slot_index
            ) a ON a.product_id = p.product_id AND a.slot_index = p.slot_index
            WHERE p.target_date = %s AND p.establishment_id = %s
            ORDER BY p.slot_index, p.predicted_quantity DESC
        """, (target_date, establishment_id, target_date, establishment_id))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@st.cache_data(ttl=120)
def load_actuals(target_date, establishment_id):
    """Load actual sales for a past date (for comparison)."""
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(f"""
            SELECT
                oi.product_id,
                MAX(oi.product_name) AS product_name,
                mf.flavor_mods,
                (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
                 + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15)
                )::SMALLINT AS slot_index,
                SUM(oi.quantity)::float AS actual_qty
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
                AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
                AND o.closed = TRUE AND o.deleted = FALSE
            {_FLAVOR_MOD_JOIN}
            WHERE DATE(oi.created_date AT TIME ZONE 'America/Chicago') = %s
              AND oi.establishment_id = %s
              AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
            GROUP BY oi.product_id, mf.flavor_mods, slot_index
        """, (target_date, establishment_id))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@st.cache_data(ttl=600)
def load_dow_history(establishment_id, pg_dow):
    """Per-(date, 15-min slot, product) actual sales for the SAME weekday, across
    all history (not bounded by a target date — see below).

    Used to build the per-15-min prediction straight from past same-weekday demand,
    which for these low-volume, high-variance slots is more reliable than the ML model.
    slot_index = hour*4 + minute//15 (0–95). pg_dow uses Postgres DOW (0=Sunday … 6=Saturday).

    Deliberately NOT parameterized by a "before this date" cutoff: that would make the
    cache key include the selected date, so every date change (even within the same
    weekday) would force a fresh multi-second DB round trip. Instead this fetches the
    full same-weekday history once per (location, weekday) and callers filter by date
    in pandas — cheap in-memory work — so browsing across dates of the same weekday
    reuses the cached pull instead of re-querying.
    """
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(f"""
            SELECT
                DATE(oi.created_date AT TIME ZONE 'America/Chicago')          AS sale_date,
                (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
                 + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15)
                )::int                                                        AS slot_index,
                oi.product_name,
                mf.flavor_mods,
                SUM(oi.quantity)::float                                       AS qty
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
                AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
                AND o.closed = TRUE AND o.deleted = FALSE
            {_FLAVOR_MOD_JOIN}
            WHERE oi.establishment_id = %s
              AND EXTRACT(DOW FROM oi.created_date AT TIME ZONE 'America/Chicago')::int = %s
              AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
            GROUP BY sale_date, slot_index, oi.product_name, mf.flavor_mods
        """, (establishment_id, pg_dow))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@st.cache_data(ttl=600)
def load_order_date_range():
    """Earliest and latest dates with order data (Central Time)."""
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT MIN(DATE(created_date AT TIME ZONE 'America/Chicago')),
                   MAX(DATE(created_date AT TIME ZONE 'America/Chicago'))
            FROM orders
            WHERE closed = TRUE AND deleted = FALSE
        """)
        lo, hi = cur.fetchone()
    conn.close()
    return lo, hi

@st.cache_data(ttl=600)
def load_recent_daily(establishment_id):
    """Per-(date, weekday, product) actual sales across all history for a location.
    Used to build the 'Last N days' predicted-vs-actual scorecard: 'predicted' for
    each day is the median of same-weekday daily totals (a typical day).

    Deliberately not parameterized by end_date/weeks (unlike the old version) — same
    reasoning as load_dow_history: keeping the cache key to just establishment_id means
    switching the selected date reuses this pull instead of re-querying every time.
    Callers slice the rolling window in pandas.
    """
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(f"""
            SELECT
                DATE(oi.created_date AT TIME ZONE 'America/Chicago')          AS sale_date,
                EXTRACT(DOW FROM oi.created_date AT TIME ZONE 'America/Chicago')::int AS dow,
                oi.product_name,
                mf.flavor_mods,
                SUM(oi.quantity)::float                                       AS qty
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
                AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
                AND o.closed = TRUE AND o.deleted = FALSE
            {_FLAVOR_MOD_JOIN}
            WHERE oi.establishment_id = %s
              AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
            GROUP BY sale_date, dow, oi.product_name, mf.flavor_mods
        """, (establishment_id,))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@st.cache_data(ttl=120)
def load_cross_location_summary(report_date):
    """Slot-level predicted vs actual for ALL locations on one date."""
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            WITH slot_actual AS (
                SELECT
                    oi.establishment_id, oi.product_id,
                    (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
                     + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15)
                    )::SMALLINT AS slot_index,
                    SUM(oi.quantity)::float AS actual_qty
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
                AND o.closed = TRUE AND o.deleted = FALSE
                WHERE DATE(oi.created_date AT TIME ZONE 'America/Chicago') = %s
                  AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
                GROUP BY oi.establishment_id, oi.product_id, slot_index
            )
            SELECT
                e.name                                                          AS location,
                p.establishment_id,
                ROUND(SUM(p.predicted_quantity)::numeric, 1)                   AS predicted,
                ROUND(SUM(COALESCE(sa.actual_qty, 0))::numeric, 1)             AS actual,
                ROUND(AVG(ABS(p.predicted_quantity - COALESCE(sa.actual_qty, 0)))::numeric, 3) AS mae,
                COUNT(*)                                                        AS slots,
                COUNT(*) FILTER (WHERE ABS(p.predicted_quantity - COALESCE(sa.actual_qty,0)) <= 1) AS within_1,
                COUNT(*) FILTER (WHERE ABS(p.predicted_quantity - COALESCE(sa.actual_qty,0)) <= 2) AS within_2
            FROM predictions_15min p
            JOIN establishments e ON e.id = p.establishment_id
            LEFT JOIN slot_actual sa ON sa.establishment_id = p.establishment_id
                AND sa.product_id = p.product_id AND sa.slot_index = p.slot_index
            WHERE p.target_date = %s
            GROUP BY e.name, p.establishment_id
            ORDER BY e.name
        """, (report_date, report_date))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@st.cache_data(ttl=300)
def load_accuracy_trend(n_dates=30):
    """Daily predicted vs actual totals per location for the last N past dates."""
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            WITH dates AS (
                SELECT DISTINCT target_date FROM predictions_15min
                WHERE target_date < CURRENT_DATE
                ORDER BY target_date DESC LIMIT %s
            ),
            slot_actual AS (
                SELECT
                    DATE(oi.created_date AT TIME ZONE 'America/Chicago') AS sale_date,
                    oi.establishment_id, oi.product_id,
                    (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
                     + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15)
                    )::SMALLINT AS slot_index,
                    SUM(oi.quantity)::float AS actual_qty
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                AND o.created_date BETWEEN oi.created_date - INTERVAL '1 day' AND oi.created_date + INTERVAL '1 day'
                AND o.closed = TRUE AND o.deleted = FALSE
                WHERE DATE(oi.created_date AT TIME ZONE 'America/Chicago') IN (SELECT target_date FROM dates)
                  AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
                GROUP BY sale_date, oi.establishment_id, oi.product_id, slot_index
            )
            SELECT
                p.target_date,
                e.name                                             AS location,
                ROUND(SUM(p.predicted_quantity)::numeric, 1)      AS predicted,
                ROUND(SUM(COALESCE(sa.actual_qty, 0))::numeric, 1) AS actual
            FROM predictions_15min p
            JOIN establishments e ON e.id = p.establishment_id
            LEFT JOIN slot_actual sa ON sa.establishment_id = p.establishment_id
                AND sa.product_id = p.product_id AND sa.slot_index = p.slot_index
                AND sa.sale_date = p.target_date
            WHERE p.target_date IN (SELECT target_date FROM dates)
            GROUP BY p.target_date, e.name
            ORDER BY p.target_date, e.name
        """, (n_dates,))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

# ── Helpers ───────────────────────────────────────────────────────────────────

CHART_COLORS = (
    px.colors.qualitative.Set2
    + px.colors.qualitative.Pastel1
    + px.colors.qualitative.Set3
)

SHIFTS = [
    ("🌅 Morning",   16, 44),   # 04:00–11:00  (locations open 4–5 AM)
    ("🍔 Lunch",     44, 56),   # 11:00–14:00
    ("☀️ Afternoon", 56, 68),   # 14:00–17:00
    ("🌆 Dinner",    68, 96),   # 17:00–midnight (Shepherd open till 23:00)
]

def slot_label(idx: int) -> str:
    h, frac = divmod(idx, 4)
    return f"{h:02d}:{frac * 15:02d}"

def slot_range_label(idx: int) -> str:
    """15-min window label, e.g. 28 → '07:00–07:15'."""
    return f"{slot_label(idx)}–{slot_label(idx + 1)}"

def gap_label(gp) -> str:
    """Colored-dot + signed percent, e.g. '🟢 +5%'. Used by predicted-vs-actual tables."""
    if pd.isna(gp):
        return "—"
    dot = "🟢" if abs(gp) <= 12 else ("🟡" if abs(gp) <= 25 else "🔴")
    return f"{dot} {gp:+.0f}%"

def zebra_style(df: pd.DataFrame):
    """Light indigo tint on alternating rows for a pandas Styler passed to st.dataframe."""
    return df.style.apply(
        lambda row: ["background-color:#eef0fa" if row.name % 2 else "" for _ in row],
        axis=1,
    )

def prep_df(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["predicted_quantity"] = df["predicted_quantity"].astype(float)
    df["slot_label"] = df["slot_index"].apply(slot_label)
    return df

def top_n_products(df: pd.DataFrame, n: int = 8) -> list:
    return (
        df.groupby("product_name")["predicted_quantity"]
        .sum()
        .sort_values(ascending=False)
        .head(n)
        .index.tolist()
    )

def build_chart(df: pd.DataFrame, top8: list, title: str, df_actual=None) -> go.Figure:
    df2 = df.copy()
    df2["group"] = df2["product_name"].where(df2["product_name"].isin(top8), "Other")
    pivot = (
        df2.groupby(["slot_label", "group"])["predicted_quantity"]
        .sum()
        .unstack(fill_value=0)
    )
    col_order = [c for c in top8 if c in pivot.columns] + (
        ["Other"] if "Other" in pivot.columns else []
    )
    pivot = pivot.reindex(columns=col_order)

    fig = go.Figure()
    for i, col in enumerate(pivot.columns):
        fig.add_trace(go.Bar(
            name=col,
            x=pivot.index,
            y=pivot[col],
            marker_color=CHART_COLORS[i % len(CHART_COLORS)],
            hovertemplate="%{y:.1f} units<extra>%{fullData.name}</extra>",
        ))

    # Overlay actual total line if actuals available
    if df_actual is not None and not df_actual.empty:
        act_by_slot = (
            df_actual.groupby("slot_label")["actual_qty"].sum().reindex(pivot.index, fill_value=0)
        )
        fig.add_trace(go.Scatter(
            name="Actual",
            x=act_by_slot.index,
            y=act_by_slot.values,
            mode="lines+markers",
            line=dict(color="black", width=2, dash="dot"),
            marker=dict(size=4),
            hovertemplate="%{y:.0f} actual<extra>Actual</extra>",
        ))

    fig.update_layout(
        barmode="stack",
        title=dict(text=title, font=dict(size=14)),
        xaxis_title="Time (CT)",
        yaxis_title="Predicted qty",
        height=380,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0,
                    font=dict(size=11)),
        margin=dict(l=0, r=0, t=55, b=30),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    # Show every-hour tick
    hour_ticks = [s for s in pivot.index if s.endswith(":00")]
    fig.update_xaxes(
        tickangle=-45,
        tickmode="array",
        tickvals=hour_ticks,
        ticktext=hour_ticks,
        showgrid=True,
        gridcolor="#eee",
    )
    fig.update_yaxes(showgrid=True, gridcolor="#eee")
    return fig

# ── Tender planning helpers ───────────────────────────────────────────────────

def build_pred_actual_chart(slotly, has_actual):
    """Modern area(predicted) + line(actual) chart across the day."""
    x = slotly["slot_index"].apply(slot_label).tolist()
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        name="Predicted", x=x, y=slotly["predicted"], mode="lines",
        line=dict(color="#6366F1", width=2.5, shape="spline", smoothing=0.6),
        fill="tozeroy", fillcolor="rgba(99,102,241,0.12)",
        hovertemplate="%{x}<br>Predicted %{y:.0f}<extra></extra>",
    ))
    if has_actual:
        fig.add_trace(go.Scatter(
            name="Actual", x=x, y=slotly["actual"], mode="lines",
            line=dict(color="#14b8a6", width=2.5, shape="spline", smoothing=0.6),
            hovertemplate="%{x}<br>Actual %{y:.0f}<extra></extra>",
        ))
    hour_ticks = [s for s in x if s.endswith(":00")]
    fig.update_layout(
        height=300, margin=dict(l=8, r=8, t=10, b=8),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Plus Jakarta Sans", size=12, color="#6b7280"),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1,
                    font=dict(size=12)),
    )
    fig.update_xaxes(tickmode="array", tickvals=hour_ticks, ticktext=hour_ticks,
                     showgrid=False, tickangle=-45, linecolor="#e5e7eb")
    fig.update_yaxes(showgrid=True, gridcolor="#f0f1f5", zeroline=False, rangemode="tozero")
    return fig


@st.dialog("Slot history", width="large")
def show_slot_history_dialog(loc_name: str, dow_name: str, time_label: str, hist_df: pd.DataFrame):
    """All past same-weekday values (with dates) for one 15-min slot."""
    st.markdown(f"**{loc_name} · every past {dow_name} · {time_label}**")
    disp = hist_df.sort_values("sale_date", ascending=False).copy()
    disp["Date"] = pd.to_datetime(disp["sale_date"]).dt.strftime("%a, %b %d, %Y")
    disp = disp.rename(columns={"tenders": "Tenders sold"})[["Date", "Tenders sold"]]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Records", f"{len(disp)}")
    c2.metric("Average", f"{hist_df['tenders'].mean():.0f}")
    c3.metric("Median", f"{hist_df['tenders'].median():.0f}")
    c4.metric("Max", f"{hist_df['tenders'].max():.0f}")

    st.dataframe(
        disp, hide_index=True, width="stretch", height=min(430, 42 + 35 * len(disp)),
        column_config={"Tenders sold": st.column_config.NumberColumn(format="%.0f")},
    )

def _norm_pname(name: str) -> str:
    s = re.sub(r'[\*\$]+', '', name).strip().lower()
    return re.sub(r'\s+', ' ', s).strip()

def get_finger_count(product_name: str) -> int:
    n = _norm_pname(product_name)
    # "10 Finger Party Pack" → 10, "25 Finger Party Pack" → 25, etc.
    m = re.match(r'^(\d+)\s+finger\s+party\s+pack', n)
    if m:
        return int(m.group(1))
    # "Party Pack 10" → 10
    m = re.match(r'^party\s+pack\s+(\d+)', n)
    if m:
        return int(m.group(1))
    # Family packs → 20
    if 'family' in n:
        return 20
    # Kids finger meals → 2
    if 'kid' in n and ('finger' in n or 'meal' in n):
        return 2
    # Grilled cheese → 0 (no chicken)
    if 'grilled cheese' in n:
        return 0
    # Wraps → 2
    if 'wrap' in n:
        return 2
    # Club sandwich → 3
    if 'club' in n:
        return 3
    # Sandwich meal / chicken finger sandwich → 3
    if 'sandwich' in n:
        return 3
    # Standard finger meals: 3, 4, 5
    for cnt in (3, 4, 5):
        if f'{cnt} finger' in n:
            return cnt
    # Mixed combos: "2 regular 1 spicy" → 3, "3 regular 2 spicy" → 5, etc.
    nums = re.findall(r'(\d+)\s+(?:regular|spicy)', n)
    if nums:
        return sum(int(x) for x in nums)
    # Individual a la carte finger
    if 'chicken finger' in n:
        return 1
    return 0

_FLAVOR_RE = r'regular|crispy|grilled|griiled'

def get_finger_split(product_name: str, flavor_mods: str | None = None) -> tuple[float, float, float]:
    """Split one unit's fingers into (regular, spicy, unspecified).

    Resolution order: (1) numeric combo already in the product name, e.g.
    "2 Regular 1 Spicy"; (2) an explicit spicy/regular word in the name;
    (3) if the name carries no flavor signal at all (older POS items like
    "3 Finger Meal*", "Chicken Finger A la Carte", "Chicken Wrap*"), fall
    back to `flavor_mods` — the linked modifier text pulled by
    `_FLAVOR_MOD_JOIN`. Anything still unresolved goes to "unspecified"
    rather than being guessed into a bucket.
    """
    fc = get_finger_count(product_name)
    if fc == 0:
        return (0.0, 0.0, 0.0)

    n = _norm_pname(product_name)
    combo = re.findall(r'(\d+)\s+(regular|spicy)', n)
    if combo:
        reg = sum(int(x) for x, f in combo if f == 'regular')
        spi = sum(int(x) for x, f in combo if f == 'spicy')
        return (float(reg), float(spi), 0.0)

    has_spicy = 'spicy' in n
    has_regular = bool(re.search(_FLAVOR_RE, n))
    if has_spicy and not has_regular:
        return (0.0, float(fc), 0.0)
    if has_regular and not has_spicy:
        return (float(fc), 0.0, 0.0)

    if isinstance(flavor_mods, str) and flavor_mods:
        # "Chicken Spicy Crispy" = spicy heat + fried (not grilled) style —
        # the "crispy" there is a cooking method, not a regular/non-spicy cue.
        m = _norm_pname(flavor_mods).replace('spicy crispy', 'spicy')
        mcombo = re.findall(r'(\d+)\s+(regular|spicy)', m)
        if mcombo:
            reg = sum(int(x) for x, f in mcombo if f == 'regular')
            spi = sum(int(x) for x, f in mcombo if f == 'spicy')
            tot = reg + spi
            if tot > 0:
                return (fc * reg / tot, fc * spi / tot, 0.0)
        has_spicy_m = 'spicy' in m
        has_regular_m = bool(re.search(_FLAVOR_RE, m))
        if has_spicy_m and not has_regular_m:
            return (0.0, float(fc), 0.0)
        if has_regular_m and not has_spicy_m:
            return (float(fc), 0.0, 0.0)
        if has_spicy_m and has_regular_m:
            # Both flavor tags present with no explicit ratio (e.g. a mixed
            # party pack) — no way to know the true split, so assume even.
            return (fc / 2, fc / 2, 0.0)

    return (0.0, 0.0, float(fc))

def _tender_cat(product_name: str) -> str:
    n = _norm_pname(product_name)
    fc = get_finger_count(product_name)
    if fc == 0:
        return "Non-Finger"
    if 'family' in n or re.match(r'^\d+\s+finger\s+party\s+pack', n) or 'party pack' in n:
        return "Packs"
    if 'kid' in n:
        return "Kids"
    if 'wrap' in n:
        return "Wraps"
    if 'club' in n or ('sandwich' in n and 'grilled cheese' not in n):
        return "Sandwiches"
    if fc in (3, 4, 5):
        return f"{fc}-Finger"
    if fc == 1:
        return "Individual"
    return "Other"

TENDER_CAT_COLORS = {
    "3-Finger":   "#4C9BE8",
    "4-Finger":   "#F4A261",
    "5-Finger":   "#E76F51",
    "Wraps":      "#2A9D8F",
    "Sandwiches": "#8338EC",
    "Kids":       "#FFB703",
    "Packs":      "#023047",
    "Individual": "#A8DADC",
    "Other":      "#BDBDBD",
}

def build_tender_chart(df: pd.DataFrame, title: str) -> go.Figure:
    df2 = df.copy()
    df2["category"] = df2["product_name"].apply(_tender_cat)
    df2 = df2[df2["category"] != "Non-Finger"]
    if df2.empty:
        return go.Figure()

    pivot = (
        df2.groupby(["slot_label", "category"])["tenders"]
        .sum()
        .unstack(fill_value=0)
    )
    cat_order = [c for c in TENDER_CAT_COLORS if c in pivot.columns]
    pivot = pivot.reindex(columns=cat_order)

    fig = go.Figure()
    for cat in cat_order:
        if cat not in pivot.columns:
            continue
        fig.add_trace(go.Bar(
            name=cat,
            x=pivot.index,
            y=pivot[cat],
            marker_color=TENDER_CAT_COLORS[cat],
            hovertemplate="%{y:.0f} tenders<extra>%{fullData.name}</extra>",
        ))

    totals = pivot.sum(axis=1)
    fig.add_trace(go.Scatter(
        name="Total",
        x=totals.index,
        y=totals.values,
        mode="lines+markers",
        line=dict(color="black", width=2),
        marker=dict(size=4),
        hovertemplate="%{y:.0f} total<extra>Total</extra>",
    ))

    fig.update_layout(
        barmode="stack",
        title=dict(text=title, font=dict(size=14)),
        xaxis_title="Time (CT)",
        yaxis_title="Tenders needed",
        height=380,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0,
                    font=dict(size=11)),
        margin=dict(l=0, r=0, t=55, b=30),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    hour_ticks = [s for s in pivot.index if s.endswith(":00")]
    fig.update_xaxes(
        tickangle=-45, tickmode="array",
        tickvals=hour_ticks, ticktext=hour_ticks,
        showgrid=True, gridcolor="#eee",
    )
    fig.update_yaxes(showgrid=True, gridcolor="#eee")
    return fig

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(
        '<div class="sb-brand"><div class="sb-logo">🍗</div>'
        '<div><div class="sb-name">Laynes Chicken</div>'
        '<div class="sb-tag">Tender Planning Dashboard</div></div></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="sb-div"></div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sb-nav-item">🍗 Tender Planning</div>',
        unsafe_allow_html=True,
    )

    page = "🍗 Tender Planning"

    _dmin, _dmax = load_order_date_range()
    if _dmin is None:
        st.error("No order data found.")
        st.stop()
    _today = date.today()
    st.markdown('<div class="sb-label">📅 Choose a day</div>', unsafe_allow_html=True)
    # Predictions are history-based, so any date works — allow today + planning ahead.
    # Actuals only exist up to the last ingested order date (_dmax).
    selected_date = st.date_input(
        "Date", value=_today, min_value=_dmin,
        max_value=_today + timedelta(days=14),
        format="YYYY-MM-DD", label_visibility="collapsed",
    )
    if selected_date > _dmax:
        st.markdown(
            f'<div class="sb-foot">ℹ️ <b>Predicted only</b> — actuals available through '
            f'{_dmax.strftime("%b %d")}.</div>',
            unsafe_allow_html=True,
        )

    st.markdown('<div class="sb-div"></div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sb-foot">🕐 All times in <b>Central Time</b> (Houston, TX)<br><br>'
        'Predictions built from past same-weekday order history, per location.</div>',
        unsafe_allow_html=True,
    )

# ── Accuracy Report page ──────────────────────────────────────────────────────

def _color_gap(val):
    if pd.isna(val):
        return "color: #aaa"
    return "color: #c0392b" if abs(val) > 20 else ("color: #e67e22" if abs(val) > 10 else "color: #27ae60")

def _bg_gap(val):
    if pd.isna(val):
        return ""
    if abs(val) > 20:
        return "background-color: #fdecea"
    if abs(val) > 10:
        return "background-color: #fff3e0"
    return "background-color: #e8f5e9"

def render_accuracy_tab(loc_name: str, loc_id: int, report_date, tab_key: str):
    acc_rows = load_accuracy_metrics(report_date, loc_id)
    if not acc_rows:
        st.warning(f"No prediction data for **{loc_name}** on {report_date}.")
        return

    df_acc = pd.DataFrame(acc_rows)
    df_acc["predicted_quantity"] = df_acc["predicted_quantity"].astype(float)
    df_acc["actual_qty"]         = df_acc["actual_qty"].astype(float)
    df_acc["abs_err"]            = (df_acc["predicted_quantity"] - df_acc["actual_qty"]).abs()
    df_acc["slot_label"]         = df_acc["slot_index"].apply(slot_label)
    df_acc = df_acc[(df_acc["slot_index"] >= 16) & (df_acc["slot_index"] < 96)]

    total_pred   = df_acc["predicted_quantity"].sum()
    total_actual = df_acc["actual_qty"].sum()
    mae          = df_acc["abs_err"].mean()
    n            = len(df_acc)
    within_1     = (df_acc["abs_err"] <= 1).sum() / n * 100 if n else 0
    within_2     = (df_acc["abs_err"] <= 2).sum() / n * 100 if n else 0
    diff_total   = total_pred - total_actual
    pct_diff     = diff_total / total_actual * 100 if total_actual else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Predicted Total",  f"{total_pred:,.0f}")
    c2.metric("Actual Total",     f"{total_actual:,.0f}",
              f"{diff_total:+.0f} ({pct_diff:+.1f}%)")
    c3.metric("MAE / slot",       f"{mae:.3f} units")
    c4.metric("Within ±1 unit",   f"{within_1:.1f}%")
    c5.metric("Within ±2 units",  f"{within_2:.1f}%")

    st.markdown("---")

    # ── Comparison chart ──────────────────────────────────────────────────────
    slot_pred = df_acc.groupby("slot_label")["predicted_quantity"].sum()
    slot_act  = df_acc.groupby("slot_label")["actual_qty"].sum()
    all_slots = sorted(set(slot_pred.index) | set(slot_act.index),
                       key=lambda s: int(s.split(":")[0]) * 60 + int(s.split(":")[1]))
    slot_pred = slot_pred.reindex(all_slots, fill_value=0)
    slot_act  = slot_act.reindex(all_slots, fill_value=0)

    fig_cmp = go.Figure()
    fig_cmp.add_trace(go.Bar(
        name="Predicted", x=all_slots, y=slot_pred.values,
        marker_color="#4C9BE8",
        hovertemplate="%{y:.1f} predicted<extra></extra>",
    ))
    fig_cmp.add_trace(go.Scatter(
        name="Actual", x=all_slots, y=slot_act.values,
        mode="lines+markers",
        line=dict(color="black", width=2, dash="dot"),
        marker=dict(size=4),
        hovertemplate="%{y:.0f} actual<extra>Actual</extra>",
    ))
    hour_ticks_acc = [s for s in all_slots if s.endswith(":00")]
    fig_cmp.update_layout(
        barmode="group",
        title=dict(text=f"{loc_name} · Predicted vs Actual (15-min slots)", font=dict(size=14)),
        xaxis_title="Time (CT)", yaxis_title="Quantity", height=380,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0, font=dict(size=11)),
        margin=dict(l=0, r=0, t=55, b=30),
        plot_bgcolor="white", paper_bgcolor="white",
    )
    fig_cmp.update_xaxes(tickangle=-45, tickmode="array",
                         tickvals=hour_ticks_acc, ticktext=hour_ticks_acc,
                         showgrid=True, gridcolor="#eee")
    fig_cmp.update_yaxes(showgrid=True, gridcolor="#eee")
    st.plotly_chart(fig_cmp, width='stretch', key=f"acc_chart_{tab_key}")

    st.markdown("---")

    # ── Item-level summary table ───────────────────────────────────────────────
    st.markdown("##### Item-Level Accuracy")
    item_tbl = (
        df_acc.groupby("product_name")
        .agg(predicted=("predicted_quantity", "sum"), actual=("actual_qty", "sum"))
        .reset_index()
    )
    item_tbl["diff"]     = item_tbl["predicted"] - item_tbl["actual"]
    item_tbl["pct_diff"] = item_tbl.apply(
        lambda r: r["diff"] / r["actual"] * 100 if r["actual"] > 0 else float("nan"), axis=1
    )
    item_tbl = item_tbl.sort_values("actual", ascending=False)
    item_tbl.columns = ["Product", "Predicted", "Actual", "Diff", "% Diff"]
    styled = (
        item_tbl.style
        .format({"Predicted": "{:.1f}", "Actual": "{:.1f}", "Diff": "{:+.1f}", "% Diff": "{:+.1f}%"}, na_rep="—")
        .map(_color_gap, subset=["% Diff"])
    )
    st.dataframe(styled, width='stretch', height=450)


if page == "📊 Accuracy Report":
    dow_acc = acc_date.strftime("%A")
    st.title(f"📊 Accuracy Report — {dow_acc}, {acc_date.strftime('%B %d, %Y')}")
    st.caption("🕐 All times in Central Time (CT)")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 1 — Cross-location summary table
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("All Locations — Daily Summary")
    summary_rows = load_cross_location_summary(acc_date)

    if summary_rows:
        df_sum = pd.DataFrame(summary_rows)
        df_sum["predicted"] = df_sum["predicted"].astype(float)
        df_sum["actual"]    = df_sum["actual"].astype(float)
        df_sum["mae"]       = df_sum["mae"].astype(float)
        df_sum["slots"]     = df_sum["slots"].astype(int)
        df_sum["within_1"]  = df_sum["within_1"].astype(int)
        df_sum["within_2"]  = df_sum["within_2"].astype(int)

        df_sum["diff"]      = df_sum["predicted"] - df_sum["actual"]
        df_sum["gap_pct"]   = df_sum.apply(
            lambda r: r["diff"] / r["actual"] * 100 if r["actual"] > 0 else float("nan"), axis=1
        )
        df_sum["w1_pct"]    = df_sum["within_1"] / df_sum["slots"] * 100
        df_sum["w2_pct"]    = df_sum["within_2"] / df_sum["slots"] * 100

        # totals row
        tot = {
            "location": "TOTAL", "predicted": df_sum["predicted"].sum(),
            "actual": df_sum["actual"].sum(), "mae": df_sum["mae"].mean(),
            "diff": df_sum["diff"].sum(), "slots": df_sum["slots"].sum(),
            "w1_pct": df_sum["within_1"].sum() / df_sum["slots"].sum() * 100,
            "w2_pct": df_sum["within_2"].sum() / df_sum["slots"].sum() * 100,
        }
        tot["gap_pct"] = tot["diff"] / tot["actual"] * 100 if tot["actual"] else float("nan")
        df_sum = pd.concat([df_sum, pd.DataFrame([tot])], ignore_index=True)

        tbl = df_sum[["location","predicted","actual","diff","gap_pct","mae","w1_pct","w2_pct"]].copy()
        tbl.columns = ["Location","Predicted","Actual","Diff","Gap %","MAE","±1 %","±2 %"]

        styled_sum = (
            tbl.style
            .format({
                "Predicted": "{:,.0f}", "Actual": "{:,.0f}",
                "Diff": "{:+,.0f}", "Gap %": "{:+.1f}%",
                "MAE": "{:.3f}", "±1 %": "{:.1f}%", "±2 %": "{:.1f}%",
            }, na_rep="—")
            .map(_bg_gap, subset=["Gap %"])
            .map(_color_gap, subset=["Gap %"])
        )
        st.dataframe(styled_sum, width='stretch', height=460)
    else:
        st.warning("No prediction data for this date.")

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 2 — Accuracy trend (gap % over time, all locations)
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("Accuracy Trend — Last 30 Days")
    trend_rows = load_accuracy_trend(n_dates=30)

    if trend_rows:
        df_trend = pd.DataFrame(trend_rows)
        df_trend["predicted"] = df_trend["predicted"].astype(float)
        df_trend["actual"]    = df_trend["actual"].astype(float)
        df_trend["gap_pct"]   = df_trend.apply(
            lambda r: (r["predicted"] - r["actual"]) / r["actual"] * 100
            if r["actual"] > 0 else float("nan"), axis=1
        )
        df_trend["target_date"] = pd.to_datetime(df_trend["target_date"])

        dates_available = df_trend["target_date"].nunique()

        if dates_available < 2:
            st.info("Trend chart needs at least 2 past dates with predictions. Check back as more days accumulate.")
        else:
            # Overall daily gap line (all locations combined)
            df_overall = (
                df_trend.groupby("target_date")
                .apply(lambda g: pd.Series({
                    "predicted": g["predicted"].sum(),
                    "actual":    g["actual"].sum(),
                }))
                .reset_index()
            )
            df_overall["gap_pct"] = (
                (df_overall["predicted"] - df_overall["actual"])
                / df_overall["actual"].replace(0, float("nan")) * 100
            )

            locations_list = sorted(df_trend["location"].unique())
            trend_colors   = CHART_COLORS

            fig_trend = go.Figure()

            # Per-location faint lines
            for i, loc in enumerate(locations_list):
                sub = df_trend[df_trend["location"] == loc].sort_values("target_date")
                fig_trend.add_trace(go.Scatter(
                    name=loc, x=sub["target_date"], y=sub["gap_pct"],
                    mode="lines+markers",
                    line=dict(color=trend_colors[i % len(trend_colors)], width=1.5),
                    marker=dict(size=5),
                    hovertemplate=f"<b>{loc}</b><br>%{{x|%b %d}}: %{{y:+.1f}}%<extra></extra>",
                ))

            # Bold overall average line
            fig_trend.add_trace(go.Scatter(
                name="ALL (avg)", x=df_overall["target_date"], y=df_overall["gap_pct"],
                mode="lines+markers",
                line=dict(color="black", width=3),
                marker=dict(size=7, symbol="diamond"),
                hovertemplate="<b>All locations</b><br>%{x|%b %d}: %{y:+.1f}%<extra></extra>",
            ))

            # Zero reference line
            fig_trend.add_hline(y=0, line_dash="dash", line_color="#aaa", line_width=1)
            fig_trend.add_hrect(y0=-10, y1=10, fillcolor="rgba(39,174,96,0.06)", line_width=0)

            fig_trend.update_layout(
                title=dict(text="Volume Gap % by Location (positive = over-predicted)", font=dict(size=14)),
                xaxis_title="Date", yaxis_title="Gap %",
                height=420,
                legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0,
                            font=dict(size=10)),
                margin=dict(l=0, r=0, t=55, b=30),
                plot_bgcolor="white", paper_bgcolor="white",
            )
            fig_trend.update_xaxes(showgrid=True, gridcolor="#eee")
            fig_trend.update_yaxes(showgrid=True, gridcolor="#eee", ticksuffix="%")
            st.plotly_chart(fig_trend, width='stretch', key="trend_chart")
    else:
        st.info("No past prediction data found for trend analysis.")

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 3 — Per-location drill-down tabs (existing)
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("Per-Location Drill-Down")
    acc_locations = load_locations()
    acc_tabs = st.tabs([l["name"] for l in acc_locations])
    for tab, loc in zip(acc_tabs, acc_locations):
        with tab:
            render_accuracy_tab(loc["name"], loc["id"], acc_date, str(loc["id"]))
    st.stop()

# ── Tender Planning page ──────────────────────────────────────────────────────

if page == "🍗 Tender Planning":
    dow_t = selected_date.strftime("%A")
    st.markdown(f"""
    <div class="hero">
      <div>
        <h1>🍗 Tender Planning</h1>
        <div class="sub">Chicken tenders needed per 15-min slot · built from past same-weekday orders</div>
      </div>
      <div class="badge">📅 {dow_t}, {selected_date.strftime('%b %d, %Y')}</div>
    </div>
    """, unsafe_allow_html=True)

    t_locations = load_locations()
    loc_names = [l["name"] for l in t_locations]

    st.markdown('<div class="sec">📍 Location</div>', unsafe_allow_html=True)
    selected_loc_name = st.selectbox(
        "Location", loc_names, index=0,
        label_visibility="collapsed", key="tender_loc_nav",
    )
    loc = next(l for l in t_locations if l["name"] == selected_loc_name)

    # Only the selected location's history/actuals are queried per rerun (vs.
    # the old st.tabs layout, which computed all 11 tabs on every rerun).
    loading_ph = st.empty()
    loading_ph.markdown(
        f'<div class="loadwrap"><div class="loadcard">'
        f'<div class="spin-ring"></div>'
        f'<div class="load-title">Crunching predictions</div>'
        f'<div class="load-sub">{selected_date.strftime("%B %d, %Y")} · {loc["name"]}</div>'
        f'<div class="skel-row"><div class="skel-bar"></div></div>'
        f'<div class="skel-row"><div class="skel-bar" style="width:80%"></div></div>'
        f'<div class="skel-row"><div class="skel-bar" style="width:64%"></div></div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )
    # ── Predicted (from past data, per 15-min) vs Actual ────────────────────
    # Predicted for each 15-min slot = a typical (median) same-weekday from
    # history at that exact slot. For these low-volume, high-variance slots
    # this is more reliable than the ML model.
    pg_dow = (selected_date.weekday() + 1) % 7   # Python Mon=0 → Postgres Sun=0
    hist_rows_all = load_dow_history(loc["id"], pg_dow)
    hist_rows = [r for r in hist_rows_all if r["sale_date"] < selected_date]

    # Actuals for the selected date, per 15-min slot.
    actual_rows = load_actuals(selected_date, loc["id"])
    rec_rows_all = load_recent_daily(loc["id"])
    _rec_start = selected_date - timedelta(days=10 * 7)
    rec_rows = [r for r in rec_rows_all if _rec_start < r["sale_date"] <= selected_date]

    def _flavor_frame(rows, qty_col):
        """Attach regular_tenders / spicy_tenders / unspecified_tenders columns."""
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        splits = df.apply(
            lambda r: get_finger_split(r["product_name"], r["flavor_mods"]),
            axis=1, result_type="expand",
        )
        splits.columns = ["reg_fc", "spi_fc", "uns_fc"]
        df = pd.concat([df, splits], axis=1)
        df["regular_tenders"]     = df[qty_col] * df["reg_fc"]
        df["spicy_tenders"]       = df[qty_col] * df["spi_fc"]
        df["unspecified_tenders"] = df[qty_col] * df["uns_fc"]
        return df

    # ── Unspecified-flavor caveat (no name/modifier flavor signal at all) ────
    if hist_rows:
        _chk = _flavor_frame(hist_rows, "qty")
        _total = (_chk["regular_tenders"] + _chk["spicy_tenders"] + _chk["unspecified_tenders"]).sum()
        _uns = _chk["unspecified_tenders"].sum()
        if _total and _uns / _total > 0.005:
            st.caption(
                f"ℹ️ {_uns / _total * 100:.0f}% of historical tender volume has no identifiable "
                f"spicy/regular tag (no matching name or modifier) and is excluded from both tabs below."
            )

    def render_tender_flavor(flavor_col: str, flavor_name: str):
        """Renders the per-15-min table, pace banner, and last-7-days section
        for one flavor (flavor_col is 'regular_tenders' or 'spicy_tenders')."""
        actual_by_slot = pd.Series(dtype=float)
        if actual_rows:
            adf_all = _flavor_frame(actual_rows, "actual_qty")
            actual_by_slot = adf_all.groupby("slot_index")[flavor_col].sum()

        if not hist_rows:
            st.warning(f"Not enough same-weekday history yet for **{loc['name']}** to predict.")
            slotly = pd.DataFrame()
        else:
            hdf = _flavor_frame(hist_rows, "qty").rename(columns={flavor_col: "tenders"})
            hist_slot = (
                hdf.groupby(["sale_date", "slot_index"])["tenders"].sum().reset_index()
            )
            # Zero-fill: a past same-weekday with no row for a given slot means
            # zero of this flavor sold then, not "no data" — without this, the
            # median for low-volume slots is computed over only the handful of
            # weeks that happened to have an order there, silently dropping the
            # (often many) weeks that had none, and skewing predictions upward.
            all_dates = sorted(hist_slot["sale_date"].unique())
            n_weeks = len(all_dates)
            pivot = (
                hist_slot.pivot(index="sale_date", columns="slot_index", values="tenders")
                .reindex(index=all_dates, columns=range(96))
                .fillna(0.0)
            )
            predicted = pivot.median(axis=0)
            predicted.index.name = "slot_index"
            predicted = predicted.rename("predicted")

            slotly = predicted.reset_index()
            slotly = slotly[slotly["predicted"] > 0]   # operating hours only
            if slotly.empty:
                st.info(f"No {flavor_name.lower()} sold historically at **{loc['name']}** for "
                        f"{selected_date.strftime('%A')}s — nothing to prep for this tab.")
                return
            slotly["Time"] = slotly["slot_index"].apply(slot_range_label)
            slotly["actual"] = slotly["slot_index"].map(actual_by_slot)
            if not actual_by_slot.empty:
                # Slots up to the last one with data are complete → blanks = 0 sales.
                # Later slots haven't happened yet → leave blank (—).
                last_data_slot = int(actual_by_slot.index.max())
                fill = slotly["slot_index"] <= last_data_slot
                slotly.loc[fill, "actual"] = slotly.loc[fill, "actual"].fillna(0)

            has_actual = slotly["actual"].notna().any()
            slotly["diff"] = slotly["actual"] - slotly["predicted"]
            slotly["gap"] = slotly["diff"] / slotly["predicted"].replace(0, pd.NA) * 100

            # ── KPI cards ────────────────────────────────────────────────────
            # HTML must be single-line (no indentation) or Streamlit's markdown
            # treats indented lines as a code block and shows raw tags.
            pred_total = slotly["predicted"].sum()
            peak_i = slotly["predicted"].idxmax()
            peak_val = slotly.loc[peak_i, "predicted"]
            peak_time = slotly.loc[peak_i, "Time"].split("–")[0]
            dow_name = selected_date.strftime('%A')

            def _kpi(label, value, foot, foot_color="#6b7280", accent=False):
                cls = "kpi accent" if accent else "kpi"
                return (f'<div class="{cls}"><div class="label">{label}</div>'
                        f'<div class="value">{value}</div>'
                        f'<div class="foot" style="color:{foot_color}">{foot}</div></div>')

            cards = [_kpi(f"Predicted {flavor_name.lower()}", f"{pred_total:,.0f}", f"typical {dow_name}", accent=True)]
            if has_actual:
                act_total = slotly["actual"].sum(skipna=True)
                g = (act_total - pred_total) / pred_total * 100 if pred_total else 0
                gcol = "#16a34a" if abs(g) <= 12 else ("#f59e0b" if abs(g) <= 25 else "#dc2626")
                cards.append(_kpi("Actual sold", f"{act_total:,.0f}", f"{g:+.0f}% vs predicted", gcol, accent=True))
            else:
                cards.append(_kpi("Actual sold", "—", "awaiting orders"))
            cards.append(_kpi("Peak 15-min", f"{peak_val:,.0f}", f"at {peak_time}"))
            cards.append(_kpi("History depth", f"{n_weeks}", f"past {dow_name}s"))
            st.markdown(f'<div class="kpi-row">{"".join(cards)}</div>', unsafe_allow_html=True)

            # ── Chart ────────────────────────────────────────────────────────
            st.markdown(f'<div class="sec">📈 Demand curve — predicted vs actual</div>', unsafe_allow_html=True)
            with st.container(border=True):
                st.plotly_chart(
                    build_pred_actual_chart(slotly, has_actual),
                    width='stretch', key=f"chart_{loc['id']}_{flavor_col}",
                    config={"displayModeBar": False},
                )

            # ── Interactive data table (click a row for full history) ────────
            st.markdown('<div class="sec">🍗 Every 15 min</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div class="sec-sub">Predicted = a typical (median) {selected_date.strftime("%A")} '
                f'per slot from the last {n_weeks} same-weekdays · Actual = what sold that day · '
                f'click a row to see every past {selected_date.strftime("%A")} at that time</div>',
                unsafe_allow_html=True,
            )
            maxv = max([slotly["predicted"].max(),
                        slotly["actual"].max() if has_actual else 0]) or 1

            table_df = slotly.reset_index(drop=True)
            display_df = pd.DataFrame({"Time": table_df["Time"], "Predicted": table_df["predicted"]})
            col_cfg = {
                "Predicted": st.column_config.ProgressColumn(
                    "Predicted", format="%.0f", min_value=0, max_value=maxv),
            }
            if has_actual:
                display_df["Actual"] = table_df["actual"]
                display_df["Gap"] = table_df["gap"].apply(gap_label)
                col_cfg["Actual"] = st.column_config.ProgressColumn(
                    "Actual", format="%.0f", min_value=0, max_value=maxv)
            else:
                display_df["Actual"] = "—"
                display_df["Gap"] = "—"

            # Zebra-striped rows so the eye tracks across 90+ slots more easily.
            event = st.dataframe(
                zebra_style(display_df),
                hide_index=True,
                width="stretch",
                height=min(600, 44 + 38 * len(display_df)),
                row_height=38,
                on_select="rerun",
                selection_mode="single-row",
                key=f"slot_table_{loc['id']}_{flavor_col}",
                column_config=col_cfg,
            )

            sel_rows = event.selection.rows if event and event.selection else []
            if sel_rows:
                sel_row = table_df.loc[sel_rows[0]]
                sel_hist = pivot[[int(sel_row["slot_index"])]].reset_index()
                sel_hist.columns = ["sale_date", "tenders"]
                show_slot_history_dialog(loc["name"], dow_name, sel_row["Time"], sel_hist)

        # ── Intraday pace ────────────────────────────────────────────────────
        if actual_rows and not slotly.empty:
            # Cumulative actual so far vs typical (median) cumulative through the
            # last slot with data — tells the kitchen if it's a high or low day.
            last_slot = int(actual_by_slot.index.max())
            cum_actual = actual_by_slot.loc[:last_slot].sum()
            typ_cum = pivot.loc[:, pivot.columns <= last_slot].sum(axis=1).median()
            through = slot_label(last_slot + 1)
            if typ_cum and typ_cum > 0:
                pace = (cum_actual / typ_cum - 1) * 100
                if pace <= -12:
                    st.error(
                        f"🔻 **Running {pace:+.0f}% vs a typical {selected_date.strftime('%A')}** "
                        f"through {through} ({cum_actual:.0f} vs ~{typ_cum:.0f} {flavor_name.lower()}). "
                        f"Slow day — prep below the numbers above for the rest of the day."
                    )
                elif pace >= 12:
                    st.warning(
                        f"🔺 **Running {pace:+.0f}% vs a typical {selected_date.strftime('%A')}** "
                        f"through {through} ({cum_actual:.0f} vs ~{typ_cum:.0f} {flavor_name.lower()}). "
                        f"Busy day — prep above the numbers above for the rest of the day."
                    )
                else:
                    st.success(
                        f"✅ **Running {pace:+.0f}% vs a typical {selected_date.strftime('%A')}** "
                        f"through {through} — pacing normally, stick to the numbers above."
                    )
        elif not actual_rows:
            st.caption("📊 Actual & pace appear here once the day's orders start coming in.")

        # ── Last 7 days — predicted vs actual (daily totals) ─────────────────
        st.markdown('<div class="sec">📅 Last 7 days — predicted vs actual</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="sec-sub">Predicted = a typical (median) day for that weekday · '
            'Actual = what actually sold</div>',
            unsafe_allow_html=True,
        )
        if not rec_rows:
            st.caption("Not enough recent history to compare.")
        else:
            rdf = _flavor_frame(rec_rows, "qty").rename(columns={flavor_col: "tenders"})
            daily = rdf.groupby(["sale_date", "dow"])["tenders"].sum().reset_index()
            dow_typical = daily.groupby("dow")["tenders"].median()   # typical day per weekday

            last7 = [selected_date - timedelta(days=i) for i in range(6, -1, -1)]
            actual_by_date = dict(zip(daily["sale_date"], daily["tenders"]))
            wk = []
            for d in last7:
                pg = (d.weekday() + 1) % 7
                pred = dow_typical.get(pg, float("nan"))
                act = actual_by_date.get(d, float("nan"))
                diff = act - pred if pd.notna(act) and pd.notna(pred) else float("nan")
                gap = (diff / pred * 100) if pd.notna(diff) and pred else float("nan")
                wk.append({
                    "Day": d.strftime("%a %b %d"),
                    "Predicted (typical)": pred,
                    "Actual": act,
                    "Diff": diff,
                    "Gap %": gap,
                })
            wk_df = pd.DataFrame(wk)
            maxv7 = max([wk_df["Predicted (typical)"].max(skipna=True) or 0,
                         wk_df["Actual"].max(skipna=True) or 0]) or 1

            disp7 = pd.DataFrame({
                "Day": wk_df["Day"],
                "Predicted": wk_df["Predicted (typical)"],
                "Actual": wk_df["Actual"],
                "Diff": wk_df["Diff"].apply(lambda v: "—" if pd.isna(v) else f"{v:+.0f}"),
                "Gap": wk_df["Gap %"].apply(gap_label),
            })

            st.dataframe(
                zebra_style(disp7),
                width='stretch',
                hide_index=True,
                height=44 + 38 * len(disp7),
                row_height=38,
                column_config={
                    "Predicted": st.column_config.ProgressColumn(
                        "Predicted", format="%.0f", min_value=0, max_value=maxv7),
                    "Actual": st.column_config.ProgressColumn(
                        "Actual", format="%.0f", min_value=0, max_value=maxv7),
                },
            )

    spicy_tab, regular_tab = st.tabs(["🌶️ Spicy", "🍗 Regular"])
    with spicy_tab:
        render_tender_flavor("spicy_tenders", "Spicy tenders")
    with regular_tab:
        render_tender_flavor("regular_tenders", "Regular tenders")

    loading_ph.empty()
    st.stop()

# ── Main content ──────────────────────────────────────────────────────────────

dow = selected_date.strftime("%A")
st.title(f"📋 Predictions — {dow}, {selected_date.strftime('%B %d, %Y')}")
st.caption("🕐 All times shown in Central Time (CT) — Houston, TX")

locations = load_locations()
loc_names = [l["name"] for l in locations]
tabs = st.tabs(loc_names)

for tab, loc in zip(tabs, locations):
    with tab:
        rows = load_predictions(selected_date, loc["id"])
        if not rows:
            st.warning(f"No predictions available for **{loc['name']}** on {selected_date}.")
            continue

        df = prep_df(rows)
        # Full operating hours: 4 AM (slot 16) to midnight (slot 96)
        # Locations open at 4–5 AM; Shepherd stays open until 23:00
        df = df[(df["slot_index"] >= 16) & (df["slot_index"] < 96)]

        df_actual = None
        if compare:
            act_rows = load_actuals(selected_date, loc["id"])
            if act_rows:
                df_actual = pd.DataFrame(act_rows)
                df_actual["actual_qty"] = df_actual["actual_qty"].astype(float)
                df_actual["slot_label"] = df_actual["slot_index"].apply(slot_label)
                df_actual = df_actual[(df_actual["slot_index"] >= 16) & (df_actual["slot_index"] < 96)]

        # ── Summary metrics ───────────────────────────────────────────────────
        prod_totals  = df.groupby("product_name")["predicted_quantity"].sum()
        slot_totals  = df.groupby("slot_index")["predicted_quantity"].sum()
        total_day    = df["predicted_quantity"].sum()
        peak_slot    = int(slot_totals.idxmax())
        top_item     = prod_totals.idxmax()
        top_item_qty = prod_totals.max()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Predicted", f"{total_day:,.0f} items")
        c2.metric("Peak Slot", slot_label(peak_slot), f"{slot_totals[peak_slot]:.0f} items")
        c3.metric("Top Item", top_item[:22], f"{top_item_qty:.0f} units")
        if compare and df_actual is not None:
            actual_total = df_actual["actual_qty"].sum()
            diff = total_day - actual_total
            c4.metric("Actual Total", f"{actual_total:,.0f}",
                      f"{diff:+.0f} ({diff/actual_total*100:+.1f}%)" if actual_total else "—")
        else:
            c4.metric("Locations", "All 11 locations")

        # ── Chart ─────────────────────────────────────────────────────────────
        top8 = top_n_products(df, n=8)
        chart_title = f"{loc['name']} · 15-Min Forecast"
        if compare and df_actual is not None:
            chart_title += " (vs Actual ···)"
        fig = build_chart(df, top8, chart_title, df_actual if compare else None)
        st.plotly_chart(fig, width='stretch', key=f"chart_{loc['id']}")

        # ── Shift breakdown ───────────────────────────────────────────────────
        st.markdown("##### Shift Breakdown")
        scols = st.columns(4)
        for (sname, s0, s1), scol in zip(SHIFTS, scols):
            sdf = df[(df["slot_index"] >= s0) & (df["slot_index"] < s1)]
            stotal = sdf["predicted_quantity"].sum()
            stop5  = (sdf.groupby("product_name")["predicted_quantity"]
                         .sum()
                         .sort_values(ascending=False)
                         .head(5))
            actual_s = ""
            if compare and df_actual is not None:
                a_total = df_actual[
                    (df_actual["slot_index"] >= s0) & (df_actual["slot_index"] < s1)
                ]["actual_qty"].sum()
                actual_s = f" / {a_total:.0f} actual"
            scol.markdown(f"**{sname}**")
            scol.metric("", f"{stotal:.0f}{actual_s}")
            for item, qty in stop5.items():
                scol.caption(f"· {item[:22]}: {qty:.0f}")

        # ── Item detail table ─────────────────────────────────────────────────
        with st.expander("📊 Full item × slot table"):
            pivot_tbl = (
                df.groupby(["product_name", "slot_label"])["predicted_quantity"]
                .sum()
                .unstack(fill_value=0)
            )
            pivot_tbl.insert(0, "Daily Total", pivot_tbl.sum(axis=1))
            pivot_tbl = pivot_tbl.sort_values("Daily Total", ascending=False)

            if compare and df_actual is not None:
                act_pivot = (
                    df_actual.groupby(["product_name", "slot_label"])["actual_qty"]
                    .sum()
                    .unstack(fill_value=0)
                )
                act_daily = act_pivot.sum(axis=1).rename("Actual Total")
                pivot_tbl = pivot_tbl.join(act_daily, how="left").fillna(0)
                pivot_tbl["Diff"] = (pivot_tbl["Daily Total"] - pivot_tbl["Actual Total"]).round(1)

            st.dataframe(
                pivot_tbl.style.format("{:.1f}"),
                width='stretch',
                height=400,
            )
