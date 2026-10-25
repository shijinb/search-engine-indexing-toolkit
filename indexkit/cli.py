"""indexkit command-line interface.

    indexkit discover https://www.example.com
    indexkit urls https://www.example.com/sitemap.xml --host www.example.com
    indexkit submit indexnow https://www.example.com/sitemap.xml --host www.example.com          # dry run
    indexkit submit indexnow https://www.example.com/sitemap.xml --host www.example.com --execute
    indexkit gsc-sitemap https://www.example.com/sitemap.xml --site sc-domain:example.com --execute
    indexkit history

Secrets come from environment variables, never from arguments (so they stay out of shell history):
INDEXNOW_KEY, BING_WEBMASTER_API_KEY, GOOGLE_APPLICATION_CREDENTIALS (path to a service-account file).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

import httpx

from indexkit import __version__
from indexkit.history import History
from indexkit.http import HttpCaller, RateLimiter, RetryPolicy
from indexkit.logging import configure as configure_logging
from indexkit.runner import RunReport, execute, plan
from indexkit.sitemap import crawl, discover_from_robots, iter_locs
from indexkit.submitters.base import Submitter
from indexkit.submitters.bing import BingSubmitter
from indexkit.submitters.google import (
    INDEXING_SCOPE,
    SEARCH_CONSOLE_SCOPE,
    GoogleIndexingSubmitter,
    SearchConsoleSitemaps,
    service_account_token_provider,
)
from indexkit.submitters.indexnow import DEFAULT_ENDPOINT, IndexNowSubmitter
from indexkit.urls import URLPolicy, clean

logger = logging.getLogger("indexkit")

USER_AGENT = f"indexkit/{__version__} (+https://github.com/shijinb/search-engine-indexing-toolkit)"


class UsageError(Exception):
    pass


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise UsageError(f"environment variable {name} is not set")
    return value


def _policy(args: argparse.Namespace) -> URLPolicy:
    if not args.host:
        raise UsageError("--host is required: URLs for any other host are rejected")
    return URLPolicy.for_hosts(args.host, args.include or (), args.exclude or (), strip_tracking=args.strip_tracking)


def _confirm(message: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise UsageError("refusing to submit without confirmation; pass --yes in non-interactive use")
    return input(f"{message} [y/N] ").strip().lower() in ("y", "yes")


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    elif isinstance(data, dict):
        for key, value in data.items():
            print(f"{key}: {value}")
    else:
        for item in data:
            print(item)


# --- commands --------------------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    with _client(args.timeout) as client:
        sitemaps = discover_from_robots(client, args.site)
    _print(sitemaps or ["no Sitemap: lines in robots.txt"], args.json)
    return 0


def cmd_urls(args: argparse.Namespace) -> int:
    with _client(args.timeout) as client:
        result = crawl(client, args.sitemaps, max_depth=args.max_depth, max_sitemaps=args.max_sitemaps)
    since = datetime.fromisoformat(args.since) if args.since else None
    accepted, rejected = clean(iter_locs(result.urls, since), _policy(args))

    if args.json:
        _print(
            {
                "sitemaps_read": result.sitemaps_read,
                "urls": accepted,
                "rejected": [{"url": r.url, "reason": r.reason} for r in rejected],
                "errors": result.errors,
                "warnings": result.warnings,
            },
            True,
        )
    else:
        for url in accepted:
            print(url)
        print(
            f"# {len(accepted)} URLs from {len(result.sitemaps_read)} sitemap(s); {len(rejected)} rejected; {len(result.errors)} error(s)",
            file=sys.stderr,
        )
        for message in result.errors + [f"rejected {r.url}: {r.reason}" for r in rejected[:20]]:
            print(f"#   {message}", file=sys.stderr)
    return 1 if result.errors and not accepted else 0


def _submitter(args: argparse.Namespace, caller: HttpCaller) -> Submitter:
    if args.engine == "indexnow":
        if len(args.host) != 1:
            raise UsageError("IndexNow submissions are per host: pass exactly one --host")
        submitter = IndexNowSubmitter(
            caller, host=args.host[0], key=_env("INDEXNOW_KEY"), key_location=args.key_location, endpoint=args.endpoint
        )
        if args.execute:
            ok, message = submitter.key_file_ok()
            if not ok:
                raise UsageError(f"IndexNow key check failed: {message}")
        return submitter
    if args.engine == "bing":
        if not args.site:
            raise UsageError("--site is required for Bing (the site URL as registered in Bing Webmaster Tools)")
        return BingSubmitter(caller, site_url=args.site, api_key=_env("BING_WEBMASTER_API_KEY"))
    if args.engine == "google-indexing":
        token = service_account_token_provider(_env("GOOGLE_APPLICATION_CREDENTIALS"), INDEXING_SCOPE)
        return GoogleIndexingSubmitter(caller, token=token, eligible_content_confirmed=args.confirm_indexing_api_eligible)
    raise UsageError(f"unknown engine {args.engine}")  # pragma: no cover


def _print_report(report: RunReport, as_json: bool) -> None:
    if as_json:
        _print(report.as_dict(), True)
        return
    mode = "DRY RUN, nothing sent" if report.dry_run else "executed"
    print(f"run {report.run_id} [{report.engine}] {mode}")
    print(f"  planned: {report.planned}, unchanged since last accepted: {report.unchanged}, rejected: {len(report.rejected)}")
    print(f"  results: {report.counts() or 'none'}")
    for problem in report.as_dict()["problems"][:20]:  # type: ignore[index]
        print(f"  ! {problem}")
    if report.dry_run and report.planned:
        print("  add --execute to send")


def cmd_submit(args: argparse.Namespace) -> int:
    policy = _policy(args)
    history = History(args.history)
    try:
        with _client(args.timeout) as client:
            result = crawl(client, args.sitemaps, max_depth=args.max_depth, max_sitemaps=args.max_sitemaps)
            for error in result.errors:
                logger.warning("sitemap error", extra={"error": error})

            since = datetime.fromisoformat(args.since) if args.since else None
            entries = [e for e in result.urls if since is None or e.loc in set(iter_locs([e], since))]
            run_plan = plan(args.engine, entries, policy, history, force=args.force)

            caller = HttpCaller(client, RetryPolicy(attempts=args.retries + 1), RateLimiter(args.min_interval))
            submitter = _submitter(args, caller)

            if args.execute and run_plan.to_submit:
                count = min(len(run_plan.to_submit), args.limit or len(run_plan.to_submit))
                if not _confirm(f"Send {count} URL(s) to {args.engine}?", args.yes):
                    print("cancelled")
                    return 1

            report = execute(run_plan, submitter, history, dry_run=not args.execute, limit=args.limit)
    finally:
        history.close()

    _print_report(report, args.json)
    return 1 if report.as_dict()["problems"] else 0


def cmd_gsc_sitemap(args: argparse.Namespace) -> int:
    if not args.execute:
        print(f"DRY RUN: would submit {args.sitemap} to Search Console property {args.site}. Add --execute to send.")
        return 0
    if not _confirm(f"Submit {args.sitemap} to Search Console property {args.site}?", args.yes):
        return 1
    token = service_account_token_provider(_env("GOOGLE_APPLICATION_CREDENTIALS"), SEARCH_CONSOLE_SCOPE)
    with _client(args.timeout) as client:
        sitemaps = SearchConsoleSitemaps(HttpCaller(client), site_url=args.site, token=token)
        result = sitemaps.submit(args.sitemap)
    _print({"sitemap": result.url, "status": result.status.value, "http_status": result.http_status, "detail": result.detail}, args.json)
    return 0 if result.status.value == "accepted" else 1


def cmd_history(args: argparse.Namespace) -> int:
    history = History(args.history)
    try:
        rows = history.summary(args.runs)
    finally:
        history.close()
    if args.json:
        _print([{"run_id": r, "engine": e, "status": s, "count": c} for r, e, s, c in rows], True)
    else:
        for run_id, engine, status, count in rows:
            print(f"{run_id}  {engine:16} {status:9} {count}")
    return 0


# --- argument parsing ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="indexkit", description="Sitemap processing and search engine submission. Dry run by default.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--log-format", choices=["text", "json"], default="text")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="list sitemaps declared in robots.txt")
    discover.add_argument("site", help="site root, e.g. https://www.example.com")
    discover.set_defaults(func=cmd_discover)

    def add_url_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("sitemaps", nargs="+", help="sitemap or sitemap index URLs")
        p.add_argument("--host", action="append", help="allowed host (repeatable); other hosts are rejected")
        p.add_argument("--include", action="append", help="regex a URL must match (repeatable)")
        p.add_argument("--exclude", action="append", help="regex that excludes a URL (repeatable)")
        p.add_argument("--since", help="only URLs with lastmod on/after this ISO date (undated URLs kept)")
        p.add_argument("--strip-tracking", action="store_true", help="remove utm_*, gclid, fbclid … query parameters")
        p.add_argument("--max-depth", type=int, default=3, help="sitemap index nesting limit")
        p.add_argument("--max-sitemaps", type=int, default=500)

    urls = sub.add_parser("urls", help="read sitemaps and print clean, de-duplicated URLs")
    add_url_options(urls)
    urls.set_defaults(func=cmd_urls)

    submit = sub.add_parser("submit", help="submit sitemap URLs to a search engine (dry run unless --execute)")
    submit.add_argument("engine", choices=["indexnow", "bing", "google-indexing"])
    add_url_options(submit)
    submit.add_argument("--execute", action="store_true", help="actually send (default is a dry run)")
    submit.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    submit.add_argument("--limit", type=int, help="send at most this many URLs")
    submit.add_argument("--force", action="store_true", help="resend URLs already accepted and unchanged")
    submit.add_argument("--history", default="indexkit-history.sqlite3", help="SQLite submission history")
    submit.add_argument("--retries", type=int, default=3, help="retries for 429/5xx/network errors")
    submit.add_argument("--min-interval", type=float, default=1.0, help="seconds between API calls")
    submit.add_argument("--site", help="Bing: site URL as registered in Bing Webmaster Tools")
    submit.add_argument("--key-location", help="IndexNow: key file URL (default https://HOST/KEY.txt)")
    submit.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="IndexNow endpoint")
    submit.add_argument(
        "--confirm-indexing-api-eligible",
        action="store_true",
        help="Google Indexing API: confirm every URL is a JobPosting or BroadcastEvent page",
    )
    submit.set_defaults(func=cmd_submit)

    gsc = sub.add_parser("gsc-sitemap", help="submit a sitemap to a Google Search Console property")
    gsc.add_argument("sitemap")
    gsc.add_argument("--site", required=True, help="property, e.g. https://www.example.com/ or sc-domain:example.com")
    gsc.add_argument("--execute", action="store_true")
    gsc.add_argument("--yes", action="store_true")
    gsc.set_defaults(func=cmd_gsc_sitemap)

    hist = sub.add_parser("history", help="summarise recent runs")
    hist.add_argument("--history", default="indexkit-history.sqlite3")
    hist.add_argument("--runs", type=int, default=10)
    hist.set_defaults(func=cmd_history)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(json_output=args.log_format == "json", verbose=args.verbose)
    try:
        code: int = args.func(args)
        return code
    except (UsageError, ValueError) as exc:
        print(f"indexkit: error: {exc}", file=sys.stderr)
        return 2
    except httpx.HTTPError as exc:
        print(f"indexkit: network error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
