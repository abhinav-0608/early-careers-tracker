"""HSBC: the student and graduate job search on apply.careers.hsbc.com (emerging talent).

Page: GET https://apply.careers.hsbc.com/emergingtalent/search/
          ?q=&sortColumn=referencedate&sortDirection=desc&startrow=0

HSBC's early-careers site (SAP SuccessFactors career pages) lists every student and
graduate programme worldwide, 25 to a server-rendered page: spring and insight
programmes, internships, placements and graduate schemes (37 roles when this was
written, two requests). Each row carries the title, location, opening date, programme
type and area of interest; the posting page (descriptions) is /emergingtalent/job/...

robots.txt (checked 2026-11-16) disallows only the apply, talent-community, e-mail and
/services/ paths, so the search and posting pages may be read. The site's terms and
conditions allow copying extracts for personal information and say nothing on
automated access (checked 2026-11-16).
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean

SOURCE = "custom"
HOST = "https://apply.careers.hsbc.com"
SEARCH = HOST + "/emergingtalent/search/"
PAGE_SIZE = 25  # fixed by the site
MAX_PAGES = 10

_TILE = re.compile(r'(?s)<li class="job-tile job-id-(\d+)[^"]*"[^>]*data-url="([^"]+)".*?</li>')
_TITLE = re.compile(r'(?s)<a class="jobTitle-link[^"]*"[^>]*>(.*?)</a>')
_TOTAL = re.compile(r"Showing\s+\d+\s+to\s+\d+\s+of\s+(\d+)\s+Jobs")


def fetch(company: Company, http: HttpClient) -> list[Job]:
    jobs: dict[str, Job] = {}
    for page in range(MAX_PAGES):
        text = http.get_text(SEARCH, params=_params(page * PAGE_SIZE))
        found = parse(company, text)
        for job in found:
            jobs.setdefault(job.job_id, job)
        if not found or (page + 1) * PAGE_SIZE >= total(text):
            break
    return list(jobs.values())


def _params(start: int) -> dict[str, str]:
    return {"q": "", "sortColumn": "referencedate", "sortDirection": "desc", "startrow": str(start)}


def total(page: str) -> int:
    """How many roles the search found; 0 when the page does not say."""
    match = _TOTAL.search(page)
    return int(match.group(1)) if match else 0


def parse(company: Company, page: str) -> list[Job]:
    if "job-tile" not in page and not _TOTAL.search(page):
        raise ValueError("HSBC search page has neither results nor a result count")
    jobs = []
    for match in _TILE.finditer(page):
        job_id, path = match.group(1), html.unescape(match.group(2))
        tile = match.group(0)
        title = _TITLE.search(tile)
        if not title:
            continue
        opened = _field(tile, job_id, "customfield1")
        jobs.append(
            Job(
                source=SOURCE,
                company=company.key,
                job_id=job_id,
                title=_text(title.group(1)),
                url=HOST + path,
                locations=clean([_field(tile, job_id, "location")]),
                department=_field(tile, job_id, "department") or None,
                posted_at=_date(opened),
            )
        )
    return jobs


def _field(tile: str, job_id: str, name: str) -> str:
    """A labelled field of one row, as the desktop layout shows it ("" if absent)."""
    match = re.search(rf'(?s)id="job-{job_id}-desktop-section-{name}-value"[^>]*>(.*?)</div>', tile)
    return _text(match.group(1)) if match else ""


def _text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def _date(value: str):
    """ "Fri Nov 06, 2026" -> date, or None."""
    try:
        return datetime.strptime(value, "%a %b %d, %Y").date()
    except ValueError:
        return None
