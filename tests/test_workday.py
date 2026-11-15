"""Workday adapter, tested against responses recorded from live tenants."""

import json

import httpx
import pytest

from conftest import load_fixture, make_company, mock_client
from jobtracker.http import RobotsDisallowedError
from jobtracker.sources import workday

LLOYDS = make_company(
    "lloyds-banking-group",
    ats="workday",
    tenant="lbg",
    host="wd3",
    sites=("Undergraduate_careers",),
)
NVIDIA = make_company(
    "nvidia", ats="workday", tenant="nvidia", host="wd5", sites=("NVIDIAExternalCareerSite",)
)


def posting(number: int, title: str = "Spring Week") -> dict:
    return {
        "title": f"{title} {number}",
        "externalPath": f"/job/London/{title.replace(' ', '-')}-{number}_R{number}",
        "locationsText": "London",
        "postedOn": "Posted Today",
    }


def page(numbers, total: int = 0) -> dict:
    return {"total": total, "jobPostings": [posting(number) for number in numbers]}


def workday_handler(respond):
    """A fake tenant: `respond(site, search, offset)` returns one page of results."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        site = request.url.path.split("/")[-2]
        assert request.method == "POST"
        assert body["limit"] == 20 and body["appliedFacets"] == {}
        calls.append((site, body["searchText"], body["offset"]))
        return httpx.Response(200, json=respond(site, body["searchText"], body["offset"]))

    return handler, calls


def test_parse_maps_every_field():
    payload = load_fixture("workday/lbg_undergraduate.json")
    jobs = workday.parse(LLOYDS, payload, "Undergraduate_careers")
    placement = jobs[2]

    assert placement.source == "workday"
    assert placement.company == "lloyds-banking-group"
    assert placement.job_id == "163439-1"  # the id at the end of the path
    assert placement.title == "Data Science and AI Industrial Placement (Edinburgh)"
    assert placement.url == (
        "https://lbg.wd3.myworkdayjobs.com/Undergraduate_careers/job/Edinburgh/"
        "Data-Science-and-AI-Industrial-Placement--Edinburgh-_163439-1"
    )
    assert placement.locations == ("Edinburgh",)
    assert placement.posted_at is None  # only "Posted 9 Days Ago" is published
    assert placement.key == "workday:lloyds-banking-group:163439-1"
    assert len(jobs) == 4
    # A posting in several places gives a count, not the places.
    assert jobs[0].locations == ("4 Locations",)


def test_parse_tolerates_missing_location_and_skips_rows_without_a_posting():
    payload = {
        "jobPostings": [
            {"title": " Spring Week ", "externalPath": "/job/Spring-Week_JR1"},
            {"title": "No path"},
            {"externalPath": "/job/No-title_JR2"},
        ]
    }
    (job,) = workday.parse(NVIDIA, payload, "NVIDIAExternalCareerSite")
    assert (job.job_id, job.title, job.locations) == ("JR1", "Spring Week", ())


def test_parse_empty_site():
    assert workday.parse(LLOYDS, {"total": 0, "jobPostings": []}, "Undergraduate_careers") == []


def test_parse_rejects_an_unexpected_shape():
    with pytest.raises(KeyError):
        workday.parse(LLOYDS, {"error": "moved"}, "Undergraduate_careers")


def test_fetch_reads_a_small_site_in_full(settings):
    """45 postings: three pages of 20, with the total given on the first page only."""

    def respond(site, search, offset):
        return page(range(offset, min(offset + 20, 45)), total=45 if offset == 0 else 0)

    handler, calls = workday_handler(respond)
    jobs = workday.fetch(LLOYDS, mock_client(settings, handler))

    assert calls == [
        ("Undergraduate_careers", "", 0),
        ("Undergraduate_careers", "", 20),
        ("Undergraduate_careers", "", 40),
    ]
    assert len(jobs) == 45


def test_fetch_posts_to_the_sites_jobs_endpoint(settings):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200, json=load_fixture("workday/lbg_undergraduate.json") | {"total": 4}
        )

    workday.fetch(LLOYDS, mock_client(settings, handler))
    assert requested == [
        "https://lbg.wd3.myworkdayjobs.com/wday/cxs/lbg/Undergraduate_careers/jobs"
    ]


def test_fetch_searches_a_large_site_instead_of_paging_through_it(settings):
    """NVIDIA reports 2000 postings. Reading them all would be 100 requests per run."""
    recorded = {
        "": load_fixture("workday/nvidia_all.json"),  # total 2000
        "spring": load_fixture("workday/nvidia_search_spring.json"),  # total 3
        "intern": load_fixture("workday/nvidia_search_intern.json"),  # total 1085
    }

    def respond(site, search, offset):
        if search in recorded and offset == 0:
            return recorded[search]
        return {"total": 0, "jobPostings": []}

    handler, calls = workday_handler(respond)
    jobs = workday.fetch(NVIDIA, mock_client(settings, handler))

    searches = [search for _, search, offset in calls if offset == 0]
    assert searches == ["", *workday.SEARCH_TERMS]
    # "intern" matched 1085: it is read to the page cap and no further.
    assert [offset for _, search, offset in calls if search == "intern"] == [0, 20]
    assert [offset for _, search, offset in calls if search == "spring"] == [0]
    assert len(calls) == 1 + len(workday.SEARCH_TERMS) + 1

    titles = {job.title for job in jobs}
    assert "PCIe Design Verification Intern - Spring 2027" in titles
    assert len({job.job_id for job in jobs}) == len(jobs)  # found by two searches, kept once


def test_fetch_keeps_a_posting_listed_on_two_sites_once(settings):
    company = make_company(
        "acme", ats="workday", tenant="acme", host="wd1", sites=("Campus", "External")
    )
    handler, calls = workday_handler(lambda site, search, offset: page([1, 2], total=2))
    jobs = workday.fetch(company, mock_client(settings, handler))

    assert [site for site, _, _ in calls] == ["Campus", "External"]
    assert [job.job_id for job in jobs] == ["R1", "R2"]
    assert all("/Campus/job/" in job.url for job in jobs)  # the first site's link is kept


def test_fetch_does_not_call_a_site_its_robots_txt_disallows(settings):
    """The JSON endpoint is not named in robots.txt; the site it serves is."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the endpoint must not be called")

    robots_txt = "User-agent: *\nAllow: /Campus/\nDisallow: /Undergraduate_careers/\n"
    http = mock_client(settings, handler, robots_txt=robots_txt)
    with pytest.raises(RobotsDisallowedError, match="Undergraduate_careers"):
        workday.fetch(LLOYDS, http)
