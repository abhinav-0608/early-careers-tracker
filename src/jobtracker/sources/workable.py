"""Workable job boards.

API: GET https://apply.workable.com/api/v1/widget/accounts/{board}
Public and unauthenticated; this is the endpoint behind the careers widget a company
embeds on its own site. It returns the whole board in one response.

**Not yet checked against the live API.** apply.workable.com rate-limits by IP address,
and the address this adapter was written from had been limited since stage 2 (HTTP 429
on every path, robots.txt included, with a Retry-After of several hours). The field
names below are those of Workable's documented widget response, and the test fixture
is written by hand to that shape rather than recorded. Until a first live poll has
confirmed it, treat a Workable parsing error as "the shape is wrong", not as an outage.

Because of that rate limit the handful of Workable boards are polled gently: a longer
gap between requests to this host and a polling interval, both in settings.yaml. When
the host answers 429 for its robots.txt the client fetches nothing from it that run.
"""

from __future__ import annotations

from typing import Any

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.common import clean, parse_iso_date

NAME = "workable"
URL = "https://apply.workable.com/api/v1/widget/accounts/{board}"
JOB_URL = "https://apply.workable.com/j/{shortcode}"


def fetch(company: Company, http: HttpClient) -> list[Job]:
    payload = http.get_json(URL.format(board=company.board))
    return parse(company, payload)


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    jobs = []
    for item in payload["jobs"]:
        # A job in several places lists them under "locations"; the top-level city,
        # state and country are the first of them, and all an older board has.
        places = item.get("locations") or [item]
        telecommuting = item.get("telecommuting")
        jobs.append(
            Job(
                source=NAME,
                company=company.key,
                job_id=str(item["shortcode"]),
                title=item["title"].strip(),
                url=item.get("url") or JOB_URL.format(shortcode=item["shortcode"]),
                locations=clean([_place(place) for place in places]),
                department=item.get("department") or None,
                posted_at=parse_iso_date(item.get("published_on") or item.get("created_at")),
                remote_hint=telecommuting if isinstance(telecommuting, bool) else None,
            )
        )
    return jobs


def _place(place: dict[str, Any]) -> str:
    """ "London, England, United Kingdom" from whichever parts are given."""
    region = place.get("region") or place.get("state")
    return ", ".join(clean([place.get("city"), region, place.get("country")]))
