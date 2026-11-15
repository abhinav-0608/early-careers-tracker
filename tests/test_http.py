from dataclasses import replace

import httpx
import pytest

from conftest import mock_client
from jobtracker.http import BlockedHostError, HttpClient, RobotsDisallowedError

# What every tal.net tenant publishes, and the reason these rules exist.
TALNET_ROBOTS = """
User-agent: *
Disallow: /*/agent/
Disallow: /*/ats/
Disallow: /*/channel/
Crawl-delay: 10

User-agent: Bytespider
User-agent: GPTBot
Disallow: /
"""


def client_with(settings, handler, sleeps=None, clock=None, robots_txt=None, **overrides):
    """A client whose robots.txt requests are answered apart from `handler`."""
    sleeps = sleeps if sleeps is not None else []
    return mock_client(
        replace(settings, **overrides),
        handler,
        robots_txt=robots_txt,
        sleep=sleeps.append,
        **({"clock": clock} if clock else {}),
    )


def test_sends_an_honest_user_agent(settings):
    seen = {}

    def handler(request):
        seen["user_agent"] = request.headers["User-Agent"]
        return httpx.Response(200, json={})

    client_with(settings, handler).get_json("https://example.com/jobs")
    assert seen["user_agent"] == settings.user_agent
    assert "early-careers-tracker" in seen["user_agent"]


def test_retries_a_server_error_then_succeeds(settings):
    responses = [httpx.Response(503), httpx.Response(200, json={"ok": True})]
    sleeps = []
    client = client_with(settings, lambda request: responses.pop(0), sleeps, retries=2)

    assert client.get_json("https://example.com/jobs") == {"ok": True}
    assert sleeps == [1.0]  # one backoff before the second attempt


def test_gives_up_after_the_configured_retries(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    client = client_with(settings, handler, retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_json("https://example.com/jobs")
    assert len(calls) == 3


def test_does_not_retry_a_client_error(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    client = client_with(settings, handler, retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_json("https://example.com/jobs")
    assert len(calls) == 1


def test_honours_retry_after(settings):
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(200, json={}),
    ]
    sleeps = []
    client = client_with(settings, lambda request: responses.pop(0), sleeps, retries=2)

    client.get_json("https://example.com/jobs")
    assert sleeps == [7.0, 30.0]


def test_does_not_retry_when_asked_to_stay_away_longer_than_a_run_can_wait(settings):
    """Retrying after 30 seconds a host that said "come back in an hour" would ignore it."""
    calls, sleeps = [], []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    client = client_with(settings, handler, sleeps, retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_json("https://example.com/jobs")
    assert calls == ["/jobs"]
    assert sleeps == []


def test_retries_a_timeout_then_raises_it(settings):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    client = client_with(settings, handler, retries=1)
    with pytest.raises(httpx.TimeoutException):
        client.get_json("https://example.com/jobs")


def test_spaces_out_requests_to_the_same_host(settings):
    sleeps = []
    client = client_with(
        settings,
        lambda request: httpx.Response(200, json={}),
        sleeps,
        clock=lambda: 100.0,  # a frozen clock: no time passes between requests
        per_host_delay_seconds=1.5,
    )

    client.get_json("https://example.com/a")  # waits once: robots.txt was a request too
    client.get_json("https://other.example.org/a")  # a different host: its own first wait
    assert sleeps == [1.5, 1.5]
    client.get_json("https://example.com/b")  # same host again: waits the full delay
    assert sleeps == [1.5, 1.5, 1.5]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/search",
        "https://uk.indeed.com/jobs",
        "https://www.glassdoor.co.uk/Job/index.htm",
    ],
)
def test_never_fetches_blocked_sites(settings, url):
    def handler(request):
        raise AssertionError("no request should be made")

    with pytest.raises(BlockedHostError):
        client_with(settings, handler).get_json(url)


# --- robots.txt ------------------------------------------------------------------------


def test_reads_robots_txt_once_per_host_before_fetching(settings):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n", json=None)

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    client.get_text("https://example.com/jobs")
    client.get_text("https://example.com/more")
    assert requested == ["/robots.txt", "/jobs", "/more"]


def test_asks_for_robots_txt_as_text_not_as_json(settings):
    """Workday answers 406 to "Accept: application/json", which would read as "no rules"."""

    def handler(request):
        if request.url.path != "/robots.txt":
            return httpx.Response(200, json={})
        if "text/plain" not in request.headers["Accept"]:
            return httpx.Response(406, json={"errorCode": "HTTP_406"})
        return httpx.Response(200, text="User-agent: *\nDisallow: /Careers/\n")

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(RobotsDisallowedError, match="Disallow: /Careers/"):
        client.require_allowed("https://acme.wd1.myworkdayjobs.com/Careers/")
    client.require_allowed("https://acme.wd1.myworkdayjobs.com/Campus/")


def test_never_requests_a_path_robots_txt_disallows(settings):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, text="<html></html>")

    client = client_with(settings, handler, robots_txt=TALNET_ROBOTS)
    with pytest.raises(RobotsDisallowedError, match=r"disallows /vx/lang-en-GB/ats/login"):
        client.get_text("https://bank.tal.net/vx/lang-en-GB/ats/login")
    assert requested == []

    client.get_text("https://bank.tal.net/vx/lang-en-GB/candidate/jobboard/vacancy/1/adv/")
    assert requested == ["/vx/lang-en-GB/candidate/jobboard/vacancy/1/adv/"]


def test_refuses_a_redirect_into_a_disallowed_path(settings):
    """A tal.net tenant's root redirects to /ats/login, which its robots.txt disallows.

    The redirect must not be followed: the disallowed page is never requested.
    """
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/":
            login = "https://bank.tal.net/vx/lang-en-GB/xf-e7dd106cee79/ats/login"
            return httpx.Response(302, headers={"Location": login})
        raise AssertionError(f"{request.url.path} must not be requested")

    client = client_with(settings, handler, robots_txt=TALNET_ROBOTS)
    with pytest.raises(RobotsDisallowedError, match="ats/login"):
        client.get_text("https://bank.tal.net/")
    assert requested == ["/"]


def test_refuses_a_redirect_into_a_path_another_host_disallows(settings):
    """Each hop is checked against the robots.txt of the host it lands on."""
    requested = []

    def handler(request):
        if request.url.path == "/robots.txt":
            closed = request.url.host == "login.example.org"
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n" if closed else "")
        requested.append(str(request.url))
        return httpx.Response(301, headers={"Location": "https://login.example.org/sso"})

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(RobotsDisallowedError, match="login.example.org"):
        client.get_text("https://example.com/jobs")
    assert requested == ["https://example.com/jobs"]


def test_follows_a_redirect_to_an_allowed_path(settings):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "/new"})  # relative, as servers do
        return httpx.Response(200, text="the page")

    client = client_with(settings, handler, robots_txt=TALNET_ROBOTS)
    assert client.get_text("https://example.com/old") == "the page"
    assert requested == ["/old", "/new"]


