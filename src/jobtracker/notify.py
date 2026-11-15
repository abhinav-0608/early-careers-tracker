"""Build and send ntfy notifications.

Messages are published as JSON to the server root with the topic in the request body
(https://docs.ntfy.sh/publish/#publish-as-json). The topics are the only secrets in this
project, and keeping them out of the URL means they cannot leak through an error message
or a request log.

Every profile has its own topic. A job notification is always sent on behalf of one
profile and can only go to that profile's topic; health alerts go to the health topic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import httpx

from jobtracker import log
from jobtracker.config import Config, Profile
from jobtracker.filters import UNCLEAR
from jobtracker.http import HttpClient
from jobtracker.state import StoredJob

MAX_SUMMARY_LINES = 10
MAX_BASELINE_LINES = 25


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    priority: int = 3
    tags: tuple[str, ...] = ()
    click: str | None = None
    job_keys: tuple[str, ...] = ()  # the jobs this notification covers


class Notifier:
    """Sends notifications to a profile's job topic or to the health topic.

    In dry-run mode nothing is sent; each notification is logged instead.
    """

    def __init__(
        self,
        http: HttpClient,
        server: str,
        job_topics: dict[str, str | None],  # profile key -> that profile's topic
        health_topic: str | None,
        dry_run: bool = False,
    ):
        self._http = http
        self._server = server
        self._job_topics = dict(job_topics)
        self._health_topic = health_topic
        self._dry_run = dry_run

    def can_reach(self, profile_key: str) -> bool:
        """Is there a topic to send this profile's alerts to?"""
        return self._dry_run or bool(self._job_topics.get(profile_key))

    def send_job(self, profile_key: str, notification: Notification) -> bool:
        topic = self._job_topics.get(profile_key)
        return self._send(f"jobs:{profile_key}", topic, notification)

    def send_health(self, notification: Notification) -> bool:
        return self._send("health", self._health_topic, notification)

    def _send(self, channel: str, topic: str | None, notification: Notification) -> bool:
        """Return True if the notification was delivered (or logged, in a dry run)."""
        if self._dry_run:
            log.event(
                "notification_dry_run",
                channel=channel,
                title=notification.title,
                message=notification.message,
                priority=notification.priority,
                tags=list(notification.tags),
                click=notification.click,
            )
            return True

        if not topic:
            log.warning("notification_failed", channel=channel, error="topic is not configured")
            return False

        payload = {
            "topic": topic,
            "title": notification.title,
            "message": notification.message,
            "priority": notification.priority,
            "tags": list(notification.tags),
        }
        if notification.click:
            payload["click"] = notification.click
        try:
            self._http.post_json(self._server, payload)
        except httpx.HTTPError as exc:
            # Log the kind of failure only. The exception text is never logged, so
            # nothing derived from the request can end up in a public Actions log.
            log.warning("notification_failed", channel=channel, error=_describe(exc))
            return False
        log.event("notification_sent", channel=channel, title=notification.title)
        return True


def _describe(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


# --- building notifications (pure functions) -------------------------------------------


def plan_job_notifications(
    pending: list[StoredJob], profile: Profile, config: Config
) -> list[Notification]:
    """Decide how to announce one profile's jobs that are awaiting an alert.

    Up to `batch_threshold` jobs get one alert each. Beyond that, the profile's
    dream-tier jobs still get their own alert and the rest are rolled into a single
    summary.
    """
    pending = sorted(pending, key=lambda job: (job.company, job.title, job.key))
    if len(pending) <= config.settings.batch_threshold:
        return [job_notification(job, profile, config) for job in pending]

    dream = [job for job in pending if profile.is_dream(job.company)]
    normal = [job for job in pending if not profile.is_dream(job.company)]
    notifications = [job_notification(job, profile, config) for job in dream]
    if len(normal) == 1:
        notifications.append(job_notification(normal[0], profile, config))
    elif normal:
        notifications.append(summary_notification(normal, config))
    return notifications


def job_notification(job: StoredJob, profile: Profile, config: Config) -> Notification:
    role = profile.filters.role_type(job.role_type)
    is_dream = profile.is_dream(job.company)
    settings = config.settings

    if job.posted_at:
        when = f"Posted {_format_date(date.fromisoformat(job.posted_at))}"
    else:
        when = f"First seen {_format_date(datetime.fromisoformat(job.first_seen).date())}"

    return Notification(
        title=f"{_company_name(job, config)} – {job.title}",
        message=f"{_location_text(job)}\n{role.label if role else job.role_type} · {when}",
        priority=settings.priority_dream if is_dream else settings.priority_normal,
        tags=(*(role.ntfy_tags if role else ()), *(("star",) if is_dream else ())),
        click=job.url,
        job_keys=(job.key,),
    )


def summary_notification(jobs: list[StoredJob], config: Config) -> Notification:
    lines = [
        f"{_company_name(job, config)} – {job.title} ({_location_text(job)})"
        for job in jobs[:MAX_SUMMARY_LINES]
    ]
    if len(jobs) > MAX_SUMMARY_LINES:
        lines.append(f"...and {len(jobs) - MAX_SUMMARY_LINES} more")
    return Notification(
        title=f"{len(jobs)} new roles",
        message="\n".join(lines),
        priority=config.settings.priority_normal,
        tags=("bell",),
        click=config.settings.repo_url,
        job_keys=tuple(job.key for job in jobs),
    )


def baseline_notification(
    jobs: list[StoredJob], config: Config, filters_changed: bool
) -> Notification:
    """The single summary sent instead of individual alerts when baselining."""
    counts: dict[str, int] = {}
    for job in jobs:
        name = _company_name(job, config)
        counts[name] = counts.get(name, 0) + 1
    lines = [f"{name}: {count}" for name, count in sorted(counts.items())]
    if len(lines) > MAX_BASELINE_LINES:
        extra = len(lines) - MAX_BASELINE_LINES
        lines = lines[:MAX_BASELINE_LINES] + [f"...and {extra} more companies"]

    noun = "role" if len(jobs) == 1 else "roles"
    if filters_changed:
        title = f"Baseline: {len(jobs)} {noun} newly match your filters"
    else:
        title = f"Baseline: {len(jobs)} matching {noun} currently open"
    return Notification(
        title=title,
        message="\n".join(lines) or "Nothing open right now. New roles will be alerted.",
        priority=config.settings.priority_normal,
        tags=("clipboard",),
        click=config.settings.repo_url,
    )


def _company_name(job: StoredJob, config: Config) -> str:
    company = config.company(job.company)
    return company.name if company else job.company


def _location_text(job: StoredJob) -> str:
    if not job.locations:
        return "Location unclear"
    text = "; ".join(job.locations[:3])
    if len(job.locations) > 3:
        text += f" +{len(job.locations) - 3} more"
    if job.region == UNCLEAR:
        text += " (unclear)"
    return text


def _format_date(value: date) -> str:
    return f"{value.day} {value:%b %Y}"
