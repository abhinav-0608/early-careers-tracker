"""One run of the tracker, start to finish.

    fetch every watched company once  ->  record board health, send health alerts
        ->  then, for each profile separately:
              classify the jobs of the companies in its scope with its own filters
              ->  reconcile with its own state  ->  send its alerts  ->  save its state

Two kinds of isolation hold the run together:

  Sources.   Each company is fetched inside its own error boundary, so a broken source
             produces a failed SourceResult and a health alert while every other source
             carries on.
  Profiles.  The fetched jobs are shared and read-only. Everything after that (filters,
             scope, dream tier, dedupe state, notified flags, ntfy topic) belongs to one
             profile and is handled inside that profile's own error boundary. A profile
             that is misconfigured, has no topic, or crashes is reported and left out;
             what the other profiles receive does not change.
"""

from __future__ import annotations

import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from jobtracker import health, log
from jobtracker.config import WORKDAY_SEARCH, Config, Profile, Settings
from jobtracker.dedupe import BASELINED, NEW, REPOST, Reconciler
from jobtracker.filters import classify
from jobtracker.http import HttpClient, RateLimitedError, RobotsDisallowedError
from jobtracker.models import (
    BACKING_OFF,
    BLOCKED,
    ERROR,
    NOT_ASKED,
    NOT_DUE,
    OK,
    RATE_LIMITED,
    TIMEOUT,
    Company,
    SourceResult,
)
from jobtracker.notify import Notifier, baseline_notification, plan_job_notifications
from jobtracker.sources import adapter_for, workday
from jobtracker.state import (
    LEGACY_OWNER,
    OPEN,
    SourceState,
    StoredJob,
    load_profile_state,
    load_sources,
    migrate_legacy,
    save_profile_state,
    save_sources,
)

# A run that starts a little early must not push an hourly source back a whole run, so
# an interval counts as elapsed this long before it strictly has.
POLL_TOLERANCE = timedelta(minutes=2)

# Why a profile took no part in a run (ProfileReport.status), besides OK and ERROR.
INVALID = "invalid"  # its config files failed validation
SKIPPED = "skipped"  # no ntfy topic to send its alerts to


@dataclass
class SourceCounts:
    """What one profile made of one board in this run."""

    matched: int = 0
    new: int = 0
    baselined: int = 0
    reposts: int = 0
    closed: int = 0


@dataclass
class ProfileReport:
    profile: str  # profile key
    status: str = OK
    error: str | None = None
    companies: int = 0  # pollable companies in the profile's scope
    sources: dict[str, SourceCounts] = field(default_factory=dict)  # by source key
    job_alerts_sent: int = 0
    job_alerts_failed: int = 0
    baseline_sent: bool = False

    def total(self, attribute: str) -> int:
        return sum(getattr(counts, attribute) for counts in self.sources.values())


@dataclass
class RunReport:
    results: list[SourceResult] = field(default_factory=list)  # one per company fetched
    profiles: list[ProfileReport] = field(default_factory=list)
    health_alert_sent: bool = False

    def profile(self, key: str) -> ProfileReport:
        return next(report for report in self.profiles if report.profile == key)

    def total(self, attribute: str) -> int:
        """A count such as "new" or "closed", summed over every profile."""
        return sum(report.total(attribute) for report in self.profiles)

    @property
    def closed(self) -> int:
        return self.total("closed")

    @property
    def job_alerts_sent(self) -> int:
        return sum(report.job_alerts_sent for report in self.profiles)

    @property
    def job_alerts_failed(self) -> int:
        return sum(report.job_alerts_failed for report in self.profiles)

    @property
    def any_profile_ran(self) -> bool:
        return any(report.status == OK for report in self.profiles)


