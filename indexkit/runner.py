"""Plans and executes a submission run.

A run is planned first (clean, de-duplicate, drop URLs already accepted and unchanged), then either
reported (dry run, the default) or executed. Nothing is sent unless `execute=True`.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from indexkit.history import History
from indexkit.sitemap import SitemapURL
from indexkit.submitters.base import Status, SubmissionResult, Submitter
from indexkit.urls import Rejected, URLPolicy, batches, clean, normalize

logger = logging.getLogger(__name__)


@dataclass
class Plan:
    engine: str
    to_submit: list[str]
    unchanged: list[str]  # already accepted after their last modification
    rejected: list[Rejected]


@dataclass
class RunReport:
    run_id: str
    engine: str
    dry_run: bool
    planned: int
    unchanged: int
    rejected: list[Rejected]
    results: list[SubmissionResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return dict(Counter(r.status.value for r in self.results))

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "engine": self.engine,
            "dry_run": self.dry_run,
            "planned": self.planned,
            "unchanged_skipped": self.unchanged,
            "rejected": [{"url": r.url, "reason": r.reason} for r in self.rejected],
            "results": self.counts(),
            "problems": [
                {"url": r.url, "status": r.status.value, "http_status": r.http_status, "detail": r.detail}
                for r in self.results
                if r.status in (Status.REJECTED, Status.FAILED)
            ],
        }


def plan(
    engine: str,
    entries: list[SitemapURL],
    policy: URLPolicy,
    history: History | None,
    *,
    force: bool = False,
) -> Plan:
    accepted, rejected = clean((e.loc for e in entries), policy)

    lastmod: dict[str, datetime | None] = {}
    for entry in entries:
        try:
            lastmod.setdefault(normalize(entry.loc, strip_tracking=policy.strip_tracking), entry.lastmod)
        except ValueError:
            continue

    if force or history is None:
        return Plan(engine, accepted, [], rejected)

    previous = history.last_accepted(engine, accepted)
    to_submit, unchanged = [], []
    for url in accepted:
        sent = previous.get(url)
        modified = lastmod.get(url)
        if sent is not None and (modified is None or _utc(modified) <= _utc(sent)):
            unchanged.append(url)
        else:
            to_submit.append(url)
    return Plan(engine, to_submit, unchanged, rejected)


def execute(plan_: Plan, submitter: Submitter, history: History | None, *, dry_run: bool = True, limit: int | None = None) -> RunReport:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    urls = plan_.to_submit[:limit] if limit is not None else plan_.to_submit
    report = RunReport(run_id, plan_.engine, dry_run, len(urls), len(plan_.unchanged), plan_.rejected)

    if dry_run:
        report.results = [SubmissionResult(u, plan_.engine, Status.SKIPPED, None, "dry run") for u in urls]
        logger.info("dry run", extra={"engine": plan_.engine, "planned": len(urls)})
        return report

    for batch in batches(urls, submitter.max_batch):
        results = submitter.submit(batch)
        report.results.extend(results)
        if history is not None:
            history.record(run_id, results)  # recorded per batch, so an interrupted run keeps its history
        logger.info(
            "batch sent", extra={"engine": plan_.engine, "size": len(batch), "statuses": dict(Counter(r.status.value for r in results))}
        )
    return report


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
