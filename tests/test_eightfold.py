"""The generic Eightfold adapter (Qualcomm; Microsoft is the same system, custom/).

The fixture is the first page of Qualcomm's intern-level search, recorded from the live
API on 2026-11-16 (the unused "debug" block removed).
"""

from datetime import date

import httpx
import pytest

from conftest import load_fixture, load_text, make_company, mock_client
from jobtracker import robots
from jobtracker.sources import adapter_for, eightfold

QUALCOMM = make_company(
    "qualcomm", name="Qualcomm", ats="eightfold", host="qualcomm.eightfold.ai", board="qualcomm.com"
)


def test_eightfold_is_registered():
    assert adapter_for(QUALCOMM) is eightfold.fetch


def test_parse_maps_every_field():
    jobs = eightfold.parse(QUALCOMM, load_fixture("eightfold/qualcomm_interns.json"))
    assert len(jobs) == 10
    gpu = jobs[0]
    assert gpu.key == "eightfold:qualcomm:446722152489"
    assert gpu.title == "FY27 Intern -  GPU Compiler - 3 months, Cambridge"
    assert gpu.url == "https://qualcomm.eightfold.ai/careers/job/446722152489"
    assert gpu.locations == ("Cambridge, United Kingdom",)
    assert gpu.department == "Interim Intern"
    assert gpu.posted_at == date(2026, 11, 16)
    assert gpu.remote_hint is False  # "onsite"


def test_parse_rejects_an_error_answer():
    with pytest.raises(ValueError, match="Qualcomm careers search answered status 500"):
        eightfold.parse(QUALCOMM, {"status": 500})


def test_fetch_pages_through_the_intern_level_on_the_company_host(settings):
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "qualcomm.eightfold.ai"
        assert request.url.path == "/api/pcsx/search"
        queries.append(request.url.params)
        start = int(request.url.params["start"])
        positions = [
            {"id": number, "name": f"Intern {number}", "positionUrl": f"/careers/job/{number}"}
            for number in range(start, min(start + 10, 24))
        ]
        return httpx.Response(
            200, json={"status": 200, "data": {"positions": positions, "count": 24}}
        )

    robots_txt = load_text("robots/eightfold-qualcomm.txt")
    jobs = eightfold.fetch(QUALCOMM, mock_client(settings, handler, robots_txt=robots_txt))

    assert len(jobs) == 24
    assert [query["start"] for query in queries] == ["0", "10", "20"]
    assert {query["domain"] for query in queries} == {"qualcomm.com"}
    assert queries[0]["filter_seniority"] == "Intern"


def test_robots_txt_allows_the_search_api_and_job_pages_and_nothing_else():
    rules = robots.parse(load_text("robots/eightfold-qualcomm.txt"), "job-alert-tracker")
    assert rules.allows("/api/pcsx/search?domain=qualcomm.com&start=0")
    assert rules.allows("/careers/job/446722152489")
    assert not rules.allows("/api/other/thing")
    assert not rules.allows("/admin")


def test_a_tenant_that_switched_the_api_off_is_not_fetched_another_way(settings):
    """Netflix and Millennium answer 403: the adapter makes one request and stops."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(403)

    netflix = make_company(
        "netflix", ats="eightfold", host="netflix.eightfold.ai", board="netflix.com"
    )
    with pytest.raises(httpx.HTTPStatusError):
        eightfold.fetch(
            netflix,
            mock_client(settings, handler, robots_txt=load_text("robots/eightfold-qualcomm.txt")),
        )
    assert requests == ["/api/pcsx/search"]
