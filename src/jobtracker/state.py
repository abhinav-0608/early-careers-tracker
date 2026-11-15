"""The tracker's memory: JSON files committed back to the repo.

    state/sources.json          per-board health, shared by every profile
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
from dataclasses import asdict, dataclass, field
from pathlib import Path

VERSION = 2

OPEN = "open"
CLOSED = "closed"

# Before profiles existed (version 1) there was a single state/jobs.json, and the
# baseline hashes sat inside state/sources.json. That state belonged to this profile.
# Only used by the migration below; delete both once state/jobs.json is gone from main.
LEGACY_OWNER = "abhinav"


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


@dataclass
class ProfileState:
    """Everything one profile remembers between runs."""

    jobs: dict[str, StoredJob] = field(default_factory=dict)
    # Board (source key) -> hash of this profile's filter rules when the board was last
    # baselined for it. A board is (re)baselined whenever that differs from the current
    # rules: the first time the profile sees the board, and after any change to the
    # profile's filters.yaml.
    baselines: dict[str, str] = field(default_factory=dict)


def profile_path(state_dir: Path | str, profile_key: str) -> Path:
    return Path(state_dir) / "profiles" / f"{profile_key}.json"


# --- shared: board health --------------------------------------------------------------


def load_sources(state_dir: Path | str) -> dict[str, SourceState]:
    data = _read(Path(state_dir) / "sources.json").get("sources", {})
    sources = {}
    for key, value in data.items():
        value = {name: item for name, item in value.items() if name != "filters_hash"}  # v1
        sources[key] = SourceState(**value)
    return sources


def save_sources(sources: dict[str, SourceState], state_dir: Path | str) -> None:
    data = {key: asdict(source) for key, source in sources.items()}
    _write(Path(state_dir) / "sources.json", {"version": VERSION, "sources": data})


# --- per profile -----------------------------------------------------------------------


def load_profile_state(state_dir: Path | str, profile_key: str) -> ProfileState:
    """Load one profile's state. A profile with no file yet starts empty.

    The exception is the owner of the pre-profile state files, who inherits them until
    migrate_legacy() has turned them into a profile file. Reading them here as well
    means a dry run before the migration shows the truth, not a pretend first run.
    """
    path = profile_path(state_dir, profile_key)
    if not path.exists():
        if profile_key == LEGACY_OWNER:
            return _load_legacy(Path(state_dir))
        return ProfileState()
    data = _read(path)
    return ProfileState(
        jobs={key: StoredJob(key=key, **value) for key, value in data.get("jobs", {}).items()},
        baselines=dict(data.get("baselines", {})),
    )


def save_profile_state(state: ProfileState, state_dir: Path | str, profile_key: str) -> None:
    jobs = {}
    for key, job in state.jobs.items():
        record = asdict(job)
        del record["key"]  # already the dictionary key
        jobs[key] = record
    _write(
        profile_path(state_dir, profile_key),
        {"version": VERSION, "jobs": jobs, "baselines": state.baselines},
    )


# --- migration from the single-user layout ---------------------------------------------


def migrate_legacy(state_dir: Path | str) -> bool:
    """Turn the version 1 files into the owner's profile file. Returns True if it did.

    Nothing is lost or reinterpreted: every job keeps its notified flag and every board
    keeps its baseline hash, so the first run after the migration alerts exactly what a
    run before it would have. Safe to repeat; a run that dies half-way simply migrates
    again next time.
    """
    state_dir = Path(state_dir)
    legacy_jobs = state_dir / "jobs.json"
    if not legacy_jobs.exists():
        return False
    if not profile_path(state_dir, LEGACY_OWNER).exists():
        save_profile_state(_load_legacy(state_dir), state_dir, LEGACY_OWNER)
    save_sources(load_sources(state_dir), state_dir)  # rewrites it without the hashes
    legacy_jobs.unlink()
    return True


def _load_legacy(state_dir: Path) -> ProfileState:
    jobs = _read(state_dir / "jobs.json").get("jobs", {})
    sources = _read(state_dir / "sources.json").get("sources", {})
    hashes = {key: value.get("filters_hash") for key, value in sources.items()}
    return ProfileState(
        jobs={key: StoredJob(key=key, **value) for key, value in jobs.items()},
        baselines={key: value for key, value in hashes.items() if value},
    )


# --- files -----------------------------------------------------------------------------


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
