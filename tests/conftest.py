"""Shared test helpers. Nothing in the test suite touches the network."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobtracker.config import Config, Filters, load_config
from jobtracker.filters import Decision
from jobtracker.models import Company, Job

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

# A decision to hand to the reconciler when a test is not about filtering.
MATCHED = Decision(True, "test", role_type="spring_week", fields=("software",), region="uk")


def load_fixture(relative_path: str):
    return json.loads((FIXTURES / relative_path).read_text(encoding="utf-8"))


def make_company(key: str = "acme", ats: str = "greenhouse", **overrides) -> Company:
    values = {
        "key": key,
        "name": key.title(),
        "industry": "quant",
        "tier": "normal",
        "ats": ats,
        "board": key,
    }
    return Company(**{**values, **overrides})


def make_job(job_id: str = "1", title: str = "Spring Week 2027", **overrides) -> Job:
    values = {
        "source": "greenhouse",
        "company": "acme",
        "job_id": job_id,
        "title": title,
        "url": f"https://example.com/jobs/{job_id}",
        "locations": ("London, UK",),
    }
    return Job(**{**values, **overrides})


def enable_all_role_types(filters: Filters) -> Filters:
    return replace(filters, role_types=tuple(replace(r, enabled=True) for r in filters.role_types))


@pytest.fixture(scope="session")
def config() -> Config:
    """The real config files, so the tests exercise the rules that actually ship."""
    return load_config(ROOT / "config")


@pytest.fixture(scope="session")
def filters(config: Config) -> Filters:
    return config.filters


@pytest.fixture(scope="session")
def settings(config: Config):
    # No waiting between requests and no retries unless a test asks for them.
    return replace(config.settings, per_host_delay_seconds=0, retries=0)
