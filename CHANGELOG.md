# Changelog

## [1.0.0] – 2026-10-25

First public version.

- Sitemap discovery (robots.txt), parsing (urlset, sitemap index, gzip) with defusedxml and 50 MB limits.
- URL normalisation, validation, allowed-host policy, include/exclude, tracking-parameter removal, `--since`.
- Submitters: IndexNow, Bing Webmaster URL Submission API, Google Search Console `sitemaps.submit`, Google Indexing API (eligibility confirmation required).
- Retries with backoff and `Retry-After`, rate limiting, quotas, SQLite history, dry run by default, JSON reports and logs.

Based on a Google Indexing API client and sitemap-sync service written for an internal publishing system. Re-implemented as a standalone, provider-neutral tool with sitemap parsing, IndexNow and Bing support, and safety checks that the original did not have.
