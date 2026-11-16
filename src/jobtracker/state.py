"""The tracker's memory: JSON files committed back to the repo.

    state/sources.json          per-board health, shared by every profile
    state/hosts.json            per-host memory of the HTTP client: each host's robots.txt
                                rules (kept for a day) and any back-off a host asked for
    state/profiles/<key>.json   one profile's memory: the matching postings it has seen
                                (open or recently closed, each with its notified flag)
                                and which boards it has been baselined against

Dedupe and notified-state are per profile, each in its own file, so one profile's run
cannot read or write another's. Whether a board is up is a fact about the board, so
health is tracked once.

Files are written sorted and indented so that git diffs are small and readable. Values
that would otherwise change on every run (last_seen, counters) are stored coarsely, so
an uneventful run leaves the files byte-identical and nothing is committed.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jobtracker import privacy

VERSION = 2

OPEN = "open"
CLOSED = "closed"


@dataclass
class StoredJob:
    key: str  # "source:company:job_id" of the most recent posting of this job
    source: str
    company: str
    title: str
    locations: list[str]
    url: str
    role_type: str
    fields: list[str]
    region: str
    fingerprint: str  # fallback dedupe key: company | title | locations
    first_seen: str  # ISO timestamp, UTC
    last_seen: str  # ISO date; date-only so it changes at most once a day
    posted_at: str | None = None  # ISO date as published by the source
    aliases: list[str] = field(default_factory=list)  # earlier keys of the same job
    missing_runs: int = 0  # successful fetches in a row that did not include it
    status: str = OPEN
    closed_at: str | None = None  # ISO date
    notified: bool = False
    # Why a first-year student may apply, e.g. 'title: "First Year"'. "" when the
    # profile does not check eligibility for this role type.
    eligibility: str = ""
    # "All years welcome · prefers penultimate/final" when the posting says which years it
    # would rather have (eligibility.py). Not written to the file when "".
    eligibility_note: str = ""

    @property
    def source_key(self) -> str:
        return f"{self.source}:{self.company}"


@dataclass
class SourceState:
    """Health of one board. Shared: a board is fetched once, whoever watches it."""

    has_returned_jobs: bool = False  # lets us spot a board that suddenly returns nothing
    empty_runs: int = 0  # runs in a row it has unexpectedly been empty
    consecutive_failures: int = 0  # capped at the alert threshold
    failing_since: str | None = None  # ISO timestamp of the first failure in this streak
    alerted: bool = False  # a health alert has been sent for the current streak
    last_alert_at: str | None = None  # ISO timestamp
    # When the part of this source that has a polling interval last ran (ISO timestamp,
    # to the minute). Only ever set for sources with an interval, and left out of the
    # file otherwise, so every-run sources add nothing to the state file.
    interval_polled_at: str | None = None


@dataclass
class ProfileState:
    """Everything one profile remembers between runs."""

    jobs: dict[str, StoredJob] = field(default_factory=dict)
    # Board (source key) -> hash of this profile's filter rules when the board was last
    # baselined for it. A board is (re)baselined whenever that differs from the current
    # rules: the first time the profile sees the board, and after any change to the
    # profile's filters.yaml.
    baselines: dict[str, str] = field(default_factory=dict)
    # Job key -> what reading its description settled: {"status": "eligible", "basis":
    # ...}, {"status": "unclear"}, {"status": "ineligible"}, or {"status": "unknown",
    # "tries": n} while the description cannot be read. Kept so that a description is
    # read once per posting, not once per run; cleared when the eligibility rules change.
    eligibility: dict[str, dict] = field(default_factory=dict)
    eligibility_rules: str = ""  # hash of the rules the verdicts above were made with


def profile_path(state_dir: Path | str, profile_key: str) -> Path:
    return Path(state_dir) / "profiles" / f"{profile_key}.json"


# --- shared: board health --------------------------------------------------------------


def load_sources(state_dir: Path | str) -> dict[str, SourceState]:
    data = _read(Path(state_dir) / "sources.json").get("sources", {})
    sources = {}
    for key, value in data.items():
        sources[key] = SourceState(**value)
    return sources


@dataclass
class SharedState:
    """Run-level memory kept beside board health in sources.json.

    pauses:        kind of source -> {"probed_at", "probe", "resumed_at", "gave_up_at"},
                   the progress of a paused kind of source (pauses.py)
    alert_groups:  "Platform: cause" -> ISO time of that group's last health alert, so a
                   group is announced at most once a day (health.py)
    """

    pauses: dict[str, dict] = field(default_factory=dict)
    alert_groups: dict[str, str] = field(default_factory=dict)


def load_shared(state_dir: Path | str) -> SharedState:
    data = _read(Path(state_dir) / "sources.json")
    return SharedState(
        pauses=dict(data.get("pauses", {})), alert_groups=dict(data.get("alert_groups", {}))
    )


def save_sources(
    sources: dict[str, SourceState], state_dir: Path | str, shared: SharedState | None = None
) -> None:
    data = {}
    for key, source in sources.items():
        data[key] = asdict(source)
        if source.interval_polled_at is None:
            del data[key]["interval_polled_at"]
    document: dict = {"version": VERSION, "sources": data}
    # Written only when there is something in them, so they add nothing to the file
    # (and no commits) while nothing is paused and nothing has alerted.
    if shared and shared.pauses:
        document["pauses"] = shared.pauses
    if shared and shared.alert_groups:
        document["alert_groups"] = shared.alert_groups
    _write(Path(state_dir) / "sources.json", document)


# --- hosts -----------------------------------------------------------------------------

HOSTS_VERSION = 1


@dataclass
class HostsState:
    """What the HTTP client remembers about hosts between runs.

    robots:  hostname -> {"fetched_at": ISO time, "rules": ["Disallow: /x", ...],
             "crawl_delay": seconds or null}. The rules this tracker was given, so a
             host's robots.txt is fetched once a day instead of on every run.
    backoff: host -> {"retry_at": ISO time, "strikes": n}. A host that answered 429 is
             left alone until retry_at; strikes counts the 429s in a row.
    """

    robots: dict[str, dict] = field(default_factory=dict)
    backoff: dict[str, dict] = field(default_factory=dict)


def load_hosts(state_dir: Path | str) -> HostsState:
    """Load the client's host memory. A missing or unreadable file means an empty one:
    it is only a cache, and losing it costs one robots.txt request per host."""
    try:
        data = _read(Path(state_dir) / "hosts.json")
        return HostsState(
            robots=dict(data.get("robots", {})), backoff=dict(data.get("backoff", {}))
        )
    except (ValueError, AttributeError, TypeError):
        return HostsState()


def save_hosts(hosts: HostsState, state_dir: Path | str) -> None:
    data = {"version": HOSTS_VERSION, "robots": hosts.robots, "backoff": hosts.backoff}
    _write(Path(state_dir) / "hosts.json", data)


# --- per profile -----------------------------------------------------------------------


def load_profile_state(state_dir: Path | str, profile_key: str) -> ProfileState:
    """Load one profile's state. A profile with no file yet starts empty."""
    path = profile_path(state_dir, profile_key)
    if not path.exists():
        return ProfileState()
    data = _read(path)
    return ProfileState(
        jobs={key: StoredJob(key=key, **value) for key, value in data.get("jobs", {}).items()},
        baselines=dict(data.get("baselines", {})),
        eligibility=dict(data.get("eligibility", {})),
        eligibility_rules=data.get("eligibility_rules", ""),
    )


