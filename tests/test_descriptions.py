"""Reading a posting's description, and the adapters that carry one already."""

import httpx
import pytest

from conftest import load_fixture, make_company, make_job, mock_client
from jobtracker.http import RobotsDisallowedError
from jobtracker.sources import ashby, lever
from jobtracker.sources.custom import amazon
from jobtracker.sources.descriptions import describe, page_text, plain_text

# --- text ------------------------------------------------------------------------------


def test_plain_text_drops_tags_keeps_line_breaks_and_unescapes():
    markup = "<p>Open to <b>first&nbsp;year</b> students.</p><p>Apply&amp;go<br>now</p>"
    assert plain_text(markup) == "Open to first\xa0year students.\nApply&go\nnow"


def test_page_text_prefers_the_job_posting_json_ld():
    page = (
        "<html><head><script type='application/ld+json'>"
        '[{"@type": "Organization"}, {"@type": "JobPosting", '
        '"description": "&lt;p&gt;For first years.&lt;/p&gt;"}]'
        "</script></head><body><nav>Menu</nav><p>Something else</p></body></html>"
    )
    assert page_text(page) == "For first years."


def test_page_text_falls_back_to_visible_text_without_scripts_or_styles():
    page = (
        "<html><head><style>p {color: red}</style><script>var x = 1;</script></head>"
        "<body><h1>Spring Week</h1><p>Open to all year groups.</p>"
        "<script type='application/ld+json'>not json</script></body></html>"
    )
    assert page_text(page) == "Spring Week\nOpen to all year groups."


# --- where the text comes from ---------------------------------------------------------


def test_a_description_already_in_the_list_response_costs_no_request(settings):
    def handler(request):
        raise AssertionError(f"unexpected request {request.url}")

    http = mock_client(settings, handler)
    job = make_job(description="<p>First years welcome.</p>")
    assert describe(make_company(), job, http) == "First years welcome."


def test_greenhouse_description_comes_from_its_job_endpoint_unescaped_twice(settings):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        escaped = "&lt;p&gt;Open to 1st year students.&lt;/p&gt;"
        return httpx.Response(200, json={"content": escaped})

    http = mock_client(settings, handler)
    text = describe(make_company("point72"), make_job("42", source="greenhouse"), http)
    assert text == "Open to 1st year students."
    assert requested == ["https://boards-api.greenhouse.io/v1/boards/point72/jobs/42"]


def test_other_sources_read_the_postings_own_page(settings):
    def handler(request):
        assert str(request.url) == "https://example.com/jobs/7"
        return httpx.Response(200, text="<html><body><p>Any year of study.</p></body></html>")

    http = mock_client(settings, handler)
    job = make_job("7", source="workday", url="https://example.com/jobs/7")
    assert describe(make_company(ats="workday"), job, http) == "Any year of study."


def test_a_posting_page_robots_txt_disallows_is_never_requested(settings):
    def handler(request):
        raise AssertionError("a disallowed page was requested")

    http = mock_client(settings, handler, robots_txt="User-agent: *\nDisallow: /jobs/\n")
    job = make_job("7", source="workday", url="https://example.com/jobs/7")
    with pytest.raises(RobotsDisallowedError):
        describe(make_company(ats="workday"), job, http)


# --- adapters that carry the description -----------------------------------------------


def test_lever_description_joins_the_body_its_lists_and_the_closing_notes():
    payload = [
        {
            "id": "abc",
            "text": "Software Engineer, Internship",
            "hostedUrl": "https://jobs.lever.co/x/abc",
            "categories": {},
            "descriptionPlain": "About the role.",
            "lists": [{"text": "Requirements", "content": "<li>First year students</li>"}],
            "additionalPlain": "Equal opportunity.",
        },
        {"id": "def", "text": "No text at all", "hostedUrl": "u", "categories": {}},
    ]
    with_text, without = lever.parse(make_company(ats="lever"), payload)
    assert with_text.description == (
        "About the role.\nRequirements\n<li>First year students</li>\nEqual opportunity."
    )
    assert without.description is None


def test_ashby_and_amazon_carry_their_descriptions():
    (first, *_) = ashby.parse(make_company(ats="ashby"), load_fixture("ashby/openai.json"))
    assert first.description.startswith("About the Team\n")

    (london, *_) = amazon.parse(
        make_company("amazon", ats="custom", board="amazon"),
        load_fixture("custom/amazon_student_programs.json"),
    )
    assert london.description and "\n" in london.description  # description + qualifications


def test_the_description_does_not_change_a_jobs_identity():
    assert make_job(description="a") == make_job(description="b")
    assert "description" not in repr(make_job(description="long text"))
