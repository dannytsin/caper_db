-- Caper beehiiv archive — Postgres schema
-- Design notes:
--   * Every table keeps a `raw` JSONB of the source payload so beehiiv schema
--     drift never loses data; typed columns are a convenience projection.
--   * Post stats are append-only snapshots (they accumulate after send), so we
--     keep the engagement curve, not just the latest number.
--   * Run with: psql "$DATABASE_URL" -f schema.sql   (idempotent)

create table if not exists publications (
    id              text primary key,            -- pub_<uuid>
    name            text,
    url             text,
    raw             jsonb not null,
    first_seen_at   timestamptz not null default now(),
    last_synced_at  timestamptz not null default now()
);

create table if not exists authors (
    id    text primary key,                      -- beehiiv id, or slugified name fallback
    name  text not null
);

create table if not exists content_tags (
    slug     text primary key,
    display  text
);

create table if not exists posts (
    id                   text primary key,        -- post_<uuid>
    publication_id       text not null references publications(id),
    title                text,
    subtitle             text,
    subject_line         text,
    preview_text         text,
    slug                 text,
    status               text,                    -- draft | confirmed | archived | scheduled | published
    platform             text,                    -- web | email | both
    audience             text,                    -- free | premium | both
    featured             boolean,
    comments_state       text,
    social_share         text,
    thumbnail_url        text,
    web_url              text,
    editor_url           text,
    split_tested         boolean,
    publish_date         timestamptz,
    displayed_date       timestamptz,
    scheduled_at         timestamptz,
    beehiiv_created_at   timestamptz,
    beehiiv_updated_at   timestamptz,
    content_hash         text,                    -- hash of metadata, to skip unchanged upserts
    raw                  jsonb not null,
    first_seen_at        timestamptz not null default now(),
    last_synced_at       timestamptz not null default now()
);
create index if not exists posts_publication_idx on posts (publication_id);
create index if not exists posts_status_idx       on posts (status);
create index if not exists posts_publish_idx      on posts (publish_date desc);

create table if not exists post_authors (
    post_id    text references posts(id) on delete cascade,
    author_id  text references authors(id),
    primary key (post_id, author_id)
);

create table if not exists post_tags (
    post_id   text references posts(id) on delete cascade,
    tag_slug  text references content_tags(slug),
    primary key (post_id, tag_slug)
);

create table if not exists post_content (
    post_id              text primary key references posts(id) on delete cascade,
    free_web_html        text,
    free_email_html      text,
    free_rss_html        text,
    premium_web_html     text,
    premium_email_html   text,
    content_hash         text,
    fetched_at           timestamptz not null default now()
);

-- Append-only. One row per (post, sync run). "Latest" is a window query (see view below).
create table if not exists post_stats_snapshots (
    id                     bigserial primary key,
    post_id                text not null references posts(id) on delete cascade,
    captured_at            timestamptz not null default now(),
    -- email
    email_recipients       integer,
    email_delivered        integer,
    delivery_rate          numeric,
    open_rate              numeric,
    unique_opens           integer,
    total_opens            integer,
    click_rate             numeric,
    unique_clicks          integer,
    total_clicks           integer,
    unsubscribes           integer,
    spam_reports           integer,
    bounce_rate            numeric,
    soft_bounced           integer,
    hard_bounced           integer,
    -- web
    web_views              integer,
    web_clicks             integer,
    web_unique_clicks      integer,
    upgrades               integer,
    raw                    jsonb not null
);
create index if not exists stats_post_time_idx on post_stats_snapshots (post_id, captured_at desc);

-- Per-link click breakdown, also snapshotted.
create table if not exists post_link_clicks (
    id                   bigserial primary key,
    post_id              text not null references posts(id) on delete cascade,
    captured_at          timestamptz not null default now(),
    url                  text not null,
    email_total_clicks   integer,
    email_unique_clicks  integer,
    web_total_clicks     integer,
    web_unique_clicks    integer,
    total_clicks         integer,
    total_unique_clicks  integer
);
create index if not exists link_clicks_post_idx on post_link_clicks (post_id, captured_at desc);

-- Convenience: most recent stats snapshot per post.
create or replace view post_stats_latest as
select distinct on (post_id) *
from post_stats_snapshots
order by post_id, captured_at desc;


