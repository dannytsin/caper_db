"""Thin, polite client for the beehiiv v2 REST API.

Built against the documented v2 contract (https://developers.beehiiv.com).
Note: field names here are the *raw API* names, which differ from the beehiiv
MCP server's renamed fields — the API is the source of truth for this pipeline.
"""
from __future__ import annotations

import time
import requests

BASE = "https://api.beehiiv.com/v2"

# All content variants the posts endpoint can inline via expand[].
CONTENT_EXPANDS = [
    "free_web_content",
    "free_email_content",
    "free_rss_content",
    "premium_web_content",
    "premium_email_content",
]
DEFAULT_EXPANDS = ["stats", *CONTENT_EXPANDS]


class Beehiiv:
    def __init__(self, api_key: str, min_interval: float = 0.5, timeout: int = 30):
        if not api_key:
            raise ValueError("BEEHIIV_API_KEY is required")
        self.s = requests.Session()
        self.s.headers.update(
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )
        self.min_interval = min_interval
        self.timeout = timeout
        self._last = 0.0

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last
        if dt < self.min_interval:
            time.sleep(self.min_interval - dt)
        self._last = time.monotonic()

    def _get(self, path: str, params=None) -> dict:
        last_exc = None
        for attempt in range(5):
            self._throttle()
            r = self.s.get(f"{BASE}{path}", params=params, timeout=self.timeout)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                last_exc = requests.HTTPError(f"{r.status_code} on {path}")
                continue
            r.raise_for_status()
            return r.json()
        if last_exc:
            raise last_exc
        raise RuntimeError(f"giving up on {path} after retries")

    def get_publication(self, pub_id: str) -> dict:
        return self._get(f"/publications/{pub_id}").get("data", {})

    def get_publication_stats(self, pub_id: str) -> dict:
        """Publication object with its aggregate stats expanded."""
        data = self._get(f"/publications/{pub_id}", params=[("expand[]", "stats")])
        return data.get("data", data)

    def list_tiers(self, pub_id: str) -> list[dict]:
        # prices only come back with expand[]=prices
        return self._get(
            f"/publications/{pub_id}/tiers", params=[("expand[]", "prices")]
        ).get("data", [])

    def iter_subscriptions(self, pub_id: str, status: str | None = None, page_size: int = 100):
        """Yield every subscriber, paginating fully (oldest first).

        Cursor-based: beehiiv forbids offset (`page`) pagination beyond page 100,
        i.e. past 10k subscribers ("PAGINATION_LIMIT_EXCEEDED"). We follow the
        `next_cursor` the API returns and stop when `has_more` is false.
        """
        cursor: str | None = None
        while True:
            params = [
                ("limit", page_size),
                ("order_by", "created"),
                ("direction", "asc"),
                ("expand[]", "subscription_premium_tiers"),
            ]
            if status:
                params.append(("status", status))
            if cursor:
                params.append(("cursor", cursor))
            data = self._get(f"/publications/{pub_id}/subscriptions", params=params)
            for sub in data.get("data", []):
                yield sub
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break

    def iter_posts(self, pub_id: str, expand=None, status: str | None = None, page_size: int = 100):
        """Yield every post for a publication, paginating fully.

        expand defaults to stats + all content variants, so one pass captures
        metadata, body, and engagement together.
        """
        expand = DEFAULT_EXPANDS if expand is None else expand
        page = 1
        while True:
            params = [
                ("limit", page_size),
                ("page", page),
                ("order_by", "created"),
                ("direction", "asc"),
            ]
            params += [("expand[]", e) for e in expand]
            if status:
                params.append(("status", status))
            data = self._get(f"/publications/{pub_id}/posts", params=params)
            for post in data.get("data", []):
                yield post
            total_pages = data.get("total_pages") or 1
            if page >= total_pages:
                break
            page += 1
