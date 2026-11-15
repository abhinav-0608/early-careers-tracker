"""Custom adapters for Amazon, Apple, Microsoft and AMD (stage 3b).

Each fixture is a trimmed copy of a response recorded from the live site on 2026-11-15
(long description text cut, a handful of roles kept).
"""

from datetime import date

import httpx
import pytest

from conftest import load_fixture, load_text, make_company, mock_client
from jobtracker import robots
from jobtracker.http import HttpClient, RobotsDisallowedError
from jobtracker.sources import adapter_for
from jobtracker.sources.custom import amazon, amd, apple, microsoft

AMAZON = make_company("amazon", ats="custom", board="amazon")
APPLE = make_company("apple", ats="custom", board="apple")
MICROSOFT = make_company("microsoft", ats="custom", board="microsoft")
AMD = make_company("amd", ats="custom", board="amd")


def test_each_company_is_polled_by_its_own_module():
    assert adapter_for(AMAZON) is amazon.fetch
    assert adapter_for(APPLE) is apple.fetch
    assert adapter_for(MICROSOFT) is microsoft.fetch
    assert adapter_for(AMD) is amd.fetch


# --- Amazon (amazon.jobs search.json) ------------------------------------------------------


def test_amazon_parse_maps_every_field():
    jobs = amazon.parse(AMAZON, load_fixture("custom/amazon_student_programs.json"))
    london = jobs[0]

    assert london.source == "custom"
    assert london.company == "amazon"
    assert london.job_id == "10602219"
    assert london.title == "Data Engineer Internship"  # the site has a trailing space
    assert london.url == "https://www.amazon.jobs/en/jobs/10602219/data-engineer-internship"
    assert london.locations == ("London, England, United Kingdom",)
    assert london.department == "Business Intelligence"
    assert london.posted_at == date(2026, 11, 11)
    assert london.remote_hint is None
    assert london.key == "custom:amazon:10602219"
    assert [job.locations for job in jobs[1:]] == [
        ("Herndon, Virginia, United States",),
        ("Seoul, South Korea",),
    ]


def test_amazon_parse_reads_a_remote_role_and_survives_missing_details():
    role = {
        "id_icims": 1,
        "title": "Insight Day",
        "job_path": "/en/jobs/1/insight-day",
        "locations": ['{"type":"VIRTUAL","normalizedCountryName":"United Kingdom"}'],
        "posted_date": "soon",
    }
    bare = {"id_icims": 2, "title": "Intern", "job_path": "/en/jobs/2/intern",
            "normalized_location": "Bengaluru, Karnataka, IND"}  # fmt: skip
    remote, plain = amazon.parse(AMAZON, {"jobs": [role, bare]})
    assert remote.remote_hint is True
    assert remote.locations == ("United Kingdom",)
    assert remote.posted_at is None
    assert plain.locations == ("Bengaluru, Karnataka, IND",)
    assert plain.remote_hint is None


def test_amazon_parse_empty_and_malformed():
    assert amazon.parse(AMAZON, {"error": None, "hits": 0, "jobs": []}) == []
    with pytest.raises(ValueError, match="returned an error"):
        amazon.parse(AMAZON, {"error": "Bad request", "jobs": []})
    with pytest.raises(KeyError):
        amazon.parse(AMAZON, {"hits": 3})


def test_amazon_fetch_asks_for_student_programmes_in_three_countries_page_by_page(settings):
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.amazon.jobs"
        assert request.url.path == "/en/search.json"
        params = request.url.params
        queries.append(params)
        offset = int(params["offset"])
        jobs = [
            {"id_icims": number, "title": f"Intern {number}", "job_path": f"/en/jobs/{number}/x"}
            for number in range(offset, min(offset + 100, 230))
        ]
        return httpx.Response(200, json={"error": None, "hits": 230, "jobs": jobs})

    robots_txt = load_text("robots/amazon-jobs.txt")
    jobs = amazon.fetch(AMAZON, mock_client(settings, handler, robots_txt=robots_txt))

    assert len(jobs) == 230
    assert [query["offset"] for query in queries] == ["0", "100", "200"]
    first = queries[0]
    assert first.get_list("business_category[]") == ["studentprograms"]
    assert first.get_list("normalized_country_code[]") == ["GBR", "IND", "USA"]
    assert first["result_limit"] == "100"
    assert first["sort"] == "recent"


