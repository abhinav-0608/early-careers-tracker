"""Greenhouse adapter, tested against responses recorded from the live API."""

from datetime import date

import httpx

from conftest import load_fixture, make_company, mock_client
from jobtracker.sources import greenhouse

POINT72 = make_company("point72")


def test_parse_maps_every_field():
    jobs = greenhouse.parse(POINT72, load_fixture("greenhouse/point72.json"))
    spring = next(job for job in jobs if job.job_id == "8845395002")

    assert spring.source == "greenhouse"
    assert spring.company == "point72"
    assert spring.title == "2027 Point72 Academy Spring Insight Programme – UK"
    assert spring.locations == ("London, UK",)
    assert spring.url.startswith("https://")
    assert spring.posted_at == date(2026, 9, 29)
    assert spring.department is None
    assert spring.remote_hint is None
    assert spring.key == "greenhouse:point72:8845395002"
    assert len(jobs) == 3


def test_parse_splits_locations_packed_into_one_string():
    jobs = greenhouse.parse(make_company("anthropic"), load_fixture("greenhouse/anthropic.json"))
    by_title = {job.title: job.locations for job in jobs}

    assert by_title["Anthropic Fellows Program, AI Safety & Security"] == (
        "London, UK",
        "Ontario, CAN",
        "Remote-Friendly, United States",
        "San Francisco, CA",
    )
    assert by_title["Applied AI Architect, International Policy"] == (
        "San Francisco, CA",
        "New York City, NY",
    )


def test_parse_tolerates_missing_location_and_date():
    payload = {"jobs": [{"id": 1, "title": " Spring Week ", "absolute_url": "https://x/1"}]}
    (job,) = greenhouse.parse(POINT72, payload)
    assert job.title == "Spring Week"
    assert job.locations == ()
    assert job.posted_at is None


def test_parse_empty_board():
    assert greenhouse.parse(POINT72, {"jobs": [], "meta": {"total": 0}}) == []


def test_fetch_requests_the_board_url(settings):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=load_fixture("greenhouse/point72.json"))

    jobs = greenhouse.fetch(POINT72, mock_client(settings, handler))

    assert requested == ["https://boards-api.greenhouse.io/v1/boards/point72/jobs"]
    assert len(jobs) == 3
