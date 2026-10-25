"""Sitemap discovery and parsing (sitemaps.org protocol 0.9).

Parsing uses defusedxml, so entity-expansion and external-entity attacks in a hostile sitemap fail
instead of exhausting memory or reading local files. Downloads are size-limited before and after gzip
decompression (50 MB uncompressed, the protocol's own limit).
"""

from __future__ import annotations

import gzip
import io
import logging
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

logger = logging.getLogger(__name__)

MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # uncompressed, per sitemaps.org
MAX_URLS_PER_SITEMAP = 50_000
_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


class SitemapError(Exception):
    pass


@dataclass(frozen=True)
class SitemapURL:
    loc: str
    lastmod: datetime | None = None
    source: str = ""  # the sitemap the URL came from


@dataclass
class ParsedSitemap:
    kind: str  # "urlset" | "sitemapindex"
    urls: list[SitemapURL] = field(default_factory=list)
    sitemaps: list[SitemapURL] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _parse_lastmod(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        # W3C datetime: "2026-10-25", "2026-10-25T10:00:00Z", "2026-10-25T10:00:00+05:30"
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse(content: bytes, source: str = "") -> ParsedSitemap:
    """Parses a <urlset> or <sitemapindex> document. Raises SitemapError for anything else."""
    if len(content) > MAX_SITEMAP_BYTES:
        raise SitemapError(f"{source or 'sitemap'} is larger than {MAX_SITEMAP_BYTES} bytes")
    try:
        root = ElementTree.fromstring(content, forbid_dtd=True)
    except DefusedXmlException as exc:
        raise SitemapError(f"{source or 'sitemap'}: rejected unsafe XML ({exc.__class__.__name__})") from exc
    except ElementTree.ParseError as exc:
        raise SitemapError(f"{source or 'sitemap'}: not well-formed XML ({exc})") from exc

    kind = _local(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        raise SitemapError(f"{source or 'sitemap'}: root element is <{kind}>, expected <urlset> or <sitemapindex>")

    result = ParsedSitemap(kind=kind)
    if not root.tag.startswith(_NS):
        result.warnings.append("missing or unexpected sitemap namespace")

    entry_tag = "url" if kind == "urlset" else "sitemap"
    target = result.urls if kind == "urlset" else result.sitemaps
    for entry in root:
        if _local(entry.tag) != entry_tag:
            continue
        fields = {_local(child.tag): (child.text or "").strip() for child in entry}
        loc = fields.get("loc", "")
        if not loc:
            result.warnings.append(f"<{entry_tag}> without <loc> skipped")
            continue
        lastmod = _parse_lastmod(fields.get("lastmod"))
        if fields.get("lastmod") and lastmod is None:
            result.warnings.append(f"unparseable lastmod {fields['lastmod']!r} for {loc}")
        target.append(SitemapURL(loc=loc, lastmod=lastmod, source=source))

    if len(result.urls) > MAX_URLS_PER_SITEMAP:
        result.warnings.append(f"{len(result.urls)} URLs exceeds the protocol limit of {MAX_URLS_PER_SITEMAP}")
    return result


def _decompress(content: bytes, url: str) -> bytes:
    if not content.startswith(b"\x1f\x8b"):
        return content
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as handle:
            data = handle.read(MAX_SITEMAP_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise SitemapError(f"{url}: invalid gzip data") from exc
    if len(data) > MAX_SITEMAP_BYTES:
        raise SitemapError(f"{url}: larger than {MAX_SITEMAP_BYTES} bytes after decompression")
    return data


def fetch(client: httpx.Client, url: str) -> bytes:
    """Downloads a sitemap, enforcing the size limit while streaming."""
    with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise SitemapError(f"{url}: HTTP {response.status_code}")
        chunks: list[bytes] = []
        size = 0
        # iter_bytes() undoes Content-Encoding (gzip/deflate/br), so the limit applies to decoded bytes.
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_SITEMAP_BYTES:
                raise SitemapError(f"{url}: download larger than {MAX_SITEMAP_BYTES} bytes")
            chunks.append(chunk)
    # Files served as .xml.gz (without Content-Encoding) are still gzip at this point.
    return _decompress(b"".join(chunks), url)


def discover_from_robots(client: httpx.Client, site: str) -> list[str]:
    """Sitemap URLs declared in robots.txt ("Sitemap: …" lines, case-insensitive)."""
    robots_url = urljoin(site if site.endswith("/") else site + "/", "/robots.txt")
    response = client.get(robots_url)
    if response.status_code != 200:
        raise SitemapError(f"{robots_url}: HTTP {response.status_code}")
    found = []
    for line in response.text.splitlines():
        key, _, value = line.partition(":")
        if key.strip().lower() == "sitemap" and value.strip():
            found.append(value.strip())
    return found


@dataclass
class CrawlResult:
    urls: list[SitemapURL] = field(default_factory=list)
    sitemaps_read: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def crawl(client: httpx.Client, start: list[str], *, max_depth: int = 3, max_sitemaps: int = 500) -> CrawlResult:
    """Reads sitemaps and follows sitemap indexes breadth-first. Errors are collected, not raised,
    so one broken child sitemap does not stop the others from being read."""
    result = CrawlResult()
    queue: list[tuple[str, int]] = [(url, 0) for url in start]
    visited: set[str] = set()

    while queue:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        if len(visited) >= max_sitemaps:
            result.errors.append(f"stopped after {max_sitemaps} sitemaps (--max-sitemaps)")
            break
        visited.add(url)

        try:
            parsed = parse(fetch(client, url), source=url)
        except (SitemapError, httpx.HTTPError) as exc:
            result.errors.append(str(exc) if isinstance(exc, SitemapError) else f"{url}: {exc}")
            continue

        result.sitemaps_read.append(url)
        result.warnings.extend(f"{url}: {w}" for w in parsed.warnings)
        result.urls.extend(parsed.urls)
        for child in parsed.sitemaps:
            if depth + 1 > max_depth:
                result.errors.append(f"{child.loc}: deeper than --max-depth {max_depth}, skipped")
            else:
                queue.append((child.loc, depth + 1))

    logger.info("crawl finished", extra={"sitemaps": len(result.sitemaps_read), "urls": len(result.urls)})
    return result


def iter_locs(urls: list[SitemapURL], since: datetime | None = None) -> Iterator[str]:
    """URL strings, optionally only those whose lastmod is on or after `since` (undated URLs are kept)."""
    for entry in urls:
        if since is None or entry.lastmod is None or _comparable(entry.lastmod) >= _comparable(since):
            yield entry.loc


def _comparable(value: datetime) -> datetime:
    # Compare naive dates ("2026-10-25") as UTC so they can be compared with timezone-aware ones.

    return value if value.tzinfo else value.replace(tzinfo=UTC)
