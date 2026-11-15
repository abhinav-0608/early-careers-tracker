"""Lever job boards.

API: GET https://api.lever.co/v0/postings/{board}?mode=json
Public and unauthenticated; returns the whole board as a JSON list.
"""

from __future__ import annotations

from typing import Any

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean, parse_epoch_ms

NAME = "lever"
URL = "https://api.lever.co/v0/postings/{board}"

_REMOTE_HINTS = {"remote": True, "hybrid": False, "onsite": False, "on-site": False}


def fetch(company: Company, http: HttpClient) -> list[Job]:
    payload = http.get_json(URL.format(board=company.board), params={"mode": "json"})
    return parse(company, payload)


def parse(company: Company, payload: list[dict[str, Any]]) -> list[Job]:
    jobs = []
    for item in payload:
        categories = item.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location")]
        department = clean([categories.get("department"), categories.get("team")])
        jobs.append(
            Job(
                source=NAME,
                company=company.key,
                job_id=str(item["id"]),
                title=item["text"].strip(),
                url=item["hostedUrl"],
                locations=clean(locations),
                department=" / ".join(department) or None,
                posted_at=parse_epoch_ms(item.get("createdAt")),
                remote_hint=_REMOTE_HINTS.get((item.get("workplaceType") or "").lower()),
            )
        )
    return jobs
