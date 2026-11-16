"""Amazon: the search API behind amazon.jobs.

API: GET https://www.amazon.jobs/en/search.json
         ?business_category[]=studentprograms&normalized_country_code[]=GBR&...
         &result_limit=100&offset=0&sort=recent

Public and unauthenticated; robots.txt on www.amazon.jobs disallows only its
/internal paths. The site lists tens of thousands of roles, so the request is narrowed
with two of the site's own filters:

  - the "Student Programs" business category, which is where Amazon files its
    internships and graduate roles (about 580 worldwide when this was written);
  - the countries in COUNTRIES.

Trade-off: a student role that Amazon files under another category (some AWS data
centre internships are filed under "aws") is not seen, nor is one in another country.

Each role carries its full description, about 10 KB, so a page of 100 is about 1 MB.
That is why this source has a polling interval in settings.yaml.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from jobtracker import log
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean

SOURCE = "custom"
HOST = "https://www.amazon.jobs"
API = HOST + "/en/search.json"

CATEGORY = "studentprograms"
COUNTRIES = ("GBR", "IND", "USA")
PAGE_SIZE = 100  # the most the API returns per request
MAX_PAGES = 10


def fetch(company: Company, http: HttpClient) -> list[Job]:
    jobs: dict[str, Job] = {}
    read = total = 0
    for page in range(MAX_PAGES):
        payload = http.get_json(API, params=_params(page * PAGE_SIZE))
        found = parse(company, payload)
        for job in found:
            jobs.setdefault(job.job_id, job)
        read += len(found)
        total = payload.get("hits") or 0
        if not found or read >= total:
            break
    if read < total:
        # Newest first, so what is cut off is the oldest roles.
        log.event("amazon_results_truncated", company=company.key, found=total, read=read)
    return list(jobs.values())


def _params(offset: int) -> dict[str, Any]:
    return {
        "business_category[]": CATEGORY,
        "normalized_country_code[]": list(COUNTRIES),
        "result_limit": PAGE_SIZE,
        "offset": offset,
        "sort": "recent",
    }


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    if payload.get("error"):
        raise ValueError(f"amazon.jobs search returned an error: {payload['error']}")
    jobs = []
    for item in payload["jobs"]:
        places = [json.loads(place) for place in item.get("locations") or []]
        locations = clean([_place(place) for place in places])
        jobs.append(
            Job(
                source=SOURCE,
                company=company.key,
                job_id=str(item["id_icims"]),
                title=item["title"].strip(),
                url=HOST + item["job_path"],
                locations=locations or clean([item.get("normalized_location")]),
                department=item.get("job_category") or None,
                posted_at=_long_date(item.get("posted_date")),
                # "VIRTUAL" is the site's word for a remote role.
                remote_hint=True if any(p.get("type") == "VIRTUAL" for p in places) else None,
                description="\n".join(
                    item[name]
                    for name in ("description", "basic_qualifications", "preferred_qualifications")
                    if item.get(name)
                )
                or None,
            )
        )
    return jobs


def _place(place: dict[str, Any]) -> str:
    """ "London, England, United Kingdom", with the country spelt out."""
    city = place.get("normalizedCityName") or place.get("city")
    country = place.get("normalizedCountryName") or place.get("normalizedCountryCode")
    return ", ".join(clean([city, place.get("normalizedStateName"), country]))


def _long_date(value: str | None) -> date | None:
    """ "November 11, 2026" -> 2026-11-11."""
    try:
        return datetime.strptime(value or "", "%B %d, %Y").date()
    except ValueError:
        return None