def run(
    config: Config,
    state_dir: Path | str,
    http: HttpClient,
    notifier: Notifier,
    now: datetime,
    only: set[str] | None = None,
    only_profiles: set[str] | None = None,
    save: bool = True,
) -> RunReport:
    settings = config.settings
    report = RunReport()
    if save and migrate_legacy(state_dir):
        log.event("state_migrated", profile=LEGACY_OWNER)

    # --- who takes part ----------------------------------------------------------------
    for key, error in config.profile_errors.items():
        report.profiles.append(ProfileReport(profile=key, status=INVALID, error=error))
    active: list[Profile] = []
    for profile in config.profiles:
        if only_profiles and profile.key not in only_profiles:
            continue
        if notifier.can_reach(profile.key):
            active.append(profile)
        else:
            # Its state is left untouched, so it starts with a baseline once it has a topic.
            error = f"{profile.topic_env} is not set"
            report.profiles.append(ProfileReport(profile=profile.key, status=SKIPPED, error=error))

    # --- fetch, once per company -------------------------------------------------------
    # Companies with no adapter (ats: unsupported, or a source not built yet) are kept in
    # the config for visibility but are not polled. Nor is one whose kind of source is
    # switched off in settings.yaml (polling.disabled), nor one that nobody is watching.
    pollable = [
        company
        for company in config.companies
        if adapter_for(company) and company.poll_kind not in settings.disabled_sources
    ]
    watched = [c for c in pollable if any(profile.watches(c) for profile in active)]
    targets = [company for company in watched if not only or company.key in only]
    log.event(
        "run_started",
        profiles=[profile.key for profile in active],
        companies=len(targets),
        skipped=len(config.companies) - len(pollable),
    )
    # A source with a polling interval is fetched only when the interval has passed since
    # its last poll. One that is not due gets a NOT_DUE result and no request at all.
    sources = load_sources(state_dir)
    due: list[Company] = []
    search_due: dict[str, bool] = {}  # Workday: are the large-site searches due?
    for company in targets:
        last = sources.get(company.source_key)
        if interval_elapsed(last, settings.poll_interval(company.poll_kind), now):
            due.append(company)
            search_due[company.key] = interval_elapsed(
                last, settings.poll_interval(WORKDAY_SEARCH), now
            )
    fetched = {result.company.key: result for result in fetch_all(due, http, settings, search_due)}
    report.results = [
        fetched.get(company.key) or SourceResult(company=company, status=NOT_DUE)
        for company in targets
    ]

    # --- board health (shared) ---------------------------------------------------------
    health_report = health.HealthReport()
    for result in report.results:
        if result.status in NOT_ASKED:
            continue  # nothing was asked of it, so there is nothing to record
        source = sources.setdefault(result.company.source_key, SourceState())
        if not result.partial:  # a partial fetch says nothing about the board being empty
            health.check_empty(source, result, settings)
        health.record_outcome(source, result, now, settings, health_report)
        if result.status == OK and settings.poll_interval(result.polled_kind) > 0:
            # To the minute: all the hourly sources of a run then share one timestamp.
            source.interval_polled_at = now.replace(second=0, microsecond=0).isoformat()
    configured = {company.source_key for company in pollable}
    sources = {key: value for key, value in sources.items() if key in configured}

    if health_report:
        notification = health.health_notification(health_report, sources)
        if notifier.send_health(notification):
            health.mark_alerted(sources, health_report, now)
            report.health_alert_sent = True

    # --- each profile, on its own ------------------------------------------------------
    for profile in active:
        try:
            profile_report = run_profile(
                profile, config, report.results, pollable, state_dir, notifier, now, save
            )
        except Exception as exc:  # noqa: BLE001 - one profile must never stop another
            # Its state file was not written, so nothing half-done is kept: the next run
            # starts from the last saved state and may repeat an alert (at-least-once).
            profile_report = ProfileReport(
                profile=profile.key, status=ERROR, error=_describe_error(exc)
            )
            log.warning("profile_crashed", profile=profile.key, traceback=traceback.format_exc())
        report.profiles.append(profile_report)

    # --- persist and report ------------------------------------------------------------
    if save:
        save_sources(sources, state_dir)
    log_report(report)
    write_step_summary(report)
    return report


