"""
pages/Revel_Data_Export.py — Revel Data Export
Streamlit page for exporting raw orders / order_items rows straight out of
Postgres. No Revel API calls — the daily pipeline already ingests everything
this page reads.

Runs as a Streamlit multipage "page" file (Streamlit auto-discovers anything
under pages/ next to the entrypoint dashboard.py), so it's reachable from the
same running app/port/nginx route with no deployment changes.
"""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg2
import psycopg2.extras
import streamlit as st
from dotenv import load_dotenv
import os

load_dotenv()

st.set_page_config(
    page_title="Laynes · Revel Data Export",
    page_icon="📤",
    layout="wide",
)

# ── Styling — same design tokens as dashboard.py, trimmed to what this page uses ──
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
:root{
  --brand:#6366f1; --brand-2:#8b5cf6; --brand-ink:#4f46e5;
  --ink:#0f172a; --muted:#64748b; --faint:#94a3b8;
  --bg:#f4f6fc; --card:#ffffff; --line:#e7eaf5;
  --r:18px; --r-sm:12px;
  --sh-sm:0 1px 3px rgba(15,23,42,.05), 0 6px 16px -10px rgba(15,23,42,.16);
  --sh-brand:0 14px 34px -14px rgba(99,102,241,.5);
}
*{ -webkit-font-smoothing:antialiased; }
html, body, [class*="css"], .stApp, button, input, textarea, select{
  font-family:'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif !important;
}
.stApp{
  background:
    radial-gradient(1100px 440px at 10% -8%, rgba(139,92,246,.10), transparent 60%),
    radial-gradient(1000px 420px at 98% -14%, rgba(99,102,241,.13), transparent 62%),
    var(--bg);
}
.block-container{ padding-top:1.5rem; padding-bottom:3.2rem; max-width:1100px; }
#MainMenu, footer, header[data-testid="stHeader"]{ display:none !important; }

.brand{ display:flex; align-items:center; gap:14px; margin-bottom:6px; }
.brand-logo{
  width:52px; height:52px; border-radius:16px; flex:0 0 auto;
  background:linear-gradient(135deg, var(--brand), var(--brand-2));
  display:flex; align-items:center; justify-content:center; font-size:1.7rem;
  box-shadow:var(--sh-brand);
}
.brand-title{ font-size:1.55rem; font-weight:800; color:var(--ink); letter-spacing:-.025em; line-height:1.05; }
.brand-sub{ font-size:.86rem; color:var(--muted); font-weight:500; margin-top:2px; }

