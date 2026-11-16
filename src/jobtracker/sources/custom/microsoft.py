"""Microsoft: the search API behind apply.careers.microsoft.com (an Eightfold site).

The site is Eightfold on Microsoft's own hostname, read by the generic Eightfold
adapter (sources/eightfold.py: the API, robots.txt and the intern-level filter are
described there). It stays a custom source so that its stored job keys
("custom:microsoft:<id>") are unchanged.

44 intern-level roles worldwide when this was written: 5 requests. Graduate ("Entry"
level) roles are not seen.
"""

from __future__ import annotations

from typing import Any

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources import eightfold

SOURCE = "custom"
HOSTNAME = "apply.careers.microsoft.com"
HOST = f"https://{HOSTNAME}"
DOMAIN = "microsoft.com"


def fetch(company: Company, http: HttpClient) -> list[Job]:
    return eightfold.search(company, http, host=HOSTNAME, domain=DOMAIN, source=SOURCE)


def parse(company: Company, payload: dict[str, Any]) -> list[Job]:
    return eightfold.parse(company, payload, host=HOSTNAME, source=SOURCE)
