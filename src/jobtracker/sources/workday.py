"""Workday job sites. One adapter for every tenant, configured per company.

API: POST https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
     {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

This is the endpoint the tenant's own job site calls. It is public and unauthenticated,
returns at most 20 postings per request, and reports the total (capped at 2000) on the
first page only.

A company lists every site worth polling (experienced, campus, graduate ...), and a
posting that appears on two of them is kept once.

Small sites are read in full. Large tenants hold thousands of postings, and paging
through all of them every 15 minutes would be both slow and rude, so a site with more
than FULL_FETCH_MAX_JOBS postings is searched instead: one query per term in
SEARCH_TERMS, a few pages each, merged by posting id. The terms are fixed here rather
than taken from anyone's filters because a board is fetched once and shared by every
profile. They cover every role type a profile can enable; a role type whose titles none
of them would find needs a term added here.

Two things the API does not give: an exact posting date (only "Posted 3 Days Ago", which
is not stored, to keep state from changing as the days tick over) and a location for
postings in several places (just "4 Locations", which the filters treat as unclear).

robots.txt: each tenant's file lists the sites it allows. The JSON endpoint lives under
/wday/ and is not named there, so before calling it the adapter asks whether the site's
own page (/{site}/) is allowed, and does not call the endpoint if it is not.
"""

from __future__ import annotations

from typing import Any

from jobtracker import log
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean

NAME = "workday"
BASE = "https://{tenant}.{host}.myworkdayjobs.com"
API = BASE + "/wday/cxs/{tenant}/{site}/jobs"

PAGE_SIZE = 20  # the most the API returns per request
FULL_FETCH_MAX_JOBS = 200  # sites up to this size are read in full (10 requests)
MAX_PAGES_PER_SEARCH = 2  # per search term on a large site

# Searched on sites too large to read in full. The search looks at titles and
# descriptions and matches word beginnings, so "intern" also finds "internship".
SEARCH_TERMS = (
    "spring",
    "insight",
    "discovery",
    "first year",
    "explore",
    "intern",
    "placement",
    "graduate",
)


def fetch(company: Company, http: HttpClient) -> list[Job]:
    jobs: dict[str, Job] = {}
    for site in company.sites:
        base = BASE.format(tenant=company.tenant, host=company.host)
        http.require_allowed(f"{base}/{site}/")
        for job in _fetch_site(company, site, http):
            jobs.setdefault(job.job_id, job)  # the same posting on two sites
    return list(jobs.values())


def _fetch_site(company: Company, site: str, http: HttpClient) -> list[Job]:
    url = API.format(tenant=company.tenant, host=company.host, site=site)
    first = _page(http, url, "", 0)
    total = first.get("total") or 0
    jobs = parse(company, first, site)

    if total <= FULL_FETCH_MAX_JOBS:
        for offset in range(PAGE_SIZE, total, PAGE_SIZE):
            jobs += parse(company, _page(http, url, "", offset), site)
        return jobs

    # A large site: the first page (its newest postings) plus the targeted searches.
    for term in SEARCH_TERMS:
        page = _page(http, url, term, 0)
        found = page.get("total") or 0
        jobs += parse(company, page, site)
        limit = min(found, MAX_PAGES_PER_SEARCH * PAGE_SIZE)
        for offset in range(PAGE_SIZE, limit, PAGE_SIZE):
            jobs += parse(company, _page(http, url, term, offset), site)
        if found > limit:
            # Results are ranked by relevance, so what is cut off is the weakest matches.
            log.event(
                "workday_search_truncated",
                company=company.key,
                site=site,
                term=term,
                found=found,
                read=limit,
            )
    return jobs


def _page(http: HttpClient, url: str, search: str, offset: int) -> dict[str, Any]:
    body = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": search}
    return http.post_json(url, body).json()


def parse(company: Company, payload: dict[str, Any], site: str) -> list[Job]:
    """Translate one page of results from one site."""
    base = BASE.format(tenant=company.tenant, host=company.host)
    jobs = []
    for item in payload["jobPostings"]:
        path = item.get("externalPath")
        if not path or not item.get("title"):
            continue  # a placeholder row with no posting behind it
        jobs.append(
            Job(
                source=NAME,
                company=company.key,
                job_id=_job_id(path),
                title=item["title"].strip(),
                url=f"{base}/{site}{path}",
                locations=clean([item.get("locationsText")]),
            )
        )
    return jobs


def _job_id(path: str) -> str:
    """The requisition id that ends a posting's path.

    "/job/London/Imperial-Work-Placements_166432" -> "166432". The words before the
    underscore are the title and change when it is edited; the id does not.
    """
    return path.rstrip("/").rsplit("/", 1)[-1].rsplit("_", 1)[-1]