.chips{ display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 20px; }
.chip{
  display:inline-flex; align-items:center; gap:6px; padding:6px 13px; border-radius:999px;
  background:var(--card); border:1px solid var(--line); box-shadow:var(--sh-sm);
  font-size:.82rem; font-weight:600; color:var(--muted);
}
.chip-brand{ background:linear-gradient(135deg, var(--brand), var(--brand-2)); border:none; color:#fff; box-shadow:var(--sh-brand); }

[data-testid="stWidgetLabel"] p{ font-size:.72rem !important; font-weight:700 !important;
  text-transform:uppercase; letter-spacing:.06em; color:var(--faint) !important; }
[data-baseweb="select"] > div, [data-baseweb="input"]{
  border-radius:var(--r-sm) !important; border-color:var(--line) !important;
  box-shadow:var(--sh-sm); font-weight:600; background:var(--card) !important;
}

[data-testid="stVerticalBlockBorderWrapper"]{ border-radius:var(--r) !important; }
[data-testid="stAlert"]{ border-radius:var(--r-sm); }
</style>
""", unsafe_allow_html=True)

CENTRAL = ZoneInfo("America/Chicago")


# ── DB ─────────────────────────────────────────────────────────────────────────
# Duplicated from dashboard.py's _connect() rather than imported: dashboard.py
# is a top-level Streamlit script (executes UI code at import time), not a
# module safe to import from another page.
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
        cur.execute("SELECT id, name FROM establishments WHERE active = true ORDER BY name")
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ── Dataset registry — add a new dataset (e.g. modifier_items) by adding an
# entry here; nothing else on the page needs to change. ────────────────────────
DATASETS = {
    "orders": {
        "label": "Orders",
        "select": """
            o.id                                          AS order_id,
            o.uuid                                         AS order_uuid,
            o.local_id,
            o.establishment_id,
            e.name                                         AS establishment_name,
            o.created_date AT TIME ZONE 'America/Chicago'  AS created_date,
            o.updated_date AT TIME ZONE 'America/Chicago'  AS updated_date,
            o.pickup_time  AT TIME ZONE 'America/Chicago'  AS pickup_time,
            o.dining_option,
            dc.name                                        AS dining_channel_name,
            dc.channel_group,
            dc.is_third_party,
            o.pos_mode,
            o.subtotal,
            o.discount_total_amount,
            o.tax,
            o.gratuity,
            o.final_total,
            o.closed,
            o.is_unpaid,
            o.deleted,
            o.is_discounted,
            o.web_order,
            o.customer_id,
            o.number_of_people,
            o.ingested_at  AT TIME ZONE 'America/Chicago'  AS ingested_at,
            o.ingestion_date
        """,
        "from_join": """
            FROM orders_v2 o
            LEFT JOIN establishments e ON e.id = o.establishment_id
            LEFT JOIN dining_channels dc ON dc.id = o.dining_option
        """,
        "date_col": "o.created_date",
        "est_col": "o.establishment_id",
        "order_by": "o.created_date",
    },
    "order_items": {
        "label": "Order Items",
        "select": """
            oi.id                                                AS order_item_id,
            oi.uuid                                               AS order_item_uuid,
            oi.order_id,
            oi.establishment_id,
            e.name                                                AS establishment_name,
            oi.product_id,
            oi.product_name,
            p.product_class,
            oi.quantity,
            oi.dining_option,
            oi.combo_product_id,
            oi.combo_uuid,
            oi.price,
            oi.pure_sales,
            oi.tax_amount,
            oi.modifier_amount,
            oi.is_discounted,
            oi.created_date     AT TIME ZONE 'America/Chicago'    AS created_date,
            oi.start_time       AT TIME ZONE 'America/Chicago'    AS start_time,
            oi.kitchen_completed AT TIME ZONE 'America/Chicago'   AS kitchen_completed,
            oi.kitchen_seconds,
            oi.is_voided,
            oi.voided_date      AT TIME ZONE 'America/Chicago'    AS voided_date,
            oi.voided_by_user_id,
            oi.voided_reason,
            oi.deleted,
            oi.deleted_date     AT TIME ZONE 'America/Chicago'    AS deleted_date,
            p.is_combo,
            p.is_modifier,
            oi.ingested_at      AT TIME ZONE 'America/Chicago'    AS ingested_at,
            oi.ingestion_date
        """,
        # No join to orders — order_items already carries establishment_id and
        # dining_option directly, so the 1M-row orders table isn't touched.
        "from_join": """
            FROM order_items_v2 oi
            LEFT JOIN establishments e ON e.id = oi.establishment_id
            LEFT JOIN products p ON p.id = oi.product_id
        """,
        "date_col": "oi.created_date",
        "est_col": "oi.establishment_id",
        "order_by": "oi.created_date",
    },
}


def _local_day_bounds(start_date: date, end_date: date):
    """Local Central-time calendar-day boundaries, timezone-aware so DST is
    handled automatically by zoneinfo — converted to UTC-comparable instants
    for the timestamptz columns, computed here in Python and passed down as
    plain >= / < bind params so partition pruning on created_date still works."""
    start_ts = datetime.combine(start_date, time.min, tzinfo=CENTRAL)
    end_ts = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=CENTRAL)
    return start_ts, end_ts


def _where_clause(ds: dict, establishment_id):
    where = f"{ds['date_col']} >= %(start_ts)s AND {ds['date_col']} < %(end_ts)s"
    params = {}
    if establishment_id != "all":
        where += f" AND {ds['est_col']} = %(establishment_id)s"
        params["establishment_id"] = establishment_id
    return where, params


def run_count(dataset_key, start_ts, end_ts, establishment_id) -> int:
    ds = DATASETS[dataset_key]
    where, extra = _where_clause(ds, establishment_id)
    sql = f"SELECT COUNT(*) {ds['from_join']} WHERE {where}"
    params = {"start_ts": start_ts, "end_ts": end_ts, **extra}
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]
    finally:
        conn.close()


def run_export(dataset_key, start_ts, end_ts, establishment_id) -> pd.DataFrame:
    ds = DATASETS[dataset_key]
    where, extra = _where_clause(ds, establishment_id)
    sql = f"SELECT {ds['select']} {ds['from_join']} WHERE {where} ORDER BY {ds['order_by']}"
    params = {"start_ts": start_ts, "end_ts": end_ts, **extra}
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    return pd.DataFrame(rows)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return s or "location"


# ── UI ───────────────────────────────────────────────────────────────────────
st.markdown(
    '<div class="brand"><div class="brand-logo">📤</div>'
    '<div><div class="brand-title">Revel Data Export</div>'
    '<div class="brand-sub">Export raw Revel transactional data from the internal database.</div>'
    '</div></div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="chips"><span class="chip">🕐 Central Time · Houston, TX</span></div>',
    unsafe_allow_html=True,
)

locations = load_locations()
loc_names = ["All Locations"] + [l["name"] for l in locations]

today = datetime.now(CENTRAL).date()

with st.container(border=True):
    c1, c2, c3, c4 = st.columns([1.3, 1, 1, 1], gap="medium")
    with c1:
        selected_loc_name = st.selectbox("📍 Location", loc_names, index=0)
    with c2:
        start_date = st.date_input("📅 Start Date", value=None, max_value=today, format="YYYY-MM-DD")
    with c3:
        end_date = st.date_input("📅 End Date", value=None, max_value=today, format="YYYY-MM-DD")
    with c4:
        dataset_key = st.selectbox(
            "🗂️ Dataset", list(DATASETS.keys()),
            format_func=lambda k: DATASETS[k]["label"], index=0,
        )

    establishment_id = "all"
    if selected_loc_name != "All Locations":
        establishment_id = next(l["id"] for l in locations if l["name"] == selected_loc_name)

    # ── Validation ──────────────────────────────────────────────────────────
    errors = []
    if start_date is None or end_date is None:
        errors.append("Pick both a start date and an end date.")
    else:
        if start_date > today or end_date > today:
            errors.append("Future dates are not allowed.")
        if end_date < start_date:
            errors.append("End date cannot be before start date.")

    st.markdown(
        f'<div style="font-size:.86rem;color:var(--muted);margin:14px 0 4px">'
        f'Location: <b style="color:var(--ink)">{selected_loc_name}</b>'
        f'&nbsp;&nbsp;·&nbsp;&nbsp;Date Range: '
        f'<b style="color:var(--ink)">'
        f'{start_date.strftime("%b %d, %Y") if start_date else "—"} → '
        f'{end_date.strftime("%b %d, %Y") if end_date else "—"}</b>'
        f'&nbsp;&nbsp;·&nbsp;&nbsp;Dataset: <b style="color:var(--ink)">{DATASETS[dataset_key]["label"]}</b>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if errors:
        for e in errors:
            st.error(e)

    b1, b2 = st.columns([1, 1], gap="medium")
    count_clicked = b1.button("Check Row Count", use_container_width=True, disabled=bool(errors))
    export_clicked = b2.button("Prepare Export", type="primary", use_container_width=True, disabled=bool(errors))

    if count_clicked and not errors:
        start_ts, end_ts = _local_day_bounds(start_date, end_date)
        with st.spinner("Counting rows..."):
            n = run_count(dataset_key, start_ts, end_ts, establishment_id)
        st.info(f"{n:,} rows found.")

    if export_clicked and not errors:
        start_ts, end_ts = _local_day_bounds(start_date, end_date)
        with st.spinner("Preparing export..."):
            df = run_export(dataset_key, start_ts, end_ts, establishment_id)

        if df.empty:
            st.warning("0 rows match this selection — nothing to export.")
        else:
            st.success(f"{len(df):,} rows ready for download.")
            csv_bytes = df.to_csv(index=False).encode("utf-8")
            loc_slug = "all_locations" if selected_loc_name == "All Locations" else _slug(selected_loc_name)
            filename = f"{dataset_key}_{loc_slug}_{start_date.isoformat()}_{end_date.isoformat()}.csv"
            st.download_button(
                "Download CSV", data=csv_bytes, file_name=filename,
                mime="text/csv", use_container_width=True,
            )
