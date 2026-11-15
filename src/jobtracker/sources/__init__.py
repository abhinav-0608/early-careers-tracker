"""Source adapters: one module per source type.

Every adapter exposes two functions:

    fetch(company, http) -> list[Job]   makes the HTTP request(s)
    parse(company, payload) -> list[Job]   pure translation, tested against fixtures

To add a source, write the module and register its fetch function here.
"""

from __future__ import annotations

from collections.abc import Callable

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources import ashby, greenhouse, lever

Adapter = Callable[[Company, HttpClient], list[Job]]

ADAPTERS: dict[str, Adapter] = {
    greenhouse.NAME: greenhouse.fetch,
    lever.NAME: lever.fetch,
    ashby.NAME: ashby.fetch,
}