def run_profile(
    profile: Profile,
    config: Config,
    results: list[SourceResult],
    pollable: list[Company],
    state_dir: Path | str,
    notifier: Notifier,
    now: datetime,
    save: bool,
) -> ProfileReport:
    """Evaluate one profile against this run's fetched jobs, alert it, save its state.

    Reads the shared results and writes only this profile's state, report and topic.
    """
    settings, filters = config.settings, profile.filters
    state = load_profile_state(state_dir, profile.key)
    in_scope = {company.source_key for company in pollable if profile.watches(company)}
    report = ProfileReport(profile=profile.key, companies=len(in_scope))

    # --- classify and reconcile --------------------------------------------------------
    reconciler = Reconciler(state, now, settings)
    baselined_jobs: list[StoredJob] = []
    filters_changed = False
    fetched: set[str] = set()

    for result in results:
        company = result.company
        # A failed fetch, or a source its polling interval skipped this run, is left
        # exactly as it was: nothing new, nothing counted as missing, nothing closed.
        if result.status != OK or not profile.watches(company):
            continue

        # A board is baselined (recorded without individual alerts) the first time this
        # profile sees it and again whenever the profile's filter rules have changed since.
        previous_hash = state.baselines.get(company.source_key)
        baseline = previous_hash != filters.hash
        if result.partial:
            # Part of the board was not read this run. A baseline taken from it would be
            # incomplete, and the rest would then arrive as a flood of alerts, so the
            # baseline waits for the next complete fetch.
            if baseline:
                continue
        else:
            # Only a complete fetch can show that a job has gone.
            fetched.add(company.source_key)
        counts = report.sources.setdefault(company.source_key, SourceCounts())
        if baseline:
            filters_changed = filters_changed or previous_hash is not None
            state.baselines[company.source_key] = filters.hash

        for job in result.jobs:
            decision = classify(job, company, filters)
            if not decision.matched:
                continue
            counts.matched += 1
            outcome = reconciler.observe(job, decision, baseline)
            if outcome == NEW:
                counts.new += 1
            elif outcome == REPOST:
                counts.reposts += 1
            elif outcome == BASELINED:
                counts.baselined += 1
                baselined_jobs.append(state.jobs[job.key])

    for job in reconciler.close_missing(fetched, in_scope):
        report.sources.setdefault(job.source_key, SourceCounts()).closed += 1
    reconciler.prune()
    # Forgetting a board that left the scope means it is baselined afresh if it returns.
    state.baselines = {key: value for key, value in state.baselines.items() if key in in_scope}

    # --- notify ------------------------------------------------------------------------
    # One summary for everything baselined this run. A baseline that found nothing (a
    # new company with no matching roles, a filter change that surfaces nothing) sends
    # no notification: "0 matching roles" is not worth a buzz.
    if baselined_jobs:
        notification = baseline_notification(baselined_jobs, config, filters_changed)
        report.baseline_sent = notifier.send_job(profile.key, notification)

    # Everything not yet announced: this run's new jobs plus any whose alert failed to
    # send on an earlier run. A job is marked notified only once ntfy has accepted it.
    pending = [job for job in state.jobs.values() if job.status == OPEN and not job.notified]
    for notification in plan_job_notifications(pending, profile, config):
        if notifier.send_job(profile.key, notification):
            report.job_alerts_sent += 1
            for key in notification.job_keys:
                state.jobs[key].notified = True
        else:
            report.job_alerts_failed += 1

    if save:
        save_profile_state(state, state_dir, profile.key)
    return report


def interval_elapsed(source: SourceState | None, minutes: int, now: datetime) -> bool:
    """Is a source with this polling interval due? One never polled always is."""
    if minutes <= 0 or source is None or source.interval_polled_at is None:
        return True
    last = datetime.fromisoformat(source.interval_polled_at)
    return now - last >= timedelta(minutes=minutes) - POLL_TOLERANCE


def fetch_all(
    companies: list[Company],
    http: HttpClient,
    settings: Settings,
    search_due: dict[str, bool] | None = None,
) -> list[SourceResult]:
    """Fetch every company, returning results in the order given.

    Companies are grouped by the host that serves them and each group is one queue,
    worked through by one thread. HttpClient only ever makes one request to a host at a
    time, so a second thread on the same host could do nothing but wait; giving each
    host exactly one thread keeps every worker busy on a different host. The longest
    queues start first, since the run cannot finish before they do.

    `search_due` says, per company key, whether a Workday tenant's large-site searches
    are due this run; a company not listed is fetched in full.
    """
    search_due = search_due or {}
    queues: dict[str, list[Company]] = {}
    for company in companies:
        queues.setdefault(company.host_group, []).append(company)
    longest_first = sorted(queues.values(), key=len, reverse=True)

    def work(queue: list[Company]) -> list[SourceResult]:
        return [
            fetch_company(company, http, search=search_due.get(company.key, True))
            for company in queue
        ]

    with ThreadPoolExecutor(max_workers=settings.max_workers) as pool:
        fetched = {
            result.company.key: result
            for results in pool.map(work, longest_first)
            for result in results
        }
    return [fetched[company.key] for company in companies]


