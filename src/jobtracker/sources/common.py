"""Small parsing helpers shared by adapters."""

from __future__ import annotations

from datetime import UTC, date, datetime


def parse_iso_date(value: str | None) -> date | None:
    """Date part of an ISO 8601 timestamp such as "2026-11-03T10:18:06-05:00"."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def parse_epoch_ms(value: int | float | None) -> date | None:
    """Date of a Unix timestamp given in milliseconds."""
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).date()


def clean(values: list[str | None]) -> tuple[str, ...]:
    """Strip whitespace, drop blanks and duplicates, keep the original order."""
    seen: dict[str, None] = {}
    for value in values:
        if value and value.strip():
            seen.setdefault(value.strip())
    return tuple(seen)
