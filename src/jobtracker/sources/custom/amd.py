"""AMD: the search API behind careers.amd.com (an iCIMS "Attract" careers site).

API: GET https://careers.amd.com/api/jobs
         ?page=1&limit=100&sortBy=posted_date&descending=true&internal=false
         &categories=Student / Intern / Temp        (or &keywords=intern)

Public and unauthenticated. robots.txt on careers.amd.com allows everything and sets
`Crawl-delay: 5`, which the client honours. (Applications are taken on a second host,
careers-amd.icims.com, whose robots.txt disallows everything. This adapter never
requests it: postings link to their page on careers.amd.com.)

The site lists about 1,300 roles and every role in a response carries its full
description, about 33 KB, so reading them all would be 43 MB. Instead the adapter makes
a few narrow requests and merges them by requisition number:

  - the site's own "Student / Intern / Temp" category;
  - one keyword search per term in KEYWORDS, because AMD files many internships and
    placements under "Engineering" instead. Newest first, first page only.

The keyword search reads descriptions as well as titles, so it also returns unrelated
roles that merely mention the word; the filters drop those by title. A search that
overflows its page is logged (`amd_results_truncated`).

With the crawl delay this is about 20 seconds of polite waiting, which is why this
source has a polling interval in settings.yaml.
"""

from __future__ import annotations

from typing import Any

from jobtracker import log
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean, parse_iso_date

SOURCE = "custom"
HOST = "https://careers.amd.com"
API = HOST + "/api/jobs"
JOB_URL = HOST + "/jobs/{slug}"

CATEGORY = "Student / Intern / Temp"
KEYWORDS = ("intern", "placement", "graduate")
PAGE_SIZE = 100


def fetch(company: Company, http: HttpClient) -> list[Job]:
    searches = [{"categories": CATEGORY}, *({"keywords": term} for term in KEYWORDS)]
    jobs: dict[str, Job] = {}
    for search in searches:
        params = {
            "page": 1,
            "limit": PAGE_SIZE,
            "sortBy": "posted_date",
            "descending": "true",
            "internal": "false",
            **search,
        }
        payload = http.get_json(API, params=params)
        found = parse(company, payload)
        for job in found:
            jobs.setdefault(job.job_id, job)
        total = payload.get("totalCount") or 0
        if total > len(found):
            log.event(
                "amd_results_truncated", company=company.key, **search, found=total, read=len(found)
            )
    return list(jobs.values())


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    jobs = []
    for item in payload["jobs"]:
        data = item["data"]
        categories = [category.get("name") for category in data.get("categories") or []]
        place = ", ".join(clean([data.get("city"), data.get("state"), data.get("country")]))
        jobs.append(
            Job(
                source=SOURCE,
                company=company.key,
                job_id=str(data["slug"]),
                title=data["title"].strip(),
                url=JOB_URL.format(slug=data["slug"]),
                locations=clean([place or data.get("location_name")]),
                department=" / ".join(clean(categories)) or None,
                posted_at=parse_iso_date(data.get("posted_date")),
            )
        )
    return jobs
