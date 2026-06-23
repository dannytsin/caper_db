"""Caper analytics dashboard (Streamlit, internal).

Reads the same Railway Postgres the sync writes to. Prefers DATABASE_URL_RO
(read-only role) if set, else DATABASE_URL. Run locally:
    streamlit run dashboard/dashboard.py
Deploy: a second Railway service with root dir `dashboard/`.
"""
import os

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

st.set_page_config(page_title="Caper Dashboard", page_icon="📰", layout="wide")


@st.cache_resource
def engine():
    url = os.environ.get("DATABASE_URL_RO") or os.environ.get("DATABASE_URL")
    if not url:
        st.error("No DATABASE_URL set."); st.stop()
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True)


@st.cache_data(ttl=600)
def q(sql: str) -> pd.DataFrame:
    with engine().connect() as c:
        return pd.read_sql_query(text(sql), c)


def hbar(df, cat, val, fmt="Q"):
    return (
        alt.Chart(df).mark_bar(color="#c0392b").encode(
            x=alt.X(f"{val}:{fmt}", title=None),
            y=alt.Y(f"{cat}:N", sort="-x", title=None),
            tooltip=list(df.columns),
        ).properties(height=min(26 * len(df) + 30, 520))
    )


def _gate():
    """Shared-password gate for the deployed (internal) dashboard. If
    DASHBOARD_PASSWORD is unset (e.g. local), the dashboard is open."""
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return
    st.title("📰 Caper Dashboard")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")
    if submitted:
        if entered == pw:
            st.session_state["authed"] = True
            st.rerun()
        st.error("Wrong password")
    st.stop()


_gate()

st.title("📰 Caper — Newsletter Analytics")
lu = q("select max(last_synced_at) lu from posts")["lu"][0]
st.caption(f"Live from Railway Postgres · last sync {lu:%Y-%m-%d %H:%M} UTC")

# ---- headline KPIs -------------------------------------------------------
k = q("select active_subscriptions, active_free, active_premium, average_open_rate from publication_stats_latest").iloc[0]
arr = q("""
    select coalesce(sum(p.amount_cents),0)/100.0 arr
    from subscriptions s
    join lateral unnest(s.tier_ids) tid on true
    join tier_prices p on p.tier_id = tid and p.enabled and p.interval = 'year'
    where s.is_premium
""")["arr"][0]
nposts = q("select count(*) n from posts")["n"][0]

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Active subscribers", f"{int(k.active_subscriptions):,}")
c2.metric("Premium", f"{int(k.active_premium):,}")
c3.metric("Free", f"{int(k.active_free):,}")
c4.metric("Avg open rate", f"{k.average_open_rate:.1f}%")
c5.metric("Est. ARR", f"${arr:,.0f}")
c6.metric("Posts archived", f"{int(nposts):,}")

tab_growth, tab_acq, tab_posts, tab_authors, tab_rev = st.tabs(
    ["📈 Growth", "📣 Acquisition", "✉️ Posts", "✍️ Authors", "💳 Revenue"]
)

# ---- Growth --------------------------------------------------------------
with tab_growth:
    signups = q("""
        select subscribed_on::date d, count(*) n
        from subscriptions where subscribed_on is not null
        group by 1 order by 1
    """)
    signups["cumulative"] = signups["n"].cumsum()
    left, right = st.columns(2)
    with left:
        st.subheader("Total subscribers over time")
        st.line_chart(signups.set_index("d")["cumulative"], height=300)
    with right:
        st.subheader("Daily signups")
        st.bar_chart(signups.set_index("d")["n"], height=300, color="#c0392b")

    st.subheader("Active subscribers (daily snapshot)")
    snaps = q("""
        select captured_at::date d, max(active_subscriptions) active,
               max(active_premium) premium
        from publication_stats_snapshots group by 1 order by 1
    """)
    if len(snaps) > 1:
        st.line_chart(snaps.set_index("d")[["active", "premium"]], height=260)
    else:
        st.info("Only one snapshot so far — this curve fills in as the daily cron runs.")