def test_amazon_fetch_stops_at_its_page_limit_and_says_so(settings, caplog):
    caplog.set_level("INFO", logger="jobtracker")

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        jobs = [
            {"id_icims": number, "title": "Intern", "job_path": f"/en/jobs/{number}/x"}
            for number in range(offset, offset + 100)
        ]
        return httpx.Response(200, json={"hits": 5000, "jobs": jobs})

    jobs = amazon.fetch(AMAZON, mock_client(settings, handler))

    assert len(jobs) == amazon.MAX_PAGES * 100
    (record,) = [r for r in caplog.records if r.getMessage() == "amazon_results_truncated"]
    assert record.fields == {"company": "amazon", "found": 5000, "read": 1000}


# --- Apple (jobs.apple.com search page) ----------------------------------------------------


def test_apple_parse_maps_every_field():
    page = load_text("custom/apple_interns_page1.html")
    jobs = apple.parse(APPLE, page)
    cambridge = jobs[1]

    assert cambridge.source == "custom"
    assert cambridge.company == "apple"
    assert cambridge.job_id == "200690125"
    assert (
        cambridge.title == "Internship - Machine Learning Research - Speech-to-Speech Translation"
    )
    assert cambridge.url == (
        "https://jobs.apple.com/en-gb/details/200690125-1251/"
        "internship-machine-learning-research-speech-to-speech-translation"
    )
    assert cambridge.locations == ("Cambridge, United Kingdom",)
    assert cambridge.department == "Students"
    assert cambridge.posted_at == date(2026, 10, 29)
    assert cambridge.remote_hint is None
    assert cambridge.key == "custom:apple:200690125"
    assert [job.title for job in jobs] == ["Apple Music Intern", cambridge.title]
    assert apple.total_results(page) == 23


def test_apple_parse_merges_a_role_listed_once_per_location():
    """The live page lists "MDS TPM Intern - Data" three times, once for each city."""
    (job,) = apple.parse(APPLE, load_text("custom/apple_interns_page2.html"))
    assert job.job_id == "200690108"
    assert job.locations == ("Chengdu, China", "Shanghai, China", "Shenzhen, China")


def test_apple_parse_a_search_with_no_results():
    page = load_text("custom/apple_interns_none.html")
    assert apple.parse(APPLE, page) == []
    assert apple.total_results(page) == 0


@pytest.mark.parametrize(
    "page",
    [
        "<html><body>Sign in to continue</body></html>",
        '<script>window.__staticRouterHydrationData = JSON.parse("{}");</script>',
        '<script>window.__staticRouterHydrationData = JSON.parse("{\\"loaderData\\":'
        '{\\"search\\":{\\"searchResults\\":[{\\"postingTitle\\":\\"No id\\"}],'
        '\\"totalRecords\\":1}}}");</script>',
    ],
)
def test_apple_parse_rejects_a_page_whose_data_has_changed_shape(page):
    with pytest.raises(ValueError, match="Apple jobs"):
        apple.parse(APPLE, page)


