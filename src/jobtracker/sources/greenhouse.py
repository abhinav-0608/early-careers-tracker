"""Greenhouse job boards.

API: GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs
Public and unauthenticated; returns the whole board in one response.

We deliberately do not pass ?content=true. It adds departments, but also every job
description, making the response roughly 20x larger on every poll. Greenhouse jobs
therefore have no department and are classified from the title alone.
"""

from __future__ import annotations

import re
from typing import Any

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean, parse_iso_date

NAME = "greenhouse"
URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"

# Greenhouse has one free-text location per job. Companies with several offices pack
# them in as "London, UK; Dublin, IE" or "San Francisco, CA | New York City, NY".
_LOCATION_SEPARATORS = re.compile(r"[;|]")


def fetch(company: Company, http: HttpClient) -> list[Job]:
    payload = http.get_json(URL.format(board=company.board))
    return parse(company, payload)


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    jobs = []
    for item in payload["jobs"]:
        location = (item.get("location") or {}).get("name") or ""
        jobs.append(
            Job(
                source=NAME,
                company=company.key,
                job_id=str(item["id"]),
                title=item["title"].strip(),
                url=item["absolute_url"],
                locations=clean(_LOCATION_SEPARATORS.split(location)),
                posted_at=parse_iso_date(item.get("first_published") or item.get("updated_at")),
            )
        )
    return jobs
