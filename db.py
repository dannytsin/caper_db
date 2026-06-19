"""Persistence: map a raw beehiiv post payload into the Postgres schema.

Everything is upserted by id and the full payload is stored as JSONB, so the
typed columns are a best-effort projection — if beehiiv renames a field, the
data still lands in `raw` and we can backfill later.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb


# ---- small helpers -------------------------------------------------------

def parse_ts(value):
    """beehiiv timestamps arrive as unix ints or ISO strings; normalize to UTC."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        if value.isdigit():
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()


def _norm_authors(raw_authors):
    """authors may be ['Name', ...] or [{'id','name'}, ...]. Yield (id, name)."""
    for a in raw_authors or []:
        if isinstance(a, dict):
            name = a.get("name") or "Unknown"
            yield a.get("id") or slugify(name), name
        else:
            yield slugify(str(a)), str(a)


def _norm_tags(raw_tags):
    """content_tags may be ['slug', ...] or [{'slug','display'}, ...]. Yield (slug, display)."""
    for t in raw_tags or []:
        if isinstance(t, dict):
            slug = t.get("slug") or slugify(t.get("display", "tag"))
            yield slug, t.get("display") or slug
        else:
            yield slugify(str(t)), str(t)


# ---- writes --------------------------------------------------------------

def upsert_publication(cur, pub: dict):
    cur.execute(
        """
        insert into publications (id, name, url, raw, last_synced_at)
        values (%s, %s, %s, %s, now())
        on conflict (id) do update set
            name = excluded.name,
            url = excluded.url,
            raw = excluded.raw,
            last_synced_at = now()
        """,
        (pub["id"], pub.get("name"), pub.get("url"), Jsonb(pub)),
    )


def upsert_post(cur, post: dict, publication_id: str):
    pid = post["id"]
    meta_hash = _hash({k: v for k, v in post.items() if k not in ("stats", "content")})

    cur.execute(
        """
        insert into posts (
            id, publication_id, title, subtitle, subject_line, preview_text, slug,
            status, platform, audience, featured, comments_state, social_share,
            thumbnail_url, web_url, editor_url, split_tested,
            publish_date, displayed_date, scheduled_at,
            beehiiv_created_at, beehiiv_updated_at, content_hash, raw, last_synced_at
        ) values (
            %s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,%s, now()
        )
        on conflict (id) do update set
            publication_id = excluded.publication_id,
            title = excluded.title, subtitle = excluded.subtitle,
            subject_line = excluded.subject_line, preview_text = excluded.preview_text,
            slug = excluded.slug, status = excluded.status, platform = excluded.platform,
            audience = excluded.audience, featured = excluded.featured,
            comments_state = excluded.comments_state, social_share = excluded.social_share,
            thumbnail_url = excluded.thumbnail_url, web_url = excluded.web_url,
            editor_url = excluded.editor_url, split_tested = excluded.split_tested,
            publish_date = excluded.publish_date, displayed_date = excluded.displayed_date,
            scheduled_at = excluded.scheduled_at,
            beehiiv_created_at = excluded.beehiiv_created_at,
            beehiiv_updated_at = excluded.beehiiv_updated_at,
            content_hash = excluded.content_hash, raw = excluded.raw,
            last_synced_at = now()
        """,
        (
            pid, publication_id, post.get("title"), post.get("subtitle"),
            _first(post, "subject_line", "email_subject_line"),
            _first(post, "preview_text", "email_preview_text"),
            post.get("slug"), post.get("status"), post.get("platform"),
            post.get("audience"), post.get("featured"), post.get("comments_state"),
            post.get("social_share"), post.get("thumbnail_url"),
            _first(post, "web_url", "url"), post.get("editor_url"),
            post.get("split_tested"),
            parse_ts(_first(post, "publish_date", "scheduled_at")),
            parse_ts(post.get("displayed_date")),
            parse_ts(post.get("scheduled_at")),
            parse_ts(_first(post, "created", "created_at")),
            parse_ts(post.get("updated_at")),
            meta_hash, Jsonb(post),
        ),
    )

    # authors + join
    cur.execute("delete from post_authors where post_id = %s", (pid,))
    for aid, name in _norm_authors(post.get("authors")):
        cur.execute(
            "insert into authors (id, name) values (%s, %s) "
            "on conflict (id) do update set name = excluded.name",
            (aid, name),
        )
        cur.execute(
            "insert into post_authors (post_id, author_id) values (%s, %s) "
            "on conflict do nothing",
            (pid, aid),
        )

    # tags + join
    cur.execute("delete from post_tags where post_id = %s", (pid,))
    for slug, display in _norm_tags(post.get("content_tags")):
        cur.execute(
            "insert into content_tags (slug, display) values (%s, %s) "
            "on conflict (slug) do update set display = excluded.display",
            (slug, display),
        )
        cur.execute(
            "insert into post_tags (post_id, tag_slug) values (%s, %s) "
            "on conflict do nothing",
            (pid, slug),
        )

    return meta_hash


