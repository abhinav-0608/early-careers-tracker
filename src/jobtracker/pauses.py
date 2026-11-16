"""Kinds of source taken off polling because their servers refuse the tracker.

Some refusals are not about how often the tracker asks: a Cloudflare challenge on every
Workday server is the owner (or their host) turning automated clients away. The rule
(DESIGN.md decision 12) is never to work around that: no other hosts, no proxies, no
headless browser. What is left is to stop asking, and to check now and then, gently,
whether the refusal has been lifted. A pause in settings.yaml does that, in four stages:

    paused     for probe_after_hours after `since`: nothing is asked at all
    probing    then one request to probe_company every probe_every_hours, with its
               cached robots.txt; still nothing else is asked
    resumed    the first probe that succeeds: polling restarts, at most every
               resume_every_minutes, with one page per search
    given up   no probe has succeeded give_up_after_days after `since`: probing stops,
               and a health alert asks for the kind to be marked unsupported

While paused, probing or given up, every source of the kind reports PAUSED, which (like
NOT_DUE) is neither a failure nor a fetch: no health record changes and nothing closes.
Progress is kept in sources.json (`pauses`), and each change of stage is announced
once on the health topic. Deleting the entry from settings.yaml resumes normal polling.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from jobtracker import log
from jobtracker.config import Pause
from jobtracker.http import HttpClient, RateLimitedError
from jobtracker.models import Company
from jobtracker.notify import Notification
from jobtracker.sources import workday

PAUSED = "paused"
PROBE = "probe"
RESUMED = "resumed"
GIVEN_UP = "given_up"

# An interval counts as elapsed this long early, as for polling intervals, so a run
# that starts a minute ahead of the hour does not push the probe back by a whole run.
TOLERANCE = timedelta(minutes=2)

# One request that shows whether the kind of source answers again.
PROBES: dict[str, Callable[[Company, HttpClient], None]] = {workday.NAME: workday.probe}


def stage(pause: Pause, memory: dict, now: datetime) -> str:
    """Where a paused kind of source is in this run: PAUSED, PROBE, RESUMED or GIVEN_UP."""
    if memory.get("resumed_at"):
        return RESUMED
    if memory.get("gave_up_at"):
        return GIVEN_UP
    if now >= pause.since + timedelta(days=pause.give_up_after_days):
        memory["gave_up_at"] = now.isoformat(timespec="seconds")
        return GIVEN_UP
    if now < pause.since + timedelta(hours=pause.probe_after_hours) - TOLERANCE:
        return PAUSED
    probed = memory.get("probed_at")
    if probed and now - datetime.fromisoformat(probed) < (
        timedelta(hours=pause.probe_every_hours) - TOLERANCE
    ):
        return PAUSED
    return PROBE


def probe(pause: Pause, company: Company, http: HttpClient, memory: dict, now: datetime) -> str:
    """Make the one probe request and record it. Returns RESUMED or PAUSED.

    A host still inside a back-off from an earlier 429 is not asked: the probe then
    waits for the next run, and nothing is recorded.
    """
    try:
        PROBES[pause.kind](company, http)
    except RateLimitedError as exc:
        if not exc.answered_now:
            log.event("pause_probe", kind=pause.kind, company=company.key, outcome="backing_off")
            return PAUSED
        outcome = str(exc)
    except Exception as exc:  # noqa: BLE001 - a failed probe is an answer, not a crash
        outcome = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    else:
        outcome = "ok"
    memory["probed_at"] = now.isoformat(timespec="seconds")
    memory["probe"] = outcome[:200]
    log.event("pause_probe", kind=pause.kind, company=company.key, outcome=memory["probe"])
    if outcome != "ok":
        return PAUSED
    memory["resumed_at"] = memory["probed_at"]
    return RESUMED


def announcement(pause: Pause, current: str, memory: dict) -> Notification | None:
    """The health alert for a change of stage not yet announced, or None.

    The caller records memory["announced"] = current once it has been delivered, so a
    failed send is tried again on the next run.
    """
    if current not in (RESUMED, GIVEN_UP) or memory.get("announced") == current:
        return None
    name = pause.kind.title()
    if current == RESUMED:
        hours = pause.resume_every_minutes / 60
        return Notification(
            title=f"{name} answers again",
            message=(
                f"A probe of {pause.probe_company} succeeded at {memory['resumed_at']}. "
                f"{name} is polled again, at most every {hours:g} h, with small searches."
            ),
            priority=3,
            tags=("white_check_mark",),
        )
    return Notification(
        title=f"{name} still refused after {pause.give_up_after_days} days",
        message=(
            f"Paused since {pause.since:%Y-%m-%d %H:%M} UTC ({pause.reason}). No probe has "
            f"succeeded, so probing has stopped. Mark the {name} companies unsupported in "
            "companies.yaml and remove the pause from settings.yaml."
        )[:1000],
        priority=4,
        tags=("warning",),
    )
