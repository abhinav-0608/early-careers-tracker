"""SmartRecruiters adapter: the public job board, not the postings API.

Fixtures are trimmed copies of pages recorded from careers.smartrecruiters.com on
2026-11-15 (a few location groups kept, icon paths cut).
"""

import httpx
import pytest

from conftest import load_text, make_company, mock_client
from jobtracker.http import HttpClient, RobotsDisallowedError
from jobtracker.sources import adapter_for, smartrecruiters

CANVA = make_company("canva", ats="smartrecruiters", board="Canva")
SERVICENOW = make_company("servicenow", ats="smartrecruiters", board="ServiceNow")
HOST = "https://careers.smartrecruiters.com"


def test_smartrecruiters_companies_use_the_adapter():
    assert adapter_for(CANVA) is smartrecruiters.fetch


def test_parse_maps_every_field():
    jobs = smartrecruiters.parse(CANVA, load_text("smartrecruiters/canva_board.html"))
    security = jobs[0]

    assert security.source == "smartrecruiters"
    assert security.company == "canva"
    assert security.job_id == "6000000001554828"
    assert security.title == "Enterprise Security Manager"  # the board has a trailing space
    assert security.url == (
        "https://jobs.smartrecruiters.com/Canva/6000000001554828-enterprise-security-manager-"
    )
    assert security.locations == ("Auckland, New Zealand",)
    assert security.department is None
    assert security.posted_at is None
    assert security.remote_hint is True  # "Employees can work remotely"
    assert security.key == "smartrecruiters:canva:6000000001554828"

    london = jobs[1]
    assert london.title == "SMB Account Executive - UKI"
    assert london.locations == ("London, United Kingdom",)
    assert london.remote_hint is None
    assert len(jobs) == 6  # one in Auckland, five in London


def test_parse_board_reads_the_groups_and_how_many_pages_of_them_there_are():
    groups, pages = smartrecruiters.parse_board(
        SERVICENOW, load_text("smartrecruiters/servicenow_board.html")
    )
    assert pages == 1
    albany, amsterdam = groups
    assert (albany.location, albany.total, len(albany.jobs)) == ("Albany, New York", 1, 1)
    assert albany.more_value is None
    # Twelve jobs in Amsterdam, nine of them on the page, and a link to the rest.
    assert (amsterdam.location, amsterdam.total, len(amsterdam.jobs)) == (
        "Amsterdam, Netherlands",
        12,
        9,
    )
    assert (amsterdam.more_type, amsterdam.more_value) == ("location", "Amsterdam, NL")


def test_parse_groups_and_parse_jobs_read_the_paging_responses():
    groups = smartrecruiters.parse_groups(
        CANVA, load_text("smartrecruiters/canva_groups_page1.html")
    )
    assert [(group.location, group.total, len(group.jobs)) for group in groups] == [
        ("Munich, Germany", 5, 5),
        ("Paris, France", 1, 1),
    ]
    more = smartrecruiters.parse_jobs(
        SERVICENOW,
        load_text("smartrecruiters/servicenow_more_page1.html"),
        "Amsterdam, Netherlands",
    )
    assert [job.job_id for job in more] == ["744000156841379", "744000156764064", "744000128763709"]
    assert {job.locations for job in more} == {("Amsterdam, Netherlands",)}


def test_parse_an_empty_board():
    page = '<html><body><div data-groups-pages="0" class="js-openings-load"></div></body></html>'
    assert smartrecruiters.parse(CANVA, page) == []


@pytest.mark.parametrize(
    "page",
    [
        # What comes back when a company points its board at its own careers site.
        "<html><head><title>MAKE A WISE MOVE</title></head><body>Careers at Wise</body></html>",
        "<html><body>Sign in</body></html>",
    ],
)
def test_a_page_that_is_not_a_board_is_an_error_not_an_empty_board(page):
    with pytest.raises(ValueError, match="no job list"):
        smartrecruiters.parse(CANVA, page)


def test_a_job_whose_link_has_no_posting_id_is_an_error():
    page = (
        '<div data-groups-pages="1"><section data-qty="1" class="js-group">'
        '<h3 class="opening-title">London</h3>'
        '<a class="js-job-ad-link" href="https://jobs.smartrecruiters.com/Canva/about-us">'
        '<h4 class="job-title">Mystery</h4></a></section></div>'
    )
    with pytest.raises(ValueError, match="unexpected shape"):
        smartrecruiters.parse(CANVA, page)


