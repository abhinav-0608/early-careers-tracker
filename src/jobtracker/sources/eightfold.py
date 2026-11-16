"""Eightfold careers sites: the public search API behind {company}.eightfold.ai.

API: GET https://{host}/api/pcsx/search
         ?domain={domain}&query=&location=&start=0&sort_by=timestamp
         &filter_seniority=Intern

Public and unauthenticated, 10 roles per request. Every Eightfold host's robots.txt
disallows everything except a short list of paths, and /api/pcsx (this endpoint) and
/careers (the job pages alerts link to, and descriptions are read from) are on it.
Eightfold's acceptable use policy has no clause on automated access (checked
2026-11-16). A tenant that answers this endpoint with 403 has switched it off and is
left alone (Netflix and Millennium, 2026-11-16).

A board holds thousands of roles, so the search is narrowed with the site's own
"Experience level: Intern" filter, where companies file their student programmes.
Trade-off: graduate ("Entry") roles are not seen.

In companies.yaml (`ats: eightfold`): `host` is the hostname and `board` the domain
the site searches, as named in its robots.txt sitemap line:

    - key: qualcomm
      ats: eightfold
      host: qualcomm.eightfold.ai
      board: qualcomm.com

Microsoft's site is the same system on its own hostname; custom/microsoft.py uses this
module but keeps its own source name, so its stored job keys do not change.
"""

from __future__ import annotations

from typing import Any

from jobtracker import log
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean, parse_epoch_ms

NAME = "eightfold"
SENIORITY = "Intern"
PAGE_SIZE = 10  # fixed by the API
MAX_PAGES = 30

_REMOTE_HINTS = {"remote": True, "hybrid": False, "onsite": False}


def fetch(company: Company, http: HttpClient) -> list[Job]:
    return search(company, http, host=company.host or "", domain=company.board or "")


def search(
    company: Company, http: HttpClient, host: str, domain: str, source: str = NAME
) -> list[Job]:
    """Every intern-level role on one Eightfold site, newest first, page by page."""
    jobs: dict[str, Job] = {}
    read = total = 0
    for page in range(MAX_PAGES):
        payload = http.get_json(
            f"https://{host}/api/pcsx/search", params=_params(domain, page * PAGE_SIZE)
        )
        found = parse(company, payload, host=host, source=source)
        for job in found:
            jobs.setdefault(job.job_id, job)
        read += len(found)
        total = payload["data"].get("count") or 0
        if not found or read >= total:
            break
    if read < total:
        log.event("eightfold_results_truncated", company=company.key, found=total, read=read)
    return list(jobs.values())


def _params(domain: str, start: int) -> dict[str, Any]:
    return {
        "domain": domain,
        "query": "",
        "location": "",
        "start": start,
        "sort_by": "timestamp",
        "filter_seniority": SENIORITY,
    }


def parse(
    company: Company, payload: dict[str, Any], host: str | None = None, source: str = NAME
) -> list[Job]:
    if payload.get("status") != 200:
        raise ValueError(f"{company.name} careers search answered status {payload.get('status')}")
    base = f"https://{host or company.host}"
    jobs = []
    for item in payload["data"]["positions"]:
        posted = item.get("postedTs")  # seconds since 1970
        jobs.append(
            Job(
                source=source,
                company=company.key,
                job_id=str(item["id"]),
                title=item["name"].strip(),
                url=base + item["positionUrl"],
                locations=clean(item.get("locations") or []),
                department=item.get("department") or None,
                posted_at=parse_epoch_ms(posted * 1000 if posted else None),
                remote_hint=_REMOTE_HINTS.get((item.get("workLocationOption") or "").lower()),
            )
        )
    return jobs
