"""Bing Webmaster Tools URL Submission API.

POST https://ssl.bing.com/webmaster/api.svc/json/SubmitUrlbatch?apikey=KEY with {siteUrl, urlList}.
At most 500 URLs per batch, and never more than the site's remaining daily quota
(GetUrlSubmissionQuota). Microsoft recommends IndexNow for new integrations; this exists for sites
that already use the Webmaster API key.
"""

from __future__ import annotations

from indexkit.http import HttpCaller
from indexkit.submitters.base import Status, SubmissionResult, body_excerpt, classify

API_BASE = "https://ssl.bing.com/webmaster/api.svc/json"


class BingSubmitter:
    name = "bing"
    max_batch = 500

    def __init__(self, caller: HttpCaller, *, site_url: str, api_key: str):
        if not api_key:
            raise ValueError("Bing Webmaster API key is required")
        self.caller = caller
        self.site_url = site_url
        self.api_key = api_key
        self._remaining: int | None = None

    def remaining_quota(self) -> int:
        # The key travels as a query parameter (the API's design); passing it via params keeps it out of
        # the URL string that indexkit logs.
        response = self.caller.request(
            "GET", f"{API_BASE}/GetUrlSubmissionQuota", params={"apikey": self.api_key, "siteUrl": self.site_url}
        )
        response.raise_for_status()
        payload = response.json().get("d")
        if not isinstance(payload, dict) or "DailyQuota" not in payload:
            raise ValueError(f"unexpected GetUrlSubmissionQuota response: {body_excerpt(response.text)}")
        self._remaining = int(payload["DailyQuota"])
        return self._remaining

    def submit(self, urls: list[str]) -> list[SubmissionResult]:
        remaining = self._remaining if self._remaining is not None else self.remaining_quota()
        sendable, over_quota = urls[:remaining], urls[remaining:]

        results = [SubmissionResult(u, self.name, Status.SKIPPED, None, "daily quota exhausted") for u in over_quota]
        if not sendable:
            return results

        response = self.caller.request(
            "POST",
            f"{API_BASE}/SubmitUrlbatch",
            params={"apikey": self.api_key},
            json={"siteUrl": self.site_url, "urlList": sendable},
        )
        status = classify(response.status_code)
        if status is Status.ACCEPTED:
            self._remaining = remaining - len(sendable)
        detail = "received" if status is Status.ACCEPTED else body_excerpt(response.text) or f"HTTP {response.status_code}"
        return [SubmissionResult(u, self.name, status, response.status_code, detail) for u in sendable] + results