def fetch_company(company: Company, http: HttpClient, search: bool = True) -> SourceResult:
    """Fetch one company. Never raises: every failure becomes a SourceResult.

    `search` only matters to Workday: False leaves a large site's searches for a later
    run (see sources/workday.py), and the result is then marked partial.
    """
    result = SourceResult(company=company)
    started = time.monotonic()
    try:
        adapter = adapter_for(company)
        if adapter is None:
            raise LookupError(f"no adapter for ats '{company.ats}'")
        if company.ats == workday.NAME:
            result.jobs, large_sites = workday.fetch_sites(company, http, search)
            result.partial = large_sites > 0 and not search
            result.polled_kind = WORKDAY_SEARCH if large_sites and search else None
        else:
            result.jobs = adapter(company, http)
            result.polled_kind = company.poll_kind
    except RateLimitedError as exc:
        # 429. Either the host said so just now (a failure, counted towards an alert),
        # or it said so earlier and was not asked at all this run (not a failure).
        result.status = RATE_LIMITED if exc.answered_now else BACKING_OFF
        result.error = str(exc)
    except RobotsDisallowedError as exc:
        # Not fetched, on the site owner's instruction. Reported like any failing source,
        # so a board that becomes disallowed is noticed and can be marked unsupported.
        result.status, result.error = BLOCKED, str(exc)
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
            partial=result.partial,
            error=result.error,
            duration_ms=result.duration_ms,
        )
    for profile in report.profiles:
        # One line per board where this profile had something happen, then its totals.
        for source_key, counts in profile.sources.items():
            if counts != SourceCounts():
                log.event(
                    "profile_source_summary",
                    profile=profile.profile,
                    source=source_key,
                    matched=counts.matched,
                    new=counts.new,
                    baselined=counts.baselined,
                    reposts=counts.reposts,
                    closed=counts.closed,
                )
        (log.event if profile.status == OK else log.warning)(
            "profile_summary",
            profile=profile.profile,
            status=profile.status,
            error=profile.error,
            companies=profile.companies,
            matched=profile.total("matched"),
            new=profile.total("new"),
            baselined=profile.total("baselined"),
            reposts=profile.total("reposts"),
            closed=profile.total("closed"),
            job_alerts_sent=profile.job_alerts_sent,
            job_alerts_failed=profile.job_alerts_failed,
            baseline_sent=profile.baseline_sent,
        )
        if profile.status != OK and os.environ.get("GITHUB_ACTIONS"):
            # Shows as a warning banner on the run page; the run itself still succeeds.
            reason = " ".join((profile.error or "unknown error").split())
            print(f"::warning title=Profile '{profile.profile}' did not run::{reason}")
    log.event(
        "run_summary",
        sources_ok=sum(result.status == OK for result in report.results),
        sources_failed=sum(result.status not in (OK, *NOT_ASKED) for result in report.results),
        sources_not_due=sum(result.status == NOT_DUE for result in report.results),
        sources_backing_off=sum(result.status == BACKING_OFF for result in report.results),
        fetched=sum(len(result.jobs) for result in report.results),
        profiles_ok=sum(profile.status == OK for profile in report.profiles),
        profiles_not_run=sum(profile.status != OK for profile in report.profiles),
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
    """Show the per-profile and per-source tables on the GitHub Actions run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "| Profile | Status | Companies | Matched | New | Baselined | Closed | Alerts sent |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for profile in report.profiles:
        status = profile.status if profile.status == OK else f"{profile.status}: {profile.error}"
        lines.append(
            f"| {profile.profile} | {status} | {profile.companies} "
            f"| {profile.total('matched')} | {profile.total('new')} "
            f"| {profile.total('baselined')} | {profile.total('closed')} "
            f"| {profile.job_alerts_sent} |"
        )
    lines += [
        "",
        "| Company | Source | Status | Fetched | ms | Matched (per profile) |",
        "|---|---|---|---:|---:|---|",
    ]
    for result in report.results:
        matched = ", ".join(
            f"{profile.profile} {profile.sources[result.company.source_key].matched}"
            for profile in report.profiles
            if result.company.source_key in profile.sources
            and profile.sources[result.company.source_key].matched
        )
        lines.append(
            f"| {result.company.name} | {result.company.ats} | {result.status} "
            f"| {len(result.jobs)} | {result.duration_ms} | {matched} |"
        )
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
