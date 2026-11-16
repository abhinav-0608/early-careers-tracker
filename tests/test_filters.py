"""Filter tests run against the real config/filters.yaml."""

from dataclasses import replace

import pytest

from conftest import enable_all_role_types, make_company, make_job
from jobtracker.filters import UNCLEAR, classify

COMPANY = make_company()


@pytest.mark.parametrize(
    ("title", "role_type"),
    [
        # Spring weeks
        ("Spring Week", "spring_week"),
        ("Spring Week 2027 - Technology", "spring_week"),
        ("Spring Insight Programme", "spring_week"),
        ("2027 Point72 Academy Spring Insight Programme – UK", "spring_week"),
        ("Spring Internship Programme 2027", "spring_week"),
        ("Spring Into Technology", "spring_week"),
        ("Spring Week – Global Markets", "spring_week"),
        ("Sales & Trading Spring Insight", "spring_week"),
        ("Quantitative Research Spring Week", "spring_week"),
        ("Technology Spring Week", "spring_week"),
        # Insight days and programmes
        ("Early Insight", "insight"),
        ("Early Insight Programme", "insight"),
        ("Technology Insight Day", "insight"),
        ("Engineering Insight Day", "insight"),
        ("Insights Week: Engineering", "insight"),
        ("Quantitative Trading Insight Programme", "insight"),
        ("Discovery Day", "insight"),
        ("Women in Technology Open Evening", "insight"),
        # First-year programmes, UK and US
        ("First Year Programme", "first_year"),
        ("First-Year Trading and Technology Programme", "first_year"),
        ("1st Year Software Engineering Internship", "first_year"),
        ("Freshman/Sophomore Discovery Program", "first_year"),
        ("Sophomore Summer Analyst", "first_year"),
        ("Student Training in Engineering Program (STEP) Intern, 2027", "first_year"),
        ("STEP Intern, First-Year", "first_year"),
        ("Explore Microsoft Internship", "first_year"),
        ("Software Engineering: Explore Internship", "first_year"),
        ("Discover Citadel", "first_year"),
    ],
)
def test_title_variants_that_must_match(filters, title, role_type):
    decision = classify(make_job(title=title), COMPANY, filters)
    assert decision.matched, decision.reason
    assert decision.role_type == role_type


@pytest.mark.parametrize(
    ("title", "reason_starts_with"),
    [
        ("Java Developer (Spring Boot)", "role: no role type"),
        ("Customer Insights Analyst", "role: no role type"),
        ("Software Engineer, Discovery", "role: no role type"),
        ("Internal Audit Associate", "role: no role type"),
        ("Senior Engineer, Spring Week Mentor", "excluded"),
        ("Early Insight Programme Manager", "excluded"),
        ("Campus Recruiter, Spring Week", "excluded"),
        ("Spring Week - Legal", "field"),
        ("Spring Week – Finance", "field: 'finance' is not one of your fields"),
        ("Insight Day: Human Resources", "field"),
        # Role types defined but not enabled yet.
        ("Graduate Software Engineer", "role: 'graduate' is disabled"),
        ("Industrial Placement - Software Engineering", "role: 'placement' is disabled"),
    ],
)
def test_titles_that_must_not_match(filters, title, reason_starts_with):
    decision = classify(make_job(title=title), COMPANY, filters)
    assert not decision.matched
    assert decision.reason.startswith(reason_starts_with), decision.reason


def test_disabled_specific_type_does_not_fall_through_to_a_generic_one(filters):
    # "Industrial Placement Intern" also contains "intern". With only internships
    # switched on it must still be treated as a placement, and so dropped.
    only_internships = replace(
        filters,
        role_types=tuple(
            replace(role, enabled=role.name == "summer_internship") for role in filters.role_types
        ),
    )
    decision = classify(
        make_job(title="Industrial Placement Intern - Software"), COMPANY, only_internships
    )
    assert not decision.matched
    assert decision.role_type == "placement"


