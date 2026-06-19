"""Google Analytics 4 Data API client (read-only reporting).

Auth: a service-account JSON, provided either as the raw JSON string in
GA_SERVICE_ACCOUNT_JSON (best for Railway/CI) or via GOOGLE_APPLICATION_CREDENTIALS
pointing at a file. The service account needs Viewer on the GA4 property, and the
property's numeric id goes in GA_PROPERTY_ID (NOT the G-XXXX measurement id).

google libs are imported lazily so the beehiiv-only path doesn't require them.
"""
from __future__ import annotations

import json
import os

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


def _client():
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    raw = os.environ.get("GA_SERVICE_ACCOUNT_JSON")
    if raw:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=SCOPES
        )
        return BetaAnalyticsDataClient(credentials=creds)
    # else fall back to GOOGLE_APPLICATION_CREDENTIALS (Application Default Creds)
    return BetaAnalyticsDataClient()


class GA:
    def __init__(self, property_id: str):
        self.property_id = str(property_id)
        self.client = _client()

    def run(self, dimensions, metrics, start: str, end: str = "today"):
        """Run one report; return rows as list[dict] keyed by dimension/metric name.

        start/end accept 'YYYY-MM-DD', 'NdaysAgo', 'today', or 'yesterday'.
        """
        from google.analytics.data_v1beta.types import (
            DateRange, Dimension, Metric, RunReportRequest,
        )
        req = RunReportRequest(
            property=f"properties/{self.property_id}",
            dimensions=[Dimension(name=d) for d in dimensions],
            metrics=[Metric(name=m) for m in metrics],
            date_ranges=[DateRange(start_date=start, end_date=end)],
            limit=100000,
        )
        resp = self.client.run_report(req)
        dims = [h.name for h in resp.dimension_headers]
        mets = [h.name for h in resp.metric_headers]
        out = []
        for r in resp.rows:
            row = {dims[i]: r.dimension_values[i].value for i in range(len(dims))}
            for i in range(len(mets)):
                row[mets[i]] = r.metric_values[i].value
            out.append(row)
        return out
