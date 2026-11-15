"""tal.net (Oleeo) candidate job boards.

Several UK banks run their campus recruiting (spring weeks, insight events, internships)
on Oleeo, each on its own subdomain of tal.net. There is no JSON API, so this adapter
reads the public job-board page a candidate sees:

    GET https://{tenant}.tal.net/vx/{board}/candidate/jobboard/vacancy/{site}/adv/

`board` is the tenant's path prefix (language, brand) and `site` the number of one job
board; a tenant usually has one board for programmes and another for events. A board
lists 50 postings per page and links to the next with `?start=50`.

robots.txt on every tal.net tenant allows these pages, disallows `/*/ats/`, `/*/agent/`
and `/*/channel/`, and sets `Crawl-delay: 10`. HttpClient enforces all of that, so a board
with two pages takes about half a minute. The tenant's root URL redirects to `/ats/login`,
which is disallowed; this adapter never requests it, and HttpClient would refuse the
redirect if it did.

Boards do not publish a posting date. (Some tenants offer an Atom feed that does, but
Morgan Stanley and Nomura have it switched off, so the page is the one source that works
for all of them.)

A board shows each posting as a table row, as a tile, or as both, depending on how the
tenant set it up. Both carry the posting id, a link, and labelled fields; the parser reads
whichever is there and merges them by id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean

NAME = "talnet"
URL = "https://{tenant}.tal.net/vx/{board}/candidate/jobboard/vacancy/{site}/adv/"
MAX_PAGES = 10  # 500 postings per board; a guard against a "next" link that loops

# The labels tenants give the column that says where a posting is. Other columns
# (deadline, event date, venue notes) are ignored.
LOCATION_LABELS = ("city", "location", "locations", "country")

_NEXT_START = re.compile(r"^\?start=(\d+)$")
_WHITESPACE = re.compile(r"\s+")


@dataclass
class Posting:
    """One posting as the board shows it, before it becomes a Job."""

    opp_id: str
    title: str = ""
    url: str = ""
    fields: dict[str, str] = field(default_factory=dict)  # label (lowercase) -> value


def fetch(company: Company, http: HttpClient) -> list[Job]:
    jobs: dict[str, Job] = {}
    for site in company.sites:
        url = URL.format(tenant=company.tenant, board=company.board, site=site)
        start = 0
        for _ in range(MAX_PAGES):
            page = http.get_text(url, params={"start": start} if start else None)
            for job in parse(company, page):
                jobs.setdefault(job.job_id, job)  # a posting can sit on two boards
            following = next_start(page, start)
            if following is None:
                break
            start = following
    return list(jobs.values())


def parse(company: Company, page: str) -> list[Job]:
    """Translate one page of a board into jobs."""
    board = _read(page)
    return [
        Job(
            source=NAME,
            company=company.key,
            job_id=posting.opp_id,
            title=posting.title,
            url=posting.url,
            locations=clean([posting.fields.get(label) for label in LOCATION_LABELS]),
        )
        for posting in board.postings.values()
    ]


def next_start(page: str, current: int = 0) -> int | None:
    """The `start` offset of the following page, or None on the last page."""
    later = [start for start in _read(page).page_starts if start > current]
    return min(later) if later else None


def _read(page: str) -> _BoardParser:
    board = _BoardParser()
    board.feed(page)
    board.close()
    if not board.is_job_board:
        # A login page, an error page or a redesign. Raising makes it a failed fetch
        # (and in time a health alert) instead of an apparently empty board.
        raise ValueError("page is not a tal.net job board (no search results section)")
    return board


class _BoardParser(HTMLParser):
    """Collects the postings and the paging links from a job-board page.

    The markup, trimmed to what is read:

        <div class="search-results"> ...
          <th class="comm_list_thead">City</th>                       column labels
          <tr class="opp_123 search_res" data-oppid="123">            a table row
            <td class="comm_list_tbody"><a class="subject" href="...">Title</a></td>
            <td class="comm_list_tbody">London</td>
          </tr>
          <div class="opp_123 search_res candidate-opp-tile" data-oppid="123">   a tile
            <h3><a class="subject" href="...">Title</a></h3>
            <div class="candidate-opp-field-3">
              <span class="candidate-opp-field-label">City:</span> London</div>
          </div>
          <a href="?start=50">Next page</a>
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.is_job_board = False
        self.postings: dict[str, Posting] = {}
        self.page_starts: list[int] = []
        self._headers: list[str] = []
        # The posting being read, the tag that opened it and how deeply that tag is nested.
        self._posting: Posting | None = None
        self._posting_tag = ""
        self._depth = 0
        self._cell = -1  # index of the current table cell within the row
        # What the text currently being read is for: "header", "title", "label", "value".
        self._capture: str | None = None
        self._capture_tag = ""
        self._text: list[str] = []
        self._label = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()

        if "search-results" in classes:
            self.is_job_board = True
        if tag == "a":
            match = _NEXT_START.match(attributes.get("href") or "")
            if match:
                self.page_starts.append(int(match.group(1)))
        if tag == "th" and "comm_list_thead" in classes:
            self._begin("header", tag)

        if self._posting is None:
            if "search_res" in classes and attributes.get("data-oppid"):
                opp_id = attributes["data-oppid"] or ""
                self._posting = self.postings.setdefault(opp_id, Posting(opp_id))
                self._posting_tag, self._depth, self._cell = tag, 1, -1
            return

        if tag == self._posting_tag:
            self._depth += 1
        if tag == "a" and "subject" in classes:
            self._posting.url = attributes.get("href") or self._posting.url
            self._begin("title", tag)
        elif tag == "td" and "comm_list_tbody" in classes:
            self._cell += 1
            self._label = self._headers[self._cell] if self._cell < len(self._headers) else ""
            self._begin("value", tag)
        elif tag == "span" and "candidate-opp-field-label" in classes:
            self._begin("label", tag)
        elif tag == "div" and any(name.startswith("candidate-opp-field-") for name in classes):
            self._label = ""
            self._begin("value", tag)

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag == self._capture_tag:
            self._finish()
        if self._posting is not None and tag == self._posting_tag:
            self._depth -= 1
            if self._depth == 0:
                self._posting = None

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)

    def _begin(self, capture: str, tag: str) -> None:
        self._capture, self._capture_tag, self._text = capture, tag, []

    def _finish(self) -> None:
        text = _WHITESPACE.sub(" ", "".join(self._text)).strip()
        capture, self._capture = self._capture, None
        if capture == "header":
            self._headers.append(_label(text))
        elif self._posting is None:
            return
        elif capture == "title":
            self._posting.title = text or self._posting.title
        elif capture == "label":
            # A tile's label sits inside the field's <div>; carry on reading its value.
            self._label = _label(text)
            self._begin("value", "div")
        elif capture == "value" and self._label and text:
            self._posting.fields.setdefault(self._label, text)


def _label(text: str) -> str:
    return text.rstrip(":").strip().lower()
