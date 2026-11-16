"""The one HTTP client every adapter and the notifier use.

Politeness rules live here so that no adapter can forget them:

  - an honest User-Agent and a timeout on every request;
  - robots.txt is read once per host and checked before every fetch. A path it
    disallows is never requested, and that includes the target of a redirect: redirects
    are followed one hop at a time, each hop checked like any other request. A host's
    rules are remembered between runs for a day (state/hosts.json), so robots.txt is
    not re-fetched on every run;
  - a minimum gap between requests to the same host, raised to the host's Crawl-delay
    when its robots.txt sets one, or to a longer gap configured for that host;
  - a small number of retries on server errors and timeouts;
  - a 429 ("too many requests") is never retried blind. A short Retry-After is waited
    out; otherwise the host is left alone, for the rest of this run and for later runs
    until the time it asked for (or a growing back-off when it named none);
  - hosts that are really one server (Workday's tenants on "wd5") share one queue, one
    gap and one back-off;
  - a hard refusal of hosts we never scrape.

The notifier is the one caller that skips the robots.txt check: publishing a message to
ntfy's API is not crawling, and ntfy has no say over what the tracker reads elsewhere.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobtracker import robots
from jobtracker.config import Settings
from jobtracker.state import HostsState

# Never fetched, whatever a config file says.
BLOCKED_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.co.uk")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_WAIT_SECONDS = 30.0  # the longest Retry-After that is waited out inside a run

# After a 429 the host is left alone until the time its Retry-After names. When it names
# none: half an hour, doubling with each further 429 in a row.
BACKOFF_FIRST = timedelta(minutes=30)
BACKOFF_MAX = timedelta(hours=6)
RETRY_AFTER_MAX = timedelta(hours=24)
# A run that starts a little early must not miss the end of a back-off by seconds.
BACKOFF_TOLERANCE = timedelta(minutes=2)

# Cached robots.txt rules older than the configured age are re-fetched. If that fetch
# fails for a passing reason (server error, timeout) the old copy is used, as RFC 9309
# allows, but never one older than this.
ROBOTS_MAX_STALE = timedelta(days=7)

# Hostnames under these domains are tenants of a few shared servers: everything after
# the tenant's own label names the server ("acme.wd5.myworkdayjobs.com" is on
# "wd5.myworkdayjobs.com"). Requests are queued, spaced and backed off per server.
SHARED_HOST_DOMAINS = ("myworkdayjobs.com",)

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


class RateLimitedError(Exception):
    """The host answered 429, or did so recently and is being left alone.

    `answered_now` tells the two apart: True when a request was made and refused this
    time, False when no request was made because the host is still backing off.
    """

    def __init__(self, message: str, answered_now: bool):
        super().__init__(message)
        self.answered_now = answered_now


def shared_host(host: str) -> str:
    """The server a hostname is served from, when many hostnames share one.

    "acme.wd5.myworkdayjobs.com" -> "wd5.myworkdayjobs.com"; any other host -> itself.
    """
    for domain in SHARED_HOST_DOMAINS:
        if host.endswith("." + domain):
            server = host[: -len(domain) - 1].split(".")[-1]
            return f"{server}.{domain}"
    return host


class HttpClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        hosts: HostsState | None = None,
        now: datetime | None = None,
    ):
        # What is remembered about hosts between runs: cached robots.txt rules and
        # back-offs. The caller loads it from state and saves hosts_state() afterwards;
        # without one the client simply starts with no memory.
        self._hosts = hosts if hosts is not None else HostsState()
        self._now = now or datetime.now(UTC)
        # Servers that answered 429 during this run: nothing more is asked of them in it.
        self._limited_now: dict[str, str] = {}
        self._robots_cache = timedelta(hours=settings.robots_cache_hours)
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
        # One lock per host (per shared server, for tenants of one): different hosts are
        # fetched in parallel, but requests to the same host queue up and are spaced out.
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
        with self._lock_for(shared_host(host)):
            self._refuse_if_backing_off(host)
            self._check_robots(parts.scheme, host, _path_of(url))

    def hosts_state(self) -> HostsState:
        """What to remember about hosts until the next run, with old entries dropped."""
        self._hosts.robots = {
            host: entry
            for host, entry in self._hosts.robots.items()
            if self._now - datetime.fromisoformat(entry["fetched_at"]) <= ROBOTS_MAX_STALE
        }
        # A back-off that ended long ago and was never tested again (its source was
        # removed, say) is forgotten; one still running, or recent, keeps its strikes.
        self._hosts.backoff = {
            host: entry
            for host, entry in self._hosts.backoff.items()
            if self._now - datetime.fromisoformat(entry["retry_at"]) <= RETRY_AFTER_MAX
        }
        return self._hosts

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

        server = shared_host(host)
        lock = self._lock_for(server)
        holding = self._hosts_being_resolved()
        if server in holding:
            raise RobotsDisallowedError(f"a robots.txt redirect loops back to {host}")
        # Normally wait for the host's turn however long it takes. While this thread
        # holds another host's lock (it is following that host's robots.txt redirect),
        # wait only so long, so that two hosts pointing at each other cannot deadlock.
        if not lock.acquire(timeout=ROBOTS_REDIRECT_LOCK_SECONDS if holding else -1):
            raise RobotsDisallowedError(f"gave up waiting for {host} while reading a robots.txt")
        try:
            if not check_robots:
                # Publishing to ntfy: not crawling, so no robots.txt and no back-off.
                return self._attempt(method, url, host, crawling=False, **kwargs)
            self._refuse_if_backing_off(host)
            self._check_robots(parts.scheme, host, _path_of(url))
            try:
                response = self._attempt(method, url, host, **kwargs)
            except (httpx.TransportError, RateLimitedError):
                self._hosts.robots.pop(host, None)
                raise
            if response.status_code >= 400:
                # A failed fetch may mean the site's rules have changed, so its
                # robots.txt is read again next run instead of waiting out the cache.
                self._hosts.robots.pop(host, None)
            return response
        finally:
            lock.release()

    def _attempt(
        self, method: str, url: str, host: str, crawling: bool = True, **kwargs: Any
    ) -> httpx.Response:
        """Send with retries. The caller holds the host's lock.

        Returns whatever the last attempt returned, error statuses included; a network
        failure on the last attempt is raised. When crawling, a 429 that cannot be
        waited out starts a back-off for the host and raises RateLimitedError.
        """
        server = shared_host(host)
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
                self._last_request_at[server] = self._clock()

            asked = _retry_after(response)
            if response.status_code == 429 and crawling:
                if asked is not None and asked <= MAX_RETRY_WAIT_SECONDS and not last_attempt:
                    self._sleep(asked)  # "in a few seconds": wait, then try once more
                    continue
                # No time named, or one a run cannot wait for. Asking again now would
                # be ignoring the answer, so the host is left alone instead.
                raise self._back_off(host, response, asked)
            if response.status_code in RETRYABLE_STATUS and not last_attempt:
                if asked is not None and asked > MAX_RETRY_WAIT_SECONDS:
                    return response
                self._sleep(asked if asked is not None else _backoff(attempt))
                continue
            if crawling and response.status_code < 400:
                self._hosts.backoff.pop(server, None)  # answering again: strikes forgotten
            return response
        raise AssertionError("unreachable: the loop always returns or raises")

    # --- back-off after a 429 -----------------------------------------------------------

    def _back_off(
        self, host: str, response: httpx.Response, asked: float | None
    ) -> RateLimitedError:
        """Record that `host` answered 429 and return the error to raise.

        The host (the shared server, for a tenant of one) is not asked again until the
        time its Retry-After named, or, when it named none, for half an hour, doubling
        with every further 429 in a row. That holds for the rest of this run and, through
        state/hosts.json, for the runs after it.
        """
        server = shared_host(host)
        strikes = self._hosts.backoff.get(server, {}).get("strikes", 0) + 1
        if asked is not None:
            wait = min(timedelta(seconds=asked), RETRY_AFTER_MAX)
        else:
            wait = min(BACKOFF_FIRST * 2 ** (strikes - 1), BACKOFF_MAX)
        retry_at = (self._now + wait).replace(microsecond=0)
        self._hosts.backoff[server] = {"retry_at": retry_at.isoformat(), "strikes": strikes}
        # Cloudflare marks a 429 that is its bot challenge, not a count of requests.
        challenge = response.headers.get("cf-mitigated") == "challenge"
        kind = "HTTP 429, a Cloudflare challenge" if challenge else "HTTP 429, rate limited"
        self._limited_now[server] = f"{kind}; next attempt after {retry_at:%Y-%m-%d %H:%M} UTC"
        return RateLimitedError(f"{host}: {self._limited_now[server]}", answered_now=True)

    def _refuse_if_backing_off(self, host: str) -> None:
        """Raise, without making a request, while `host` is being left alone.

        A server that answered 429 earlier in this run failed this run, for every
        tenant on it; one still inside a back-off from an earlier run was not asked.
        """
        server = shared_host(host)
        if server in self._limited_now:
            raise RateLimitedError(f"{server}: {self._limited_now[server]}", answered_now=True)
        entry = self._hosts.backoff.get(server)
        if not entry:
            return
        retry_at = datetime.fromisoformat(entry["retry_at"])
        if self._now < retry_at - BACKOFF_TOLERANCE:
            raise RateLimitedError(
                f"{server} answered HTTP 429 in an earlier run; not asked again until "
                f"{retry_at:%Y-%m-%d %H:%M} UTC",
                answered_now=False,
            )

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
        a 5xx answer or a network failure means the rules are unknown, and then the
        last copy read in the past week is used or, without one, nothing is fetched
        from the host for the rest of the run. A 429 starts a back-off (_back_off).

        Rules read less than `http.robots_cache_hours` ago, in this run or an earlier
        one, are used without asking again.
        """
        if host in self._robots:
            return self._robots[host]
        cached = self._cached_robots(host)
        if cached and cached[1] < self._robots_cache:
            self._robots[host] = cached[0]  # read less than a day ago: not fetched again
            return cached[0]
        if host not in self._robots_errors:
            problem = None
            transient = False
            try:
                response = self._fetch_robots(f"{scheme}://{host}/robots.txt", host)
            except httpx.TransportError as exc:
                problem, transient = type(exc).__name__, True
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
                    problem, transient = f"HTTP {status}", True
            if problem and transient and cached:
                # The file could not be re-read just now. The last copy, at most a week
                # old, is better than fetching nothing and better than guessing.
                self._robots[host] = cached[0]
                return cached[0]
            if problem:
                self._robots_errors[host] = f"robots.txt on {host} could not be read ({problem})"
            elif (rules.crawl_delay or 0) > MAX_CRAWL_DELAY_SECONDS:
                self._robots_errors[host] = (
                    f"robots.txt on {host} asks for {rules.crawl_delay:g}s between requests, "
                    "longer than a run can honour"
                )
            else:
                self._robots[host] = rules
                self._hosts.robots[host] = {
                    "fetched_at": self._now.replace(second=0, microsecond=0).isoformat(),
                    "rules": rules.to_lines(),
                    "crawl_delay": rules.crawl_delay,
                }
                return rules
        raise RobotsDisallowedError(self._robots_errors[host])

    def _cached_robots(self, host: str) -> tuple[robots.RobotsRules, timedelta] | None:
        """The rules remembered for `host` and their age, if any are still usable."""
        entry = self._hosts.robots.get(host)
        if not entry:
            return None
        try:
            age = self._now - datetime.fromisoformat(entry["fetched_at"])
            rules = robots.RobotsRules.from_lines(entry["rules"], entry.get("crawl_delay"))
        except (KeyError, ValueError, TypeError):
            return None  # a damaged entry is just a cache miss
        return (rules, age) if age <= ROBOTS_MAX_STALE else None

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
        holding.add(shared_host(host))
        try:
            return self._request("GET", url)
        except httpx.HTTPStatusError as exc:
            return exc.response
        except (RobotsDisallowedError, RateLimitedError, BlockedHostError, httpx.HTTPError):
            return None
        finally:
            holding.discard(shared_host(host))

    def _hosts_being_resolved(self) -> set[str]:
        if not hasattr(self._resolving, "hosts"):
            self._resolving.hosts = set()
        return self._resolving.hosts

    def _lock_for(self, host: str) -> threading.Lock:
        with self._host_locks_guard:
            return self._host_locks.setdefault(host, threading.Lock())

    def _wait_for_turn(self, host: str) -> None:
        # Timed per server: every tenant of a shared server waits on the same clock.
        server = shared_host(host)
        last = self._last_request_at.get(server)
        if last is None:
            return
        # The configured gap (for every host, or a longer one set for this host), or the
        # host's own Crawl-delay when that is longer still.
        known = self._robots.get(host)
        delay = max(
            self._delay,
            self._host_delays.get(host, 0.0),
            self._host_delays.get(server, 0.0),
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
