from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol


class Status(enum.StrEnum):
    ACCEPTED = "accepted"  # the API acknowledged receipt (not that the page was or will be indexed)
    REJECTED = "rejected"  # the API refused this request; retrying unchanged will not help
    FAILED = "failed"  # network error or retries exhausted; may succeed later
    SKIPPED = "skipped"  # not sent (dry run, already submitted, quota)


@dataclass(frozen=True)
class SubmissionResult:
    url: str
    engine: str
    status: Status
    http_status: int | None = None
    detail: str = ""


class Submitter(Protocol):
    """Sends a batch of URLs to one search engine API."""

    name: str
    max_batch: int

    def submit(self, urls: list[str]) -> list[SubmissionResult]: ...


def classify(status_code: int) -> Status:
    if 200 <= status_code < 300:
        return Status.ACCEPTED
    if status_code == 429 or status_code >= 500:
        return Status.FAILED
    return Status.REJECTED


def body_excerpt(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]
