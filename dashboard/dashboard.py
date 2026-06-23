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

RED = "#c0392b"
REGULARS = ("Annie Armstrong", "Emma Orlow", "Chris Crowley")  # exclude from contributor view


@st.cache_resource
def engine():
    url = os.environ.get("DATABASE_URL_RO") or os.environ.get("DATABASE_URL")
    if not url:
        st.error("No DATABASE_URL set."); st.stop()
    # ignore the .env.example placeholder if it leaks in
    if "@host:" in url or "CHANGE_ME" in url:
        url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True)


@st.cache_data(ttl=600)
def q(sql: str) -> pd.DataFrame:
    with engine().connect() as c:
        return pd.read_sql_query(text(sql), c)


def trend(df, x, y, ytitle=""):
    base = alt.Chart(df).encode(
        x=alt.X(f"{x}:T", title=None),
        y=alt.Y(f"{y}:Q", title=ytitle),
        tooltip=list(df.columns),
    )
    return (base.mark_line(color=RED) + base.mark_circle(color=RED, size=28)).properties(height=340)


def hbar(df, cat, val):
    return alt.Chart(df).mark_bar(color=RED).encode(
        x=alt.X(f"{val}:Q", title=None),
        y=alt.Y(f"{cat}:N", sort="-x", title=None),
        tooltip=list(df.columns),
    ).properties(height=min(26 * len(df) + 30, 560))


def _gate():
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
k = q("select active_subscriptions, active_premium, average_open_rate from publication_stats_latest").iloc[0]
total_leads = q("select count(*) n from subscriptions")["n"][0]
arr = q("""
    select coalesce(sum(p.amount_cents),0)/100.0 arr
    from subscriptions s
    join lateral unnest(s.tier_ids) tid on true
    join tier_prices p on p.tier_id = tid and p.enabled and p.interval = 'year'
    where s.is_premium
""")["arr"][0]
nposts = q("select count(*) n from posts")["n"][0]

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Total leads", f"{int(total_leads):,}")
c2.metric("Active subscribers", f"{int(k.active_subscriptions):,}")
c3.metric("Paying subscribers", f"{int(k.active_premium):,}")
c4.metric("Avg open rate", f"{k.average_open_rate:.1f}%")
c5.metric("Est. ARR", f"${arr:,.0f}")
c6.metric("Posts", f"{int(nposts):,}")

t1, t2, t3, t4, t5 = st.tabs(
    ["📈 Lead growth", "💳 Paid growth", "⚖️ Paid vs Unpaid", "🌟 Contributors", "✍️ By author"]
)

# ---- 1. Lead growth (TOTAL leads, all-time) ------------------------------
with t1:
    st.subheader("Total leads over time")
    st.caption("Every subscriber ever acquired (active + churned), cumulative by signup date.")
    leads = q("""
        select subscribed_on::date d, count(*) n
        from subscriptions where subscribed_on is not null
        group by 1 order by 1
    """)
    leads["total_leads"] = leads["n"].cumsum()
    st.altair_chart(trend(leads[["d", "total_leads"]], "d", "total_leads", "total leads"),
                    use_container_width=True)
    st.altair_chart(
        alt.Chart(leads).mark_circle(color=RED, size=40, opacity=0.6).encode(
            x=alt.X("d:T", title=None), y=alt.Y("n:Q", title="new leads / day"),
            tooltip=["d", "n"]).properties(height=240),
        use_container_width=True,
    )

# ---- 2. Paid growth (paying subscribers over time) -----------------------
with t2:
    st.subheader("Paying subscribers over time")
    st.caption("Cumulative conversions (free→paid upgrades) by send date — proxy for paid growth, "
               "since beehiiv's API doesn't expose per-subscriber upgrade dates. Live count uses snapshots.")
    conv = q("""
        select p.publish_date::date d, sum(ps.upgrades) n
        from posts p join post_stats_latest ps on ps.post_id = p.id
        where p.publish_date is not null and coalesce(ps.upgrades,0) > 0
        group by 1 order by 1
    """)
    conv["cumulative_conversions"] = conv["n"].cumsum()
    st.altair_chart(trend(conv[["d", "cumulative_conversions"]], "d", "cumulative_conversions",
                          "cumulative conversions"), use_container_width=True)
    st.subheader("Live paying count (daily snapshots)")
    prem = q("""select captured_at::date d, max(active_premium) paying
               from publication_stats_snapshots group by 1 order by 1""")
    st.line_chart(prem.set_index("d")["paying"], height=240, color=RED)

