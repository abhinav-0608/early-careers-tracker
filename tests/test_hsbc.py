"""HSBC's early-careers job search (custom/hsbc.py).

The fixture is the first results page recorded from the live site on 2026-11-16,
trimmed to three rows (two UK insight programmes and one graduate role in Dubai).
"""

from datetime import date

import httpx
import pytest

from conftest import load_text, make_company, mock_client
from jobtracker import robots
from jobtracker.sources import adapter_for
from jobtracker.sources.custom import hsbc

HSBC = make_company("hsbc", name="HSBC", industry="finance", ats="custom", board="hsbc")
PAGE = load_text("custom/hsbc_search.html")


def test_hsbc_is_registered():
    assert adapter_for(HSBC) is hsbc.fetch


def test_parse_maps_every_field():
    jobs = hsbc.parse(HSBC, PAGE)
    assert [job.title for job in jobs] == [
        "Women in Banking - Insight Programme",
        "Sheffield Tech Insight Programme",
        "Investment Banking - Graduate",
    ]
    banking = jobs[0]
    assert banking.key == "custom:hsbc:1376880057"
    assert banking.url == (
        "https://apply.careers.hsbc.com/emergingtalent/job/"
        "Birmingham-Women-in-Banking-Insight-Programme-B1-1HQ/1376880057/"
    )
    assert banking.locations == ("Birmingham, GB, B1 1HQ",)
    assert banking.department == "Relationship Management"
    assert banking.posted_at == date(2026, 11, 6)
    assert hsbc.total(PAGE) == 37


def test_a_page_that_is_not_a_results_page_is_an_error_not_an_empty_board():
    with pytest.raises(ValueError, match="neither results nor a result count"):
        hsbc.parse(HSBC, "<html><body>Maintenance</body></html>")


def test_fetch_reads_every_page_newest_first(settings):
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "apply.careers.hsbc.com"
        assert request.url.path == "/emergingtalent/search/"
        queries.append(request.url.params)
        start = int(request.url.params["startrow"])
        tiles = "".join(
            f'<li class="job-tile job-id-{n}" data-url="/emergingtalent/job/x/{n}/">'
            f'<a class="jobTitle-link" href="/emergingtalent/job/x/{n}/">Insight {n}</a></li>'
            for n in range(start, min(start + 25, 37))
        )
        body = f"<p>Showing {start + 1} to {min(start + 25, 37)} of 37 Jobs</p><ul>{tiles}</ul>"
        return httpx.Response(200, text=body)

    robots_txt = load_text("robots/hsbc-careers.txt")
    jobs = hsbc.fetch(HSBC, mock_client(settings, handler, robots_txt=robots_txt))
    assert len(jobs) == 37
    assert [query["startrow"] for query in queries] == ["0", "25"]
    assert queries[0]["sortColumn"] == "referencedate"


def test_robots_txt_allows_the_search_and_postings_but_not_services_or_apply():
    rules = robots.parse(load_text("robots/hsbc-careers.txt"), "job-alert-tracker")
    assert rules.allows("/emergingtalent/search/?q=&startrow=25")
    assert rules.allows("/emergingtalent/job/Birmingham-Women-in-Banking/1376880057/")
    assert not rules.allows("/services/t/l")
    assert not rules.allows("/applybutton/x")
