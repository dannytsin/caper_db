# Caper beehiiv archive

Pulls **every post** from the Caper beehiiv publication into Postgres for
long-term storage: metadata, full content (free/premium × web/email), and an
append-only time series of engagement stats.

## Why direct API, not MCP
The beehiiv MCP server is for interactive/agent use. This pipeline runs headless
on a schedule, so it hits the **beehiiv v2 REST API** directly with a static API
key — deterministic, retry-safe, cheap. (MCP field names differ from the raw API;
this code follows the raw API.)

## Data model

**Post domain**
| Table | What |
|---|---|
| `publications` | publication metadata |
| `posts` | one row per post id, typed columns + full `raw` JSONB |
| `post_content` | free/premium × web/email/rss HTML bodies |
| `post_stats_snapshots` | **append-only** — one row per post per sync run |
| `post_link_clicks` | per-URL click breakdown, snapshotted |
| `authors` / `post_authors`, `content_tags` / `post_tags` | normalized lookups |

**Subscriber / growth / revenue domain**
| Table | What |
|---|---|
| `subscriptions` | one row per subscriber: status, tier, source, `subscribed_on`, `unsubscribed_on` |
| `publication_stats_snapshots` | **append-only** daily list-level numbers (active/free/premium, rates, earnings) |
| `tiers` / `tier_prices` | premium tier + pricing structure |

**Google Analytics 4 domain** (caper.media web traffic)
| Table | What |
|---|---|
| `ga_daily` | per-day sessions, users, pageviews, engagement, bounce, conversions |
| `ga_daily_by_channel` | per-day by GA channel group (Organic / Direct / Referral / …) |

Upserted by `(property_id, date[, channel])`; recent days re-pull each run (GA
finalizes over ~24-48h). GA sessions ≠ beehiiv's web numbers — different
definitions; don't reconcile, treat as two lenses. Join GA to growth on `date`.

Views: `post_stats_latest`, `publication_stats_latest` (most recent snapshot each).

Stats accumulate after send, so snapshotting each run preserves the engagement
curve. **Signups + acquisition channel** come from `subscriptions` (`GROUP BY
date_trunc('day', subscribed_on)` / `acquisition_source`) — no need to have
snapshotted since launch. **Churn + net growth** come from the daily
`publication_stats_snapshots` active-count curve, because beehiiv's API does not
expose a per-subscriber unsubscribe date (`unsubscribed_on` is usually NULL).

> Verified against the live REST API: the public `?expand[]=stats` object carries
> active/free/premium counts + avg rates, but **no earnings and no new/churned/net**.
> Estimate revenue from `tier_prices` × active premium subs; `stripe_customer_id`
> on each subscriber links to Stripe. Typed columns are best-effort; `raw` keeps all.

> Note: Caper publishes paid/unpaid **twins** of the same article as separate
> beehiiv posts (distinct ids). Split them by **`audience`** (`free` = unpaid,
> `premium` = paid) — the REST API returns `content_tags` empty, so tags aren't
> a reliable signal.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env          # fill in BEEHIIV_API_KEY and DATABASE_URL
psql "$DATABASE_URL" -f schema.sql
```
Create the API key at app.beehiiv.com → Settings → API (this is **not** the MCP token).

## Run
```bash
python sync.py                 # posts: metadata + content + a stats snapshot
python sync.py --stats-only    # posts: append fresh stats snapshots only
python sync.py --subscribers   # subscribers + tiers (full upsert; ~8k rows)
python sync.py --snapshot      # one publication-level stats snapshot (cheap)
python sync.py --ga            # Google Analytics (last 30 days)
python sync.py --ga --ga-days 400   # GA initial backfill (~13 months)
python sync.py --daily         # everything (beehiiv + GA if GA_PROPERTY_ID set)
```

## Google Analytics 4 setup (one-time)
1. **Google Cloud** → create/select a project → APIs & Services → enable the
   **Google Analytics Data API**.
2. Create a **Service Account** → Keys → **Add key → JSON** → download it.
3. **GA4 Admin → Property Access Management** → add the service account's email
   (`...@...iam.gserviceaccount.com`) as a **Viewer**.
4. **GA4 Admin → Property Settings** → copy the numeric **Property ID** (e.g.
   `123456789`) — NOT the `G-XXXX` measurement id.
5. Set env vars: `GA_PROPERTY_ID` and `GA_SERVICE_ACCOUNT_JSON` (the JSON file's
   contents on one line). On Railway, paste both as service variables.
6. Backfill once: `python sync.py --ga --ga-days 400`. After that `--daily` keeps
   it current (it runs GA automatically when `GA_PROPERTY_ID` is set, and silently
   skips it when not).

## Scheduling — GitHub Actions (no servers)
`.github/workflows/sync.yml` runs **once a day (09:00 UTC)**: full posts +
content + a stats snapshot, then subscribers + tiers + a publication snapshot.
cron is UTC — edit the `cron:` line to shift the hour.

Add three repo secrets: `BEEHIIV_API_KEY`, `BEEHIIV_PUBLICATION_ID`,
`DATABASE_URL`. Trigger a first run manually from the Actions tab ("Run workflow").
A daily write also keeps a Supabase free project from auto-pausing.

## Chat with the archive
A CLI where Claude answers questions by writing read-only SQL.

```bash
psql "$DATABASE_URL" -f roles.sql      # once, as owner — creates caper_readonly (edit the password)
export ANTHROPIC_API_KEY=...
export DATABASE_URL_RO=postgresql://caper_readonly:...@host/db
python chat.py
```
```
you › how did paid vs unpaid open rates compare over the last month?
you › which 5 posts had the highest click rate, and who wrote them?
```
The agent connects as **caper_readonly** (SELECT-only, 15s statement timeout), so
a generated query can't write or escape the archive — that role is the safety
boundary. SQL-only for now; semantic search over article bodies is a later add.

## Files
```
schema.sql    Postgres DDL (idempotent)
roles.sql     read-only role for the chat agent (run once as owner)
beehiiv.py    polite v2 API client (throttle + retry + pagination)
db.py         payload → schema mapping, upserts, snapshot inserts
sync.py       CLI orchestrator
chat.py       Claude + read-only SQL tool — CLI chat over the archive
.github/workflows/sync.yml   scheduled full + stats-only syncs
```
