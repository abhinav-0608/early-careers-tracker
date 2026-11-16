"""One run of the tracker, start to finish.

    fetch every watched company once  ->  record board health, send health alerts
        ->  then, for each profile separately:
              classify the jobs of the companies in its scope with its own filters
              ->  read the descriptions its eligibility check needs (new postings only)
              ->  reconcile with its own state  ->  send its alerts  ->  save its state

Two kinds of isolation hold the run together:

  Sources.   Each company is fetched inside its own error boundary, so a broken source
             produces a failed SourceResult and a health alert while every other source
             carries on.
  Profiles.  The fetched jobs are shared and read-only. Everything after that (filters,
             scope, FAANG+, dedupe state, notified flags, ntfy topic) belongs to one
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

from jobtracker import eligibility, health, log, pauses, readme
from jobtracker.config import WORKDAY_SEARCH, Config, Filters, Pause, Profile, Settings
from jobtracker.dedupe import BASELINED, NEW, REPOST, Reconciler
from jobtracker.filters import Decision, classify
from jobtracker.http import HttpClient, RateLimitedError, RobotsDisallowedError
from jobtracker.models import (
    BACKING_OFF,
    BLOCKED,
    ERROR,
    NOT_ASKED,
    NOT_DUE,
    OK,
    PAUSED,
    RATE_LIMITED,
    TIMEOUT,
    Company,
    Job,
    SourceResult,
)
from jobtracker.notify import Notifier, baseline_notification, plan_job_notifications
from jobtracker.sources import adapter_for, descriptions, workday
from jobtracker.state import (
    OPEN,
    ProfileState,
    SharedState,
    SourceState,
    StoredJob,
    load_profile_state,
    load_shared,
    load_sources,
    save_profile_state,
    save_sources,
)

# A run that starts a little early must not push an hourly source back a whole run, so
# an interval counts as elapsed this long before it strictly has.
POLL_TOLERANCE = timedelta(minutes=2)

# A description that cannot be read is tried again on this many runs before the posting
# is treated as not saying who may apply.
DESCRIPTION_TRIES = 3

# Why a profile took no part in a run (ProfileReport.status), besides OK and ERROR.
INVALID = "invalid"  # its config files failed validation
SKIPPED = "skipped"  # no ntfy topic to send its alerts to


@dataclass
class SourceCounts:
    """What one profile made of one board in this run."""

    matched: int = 0
    ineligible: int = 0  # passed the filters, but not open (or not clearly) to first years
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
    descriptions_read: int = 0  # requests made for the eligibility check
    # (company key, title, why) for every posting the eligibility check dropped.
    dropped: list[tuple[str, str, str]] = field(default_factory=list)

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
    readme_path: Path | str | None = None,
) -> RunReport:
    settings = config.settings
    report = RunReport()

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
    shared = load_shared(state_dir)
    # A paused kind of source (settings.yaml, pauses) is not asked at all, apart from
    # one probe now and then; once a probe succeeds it is polled again, gently.
    held, resumed = resolve_pauses(config, shared, targets, http, notifier, now)

    def interval(company: Company, kind: str | None) -> int:
        minutes = settings.poll_interval(kind)
        if company.ats in resumed:
            minutes = max(minutes, resumed[company.ats].resume_every_minutes)
        return minutes

    due: list[Company] = []
    search_due: dict[str, bool] = {}  # Workday: are the large-site searches due?
    search_pages: dict[str, int] = {}  # Workday: pages per search term, when not the usual
    for company in targets:
        if company.ats in held:
            continue
        last = sources.get(company.source_key)
        if interval_elapsed(last, interval(company, company.poll_kind), now):
            due.append(company)
            search_due[company.key] = interval_elapsed(last, interval(company, WORKDAY_SEARCH), now)
            if company.ats in resumed:
                search_pages[company.key] = 1  # small searches while it is eased back in
    fetched = {
        result.company.key: result
        for result in fetch_all(due, http, settings, search_due, search_pages)
    }
    report.results = [
        fetched.get(company.key)
        or SourceResult(company=company, status=PAUSED if company.ats in held else NOT_DUE)
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
        if result.status == OK and interval(result.company, result.polled_kind) > 0:
            # To the minute: all the hourly sources of a run then share one timestamp.
            source.interval_polled_at = now.replace(second=0, microsecond=0).isoformat()
    configured = {company.source_key for company in pollable}
    sources = {key: value for key, value in sources.items() if key in configured}

    # One alert, failures grouped by platform and cause, each group at most once a day.
    alert = health.plan_alert(health_report, sources, shared, now, settings)
    if alert and notifier.send_health(alert.notification):
        health.mark_alerted(sources, health_report, alert, shared, now)
        report.health_alert_sent = True
    health.forget_quiet_groups(shared, now, settings)

    # --- each profile, on its own ------------------------------------------------------
    read: dict[str, str | None] = {}  # descriptions read this run, shared by the profiles
    for profile in active:
        try:
            profile_report = run_profile(
                profile,
                config,
                report.results,
                pollable,
                state_dir,
                notifier,
                now,
                save,
                http=http,
                read=read,
                readme_path=readme_path,
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
        save_sources(sources, state_dir, shared)
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
    http: HttpClient | None = None,
    read: dict[str, str | None] | None = None,
    readme_path: Path | str | None = None,
) -> ProfileReport:
    """Evaluate one profile against this run's fetched jobs, alert it, save its state.

    Reads the shared results and writes only this profile's state, report and topic.
    `read` holds the descriptions already read in this run, so two profiles that need
    the same one cost one request.
    """
    settings, filters = config.settings, profile.filters
    state = load_profile_state(state_dir, profile.key)
    in_scope = {company.source_key for company in pollable if profile.watches(company)}
    report = ProfileReport(profile=profile.key, companies=len(in_scope))
    read = {} if read is None else read

    # --- classify ----------------------------------------------------------------------
    # Title, field and location first, for every board. Only what passes goes on.
    matches: dict[str, list[tuple[Job, Decision]]] = {}
    for result in results:
        company = result.company
        # A failed fetch, or a source its polling interval skipped this run, is left
        # exactly as it was: nothing new, nothing counted as missing, nothing closed.
        if result.status != OK or not profile.watches(company):
            continue
        matches[company.key] = [
            (job, decision)
            for job in result.jobs
            if (decision := classify(job, company, filters)).matched
        ]

    # --- who may apply -----------------------------------------------------------------
    # Verdicts made under other rules are forgotten, so a change to the eligibility
    # keywords is applied to every open posting, not just to new ones.
    rules_changed = state.eligibility_rules != filters.eligibility.hash
    if rules_changed:
        state.eligibility = {}
        state.eligibility_rules = filters.eligibility.hash
    # Postings already settled before this run: their drops were logged when they were.
    settled_before = {
        key
        for key, value in state.eligibility.items()
        if value.get("status") != eligibility.UNKNOWN
    }
    wanted = [
        (result.company, job)
        for result in results
        for job, decision in matches.get(result.company.key, ())
        if _needs_reading(job, decision, state, read, filters)
    ]
    if wanted and http is not None:
        before = len(read)
        # The run in which the rules are new or changed re-checks every open posting,
        # with a larger budget, so the baseline completes in it and is one summary.
        limit = (
            settings.descriptions_when_rules_change
            if rules_changed
            else settings.descriptions_per_run
        )
        read_descriptions(wanted, http, settings, read, limit)
        report.descriptions_read = len(read) - before

    # --- reconcile ---------------------------------------------------------------------
    reconciler = Reconciler(state, now, settings)
    baselined_jobs: list[StoredJob] = []
    filters_changed = False
    fetched: set[str] = set()

    for result in results:
        company = result.company
        if company.key not in matches:
            continue
        verdicts = [
            (job, decision, _verdict(job, decision, state, read, filters))
            for job, decision in matches[company.key]
        ]

        # A board is baselined (recorded without individual alerts) the first time this
        # profile sees it and again whenever the profile's filter rules have changed since.
        previous_hash = state.baselines.get(company.source_key)
        baseline = previous_hash != filters.hash
        # Part of the board was not read this run, or a description on it could not be
        # read yet. A baseline taken now would be incomplete, and the rest would then
        # arrive as a flood of alerts, so the baseline waits for the next complete run.
        undecided = any(verdict.status == eligibility.UNKNOWN for _, _, verdict in verdicts)
        if baseline and (result.partial or undecided):
            continue
        if not result.partial:
            # Only a complete fetch can show that a job has gone.
            fetched.add(company.source_key)
            _forget_verdicts(state, company, {job.key for job, _, _ in verdicts})
        counts = report.sources.setdefault(company.source_key, SourceCounts())
        if baseline:
            filters_changed = filters_changed or previous_hash is not None
            state.baselines[company.source_key] = filters.hash

        for job, decision, verdict in verdicts:
            if verdict.status in (*eligibility.DROPPED, eligibility.UNKNOWN):
                # Not for first years, or not known yet (tried again next run).
                counts.ineligible += 1
                report.dropped.append((company.key, job.title, verdict.reason))
                if job.key not in settled_before:
                    log.event(
                        "eligibility_dropped",
                        profile=profile.key,
                        company=company.key,
                        title=job.title,
                        why=verdict.reason,
                    )
                continue
            counts.matched += 1
            outcome = reconciler.observe(job, decision, baseline, verdict.basis, verdict.note)
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
    notification = baseline_notification(baselined_jobs, profile, config, filters_changed)
    if notification:
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
        if readme_path and settings.readme_profile == profile.key:
            # The state is already saved; a README that cannot be written costs the
            # table one run, not the profile its alerts.
            try:
                readme.update(readme_path, readme.open_roles(state, profile, config, now.date()))
            except Exception as exc:  # noqa: BLE001
                log.warning("readme_failed", profile=profile.key, error=_describe_error(exc))
    return report


def _needs_reading(
    job: Job, decision: Decision, state: ProfileState, read: dict, filters: Filters
) -> bool:
    """Must this posting's description be requested before its eligibility is known?"""
    role = filters.role_type(decision.role_type or "")
    if role is None or not eligibility.needs_description(role, job.title, filters):
        return False  # the title settles it, or this role type is not checked
    if job.description is not None or job.key in read:
        return False  # already in hand
    remembered = state.eligibility.get(job.key, {})
    return remembered.get("status") in (None, eligibility.UNKNOWN) or _before_notes(remembered)


