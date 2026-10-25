from __future__ import annotations

import gzip
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def urlset(*locs: str) -> bytes:
    rows = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'.encode()


class FakeWeb:
    """A tiny in-memory web: maps URL -> (status, body, headers) or a handler, and records every request."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response] | tuple[int, bytes, dict[str, str]]] = {}
        self.requests: list[httpx.Request] = []

    def add(self, url: str, body: bytes | str = b"", status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.routes[url] = (status, body.encode() if isinstance(body, str) else body, headers or {})

    def handle(self, url: str, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes[url] = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = str(request.url.copy_with(query=None))
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, text="not found")
        if callable(route):
            return route(request)
        status, body, headers = route
        return httpx.Response(status, content=body, headers=headers)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def sent(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def gzipped() -> Callable[[bytes], bytes]:
    return gzip.compress