def _board_site(requested):
    """careers.smartrecruiters.com serving the fixtures, as the live site does."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        requested.append(url.path + (f"?{url.query.decode()}" if url.query else ""))
        if url.path == "/Canva":
            return httpx.Response(200, text=load_text("smartrecruiters/canva_board.html"))
        if url.path == "/Canva/api/groups":
            return httpx.Response(200, text=load_text("smartrecruiters/canva_groups_page1.html"))
        if url.path == "/ServiceNow":
            return httpx.Response(200, text=load_text("smartrecruiters/servicenow_board.html"))
        if url.path == "/ServiceNow/api/more" and url.params["page"] == "1":
            return httpx.Response(200, text=load_text("smartrecruiters/servicenow_more_page1.html"))
        return httpx.Response(200, text="")

    return handler


def test_fetch_reads_every_page_of_groups(settings):
    requested = []
    jobs = smartrecruiters.fetch(CANVA, mock_client(settings, _board_site(requested)))

    assert requested == ["/Canva", "/Canva/api/groups?page=1"]  # the board says two pages
    assert len(jobs) == 12
    assert {job.locations[0] for job in jobs} == {
        "Auckland, New Zealand",
        "London, United Kingdom",
        "Munich, Germany",
        "Paris, France",
    }


def test_fetch_follows_show_more_until_a_long_group_is_complete(settings):
    requested = []
    jobs = smartrecruiters.fetch(SERVICENOW, mock_client(settings, _board_site(requested)))

    # Page 0 of "more" repeats the nine jobs already on the board, so it is never asked for.
    assert requested[0] == "/ServiceNow"
    assert len(requested) == 2
    more = httpx.URL(HOST + requested[1])
    assert more.path == "/ServiceNow/api/more"
    assert dict(more.params) == {
        "search": "",
        "type": "location",
        "value": "Amsterdam, NL",
        "page": "1",
    }
    assert len(jobs) == 13  # one in Albany, nine plus three in Amsterdam


def test_fetch_merges_a_job_listed_under_two_locations(settings):
    def group(location):
        return (
            f'<section data-qty="1" class="js-group"><h3 class="opening-title">{location}</h3>'
            '<a class="js-job-ad-link" href="https://jobs.smartrecruiters.com/Canva/77-intern">'
            '<h4 class="job-title">Spring Intern</h4></a></section>'
        )

    groups = group("London, United Kingdom") + group("Dublin, Ireland")
    page = f'<div data-groups-pages="1">{groups}</div>'
    http = mock_client(settings, lambda request: httpx.Response(200, text=page))
    (job,) = smartrecruiters.fetch(CANVA, http)
    assert job.locations == ("London, United Kingdom", "Dublin, Ireland")


def test_fetch_stops_at_its_request_budget_and_says_so(settings, caplog, monkeypatch):
    caplog.set_level("INFO", logger="jobtracker")
    monkeypatch.setattr(smartrecruiters, "MAX_REQUESTS", 3)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text='<div data-groups-pages="50"></div>')

    assert smartrecruiters.fetch(CANVA, mock_client(settings, handler)) == []
    assert len(calls) == 3
    assert [r.getMessage() for r in caplog.records] == ["smartrecruiters_truncated"]


def test_a_board_that_redirects_to_the_companys_own_site_fails_the_fetch(settings):
    """Wise, Legal & General and Check Point do this. Their site is not read as a board."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "careers.smartrecruiters.com":
            return httpx.Response(302, headers={"Location": "https://wise.jobs/"})
        return httpx.Response(200, text="<html><title>MAKE A WISE MOVE</title></html>")

    wise = make_company("wise", ats="smartrecruiters", board="Wise")
    with pytest.raises(ValueError, match="no job list"):
        smartrecruiters.fetch(wise, mock_client(settings, handler))


def test_the_board_host_is_read_although_its_robots_txt_request_redirects(config):
    """careers.smartrecruiters.com has no robots.txt: the request redirects to the
    job-board home page. settings.yaml records that, and only for that host."""
    requested = []
    board = _board_site(requested)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            requested.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://jobs.smartrecruiters.com"})
        return board(request)

    http = HttpClient(
        config.settings, transport=httpx.MockTransport(handler), sleep=lambda seconds: None
    )
    assert len(smartrecruiters.fetch(CANVA, http)) == 12
    # The redirect is not followed: nothing is requested from the other host.
    assert requested == [
        "https://careers.smartrecruiters.com/robots.txt",
        "/Canva",
        "/Canva/api/groups?page=1",
    ]
    # Any other host doing the same is still treated as unreadable.
    with pytest.raises(RobotsDisallowedError, match="could not be read"):
        http.get_text("https://careers.example.com/Canva")


def test_the_postings_api_host_is_never_requested(settings):
    """api.smartrecruiters.com disallows every crawler but one; the client enforces it."""
    http = mock_client(
        settings,
        lambda request: httpx.Response(200, json={"content": []}),
        robots_txt=load_text("robots/smartrecruiters-api.txt"),
    )
    with pytest.raises(RobotsDisallowedError, match="Disallow: /"):
        http.get_json("https://api.smartrecruiters.com/v1/companies/Canva/postings")
