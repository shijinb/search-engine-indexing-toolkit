"""IndexNow (https://www.indexnow.org/documentation).

POST {host, key, keyLocation, urlList} to an IndexNow endpoint, up to 10,000 URLs per request. Every
participating search engine shares submissions with the others, so one endpoint is enough.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from indexkit.http import HttpCaller
from indexkit.submitters.base import Status, SubmissionResult, body_excerpt, classify

DEFAULT_ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,128}$")

# Documented response codes and what they mean for the caller.
RESPONSES = {
    200: "URL(s) received",
    202: "received; key validation pending",
    400: "bad request: invalid format",
    403: "key not valid (key file missing or does not contain the key)",
    422: "URL does not belong to the host, or key does not match the protocol schema",
    429: "too many requests (possible spam)",
}


class IndexNowSubmitter:
    name = "indexnow"
    max_batch = 10_000

    def __init__(self, caller: HttpCaller, *, host: str, key: str, key_location: str | None = None, endpoint: str = DEFAULT_ENDPOINT):
        if not KEY_PATTERN.match(key):
            raise ValueError("IndexNow key must be 8-128 characters: letters, digits and dashes")
        self.caller = caller
        self.host = host
        self.key = key
        self.key_location = key_location or f"https://{host}/{key}.txt"
        self.endpoint = endpoint

    def key_file_ok(self) -> tuple[bool, str]:
        """Checks that the key file is published and contains the key, before anything is submitted."""
        response = self.caller.request("GET", self.key_location)
        if response.status_code != 200:
            return False, f"{self.key_location} returned HTTP {response.status_code}"
        if response.text.strip() != self.key:
            return False, f"{self.key_location} does not contain the configured key"
        return True, "key file found and matches"

    def submit(self, urls: list[str]) -> list[SubmissionResult]:
        foreign = [u for u in urls if (urlsplit(u).hostname or "") != self.host]
        if foreign:
            raise ValueError(f"IndexNow batch contains URLs for another host: {foreign[0]}")

        response = self.caller.request(
            "POST",
            self.endpoint,
            json={"host": self.host, "key": self.key, "keyLocation": self.key_location, "urlList": urls},
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        status = classify(response.status_code)
        detail = RESPONSES.get(response.status_code) or body_excerpt(response.text) or f"HTTP {response.status_code}"
        return [SubmissionResult(url, self.name, status, response.status_code, detail) for url in urls]


__all__ = ["DEFAULT_ENDPOINT", "IndexNowSubmitter", "Status"]
