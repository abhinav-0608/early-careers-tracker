"""Track whether each source is working and decide when to raise a health alert.

Alerts follow state changes rather than runs, and are grouped by root cause. A source
counts as failing once it has failed `fail_after_consecutive` runs in a row. Failing
sources are grouped by platform and cause ("Workday: 64 sources blocked (Cloudflare
429)"), each group is one line of one alert, and a group is announced at most once every
`reminder_hours` (a day) however many sources join it. Recoveries are grouped by
platform and sent as they happen. A platform that is down for a week therefore sends
about eight messages, not one per source, and not one every fifteen minutes.

A host answering 429 (rate limited) is treated as passing: it takes
`fail_after_rate_limited` refusals in a row, and a run in which the backed-off host was
not asked at all counts as neither a failure nor a success.

Health is shared, not per profile: a board is fetched once per run and is either working
or not, whoever watches it. Health alerts go to the one health topic (the person running
the tracker), never to a profile's job topic.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobtracker.config import Settings
from jobtracker.models import BLOCKED, EMPTY, OK, RATE_LIMITED, TIMEOUT, Company, SourceResult
from jobtracker.notify import Notification, fit_lines
from jobtracker.state import SharedState, SourceState

MAX_ERROR_LENGTH = 160


@dataclass
class HealthReport:
    failing: list[SourceResult] = field(default_factory=list)  # over the threshold this run
    recovered: list[SourceResult] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.failing or self.recovered)


def check_empty(source: SourceState, result: SourceResult, settings: Settings) -> None:
    """Downgrade a successful-but-empty fetch to EMPTY if the board used to have jobs.

    An empty response from a board that had jobs last time is far more likely to be a
    broken board id or a changed API than a company that deleted every posting, so we
    treat it as a failure: nothing gets closed and a health alert goes out. If it stays
    empty for `accept_empty_after_runs` runs we accept that it really is empty.
    """
    if result.status != OK:
        return
    if result.jobs:
        source.has_returned_jobs = True
        source.empty_runs = 0
    elif source.has_returned_jobs:
        source.empty_runs += 1
        if source.empty_runs < settings.accept_empty_after_runs:
            result.status = EMPTY
            result.error = "returned 0 jobs after previously returning some"
        else:
            source.has_returned_jobs = False
            source.empty_runs = 0


def record_outcome(
    source: SourceState,
    result: SourceResult,
    now: datetime,
    settings: Settings,
    report: HealthReport,
) -> None:
    """Update one source's failure streak; add it to the report once it counts as failing."""
    if result.status == OK:
        if source.alerted:
            report.recovered.append(result)
        source.consecutive_failures = 0
        source.failing_since = None
        source.alerted = False
        source.last_alert_at = None
        return

    if source.failing_since is None:
        source.failing_since = now.isoformat(timespec="seconds")
    # A rate limit (429) is usually short-lived and the host is backed off between
    # attempts, so it has to persist for more attempts before it is worth an alert.
    threshold = (
        settings.fail_after_rate_limited
        if result.status == RATE_LIMITED
        else settings.fail_after_consecutive
    )
    # Capped so that a long outage does not change the state file on every run.
    cap = max(settings.fail_after_consecutive, settings.fail_after_rate_limited)
    source.consecutive_failures = min(source.consecutive_failures + 1, cap)
    if source.consecutive_failures >= threshold:
        report.failing.append(result)


# --- grouping ---------------------------------------------------------------------------

PLATFORMS = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters",
    "workable": "Workable",
    "workday": "Workday",
    "talnet": "tal.net",
    "eightfold": "Eightfold",
}


def platform(company: Company) -> str:
    """The system a source runs on, as people call it. A custom site is its company."""
    return PLATFORMS.get(company.ats, company.name)


def cause(result: SourceResult) -> str:
    """Why a source is failing, worded to follow "N sources"."""
    error = result.error or ""
    if result.status == RATE_LIMITED:
        return "blocked (Cloudflare 429)" if "Cloudflare" in error else "rate limited (429)"
    if result.status == BLOCKED:
        return "blocked by robots.txt"
    if result.status == TIMEOUT:
        return "timing out"
    if result.status == EMPTY:
        return "returning no jobs"
    status = re.search(r"HTTP (\d{3})", error)
    return f"failing (HTTP {status.group(1)})" if status else "failing (error)"


def group_key(result: SourceResult) -> str:
    return f"{platform(result.company)}: {cause(result)}"


@dataclass
class Alert:
    notification: Notification
    groups: list[str]  # the failure groups it announces


