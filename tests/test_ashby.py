"""Ashby adapter, tested against responses recorded from the live API."""

import copy
from datetime import date

import httpx

from conftest import load_fixture, make_company, mock_client
from jobtracker.sources import ashby

OPENAI = make_company("openai", ats="ashby")


def test_parse_maps_every_field():
    jobs = ashby.parse(OPENAI, load_fixture("ashby/openai.json"))
    researcher = next(job for job in jobs if job.title == "Researcher, Safety Training")

    assert researcher.source == "ashby"
    assert researcher.company == "openai"
    assert researcher.job_id == "2560ed50-5535-42b8-b069-9ebc28ce7493"
    assert researcher.url == "https://jobs.ashbyhq.com/openai/2560ed50-5535-42b8-b069-9ebc28ce7493"
    assert researcher.department == "Research / Safety Systems"
    assert researcher.posted_at == date(2023, 5, 25)
    assert researcher.remote_hint is None  # no workplaceType on this posting
    assert len(jobs) == 2


def test_parse_includes_secondary_locations_and_adds_missing_countries():
    jobs = ashby.parse(OPENAI, load_fixture("ashby/openai.json"))
    researcher = next(job for job in jobs if job.title == "Researcher, Safety Training")
    # "San Francisco" gains its country; "London, UK" gains "United Kingdom".
    assert researcher.locations == ("San Francisco, United States", "London, UK, United Kingdom")


def test_parse_reads_workplace_type_for_the_remote_flag():
    jobs = ashby.parse(OPENAI, load_fixture("ashby/openai.json"))
    remote = next(job for job in jobs if job.locations[0].startswith("US - Remote"))
    assert remote.remote_hint is True
    assert remote.department == "Security"  # department and team are the same: not repeated


def test_parse_does_not_trust_is_remote():
    payload = copy.deepcopy(load_fixture("ashby/openai.json"))
    payload["jobs"][0]["isRemote"] = True
    payload["jobs"][0]["workplaceType"] = "Hybrid"
    assert ashby.parse(OPENAI, payload)[0].remote_hint is False


def test_parse_skips_unlisted_postings():
    payload = copy.deepcopy(load_fixture("ashby/openai.json"))
    payload["jobs"][0]["isListed"] = False
    assert len(ashby.parse(OPENAI, payload)) == 1


def test_fetch_requests_the_board_url(settings):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=load_fixture("ashby/openai.json"))

    jobs = ashby.fetch(OPENAI, mock_client(settings, handler))

    assert requested == ["https://api.ashbyhq.com/posting-api/job-board/openai"]
    assert len(jobs) == 2
