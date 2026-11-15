"""SmartRecruiters, read from the public job board a candidate sees.

Board: GET https://careers.smartrecruiters.com/{board}
       GET https://careers.smartrecruiters.com/{board}/api/groups?page=1
       GET https://careers.smartrecruiters.com/{board}/api/more?search=&type=location&value=...&page=1

**Not the postings API.** SmartRecruiters also has a JSON API, on
api.smartrecruiters.com, and that host's robots.txt disallows every crawler but one
(`User-agent: * / Disallow: /`). An explicit disallow is never worked around, so this
adapter does not touch that host. The public board is a different thing on a different
host: the page SmartRecruiters publishes for candidates, which nobody has disallowed.
careers.smartrecruiters.com has no robots.txt at all (its /robots.txt redirects to the
job-board home page), which settings.yaml records for the client
(`http.hosts_without_robots_txt`).

The board is HTML, read with the standard library's parser. It lists jobs grouped by
location, a few groups to a page:

  - the board page holds the first groups and says how many pages of groups there are;
    the rest come from /api/groups, one request a page;
  - a group shows its first nine jobs. A larger group links to /api/more, which is paged
    the same way and whose page 0 repeats the nine already shown.

So a small board is one or two requests and a large one (ServiceNow: about 1,000 jobs
in 90 locations) is dozens, which is why this source has a polling interval in
settings.yaml. MAX_REQUESTS caps one company; a board cut short is logged
(`smartrecruiters_truncated`).

A job open in several locations appears in each group and is kept once, with all of
them. The board gives no department and no date.

**Only boards that are actually served here.** A company can point its board at its own
careers site instead (Wise, Legal & General and Check Point do): the board URL then
redirects away, there is no list on the page that comes back, and the fetch fails
rather than reading an unrelated site. Those companies are not configured for this
adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from urllib.parse import unquote

from jobtracker import log
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean

NAME = "smartrecruiters"
BOARD = "https://careers.smartrecruiters.com/{board}"

MAX_REQUESTS = 150  # for one company in one run


@dataclass
class Group:
    """One location's section of a board."""

    location: str = ""
    total: int = 0  # how many jobs the board says the group has
    jobs: list[Job] = field(default_factory=list)
    # Where the rest of a long group is fetched from: the "Show more jobs" link.
    more_type: str | None = None
    more_value: str | None = None


def fetch(company: Company, http: HttpClient) -> list[Job]:
    board = BOARD.format(board=company.board)
    budget = _Budget(company, http)

    page = budget.get(board)
    groups, group_pages = parse_board(company, page)
    for number in range(1, group_pages):
        more = budget.get(f"{board}/api/groups", {"page": number})
        if more is None:
            break
        groups += parse_groups(company, more)

    jobs: dict[str, Job] = {}
    for group in groups:
        found = list(group.jobs)
        number = 1
        while len(found) < group.total and group.more_value:
            params = {
                "search": "",
                "type": group.more_type or "location",
                "value": group.more_value,
                "page": number,
            }
            fragment = budget.get(f"{board}/api/more", params)
            extra = parse_jobs(company, fragment, group.location) if fragment else []
            if not extra:
                break  # out of budget, or the board has fewer jobs than it announced
            found += extra
            number += 1
        for job in found:
            _add(jobs, job)
    if budget.exhausted:
        log.event("smartrecruiters_truncated", company=company.key, requests=MAX_REQUESTS)
    return list(jobs.values())


class _Budget:
    """Fetches for one company, refusing once MAX_REQUESTS have been made."""

    def __init__(self, company: Company, http: HttpClient):
        self._company, self._http = company, http
        self._made = 0
        self.exhausted = False

    def get(self, url: str, params: dict | None = None) -> str | None:
        if self._made >= MAX_REQUESTS:
            self.exhausted = True
            return None
        self._made += 1
        return self._http.get_text(url, params=params)


def parse(company: Company, page: str) -> list[Job]:
    """Every job on one board page (the adapters' common entry point)."""
    jobs: dict[str, Job] = {}
    for group in parse_board(company, page)[0]:
        for job in group.jobs:
            _add(jobs, job)
    return list(jobs.values())


