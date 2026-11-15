"""Track whether each source is working and decide when to raise a health alert.

Alerts follow state changes rather than runs. A source alerts once it has failed
`fail_after_consecutive` runs in a row, again every `reminder_hours` while it stays
broken, and once more when it recovers. A source that is down for a week therefore
sends about eight messages, not one every fifteen minutes.

Health is shared, not per profile: a board is fetched once per run and is either working
or not, whoever watches it. Health alerts go to the one health topic (the person running
the tracker), never to a profile's job topic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobtracker.config import Settings
from jobtracker.models import EMPTY, OK, SourceResult
from jobtracker.notify import Notification
from jobtracker.state import SourceState

MAX_ERROR_LENGTH = 160


@dataclass
class HealthReport:
    failing: list[SourceResult] = field(default_factory=list)  # due an alert this run
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
    """Update one source's failure streak and add it to the report if an alert is due."""
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
    # Capped so that a long outage does not change the state file on every run.
    source.consecutive_failures = min(
        source.consecutive_failures + 1, settings.fail_after_consecutive
    )
    if source.consecutive_failures < settings.fail_after_consecutive:
        return

    reminder_due = source.last_alert_at is None or now - datetime.fromisoformat(
        source.last_alert_at
    ) >= timedelta(hours=settings.reminder_hours)
    if not source.alerted or reminder_due:
        report.failing.append(result)


def mark_alerted(sources: dict[str, SourceState], report: HealthReport, now: datetime) -> None:
    """Call only after the alert was delivered, so a failed send is retried next run."""
    for result in report.failing:
        source = sources[result.company.source_key]
        source.alerted = True
        source.last_alert_at = now.isoformat(timespec="seconds")


def health_notification(report: HealthReport, sources: dict[str, SourceState]) -> Notification:
    lines = []
    for result in report.failing:
        since = sources[result.company.source_key].failing_since or "unknown"
        error = (result.error or "unknown error")[:MAX_ERROR_LENGTH]
        lines.append(f"{result.company.name} ({result.company.ats}): {result.status}, {error}")
        lines.append(f"  failing since {since}")
    for result in report.recovered:
        lines.append(f"Recovered: {result.company.name} ({result.company.ats})")

    if report.failing:
        count = len(report.failing)
        title = (
            f"Source failing: {report.failing[0].company.name}"
            if count == 1
            else f"{count} sources failing"
        )
        return Notification(title=title, message="\n".join(lines), priority=4, tags=("warning",))
    return Notification(
        title="Sources recovered", message="\n".join(lines), priority=2, tags=("white_check_mark",)
    )
