"""
sales_report.py — Laynes Network Sales Intelligence
Modern weekly performance dashboard for all 11 locations
"""

import os
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import psycopg2
import psycopg2.extras
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Laynes | Sales Intelligence",
    page_icon="🍗",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Palette ───────────────────────────────────────────────────────────────────
PALETTE = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#F7DC6F",
    "#C39BD3", "#85C1E9", "#F0B27A", "#82E0AA", "#F1948A", "#AED6F1",
]
DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');

* { font-family: 'Inter', sans-serif !important; }

[data-testid="stAppViewContainer"] { background: #F0F2F6; }
[data-testid="block-container"] { padding: 1.2rem 2rem 2rem !important; max-width: 1400px; }
[data-testid="stSidebar"] { background: #1a1a2e; }
[data-testid="stSidebar"] * { color: #e0e0e0 !important; }
section[data-testid="stSidebar"] .stSelectbox label { color: #aaa !important; }
[data-testid="stMetricValue"] { font-size: 1.4rem !important; font-weight: 700 !important; }
h4 { color: #1a1a2e !important; font-weight: 700 !important; margin-bottom: 0.5rem !important; }

.dash-header {
    background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
    border-radius: 18px;
    padding: 1.8rem 2.2rem;
    margin-bottom: 1.2rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
    box-shadow: 0 8px 32px rgba(48,43,99,0.35);
}
.dash-header-left .eyebrow {
    font-size: 0.72rem; letter-spacing: 0.15em; text-transform: uppercase;
    color: rgba(255,255,255,0.55); margin-bottom: 6px;
}
.dash-header-left .title {
    font-size: 1.65rem; font-weight: 900; color: #fff; margin: 0;
}
.dash-header-left .subtitle {
    font-size: 0.88rem; color: rgba(255,255,255,0.6); margin-top: 4px;
}
.dash-header-right { text-align: right; }
.dash-header-right .big-num {
    font-size: 2.4rem; font-weight: 900; color: #fff; line-height: 1;
}
.dash-header-right .wow { font-size: 1rem; font-weight: 700; margin-top: 4px; }
.dash-header-right .sub { font-size: 0.78rem; color: rgba(255,255,255,0.5); margin-top: 2px; }

.kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 1.2rem; }
.kpi-card {
    background: white; border-radius: 14px; padding: 1.1rem 1.3rem;
    box-shadow: 0 2px 12px rgba(0,0,0,0.07);
    border-top: 4px solid var(--accent, #6C63FF);
    transition: transform 0.15s;
}
.kpi-card:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(0,0,0,0.11); }
.kpi-label { font-size: 0.72rem; font-weight: 600; color: #999; text-transform: uppercase; letter-spacing: 0.08em; margin: 0 0 6px; }
.kpi-value { font-size: 1.75rem; font-weight: 800; color: #1a1a2e; margin: 0 0 4px; line-height: 1.1; }
.kpi-sub { font-size: 0.8rem; font-weight: 500; }
.pos { color: #10b981; } .neg { color: #ef4444; } .neu { color: #94a3b8; }

.card {
    background: white; border-radius: 16px; padding: 1.3rem 1.5rem;
    box-shadow: 0 2px 12px rgba(0,0,0,0.06); margin-bottom: 1.2rem;
}
.card-title {
    font-size: 0.95rem; font-weight: 700; color: #1a1a2e;
    margin: 0 0 1rem; letter-spacing: -0.01em;
    display: flex; align-items: center; gap: 6px;
}

.lb-item {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 0; border-bottom: 1px solid #F5F5F7;
}
.lb-item:last-child { border-bottom: none; }
.lb-rank { font-size: 1rem; font-weight: 800; width: 28px; text-align: center; flex-shrink: 0; }
.lb-name { font-size: 0.88rem; font-weight: 600; color: #1a1a2e; flex: 1; min-width: 90px; }
.lb-bar-wrap { flex: 1.5; min-width: 60px; }
.lb-bar-bg { background: #F0F2F6; border-radius: 99px; height: 7px; }
.lb-bar-fill { height: 7px; border-radius: 99px; transition: width 0.4s; }
.lb-rev { font-size: 0.88rem; font-weight: 700; color: #1a1a2e; width: 68px; text-align: right; flex-shrink: 0; }
.lb-wow { font-size: 0.78rem; font-weight: 700; width: 68px; text-align: right; flex-shrink: 0; }

.footer {
    text-align: center; color: #b0b7c3; font-size: 0.75rem;
    margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #E8EAF0;
}
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


@st.cache_data(ttl=300)
def load_available_weeks():
    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT DATE_TRUNC('week', date + 1)::date - 1 AS week_mon
            FROM features_daily_summary
            ORDER BY week_mon DESC LIMIT 12
        """)
        rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


@st.cache_data(ttl=120)
def load_week_summary(week_start: date):
    week_end  = week_start + timedelta(days=7)
    prev_s    = week_start - timedelta(days=7)
    prev_e    = week_start
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            WITH cur AS (
                SELECT establishment_id,
                    SUM(total_revenue)              AS revenue,
                    SUM(total_orders)               AS orders,
                    AVG(pct_drive_through)          AS dt_pct,
                    AVG(pct_third_party)            AS tp_pct,
                    AVG(pct_in_store)               AS in_pct,
                    SUM(total_discounts)            AS discounts,
                    COUNT(date)                     AS active_days
                FROM features_daily_summary
                WHERE date >= %s AND date < %s
                GROUP BY establishment_id
            ),
            prev AS (
                SELECT establishment_id,
                    SUM(total_revenue)  AS revenue,
                    SUM(total_orders)   AS orders
                FROM features_daily_summary
                WHERE date >= %s AND date < %s
                GROUP BY establishment_id
            )
            SELECT
                e.id, e.name,
                cur.revenue, cur.orders,
                CASE WHEN cur.orders > 0 THEN cur.revenue / cur.orders ELSE 0 END AS aov,
                cur.dt_pct, cur.tp_pct, cur.in_pct,
                cur.discounts, cur.active_days,
                COALESCE(prev.revenue, 0) AS prev_revenue,
                COALESCE(prev.orders,  0) AS prev_orders
            FROM cur
            JOIN establishments e ON e.id = cur.establishment_id
            LEFT JOIN prev ON prev.establishment_id = cur.establishment_id
            ORDER BY cur.revenue DESC NULLS LAST
        """, (week_start, week_end, prev_s, prev_e))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@st.cache_data(ttl=300)
def load_trend(n_weeks: int = 8):
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT e.name,
                -- Monday-anchored week start (ISODOW: Mon=1)
                (DATE_TRUNC('week', fds.date + 1) - INTERVAL '1 day')::date AS week_start,
                SUM(fds.total_revenue)  AS revenue,
                SUM(fds.total_orders)   AS orders
            FROM features_daily_summary fds
            JOIN establishments e ON e.id = fds.establishment_id
            WHERE fds.date >= CURRENT_DATE - (%s * 7)
            GROUP BY e.name, week_start
            ORDER BY week_start, revenue DESC
        """, (n_weeks,))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@st.cache_data(ttl=120)
def load_daily(week_start: date):
    week_end = week_start + timedelta(days=7)
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT e.name, fds.date, fds.day_of_week,
                   fds.total_revenue, fds.total_orders
            FROM features_daily_summary fds
            JOIN establishments e ON e.id = fds.establishment_id
            WHERE fds.date >= %s AND fds.date < %s
            ORDER BY fds.date, fds.total_revenue DESC
        """, (week_start, week_end))
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@st.cache_data(ttl=600)
def load_dow_heatmap():
    conn = _connect()
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT e.name, fds.day_of_week,
                   AVG(fds.total_revenue)  AS avg_revenue,
                   AVG(fds.total_orders)   AS avg_orders
            FROM features_daily_summary fds
            JOIN establishments e ON e.id = fds.establishment_id
            GROUP BY e.name, fds.day_of_week
            ORDER BY e.name, fds.day_of_week
        """)
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_k(v: float) -> str:
    return f"${v/1000:.1f}K" if v >= 1000 else f"${v:.0f}"

def wow_parts(cur: float, prev: float):
    if not prev:
        return 0.0, "neu"
    pct = (cur - prev) / prev * 100
    return pct, "pos" if pct >= 0 else "neg"

def rank_emoji(r: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(r, f"<span style='color:#94a3b8;font-size:0.8rem'>#{r}</span>")


# ── Sidebar ───────────────────────────────────────────────────────────────────
weeks = load_available_weeks()
if not weeks:
    st.error("No feature data found in database.")
    st.stop()

with st.sidebar:
    st.markdown("### 📅 Report Period")
    sel_week = st.selectbox(
        "Week of",
        weeks,
        format_func=lambda d: d.strftime("%b %d, %Y"),
        label_visibility="collapsed",
    )
    st.markdown("---")
    n_trend_weeks = st.slider("Trend weeks", 4, 12, 8, label_visibility="collapsed")
    st.caption(f"Showing {n_trend_weeks}-week trend")
    st.markdown("---")
    st.markdown("**Laynes Chicken Fingers**")
    st.caption("Sales Intelligence Dashboard")
    st.caption("Refreshes every 5 min")

week_start = sel_week
week_end   = week_start + timedelta(days=7)

# ── Load ──────────────────────────────────────────────────────────────────────
summary  = load_week_summary(week_start)
trend    = load_trend(n_trend_weeks)
daily    = load_daily(week_start)
dow_data = load_dow_heatmap()

if not summary:
    st.warning(f"No data for week of {week_start.strftime('%b %d, %Y')}.")
    st.stop()

df   = pd.DataFrame(summary)
df_t = pd.DataFrame(trend)
df_d = pd.DataFrame(daily)
df_h = pd.DataFrame(dow_data)

for c in ["revenue", "orders", "aov", "prev_revenue", "prev_orders",
          "discounts", "dt_pct", "tp_pct", "in_pct"]:
    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

df["wow_pct"] = df.apply(
    lambda r: (r.revenue - r.prev_revenue) / r.prev_revenue * 100 if r.prev_revenue > 0 else 0.0,
    axis=1,
)
df["rank"]       = range(1, len(df) + 1)
df["name_short"] = df["name"].str.replace("LCF ", "", regex=False)

total_rev   = df["revenue"].sum()
total_ord   = int(df["orders"].sum())
prev_rev    = df["prev_revenue"].sum()
prev_ord    = int(df["prev_orders"].sum())
net_aov     = total_rev / total_ord if total_ord else 0
max_rev     = df["revenue"].max()
top_loc     = df.iloc[0]

week_label  = f"{week_start.strftime('%b %d')} – {(week_end - timedelta(days=1)).strftime('%b %d, %Y')}"
net_wow, net_wow_cls = wow_parts(total_rev, prev_rev)
net_wow_arrow = "▲" if net_wow >= 0 else "▼"
net_wow_color = "#10b981" if net_wow >= 0 else "#ef4444"


# ══════════════════════════════════════════════════════════════════════════════
# HEADER
# ══════════════════════════════════════════════════════════════════════════════
st.markdown(f"""
<div class="dash-header">
  <div class="dash-header-left">
    <div class="eyebrow">🍗 Laynes Chicken Fingers · Network Report</div>
    <div class="title">Sales Intelligence</div>
    <div class="subtitle">{week_label} &nbsp;·&nbsp; All 11 Locations</div>
  </div>
  <div class="dash-header-right">
    <div class="big-num">${total_rev:,.0f}</div>
    <div class="wow" style="color:{net_wow_color}">{net_wow_arrow} {abs(net_wow):.1f}% vs prior week</div>
    <div class="sub">Network Total Revenue</div>
  </div>
</div>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# KPI CARDS
# ══════════════════════════════════════════════════════════════════════════════
ord_wow, ord_wow_cls = wow_parts(total_ord, prev_ord)
ord_arrow = "▲" if ord_wow >= 0 else "▼"

top_wow, top_wow_cls = wow_parts(top_loc.revenue, top_loc.prev_revenue)
top_arrow = "▲" if top_wow >= 0 else "▼"

def kpi_card(accent, label, value, delta_text, delta_color):
    return f"""
    <div style="background:white;border-radius:14px;padding:1.1rem 1.3rem;
                box-shadow:0 2px 12px rgba(0,0,0,0.07);border-top:4px solid {accent};
                height:100%">
      <p style="font-size:0.72rem;font-weight:600;color:#999;text-transform:uppercase;
                letter-spacing:0.08em;margin:0 0 6px">{label}</p>
      <p style="font-size:1.7rem;font-weight:800;color:#1a1a2e;margin:0 0 4px;line-height:1.1">{value}</p>
      <span style="font-size:0.8rem;font-weight:500;color:{delta_color}">{delta_text}</span>
    </div>"""

k1, k2, k3, k4 = st.columns(4, gap="small")
with k1:
    st.markdown(kpi_card(
        "#6C63FF", "Network Revenue", f"${total_rev:,.0f}",
        f"{net_wow_arrow} {abs(net_wow):.1f}% vs prev week",
        "#10b981" if net_wow >= 0 else "#ef4444",
    ), unsafe_allow_html=True)
with k2:
    st.markdown(kpi_card(
        "#FF6B6B", "Total Orders", f"{total_ord:,}",
        f"{ord_arrow} {abs(ord_wow):.1f}% vs prev week",
        "#10b981" if ord_wow >= 0 else "#ef4444",
    ), unsafe_allow_html=True)
with k3:
    st.markdown(kpi_card(
        "#4ECDC4", "Avg Order Value", f"${net_aov:.2f}",
        "Network average per closed order", "#94a3b8",
    ), unsafe_allow_html=True)
with k4:
    st.markdown(kpi_card(
        "#FFD700", "🥇 Top Location", top_loc["name_short"],
        f"{fmt_k(top_loc['revenue'])}  ·  {top_arrow} {abs(top_wow):.1f}%",
        "#10b981" if top_wow >= 0 else "#ef4444",
    ), unsafe_allow_html=True)

st.markdown("<div style='margin-bottom:1rem'></div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# ROW 2: Leaderboard + 8-Week Trend
# ══════════════════════════════════════════════════════════════════════════════
col_lb, col_trend = st.columns([1, 1.65], gap="medium")

# ── Leaderboard ───────────────────────────────────────────────────────────────
with col_lb:
    lb_html = """<div style="background:white;border-radius:16px;padding:1.3rem 1.5rem;
                              box-shadow:0 2px 12px rgba(0,0,0,0.06);height:100%">
      <p style="font-size:0.95rem;font-weight:700;color:#1a1a2e;margin:0 0 0.8rem;
                letter-spacing:-0.01em">🏆 Location Leaderboard</p>"""

    for _, row in df.iterrows():
        r    = int(row["rank"])
        emp  = {1: "🥇", 2: "🥈", 3: "🥉"}.get(r, f"#{r}")
        pct  = row["revenue"] / max_rev * 100 if max_rev else 0
        bar_color = PALETTE[(r - 1) % len(PALETTE)]
        wow  = row["wow_pct"]
        warr = "&#9650;" if wow >= 0 else "&#9660;"
        wow_color = "#10b981" if wow >= 0 else "#ef4444"
        name = row["name_short"]
        rank_fs = "1.05rem" if r <= 3 else "0.8rem"
        rank_color = "#1a1a2e" if r <= 3 else "#94a3b8"
        border = "1px solid #F5F5F7" if r < len(df) else "none"

        lb_html += f"""
        <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:{border}">
          <span style="font-size:{rank_fs};font-weight:800;color:{rank_color};
                       width:26px;text-align:center;flex-shrink:0">{emp}</span>
          <span style="font-size:0.87rem;font-weight:600;color:#1a1a2e;
                       flex:1;min-width:80px;white-space:nowrap;overflow:hidden;
                       text-overflow:ellipsis">{name}</span>
          <div style="flex:1.4;min-width:50px">
            <div style="background:#F0F2F6;border-radius:99px;height:6px">
              <div style="width:{pct:.0f}%;height:6px;border-radius:99px;
                           background:{bar_color}"></div>
            </div>
          </div>
          <span style="font-size:0.87rem;font-weight:700;color:#1a1a2e;
                       width:64px;text-align:right;flex-shrink:0">{fmt_k(row['revenue'])}</span>
          <span style="font-size:0.77rem;font-weight:700;color:{wow_color};
                       width:62px;text-align:right;flex-shrink:0">{warr} {abs(wow):.1f}%</span>
        </div>"""

    lb_html += "</div>"
    st.markdown(lb_html, unsafe_allow_html=True)

# ── 8-Week Trend ──────────────────────────────────────────────────────────────
with col_trend:
    st.markdown(f"**📈 {n_trend_weeks}-Week Revenue Trend**")

    if not df_t.empty:
        df_t["revenue"]     = pd.to_numeric(df_t["revenue"], errors="coerce").fillna(0)
        df_t["week_label"]  = pd.to_datetime(df_t["week_start"]).dt.strftime("%b %d")
        df_t["name_short"]  = df_t["name"].str.replace("LCF ", "", regex=False)
        loc_order           = df["name_short"].tolist()

        fig = go.Figure()
        for i, loc in enumerate(loc_order):
            sub = df_t[df_t["name_short"] == loc].sort_values("week_start")
            if sub.empty:
                continue
            is_top3 = i < 3
            fig.add_trace(go.Scatter(
                name=loc,
                x=sub["week_label"],
                y=sub["revenue"],
                mode="lines+markers",
                line=dict(color=PALETTE[i % len(PALETTE)],
                          width=3 if is_top3 else 1.5),
                marker=dict(size=8 if is_top3 else 5,
                            symbol="circle",
                            line=dict(color="white", width=1.5 if is_top3 else 1)),
                opacity=1.0 if is_top3 else 0.65,
                hovertemplate=f"<b>{loc}</b><br>%{{x}}: $%{{y:,.0f}}<extra></extra>",
            ))

        # Highlight selected week via shape (add_vline breaks on categorical axes)
        selected_label = week_start.strftime("%b %d")
        all_week_labels = sorted(
            df_t["week_label"].unique(),
            key=lambda s: pd.to_datetime(s + " 2026", format="%b %d %Y", errors="coerce"),
        )
        if selected_label in all_week_labels:
            x_idx = all_week_labels.index(selected_label)
            fig.add_shape(
                type="line",
                x0=x_idx, x1=x_idx, y0=0, y1=1,
                xref="x", yref="paper",
                line=dict(color="#6C63FF", width=1.5, dash="dot"),
            )
            fig.add_annotation(
                x=x_idx, y=1.0, xref="x", yref="paper",
                text="▼ This week", showarrow=False,
                font=dict(size=10, color="#6C63FF"),
                yanchor="bottom",
            )

        fig.update_layout(
            plot_bgcolor="white", paper_bgcolor="white",
            height=370, margin=dict(l=0, r=10, t=8, b=35),
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02,
                xanchor="left", x=0, font=dict(size=9.5),
                bgcolor="rgba(0,0,0,0)",
                itemwidth=50,
            ),
            xaxis=dict(showgrid=False, tickfont=dict(size=11), title=""),
            yaxis=dict(
                showgrid=True, gridcolor="#F0F0F5",
                tickprefix="$", tickformat=",.0f",
                tickfont=dict(size=11), title="",
                zeroline=False,
            ),
            hovermode="x unified",
        )
        st.plotly_chart(fig, width="stretch", key="trend")


# ══════════════════════════════════════════════════════════════════════════════
# ROW 3: Channel Mix + AOV Ranking
# ══════════════════════════════════════════════════════════════════════════════
col_ch, col_aov = st.columns([1.6, 1], gap="medium")

with col_ch:
    st.markdown("**📊 Sales Channel Mix**")

    df_ch = df.copy().sort_values("revenue", ascending=True)

    fig_ch = go.Figure()
    for ch, color, label, emoji in [
        ("dt_pct",  "#45B7D1", "Drive-Through", "🚗"),
        ("tp_pct",  "#96CEB4", "Third Party",   "📦"),
        ("in_pct",  "#F7DC6F", "In-Store",      "🏪"),
    ]:
        fig_ch.add_trace(go.Bar(
            name=f"{emoji} {label}",
            x=df_ch[ch] * 100,
            y=df_ch["name_short"],
            orientation="h",
            marker_color=color,
            text=(df_ch[ch] * 100).apply(lambda v: f"{v:.0f}%" if v >= 8 else ""),
            textposition="inside",
            insidetextanchor="middle",
            textfont=dict(size=11, color="#1a1a2e"),
            hovertemplate="%{y}<br>" + label + ": %{x:.1f}%<extra></extra>",
        ))

    fig_ch.update_layout(
        barmode="stack",
        plot_bgcolor="white", paper_bgcolor="white",
        height=340, margin=dict(l=0, r=0, t=6, b=10),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02,
            xanchor="left", x=0, font=dict(size=11),
            bgcolor="rgba(0,0,0,0)",
        ),
        xaxis=dict(showgrid=False, ticksuffix="%", range=[0, 100],
                   tickfont=dict(size=11), title=""),
        yaxis=dict(showgrid=False, tickfont=dict(size=11), title=""),
    )
    st.plotly_chart(fig_ch, width="stretch", key="channel_mix")

with col_aov:
    st.markdown("**💰 Avg Order Value Ranking**")

    df_aov = df.copy().sort_values("aov", ascending=True)

    max_aov = df_aov["aov"].max()
    colors_aov = [
        f"rgba(108,99,255,{0.35 + 0.65 * v / max_aov})" if max_aov else "#6C63FF"
        for v in df_aov["aov"]
    ]

    fig_aov = go.Figure(go.Bar(
        x=df_aov["aov"],
        y=df_aov["name_short"],
        orientation="h",
        marker_color=colors_aov,
        text=df_aov["aov"].apply(lambda v: f"${v:.2f}"),
        textposition="outside",
        textfont=dict(size=11, color="#1a1a2e"),
        hovertemplate="%{y}: $%{x:.2f}<extra></extra>",
    ))
    fig_aov.update_layout(
        plot_bgcolor="white", paper_bgcolor="white",
        height=340, margin=dict(l=0, r=55, t=6, b=10),
        xaxis=dict(showgrid=True, gridcolor="#F0F0F5", tickprefix="$",
                   tickfont=dict(size=10), title=""),
        yaxis=dict(showgrid=False, tickfont=dict(size=11), title=""),
    )
    st.plotly_chart(fig_aov, width="stretch", key="aov_rank")


# ══════════════════════════════════════════════════════════════════════════════
# ROW 4: DOW Heatmap
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.markdown("**🗓️ Avg Daily Revenue by Day of Week (all-time)**")

if not df_h.empty:
    df_h["avg_revenue"] = pd.to_numeric(df_h["avg_revenue"], errors="coerce").fillna(0)
    df_h["day_label"]   = df_h["day_of_week"].map({i: v for i, v in enumerate(DOW_LABELS)})
    df_h["name_short"]  = df_h["name"].str.replace("LCF ", "", regex=False)

    pivot = df_h.pivot_table(
        index="name_short", columns="day_label", values="avg_revenue", fill_value=0
    ).reindex(columns=DOW_LABELS)

    loc_order_hm = df["name_short"].tolist()
    pivot = pivot.reindex([l for l in loc_order_hm if l in pivot.index])

    z_vals = pivot.values
    text_vals = [[f"${v:,.0f}" for v in row] for row in z_vals]

    fig_hm = go.Figure(go.Heatmap(
        z=z_vals,
        x=pivot.columns.tolist(),
        y=pivot.index.tolist(),
        colorscale=[
            [0.00, "#EEF2FF"],
            [0.20, "#A5B4FC"],
            [0.50, "#6C63FF"],
            [0.75, "#4338CA"],
            [1.00, "#1E1B4B"],
        ],
        text=text_vals,
        texttemplate="%{text}",
        textfont=dict(size=11),
        hovertemplate="<b>%{y}</b> · %{x}<br>Avg Revenue: $%{z:,.0f}<extra></extra>",
        showscale=True,
        colorbar=dict(
            tickprefix="$", tickformat=",.0f",
            thickness=10, len=0.85,
            tickfont=dict(size=10),
            title=dict(text="Avg $", font=dict(size=10), side="right"),
        ),
    ))
    fig_hm.update_layout(
        plot_bgcolor="white", paper_bgcolor="white",
        height=345, margin=dict(l=0, r=0, t=4, b=4),
        xaxis=dict(side="top", tickfont=dict(size=13, color="#1a1a2e"),
                   ticklen=0, title=""),
        yaxis=dict(tickfont=dict(size=11), title="", autorange="reversed"),
    )
    st.plotly_chart(fig_hm, width="stretch", key="dow_heatmap")


# ══════════════════════════════════════════════════════════════════════════════
# ROW 5: Daily Breakdown (selected week)
# ══════════════════════════════════════════════════════════════════════════════
if not df_d.empty:
    st.markdown("---")
    st.markdown(f"**📅 Daily Revenue — Week of {week_start.strftime('%b %d')}**")

    df_d["total_revenue"] = pd.to_numeric(df_d["total_revenue"], errors="coerce").fillna(0)
    df_d["name_short"]    = df_d["name"].str.replace("LCF ", "", regex=False)
    df_d["date_label"]    = pd.to_datetime(df_d["date"]).dt.strftime("%a %b %d")

    tab_grouped, tab_stacked, tab_table = st.tabs(["Grouped", "Stacked", "Table"])

    loc_order2 = df["name_short"].tolist()
    date_labels = sorted(df_d["date_label"].unique(),
                         key=lambda s: pd.to_datetime(s, format="%a %b %d"))

    with tab_grouped:
        fig_g = go.Figure()
        for i, loc in enumerate(loc_order2):
            sub = df_d[df_d["name_short"] == loc].sort_values("date")
            if sub.empty:
                continue
            fig_g.add_trace(go.Bar(
                name=loc,
                x=sub["date_label"],
                y=sub["total_revenue"],
                marker_color=PALETTE[i % len(PALETTE)],
                hovertemplate=f"<b>{loc}</b><br>%{{x}}: $%{{y:,.0f}}<extra></extra>",
            ))
        fig_g.update_layout(
            barmode="group",
            plot_bgcolor="white", paper_bgcolor="white",
            height=340, margin=dict(l=0, r=0, t=6, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        xanchor="left", x=0, font=dict(size=9.5),
                        bgcolor="rgba(0,0,0,0)"),
            xaxis=dict(showgrid=False, tickfont=dict(size=11), title="",
                       categoryorder="array", categoryarray=date_labels),
            yaxis=dict(showgrid=True, gridcolor="#F0F0F5",
                       tickprefix="$", tickformat=",.0f",
                       tickfont=dict(size=11), title=""),
        )
        st.plotly_chart(fig_g, width="stretch", key="daily_grouped")

    with tab_stacked:
        fig_s = go.Figure()
        for i, loc in enumerate(loc_order2):
            sub = df_d[df_d["name_short"] == loc].sort_values("date")
            if sub.empty:
                continue
            fig_s.add_trace(go.Bar(
                name=loc,
                x=sub["date_label"],
                y=sub["total_revenue"],
                marker_color=PALETTE[i % len(PALETTE)],
                hovertemplate=f"<b>{loc}</b><br>%{{x}}: $%{{y:,.0f}}<extra></extra>",
            ))
        fig_s.update_layout(
            barmode="stack",
            plot_bgcolor="white", paper_bgcolor="white",
            height=340, margin=dict(l=0, r=0, t=6, b=10),
            legend=dict(orientation="h", yanchor="bottom", y=1.02,
                        xanchor="left", x=0, font=dict(size=9.5),
                        bgcolor="rgba(0,0,0,0)"),
            xaxis=dict(showgrid=False, tickfont=dict(size=11), title="",
                       categoryorder="array", categoryarray=date_labels),
            yaxis=dict(showgrid=True, gridcolor="#F0F0F5",
                       tickprefix="$", tickformat=",.0f",
                       tickfont=dict(size=11), title=""),
        )
        st.plotly_chart(fig_s, width="stretch", key="daily_stacked")

    with tab_table:
        pivot_d = (
            df_d.pivot_table(
                index="name_short", columns="date_label",
                values="total_revenue", aggfunc="sum", fill_value=0,
            ).reindex(columns=[c for c in date_labels if c in
                                df_d["date_label"].unique()])
        )
        pivot_d.insert(0, "Week Total", pivot_d.sum(axis=1))
        pivot_d = pivot_d.sort_values("Week Total", ascending=False)
        pivot_d.index.name = "Location"

        styled = (
            pivot_d.style
            .format("${:,.0f}")
            .background_gradient(cmap="Blues", subset=["Week Total"])
            .set_properties(**{"font-size": "12px"})
        )
        st.dataframe(styled, width="stretch", height=430)


# ══════════════════════════════════════════════════════════════════════════════
# ROW 6: WoW Change Chart
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.markdown("**📊 Week-over-Week Revenue Change**")

fig_wow = go.Figure()
df_sorted_wow = df.sort_values("wow_pct", ascending=True)

colors_wow = [
    "#10b981" if v >= 0 else "#ef4444"
    for v in df_sorted_wow["wow_pct"]
]

fig_wow.add_trace(go.Bar(
    x=df_sorted_wow["wow_pct"],
    y=df_sorted_wow["name_short"],
    orientation="h",
    marker_color=colors_wow,
    text=df_sorted_wow["wow_pct"].apply(lambda v: f"+{v:.1f}%" if v >= 0 else f"{v:.1f}%"),
    textposition="outside",
    textfont=dict(size=12, color="#1a1a2e"),
    customdata=df_sorted_wow[["revenue", "prev_revenue"]].values,
    hovertemplate=(
        "<b>%{y}</b><br>"
        "This week: $%{customdata[0]:,.0f}<br>"
        "Prior week: $%{customdata[1]:,.0f}<br>"
        "Change: %{x:+.1f}%<extra></extra>"
    ),
))

fig_wow.add_shape(
    type="line",
    x0=0, x1=0, y0=0, y1=1,
    xref="x", yref="paper",
    line=dict(color="#94a3b8", width=1.5),
)
fig_wow.update_layout(
    plot_bgcolor="white", paper_bgcolor="white",
    height=340, margin=dict(l=0, r=70, t=6, b=10),
    xaxis=dict(showgrid=True, gridcolor="#F0F0F5",
               ticksuffix="%", tickfont=dict(size=11), title=""),
    yaxis=dict(showgrid=False, tickfont=dict(size=11), title=""),
)
st.plotly_chart(fig_wow, width="stretch", key="wow_chart")


# ── Footer ────────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="footer">
  Laynes Sales Intelligence &nbsp;·&nbsp; Data from Revel POS &nbsp;·&nbsp;
  Week of {week_label} &nbsp;·&nbsp; All times Central Time &nbsp;·&nbsp;
  Auto-refreshes every 5 min
</div>
""", unsafe_allow_html=True)
