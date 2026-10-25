"""URL normalisation, validation and de-duplication.

Normalisation is deliberately conservative: it only applies changes that never alter which resource a
URL points to (scheme/host case, default ports, fragments, empty path). Query parameters are kept in
order, because reordering or dropping them can change the page. Tracking parameters are removed only
when explicitly requested.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

MAX_URL_LENGTH = 2048  # sitemaps.org protocol limit for <loc>
_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|gclid|fbclid|msclkid|mc_cid|mc_eid)$", re.IGNORECASE)


class InvalidURLError(ValueError):
    pass


def normalize(url: str, *, strip_tracking: bool = False) -> str:
    """Canonical form used for comparing and de-duplicating URLs."""
    url = url.strip()
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise InvalidURLError(f"unsupported scheme: {url!r}")
    if not parts.hostname:
        raise InvalidURLError(f"missing host: {url!r}")

    host = parts.hostname.lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidURLError(f"invalid host: {url!r}") from exc

    netloc = host
    if parts.port and parts.port != _DEFAULT_PORTS[scheme]:
        netloc = f"{host}:{parts.port}"
    if parts.username or parts.password:
        raise InvalidURLError(f"credentials in URL are not allowed: {scheme}://…@{host}")

    # Re-quote the path so "/a b" and "/a%20b" compare equal; keep reserved characters as they are.
    path = quote(unquote(parts.path), safe="/:@!$&'()*+,;=-._~") or "/"

    query = parts.query
    if strip_tracking and query:
        kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
        query = urlencode(kept, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


@dataclass(frozen=True)
class URLPolicy:
    """Which URLs may be submitted. Hosts are compared after normalisation."""

    allowed_hosts: frozenset[str]
    include: tuple[re.Pattern[str], ...] = ()
    exclude: tuple[re.Pattern[str], ...] = ()
    strip_tracking: bool = False

    @classmethod
    def for_hosts(
        cls,
        hosts: Iterable[str],
        include: Iterable[str] = (),
        exclude: Iterable[str] = (),
        strip_tracking: bool = False,
    ) -> URLPolicy:
        return cls(
            allowed_hosts=frozenset(h.lower().rstrip(".") for h in hosts),
            include=tuple(re.compile(p) for p in include),
            exclude=tuple(re.compile(p) for p in exclude),
            strip_tracking=strip_tracking,
        )


@dataclass(frozen=True)
class Rejected:
    url: str
    reason: str


def clean(urls: Iterable[str], policy: URLPolicy) -> tuple[list[str], list[Rejected]]:
    """Normalises, validates, filters and de-duplicates, preserving first-seen order."""
    accepted: list[str] = []
    rejected: list[Rejected] = []
    seen: set[str] = set()

    for raw in urls:
        try:
            url = normalize(raw, strip_tracking=policy.strip_tracking)
        except InvalidURLError as exc:
            rejected.append(Rejected(raw, str(exc)))
            continue

        host = urlsplit(url).hostname or ""
        if len(url) > MAX_URL_LENGTH:
            rejected.append(Rejected(raw, f"longer than {MAX_URL_LENGTH} characters"))
        elif host not in policy.allowed_hosts:
            rejected.append(Rejected(raw, f"host {host!r} is not in the allowed hosts"))
        elif policy.include and not any(p.search(url) for p in policy.include):
            rejected.append(Rejected(raw, "does not match any --include pattern"))
        elif any(p.search(url) for p in policy.exclude):
            rejected.append(Rejected(raw, "matches an --exclude pattern"))
        elif url in seen:
            rejected.append(Rejected(raw, "duplicate"))
        else:
            seen.add(url)
            accepted.append(url)

    return accepted, rejected


def batches(items: list[str], size: int) -> Iterator[list[str]]:
    if size < 1:
        raise ValueError("batch size must be at least 1")
    for start in range(0, len(items), size):
        yield items[start : start + size]
