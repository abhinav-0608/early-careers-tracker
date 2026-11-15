"""The one HTTP client every adapter and the notifier use.

Politeness rules live here so that no adapter can forget them: an honest User-Agent,
a timeout on every request, a minimum gap between requests to the same host, a small
number of retries that honour Retry-After, and a hard refusal of hosts we never scrape.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobtracker.config import Settings

# Never fetched, whatever a config file says.
BLOCKED_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.co.uk")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_WAIT_SECONDS = 30.0


class BlockedHostError(Exception):
    """Raised when something tries to fetch a host on the never-scrape list."""


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
        self._sleep = sleep
        self._clock = clock
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        # One lock per host: different hosts are fetched in parallel, but requests to
        # the same host queue up and are spaced out.
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_locks_guard = threading.Lock()
        self._last_request_at: dict[str, float] = {}

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", url, params=params).json()

    def post_json(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        return self._request("POST", url, json=payload)

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        if any(host == domain or host.endswith("." + domain) for domain in BLOCKED_DOMAINS):
            raise BlockedHostError(f"refusing to fetch {host}: on the never-scrape list")

        with self._lock_for(host):
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
                    self._sleep(_retry_after(response) or _backoff(attempt))
                    continue
                response.raise_for_status()
                return response
        raise AssertionError("unreachable: the loop always returns or raises")

    def _lock_for(self, host: str) -> threading.Lock:
        with self._host_locks_guard:
            return self._host_locks.setdefault(host, threading.Lock())

    def _wait_for_turn(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is None:
            return
        remaining = self._delay - (self._clock() - last)
        if remaining > 0:
            self._sleep(remaining)


def _backoff(attempt: int) -> float:
    return float(2**attempt)  # 1s, 2s, 4s ...


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the server asked us to wait, capped so one source cannot stall the run."""
    value = response.headers.get("Retry-After", "")
    if not value.isdigit():
        return None
    return min(float(value), MAX_RETRY_WAIT_SECONDS)