def _before_notes(remembered: dict) -> bool:
    """A description verdict stored before notes on preferred years existed (2026-11-16).
    Its description is read once more to find any; if that read fails, it stands as it is."""
    return (
        remembered.get("status") == eligibility.ELIGIBLE
        and remembered.get("basis", "").startswith(f"{eligibility.DESCRIPTION}:")
        and "note" not in remembered
    )


def _verdict(
    job: Job, decision: Decision, state: ProfileState, read: dict, filters: Filters
) -> eligibility.Eligibility:
    """Decide one posting's eligibility, remembering what a description settled."""
    role = filters.role_type(decision.role_type or "")
    if role is None:
        return eligibility.Eligibility(eligibility.NOT_APPLICABLE)
    if not eligibility.needs_description(role, job.title, filters):
        return eligibility.assess(role, job.title, None, filters)

    remembered = state.eligibility.get(job.key, {})
    text = job.description if job.description is not None else read.get(job.key)
    if _before_notes(remembered):
        _add_note(job, role, text, remembered, read, filters)
    if remembered.get("status") == eligibility.ELIGIBLE:
        return eligibility.Eligibility(
            eligibility.ELIGIBLE,
            eligibility.REMEMBERED,
            remembered.get("basis", ""),
            note=remembered.get("note", ""),
        )
    if remembered.get("status") in eligibility.DROPPED:
        return eligibility.Eligibility(remembered["status"], evidence=remembered.get("why", ""))

    verdict = eligibility.assess(
        role, job.title, None if text is None else descriptions.plain_text(text), filters
    )
    if verdict.status == eligibility.UNKNOWN:
        tries = remembered.get("tries", 0) + (1 if job.key in read else 0)
        if tries < DESCRIPTION_TRIES:
            state.eligibility[job.key] = {"status": eligibility.UNKNOWN, "tries": tries}
            return verdict
        # It cannot be read. Without it nothing says first years may apply.
        verdict = eligibility.Eligibility(eligibility.UNCLEAR)
    record = {"status": verdict.status}
    if verdict.eligible:
        record["basis"] = verdict.basis
        if verdict.source == eligibility.DESCRIPTION:
            record["note"] = verdict.note  # "" too: says the description was looked at
    elif verdict.evidence:
        record["why"] = verdict.evidence
    state.eligibility[job.key] = record
    return verdict


