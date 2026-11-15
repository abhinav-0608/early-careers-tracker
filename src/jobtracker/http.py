"""The one HTTP client every adapter and the notifier use.

Politeness rules live here so that no adapter can forget them:

  - an honest User-Agent and a timeout on every request;
  - robots.txt is read once per host and checked before every fetch. A path it
    disallows is never requested, and that includes the target of a redirect: redirects
    are followed one hop at a time, each hop checked like any other request;
  - a minimum gap between requests to the same host, raised to the host's Crawl-delay
    when its robots.txt sets one, or to a longer gap configured for that host;
  - a small number of retries that honour Retry-After, and none at all when the host
    asks to be left alone for longer than a run can wait;
  - a hard refusal of hosts we never scrape.

The notifier is the one caller that skips the robots.txt check: publishing a message to
ntfy's API is not crawling, and ntfy has no say over what the tracker reads elsewhere.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobtracker import robots
from jobtracker.config import Settings

# Never fetched, whatever a config file says.
BLOCKED_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.co.uk")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_WAIT_SECONDS = 30.0

REDIRECT_STATUS = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5

# While a robots.txt redirect to another host is being followed, how long to wait for
# that host's turn before giving up (see _follow_robots_redirect).
ROBOTS_REDIRECT_LOCK_SECONDS = 60.0

# robots.txt is asked for as text. The client's default "Accept: application/json" makes
# some servers refuse the file (Workday answers 406), and a 4xx would then be read as
# "this host has no rules".
ROBOTS_HEADERS = {"Accept": "text/plain,*/*;q=0.8"}

# A Crawl-delay is always honoured. One longer than this cannot be honoured inside a
# 12-minute run, so the host is not fetched at all rather than fetched too fast.
MAX_CRAWL_DELAY_SECONDS = 60.0


class BlockedHostError(Exception):
    """Raised when something tries to fetch a host on the never-scrape list."""


class RobotsDisallowedError(Exception):
    """The host's robots.txt does not allow this request.

    Also raised when robots.txt could not be read (server error, timeout): not knowing
    the rules is treated as not being allowed, for this run.
    """


class HttpClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._retries = settings.retries
        self._delay = settings.per_host_delay_seconds
        self._host_delays = dict(settings.host_delay_seconds)
        self._no_robots_hosts = frozenset(settings.hosts_without_robots_txt)
        self._sleep = sleep
        self._clock = clock
        self._agent = robots.product_token(settings.user_agent)
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            timeout=settings.timeout_seconds,
            # Redirects are followed by hand in _request, so each hop can be checked.
            follow_redirects=False,
            transport=transport,
        )
        # robots.txt per host, read on the first request to it and kept for the run.
        # A host whose robots.txt could not be read is remembered with the reason.
        self._robots: dict[str, robots.RobotsRules] = {}
        self._robots_errors: dict[str, str] = {}
        # One lock per host: different hosts are fetched in parallel, but requests to
        # the same host queue up and are spaced out.
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_locks_guard = threading.Lock()
        self._last_request_at: dict[str, float] = {}
        # Per thread: the hosts whose robots.txt this thread is in the middle of
        # resolving, and whose locks it therefore holds (see _follow_robots_redirect).
        self._resolving = threading.local()

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", _with_params(url, params)).json()

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        """Fetch a page (HTML or XML) as text."""
        headers = {"Accept": "text/html,application/xhtml+xml"}
        return self._request("GET", _with_params(url, params), headers=headers).text

    def post_json(
        self, url: str, payload: dict[str, Any], check_robots: bool = True
    ) -> httpx.Response:
        return self._request("POST", url, check_robots=check_robots, json=payload)

    def require_allowed(self, url: str) -> None:
        """Raise RobotsDisallowedError if robots.txt disallows `url`. Fetches only robots.txt.

        For an adapter whose data comes from an endpoint robots.txt does not mention
        while the page it stands for is disallowed: the adapter names the page here
        before calling the endpoint, so the owner's rule is respected either way.
        """
        parts = urlsplit(url)
        host = parts.hostname or ""
        with self._lock_for(host):
            self._check_robots(parts.scheme, host, _path_of(url))

    def close(self) -> None:
        self._client.close()

    def _request(
        self, method: str, url: str, check_robots: bool = True, **kwargs: Any
    ) -> httpx.Response:
        """Make a request, following redirects one checked hop at a time."""
        for _ in range(MAX_REDIRECTS + 1):
            response = self._send(method, url, check_robots, **kwargs)
            if response.status_code not in REDIRECT_STATUS or method != "GET":
                response.raise_for_status()  # a redirected POST is an error, not followed
                return response
            # The next hop goes through _send like any request, so a redirect to a
            # never-scrape host or into a path robots.txt disallows is refused there.
            url = str(response.url.join(response.headers.get("Location", "")))
        raise httpx.TooManyRedirects(
            f"more than {MAX_REDIRECTS} redirects", request=response.request
        )

    def _send(self, method: str, url: str, check_robots: bool, **kwargs: Any) -> httpx.Response:
        """One request to one URL: host guard, robots.txt, spacing and retries."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        if any(host == domain or host.endswith("." + domain) for domain in BLOCKED_DOMAINS):
            raise BlockedHostError(f"refusing to fetch {host}: on the never-scrape list")

        lock = self._lock_for(host)
        holding = self._hosts_being_resolved()
        if host in holding:
            raise RobotsDisallowedError(f"a robots.txt redirect loops back to {host}")
        # Normally wait for the host's turn however long it takes. While this thread
        # holds another host's lock (it is following that host's robots.txt redirect),
        # wait only so long, so that two hosts pointing at each other cannot deadlock.
        if not lock.acquire(timeout=ROBOTS_REDIRECT_LOCK_SECONDS if holding else -1):
            raise RobotsDisallowedError(f"gave up waiting for {host} while reading a robots.txt")
        try:
            if check_robots:
                self._check_robots(parts.scheme, host, _path_of(url))
            return self._attempt(method, url, host, **kwargs)
        finally:
            lock.release()

    def _attempt(self, method: str, url: str, host: str, **kwargs: Any) -> httpx.Response:
        """Send with retries. The caller holds the host's lock.

        Returns whatever the last attempt returned, error statuses included; a network
        failure on the last attempt is raised.
        """
        for attempt in range(self._retries + 1):
            last_attempt = attempt == self._retries
            self._wait_for_turn(host)
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError:  # timeouts, connection resets, DNS failures
                if last_attempt:
                    raise
                self._sleep(_backoff(attempt))
                continue
            finally:
                self._last_request_at[host] = self._clock()

            if response.status_code in RETRYABLE_STATUS and not last_attempt:
                asked = _retry_after(response)
                if asked is not None and asked > MAX_RETRY_WAIT_SECONDS:
                    # Told to stay away for longer than a run can wait. Coming back
                    # sooner would ignore the request, so this is the answer for now.
                    return response
                self._sleep(asked if asked is not None else _backoff(attempt))
                continue
            return response
        raise AssertionError("unreachable: the loop always returns or raises")

    # --- robots.txt --------------------------------------------------------------------

    def _check_robots(self, scheme: str, host: str, path: str) -> None:
        """Raise unless the host's robots.txt allows `path`. The caller holds the lock."""
        rule = self._rules_for(scheme, host).rule_for(path)
        if rule is not None and not rule.allow:
            raise RobotsDisallowedError(
                f"robots.txt on {host} disallows {path.split('?')[0]} (Disallow: {rule.pattern})"
            )

    def _rules_for(self, scheme: str, host: str) -> robots.RobotsRules:
        """This host's robots.txt, fetched on first use. The caller holds the lock.

        Following RFC 9309: a 4xx answer means there is no robots.txt and nothing is
        restricted (also when it is where a checked redirect ends, see _fetch_robots);
        a 5xx answer, a 429 or a network failure means the rules are unknown, and then
        nothing is fetched from the host for the rest of the run.
        """
        if host in self._robots:
            return self._robots[host]
        if host not in self._robots_errors:
            problem = None
            try:
                response = self._fetch_robots(f"{scheme}://{host}/robots.txt", host)
            except httpx.TransportError as exc:
                problem = type(exc).__name__
            else:
                status = response.status_code
                if response.is_success and response.url.path == "/robots.txt":
                    rules = robots.parse(response.text, self._agent)
                elif 400 <= status < 500 and status != 429:
                    rules = robots.ALLOW_ALL
                elif status in REDIRECT_STATUS and host in self._no_robots_hosts:
                    # Checked by hand and recorded in settings.yaml: this host sends
                    # /robots.txt to an ordinary page because it has no such file.
                    rules = robots.ALLOW_ALL
                elif status in REDIRECT_STATUS or response.is_success:
                    # Still a redirect, or a page that is not a robots.txt at the end of one.
                    problem = "it redirects somewhere that is not a robots.txt"
                else:
                    problem = f"HTTP {status}"
            if problem:
                self._robots_errors[host] = f"robots.txt on {host} could not be read ({problem})"
            elif (rules.crawl_delay or 0) > MAX_CRAWL_DELAY_SECONDS:
                self._robots_errors[host] = (
                    f"robots.txt on {host} asks for {rules.crawl_delay:g}s between requests, "
                    "longer than a run can honour"
                )
            else:
                self._robots[host] = rules
                return rules
        raise RobotsDisallowedError(self._robots_errors[host])

    def _fetch_robots(self, url: str, host: str) -> httpx.Response:
        """GET a robots.txt, following only the redirects that can be checked.

        Sites commonly redirect /robots.txt to the same file on their www host, and
        that is followed. A redirect to any other page on the *same* host is not: with
        the host's rules still unread there is no telling whether that page is allowed
        (tal.net sends its root to a disallowed login page). The redirect response is
        returned as it is, and the caller treats the rules as unreadable.

        A redirect to a page on *another* host can be checked, against that host's own
        robots.txt, so it is followed (see _follow_robots_redirect). The exception is a
        host listed in settings.yaml as having no robots.txt: its redirect is known to
        lead to an ordinary page, so it is not followed, and the caller reads it as "no
        rules". A real robots.txt on such a host (a 200) is still read and obeyed.
        """
        for _ in range(MAX_REDIRECTS):
            response = self._attempt("GET", url, host, headers=ROBOTS_HEADERS)
            if response.status_code not in REDIRECT_STATUS:
                return response
            target = response.url.join(response.headers.get("Location", ""))
            if target.path != "/robots.txt":
                if target.host == host or host in self._no_robots_hosts:
                    return response
                return self._follow_robots_redirect(str(target), host) or response
            url = str(target)
        return response

    def _follow_robots_redirect(self, url: str, host: str) -> httpx.Response | None:
        """Follow `host`'s robots.txt redirect to a page on another host.

        RFC 9309 has a crawler follow robots.txt redirects, across hosts too, and take
        where they end as the answer: a "not found" there means the host has no
        robots.txt. jobs.apple.com does exactly this (its /robots.txt redirects to
        apple.com's not-found page). Every hop goes through the ordinary checked
        request, so it is made only if the host it lands on allows it.

        Returns the final response, or None when the redirect could not be followed.
        Only a 4xx at the end is then taken to mean "no robots.txt"; any other page is
        not a robots.txt and leaves the rules unreadable.
        """
        holding = self._hosts_being_resolved()
        if holding:
            return None  # already inside one such redirect: no chains of them
        holding.add(host)
        try:
            return self._request("GET", url)
        except httpx.HTTPStatusError as exc:
            return exc.response
        except (RobotsDisallowedError, BlockedHostError, httpx.HTTPError):
            return None
        finally:
            holding.discard(host)

    def _hosts_being_resolved(self) -> set[str]:
        if not hasattr(self._resolving, "hosts"):
            self._resolving.hosts = set()
        return self._resolving.hosts

    def _lock_for(self, host: str) -> threading.Lock:
        with self._host_locks_guard:
            return self._host_locks.setdefault(host, threading.Lock())

    def _wait_for_turn(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is None:
            return
        # The configured gap (for every host, or a longer one set for this host), or the
        # host's own Crawl-delay when that is longer still.
        known = self._robots.get(host)
        delay = max(
            self._delay,
            self._host_delays.get(host, 0.0),
            (known.crawl_delay or 0.0) if known else 0.0,
        )
        remaining = delay - (self._clock() - last)
        if remaining > 0:
            self._sleep(remaining)


def _with_params(url: str, params: dict[str, Any] | None) -> str:
    """The full URL including its query, so robots.txt rules can see the query too."""
    return str(httpx.URL(url, params=params)) if params else url


def _path_of(url: str) -> str:
    """What robots.txt rules are matched against: the path, plus ?query when present."""
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _backoff(attempt: int) -> float:
    return float(2**attempt)  # 1s, 2s, 4s ...


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the server asked us to wait, when it said so as a number."""
    value = response.headers.get("Retry-After", "")
    return float(value) if value.isdigit() else None
