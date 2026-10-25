"""Google APIs: Search Console sitemap submission and the Indexing API.

**Search Console API** (general purpose): `sitemaps.submit` tells Google about a sitemap for a verified
property. It does not submit individual URLs; there is no general URL-submission API for Google Search.

**Indexing API** (restricted): officially only for pages with JobPosting or BroadcastEvent (in a
VideoObject) structured data. Default quota 200 publish requests/day per project; use outside that
scope can lead to access being revoked. This submitter refuses to run unless the caller confirms the
pages are eligible.

Credentials come from a service account JSON file with Owner access to the Search Console property.
`google-auth` is an optional dependency (`pip install "search-engine-indexing-toolkit[google]"`).
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

from indexkit.http import HttpCaller
from indexkit.submitters.base import Status, SubmissionResult, body_excerpt, classify

SEARCH_CONSOLE_SCOPE = "https://www.googleapis.com/auth/webmasters"
INDEXING_SCOPE = "https://www.googleapis.com/auth/indexing"
SEARCH_CONSOLE_BASE = "https://www.googleapis.com/webmasters/v3"
INDEXING_PUBLISH_URL = "https://indexing.googleapis.com/v3/urlNotifications:publish"
INDEXING_DAILY_QUOTA = 200

TokenProvider = Callable[[], str]


def service_account_token_provider(credentials_file: str, scope: str) -> TokenProvider:
    """Returns a callable that yields a valid access token, refreshing it when it expires."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise RuntimeError('Google support needs: pip install "search-engine-indexing-toolkit[google]"') from exc

    credentials = service_account.Credentials.from_service_account_file(credentials_file, scopes=[scope])  # type: ignore[no-untyped-call]

    def token() -> str:
        if not credentials.valid:
            credentials.refresh(Request())
        return str(credentials.token)

    return token


class SearchConsoleSitemaps:
    """Submit and list sitemaps for a Search Console property (URL-prefix or sc-domain:)."""

    def __init__(self, caller: HttpCaller, *, site_url: str, token: TokenProvider):
        self.caller = caller
        self.site_url = site_url
        self.token = token

    def _endpoint(self, feedpath: str | None = None) -> str:
        url = f"{SEARCH_CONSOLE_BASE}/sites/{quote(self.site_url, safe='')}/sitemaps"
        return f"{url}/{quote(feedpath, safe='')}" if feedpath else url

    def submit(self, sitemap_url: str) -> SubmissionResult:
        response = self.caller.request("PUT", self._endpoint(sitemap_url), headers={"Authorization": f"Bearer {self.token()}"})
        status = classify(response.status_code)
        detail = "sitemap submitted" if status is Status.ACCEPTED else body_excerpt(response.text)
        return SubmissionResult(sitemap_url, "search-console", status, response.status_code, detail)

    def list(self) -> list[dict[str, object]]:
        response = self.caller.request("GET", self._endpoint(), headers={"Authorization": f"Bearer {self.token()}"})
        response.raise_for_status()
        sitemaps: list[dict[str, object]] = response.json().get("sitemap", [])
        return sitemaps


class GoogleIndexingSubmitter:
    """URL_UPDATED / URL_DELETED notifications. Only for JobPosting and BroadcastEvent pages."""

    name = "google-indexing"
    max_batch = 1  # one publish call per URL; batching via multipart is not implemented

    def __init__(
        self,
        caller: HttpCaller,
        *,
        token: TokenProvider,
        eligible_content_confirmed: bool,
        notification_type: str = "URL_UPDATED",
        daily_quota: int = INDEXING_DAILY_QUOTA,
    ):
        if not eligible_content_confirmed:
            raise ValueError(
                "The Google Indexing API only supports pages with JobPosting or BroadcastEvent structured data. "
                "Confirm eligibility explicitly (--confirm-indexing-api-eligible) or use Search Console sitemaps."
            )
        if notification_type not in ("URL_UPDATED", "URL_DELETED"):
            raise ValueError("notification_type must be URL_UPDATED or URL_DELETED")
        self.caller = caller
        self.token = token
        self.notification_type = notification_type
        self.remaining = daily_quota

    def submit(self, urls: list[str]) -> list[SubmissionResult]:
        results = []
        for url in urls:
            if self.remaining <= 0:
                results.append(SubmissionResult(url, self.name, Status.SKIPPED, None, "daily quota reached"))
                continue
            response = self.caller.request(
                "POST",
                INDEXING_PUBLISH_URL,
                json={"url": url, "type": self.notification_type},
                headers={"Authorization": f"Bearer {self.token()}"},
            )
            status = classify(response.status_code)
            if status is Status.ACCEPTED:
                self.remaining -= 1
            detail = "notification received" if status is Status.ACCEPTED else body_excerpt(response.text)
            results.append(SubmissionResult(url, self.name, status, response.status_code, detail))
        return results