# ---- Acquisition ---------------------------------------------------------
with tab_acq:
    st.subheader("Signups by acquisition source")
    src = q("""
        select coalesce(nullif(acquisition_source,''),'(unknown)') source, count(*) signups
        from subscriptions group by 1 order by signups desc limit 15
    """)
    st.altair_chart(hbar(src, "source", "signups"), use_container_width=True)

    st.subheader("Signups by UTM source")
    utm = q("""
        select coalesce(nullif(utm_source,''),'(direct)') utm_source, count(*) signups
        from subscriptions group by 1 order by signups desc limit 12
    """)
    st.altair_chart(hbar(utm, "utm_source", "signups"), use_container_width=True)

# ---- Posts ---------------------------------------------------------------
with tab_posts:
    st.subheader("Top posts by open rate")
    top = q("""
        select p.title, ps.open_rate
        from posts p join post_stats_latest ps on ps.post_id = p.id
        where ps.email_recipients > 500
        order by ps.open_rate desc limit 12
    """)
    st.altair_chart(hbar(top, "title", "open_rate"), use_container_width=True)

    st.subheader("Open rate over time (by send)")
    ot = q("""
        select p.publish_date::date d, ps.open_rate, p.audience, p.title
        from posts p join post_stats_latest ps on ps.post_id = p.id
        where ps.email_recipients > 0 and p.publish_date is not null
    """)
    scatter = alt.Chart(ot).mark_circle(size=70, opacity=0.8).encode(
        x=alt.X("d:T", title=None),
        y=alt.Y("open_rate:Q", title="open rate %"),
        color=alt.Color("audience:N", scale=alt.Scale(scheme="set1")),
        tooltip=["title", "open_rate", "d", "audience"],
    ).properties(height=320)
    st.altair_chart(scatter, use_container_width=True)

    st.subheader("All sent posts")
    posts = q("""
        select p.publish_date::date as date, p.title, p.audience,
               string_agg(distinct a.name, ', ') as authors,
               ps.email_recipients as recipients, ps.open_rate, ps.click_rate, ps.upgrades
        from posts p
        left join post_stats_latest ps on ps.post_id = p.id
        left join post_authors pa on pa.post_id = p.id
        left join authors a on a.id = pa.author_id
        where p.status in ('confirmed','published')
        group by p.id, p.publish_date, p.title, p.audience,
                 ps.email_recipients, ps.open_rate, ps.click_rate, ps.upgrades
        order by p.publish_date desc nulls last
    """)
    st.dataframe(posts, use_container_width=True, hide_index=True)

# ---- Authors -------------------------------------------------------------
with tab_authors:
    st.subheader("Author performance")
    au = q("""
        select a.name,
               count(distinct p.id) as posts,
               round(avg(ps.open_rate)::numeric, 1) as avg_open_rate,
               round(avg(ps.click_rate)::numeric, 2) as avg_click_rate,
               sum(ps.upgrades) as upgrades
        from authors a
        join post_authors pa on pa.author_id = a.id
        join posts p on p.id = pa.post_id
        left join post_stats_latest ps on ps.post_id = p.id
        group by a.name
        having count(distinct p.id) > 0
        order by posts desc
    """)
    st.dataframe(au, use_container_width=True, hide_index=True)
    st.altair_chart(
        hbar(au.sort_values("avg_open_rate", ascending=False).head(15), "name", "avg_open_rate"),
        use_container_width=True,
    )

# ---- Revenue -------------------------------------------------------------
with tab_rev:
    a, b = st.columns(2)
    a.metric("Est. annual recurring revenue", f"${arr:,.0f}")
    b.metric("Premium subscribers", f"{int(k.active_premium):,}")
    st.subheader("Tiers & pricing")
    tiers = q("""
        select t.name as tier, t.status,
               (p.amount_cents/100.0) as price, p.interval, p.enabled
        from tiers t join tier_prices p on p.tier_id = t.id
        order by t.status, price desc
    """)
    st.dataframe(tiers, use_container_width=True, hide_index=True)
    st.subheader("Open rate: paid vs free posts")
    aud = q("""
        select p.audience, round(avg(ps.open_rate)::numeric,1) as avg_open_rate, count(*) as posts
        from posts p join post_stats_latest ps on ps.post_id = p.id
        where ps.email_recipients > 0
        group by p.audience order by avg_open_rate desc
    """)
    st.dataframe(aud, use_container_width=True, hide_index=True)
