"""Decide whether a job matches, and say why.

A job must pass three independent checks, in this order: role type (from the title),
field (title plus department) and location. Every decision carries a human-readable
reason, which is what `python -m jobtracker explain` prints.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobtracker.config import Filters, RoleType
from jobtracker.models import Company, Job
from jobtracker.text import normalise

# Region given to a job whose location we could not place anywhere. Such jobs pass the
# filter (a missed alert is worse than an irrelevant one) and are labelled as unclear.
UNCLEAR = "unclear"


@dataclass(frozen=True)
class Decision:
    matched: bool
    reason: str
    role_type: str | None = None
    fields: tuple[str, ...] = ()
    region: str | None = None


def classify(job: Job, company: Company, filters: Filters) -> Decision:
    title = normalise(job.title)

    excluded = filters.global_exclude.find(title)
    if excluded:
        return Decision(False, f"excluded: title contains '{excluded}'")

    role = match_role_type(title, filters)
    if role is None:
        return Decision(False, "role: no role type matched")
    if not role.enabled:
        return Decision(False, f"role: '{role.name}' is disabled", role_type=role.name)

    fields, field_reason = match_fields(title, normalise(job.department), role, filters)
    if fields is None:
        return Decision(False, field_reason, role_type=role.name)

    region, location_reason = match_location(job, company, filters)
    if region is None:
        return Decision(False, location_reason, role_type=role.name, fields=fields)

    return Decision(True, location_reason, role_type=role.name, fields=fields, region=region)


def match_role_type(title: str, filters: Filters) -> RoleType | None:
    """First role type, in config order, whose keywords fit the title.

    Disabled types take part too. Otherwise "Industrial Placement Intern" would slip
    past a disabled `placement` type and be picked up by the generic internship type.
    """
    for role in filters.role_types:
        if role.include.find(title) and not role.exclude.find(title):
            return role
    return None


def match_fields(
    title: str, department: str, role: RoleType, filters: Filters
) -> tuple[tuple[str, ...] | None, str]:
    """Return (fields, reason). Fields is None when the job fails the field check."""
    text = f"{title} {department}".strip()
    fields = tuple(
        rule.name
        for rule in filters.fields
        if rule.include.find(text) and not rule.exclude.find(text)
    )
    if fields:
        return fields, f"field: {', '.join(fields)}"
    if role.strict_field:
        return None, f"field: '{role.name}' needs a field keyword and none matched"

    other_discipline = filters.field_exclude.find(text)
    if other_discipline:
        return None, f"field: '{other_discipline}' is not one of your fields"
    return (), "field: none named (allowed for this role type)"


def match_location(job: Job, company: Company, filters: Filters) -> tuple[str | None, str]:
    """Return (region, reason). Region is None when no location is acceptable.

    A job passes if any one of its locations is acceptable. When several are, a real
    region beats "unclear", and regions are preferred in config order.
    """
    if not job.locations:
        return UNCLEAR, "location: none given"

    accepted: list[tuple[str, str]] = []
    rejected: list[str] = []
    for raw in job.locations:
        region, reason = classify_location(raw, job.remote_hint, company, filters)
        if region is None:
            rejected.append(reason)
        else:
            accepted.append((region, reason))

    if not accepted:
        return None, rejected[0]
    preference = [region.name for region in filters.regions] + [UNCLEAR]
    return min(accepted, key=lambda item: preference.index(item[0]))


def classify_location(
    raw: str, remote_hint: bool | None, company: Company, filters: Filters
) -> tuple[str | None, str]:
    """Place one location string in a region and apply that region's rules."""
    text = normalise(raw)
    is_remote = remote_hint is True or filters.remote_keywords.find(text) is not None
    mode = "remote" if is_remote else "onsite"

    regions = [
        region
        for region in filters.regions
        if region.match.find(text) and not region.exclude.find(text)
    ]
    if not regions:
        if filters.other_locations.find(text):
            return None, f"location: '{raw}' is outside your regions"
        return UNCLEAR, f"location: '{raw}' is unclear"

    rejections = []
    for region in regions:
        if mode not in region.allow:
            allowed = "/".join(sorted(region.allow))
            rejections.append(f"location: '{raw}' is {mode}, {region.name} allows {allowed} only")
        elif region.require_company_tags and not region.require_company_tags & set(company.tags):
            needed = ", ".join(sorted(region.require_company_tags))
            rejections.append(f"location: {region.name} needs a company tagged {needed}")
        else:
            return region.name, f"location: {region.name} ({mode})"
    return None, rejections[0]
