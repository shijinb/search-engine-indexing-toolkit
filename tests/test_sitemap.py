from datetime import UTC, datetime, timedelta, timezone

import pytest

from indexkit import sitemap
from indexkit.sitemap import SitemapError, crawl, discover_from_robots, iter_locs, parse
from tests.conftest import FakeWeb, fixture, urlset


def test_parse_urlset() -> None:
    parsed = parse(fixture("urlset.xml"), "s.xml")
    assert parsed.kind == "urlset"
    locs = [u.loc for u in parsed.urls]
    assert locs[0] == "https://www.example.com/"
    assert len(locs) == 6  # the <url> without <loc> is skipped
    assert parsed.urls[0].lastmod == datetime(2026, 10, 1)
    assert parsed.urls[1].lastmod == datetime(2026, 10, 20, 9, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert parsed.urls[3].loc.endswith("utm_source=newsletter&page=2")  # entity-escaped &amp; decoded
    assert any("without <loc>" in w for w in parsed.warnings)
    assert any("unparseable lastmod 'last tuesday'" in w for w in parsed.warnings)


def test_parse_index() -> None:
    parsed = parse(fixture("index.xml"))
    assert parsed.kind == "sitemapindex"
    assert [s.loc.rsplit("/", 1)[1] for s in parsed.sitemaps] == [
        "sitemap-pages.xml",
        "sitemap-jobs.xml.gz",
        "sitemap-missing.xml",
        "sitemap.xml",
    ]


@pytest.mark.parametrize("name", ["billion-laughs.xml", "xxe.xml"])
def test_hostile_xml_is_rejected(name: str) -> None:
    with pytest.raises(SitemapError, match="unsafe XML"):
        parse(fixture(name))


def test_wrong_root_and_broken_xml() -> None:
    with pytest.raises(SitemapError, match="root element is <html>"):
        parse(b"<html><body>Not a sitemap</body></html>")
    with pytest.raises(SitemapError, match="not well-formed"):
        parse(b"<urlset><url>")


def test_missing_namespace_is_a_warning() -> None:
    parsed = parse(b"<urlset><url><loc>https://a.example/</loc></url></urlset>")
    assert parsed.urls[0].loc == "https://a.example/"
    assert parsed.warnings == ["missing or unexpected sitemap namespace"]


def test_size_limits(monkeypatch: pytest.MonkeyPatch, web: FakeWeb, gzipped) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sitemap, "MAX_SITEMAP_BYTES", 200)
    with pytest.raises(SitemapError, match="larger than"):
        parse(b" " * 201)

    # A small gzip that expands past the limit (a "zip bomb") is caught after decompression.
    web.add("https://www.example.com/bomb.xml.gz", gzipped(b" " * 10_000))
    with web.client() as client, pytest.raises(SitemapError, match="after decompression"):
        sitemap.fetch(client, "https://www.example.com/bomb.xml.gz")


def test_crawl_follows_index_and_collects_errors(web: FakeWeb, gzipped) -> None:  # type: ignore[no-untyped-def]
    web.add("https://www.example.com/sitemap.xml", fixture("index.xml"))
    web.add("https://www.example.com/sitemap-pages.xml", urlset("https://www.example.com/a", "https://www.example.com/b"))
    web.add("https://www.example.com/sitemap-jobs.xml.gz", gzipped(urlset("https://www.example.com/jobs/1")))
    # sitemap-missing.xml is not registered -> 404

    with web.client() as client:
        result = crawl(client, ["https://www.example.com/sitemap.xml"])

    assert [u.loc for u in result.urls] == [
        "https://www.example.com/a",
        "https://www.example.com/b",
        "https://www.example.com/jobs/1",
    ]
    assert result.errors == ["https://www.example.com/sitemap-missing.xml: HTTP 404"]
    # The index lists itself; it is read once, not looped over.
    assert result.sitemaps_read.count("https://www.example.com/sitemap.xml") == 1


def test_crawl_depth_limit(web: FakeWeb) -> None:
    web.add("https://www.example.com/sitemap.xml", fixture("index.xml"))
    with web.client() as client:
        result = crawl(client, ["https://www.example.com/sitemap.xml"], max_depth=0)
    assert result.urls == []
    assert all("deeper than --max-depth 0" in e for e in result.errors)


def test_discover_from_robots(web: FakeWeb) -> None:
    web.add(
        "https://www.example.com/robots.txt",
        "User-agent: *\nDisallow: /admin\nSitemap: https://www.example.com/sitemap.xml\nsitemap:https://www.example.com/news.xml\n",
    )
    with web.client() as client:
        assert discover_from_robots(client, "https://www.example.com/any/page") == [
            "https://www.example.com/sitemap.xml",
            "https://www.example.com/news.xml",
        ]


def test_iter_locs_since_keeps_undated_and_compares_timezones() -> None:
    parsed = parse(fixture("urlset.xml"))
    recent = list(iter_locs(parsed.urls, since=datetime(2026, 10, 15, tzinfo=UTC)))
    assert "https://www.example.com/jobs/data-analyst" in recent  # 2026-10-20 +05:30
    assert "https://www.example.com/" not in recent  # 2026-10-01
    assert "https://cdn.other-site.com/image.png" in recent  # no lastmod: kept
