"""
dashboard.py — Laynes Daily Prediction Dashboard
Streamlit app serving 15-min item forecasts for all 11 locations.
"""

import os
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
import psycopg2.extras
import streamlit as st
from dotenv import load_dotenv

load_dotenv("/opt/laynes/.env")

st.set_page_config(
    page_title="Laynes Predictions",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────

st.markdown("""
<style>
[data-testid="stMetricValue"] { font-size: 1.6rem; }
[data-testid="stMetricLabel"] { font-size: 0.85rem; color: #888; }
.shift-card { background:#f8f8f8; border-radius:8px; padding:10px 14px; margin:4px 0; }
</style>
""", unsafe_allow_html=True)

# ── DB helpers ────────────────────────────────────────────────────────────────

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
        cur.execute("""
            SELECT
                oi.product_id,
                MAX(oi.product_name) AS product_name,
                (EXTRACT(HOUR   FROM oi.created_date AT TIME ZONE 'America/Chicago') * 4
                 + FLOOR(EXTRACT(MINUTE FROM oi.created_date AT TIME ZONE 'America/Chicago') / 15)
                )::SMALLINT AS slot_index,
                SUM(oi.quantity)::float AS actual_qty
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id AND o.closed = TRUE AND o.deleted = FALSE
            WHERE DATE(oi.created_date AT TIME ZONE 'America/Chicago') = %s
              AND oi.establishment_id = %s
              AND oi.deleted = FALSE AND oi.is_voided = FALSE AND oi.product_id IS NOT NULL
            GROUP BY oi.product_id, slot_index
        """, (target_date, establishment_id))
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

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/thumb/1/1e/Circle-icons-chicken.svg/120px-Circle-icons-chicken.svg.png", width=60)
    st.title("Laynes Chicken Fingers")
    st.caption("Daily Prediction Dashboard")
    st.markdown("---")

    page = st.radio("View", ["📋 Daily Predictions", "📊 Accuracy Report"], label_visibility="collapsed")

    st.markdown("---")

    pred_dates = load_prediction_dates()
    if not pred_dates:
        st.error("No prediction data found.")
        st.stop()

    past_dates = [d for d in pred_dates if d < date.today()]

    if page == "📋 Daily Predictions":
        selected_date = st.selectbox(
            "Prediction date",
            pred_dates,
            format_func=lambda d: d.strftime("%A, %b %d %Y"),
        )
        show_actuals = selected_date < date.today()
        if show_actuals:
            compare = st.checkbox("Compare with actual sales", value=True)
        else:
            compare = False
            st.info("Actuals available after the day ends.")
    else:
        if not past_dates:
            st.error("No past prediction dates available.")
            st.stop()
        acc_date = st.selectbox(
            "Report date",
            past_dates,
            format_func=lambda d: d.strftime("%A, %b %d %Y"),
        )
        locations_for_acc = load_locations()
        acc_loc_name = st.selectbox(
            "Location",
            [l["name"] for l in locations_for_acc],
        )
        acc_loc = next(l for l in locations_for_acc if l["name"] == acc_loc_name)

    st.markdown("---")
    st.caption("🕐 All times in **Central Time** (Houston, TX)")
    st.caption("Predictions generated nightly at 2 AM CT by LightGBM (lgbm_v4).")
    st.caption("Accuracy: 88% within ±1 unit, 94% within ±2 units.")

# ── Accuracy Report page ──────────────────────────────────────────────────────

if page == "📊 Accuracy Report":
    dow_acc = acc_date.strftime("%A")
    st.title(f"📊 Accuracy Report — {dow_acc}, {acc_date.strftime('%B %d, %Y')}")
    st.caption(f"Location: **{acc_loc_name}** · All times in Central Time (CT)")

    acc_rows = load_accuracy_metrics(acc_date, acc_loc["id"])
    if not acc_rows:
        st.warning(f"No prediction data for **{acc_loc_name}** on {acc_date}.")
        st.stop()

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
        name="Predicted",
        x=all_slots, y=slot_pred.values,
        marker_color="#4C9BE8",
        hovertemplate="%{y:.1f} predicted<extra></extra>",
    ))
    fig_cmp.add_trace(go.Scatter(
        name="Actual",
        x=all_slots, y=slot_act.values,
        mode="lines+markers",
        line=dict(color="black", width=2, dash="dot"),
        marker=dict(size=4),
        hovertemplate="%{y:.0f} actual<extra>Actual</extra>",
    ))
    hour_ticks_acc = [s for s in all_slots if s.endswith(":00")]
    fig_cmp.update_layout(
        barmode="group",
        title=dict(text=f"{acc_loc_name} · Predicted vs Actual (15-min slots)", font=dict(size=14)),
        xaxis_title="Time (CT)",
        yaxis_title="Quantity",
        height=380,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0,
                    font=dict(size=11)),
        margin=dict(l=0, r=0, t=55, b=30),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    fig_cmp.update_xaxes(
        tickangle=-45, tickmode="array",
        tickvals=hour_ticks_acc, ticktext=hour_ticks_acc,
        showgrid=True, gridcolor="#eee",
    )
    fig_cmp.update_yaxes(showgrid=True, gridcolor="#eee")
    st.plotly_chart(fig_cmp, width='stretch', key="acc_chart")

    st.markdown("---")

    # ── Item-level summary table ───────────────────────────────────────────────
    st.markdown("##### Item-Level Accuracy")
    item_tbl = (
        df_acc.groupby("product_name")
        .agg(predicted=("predicted_quantity", "sum"),
             actual=("actual_qty", "sum"))
        .reset_index()
    )
    item_tbl["diff"]     = item_tbl["predicted"] - item_tbl["actual"]
    item_tbl["pct_diff"] = item_tbl.apply(
        lambda r: r["diff"] / r["actual"] * 100 if r["actual"] > 0 else float("nan"), axis=1
    )
    item_tbl = item_tbl.sort_values("actual", ascending=False)
    item_tbl.columns = ["Product", "Predicted", "Actual", "Diff", "% Diff"]

    def _color_diff(val):
        if pd.isna(val):
            return "color: #aaa"
        return "color: #c0392b" if abs(val) > 20 else ("color: #e67e22" if abs(val) > 10 else "color: #27ae60")

    styled = (
        item_tbl.style
        .format({"Predicted": "{:.1f}", "Actual": "{:.1f}", "Diff": "{:+.1f}", "% Diff": "{:+.1f}%"}, na_rep="—")
        .map(_color_diff, subset=["% Diff"])
    )
    st.dataframe(styled, width='stretch', height=450)
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
