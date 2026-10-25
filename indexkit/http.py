"""HTTP calls with timeouts, rate limiting and retries.

Retries happen only for responses that may succeed later: 429 and 5xx, plus connection errors and
timeouts. A Retry-After header (seconds or HTTP date) takes precedence over the computed backoff.
"""

from __future__ import annotations

import email.utils
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class RetryPolicy:
    attempts: int = 4  # total tries, including the first
    base_delay: float = 1.0  # seconds; doubles each retry
    max_delay: float = 60.0
    jitter: float = 0.25  # +/- fraction of the delay


class RateLimiter:
    """Enforces a minimum interval between calls (per submitter)."""

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    if value.strip().isdigit():
        return float(value.strip())
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class HttpCaller:
    def __init__(
        self,
        client: httpx.Client,
        retry: RetryPolicy | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.retry = retry or RetryPolicy()
        self.limiter = limiter
        self._sleep = sleep

    def _delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None and (server := retry_after_seconds(response)) is not None:
            return min(server, self.retry.max_delay)
        delay = min(self.retry.base_delay * 2.0 ** (attempt - 1), self.retry.max_delay)
        return max(0.0, delay * (1 + random.uniform(-self.retry.jitter, self.retry.jitter)))

    def request(self, method: str, url: str, *, redact: str | None = None, **kwargs: Any) -> httpx.Response:
        """Returns the final response (retryable statuses included, after the last attempt).
        Raises httpx.HTTPError only when every attempt failed at the network level."""
        shown = url.replace(redact, "***") if redact else url
        for attempt in range(1, self.retry.attempts + 1):
            if self.limiter:
                self.limiter.wait()
            try:
                response = self.client.request(method, url, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == self.retry.attempts:
                    raise
                delay = self._delay(attempt, None)
                logger.warning(
                    "network error, retrying", extra={"url": shown, "attempt": attempt, "delay": round(delay, 2), "error": str(exc)}
                )
                self._sleep(delay)
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self.retry.attempts:
                delay = self._delay(attempt, response)
                logger.warning(
                    "retryable status", extra={"url": shown, "status": response.status_code, "attempt": attempt, "delay": round(delay, 2)}
                )
                self._sleep(delay)
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover
