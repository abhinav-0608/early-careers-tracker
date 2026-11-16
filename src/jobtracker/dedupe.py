"""Work out what changed since the last run: new jobs, reposts, and closures.

Two keys identify a job:

  primary key   source + company + the source's job id. Exact, but it changes when a
                company takes a posting down and reposts it.
  fingerprint   company + normalised title + normalised locations. Survives a repost.

A posting whose primary key is unknown but whose fingerprint matches a recent record is
a repost: no alert. "Recent" means the record is still open, or closed within
`repost_window_days`. An older match is a new job, because programmes such as spring
weeks reuse the same title every year and each year's opening deserves an alert.

All of this is per profile. A Reconciler is given one profile's state and only ever sees
the jobs that matched that profile's filters, so what one person has been told about has
no bearing on what another is told.
"""

from __future__ import annotations

from datetime import date, datetime

from jobtracker.config import Settings
from jobtracker.filters import Decision
from jobtracker.models import Job
from jobtracker.state import CLOSED, OPEN, ProfileState, StoredJob
from jobtracker.text import normalise

# What observe() concluded about a posting.
KNOWN = "known"  # already tracked under this key
REPOST = "repost"  # new key, same job as a recent record: no alert
NEW = "new"  # alert
BASELINED = "baselined"  # new to us, but recorded silently as part of a baseline


def fingerprint(job: Job) -> str:
    """Fallback identity. Digits are kept, so "Spring Week 2027" != "Spring Week 2028"."""
    locations = sorted(normalise(location) for location in job.locations)
    return "|".join([job.company, normalise(job.title), "; ".join(locations)])


class Reconciler:
    """Compares one run's matching jobs with one profile's state, updating it in place.

    Usage: call observe() for every matching job from every successful fetch, then
    close_missing() once to close whatever was not observed.
    """

    def __init__(self, state: ProfileState, now: datetime, settings: Settings):
        self._jobs = state.jobs
        self._now = now
        self._today = now.date()
        self._settings = settings
        self._seen: set[str] = set()  # record keys observed during this run
        # Lookup tables from an alias or fingerprint to the record keys that carry it.
        self._key_by_alias: dict[str, str] = {}
        self._keys_by_fingerprint: dict[str, list[str]] = {}
        for record in self._jobs.values():
            self._index(record)

    def observe(self, job: Job, decision: Decision, baseline: bool, eligibility: str = "") -> str:
        """Record one matching job seen in this run and return what it turned out to be.

        `eligibility` is why a first-year student may apply ("" if that is not checked).
        """
        record = self._recent(self._jobs.get(job.key))
        if record is not None:
            self._refresh(record, job, decision, eligibility)
            return KNOWN

        record = self._recent(self._jobs.get(self._key_by_alias.get(job.key, "")))
        if record is not None:
            # An older posting of a job we now track under a newer id. It proves the job
            # is still open, but the newer posting stays the source of truth.
            self._mark_seen(record)
            return KNOWN

        record = self._find_by_fingerprint(fingerprint(job))
        if record is not None:
            self._adopt_repost(record, job, decision, eligibility)
            return REPOST

        self._add(job, decision, notified=baseline, eligibility=eligibility)
        return BASELINED if baseline else NEW

    def close_missing(
        self, fetched_sources: set[str], configured_sources: set[str]
    ) -> list[StoredJob]:
        """Close open jobs that were not observed. Returns the jobs closed.

        A job is only counted as missing when its board was fetched successfully this
        run, so a failed or suspiciously empty source never closes anything. Jobs
        belonging to a company that is no longer configured (removed from the company
        list, or from this profile's scope) are closed straight away.
        """
        closed = []
        for record in self._jobs.values():
            if record.status != OPEN or record.key in self._seen:
                continue
            if record.source_key not in configured_sources:
                self._close(record)
                closed.append(record)
            elif record.source_key in fetched_sources:
                record.missing_runs += 1
                if record.missing_runs >= self._settings.close_after_missing_runs:
                    self._close(record)
                    closed.append(record)
        return closed

    def prune(self) -> int:
        """Delete jobs closed longer ago than `prune_closed_after_days`."""
        expired = [
            key
            for key, record in self._jobs.items()
            if record.status == CLOSED
            and self._days_closed(record) > self._settings.prune_closed_after_days
        ]
        for key in expired:
            del self._jobs[key]
        return len(expired)

    # --- lookups -----------------------------------------------------------------------

    def _recent(self, record: StoredJob | None) -> StoredJob | None:
        """The record, if it is open or closed within the repost window; else None."""
        if record is None:
            return None
        if record.status == OPEN:
            return record
        if self._days_closed(record) <= self._settings.repost_window_days:
            return record
        return None

    def _find_by_fingerprint(self, wanted: str) -> StoredJob | None:
        for key in self._keys_by_fingerprint.get(wanted, []):
            record = self._recent(self._jobs.get(key))
            if record is not None and record.fingerprint == wanted:
                return record
        return None

    def _days_closed(self, record: StoredJob) -> int:
        if record.closed_at is None:
            return 0
        return (self._today - date.fromisoformat(record.closed_at)).days

    def _index(self, record: StoredJob) -> None:
        for alias in record.aliases:
            self._key_by_alias[alias] = record.key
        self._keys_by_fingerprint.setdefault(record.fingerprint, []).append(record.key)

    # --- updates -----------------------------------------------------------------------

    def _mark_seen(self, record: StoredJob) -> None:
        self._seen.add(record.key)
        record.last_seen = self._today.isoformat()
        record.missing_runs = 0
        if record.status == CLOSED:  # back within the repost window: reopen quietly
            record.status = OPEN
            record.closed_at = None

    def _refresh(
        self, record: StoredJob, job: Job, decision: Decision, eligibility: str = ""
    ) -> None:
        """Bring a known record up to date with the posting as it is now."""
        self._mark_seen(record)
        record.eligibility = eligibility
        record.title = job.title
        record.locations = list(job.locations)
        record.url = job.url
        record.role_type = decision.role_type or record.role_type
        record.fields = list(decision.fields)
        record.region = decision.region or record.region
        if job.posted_at:
            record.posted_at = job.posted_at.isoformat()
        new_fingerprint = fingerprint(job)
        if new_fingerprint != record.fingerprint:  # the listing was edited
            record.fingerprint = new_fingerprint
            self._keys_by_fingerprint.setdefault(new_fingerprint, []).append(record.key)

    def _adopt_repost(
        self, record: StoredJob, job: Job, decision: Decision, eligibility: str = ""
    ) -> None:
        """Move a record to the id of its newer posting, remembering the old id."""
        del self._jobs[record.key]
        record.aliases.append(record.key)
        record.key = job.key
        record.source = job.source
        self._jobs[record.key] = record
        self._index(record)
        self._refresh(record, job, decision, eligibility)

    def _add(self, job: Job, decision: Decision, notified: bool, eligibility: str = "") -> None:
        record = StoredJob(
            key=job.key,
            source=job.source,
            company=job.company,
            title=job.title,
            locations=list(job.locations),
            url=job.url,
            role_type=decision.role_type or "",
            fields=list(decision.fields),
            region=decision.region or "",
            fingerprint=fingerprint(job),
            first_seen=self._now.isoformat(timespec="seconds"),
            last_seen=self._today.isoformat(),
            posted_at=job.posted_at.isoformat() if job.posted_at else None,
            notified=notified,
            eligibility=eligibility,
        )
        self._jobs[record.key] = record  # replaces any stale record with the same key
        self._index(record)
        self._seen.add(record.key)

    def _close(self, record: StoredJob) -> None:
        record.status = CLOSED
        record.closed_at = self._today.isoformat()
        record.missing_runs = 0