def save_profile_state(
    state: ProfileState,
    state_dir: Path | str,
    profile_key: str,
    private: Collection[str] = (),
) -> None:
    """Write one profile's state. Roles of the `private` companies (keys) are written
    without title, link, location or quoted text: the file is committed to a public
    repository (privacy.py). In memory they stay whole for the rest of the run."""
    jobs = {}
    for key, job in state.jobs.items():
        record = asdict(job)
        del record["key"]  # already the dictionary key
        if job.company in private:
            record.update(title="", url="", locations=[])
            if record["eligibility"]:
                record["eligibility"] = privacy.redact_basis(record["eligibility"])
        if not record["eligibility_note"]:
            del record["eligibility_note"]  # rare: keep the file small and stable
        jobs[key] = record
    data = {"version": VERSION, "jobs": jobs, "baselines": state.baselines}
    if state.eligibility_rules:
        data["eligibility"] = {
            key: _redact_verdict(verdict) if _company_of(key) in private else verdict
            for key, verdict in state.eligibility.items()
        }
        data["eligibility_rules"] = state.eligibility_rules
    _write(profile_path(state_dir, profile_key), data)


def _company_of(job_key: str) -> str:
    """ "source:company:job_id" -> company."""
    parts = job_key.split(":")
    return parts[1] if len(parts) > 2 else ""


def _redact_verdict(verdict: dict) -> dict:
    redacted = {key: value for key, value in verdict.items() if key != "why"}
    if redacted.get("basis"):
        redacted["basis"] = privacy.redact_basis(redacted["basis"])
    return redacted


# --- files -----------------------------------------------------------------------------


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
