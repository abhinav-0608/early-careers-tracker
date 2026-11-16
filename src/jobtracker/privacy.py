"""Private companies: alerts to the owner only, nothing readable in public output.

A company marked `private: true` in companies.yaml is polled like any other, and its new
roles are sent to the profile's ntfy topic in full. Everything else the tracker produces
is public (the repository, its README, its committed state and its Actions logs), so for
these companies:

- each posting's id is replaced by a hash as soon as it is fetched, so job keys in state
  and logs say nothing;
- the dedupe fingerprint (company + title + locations) is stored as a hash;
- stored records keep no title, link, location or quoted posting text;
- log lines name the company but never the role, and the README leaves the roles out.

See DESIGN.md decision 32.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from jobtracker.models import Job

WITHHELD = "(private)"


def digest(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def pseudonymise(job: Job) -> Job:
    """The same posting with an opaque, stable id, marked private."""
    return replace(job, job_id=digest(job.job_id), private=True)


def redact_basis(basis: str) -> str:
    """'description: "all years welcome"' -> 'description: (private)'. The kind of
    evidence is kept, because the pipeline reads it; the quoted text is not."""
    kind, colon, _ = basis.partition(":")
    return f"{kind}: {WITHHELD}" if colon else WITHHELD
