"""Shared test helpers. Nothing in the test suite touches the network."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobtracker.config import Config, Filters, Profile, load_config
from jobtracker.filters import Decision
from jobtracker.models import Company, Job

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

# The profile that ships in config/profiles. Its real filters are what the tests exercise.
OWNER = "abhinav"

# A decision to hand to the reconciler when a test is not about filtering.
MATCHED = Decision(True, "test", role_type="spring_week", fields=("software",), region="uk")


def load_fixture(relative_path: str):
    return json.loads((FIXTURES / relative_path).read_text(encoding="utf-8"))


def make_company(key: str = "acme", ats: str = "greenhouse", **overrides) -> Company:
    values = {
        "key": key,
        "name": key.title(),
        "industry": "quant",
        "ats": ats,
        "board": key,
    }
    return Company(**{**values, **overrides})


def make_job(job_id: str = "1", title: str = "Spring Week 2027", **overrides) -> Job:
    values = {
        "source": "greenhouse",
        "company": "acme",
        "job_id": job_id,
        "title": title,
        "url": f"https://example.com/jobs/{job_id}",
        "locations": ("London, UK",),
    }
    return Job(**{**values, **overrides})


def make_profile(
    base: Profile,
    key: str,
    industries: tuple[str, ...] = (),
    companies: tuple[str, ...] = (),
    dream: tuple[str, ...] = (),
    filters: Filters | None = None,
) -> Profile:
    """A profile for a test, borrowing the shipped filters unless given its own."""
    return Profile(
        key=key,
        name=key.title(),
        filters=filters or base.filters,
        industries=frozenset(industries),
        companies=frozenset(companies),
        dream=frozenset(dream),
    )


def enable_all_role_types(filters: Filters) -> Filters:
    return replace(filters, role_types=tuple(replace(r, enabled=True) for r in filters.role_types))


def changed_filters(filters: Filters, hash: str = "changed") -> Filters:
    """What editing filters.yaml to switch every role type on produces: new rules, new hash."""
    return replace(enable_all_role_types(filters), hash=hash)


@pytest.fixture(scope="session")
def config() -> Config:
    """The real config files, so the tests exercise the rules that actually ship."""
    return load_config(ROOT / "config")


@pytest.fixture(scope="session")
def profile(config: Config) -> Profile:
    return config.profile(OWNER)


@pytest.fixture(scope="session")
def filters(profile: Profile) -> Filters:
    return profile.filters


@pytest.fixture(scope="session")
def settings(config: Config):
    # No waiting between requests and no retries unless a test asks for them.
    return replace(config.settings, per_host_delay_seconds=0, retries=0)


GREENHOUSE = "boards-api.greenhouse.io"


class FakeWeb:
    """Stands in for the three ATS APIs and for ntfy, and records what was published.

    Each ATS host serves one fixture board whichever company asks. A Greenhouse company
    can be given a board of its own with add_greenhouse_board().
    """

    def __init__(self):
        self.boards = {
            GREENHOUSE: load_fixture("greenhouse/point72.json"),
            "api.lever.co": load_fixture("lever/palantir.json"),
            "api.ashbyhq.com": load_fixture("ashby/openai.json"),
        }
        self.own_boards: dict[str, dict] = {}  # Greenhouse board id -> payload
        self.broken_hosts: set[str] = set()
        self.ntfy_down = False
        self.rejected_topics: set[str] = set()  # ntfy fails for these topics only
        self.published: list[dict] = []
        self.fetches: list[str] = []  # every board request, as host + path

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "ntfy.sh":
            message = json.loads(request.content)
            if self.ntfy_down or message["topic"] in self.rejected_topics:
                return httpx.Response(503)
            self.published.append(message)
            return httpx.Response(200, json={})
        self.fetches.append(host + request.url.path)
        if host in self.broken_hosts:
            return httpx.Response(500)
        return httpx.Response(200, json=self._board(host, request.url.path))

    def _board(self, host: str, path: str = ""):
        if host == GREENHOUSE:
            for board_id, payload in self.own_boards.items():
                if f"/boards/{board_id}/" in path:
                    return payload
        return self.boards[host]

    def add_greenhouse_board(self, board_id: str) -> None:
        """Give one Greenhouse company its own, initially empty, board."""
        self.own_boards[board_id] = {"jobs": [], "meta": {"total": 0}}

    def add_greenhouse_job(
        self, job_id: int, title: str, location: str = "London, UK", board: str | None = None
    ):
        jobs = (self.own_boards[board] if board else self.boards[GREENHOUSE])["jobs"]
        job = copy.deepcopy(self.boards[GREENHOUSE]["jobs"][0])
        job.update(
            id=job_id,
            title=title,
            location={"name": location},
            absolute_url=f"https://boards.greenhouse.io/{board or 'point72'}/jobs/{job_id}",
            first_published="2026-11-15T09:00:00-05:00",
        )
        jobs.append(job)

    def remove_greenhouse_job(self, job_id: int):
        board = self.boards[GREENHOUSE]
        board["jobs"] = [job for job in board["jobs"] if job["id"] != job_id]

    def sent_to(self, topic: str) -> list[dict]:
        return [message for message in self.published if message["topic"] == topic]


@pytest.fixture
def web():
    return FakeWeb()
