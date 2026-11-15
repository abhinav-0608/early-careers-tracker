"""Apple: the search page of jobs.apple.com.

Page: GET https://jobs.apple.com/en-gb/search?team=internships-STDNT-INTRN&sort=newest&page=2

The page is rendered on the server and carries its results as JSON inside a script tag
(`window.__staticRouterHydrationData = JSON.parse("...")`), which is what this adapter
reads: no HTML parsing, no browser, and none of the site's token-protected API calls.

The search is the site's own "Students: Internships" team filter, worldwide, newest
first: about 130 roles when this was written, 20 to a page, so 7 pages of about
320 KB each. That weight is why this source has a polling interval in settings.yaml.
Trade-off: only roles Apple files under that team are seen (not graduate roles, and not
its retail or support programmes).

A role open in several places is listed once per place under the same position number;
those are merged into one job with every location.

robots.txt: jobs.apple.com has none. Its /robots.txt redirects to apple.com's
"page not found", which the client follows (checking apple.com's own robots.txt first)
and reads as "no rules", as RFC 9309 describes.

The data is read by name and checked before use, so a change to Apple's page shows up
as a failed fetch and a health alert rather than as wrong data.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from jobtracker import log
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean, parse_iso_date

SOURCE = "custom"
HOST = "https://jobs.apple.com"
URL = HOST + "/en-gb/search"
JOB_URL = HOST + "/en-gb/details/{listing_id}/{slug}"

TEAM = "internships-STDNT-INTRN"
PAGE_SIZE = 20  # fixed by the site
MAX_PAGES = 15

_DATA_MARKER = "window.__staticRouterHydrationData = JSON.parse("


def fetch(company: Company, http: HttpClient) -> list[Job]:
    jobs: dict[str, Job] = {}
    read = total = 0
    for page_number in range(1, MAX_PAGES + 1):
        params = {"team": TEAM, "sort": "newest"}
        if page_number > 1:
            params["page"] = str(page_number)
        page = http.get_text(URL, params=params)
        results = _search(page)["searchResults"]
        for job in parse(company, page):
            _add(jobs, job)
        read += len(results)
        total = total_results(page)
        if not results or read >= total:
            break
    if read < total:
        log.event("apple_results_truncated", company=company.key, found=total, read=read)
    return list(jobs.values())


def parse(company: Company, page: str) -> list[Job]:
    """Translate the roles embedded in one results page."""
    jobs: dict[str, Job] = {}
    for role in _search(page)["searchResults"]:
        position = _text(role.get("positionId"))
        locations = [_place(place) for place in role.get("locations") or []]
        team = role.get("team") or {}
        _add(
            jobs,
            Job(
                source=SOURCE,
                company=company.key,
                job_id=position,
                title=_text(role.get("postingTitle")).strip(),
                url=JOB_URL.format(
                    listing_id=role.get("id") or position,
                    slug=role.get("transformedPostingTitle") or "",
                ).rstrip("/"),
                locations=clean(locations),
                department=team.get("teamName") or None,
                posted_at=parse_iso_date(role.get("postDateInGMT")),
                remote_hint=True if role.get("homeOffice") is True else None,
            ),
        )
    return list(jobs.values())


def total_results(page: str) -> int:
    """How many roles the search matched in all, of which a page shows 20."""
    return _search(page)["totalRecords"]


def _add(jobs: dict[str, Job], job: Job) -> None:
    """Keep one job per position, gathering the locations of its repeat listings."""
    seen = jobs.get(job.job_id)
    if seen is None:
        jobs[job.job_id] = job
    else:
        jobs[job.job_id] = replace(seen, locations=clean([*seen.locations, *job.locations]))


def _place(place: dict[str, Any]) -> str:
    """ "Cambridge, United Kingdom"; just "Singapore" when city and country are the same."""
    return ", ".join(clean([place.get("name"), place.get("countryName")]))


def _search(page: str) -> dict[str, Any]:
    """The search part of the page's embedded data: results, total, page number."""
    start = page.find(_DATA_MARKER)
    if start < 0:
        raise ValueError("Apple jobs page has no embedded data (layout changed?)")
    # The data is a JSON document written out as a JavaScript string literal.
    text, _ = json.JSONDecoder().raw_decode(page, start + len(_DATA_MARKER))
    data = json.loads(text) if isinstance(text, str) else None
    search = ((data or {}).get("loaderData") or {}).get("search")
    if (
        not isinstance(search, dict)
        or not isinstance(search.get("searchResults"), list)
        or not isinstance(search.get("totalRecords"), int)
    ):
        raise ValueError("Apple jobs search data has an unexpected shape")
    return search


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Apple jobs role has an unexpected shape")
    return value
