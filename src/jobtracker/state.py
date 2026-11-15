"""The tracker's memory: two JSON files committed back to the repo.

    state/jobs.json     every matching posting seen, open or recently closed
    state/sources.json  per-board health and baseline bookkeeping

Both are written sorted and indented so that git diffs are small and readable. Values
that would otherwise change on every run (last_seen, counters) are stored coarsely, so
an uneventful run leaves the files byte-identical and nothing is committed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

VERSION = 1

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

    @property
    def source_key(self) -> str:
        return f"{self.source}:{self.company}"


@dataclass
class SourceState:
    # The filter rules this board was last baselined against. A board is (re)baselined
    # whenever this differs from the current rules: on its first successful fetch, and
    # after any change to filters.yaml.
    filters_hash: str | None = None
    has_returned_jobs: bool = False  # lets us spot a board that suddenly returns nothing
    empty_runs: int = 0  # runs in a row it has unexpectedly been empty
    consecutive_failures: int = 0  # capped at the alert threshold
    failing_since: str | None = None  # ISO timestamp of the first failure in this streak
    alerted: bool = False  # a health alert has been sent for the current streak
    last_alert_at: str | None = None  # ISO timestamp


@dataclass
class State:
    jobs: dict[str, StoredJob] = field(default_factory=dict)
    sources: dict[str, SourceState] = field(default_factory=dict)


def load_state(state_dir: Path | str) -> State:
    state_dir = Path(state_dir)
    jobs_data = _read(state_dir / "jobs.json").get("jobs", {})
    sources_data = _read(state_dir / "sources.json").get("sources", {})
    return State(
        jobs={key: StoredJob(key=key, **value) for key, value in jobs_data.items()},
        sources={key: SourceState(**value) for key, value in sources_data.items()},
    )


def save_state(state: State, state_dir: Path | str) -> None:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    jobs = {}
    for key, job in state.jobs.items():
        record = asdict(job)
        del record["key"]  # already the dictionary key
        jobs[key] = record
    sources = {key: asdict(source) for key, source in state.sources.items()}
    _write(state_dir / "jobs.json", {"version": VERSION, "jobs": jobs})
    _write(state_dir / "sources.json", {"version": VERSION, "sources": sources})


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
