"""Chat with the beehiiv archive — a CLI REPL.

Claude (claude-opus-4-8) answers questions by writing read-only SQL against the
archive. It connects as the caper_readonly role (see roles.sql), so the worst a
generated query can do is time out. Structured questions only for now
(counts, rates, dates, trends, keyword lookups) — semantic search is a later add.

  export ANTHROPIC_API_KEY=...           # your Anthropic key
  export DATABASE_URL_RO=postgresql://caper_readonly:...@host/db
  python chat.py
"""
from __future__ import annotations

import json
import os
import re
import sys

import anthropic
import psycopg
from dotenv import load_dotenv

MODEL = "claude-opus-4-8"
ROW_CAP = 200

SCHEMA_DOC = """\
You answer questions about the Caper newsletter (a beehiiv publication) by
querying a Postgres archive. You have ONE tool: query_sql. Write a single
read-only SELECT (or WITH ... SELECT) and read the rows back.

Tables:
- posts(id, publication_id, title, subtitle, subject_line, preview_text, slug,
        status, platform, audience, featured, web_url, publish_date,
        beehiiv_created_at, beehiiv_updated_at, raw jsonb, first_seen_at, last_synced_at)
- post_content(post_id, free_web_html, free_email_html, premium_web_html,
        premium_email_html, ...)   -- article bodies; large, avoid SELECT *
- post_stats_snapshots(post_id, captured_at, email_recipients, email_delivered,
        delivery_rate, open_rate, unique_opens, total_opens, click_rate,
        unique_clicks, total_clicks, unsubscribes, bounce_rate, web_views,
        upgrades, ...)             -- APPEND-ONLY time series (stats change after send)
- post_link_clicks(post_id, captured_at, url, total_clicks, ...)
- authors(id, name) + post_authors(post_id, author_id)
- content_tags(slug, display) + post_tags(post_id, tag_slug)

Subscriber / growth / revenue:
- subscriptions(id, email, status, is_premium, acquisition_source, utm_source,
        utm_medium, utm_channel, utm_campaign, referring_site, referral_code,
        stripe_customer_id, tier_ids, subscribed_on, unsubscribed_on, ...) -- ONE per subscriber
- publication_stats_snapshots(publication_id, captured_at, active_subscriptions,
        active_free, active_premium, average_open_rate, average_click_rate,
        earnings_cents, new_subscribers, churned_subscribers, net_subscribers, ...)
        -- APPEND-ONLY daily snapshot of list-level numbers
- tiers(id, name, status, ...) + tier_prices(tier_id, amount_cents, currency, interval, enabled)

Google Analytics (caper.media web traffic):
- ga_daily(property_id, date, sessions, active_users, new_users, screen_page_views,
        engaged_sessions, average_session_duration, bounce_rate, conversions)
- ga_daily_by_channel(property_id, date, channel, sessions, active_users, new_users, conversions)
        -- channel = GA default channel group (Organic Search, Direct, Referral, ...)

Views:
- post_stats_latest        -- most recent snapshot per post (use for "current" post numbers)
- publication_stats_latest -- most recent publication-level snapshot

Key facts:
- For a post's CURRENT open_rate/clicks etc., join post_stats_latest. For trends
  over time, use post_stats_snapshots and group by captured_at.
- Caper publishes paid/unpaid TWINS of the same article as SEPARATE posts with
  distinct ids. Split by `audience`: 'free' = unpaid, 'premium' = paid. The REST
  API leaves content_tags empty, so do NOT use tags for paid/unpaid. Titles
  aren't unique either.
- Rates (open_rate, click_rate, average_open_rate, ...) are PERCENTAGES (59.4 = 59.4%).
- SIGNUPS / channel come from `subscriptions`: daily signups = count grouped by
  date_trunc('day', subscribed_on); channel mix = group by acquisition_source
  (or utm_source / utm_channel). beehiiv's API does NOT expose an unsubscribe date,
  so unsubscribed_on is usually NULL.
- CHURN / net growth come from the daily active-count curve, NOT from subscriptions:
  use publication_stats_snapshots (today's active_subscriptions minus the prior day).
  active_free / active_premium are there too.
- REVENUE: there is no earnings field in the API. Estimate from tier_prices
  (amount_cents) × active premium subs. Per-sub tier is in subscriptions.tier_ids /
  is_premium; stripe_customer_id links a subscriber to Stripe.
- WEB TRAFFIC: ga_daily / ga_daily_by_channel are Google Analytics for caper.media.
  bounce_rate is a 0-1 ratio (×100 for %). GA sessions are a DIFFERENT measure from
  beehiiv's web numbers — don't reconcile them. Join GA to growth on `date` (e.g.
  GA sessions vs subscriptions signups per day) for funnel questions.
- Join authors via post_authors; tags via post_tags. Titles are not unique keys.
  Subscriber/growth data has NO author dimension (authors are post-level only).

Rules:
- Only SELECT. Never write. Always cap rows with LIMIT (<= 200) unless aggregating.
- When you have the data, answer in plain prose. Show the SQL you ran only if asked.
- If a query errors, read the message and fix the SQL; don't give up after one try.
"""

SELECT_OK = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)

QUERY_TOOL = {
    "name": "query_sql",
    "description": "Run a single read-only SELECT against the Caper archive and return the rows as JSON.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "A single SELECT or WITH...SELECT statement."}
        },
        "required": ["sql"],
    },
}


def run_sql(conn, sql: str) -> str:
    """Execute one read-only statement and return JSON rows (capped)."""
    stripped = sql.strip().rstrip(";")
    if not SELECT_OK.match(stripped) or ";" in stripped:
        return json.dumps({"error": "Only a single SELECT/WITH statement is allowed."})
    try:
        with conn.cursor() as cur:
            cur.execute(stripped)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchmany(ROW_CAP)
        conn.rollback()  # keep the session clean; no writes ever committed
        data = [dict(zip(cols, r)) for r in rows]
        return json.dumps(
            {"row_count": len(data), "truncated": len(data) == ROW_CAP, "rows": data},
            default=str,
        )
    except Exception as e:  # surface the DB error so Claude can self-correct
        conn.rollback()
        return json.dumps({"error": str(e)})


def answer(client, conn, history) -> None:
    """Run the agentic loop for one user turn, streaming text as it arrives."""
    while True:
        with client.messages.stream(
            model=MODEL,
            max_tokens=8192,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": SCHEMA_DOC, "cache_control": {"type": "ephemeral"}}],
            tools=[QUERY_TOOL],
            messages=history,
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
            final = stream.get_final_message()

        history.append({"role": "assistant", "content": final.content})
        if final.stop_reason != "tool_use":
            print()
            return

        results = []
        for block in final.content:
            if block.type == "tool_use" and block.name == "query_sql":
                print(f"\n  \033[2m⮑ querying…\033[0m", flush=True)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": run_sql(conn, block.input["sql"]),
                })
        history.append({"role": "user", "content": results})


def main() -> int:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL_RO")
    if not dsn:
        print("Set DATABASE_URL_RO (the caper_readonly connection string).", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2

    client = anthropic.Anthropic()
    conn = psycopg.connect(dsn, autocommit=False)
    history = []

    print("Caper archive chat. Ask about posts, authors, open rates, trends. Ctrl-D to exit.\n")
    while True:
        try:
            q = input("\033[1myou ›\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        history.append({"role": "user", "content": q})
        try:
            answer(client, conn, history)
        except anthropic.APIError as e:
            print(f"\n[API error: {e}]")
        print()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
