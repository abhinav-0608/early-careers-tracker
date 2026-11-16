"""Ashby job boards.

API: GET https://api.ashbyhq.com/posting-api/job-board/{board}
Public and unauthenticated; returns the whole board in one response.
"""

from __future__ import annotations

from typing import Any

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean, parse_iso_date

NAME = "ashby"
URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"

# We read workplaceType, not isRemote. Live data shows isRemote set to true on hundreds
# of jobs whose workplaceType is "Hybrid", so it cannot be trusted to mean fully remote.
_REMOTE_HINTS = {"remote": True, "hybrid": False, "onsite": False}


def fetch(company: Company, http: HttpClient) -> list[Job]:
    payload = http.get_json(URL.format(board=company.board))
    return parse(company, payload)


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    jobs = []
    for item in payload["jobs"]:
        if item.get("isListed") is False:
            continue  # unlisted postings are reachable by direct link only
        locations = [_with_country(item.get("location"), item.get("address"))]
        for secondary in item.get("secondaryLocations") or []:
            locations.append(_with_country(secondary.get("location"), secondary.get("address")))
        department = clean([item.get("department"), item.get("team")])
        jobs.append(
            Job(
                source=NAME,
                company=company.key,
                job_id=str(item["id"]),
                title=item["title"].strip(),
                url=item["jobUrl"],
                locations=clean(locations),
                department=" / ".join(department) or None,
                posted_at=parse_iso_date(item.get("publishedAt")),
                remote_hint=_REMOTE_HINTS.get((item.get("workplaceType") or "").lower()),
                description=item.get("descriptionPlain") or item.get("descriptionHtml") or None,
            )
        )
    return jobs


def _with_country(name: str | None, address: dict[str, Any] | None) -> str | None:
    """Append the country from the structured address when the name omits it.

    Ashby location names are often just a city ("London"); the country removes any
    doubt about which London.
    """
    country = ((address or {}).get("postalAddress") or {}).get("addressCountry")
    if not name or not country or country.lower() in name.lower():
        return name
    return f"{name}, {country}"
