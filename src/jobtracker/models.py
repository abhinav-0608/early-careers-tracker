"""The data shapes passed between pipeline steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Company:
    """One entry from config/companies.yaml, the list shared by every profile.

    Nothing here is personal. Whether a company is "FAANG+" is a property of a
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

    @property
    def host_group(self) -> str:
        """Companies with the same value are fetched from the same host.

        Every Greenhouse board is on one host; every tal.net tenant has its own; a
        custom company has its own site. Workday tenants have a hostname each but share
        a handful of servers ("wd5"), and the client spaces requests per server, so the
        tenants of one server are one group. Requests to one host are made one at a
        time, so the run fetches each group in a single queue (see pipeline.fetch_all).
        """
        if self.ats == "workday":
            return f"{self.ats}:{self.host}"
        own_host = self.tenant or (self.board if self.ats == "custom" else None)
        return f"{self.ats}:{own_host}" if own_host else self.ats

    @property
    def poll_kind(self) -> str:
        """The key this company's polling interval is set under in settings.yaml.

        The ATS name, or for a custom company the name of its module ("google").
        """
        return (self.board or self.ats) if self.ats == "custom" else self.ats


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
    # The posting's text, only when the source's list response already carries it
    # (Lever, Ashby, Amazon). Other sources are asked for it per job, and only for jobs
    # that have passed the title and location filters (sources/descriptions.py).
    description: str | None = field(default=None, compare=False, repr=False)

    @property
    def key(self) -> str:
        """Primary dedupe key: source + company + the source's job id."""
        return f"{self.source}:{self.company}:{self.job_id}"


# Outcomes of fetching one company.
OK = "ok"
ERROR = "error"  # HTTP error, malformed response, unexpected shape
TIMEOUT = "timeout"
EMPTY = "empty"  # returned 0 jobs after previously returning some
BLOCKED = "blocked"  # robots.txt disallows the fetch, or could not be read
NOT_DUE = "not_due"  # skipped this run by its polling interval: neither a failure nor a fetch
RATE_LIMITED = "rate_limited"  # the host answered 429 this run; it is now backing off
BACKING_OFF = "backing_off"  # not asked: its host answered 429 earlier and is being left alone
PAUSED = "paused"  # not asked: its whole kind of source is paused in settings.yaml (pauses.py)
# Statuses in which nothing was asked of the source, so nothing is recorded about it.
NOT_ASKED = (NOT_DUE, BACKING_OFF, PAUSED)


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
    # True when the fetch succeeded but left out the part of the source that has a
    # polling interval and was not due (a large Workday site's searches). The jobs it
    # did find count as usual; a job it did not find proves nothing, so none is closed.
    partial: bool = False
    # The polling-interval key this fetch used up, if any: the source's own kind, or
    # "workday_search" when a Workday tenant's large sites were searched.
    polled_kind: str | None = None
