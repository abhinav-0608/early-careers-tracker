from dataclasses import replace

import httpx
import pytest

from jobtracker.http import BlockedHostError, HttpClient


def client_with(settings, handler, sleeps=None, clock=None, **overrides):
    sleeps = sleeps if sleeps is not None else []
    return HttpClient(
        replace(settings, **overrides),
        transport=httpx.MockTransport(handler),
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


def test_honours_retry_after_with_a_cap(settings):
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(429, headers={"Retry-After": "9999"}),
        httpx.Response(200, json={}),
    ]
    sleeps = []
    client = client_with(settings, lambda request: responses.pop(0), sleeps, retries=2)

    client.get_json("https://example.com/jobs")
    assert sleeps == [7.0, 30.0]


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

    client.get_json("https://example.com/a")
    client.get_json("https://other.example.org/a")  # different host: no wait
    client.get_json("https://example.com/b")  # same host again: waits the full delay
    assert sleeps == [1.5]


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