def _add_note(
    job: Job, role, text: str | None, remembered: dict, read: dict, filters: Filters
) -> None:
    """Look for a preference note in a verdict settled before notes existed.

    This only ever adds a note: the verdict itself stands whatever the description now
    says (empty, disallowed, gone or edited), so the read cannot close or drop an open
    role. A read that keeps failing is given up after DESCRIPTION_TRIES runs.
    """
    if text is None:
        tries = remembered.get("note_tries", 0) + (1 if job.key in read else 0)
        if tries < DESCRIPTION_TRIES:
            if tries:
                remembered["note_tries"] = tries
            return
        text = ""
    fresh = eligibility.assess(role, job.title, descriptions.plain_text(text), filters)
    found = fresh.eligible and fresh.source == eligibility.DESCRIPTION
    remembered.pop("note_tries", None)
    remembered["note"] = fresh.note if found else ""


def _forget_verdicts(state: ProfileState, company: Company, still_matching: set[str]) -> None:
    """Drop the remembered verdicts of postings that have left this board."""
    prefix = company.source_key + ":"
    for key in [key for key in state.eligibility if key.startswith(prefix)]:
        if key not in still_matching:
            del state.eligibility[key]


def read_descriptions(
    wanted: list[tuple[Company, Job]],
    http: HttpClient,
    settings: Settings,
    read: dict[str, str | None],
    limit: int | None = None,
) -> None:
    """Read each posting's description into `read`, one queue per host.

    A posting that cannot be read is recorded as None and tried again on a later run;
    one whose page is gone, or may not be read, as "" (there is nothing to read). At
    most `limit` (normally `descriptions_per_run`) are requested in a run, counting
    those earlier profiles read in it; the rest wait for the next.
    """
    limit = settings.descriptions_per_run if limit is None else limit
    budget = max(0, limit - len(read))
    queues: dict[str, list[tuple[Company, Job]]] = {}
    for company, job in wanted[:budget]:
        queues.setdefault(company.host_group, []).append((company, job))

    def work(queue: list[tuple[Company, Job]]) -> list[tuple[str, str | None]]:
        results: list[tuple[str, str | None]] = []
        for company, job in queue:
            try:
                text: str | None = descriptions.describe(company, job, http)
            except RobotsDisallowedError:
                text = ""
            except httpx.HTTPStatusError as exc:
                text = "" if exc.response.status_code in (404, 410) else None
            except Exception as exc:  # noqa: BLE001 - one posting must never fail the run
                text = None
                log.warning("description_failed", job=job.key, error=_describe_error(exc))
            results.append((job.key, text))
        return results

    requested = unreadable = 0
    with ThreadPoolExecutor(max_workers=settings.max_workers) as pool:
        for results in pool.map(work, sorted(queues.values(), key=len, reverse=True)):
            read.update(results)
            requested += len(results)
            unreadable += sum(1 for _, text in results if text is None)
    log.event(
        "descriptions_read",
        requested=requested,
        unreadable=unreadable,
        waiting=len(wanted) - requested,
    )


