"""Orchestrator: pull beehiiv data into Postgres.

  python sync.py                  # posts: metadata + content + a stats snapshot
  python sync.py --stats-only     # posts: append a fresh stats snapshot only
  python sync.py --subscribers    # subscribers + tiers (full upsert)
  python sync.py --snapshot       # one publication-level stats snapshot (cheap, daily)
  python sync.py --subscribers --snapshot   # flags combine; run in order

Idempotent: posts/subscribers/tiers upsert by id; stats snapshots are append-only.
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

import db
from beehiiv import Beehiiv, DEFAULT_EXPANDS


def sync_posts(client, conn, pub_id, stats_only: bool, status):
    with conn.cursor() as cur:
        if not stats_only:
            pub = client.get_publication(pub_id) or {"id": pub_id}
            pub.setdefault("id", pub_id)
            db.upsert_publication(cur, pub)
            conn.commit()

        expand = ["stats"] if stats_only else DEFAULT_EXPANDS
        # full content expands make a 100-post page multi-MB → beehiiv 503s; page small
        page_size = 100 if stats_only else 10
        n = 0
        for post in client.iter_posts(pub_id, expand=expand, status=status, page_size=page_size):
            n += 1
            db.upsert_post(cur, post, pub_id)
            if not stats_only:
                db.upsert_content(cur, post)
            db.insert_stats_snapshot(cur, post)
            conn.commit()
            print(f"  post [{n}] {post.get('title','(untitled)')[:55]}")
    print(f"posts: {n} archived"
          + (" (stats only)" if stats_only else "") + ".")


def sync_subscribers(client, conn, pub_id):
    with conn.cursor() as cur:
        _ensure_publication(client, cur, pub_id)
        conn.commit()

        tiers = client.list_tiers(pub_id)
        for tier in tiers:
            db.upsert_tier(cur, tier, pub_id)
        conn.commit()

        n = 0
        for sub in client.iter_subscriptions(pub_id):
            db.upsert_subscription(cur, sub, pub_id)
            n += 1
            if n % 500 == 0:
                conn.commit()
                print(f"  subscribers … {n}")
        conn.commit()
    print(f"subscribers: {n} upserted, {len(tiers)} tiers.")


def sync_snapshot(client, conn, pub_id):
    with conn.cursor() as cur:
        _ensure_publication(client, cur, pub_id)
        stats = client.get_publication_stats(pub_id)
        # stats may be nested under a "stats" key or be the publication object itself
        payload = stats.get("stats") if isinstance(stats.get("stats"), dict) else stats
        db.insert_publication_stats_snapshot(cur, pub_id, payload)
        conn.commit()
    print(f"snapshot: active_subscriptions="
          f"{payload.get('active_subscriptions') or payload.get('current_active_subscribers')}.")


def _ensure_publication(client, cur, pub_id):
    pub = client.get_publication(pub_id) or {"id": pub_id}
    pub.setdefault("id", pub_id)
    db.upsert_publication(cur, pub)


GA_DAILY_METRICS = ["sessions", "activeUsers", "newUsers", "screenPageViews",
                    "engagedSessions", "averageSessionDuration", "bounceRate", "conversions"]
GA_CHANNEL_METRICS = ["sessions", "activeUsers", "newUsers", "conversions"]


def sync_ga(conn, property_id, days):
    from ga import GA  # lazy: google libs only needed when GA is used
    client = GA(property_id)
    start = f"{days}daysAgo"
    daily = client.run(["date"], GA_DAILY_METRICS, start)
    channels = client.run(["date", "sessionDefaultChannelGroup"], GA_CHANNEL_METRICS, start)
    with conn.cursor() as cur:
        for r in daily:
            db.upsert_ga_daily(cur, property_id, r)
        for r in channels:
            db.upsert_ga_channel(cur, property_id, r)
        conn.commit()
    print(f"ga: {len(daily)} daily rows, {len(channels)} channel rows (last {days}d).")


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", help="posts filter: draft|scheduled|published|archived|confirmed")
    ap.add_argument("--stats-only", action="store_true", help="posts: append stats snapshots only")
    ap.add_argument("--subscribers", action="store_true", help="sync subscribers + tiers")
    ap.add_argument("--snapshot", action="store_true", help="append a publication stats snapshot")
    ap.add_argument("--daily", action="store_true",
                    help="full daily run: posts + content + stats + subscribers + tiers + snapshot + GA")
    ap.add_argument("--ga", action="store_true", help="sync Google Analytics (needs GA_PROPERTY_ID + creds)")
    ap.add_argument("--ga-days", type=int, default=30,
                    help="GA lookback window in days (default 30; use a big number for the initial backfill)")
    args = ap.parse_args()

    api_key = os.environ.get("BEEHIIV_API_KEY")
    pub_id = os.environ.get("BEEHIIV_PUBLICATION_ID")
    dsn = os.environ.get("DATABASE_URL")
    ga_property = os.environ.get("GA_PROPERTY_ID")
    interval = float(os.environ.get("BEEHIIV_MIN_INTERVAL", "0.5"))

    missing = [k for k, v in {
        "BEEHIIV_API_KEY": api_key,
        "BEEHIIV_PUBLICATION_ID": pub_id,
        "DATABASE_URL": dsn,
    }.items() if not v]
    if missing:
        print(f"Missing env vars: {', '.join(missing)} (see .env.example)", file=sys.stderr)
        return 2

    client = Beehiiv(api_key, min_interval=interval)

    # Default (no domain flags) → posts. Flags combine and run in order.
    with db.connect(dsn) as conn:
        if args.daily:
            sync_posts(client, conn, pub_id, False, args.status)
            sync_subscribers(client, conn, pub_id)
            sync_snapshot(client, conn, pub_id)
            if ga_property:
                try:
                    sync_ga(conn, ga_property, args.ga_days)
                except Exception as e:  # never let GA break the beehiiv sync
                    print(f"ga: skipped ({e})")
            else:
                print("ga: skipped (GA_PROPERTY_ID not set)")
        else:
            do_posts = not (args.subscribers or args.snapshot or args.ga) or args.stats_only
            if do_posts:
                sync_posts(client, conn, pub_id, args.stats_only, args.status)
            if args.subscribers:
                sync_subscribers(client, conn, pub_id)
            if args.snapshot:
                sync_snapshot(client, conn, pub_id)
            if args.ga:
                if not ga_property:
                    print("GA_PROPERTY_ID not set (see .env.example)", file=sys.stderr)
                    return 2
                sync_ga(conn, ga_property, args.ga_days)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