def test_apple_fetch_reads_every_page_of_the_internships_search(settings):
    requested = []
    pages = {
        None: load_text("custom/apple_interns_page1.html"),
        "2": load_text("custom/apple_interns_page2.html"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "jobs.apple.com"
        assert request.url.path == "/en-gb/search"
        requested.append(dict(request.url.params))
        return httpx.Response(200, text=pages[request.url.params.get("page")])

    # The fixtures say 23 roles in all and hold 2 and 3 listings, so pretend a page is 2.
    jobs = apple.fetch(APPLE, mock_client(settings, _counting_down(handler, total=5)))

    assert requested == [
        {"team": "internships-STDNT-INTRN", "sort": "newest"},
        {"team": "internships-STDNT-INTRN", "sort": "newest", "page": "2"},
    ]
    assert [job.job_id for job in jobs] == ["200694171", "200690125", "200690108"]


def _counting_down(handler, total: int):
    """Serve the Apple fixtures with their total rewritten, so paging ends on page 2."""

    def handle(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        text = response.text.replace('\\"totalRecords\\":23', f'\\"totalRecords\\":{total}')
        return httpx.Response(200, text=text)

    return handle


def test_apple_is_fetched_although_its_robots_txt_redirects_to_a_not_found_page(settings):
    """What jobs.apple.com does: /robots.txt -> apple.com/pagenotfound -> 404."""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == "https://jobs.apple.com/robots.txt":
            return httpx.Response(301, headers={"Location": "https://apple.com/pagenotfound"})
        if url == "https://apple.com/robots.txt":
            return httpx.Response(301, headers={"Location": "https://www.apple.com/robots.txt"})
        if url == "https://www.apple.com/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /tmall*\n")
        if url == "https://apple.com/pagenotfound":
            return httpx.Response(301, headers={"Location": "https://www.apple.com/pagenotfound"})
        if url == "https://www.apple.com/pagenotfound":
            return httpx.Response(404, text="Page not found")
        return httpx.Response(200, text=load_text("custom/apple_interns_none.html"))

    http = HttpClient(settings, transport=httpx.MockTransport(handler))
    assert apple.fetch(APPLE, http) == []
    assert requested == [
        "https://jobs.apple.com/robots.txt",
        "https://apple.com/robots.txt",
        "https://www.apple.com/robots.txt",
        "https://apple.com/pagenotfound",
        "https://www.apple.com/robots.txt",  # the www host's own rules, before its page
        "https://www.apple.com/pagenotfound",
        "https://jobs.apple.com/en-gb/search?team=internships-STDNT-INTRN&sort=newest",
    ]


# --- Microsoft (apply.careers.microsoft.com, Eightfold) ------------------------------------


def test_microsoft_parse_maps_every_field():
    jobs = microsoft.parse(MICROSOFT, load_fixture("custom/microsoft_interns.json"))
    cambridge = jobs[0]

    assert cambridge.source == "custom"
    assert cambridge.company == "microsoft"
    assert cambridge.job_id == "1970393557070382"
    assert cambridge.title == "Cambridge Internship Program: MSRC AI Security Research"
    assert cambridge.url == "https://apply.careers.microsoft.com/careers/job/1970393557070382"
    assert cambridge.locations == ("United Kingdom, Cambridgeshire, Cambridge",)
    assert cambridge.department == "Research Sciences"
    assert cambridge.posted_at == date(2026, 11, 11)
    assert cambridge.remote_hint is False  # "onsite"
    assert cambridge.key == "custom:microsoft:1970393557070382"
    assert jobs[2].locations == (
        "United States, Washington, Redmond",
        "United States, California, Mountain View",
    )
    assert len(jobs) == 3


def test_microsoft_parse_empty_and_malformed():
    empty = {"status": 200, "data": {"positions": [], "count": 0}}
    assert microsoft.parse(MICROSOFT, empty) == []
    with pytest.raises(ValueError, match="status 500"):
        microsoft.parse(MICROSOFT, {"status": 500, "error": {"message": "boom"}})
    with pytest.raises(KeyError):
        microsoft.parse(MICROSOFT, {"status": 200, "data": {}})


def test_microsoft_fetch_pages_through_the_intern_level(settings):
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "apply.careers.microsoft.com"
        assert request.url.path == "/api/pcsx/search"
        params = request.url.params
        queries.append(params)
        start = int(params["start"])
        positions = [
            {"id": number, "name": f"Intern {number}", "positionUrl": f"/careers/job/{number}"}
            for number in range(start, min(start + 10, 24))
        ]
        return httpx.Response(
            200, json={"status": 200, "data": {"positions": positions, "count": 24}}
        )

    robots_txt = load_text("robots/microsoft-apply.txt")
    jobs = microsoft.fetch(MICROSOFT, mock_client(settings, handler, robots_txt=robots_txt))

    assert len(jobs) == 24
    assert [query["start"] for query in queries] == ["0", "10", "20"]
    assert queries[0]["domain"] == "microsoft.com"
    assert queries[0]["filter_seniority"] == "Intern"
    assert queries[0]["sort_by"] == "timestamp"


def test_microsoft_robots_txt_allows_the_search_api_and_job_pages_only():
    rules = robots.parse(load_text("robots/microsoft-apply.txt"), "job-alert-tracker")
    assert rules.allows("/api/pcsx/search?domain=microsoft.com&start=0&filter_seniority=Intern")
    assert rules.allows("/careers/job/1970393557070382")
    assert not rules.allows("/api/other/thing")
    assert not rules.allows("/user/profile")


# --- AMD (careers.amd.com) -----------------------------------------------------------------


def test_amd_parse_maps_every_field():
    jobs = amd.parse(AMD, load_fixture("custom/amd_students.json"))
    cork = jobs[0]

    assert cork.source == "custom"
    assert cork.company == "amd"
    assert cork.job_id == "88848"
    assert cork.title == "2027 Analog & Mixed-Signal Design Intern"
    assert cork.url == "https://careers.amd.com/jobs/88848"
    assert cork.locations == ("Cork, Ireland",)  # city and county are both "Cork"
    assert cork.department == "Student / Intern / Temp"
    assert cork.posted_at == date(2026, 11, 6)
    assert cork.key == "custom:amd:88848"
    belfast = jobs[1]
    assert belfast.title == "Engineering Placement Intern - 2027/2028"
    assert belfast.locations == ("Belfast, United Kingdom",)
    assert belfast.department == "Engineering"
    assert jobs[2].locations == ("Singapore",)
    assert len(jobs) == 3


def test_amd_parse_empty_and_malformed():
    assert amd.parse(AMD, {"jobs": [], "totalCount": 0}) == []
    with pytest.raises(KeyError):
        amd.parse(AMD, {"error": "nope"})
    with pytest.raises(KeyError):
        amd.parse(AMD, {"jobs": [{"data": {"title": "No slug"}}]})


def test_amd_fetch_reads_the_student_category_and_three_keyword_searches(settings):
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "careers.amd.com"
        assert request.url.path == "/api/jobs"
        queries.append(dict(request.url.params))
        return httpx.Response(200, json=load_fixture("custom/amd_students.json"))

    jobs = amd.fetch(AMD, mock_client(settings, handler))

    common = {"page": "1", "limit": "100", "sortBy": "posted_date", "descending": "true",
              "internal": "false"}  # fmt: skip
    assert queries == [
        {**common, "categories": "Student / Intern / Temp"},
        {**common, "keywords": "intern"},
        {**common, "keywords": "placement"},
        {**common, "keywords": "graduate"},
    ]
    assert len(jobs) == 3  # the same three roles from every search, kept once


def test_amd_fetch_waits_out_the_sites_crawl_delay_between_requests(settings):
    sleeps = []
    http = mock_client(
        settings,
        lambda request: httpx.Response(200, json={"jobs": [], "totalCount": 0}),
        robots_txt=load_text("robots/amd.txt"),
        sleep=sleeps.append,
        clock=lambda: 100.0,  # a frozen clock: no time passes between requests
    )
    amd.fetch(AMD, http)
    assert sleeps == [5.0, 5.0, 5.0, 5.0]  # after robots.txt and between the four requests


def test_amd_fetch_logs_a_search_that_overflows_its_page(settings, caplog):
    caplog.set_level("INFO", logger="jobtracker")
    payload = {**load_fixture("custom/amd_students.json"), "totalCount": 240}
    amd.fetch(AMD, mock_client(settings, lambda request: httpx.Response(200, json=payload)))
    logged = [r.fields for r in caplog.records if r.getMessage() == "amd_results_truncated"]
    assert len(logged) == 4
    assert logged[3] == {"company": "amd", "keywords": "graduate", "found": 240, "read": 3}


def test_amd_never_touches_its_icims_host(settings):
    """careers-amd.icims.com takes the applications and disallows every crawler."""
    http = mock_client(
        settings,
        lambda request: httpx.Response(200, text="x"),
        robots_txt="User-agent: *\nDisallow: /",
    )
    with pytest.raises(RobotsDisallowedError):
        http.get_text("https://careers-amd.icims.com/jobs/88848/login")
    for job in amd.parse(AMD, load_fixture("custom/amd_students.json")):
        assert job.url.startswith("https://careers.amd.com/")
