"""The data shapes passed between pipeline steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Company:
    """One entry from config/companies.yaml, the list shared by every profile.

    Nothing here is personal. Whether a company is "dream tier" is a property of a
    profile, not of the company.
    """

    key: str
    name: str
    industry: str
    ats: str  # which adapter fetches it, or "unsupported"
    board: str | None = None  # board id in the ATS's API (or module name, for custom)
    # Workday boards are addressed by three parts instead of one board id:
    # https://{tenant}.{host}.myworkdayjobs.com/{site}. One tenant often runs several
    # sites (experienced hires, campus, graduates), so a company lists every site to poll.
    tenant: str | None = None
    host: str | None = None
    sites: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    careers_url: str | None = None
    note: str | None = None
    # True = only profiles that name this company watch it; watching its industry is
    # not enough. Lets a company be added for one person without alerting anyone else.
    opt_in: bool = False

    @property
    def source_key(self) -> str:
        """Identifies this company's board in state, e.g. "greenhouse:jane-street"."""
        return f"{self.ats}:{self.key}"


@dataclass(frozen=True)
class Job:
    """A posting in the common shape every adapter returns.

    Adapters only translate; they never filter or interpret. That keeps each adapter a
    short, exactly testable mapping from one source's response to this class.
    """

    source: str  # adapter name, e.g. "greenhouse"
    company: str  # Company.key
    job_id: str  # the source's own id for the posting
    title: str
    url: str
    locations: tuple[str, ...] = ()  # as published, one entry per location
    department: str | None = None
    posted_at: date | None = None
    remote_hint: bool | None = None  # explicit remote flag, when the source has one

    @property
    def key(self) -> str:
        """Primary dedupe key: source + company + the source's job id."""
        return f"{self.source}:{self.company}:{self.job_id}"


# Outcomes of fetching one company.
OK = "ok"
ERROR = "error"  # HTTP error, malformed response, unexpected shape
TIMEOUT = "timeout"
EMPTY = "empty"  # returned 0 jobs after previously returning some


@dataclass
class SourceResult:
    """What happened when one company's board was fetched.

    A board is fetched once per run and the result is then read by every profile that
    watches the company, so nothing profile-specific is recorded here.
    """

    company: Company
    status: str = OK
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