# ---- 3. Open rate: paid vs unpaid flag (recent only) ---------------------
with t3:
    st.subheader("Open rate by paid / unpaid email")
    st.caption("Only posts tagged paidemail / unpaidemail, from when the unpaid split began "
               "(earlier untagged sends are excluded).")
    cutoff = "(select min(p2.publish_date) from post_tags pt2 join posts p2 on p2.id=pt2.post_id where pt2.tag_slug='unpaidemail')"
    summary = q(f"""
        select pt.tag_slug as flag, round(avg(ps.open_rate)::numeric,1) as avg_open_rate,
               count(*) as posts, round(avg(ps.click_rate)::numeric,2) as avg_click_rate,
               sum(ps.upgrades) as conversions
        from post_tags pt
        join posts p on p.id = pt.post_id
        join post_stats_latest ps on ps.post_id = pt.post_id
        where pt.tag_slug in ('paidemail','unpaidemail')
          and ps.email_recipients > 0 and p.publish_date >= {cutoff}
        group by pt.tag_slug order by avg_open_rate desc
    """)
    a, b = st.columns([1, 2])
    a.dataframe(summary, use_container_width=True, hide_index=True)
    over = q(f"""
        select p.publish_date::date d, ps.open_rate, pt.tag_slug as flag, p.title
        from post_tags pt
        join posts p on p.id = pt.post_id
        join post_stats_latest ps on ps.post_id = pt.post_id
        where pt.tag_slug in ('paidemail','unpaidemail')
          and ps.email_recipients > 0 and p.publish_date >= {cutoff}
    """)
    b.altair_chart(
        alt.Chart(over).mark_circle(size=80, opacity=0.85).encode(
            x=alt.X("d:T", title=None), y=alt.Y("open_rate:Q", title="open rate %"),
            color=alt.Color("flag:N", scale=alt.Scale(domain=["paidemail", "unpaidemail"],
                            range=[RED, "#2c7fb8"])),
            tooltip=["title", "open_rate", "flag", "d"]).properties(height=300),
        use_container_width=True,
    )

# ---- 4. Contributor conversions (excl. the 3 regulars) -------------------
with t4:
    st.subheader("Conversions by contributor piece")
    st.caption(f"Pieces NOT written by {', '.join(REGULARS)}, ranked by conversions (upgrades).")
    names = "', '".join(REGULARS)
    contrib = q(f"""
        select p.publish_date::date as date, p.title,
               string_agg(distinct a.name, ', ') as authors,
               coalesce(ps.upgrades,0) as conversions, ps.open_rate
        from posts p
        join post_authors pa on pa.post_id = p.id
        join authors a on a.id = pa.author_id
        left join post_stats_latest ps on ps.post_id = p.id
        where p.id not in (
            select pa2.post_id from post_authors pa2 join authors a2 on a2.id = pa2.author_id
            where a2.name in ('{names}')
        )
        group by p.id, p.publish_date, p.title, ps.upgrades, ps.open_rate
        order by conversions desc nulls last
        limit 40
    """)
    st.altair_chart(hbar(contrib.head(15)[["title", "conversions"]], "title", "conversions"),
                    use_container_width=True)
    st.dataframe(contrib, use_container_width=True, hide_index=True)

# ---- 5. Open rate by author (filterable) ---------------------------------
with t5:
    st.subheader("Open rate & conversions by author")
    st.caption("Avg open rate split by paid vs unpaid email, plus conversions from the unpaid email.")
    by_author = q("""
        with pf as (
            select p.id post_id,
                   max((pt.tag_slug='unpaidemail')::int) is_unpaid,
                   max((pt.tag_slug='paidemail')::int) is_paid
            from posts p left join post_tags pt on pt.post_id = p.id
            group by p.id
        )
        select a.name as author,
               count(distinct p.id) as posts,
               round(avg(ps.open_rate) filter (where pf.is_unpaid=1)::numeric,1) as unpaid_open_rate,
               round(avg(ps.open_rate) filter (where pf.is_paid=1)::numeric,1) as paid_open_rate,
               sum(ps.upgrades) filter (where pf.is_unpaid=1) as unpaid_conversions
        from authors a
        join post_authors pa on pa.author_id = a.id
        join posts p on p.id = pa.post_id
        join pf on pf.post_id = p.id
        left join post_stats_latest ps on ps.post_id = p.id
        group by a.name
        having count(distinct p.id) > 0
        order by posts desc
    """)
    pick = st.selectbox("Filter author", ["All authors"] + by_author["author"].tolist())
    view = by_author if pick == "All authors" else by_author[by_author["author"] == pick]
    st.dataframe(view, use_container_width=True, hide_index=True)