def resolve_pauses(
    config: Config,
    shared: SharedState,
    targets: list[Company],
    http: HttpClient,
    notifier: Notifier,
    now: datetime,
) -> tuple[set[str], dict[str, Pause]]:
    """Decide this run's stage for each paused kind of source, probing if one is due.

    Returns the kinds to leave alone this run and the kinds that have resumed. A kind
    no profile is watching is left alone without a probe.
    """
    held: set[str] = set()
    resumed: dict[str, Pause] = {}
    watched_kinds = {company.ats for company in targets}
    for kind, pause in config.settings.pauses.items():
        memory = shared.pauses.setdefault(kind, {})
        current = pauses.stage(pause, memory, now)
        if current == pauses.PROBE and kind in watched_kinds:
            company = config.company(pause.probe_company)
            current = pauses.probe(pause, company, http, memory, now)
        if current == pauses.RESUMED:
            resumed[kind] = pause
        else:
            held.add(kind)
        notification = pauses.announcement(pause, current, memory)
        if notification and notifier.send_health(notification):
            memory["announced"] = current
        if not memory:
            del shared.pauses[kind]  # nothing to remember yet: keep the file unchanged
        log.event("pause", kind=kind, stage=current)
    for kind in list(shared.pauses):
        if kind not in config.settings.pauses:
            del shared.pauses[kind]  # the pause was removed from settings.yaml
    return held, resumed


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
    search_pages: dict[str, int] | None = None,
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
    search_pages = search_pages or {}
    queues: dict[str, list[Company]] = {}
    for company in companies:
        queues.setdefault(company.host_group, []).append(company)
    longest_first = sorted(queues.values(), key=len, reverse=True)

    def work(queue: list[Company]) -> list[SourceResult]:
        return [
            fetch_company(
                company,
                http,
                search=search_due.get(company.key, True),
                search_pages=search_pages.get(company.key, workday.MAX_PAGES_PER_SEARCH),
            )
            for company in queue
        ]

    with ThreadPoolExecutor(max_workers=settings.max_workers) as pool:
        fetched = {
            result.company.key: result
            for results in pool.map(work, longest_first)
            for result in results
        }
    return [fetched[company.key] for company in companies]


def fetch_company(
    company: Company,
    http: HttpClient,
    search: bool = True,
    search_pages: int = workday.MAX_PAGES_PER_SEARCH,
) -> SourceResult:
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
            result.jobs, large_sites = workday.fetch_sites(company, http, search, search_pages)
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
                    ineligible=counts.ineligible,
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
            ineligible=profile.total("ineligible"),
            descriptions_read=profile.descriptions_read,
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
        sources_paused=sum(result.status == PAUSED for result in report.results),
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
