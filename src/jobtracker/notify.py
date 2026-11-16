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

from jobtracker import eligibility, log, privacy
from jobtracker.config import Config, Profile
from jobtracker.filters import UNCLEAR
from jobtracker.http import HttpClient
from jobtracker.state import StoredJob

# ntfy accepts a message of up to 4,096 bytes; a longer one is refused or turned into an
# attachment. Lists are cut to fit under this with room to spare, ending "+N more".
MAX_MESSAGE_BYTES = 3800
OPEN_ROLES_ANCHOR = "#open-roles"  # the README table that a summary opens when tapped
FIRE = "🔥"
TICK = "✅"
MAX_ACTIONS = 3  # ntfy shows at most three buttons on a notification
BUTTON_TITLE_CHARS = 32  # a role title is cut to this in a button label


@dataclass(frozen=True)
class Action:
    """An ntfy "view" button: tapping it opens `url`."""

    label: str
    url: str


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    priority: int = 3
    tags: tuple[str, ...] = ()
    click: str | None = None
    job_keys: tuple[str, ...] = ()  # the jobs this notification covers
    private: bool = False  # names a private company's role: its title is not logged
    # Buttons, at most MAX_ACTIONS. Like the click, never logged on a real send: the
    # Actions log is public, and a private company's posting links must not reach it.
    actions: tuple[Action, ...] = ()


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
                actions=[[action.label, action.url] for action in notification.actions],
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
        if notification.actions:
            payload["actions"] = [
                {"action": "view", "label": action.label, "url": action.url}
                for action in notification.actions[:MAX_ACTIONS]
            ]
        try:
            self._http.post_json(self._server, payload, check_robots=False)
        except httpx.HTTPError as exc:
            # Log the kind of failure only. The exception text is never logged, so
            # nothing derived from the request can end up in a public Actions log.
            log.warning("notification_failed", channel=channel, error=_describe(exc))
            return False
        title = privacy.WITHHELD if notification.private else notification.title
        log.event("notification_sent", channel=channel, title=title)
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
    FAANG+ jobs still get their own alert and the rest are rolled into a single
    summary. FAANG+ alerts go first.
    """
    pending = in_order(pending, profile, config)
    if len(pending) <= config.settings.batch_threshold:
        return [job_notification(job, profile, config) for job in pending]

    faang_plus = [job for job in pending if profile.is_faang_plus(job.company)]
    normal = [job for job in pending if not profile.is_faang_plus(job.company)]
    notifications = [job_notification(job, profile, config) for job in faang_plus]
    if len(normal) == 1:
        notifications.append(job_notification(normal[0], profile, config))
    elif normal:
        notifications.append(summary_notification(normal, profile, config))
    return notifications


def in_order(jobs: list[StoredJob], profile: Profile, config: Config) -> list[StoredJob]:
    """FAANG+ roles first, then the rest, each by company name and title."""
    return sorted(
        jobs,
        key=lambda job: (
            not profile.is_faang_plus(job.company),
            _company_name(job, config).lower(),
            job.title.lower(),
            job.key,
        ),
    )


def job_notification(job: StoredJob, profile: Profile, config: Config) -> Notification:
    role = profile.filters.role_type(job.role_type)
    is_faang_plus = profile.is_faang_plus(job.company)
    settings = config.settings

    if job.posted_at:
        when = f"Posted {_format_date(date.fromisoformat(job.posted_at))}"
    else:
        when = f"First seen {_format_date(datetime.fromisoformat(job.first_seen).date())}"

    lines = [_location_text(job), f"{role.label if role else job.role_type} · {when}"]
    if job.eligibility:
        lines.append(eligibility.label(job.eligibility, job.eligibility_note))
    return Notification(
        title=f"{FIRE + ' ' if is_faang_plus else ''}{_company_name(job, config)} – {job.title}",
        message="\n".join(lines),
        priority=settings.priority_faang_plus if is_faang_plus else settings.priority_normal,
        tags=(*(role.ntfy_tags if role else ()), *(("fire",) if is_faang_plus else ())),
        click=job.url,
        job_keys=(job.key,),
        private=_is_private(job, config),
    )


def role_line(job: StoredJob, profile: Profile, config: Config) -> str:
    """One role on one line: "🔥 Morgan Stanley — Spring Insight Programme · London · ✅"."""
    parts = [f"{_company_name(job, config)} — {job.title}", _short_location(job)]
    if job.eligibility:
        parts.append(eligibility.mark(job.eligibility_note))
    return (f"{FIRE} " if profile.is_faang_plus(job.company) else "") + " · ".join(parts)


def fit_lines(lines: list[str], limit: int = MAX_MESSAGE_BYTES) -> str:
    """As many whole lines as fit in `limit` bytes, then "+N more" for the rest."""
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        remaining = len(lines) - index
        size = len(line.encode()) + 1
        # Leave room for the "+N more" line unless this is the last line and it fits.
        reserve = 0 if remaining == 1 else len(f"+{remaining} more".encode()) + 1
        if used + size + reserve > limit:
            kept.append(f"+{remaining} more")
            break
        kept.append(line)
        used += size
    return "\n".join(kept)


def summary_notification(
    jobs: list[StoredJob], profile: Profile, config: Config
) -> Notification | None:
    """One notification listing several new roles. None if there are no roles to list."""
    if not jobs:
        return None
    jobs = in_order(jobs, profile, config)
    click, actions = summary_links(jobs, profile, config)
    return Notification(
        title=f"{len(jobs)} new {'role' if len(jobs) == 1 else 'roles'}",
        message=fit_lines([role_line(job, profile, config) for job in jobs]),
        priority=config.settings.priority_normal,
        tags=("bell",),
        click=click,
        job_keys=tuple(job.key for job in jobs),
        actions=actions,
    )


def baseline_notification(
    jobs: list[StoredJob], profile: Profile, config: Config, filters_changed: bool
) -> Notification | None:
    """The single summary sent instead of individual alerts when baselining.

    Lists every role, one line each. None if there are no roles: a summary of nothing
    is never sent.
    """
    if not jobs:
        return None
    jobs = in_order(jobs, profile, config)
    noun = "role" if len(jobs) == 1 else "roles"
    if filters_changed:
        title = f"Baseline: {len(jobs)} {noun} newly match your filters"
    else:
        title = f"Baseline: {len(jobs)} matching {noun} currently open"
    click, actions = summary_links(jobs, profile, config)
    return Notification(
        title=title,
        message=fit_lines([role_line(job, profile, config) for job in jobs]),
        priority=config.settings.priority_normal,
        tags=("clipboard",),
        click=click,
        actions=actions,
    )


def summary_links(
    jobs: list[StoredJob], profile: Profile, config: Config
) -> tuple[str, tuple[Action, ...]]:
    """Where a summary's tap goes, and its buttons. `jobs` are already in_order.

    Up to MAX_ACTIONS roles: a button for each, and the tap opens the first. More: the
    tap opens the README's table, and only FAANG+ roles (first in the order) get
    buttons, up to MAX_ACTIONS. ntfy is private to the profile, so a private company's
    roles get buttons too (privacy.py); the buttons are never logged.
    """
    linked = [job for job in jobs if job.url]  # a private record not refreshed has none
    if len(jobs) > MAX_ACTIONS:
        linked = [job for job in linked if profile.is_faang_plus(job.company)]
    actions = tuple(
        Action(label=_apply_label(job, config), url=job.url) for job in linked[:MAX_ACTIONS]
    )
    readme = config.settings.repo_url + OPEN_ROLES_ANCHOR
    if len(jobs) <= MAX_ACTIONS and actions:
        return actions[0].url, actions
    return readme, actions


def _apply_label(job: StoredJob, config: Config) -> str:
    """ "Apply: Morgan Stanley – 2027 Technology Spring…": short enough for a button."""
    title = job.title
    if len(title) > BUTTON_TITLE_CHARS:
        cut = title[: BUTTON_TITLE_CHARS - 1]
        if title[BUTTON_TITLE_CHARS - 1] != " ":  # mid-word: back to the last whole word
            cut = cut.rsplit(" ", 1)[0]
        cut = cut.rstrip(" -–—|,:(")
        title = (cut or title[: BUTTON_TITLE_CHARS - 1]) + "…"
    return f"Apply: {_company_name(job, config)} – {title}"


def health_test_notification(now: datetime) -> Notification:
    """What `python -m jobtracker test-alert` sends to the health topic."""
    return Notification(
        title="Test health alert",
        message=(
            f"Sent by hand at {now:%Y-%m-%d %H:%M} UTC. If you can read this, health "
            "alerts are reaching this device. Nothing is wrong."
        ),
        priority=4,
        tags=("test_tube",),
    )


def _is_private(job: StoredJob, config: Config) -> bool:
    company = config.company(job.company)
    return bool(company and company.private)


def _company_name(job: StoredJob, config: Config) -> str:
    company = config.company(job.company)
    return company.name if company else job.company


def _short_location(job: StoredJob) -> str:
    """The first location, for a one-line listing."""
    if not job.locations:
        return "Location unclear"
    extra = f" +{len(job.locations) - 1}" if len(job.locations) > 1 else ""
    return job.locations[0] + extra


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