def test_refuses_a_redirect_to_a_never_scrape_host(settings):
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://www.linkedin.com/jobs"})

    with pytest.raises(BlockedHostError):
        client_with(settings, handler).get_text("https://example.com/jobs")


def test_gives_up_on_a_redirect_loop(settings):
    def handler(request):
        return httpx.Response(302, headers={"Location": "/again"})

    with pytest.raises(httpx.TooManyRedirects):
        client_with(settings, handler).get_text("https://example.com/jobs")


def test_does_not_follow_a_redirected_post(settings):
    def handler(request):
        return httpx.Response(302, headers={"Location": "/elsewhere"})

    with pytest.raises(httpx.HTTPStatusError):
        client_with(settings, handler).post_json("https://example.com/api", {})


def test_honours_crawl_delay_when_longer_than_the_configured_gap(settings):
    sleeps = []
    client = client_with(
        settings,
        lambda request: httpx.Response(200, text="page"),
        sleeps,
        clock=lambda: 100.0,  # a frozen clock: no time passes between requests
        robots_txt=TALNET_ROBOTS,
        per_host_delay_seconds=1.0,
    )
    client.get_text("https://bank.tal.net/vx/a")
    client.get_text("https://bank.tal.net/vx/b")
    # Ten seconds after reading robots.txt, and ten more before the second page.
    assert sleeps == [10.0, 10.0]


def test_crawl_delay_applies_only_to_the_host_that_asked_for_it(settings):
    sleeps = []

    def handler(request):
        if request.url.path == "/robots.txt":
            slow = request.url.host == "slow.example.com"
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 10\n" if slow else "")
        return httpx.Response(200, text="page")

    client = HttpClient(
        replace(settings, per_host_delay_seconds=1.0),
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        clock=lambda: 100.0,
    )
    client.get_text("https://slow.example.com/a")
    client.get_text("https://quick.example.com/a")
    assert sleeps == [10.0, 1.0]


def test_a_shorter_crawl_delay_does_not_lower_the_configured_gap(settings):
    sleeps = []
    client = client_with(
        settings,
        lambda request: httpx.Response(200, text="page"),
        sleeps,
        clock=lambda: 100.0,
        robots_txt="User-agent: *\nCrawl-delay: 1\n",
        per_host_delay_seconds=2.5,
    )
    client.get_text("https://example.com/a")
    assert sleeps == [2.5]