-- =====================================================================
-- Subscriber / growth / revenue domain
-- =====================================================================

-- Append-only daily snapshot of publication-level numbers. The REST stats
-- endpoint gives authoritative active counts + avg rates; new/churned/net and
-- earnings may be null (derive those from `subscriptions` in SQL).
create table if not exists publication_stats_snapshots (
    id                     bigserial primary key,
    publication_id         text not null references publications(id),
    captured_at            timestamptz not null default now(),
    active_subscriptions   integer,
    active_free            integer,
    active_premium         integer,
    average_open_rate      numeric,
    average_click_rate     numeric,
    total_sent             integer,
    total_delivered        integer,
    total_unique_opened    integer,
    total_clicked          integer,
    new_subscribers        integer,   -- may be null from REST; derivable from subscriptions
    churned_subscribers    integer,
    net_subscribers        integer,
    earnings_cents         integer,
    raw                    jsonb not null
);
create index if not exists pub_stats_time_idx on publication_stats_snapshots (publication_id, captured_at desc);

-- One row per subscriber. subscribed_on + unsubscribed_on make daily signups,
-- churn, and channel-mix-over-time derivable from this table alone.
create table if not exists subscriptions (
    id                text primary key,            -- sub_<uuid>
    publication_id    text not null references publications(id),
    email             text,                        -- raw address
    status            text,                        -- active | inactive | pending | needs_attention
    is_premium        boolean,
    acquisition_source text,                       -- collapsed "website: google.com / organic"
    utm_source        text,
    utm_medium        text,
    utm_campaign      text,
    utm_channel       text,
    referring_site    text,
    referral_code     text,
    stripe_customer_id text,
    tier_ids          text[],
    tags              jsonb,
    subscribed_on     timestamptz,
    unsubscribed_on   timestamptz,
    raw               jsonb not null,
    first_seen_at     timestamptz not null default now(),
    last_synced_at    timestamptz not null default now()
);
create index if not exists subs_pub_idx        on subscriptions (publication_id);
create index if not exists subs_subscribed_idx on subscriptions (subscribed_on);
create index if not exists subs_status_idx     on subscriptions (status);
create index if not exists subs_premium_idx    on subscriptions (is_premium);

create table if not exists tiers (
    id                  text primary key,          -- tier_<uuid>
    publication_id      text not null references publications(id),
    name                text,
    description         text,
    status              text,                      -- active | archived
    beehiiv_created_at  timestamptz,
    raw                 jsonb not null,
    last_synced_at      timestamptz not null default now()
);

create table if not exists tier_prices (
    id            text primary key,                -- price_<uuid>
    tier_id       text not null references tiers(id) on delete cascade,
    amount_cents  integer,
    currency      text,
    interval      text,                            -- month | quarter | year | one_time
    enabled       boolean
);

-- Convenience: latest publication snapshot.
create or replace view publication_stats_latest as
select distinct on (publication_id) *
from publication_stats_snapshots
order by publication_id, captured_at desc;


-- =====================================================================
-- Google Analytics 4 (caper.media web traffic)
-- =====================================================================
-- Upserted by (property_id, date[, channel]); recent days are re-pulled each
-- run because GA finalizes data over ~24-48h. NOTE: GA sessions/users differ
-- from beehiiv's website analytics (different definitions, GA loses
-- adblocked/consent-declined traffic). bounce_rate is a 0-1 ratio.

create table if not exists ga_daily (
    property_id              text not null,
    date                     date not null,
    sessions                 integer,
    active_users             integer,
    new_users                integer,
    screen_page_views        integer,
    engaged_sessions         integer,
    average_session_duration numeric,
    bounce_rate              numeric,
    conversions              numeric,
    raw                      jsonb not null,
    synced_at                timestamptz not null default now(),
    primary key (property_id, date)
);

create table if not exists ga_daily_by_channel (
    property_id    text not null,
    date           date not null,
    channel        text not null,
    sessions       integer,
    active_users   integer,
    new_users      integer,
    conversions    numeric,
    raw            jsonb not null,
    synced_at      timestamptz not null default now(),
    primary key (property_id, date, channel)
);
create index if not exists ga_channel_date_idx on ga_daily_by_channel (date);
