"""Lever adapter, tested against responses recorded from the live API."""

from datetime import date

import httpx

from conftest import load_fixture, make_company
from jobtracker.http import HttpClient
from jobtracker.sources import lever

PALANTIR = make_company("palantir", ats="lever")


def test_parse_maps_every_field():
    jobs = lever.parse(PALANTIR, load_fixture("lever/palantir.json"))
    intern = next(job for job in jobs if job.title == "Software Engineer, Internship")

    assert intern.source == "lever"
    assert intern.company == "palantir"
    assert intern.job_id == "76a60923-bb49-40f5-b061-7c7eb1299602"
    assert intern.locations == ("London, United Kingdom",)
    assert intern.url == "https://jobs.lever.co/palantir/76a60923-bb49-40f5-b061-7c7eb1299602"
    assert intern.department == "Dev"
    assert intern.posted_at == date(2021, 7, 1)  # createdAt is epoch milliseconds
    assert intern.remote_hint is False  # workplaceType: onsite
    assert len(jobs) == 3


def test_parse_reads_the_remote_flag():
    jobs = lever.parse(PALANTIR, load_fixture("lever/palantir.json"))
    fellowship = next(job for job in jobs if job.title == "American Tech Fellowship")
    assert fellowship.remote_hint is True
    assert fellowship.locations == ("North America",)


def test_parse_handles_sparse_postings():
    payload = [{"id": "abc", "text": "Spring Week", "hostedUrl": "https://x/abc"}]
    (job,) = lever.parse(PALANTIR, payload)
    assert job.locations == ()
    assert job.department is None
    assert job.posted_at is None
    assert job.remote_hint is None


def test_parse_falls_back_to_single_location_and_joins_department_and_team():
    payload = [
        {
            "id": "abc",
            "text": "Spring Week",
            "hostedUrl": "https://x/abc",
            "categories": {"location": "London", "department": "Engineering", "team": "Infra"},
        }
    ]
    (job,) = lever.parse(PALANTIR, payload)
    assert job.locations == ("London",)
    assert job.department == "Engineering / Infra"


def test_fetch_asks_for_json(settings):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=load_fixture("lever/palantir.json"))

    jobs = lever.fetch(PALANTIR, HttpClient(settings, transport=httpx.MockTransport(handler)))

    assert requested == ["https://api.lever.co/v0/postings/palantir?mode=json"]
    assert len(jobs) == 3
