"""One run of the tracker, start to finish.

    fetch every company  ->  classify each job  ->  reconcile with stored state
        ->  send health alerts and job alerts  ->  save state

Each company is fetched inside its own error boundary, so a broken source produces a
failed SourceResult and a health alert while every other source carries on.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from jobtracker import health, log
from jobtracker.config import Config
from jobtracker.dedupe import BASELINED, NEW, REPOST, Reconciler
from jobtracker.filters import classify
from jobtracker.http import HttpClient
from jobtracker.models import ERROR, OK, TIMEOUT, Company, SourceResult
from jobtracker.notify import Notifier, baseline_notification, plan_job_notifications
from jobtracker.sources import ADAPTERS
from jobtracker.state import OPEN, SourceState, StoredJob, load_state, save_state


@dataclass
class RunReport:
    results: list[SourceResult] = field(default_factory=list)
    closed: int = 0
    job_alerts_sent: int = 0
    job_alerts_failed: int = 0
    health_alert_sent: bool = False
    baseline_sent: bool = False

    def total(self, attribute: str) -> int:
        return sum(getattr(result, attribute) for result in self.results)


def run(
    config: Config,
    state_dir: Path | str,
    http: HttpClient,
    notifier: Notifier,
    now: datetime,
    only: set[str] | None = None,
    save: bool = True,
) -> RunReport:
    settings, filters = config.settings, config.filters
    state = load_state(state_dir)
    report = RunReport()

    # Companies with no adapter (ats: unsupported, or a source not built yet) are kept in
    # the config for visibility but are not polled.
    pollable = [company for company in config.companies if company.ats in ADAPTERS]
    targets = [company for company in pollable if not only or company.key in only]
    log.event("run_started", companies=len(targets), skipped=len(config.companies) - len(pollable))

    report.results = fetch_all(targets, http, settings.max_workers)

    # --- classify and reconcile --------------------------------------------------------
    reconciler = Reconciler(state, now, settings)
    baselined_jobs: list[StoredJob] = []
    baseline_ran = False
    filters_changed = False
    health_report = health.HealthReport()

    for result in report.results:
        company = result.company
        source = state.sources.setdefault(company.source_key, SourceState())
        health.check_empty(source, result, settings)
        health.record_outcome(source, result, now, settings, health_report)
        if result.status != OK:
            continue

        # A board is baselined (recorded without individual alerts) the first time it is
        # fetched and again whenever the filter rules have changed since.
        baseline = source.filters_hash != filters.hash
        if baseline:
            baseline_ran = True
            filters_changed = filters_changed or source.filters_hash is not None
            source.filters_hash = filters.hash

        for job in result.jobs:
            decision = classify(job, company, filters)
            if not decision.matched:
                continue
            result.matched += 1
            outcome = reconciler.observe(job, decision, baseline)
            if outcome == NEW:
                result.new += 1
            elif outcome == REPOST:
                result.reposts += 1
            elif outcome == BASELINED:
                result.baselined += 1
                baselined_jobs.append(state.jobs[job.key])

    fetched = {result.company.source_key for result in report.results if result.status == OK}
    configured = {company.source_key for company in pollable}
    closed_jobs = reconciler.close_missing(fetched, configured)
    reconciler.prune()
    state.sources = {key: value for key, value in state.sources.items() if key in configured}

    report.closed = len(closed_jobs)
    for result in report.results:
        result.closed = sum(job.source_key == result.company.source_key for job in closed_jobs)

    # --- notify ------------------------------------------------------------------------
    if health_report:
        notification = health.health_notification(health_report, state.sources)
        if notifier.send_health(notification):
            health.mark_alerted(state.sources, health_report, now)
            report.health_alert_sent = True

    # A filter change that surfaces nothing new is not worth a notification; a newly
    # added company is, even with zero matches, as confirmation that it is being watched.
    if baseline_ran and (baselined_jobs or not filters_changed):
        notification = baseline_notification(baselined_jobs, config, filters_changed)
        report.baseline_sent = notifier.send_job(notification)

    # Everything not yet announced: this run's new jobs plus any whose alert failed to
    # send on an earlier run. A job is marked notified only once ntfy has accepted it.
    pending = [job for job in state.jobs.values() if job.status == OPEN and not job.notified]
    for notification in plan_job_notifications(pending, config):
        if notifier.send_job(notification):
            report.job_alerts_sent += 1
            for key in notification.job_keys:
                state.jobs[key].notified = True
        else:
            report.job_alerts_failed += 1

    # --- persist and report ------------------------------------------------------------
    if save:
        save_state(state, state_dir)
    log_report(report)
    write_step_summary(report)
    return report


def fetch_all(companies: list[Company], http: HttpClient, max_workers: int) -> list[SourceResult]:
    """Fetch every company in a small thread pool, returning results in config order.

    Threads only overlap across different hosts; HttpClient serialises and spaces out
    requests to any single host.
    """
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(lambda company: fetch_company(company, http), companies))


def fetch_company(company: Company, http: HttpClient) -> SourceResult:
    """Fetch one company. Never raises: every failure becomes a SourceResult."""
    result = SourceResult(company=company)
    started = time.monotonic()
    try:
        result.jobs = ADAPTERS[company.ats](company, http)
    except httpx.TimeoutException as exc:
        result.status, result.error = TIMEOUT, type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - one broken source must never fail the run
        result.status, result.error = ERROR, _describe_error(exc)
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url.host}"
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {first_line}".rstrip(": ")


def log_report(report: RunReport) -> None:
    for result in report.results:
        log.event(
            "source_summary",
            company=result.company.key,
            source=result.company.ats,
            status=result.status,
            fetched=len(result.jobs),
            matched=result.matched,
            new=result.new,
            baselined=result.baselined,
            reposts=result.reposts,
            closed=result.closed,
            error=result.error,
            duration_ms=result.duration_ms,
        )
    log.event(
        "run_summary",
        sources_ok=sum(result.status == OK for result in report.results),
        sources_failed=sum(result.status != OK for result in report.results),
        fetched=sum(len(result.jobs) for result in report.results),
        matched=report.total("matched"),
        new=report.total("new"),
        baselined=report.total("baselined"),
        reposts=report.total("reposts"),
        closed=report.closed,
        job_alerts_sent=report.job_alerts_sent,
        job_alerts_failed=report.job_alerts_failed,
        health_alert_sent=report.health_alert_sent,
    )


def write_step_summary(report: RunReport) -> None:
    """Show the per-source table on the GitHub Actions run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "| Company | Source | Status | Fetched | Matched | New | Closed | ms |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for result in report.results:
        lines.append(
            f"| {result.company.name} | {result.company.ats} | {result.status} "
            f"| {len(result.jobs)} | {result.matched} | {result.new} | {result.closed} "
            f"| {result.duration_ms} |"
        )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
