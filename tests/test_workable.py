"""Workable adapter.

The fixture is written by hand to the shape of Workable's documented widget response,
not recorded: apply.workable.com was rate-limiting when the adapter was built.
"""

from dataclasses import replace
from datetime import date

import httpx
import pytest

from conftest import load_fixture, make_company, mock_client
from jobtracker.http import HttpClient, RateLimitedError
from jobtracker.sources import adapter_for, workable

HUGGING_FACE = make_company("hugging-face", ats="workable", board="huggingface")
URL = "https://apply.workable.com/api/v1/widget/accounts/huggingface"


def test_workable_companies_use_the_adapter():
    assert adapter_for(HUGGING_FACE) is workable.fetch


def test_parse_maps_every_field():
    jobs = workable.parse(HUGGING_FACE, load_fixture("workable/board.json"))
    intern = jobs[0]

    assert intern.source == "workable"
    assert intern.company == "hugging-face"
    assert intern.job_id == "A1B2C3D4E5"
    assert intern.title == "Software Engineering Intern (Summer 2027)"
    assert intern.url == "https://apply.workable.com/j/A1B2C3D4E5"
    assert intern.locations == ("London, England, United Kingdom", "Paris, Île-de-France, France")
    assert intern.department == "Engineering"
    assert intern.posted_at == date(2026, 11, 12)
    assert intern.remote_hint is False
    assert intern.key == "workable:hugging-face:A1B2C3D4E5"
    assert len(jobs) == 2


def test_parse_reads_the_recorded_live_board():
    """Hugging Face's board as apply.workable.com returned it on 2026-11-16."""
    jobs = workable.parse(HUGGING_FACE, load_fixture("workable/huggingface.json"))

    assert len(jobs) == 6
    first = jobs[0]
    assert first.job_id == "81B46579FE"
    assert first.title == "Open-Source Machine Learning Engineer - EMEA Remote"
    assert first.url == "https://apply.workable.com/j/81B46579FE"
    assert first.locations == ("Paris, Île-de-France, France",)
    assert first.department == "Open Source"
    assert first.posted_at == date(2026, 5, 29)
    assert first.remote_hint is True
    # A location with no city is just the country; one marked hidden is still read.
    assert jobs[3].locations == ("France",)
    assert jobs[2].locations == ("Paris, Île-de-France, France",)
    assert len({job.key for job in jobs}) == 6


def test_parse_falls_back_to_the_single_location_and_reads_the_remote_flag():
    remote = workable.parse(HUGGING_FACE, load_fixture("workable/board.json"))[1]
    assert remote.locations == ("United States",)  # no "locations" list, no city
    assert remote.department is None
    assert remote.remote_hint is True


def test_parse_tolerates_missing_optional_fields():
    payload = {"jobs": [{"shortcode": "ABC", "title": " Spring Week "}]}
    (job,) = workable.parse(HUGGING_FACE, payload)
    assert job.title == "Spring Week"
    assert job.url == "https://apply.workable.com/j/ABC"
    assert job.locations == ()
    assert job.posted_at is None
    assert job.remote_hint is None


def test_parse_empty_and_malformed():
    assert workable.parse(HUGGING_FACE, {"name": "Hugging Face", "jobs": []}) == []
    with pytest.raises(KeyError):
        workable.parse(HUGGING_FACE, {"error": "Not found"})
    with pytest.raises(KeyError):
        workable.parse(HUGGING_FACE, {"jobs": [{"title": "No shortcode"}]})


def test_fetch_requests_the_board_once(settings):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=load_fixture("workable/board.json"))

    jobs = workable.fetch(HUGGING_FACE, mock_client(settings, handler))

    assert requested == [URL]
    assert len(jobs) == 2


def test_a_rate_limited_workable_is_left_alone_for_the_run(settings):
    """What the live host did while this was written: 429 on every path, robots.txt
    included, asking for hours. Nothing is retried and no board is requested."""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "25028"})

    sleeps = []
    transport = httpx.MockTransport(handler)
    http = HttpClient(replace(settings, retries=2), transport=transport, sleep=sleeps.append)
    for _ in range(2):  # two boards in the same run
        with pytest.raises(RateLimitedError, match="HTTP 429"):
            workable.fetch(HUGGING_FACE, http)
    assert requested == ["/robots.txt"]
    assert sleeps == []
    # ...and the hours it asked for are remembered for the runs that follow.
    assert http.hosts_state().backoff["apply.workable.com"]["strikes"] == 1


def test_workable_boards_are_spaced_by_the_gap_configured_for_the_host(config):
    """The shipped settings give apply.workable.com a longer gap than other hosts."""
    sleeps = []
    http = mock_client(
        config.settings,
        lambda request: httpx.Response(200, json={"jobs": []}),
        sleep=sleeps.append,
        clock=lambda: 100.0,  # a frozen clock: no time passes between requests
    )
    workable.fetch(HUGGING_FACE, http)
    workable.fetch(HUGGING_FACE, http)
    assert sleeps == [5.0, 5.0]  # after robots.txt, and between the two boards
    assert config.settings.poll_interval("workable") == 60
