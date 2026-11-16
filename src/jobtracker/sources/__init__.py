"""Source adapters: one module per source type.

Every adapter exposes two functions:

    fetch(company, http) -> list[Job]   makes the HTTP request(s)
    parse(company, payload) -> list[Job]   pure translation, tested against fixtures

To add a source, write the module and register its fetch function here. A company whose
careers site is its own system gets a module under custom/ instead, registered there.
"""

from __future__ import annotations

from collections.abc import Callable

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources import (
    ashby,
    custom,
    eightfold,
    greenhouse,
    lever,
    smartrecruiters,
    talnet,
    workable,
    workday,
)

Adapter = Callable[[Company, HttpClient], list[Job]]

ADAPTERS: dict[str, Adapter] = {
    greenhouse.NAME: greenhouse.fetch,
    lever.NAME: lever.fetch,
    ashby.NAME: ashby.fetch,
    workday.NAME: workday.fetch,
    talnet.NAME: talnet.fetch,
    smartrecruiters.NAME: smartrecruiters.fetch,
    workable.NAME: workable.fetch,
    eightfold.NAME: eightfold.fetch,
}


def adapter_for(company: Company) -> Adapter | None:
    """The function that fetches this company, or None if it cannot be polled.

    None covers `ats: unsupported`, a source type with no adapter yet, and a custom
    company whose module has not been written.
    """
    if company.ats == custom.NAME:
        return custom.MODULES.get(company.board or "")
    return ADAPTERS.get(company.ats)