def test_enabling_later_role_types_needs_no_code_change(filters):
    everything = enable_all_role_types(filters)
    expected = {
        "Software Engineer Intern": "summer_internship",
        "Quantitative Trading Summer Analyst": "summer_internship",
        "Industrial Placement - Software Engineering": "placement",
        "12-Month Cyber Security Placement": "placement",
        "Graduate Software Engineer": "graduate",
        "Machine Learning Engineer, New Grad": "graduate",
    }
    for title, role_type in expected.items():
        decision = classify(make_job(title=title), COMPANY, everything)
        assert decision.matched, f"{title}: {decision.reason}"
        assert decision.role_type == role_type


def test_strict_role_types_need_a_field(filters):
    everything = enable_all_role_types(filters)
    assert not classify(make_job(title="Marketing Intern"), COMPANY, everything).matched
    assert not classify(make_job(title="Mechanical Engineer Intern"), COMPANY, everything).matched
    assert classify(make_job(title="Data Science Intern"), COMPANY, everything).fields == ("data",)


def test_field_can_come_from_the_department(filters):
    everything = enable_all_role_types(filters)
    job = make_job(title="Summer Intern", department="Cyber Security")
    assert classify(job, COMPANY, everything).fields == ("security",)


def test_lenient_role_types_pass_with_no_field_named(filters):
    decision = classify(make_job(title="Spring Week 2027"), COMPANY, filters)
    assert decision.matched
    assert decision.fields == ()


def test_lenient_role_types_keep_mixed_discipline_titles(filters):
    decision = classify(make_job(title="Spring Week - Technology & Operations"), COMPANY, filters)
    assert decision.matched
    assert "software" in decision.fields


@pytest.mark.parametrize(
    ("locations", "remote_hint", "tags", "region"),
    [
        # UK: in person and remote
        (("London, UK",), None, (), "uk"),
        (("London, England, United Kingdom",), None, (), "uk"),
        (("Remote - UK",), None, (), "uk"),
        (("Cheltenham",), None, (), "uk"),
        # US: remote only
        (("New York, NY",), None, (), None),
        (("Chicago, Illinois, United States",), None, (), None),
        (("Remote-Friendly, United States",), None, (), "us"),
        (("San Francisco",), True, (), "us"),  # source flags the job as remote
        (("San Francisco",), False, (), None),
        # India: only for tagged companies
        (("Bengaluru, India",), None, (), None),
        (("Bengaluru, India",), None, ("yc",), "india"),
        (("Remote, India",), None, ("big_tech",), "india"),
        # Elsewhere
        (("Singapore",), None, (), None),
        (("Remote - Canada",), None, (), None),
        (("Amsterdam, North Holland, Netherlands",), None, (), None),
        # Lookalikes
        (("Cambridge",), None, (), "uk"),
        (("Cambridge, MA",), None, (), None),
        # One acceptable location is enough
        (("New York, NY", "London"), None, (), "uk"),
        (("London, Paris, Hong Kong, Tokyo",), None, (), "uk"),
        # Unclear locations pass, labelled as unclear
        ((), None, (), UNCLEAR),
        (("Multiple Locations",), None, (), UNCLEAR),
        (("Remote",), None, (), UNCLEAR),
        (("EMEA",), None, (), UNCLEAR),
    ],
)
def test_location_rules(filters, locations, remote_hint, tags, region):
    job = make_job(locations=locations, remote_hint=remote_hint)
    decision = classify(job, make_company(tags=tags), filters)
    assert decision.matched == (region is not None), decision.reason
    assert decision.region == region


def test_a_real_region_is_preferred_over_unclear(filters):
    job = make_job(locations=("Multiple Locations", "London"))
    assert classify(job, COMPANY, filters).region == "uk"


def test_every_decision_explains_itself(filters):
    rejected = classify(make_job(locations=("New York, NY",)), COMPANY, filters)
    assert rejected.reason == "location: 'New York, NY' is onsite, us allows remote only"
