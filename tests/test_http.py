import httpx
import pytest

from indexkit.http import HttpCaller, RateLimiter, RetryPolicy, retry_after_seconds
from tests.conftest import FakeWeb

URL = "https://api.example/endpoint"


def caller(web: FakeWeb, sleeps: list[float], attempts: int = 3) -> HttpCaller:
    return HttpCaller(web.client(), RetryPolicy(attempts=attempts, base_delay=1.0, jitter=0.0), sleep=sleeps.append)


def sequence(*responses: httpx.Response):  # type: ignore[no-untyped-def]
    queue = list(responses)
    return lambda request: queue.pop(0)


def test_retries_retryable_status_then_succeeds(web: FakeWeb) -> None:
    web.handle(URL, sequence(httpx.Response(503), httpx.Response(429), httpx.Response(200)))
    sleeps: list[float] = []
    assert caller(web, sleeps).request("POST", URL).status_code == 200
    assert sleeps == [1.0, 2.0]  # exponential backoff


def test_returns_last_retryable_response_when_attempts_run_out(web: FakeWeb) -> None:
    web.handle(URL, sequence(httpx.Response(503), httpx.Response(503)))
    sleeps: list[float] = []
    assert caller(web, sleeps, attempts=2).request("GET", URL).status_code == 503
    assert sleeps == [1.0]


def test_client_errors_are_not_retried(web: FakeWeb) -> None:
    web.add(URL, status=422)
    sleeps: list[float] = []
    assert caller(web, sleeps).request("POST", URL).status_code == 422
    assert sleeps == [] and len(web.requests) == 1


def test_retry_after_header_wins(web: FakeWeb) -> None:
    web.handle(URL, sequence(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)))
    sleeps: list[float] = []
    caller(web, sleeps).request("GET", URL)
    assert sleeps == [7.0]


def test_retry_after_http_date() -> None:
    response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert retry_after_seconds(response) == 0.0  # in the past
    assert retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"})) is None


def test_network_errors_retry_then_raise(web: FakeWeb) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    web.handle(URL, boom)
    sleeps: list[float] = []
    with pytest.raises(httpx.ConnectError):
        caller(web, sleeps).request("GET", URL)
    assert len(web.requests) == 3 and len(sleeps) == 2


def test_rate_limiter_waits_for_the_remaining_interval() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(2.0, clock=lambda: now[0], sleep=sleep)
    limiter.wait()  # first call: no wait
    now[0] += 0.5
    limiter.wait()
    assert sleeps == [1.5]