def parse_board(company: Company, page: str) -> tuple[list[Group], int]:
    """The groups on a board page, and how many pages of groups the board has."""
    parser = _run(company, page)
    if parser.group_pages is None:
        # A login page, an error, or the company's own careers site after a redirect.
        raise ValueError("SmartRecruiters board page has no job list (redirected elsewhere?)")
    return parser.groups, parser.group_pages


def parse_groups(company: Company, fragment: str) -> list[Group]:
    """The groups in one /api/groups response."""
    return _run(company, fragment).groups


def parse_jobs(company: Company, fragment: str, location: str) -> list[Job]:
    """The jobs in one /api/more response, which belong to the group that asked."""
    return _run(company, fragment, location).loose


def _add(jobs: dict[str, Job], job: Job) -> None:
    """Keep one job per posting, gathering the locations of its repeat listings."""
    seen = jobs.get(job.job_id)
    if seen is None:
        jobs[job.job_id] = job
    else:
        jobs[job.job_id] = replace(
            seen,
            locations=clean([*seen.locations, *job.locations]),
            remote_hint=seen.remote_hint or job.remote_hint,
        )


def _run(company: Company, text: str, location: str = "") -> _BoardParser:
    parser = _BoardParser(company, location)
    parser.feed(text)
    parser.close()
    return parser


class _BoardParser(HTMLParser):
    """Reads groups and jobs out of board markup.

        <div data-groups-pages="3" ...>
          <section data-qty="12" class="... js-group">
            <h3 class="opening-title ...">Amsterdam, Netherlands</h3>
            <a class="... js-job-ad-link" href="https://jobs.smartrecruiters.com/Acme/123-title">
              <h4 class="... job-title ...">Title</h4>
              <spl-tooltip content="Employees can work remotely">   (remote jobs only)
            </a>
            <a class="... js-more" data-type="location" data-value="Amsterdam%2C%20NL">

    Jobs outside any section (an /api/more response) are collected in `loose`.
    """

    def __init__(self, company: Company, location: str):
        super().__init__(convert_charrefs=True)
        self._company = company
        self._loose_location = location
        self.group_pages: int | None = None
        self.groups: list[Group] = []
        self.loose: list[Job] = []
        self._group: Group | None = None
        self._href: str | None = None  # set while inside a job's link
        self._remote = False
        self._text: list[str] | None = None  # collecting the text of an h3 or h4
        self._title = ""

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        attrs = dict(attributes)
        classes = (attrs.get("class") or "").split()
        if "data-groups-pages" in attrs:
            self.group_pages = int(attrs["data-groups-pages"] or 0)
        elif tag == "section" and "js-group" in classes:
            self._group = Group(total=int(attrs.get("data-qty") or 0))
            self.groups.append(self._group)
        elif tag == "h3" and "opening-title" in classes:
            self._text = []
        elif tag == "a" and "js-job-ad-link" in classes:
            self._href, self._remote, self._title = attrs.get("href"), False, ""
        elif tag == "h4" and "job-title" in classes and self._href:
            self._text = []
        elif tag == "spl-tooltip" and self._href:
            self._remote = self._remote or "remote" in (attrs.get("content") or "").lower()
        elif tag == "a" and "js-more" in classes and self._group:
            self._group.more_type = attrs.get("data-type")
            self._group.more_value = unquote(attrs.get("data-value") or "") or None

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h3" and self._text is not None and self._group:
            self._group.location = " ".join("".join(self._text).split())
            self._text = None
        elif tag == "h4" and self._text is not None:
            self._title = " ".join("".join(self._text).split())
            self._text = None
        elif tag == "a" and self._href:
            self._finish_job()
        elif tag == "section":
            self._group = None

    def _finish_job(self) -> None:
        href, self._href = self._href or "", None
        # ".../Acme/744000160987819-senior-engineer" -> "744000160987819"
        job_id = href.rstrip("/").rsplit("/", 1)[-1].split("-", 1)[0]
        if not job_id.isdigit() or not self._title:
            raise ValueError("SmartRecruiters board job has an unexpected shape")
        location = self._group.location if self._group else self._loose_location
        job = Job(
            source=NAME,
            company=self._company.key,
            job_id=job_id,
            title=self._title,
            url=href,
            locations=clean([location]),
            remote_hint=True if self._remote else None,
        )
        (self._group.jobs if self._group else self.loose).append(job)
