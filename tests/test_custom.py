"""Custom adapters (one company each), tested against recorded responses."""

from datetime import date

import httpx
import pytest

from conftest import load_fixture, load_text, make_company, mock_client
from jobtracker.http import RobotsDisallowedError
from jobtracker.sources import adapter_for
from jobtracker.sources.custom import google, jpmorgan

JPMORGAN = make_company("jpmorgan-chase", ats="custom", board="jpmorgan")
GOOGLE = make_company("google", ats="custom", board="google")
DEEPMIND = make_company("google-deepmind", ats="custom", board="google")


def test_a_custom_company_is_polled_only_once_its_module_exists():
    assert adapter_for(JPMORGAN) is jpmorgan.fetch
    assert adapter_for(GOOGLE) is google.fetch
    assert adapter_for(make_company("apple", ats="custom", board="apple")) is None
    assert adapter_for(make_company("citadel", ats="unsupported", board=None)) is None


# --- JPMorgan Chase (Oracle Recruiting Cloud) --------------------------------------------


def test_jpmorgan_parse_maps_every_field():
    jobs = jpmorgan.parse(JPMORGAN, load_fixture("custom/jpmorgan_programs.json"))
    internship = jobs[0]

    assert internship.source == "custom"
    assert internship.company == "jpmorgan-chase"
    assert internship.job_id == "210775780"
    assert internship.title == (
        "2027 Quantitative Research Markets Associate Program – Off-Cycle Internship – London"
    )
    assert internship.url == (
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210775780"
    )
    assert internship.locations == ("LONDON, LONDON, United Kingdom",)
    assert internship.department == "Seasonal Employee"
    assert internship.posted_at == date(2026, 11, 13)
    assert internship.key == "custom:jpmorgan-chase:210775780"
    assert len(jobs) == 3


def test_jpmorgan_parse_includes_secondary_locations():
    jobs = jpmorgan.parse(JPMORGAN, load_fixture("custom/jpmorgan_programs.json"))
    assert jobs[2].locations == ("Boston, MA, United States", "Philadelphia, PA, United States")


def test_jpmorgan_parse_empty_and_malformed():
    empty = {"items": [{"TotalJobsCount": 0, "requisitionList": []}]}
    assert jpmorgan.parse(JPMORGAN, empty) == []
    with pytest.raises((KeyError, IndexError)):
        jpmorgan.parse(JPMORGAN, {"items": []})


def test_jpmorgan_fetch_asks_for_the_programme_job_family_page_by_page(settings):
    finders = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "jpmc.fa.oraclecloud.com"
        assert request.url.path == "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        finder = request.url.params["finder"]
        finders.append(finder)
        offset = int(finder.split("offset=")[1].split(",")[0])
        requisitions = [
            {"Id": str(number), "Title": f"Programme {number}", "PrimaryLocation": "London"}
            for number in range(offset, min(offset + 200, 249))
        ]
        return httpx.Response(
            200, json={"items": [{"TotalJobsCount": 249, "requisitionList": requisitions}]}
        )

    jobs = jpmorgan.fetch(JPMORGAN, mock_client(settings, handler))

    assert len(jobs) == 249
    assert [finder.split("offset=")[1].split(",")[0] for finder in finders] == ["0", "200"]
    assert all("selectedTitlesFacet=PAA" in finder for finder in finders)
    assert all("siteNumber=CX_1001" in finder for finder in finders)


# --- Google Careers (also Google DeepMind) -----------------------------------------------


def test_google_parse_maps_every_field():
    jobs = google.parse(GOOGLE, load_text("custom/google_interns_uk.html"))
    first = jobs[0]

    assert first.source == "custom"
    assert first.company == "google"
    assert first.job_id == "73321728058499782"
    assert first.title == "Data Science PhD Intern, 2027"
    assert first.url == (
        "https://www.google.com/about/careers/applications/jobs/results/73321728058499782"
    )
    assert first.locations == ("London, UK",)
    assert first.posted_at is None
    assert first.key == "custom:google:73321728058499782"
    assert [job.title for job in jobs] == [
        "Data Science PhD Intern, 2027",
        "Student Researcher, 2027",
        "Forward Deployed Engineering Intern, BS/MS, 2027",
    ]


def test_google_parse_keeps_every_location_of_a_role():
    jobs = google.parse(GOOGLE, load_text("custom/google_interns_uk.html"))
    researcher = jobs[1]
    assert researcher.locations[:2] == ("Zürich, Switzerland", "Berlin, Germany")
    assert "London, UK" in researcher.locations
    assert len(researcher.locations) == 9


def test_google_parse_a_search_with_no_results():
    page = load_text("custom/google_deepmind_interns_none.html")
    assert google.parse(DEEPMIND, page) == []
    assert google.total_results(page) == 0


def test_google_total_results_shows_when_a_search_overflows_its_one_page():
    page = load_text("custom/google_deepmind_uk_overflow.html")
    assert google.total_results(page) == 41  # the live page showed 20 of them


@pytest.mark.parametrize(
    "page",
    [
        "<html><body>Sign in</body></html>",
        "<script>AF_initDataCallback({key: 'ds:1', hash: '2', data:{\"jobs\": []}});</script>",
        "<script>AF_initDataCallback({key: 'ds:1', hash: '2', data:[[[42]],null,1,20]});</script>",
    ],
)
def test_google_parse_rejects_a_page_whose_data_has_changed_shape(page):
    """The data is read by position, so anything unexpected must fail loudly."""
    with pytest.raises((ValueError, IndexError, TypeError)):
        google.parse(GOOGLE, page)


def test_google_fetch_runs_one_search_per_country_and_never_asks_for_page_two(settings):
    searches = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/about/careers/applications/jobs/results"
        searches.append(dict(request.url.params))
        return httpx.Response(200, text=load_text("custom/google_interns_uk.html"))

    http = mock_client(settings, handler, robots_txt=load_text("robots/google.txt"))
    jobs = google.fetch(GOOGLE, http)

    assert [search["location"] for search in searches] == [
        "United Kingdom",
        "India",
        "United States",
    ]
    for search in searches:
        assert search["company"] == "Google"
        assert search["target_level"] == "INTERN_AND_APPRENTICE"
        assert search["sort_by"] == "date"
        assert "page" not in search
    assert len(jobs) == 3  # the same three roles from each search, kept once


def test_google_fetch_searches_deepmind_once_worldwide(settings):
    searches = []

    def handler(request: httpx.Request) -> httpx.Response:
        searches.append(dict(request.url.params))
        return httpx.Response(200, text=load_text("custom/google_deepmind_interns_none.html"))

    assert google.fetch(DEEPMIND, mock_client(settings, handler)) == []
    assert searches == [
        {"company": "DeepMind", "target_level": "INTERN_AND_APPRENTICE", "sort_by": "date"}
    ]


def test_google_robots_txt_would_refuse_a_second_page(settings):
    """The limit the adapter is designed around, checked against the real robots.txt."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("page two must not be requested")

    http = mock_client(settings, handler, robots_txt=load_text("robots/google.txt"))
    with pytest.raises(RobotsDisallowedError):
        http.get_text(google.URL, params={"company": "Google", "page": 2})
