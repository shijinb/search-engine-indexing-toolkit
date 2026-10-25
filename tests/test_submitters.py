import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest

from indexkit.http import HttpCaller, RetryPolicy
from indexkit.submitters.base import Status
from indexkit.submitters.bing import API_BASE, BingSubmitter
from indexkit.submitters.google import INDEXING_PUBLISH_URL, GoogleIndexingSubmitter, SearchConsoleSitemaps
from indexkit.submitters.indexnow import DEFAULT_ENDPOINT, IndexNowSubmitter
from tests.conftest import FakeWeb

KEY = "a1b2c3d4e5f60718"


def caller(web: FakeWeb) -> HttpCaller:
    return HttpCaller(web.client(), RetryPolicy(attempts=1))


# --- IndexNow --------------------------------------------------------------------------------------


def test_indexnow_payload_matches_protocol(web: FakeWeb) -> None:
    web.add(DEFAULT_ENDPOINT, status=200)
    submitter = IndexNowSubmitter(caller(web), host="www.example.com", key=KEY)
    results = submitter.submit(["https://www.example.com/a", "https://www.example.com/b"])

    body = json.loads(web.sent("POST")[0].content)
    assert body == {
        "host": "www.example.com",
        "key": KEY,
        "keyLocation": f"https://www.example.com/{KEY}.txt",
        "urlList": ["https://www.example.com/a", "https://www.example.com/b"],
    }
    assert {r.status for r in results} == {Status.ACCEPTED}


@pytest.mark.parametrize(
    ("code", "status", "detail"),
    [
        (202, Status.ACCEPTED, "key validation pending"),
        (400, Status.REJECTED, "invalid format"),
        (403, Status.REJECTED, "key not valid"),
        (422, Status.REJECTED, "does not belong to the host"),
        (429, Status.FAILED, "too many requests"),
    ],
)
def test_indexnow_response_codes(web: FakeWeb, code: int, status: Status, detail: str) -> None:
    web.add(DEFAULT_ENDPOINT, status=code)
    (result,) = IndexNowSubmitter(caller(web), host="www.example.com", key=KEY).submit(["https://www.example.com/a"])
    assert result.status is status and detail in result.detail and result.http_status == code


@pytest.mark.parametrize("key", ["short", "has spaces in it", "x" * 129, "under_score_key"])
def test_indexnow_key_format(web: FakeWeb, key: str) -> None:
    with pytest.raises(ValueError, match="8-128 characters"):
        IndexNowSubmitter(caller(web), host="www.example.com", key=key)


def test_indexnow_refuses_other_hosts(web: FakeWeb) -> None:
    submitter = IndexNowSubmitter(caller(web), host="www.example.com", key=KEY)
    with pytest.raises(ValueError, match="another host"):
        submitter.submit(["https://blog.example.com/a"])
    assert web.requests == []


def test_indexnow_key_file_check(web: FakeWeb) -> None:
    submitter = IndexNowSubmitter(caller(web), host="www.example.com", key=KEY)
    assert submitter.key_file_ok() == (False, f"https://www.example.com/{KEY}.txt returned HTTP 404")
    web.add(f"https://www.example.com/{KEY}.txt", "different-key\n")
    assert submitter.key_file_ok()[0] is False
    web.add(f"https://www.example.com/{KEY}.txt", f"{KEY}\n")
    assert submitter.key_file_ok() == (True, "key file found and matches")


# --- Bing ------------------------------------------------------------------------------------------


def test_bing_respects_quota_and_keeps_key_out_of_logs(web: FakeWeb, caplog: pytest.LogCaptureFixture) -> None:
    web.add(f"{API_BASE}/GetUrlSubmissionQuota", json.dumps({"d": {"DailyQuota": 2, "MonthlyQuota": 50}}))
    posts = iter([httpx.Response(503), httpx.Response(200, json={"d": None})])
    web.handle(f"{API_BASE}/SubmitUrlbatch", lambda request: next(posts))

    submitter = BingSubmitter(
        HttpCaller(web.client(), RetryPolicy(attempts=2, base_delay=0)), site_url="https://www.example.com", api_key="SECRETKEY123"
    )
    with caplog.at_level(logging.DEBUG):
        results = submitter.submit(["https://www.example.com/1", "https://www.example.com/2", "https://www.example.com/3"])

    assert [r.status for r in results] == [Status.ACCEPTED, Status.ACCEPTED, Status.SKIPPED]
    assert results[2].detail == "daily quota exhausted"
    quota_request = web.sent("GET")[0]
    assert parse_qs(quota_request.url.query.decode())["siteUrl"] == ["https://www.example.com"]  # encoded, not concatenated
    assert json.loads(web.sent("POST")[-1].content) == {
        "siteUrl": "https://www.example.com",
        "urlList": ["https://www.example.com/1", "https://www.example.com/2"],
    }
    assert "SECRETKEY123" not in caplog.text  # the retry warning was logged without the key
    assert "retryable status" in caplog.text
    # The next call knows the quota is used up and sends nothing.
    assert submitter.submit(["https://www.example.com/4"])[0].status is Status.SKIPPED


def test_bing_unexpected_quota_response_is_an_error(web: FakeWeb) -> None:
    web.add(f"{API_BASE}/GetUrlSubmissionQuota", json.dumps({"d": None}))
    submitter = BingSubmitter(caller(web), site_url="https://www.example.com", api_key="k")
    with pytest.raises(ValueError, match="unexpected GetUrlSubmissionQuota"):
        submitter.submit(["https://www.example.com/1"])
    assert web.sent("POST") == []


# --- Google ----------------------------------------------------------------------------------------


def test_indexing_api_requires_explicit_eligibility(web: FakeWeb) -> None:
    with pytest.raises(ValueError, match="JobPosting or BroadcastEvent"):
        GoogleIndexingSubmitter(caller(web), token=lambda: "t", eligible_content_confirmed=False)


def test_indexing_api_counts_daily_quota(web: FakeWeb) -> None:
    web.add(INDEXING_PUBLISH_URL, json.dumps({"urlNotificationMetadata": {}}))
    submitter = GoogleIndexingSubmitter(caller(web), token=lambda: "tok", eligible_content_confirmed=True, daily_quota=2)
    results = submitter.submit(["https://www.example.com/jobs/1", "https://www.example.com/jobs/2", "https://www.example.com/jobs/3"])

    assert [r.status for r in results] == [Status.ACCEPTED, Status.ACCEPTED, Status.SKIPPED]
    first = web.sent("POST")[0]
    assert first.headers["Authorization"] == "Bearer tok"
    assert json.loads(first.content) == {"url": "https://www.example.com/jobs/1", "type": "URL_UPDATED"}


def test_search_console_sitemap_submit_encodes_property_and_feed(web: FakeWeb) -> None:
    def put(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204 if request.method == "PUT" else 405)

    expected = "https://www.googleapis.com/webmasters/v3/sites/sc-domain%3Aexample.com/sitemaps/https%3A%2F%2Fwww.example.com%2Fsitemap.xml"
    web.handle(expected, put)
    result = SearchConsoleSitemaps(caller(web), site_url="sc-domain:example.com", token=lambda: "tok").submit(
        "https://www.example.com/sitemap.xml"
    )

    assert result.status is Status.ACCEPTED and result.http_status == 204
    assert web.requests[0].content == b""  # the API takes no request body