def test_refuses_a_host_whose_crawl_delay_cannot_be_honoured_in_a_run(settings):
    def handler(request):
        raise AssertionError("no page should be requested")

    client = client_with(settings, handler, robots_txt="User-agent: *\nCrawl-delay: 3600\n")
    with pytest.raises(RobotsDisallowedError, match="3600s between requests"):
        client.get_text("https://example.com/jobs")


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_missing_robots_txt_means_no_rules(settings, status):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(status, text="Blocked")
        return httpx.Response(200, json={"ok": True})

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    assert client.get_json("https://example.com/jobs") == {"ok": True}


@pytest.mark.parametrize("status", [429, 500, 503])
def test_an_unreadable_robots_txt_means_nothing_is_fetched(settings, status):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(status)

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    for _ in range(2):  # and the failure is remembered: robots.txt is not asked for again
        with pytest.raises(RobotsDisallowedError, match="could not be read"):
            client.get_json("https://example.com/jobs")
    assert requested == ["/robots.txt"]


def test_follows_a_robots_txt_redirect_only_to_another_robots_txt(settings):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.path != "/robots.txt":
            return httpx.Response(200, text="page")
        if request.url.host == "example.com":  # the bare domain points at www
            return httpx.Response(301, headers={"Location": "https://www.example.com/robots.txt"})
        return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(RobotsDisallowedError):
        client.get_text("https://example.com/private/x")
    assert client.get_text("https://example.com/jobs") == "page"
    assert requested == [
        "https://example.com/robots.txt",
        "https://www.example.com/robots.txt",
        "https://example.com/jobs",
    ]


def test_does_not_follow_a_robots_txt_redirect_to_any_other_page(settings):
    """With the rules unread, a redirect to a login page cannot be known to be allowed."""
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/vx/ats/login"})

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(RobotsDisallowedError, match="could not be read"):
        client.get_text("https://bank.tal.net/vx/candidate/jobboard/vacancy/1/adv/")
    assert requested == ["/robots.txt"]


def _site_whose_robots_txt_redirects_to(target: str, other_robots: str, status: int, requested):
    """jobs.example.com sends /robots.txt to a page on www.example.com."""

    def handler(request):
        url = str(request.url)
        requested.append(url)
        if url == "https://jobs.example.com/robots.txt":
            return httpx.Response(301, headers={"Location": target})
        if url == "https://www.example.com/robots.txt":
            return httpx.Response(200, text=other_robots)
        if request.url.host == "www.example.com":
            return httpx.Response(status, text="a page")
        return httpx.Response(200, text="jobs")

    return handler


def test_a_robots_txt_redirect_to_another_hosts_not_found_page_means_no_rules(settings):
    """RFC 9309: follow the redirect; a 404 where it ends means there is no robots.txt.

    The hop is an ordinary checked request, so the other host's rules are read first.
    """
    requested = []
    handler = _site_whose_robots_txt_redirects_to(
        "https://www.example.com/pagenotfound",
        "User-agent: *\nDisallow: /private/\n",
        404,
        requested,
    )
    client = HttpClient(settings, transport=httpx.MockTransport(handler))

    assert client.get_text("https://jobs.example.com/search") == "jobs"
    assert client.get_text("https://jobs.example.com/search?page=2") == "jobs"
    assert requested == [
        "https://jobs.example.com/robots.txt",
        "https://www.example.com/robots.txt",
        "https://www.example.com/pagenotfound",
        "https://jobs.example.com/search",
        "https://jobs.example.com/search?page=2",
    ]


def test_a_robots_txt_redirect_into_a_path_the_other_host_disallows_is_not_followed(settings):
    requested = []
    handler = _site_whose_robots_txt_redirects_to(
        "https://www.example.com/private/login",
        "User-agent: *\nDisallow: /private/\n",
        404,
        requested,
    )
    client = HttpClient(settings, transport=httpx.MockTransport(handler))

    with pytest.raises(RobotsDisallowedError, match="could not be read"):
        client.get_text("https://jobs.example.com/search")
    assert requested == [
        "https://jobs.example.com/robots.txt",
        "https://www.example.com/robots.txt",
    ]


