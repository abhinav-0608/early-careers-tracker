"""JPMorgan Chase: Oracle Recruiting Cloud, through the API its careers site calls.

API: GET https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions
         ?onlyData=true&expand=requisitionList.secondaryLocations
         &finder=findReqs;siteNumber=CX_1001,limit=200,offset=0,selectedTitlesFacet=PAA,...

Public and unauthenticated. The site holds about 7,000 postings, nearly all of them for
experienced hires, so the request is narrowed with the site's own "Program Analysts &
Associate" job-family filter (PAA): the couple of hundred analyst, associate, internship
and student-programme roles. That is two requests per run instead of thirty-five.

Trade-off: an early-careers posting that JPMorgan files under another job family is not
seen. Widening the filter is a change to FACET below.

robots.txt: jpmc.fa.oraclecloud.com answers /robots.txt with 403 from its firewall, for
every client. Under RFC 9309 a 4xx means the host publishes no rules.
"""

from __future__ import annotations

from typing import Any

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean, parse_iso_date

SOURCE = "custom"
HOST = "https://jpmc.fa.oraclecloud.com"
API = HOST + "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
JOB_URL = HOST + "/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"

SITE = "CX_1001"
FACET = "PAA"  # titles facet: "Program Analysts & Associate"
PAGE_SIZE = 200
MAX_PAGES = 10


def fetch(company: Company, http: HttpClient) -> list[Job]:
    jobs: dict[str, Job] = {}
    for page in range(MAX_PAGES):
        offset = page * PAGE_SIZE
        payload = http.get_json(API, params=_params(offset))
        for job in parse(company, payload):
            jobs.setdefault(job.job_id, job)
        if offset + PAGE_SIZE >= _total(payload):
            break
    return list(jobs.values())


def _params(offset: int) -> dict[str, str]:
    finder = (
        f"findReqs;siteNumber={SITE},limit={PAGE_SIZE},offset={offset},"
        f"selectedTitlesFacet={FACET},sortBy=POSTING_DATES_DESC"
    )
    return {
        "onlyData": "true",
        "expand": "requisitionList.secondaryLocations",
        "finder": finder,
    }


def _total(payload: dict[str, Any]) -> int:
    return payload["items"][0].get("TotalJobsCount") or 0


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    jobs = []
    for item in payload["items"][0]["requisitionList"]:
        secondary = [place.get("Name") for place in item.get("secondaryLocations") or []]
        jobs.append(
            Job(
                source=SOURCE,
                company=company.key,
                job_id=str(item["Id"]),
                title=item["Title"].strip(),
                url=JOB_URL.format(site=SITE, job_id=item["Id"]),
                locations=clean([item.get("PrimaryLocation"), *secondary]),
                department=item.get("JobFamily") or None,
                posted_at=parse_iso_date(item.get("PostedDate")),
            )
        )
    return jobs