def plan_alert(
    report: HealthReport,
    sources: dict[str, SourceState],
    shared: SharedState,
    now: datetime,
    settings: Settings,
) -> Alert | None:
    """The one health alert this run should send, or None.

    A failure group is due when it has not been announced for `reminder_hours`. A
    source that joins a group announced less than that ago is covered by that alert
    and is marked alerted straight away, so its recovery is reported later.
    """
    groups: dict[str, list[SourceResult]] = defaultdict(list)
    for result in report.failing:
        groups[group_key(result)].append(result)
    reminder = timedelta(hours=settings.reminder_hours)

    def announced_recently(key: str, members: list[SourceResult]) -> bool:
        if key in shared.alert_groups:
            return now - datetime.fromisoformat(shared.alert_groups[key]) < reminder
        # No group record yet (state from before alerts were grouped): a group whose
        # every source was alerted within the day has been announced already.
        return all(
            (state := sources[r.company.source_key]).alerted
            and state.last_alert_at is not None
            and now - datetime.fromisoformat(state.last_alert_at) < reminder
            for r in members
        )

    due = {key: members for key, members in groups.items() if not announced_recently(key, members)}
    for key, members in groups.items():
        if key not in due:
            for result in members:
                sources[result.company.source_key].alerted = True

    recovered: dict[str, list[SourceResult]] = defaultdict(list)
    for result in report.recovered:
        recovered[platform(result.company)].append(result)
    if not due and not recovered:
        return None

    lines: list[str] = []
    for key in sorted(due, key=lambda k: (-len(due[k]), k)):
        members = sorted(due[key], key=lambda r: r.company.name.lower())
        lines.append(_group_line(key, len(members)))
        names = [result.company.name for result in members]
        shown = ", ".join(names[:6]) + (f" +{len(names) - 6} more" if len(names) > 6 else "")
        lines.append(f"  {shown}")
        if len(members) == 1 and members[0].error:
            lines.append(f"  {members[0].error[:MAX_ERROR_LENGTH]}")
        since = min(sources[r.company.source_key].failing_since or "" for r in members)
        if since:
            lines.append(f"  failing since {since}")
    for name in sorted(recovered):
        count = len(recovered[name])
        lines.append(f"Recovered: {name}: {count} {'source' if count == 1 else 'sources'}")

    if due:
        total = sum(len(members) for members in due.values())
        if total == 1:
            (only,) = next(iter(due.values()))
            title = f"{_label(only.company)}: {cause(only)}"
        elif len(due) == 1:
            title = _group_line(next(iter(due)), total)
        else:
            title = f"{total} sources failing"
        notification = Notification(
            title=title, message=fit_lines(lines), priority=4, tags=("warning",)
        )
    else:
        total = sum(len(members) for members in recovered.values())
        if total == 1:
            (only,) = next(iter(recovered.values()))
            title = f"{_label(only.company)} recovered"
        elif len(recovered) == 1:
            title = f"{next(iter(recovered))}: {total} sources recovered"
        else:
            title = f"{total} sources recovered"
        notification = Notification(
            title=title, message=fit_lines(lines), priority=2, tags=("white_check_mark",)
        )
    return Alert(notification=notification, groups=sorted(due))


def _label(company: Company) -> str:
    """ "Palantir (Lever)", or just "Google" for a custom site."""
    name = platform(company)
    return company.name if name == company.name else f"{company.name} ({name})"


def _group_line(key: str, count: int) -> str:
    """ "Workday: blocked (Cloudflare 429)", 64 -> "Workday: 64 sources blocked (...)"."""
    name, why = key.split(": ", 1)
    return f"{name}: {count} {'source' if count == 1 else 'sources'} {why}"


def mark_alerted(
    sources: dict[str, SourceState],
    report: HealthReport,
    alert: Alert,
    shared: SharedState,
    now: datetime,
) -> None:
    """Call only after the alert was delivered, so a failed send is retried next run."""
    stamp = now.isoformat(timespec="seconds")
    for result in report.failing:
        if group_key(result) in alert.groups:
            source = sources[result.company.source_key]
            source.alerted = True
            source.last_alert_at = stamp
    for key in alert.groups:
        shared.alert_groups[key] = stamp


def forget_quiet_groups(shared: SharedState, now: datetime, settings: Settings) -> None:
    """Drop group timestamps that no longer hold anything back, to keep the file small."""
    reminder = timedelta(hours=settings.reminder_hours)
    shared.alert_groups = {
        key: stamp
        for key, stamp in shared.alert_groups.items()
        if now - datetime.fromisoformat(stamp) < reminder
    }