def upsert_content(cur, post: dict):
    content = post.get("content") or {}
    free = content.get("free") or {}
    premium = content.get("premium") or {}
    payload = (
        free.get("web"), free.get("email"), free.get("rss"),
        premium.get("web"), premium.get("email"),
    )
    if not any(payload):
        return
    cur.execute(
        """
        insert into post_content (
            post_id, free_web_html, free_email_html, free_rss_html,
            premium_web_html, premium_email_html, content_hash, fetched_at
        ) values (%s,%s,%s,%s,%s,%s,%s, now())
        on conflict (post_id) do update set
            free_web_html = excluded.free_web_html,
            free_email_html = excluded.free_email_html,
            free_rss_html = excluded.free_rss_html,
            premium_web_html = excluded.premium_web_html,
            premium_email_html = excluded.premium_email_html,
            content_hash = excluded.content_hash,
            fetched_at = now()
        """,
        (post["id"], *payload, _hash(payload)),
    )


def insert_stats_snapshot(cur, post: dict):
    stats = post.get("stats") or {}
    if not stats:
        return
    email = stats.get("email") or {}
    web = stats.get("web") or {}
    # upgrades (free→paid conversions) is a top-level int on stats, not under web
    _upg = stats.get("upgrades")
    upgrades = _upg if isinstance(_upg, int) else _first(_upg or {}, "total", "count", "total_upgrades")

    cur.execute(
        """
        insert into post_stats_snapshots (
            post_id, email_recipients, email_delivered, delivery_rate, open_rate,
            unique_opens, total_opens, click_rate, unique_clicks, total_clicks,
            unsubscribes, spam_reports, bounce_rate, soft_bounced, hard_bounced,
            web_views, web_clicks, web_unique_clicks, upgrades, raw
        ) values (
            %s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s
        )
        """,
        (
            post["id"],
            _first(email, "recipients", "total_sent"),
            _first(email, "delivered", "total_delivered"),
            _first(email, "delivery_rate"),
            _first(email, "open_rate"),
            _first(email, "unique_opens", "total_unique_opened"),
            _first(email, "opens", "total_opened"),
            _first(email, "click_rate"),
            _first(email, "unique_clicks", "total_unique_email_clicked_raw"),
            _first(email, "clicks", "total_email_clicked_raw"),
            _first(email, "unsubscribes", "total_unsubscribes"),
            _first(email, "spam_reports", "total_spam_reported"),
            _first(email, "bounce_rate"),
            _first(email, "soft_bounced", "total_soft_bounced"),
            _first(email, "hard_bounced", "total_hard_bounced"),
            _first(web, "views", "total_web_viewed"),
            _first(web, "clicks", "total_web_clicked"),
            _first(web, "unique_clicks", "total_unique_web_clicked"),
            upgrades,
            Jsonb(stats),
        ),
    )

    # per-link clicks (stats.clicks[] on the raw API)
    for link in stats.get("clicks") or []:
        e = link.get("email") or {}
        w = link.get("web") or {}
        tot = link.get("total") or {}
        cur.execute(
            """
            insert into post_link_clicks (
                post_id, url, email_total_clicks, email_unique_clicks,
                web_total_clicks, web_unique_clicks, total_clicks, total_unique_clicks
            ) values (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                post["id"], link.get("url"),
                _first(e, "total_clicked", "clicks"),
                _first(e, "unique_clicked", "unique_clicks"),
                _first(w, "total_clicked", "clicks"),
                _first(w, "unique_clicked", "unique_clicks"),
                _first(tot, "total_clicked", "clicks"),
                _first(tot, "unique_clicked", "unique_clicks"),
            ),
        )


def _to_cents(value):
    """earnings may arrive as cents (int) or a '$11,740.50' string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = re.sub(r"[^0-9.]", "", str(value))
    if not s:
        return None
    try:
        return round(float(s) * 100)
    except ValueError:
        return None


def insert_publication_stats_snapshot(cur, publication_id: str, stats: dict):
    """Append a point-in-time publication snapshot. Tolerant of REST vs curated shapes."""
    cur.execute(
        """
        insert into publication_stats_snapshots (
            publication_id, active_subscriptions, active_free, active_premium,
            average_open_rate, average_click_rate, total_sent, total_delivered,
            total_unique_opened, total_clicked, new_subscribers, churned_subscribers,
            net_subscribers, earnings_cents, raw
        ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            publication_id,
            _first(stats, "active_subscriptions", "current_active_subscribers", "active_subscriptions_count"),
            _first(stats, "active_free_subscriptions", "active_free"),
            _first(stats, "active_premium_subscriptions", "active_premium"),
            _first(stats, "average_open_rate", "open_rate"),
            _first(stats, "average_click_rate", "click_rate"),
            _first(stats, "total_sent"),
            _first(stats, "total_delivered"),
            _first(stats, "total_unique_opened"),
            _first(stats, "total_clicked"),
            _first(stats, "new_subscribers"),
            _first(stats, "churned_subscribers"),
            _first(stats, "net_subscribers"),
            _to_cents(_first(stats, "earnings_cents", "earnings")),
            Jsonb(stats),
        ),
    )


def _acq_source(sub: dict):
    """beehiiv REST returns utm_* separately (no collapsed source). Rebuild a
    readable 'channel: source / medium' string like the dashboard shows."""
    if sub.get("acquisition_source"):
        return sub["acquisition_source"]
    chan = sub.get("utm_channel") or "unknown"
    src = sub.get("utm_source") or "(direct)"
    med = sub.get("utm_medium") or "(none)"
    return f"{chan}: {src} / {med}"


def upsert_subscription(cur, sub: dict, publication_id: str):
    tiers = sub.get("tiers") or sub.get("subscription_premium_tiers") or []
    tier_ids = [t.get("id") if isinstance(t, dict) else str(t) for t in tiers]
    is_premium = bool(tier_ids) or _first(sub, "subscription_tier", "tier") == "premium"

    cur.execute(
        """
        insert into subscriptions (
            id, publication_id, email, status, is_premium, acquisition_source,
            utm_source, utm_medium, utm_campaign, utm_channel, referring_site,
            referral_code, stripe_customer_id, tier_ids, tags, subscribed_on,
            unsubscribed_on, raw, last_synced_at
        ) values (%s,%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s, now())
        on conflict (id) do update set
            email = excluded.email, status = excluded.status,
            is_premium = excluded.is_premium,
            acquisition_source = excluded.acquisition_source,
            utm_source = excluded.utm_source, utm_medium = excluded.utm_medium,
            utm_campaign = excluded.utm_campaign, utm_channel = excluded.utm_channel,
            referring_site = excluded.referring_site, referral_code = excluded.referral_code,
            stripe_customer_id = excluded.stripe_customer_id,
            tier_ids = excluded.tier_ids, tags = excluded.tags,
            subscribed_on = excluded.subscribed_on, unsubscribed_on = excluded.unsubscribed_on,
            raw = excluded.raw, last_synced_at = now()
        """,
        (
            sub["id"], publication_id, sub.get("email"), sub.get("status"), is_premium,
            _acq_source(sub),
            sub.get("utm_source"), sub.get("utm_medium"), sub.get("utm_campaign"),
            sub.get("utm_channel"), _first(sub, "referring_site", "referrer_url"),
            sub.get("referral_code"), sub.get("stripe_customer_id"),
            tier_ids or None, Jsonb(sub.get("tags") or []),
            parse_ts(_first(sub, "created", "subscribed_on", "created_at")),
            parse_ts(_first(sub, "unsubscribed_on", "unsubscribed_at", "deactivated_on")),
            Jsonb(sub),
        ),
    )


def upsert_tier(cur, tier: dict, publication_id: str):
    cur.execute(
        """
        insert into tiers (id, publication_id, name, description, status, beehiiv_created_at, raw, last_synced_at)
        values (%s,%s,%s,%s,%s,%s,%s, now())
        on conflict (id) do update set
            name = excluded.name, description = excluded.description,
            status = excluded.status, beehiiv_created_at = excluded.beehiiv_created_at,
            raw = excluded.raw, last_synced_at = now()
        """,
        (
            tier["id"], publication_id, tier.get("name"), tier.get("description"),
            tier.get("status"), parse_ts(_first(tier, "created_at", "created")), Jsonb(tier),
        ),
    )
    for price in tier.get("prices") or []:
        if not price.get("id"):
            continue
        cur.execute(
            """
            insert into tier_prices (id, tier_id, amount_cents, currency, interval, enabled)
            values (%s,%s,%s,%s,%s,%s)
            on conflict (id) do update set
                amount_cents = excluded.amount_cents, currency = excluded.currency,
                interval = excluded.interval, enabled = excluded.enabled
            """,
            (
                price["id"], tier["id"], price.get("amount_cents"),
                price.get("currency"), price.get("interval"), price.get("enabled"),
            ),
        )


def _ga_date(v):
    """GA returns the date dimension as 'YYYYMMDD'."""
    try:
        return datetime.strptime(v, "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _gi(v):
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _gf(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def upsert_ga_daily(cur, property_id: str, row: dict):
    cur.execute(
        """
        insert into ga_daily (
            property_id, date, sessions, active_users, new_users, screen_page_views,
            engaged_sessions, average_session_duration, bounce_rate, conversions, raw, synced_at
        ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        on conflict (property_id, date) do update set
            sessions = excluded.sessions, active_users = excluded.active_users,
            new_users = excluded.new_users, screen_page_views = excluded.screen_page_views,
            engaged_sessions = excluded.engaged_sessions,
            average_session_duration = excluded.average_session_duration,
            bounce_rate = excluded.bounce_rate, conversions = excluded.conversions,
            raw = excluded.raw, synced_at = now()
        """,
        (
            property_id, _ga_date(row.get("date")),
            _gi(row.get("sessions")), _gi(row.get("activeUsers")), _gi(row.get("newUsers")),
            _gi(row.get("screenPageViews")), _gi(row.get("engagedSessions")),
            _gf(row.get("averageSessionDuration")), _gf(row.get("bounceRate")),
            _gf(row.get("conversions")), Jsonb(row),
        ),
    )


def upsert_ga_channel(cur, property_id: str, row: dict):
    cur.execute(
        """
        insert into ga_daily_by_channel (
            property_id, date, channel, sessions, active_users, new_users, conversions, raw, synced_at
        ) values (%s,%s,%s,%s,%s,%s,%s,%s, now())
        on conflict (property_id, date, channel) do update set
            sessions = excluded.sessions, active_users = excluded.active_users,
            new_users = excluded.new_users, conversions = excluded.conversions,
            raw = excluded.raw, synced_at = now()
        """,
        (
            property_id, _ga_date(row.get("date")),
            row.get("sessionDefaultChannelGroup") or "(unknown)",
            _gi(row.get("sessions")), _gi(row.get("activeUsers")),
            _gi(row.get("newUsers")), _gf(row.get("conversions")), Jsonb(row),
        ),
    )


def connect(dsn: str):
    return psycopg.connect(dsn)
