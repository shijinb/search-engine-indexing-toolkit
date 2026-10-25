import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indexkit import cli
from indexkit.history import History
from indexkit.runner import execute, plan
from indexkit.sitemap import SitemapURL
from indexkit.submitters.base import Status, SubmissionResult
from indexkit.submitters.indexnow import DEFAULT_ENDPOINT
from indexkit.urls import URLPolicy
from tests.conftest import FakeWeb, urlset

KEY = "a1b2c3d4e5f60718"
POLICY = URLPolicy.for_hosts(["www.example.com"])


class RecordingSubmitter:
    name = "test"
    max_batch = 2

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def submit(self, urls: list[str]) -> list[SubmissionResult]:
        self.batches.append(urls)
        return [SubmissionResult(u, self.name, Status.ACCEPTED, 200, "ok") for u in urls]


def entries(*pairs: tuple[str, datetime | None]) -> list[SitemapURL]:
    return [SitemapURL(loc, lastmod) for loc, lastmod in pairs]


def test_dry_run_sends_nothing(tmp_path: Path) -> None:
    submitter = RecordingSubmitter()
    history = History(tmp_path / "h.sqlite3")
    report = execute(plan("test", entries(("https://www.example.com/a", None)), POLICY, history), submitter, history)
    assert submitter.batches == []
    assert report.dry_run and report.counts() == {"skipped": 1}
    assert history.summary() == []  # dry runs are not recorded


def test_execute_batches_and_records_history(tmp_path: Path) -> None:
    submitter = RecordingSubmitter()
    history = History(tmp_path / "h.sqlite3")
    urls = [(f"https://www.example.com/{i}", None) for i in range(5)]
    report = execute(plan("test", entries(*urls), POLICY, history), submitter, history, dry_run=False)
    assert [len(b) for b in submitter.batches] == [2, 2, 1]
    assert report.counts() == {"accepted": 5}
    assert history.summary()[0][1:] == ("test", "accepted", 5)


def test_unchanged_urls_are_skipped_and_modified_ones_resent(tmp_path: Path) -> None:
    history = History(tmp_path / "h.sqlite3")
    sent_at = datetime(2026, 10, 10, tzinfo=UTC)
    history.record(
        "r0", [SubmissionResult(f"https://www.example.com/{p}", "test", Status.ACCEPTED, 200) for p in ("a", "b", "c")], when=sent_at
    )
    history.record("r0", [SubmissionResult("https://www.example.com/d", "test", Status.REJECTED, 422)], when=sent_at)

    run = plan(
        "test",
        entries(
            ("https://www.example.com/a", datetime(2026, 10, 1)),  # older than the submission
            ("https://www.example.com/b", datetime(2026, 10, 20)),  # modified since
            ("https://www.example.com/c", None),  # no lastmod: treat as unchanged
            ("https://www.example.com/d", None),  # only ever rejected: try again
            ("https://www.example.com/e", None),  # never sent
        ),
        POLICY,
        history,
    )
    assert run.unchanged == ["https://www.example.com/a", "https://www.example.com/c"]
    assert run.to_submit == ["https://www.example.com/b", "https://www.example.com/d", "https://www.example.com/e"]
    assert plan("test", entries(("https://www.example.com/a", None)), POLICY, history, force=True).to_submit == [
        "https://www.example.com/a"
    ]


def test_limit(tmp_path: Path) -> None:
    submitter = RecordingSubmitter()
    run = plan("test", entries(*[(f"https://www.example.com/{i}", None) for i in range(5)]), POLICY, None)
    assert execute(run, submitter, None, dry_run=False, limit=3).planned == 3


# --- CLI end to end (all HTTP mocked) ---------------------------------------------------------------


@pytest.fixture
def site(web: FakeWeb, monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    web.add(
        "https://www.example.com/sitemap.xml", urlset("https://www.example.com/a", "https://www.example.com/b", "https://other.example/x")
    )
    web.add(f"https://www.example.com/{KEY}.txt", KEY)
    web.add(DEFAULT_ENDPOINT, status=200)
    monkeypatch.setattr(cli, "_client", lambda timeout: web.client())
    monkeypatch.setenv("INDEXNOW_KEY", KEY)
    return web


def run_cli(*args: str) -> int:
    return cli.main(list(args))


def test_cli_submit_defaults_to_dry_run(site: FakeWeb, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_cli(
        "--json",
        "submit",
        "indexnow",
        "https://www.example.com/sitemap.xml",
        "--host",
        "www.example.com",
        "--history",
        str(tmp_path / "h.db"),
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["dry_run"] is True and report["planned"] == 2
    assert report["rejected"] == [{"url": "https://other.example/x", "reason": "host 'other.example' is not in the allowed hosts"}]
    assert site.sent("POST") == []


def test_cli_execute_requires_confirmation_when_not_interactive(site: FakeWeb, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_cli(
        "submit",
        "indexnow",
        "https://www.example.com/sitemap.xml",
        "--host",
        "www.example.com",
        "--history",
        str(tmp_path / "h.db"),
        "--execute",
    )
    assert code == 2
    assert "--yes" in capsys.readouterr().err
    assert site.sent("POST") == []


def test_cli_execute_with_yes_checks_key_then_sends_once(site: FakeWeb, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    history = str(tmp_path / "h.db")
    args = [
        "--json",
        "submit",
        "indexnow",
        "https://www.example.com/sitemap.xml",
        "--host",
        "www.example.com",
        "--history",
        history,
        "--execute",
        "--yes",
        "--min-interval",
        "0",
    ]
    assert run_cli(*args) == 0
    assert json.loads(capsys.readouterr().out)["results"] == {"accepted": 2}
    assert [r.url.path for r in site.sent("GET")] == ["/sitemap.xml", f"/{KEY}.txt"]
    assert len(site.sent("POST")) == 1

    # Second run: both URLs already accepted and unchanged, so nothing is sent.
    assert run_cli(*args) == 0
    assert json.loads(capsys.readouterr().out)["unchanged_skipped"] == 2
    assert len(site.sent("POST")) == 1


def test_cli_refuses_when_key_file_missing(site: FakeWeb, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    del site.routes[f"https://www.example.com/{KEY}.txt"]
    code = run_cli(
        "submit",
        "indexnow",
        "https://www.example.com/sitemap.xml",
        "--host",
        "www.example.com",
        "--history",
        str(tmp_path / "h.db"),
        "--execute",
        "--yes",
    )
    assert code == 2
    assert "key check failed" in capsys.readouterr().err
    assert site.sent("POST") == []


def test_cli_missing_secret_is_a_usage_error(
    site: FakeWeb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("INDEXNOW_KEY")
    assert (
        run_cli(
            "submit", "indexnow", "https://www.example.com/sitemap.xml", "--host", "www.example.com", "--history", str(tmp_path / "h.db")
        )
        == 2
    )
    assert "INDEXNOW_KEY is not set" in capsys.readouterr().err


def test_cli_urls_and_gsc_dry_run(site: FakeWeb, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli("urls", "https://www.example.com/sitemap.xml", "--host", "www.example.com") == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == ["https://www.example.com/a", "https://www.example.com/b"]
    assert "1 rejected" in out.err

    assert run_cli("gsc-sitemap", "https://www.example.com/sitemap.xml", "--site", "sc-domain:example.com") == 0
    assert capsys.readouterr().out.startswith("DRY RUN")
