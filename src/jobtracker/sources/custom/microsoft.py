"""Microsoft: the search API behind apply.careers.microsoft.com (an Eightfold site).

API: GET https://apply.careers.microsoft.com/api/pcsx/search
         ?domain=microsoft.com&query=&location=&start=0&sort_by=timestamp
         &filter_seniority=Intern

Public and unauthenticated. robots.txt on that host disallows everything except a
short list of paths, and /api/pcsx (this endpoint) and /careers (the job pages the
alerts link to) are both on the list.

The site holds a few thousand roles and returns 10 per request, so the search is
narrowed with the site's own "Experience level: Intern" filter: internships worldwide,
which is where Microsoft files its student programmes (44 roles when this was written,
5 requests). Trade-off: graduate ("Entry" level) roles are not seen.
"""

from __future__ import annotations

from typing import Any

from jobtracker import log
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean, parse_epoch_ms

SOURCE = "custom"
HOST = "https://apply.careers.microsoft.com"
API = HOST + "/api/pcsx/search"

SENIORITY = "Intern"
PAGE_SIZE = 10  # fixed by the API
MAX_PAGES = 30

_REMOTE_HINTS = {"remote": True, "hybrid": False, "onsite": False}


def fetch(company: Company, http: HttpClient) -> list[Job]:
    jobs: dict[str, Job] = {}
    read = total = 0
    for page in range(MAX_PAGES):
        payload = http.get_json(API, params=_params(page * PAGE_SIZE))
        found = parse(company, payload)
        for job in found:
            jobs.setdefault(job.job_id, job)
        read += len(found)
        total = payload["data"].get("count") or 0
        if not found or read >= total:
            break
    if read < total:
        log.event("microsoft_results_truncated", company=company.key, found=total, read=read)
    return list(jobs.values())


def _params(start: int) -> dict[str, Any]:
    return {
        "domain": "microsoft.com",
        "query": "",
        "location": "",
        "start": start,
        "sort_by": "timestamp",
        "filter_seniority": SENIORITY,
    }


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    if payload.get("status") != 200:
        raise ValueError(f"Microsoft careers search answered status {payload.get('status')}")
    jobs = []
    for item in payload["data"]["positions"]:
        posted = item.get("postedTs")  # seconds since 1970
        jobs.append(
            Job(
                source=SOURCE,
                company=company.key,
                job_id=str(item["id"]),
                title=item["name"].strip(),
                url=HOST + item["positionUrl"],
                locations=clean(item.get("locations") or []),
                department=item.get("department") or None,
                posted_at=parse_epoch_ms(posted * 1000 if posted else None),
                remote_hint=_REMOTE_HINTS.get((item.get("workLocationOption") or "").lower()),
            )
        )
    return jobs
