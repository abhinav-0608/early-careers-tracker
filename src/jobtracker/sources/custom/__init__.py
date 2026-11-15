"""Custom adapters: one module per company whose careers site is its own system.

A company with `ats: custom` names its module in `board`. Every module has the same
shape as the ATS adapters (`fetch` and `parse`) and goes through the same HttpClient, so
robots.txt, crawl delay and request spacing apply to it exactly as they do elsewhere.

A custom company whose module is not written yet is not polled.
"""

from __future__ import annotations

from collections.abc import Callable

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources.custom import google, jpmorgan

NAME = "custom"

MODULES: dict[str, Callable[[Company, HttpClient], list[Job]]] = {
    "google": google.fetch,
    "jpmorgan": jpmorgan.fetch,
}
