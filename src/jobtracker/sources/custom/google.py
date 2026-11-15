"""Google Careers, which also lists Google DeepMind's roles.

Page: GET https://www.google.com/about/careers/applications/jobs/results
          ?company=Google&target_level=INTERN_AND_APPRENTICE&location=United Kingdom&sort_by=date

There is no API. The results page is rendered on the server and carries its data as a
JSON array inside a script tag (`AF_initDataCallback({key: 'ds:1', ... data: [...]})`),
which is what this adapter reads: no HTML parsing and no browser.

**What robots.txt allows, and the limit that follows.** google.com's robots.txt allows
the results page but disallows every paginated form of it (`results?page=`,
`results?*&page=`). So only the first page of a search, 20 roles, may be read, and this
adapter never asks for a second. It therefore keeps each search narrow enough to fit on
one page, and sorts newest first so that when a search does overflow it is the oldest
roles that are missing, never a new one:

  - intern and apprentice level only (which is where spring, first-year and STEP
    programmes are filed), not graduate or experienced roles;
  - Google: one search per country in COUNTRIES; DeepMind: one search, worldwide.

An overflow is logged (`google_results_truncated`) with the counts. Roles outside those
searches (graduate roles, other countries) are not seen at all. That is the price of
staying inside robots.txt.

The data is an array read by position, not by name, so a change to Google's page shows
up as a failed fetch and a health alert rather than as wrong data: every field is checked
before use.

No posting date is read. The array has three unlabelled timestamps per role and nothing
to say which is "posted".
"""

from __future__ import annotations

import json
from typing import Any

from jobtracker import log
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean

SOURCE = "custom"
URL = "https://www.google.com/about/careers/applications/jobs/results"
JOB_URL = URL + "/{job_id}"

LEVEL = "INTERN_AND_APPRENTICE"
COUNTRIES = ("United Kingdom", "India", "United States")

# Which searches each company key gets: (the site's "company" filter, countries).
# None means one worldwide search.
SEARCHES: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "google": ("Google", COUNTRIES),
    "google-deepmind": ("DeepMind", None),
}

_DATA_MARKER = "AF_initDataCallback({key: 'ds:1'"

# Positions within one role's array.
_ID, _TITLE, _LOCATIONS = 0, 1, 9


def fetch(company: Company, http: HttpClient) -> list[Job]:
    employer, countries = SEARCHES[company.key]
    jobs: dict[str, Job] = {}
    for country in countries or (None,):
        params = {"company": employer, "target_level": LEVEL, "sort_by": "date"}
        if country:
            params["location"] = country
        page = http.get_text(URL, params=params)
        found = parse(company, page)
        for job in found:
            jobs.setdefault(job.job_id, job)
        total = total_results(page)
        if total > len(found):
            log.event(
                "google_results_truncated",
                company=company.key,
                country=country or "worldwide",
                found=total,
                read=len(found),
            )
    return list(jobs.values())


def parse(company: Company, page: str) -> list[Job]:
    """Translate the roles embedded in one results page."""
    roles = _data(page)[0] or []  # null when the search has no results
    jobs = []
    for role in roles:
        locations = [place[0] for place in role[_LOCATIONS] or []]
        jobs.append(
            Job(
                source=SOURCE,
                company=company.key,
                job_id=_text(role[_ID]),
                title=_text(role[_TITLE]).strip(),
                url=JOB_URL.format(job_id=_text(role[_ID])),
                locations=clean(locations),
            )
        )
    return jobs


def total_results(page: str) -> int:
    """How many roles the search matched in all, of which the page shows the first 20."""
    return int(_data(page)[2])


def _data(page: str) -> list[Any]:
    """The results array: [roles or null, _, total, page size]."""
    start = page.find(_DATA_MARKER)
    if start < 0:
        raise ValueError("Google careers page has no results data (layout changed?)")
    start = page.index("data:", start) + len("data:")
    data, _ = json.JSONDecoder().raw_decode(page, start)
    if not isinstance(data, list) or len(data) < 4 or not isinstance(data[2], int):
        raise ValueError("Google careers results data has an unexpected shape")
    return data


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Google careers role has an unexpected shape")
    return value