@pytest.mark.parametrize("status", [200, 500])
def test_a_robots_txt_redirect_that_ends_on_a_real_page_leaves_the_rules_unread(settings, status):
    """Only "not found" means "no robots.txt". A home page or an error says nothing."""
    requested = []
    handler = _site_whose_robots_txt_redirects_to(
        "https://www.example.com/welcome", "", status, requested
    )
    client = HttpClient(settings, transport=httpx.MockTransport(handler))

    with pytest.raises(RobotsDisallowedError, match="could not be read"):
        client.get_text("https://jobs.example.com/search")
    assert "https://jobs.example.com/search" not in requested


def test_a_robots_txt_redirect_that_loops_back_is_given_up_on(settings):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.path == "/robots.txt" and request.url.host == "www.example.com":
            return httpx.Response(404)
        if request.url.host == "www.example.com":  # ... and straight back again
            return httpx.Response(302, headers={"Location": "https://jobs.example.com/home"})
        return httpx.Response(301, headers={"Location": "https://www.example.com/moved"})

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(RobotsDisallowedError, match="could not be read"):
        client.get_text("https://jobs.example.com/search")
    assert requested == [
        "https://jobs.example.com/robots.txt",
        "https://www.example.com/robots.txt",
        "https://www.example.com/moved",
    ]


def test_a_host_recorded_as_having_no_robots_txt_may_redirect_the_request(settings):
    """For a host checked by hand (settings.yaml). A real file there is still obeyed."""
    requested = []
    answers = {"redirecting.example.com": httpx.Response(302, headers={"Location": "/home"})}

    def handler(request):
        requested.append(request.url.host + request.url.path)
        if request.url.path == "/robots.txt":
            return answers.get(
                request.url.host, httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
            )
        return httpx.Response(200, text="page")

    listed = frozenset({"redirecting.example.com", "strict.example.com"})
    client = HttpClient(
        replace(settings, hosts_without_robots_txt=listed), transport=httpx.MockTransport(handler)
    )
    assert client.get_text("https://redirecting.example.com/private/x") == "page"
    with pytest.raises(RobotsDisallowedError, match="Disallow: /private/"):
        client.get_text("https://strict.example.com/private/x")
    assert "redirecting.example.com/home" not in requested  # the redirect is not followed


def test_an_unreadable_robots_txt_is_not_retried_when_the_host_asks_for_hours(settings):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "25028"})

    client = HttpClient(
        replace(settings, retries=2), transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    for _ in range(2):
        with pytest.raises(RobotsDisallowedError, match="HTTP 429"):
            client.get_json("https://example.com/api/jobs")
    assert requested == ["/robots.txt"]


def test_a_host_can_be_given_a_longer_gap_than_the_rest(settings):
    sleeps = []
    client = client_with(
        settings,
        lambda request: httpx.Response(200, text="page"),
        sleeps,
        clock=lambda: 100.0,  # a frozen clock: no time passes between requests
        per_host_delay_seconds=1.0,
        host_delay_seconds={"slow.example.com": 5.0},
    )
    client.get_text("https://slow.example.com/a")
    client.get_text("https://slow.example.com/b")
    client.get_text("https://example.com/a")
    client.get_text("https://example.com/b")
    # Each host: robots.txt, then two pages, so two waits each.
    assert sleeps == [5.0, 5.0, 1.0, 1.0]


def test_a_robots_txt_that_times_out_means_nothing_is_fetched(settings):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(RobotsDisallowedError, match="ReadTimeout"):
        client.get_json("https://example.com/jobs")


def test_require_allowed_checks_a_url_without_fetching_it(settings):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, json={})

    robots_txt = "User-agent: *\nAllow: /Campus/\nDisallow: /Internal/\n"
    client = client_with(settings, handler, robots_txt=robots_txt)
    client.require_allowed("https://acme.wd1.myworkdayjobs.com/Campus/")
    with pytest.raises(RobotsDisallowedError):
        client.require_allowed("https://acme.wd1.myworkdayjobs.com/Internal/")
    assert requested == []


def test_query_strings_are_matched_against_robots_rules(settings):
    robots_txt = "User-agent: *\nDisallow: /jobs/results?page=\nDisallow: /jobs/results?*&page=\n"
    client = client_with(
        settings, lambda request: httpx.Response(200, text="page"), robots_txt=robots_txt
    )
    assert client.get_text("https://example.com/jobs/results", params={"q": "intern"}) == "page"
    with pytest.raises(RobotsDisallowedError):
        client.get_text("https://example.com/jobs/results", params={"q": "intern", "page": 2})


def test_the_notifier_can_post_without_a_robots_check(settings):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, json={})

    client = HttpClient(settings, transport=httpx.MockTransport(handler))
    client.post_json("https://ntfy.sh/", {"topic": "t"}, check_robots=False)
    assert requested == ["/"]
