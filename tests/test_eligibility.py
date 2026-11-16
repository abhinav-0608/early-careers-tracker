"""The first-year eligibility check, against the rules that ship in filters.yaml."""

import pytest

from jobtracker.eligibility import (
    DESCRIPTION,
    ELIGIBLE,
    INELIGIBLE,
    NOT_APPLICABLE,
    PROGRAMME,
    TITLE,
    UNCLEAR,
    UNKNOWN,
    assess,
    label,
    needs_description,
)


@pytest.fixture
def role(filters):
    return lambda name: filters.role_type(name)


def check(filters, role_type, title, description=None):
    return assess(filters.role_type(role_type), title, description, filters)


# --- the title settles it --------------------------------------------------------------


@pytest.mark.parametrize(
    ("role_type", "title", "evidence"),
    [
        ("first_year", "First Year Software Engineering Programme", "First Year"),
        ("first_year", "First-Year Technology Programme", "First-Year"),
        ("first_year", "Explore Microsoft", "Explore Microsoft"),
        ("first_year", "STEP Intern 2027", "STEP Intern"),
        ("summer_internship", "Summer Internship for 1st Year Students", "1st Year"),
        ("summer_internship", "Software Engineer Intern (Freshman / Sophomore)", "Freshman"),
        ("spring_week", "Spring Week 2027 - open to all year groups", "all year groups"),
    ],
)
def test_title_alone_makes_it_eligible(filters, role_type, title, evidence):
    verdict = check(filters, role_type, title)
    assert (verdict.status, verdict.source, verdict.evidence) == (ELIGIBLE, TITLE, evidence)
    assert not needs_description(filters.role_type(role_type), title, filters)
    assert verdict.basis == f'title: "{evidence}"'


def test_a_first_year_scheme_that_names_later_years_in_its_title_is_not_settled_by_it(filters):
    title = "Discovery Programme for Penultimate Year Students"
    assert needs_description(filters.role_type("first_year"), title, filters)
    assert check(filters, "first_year", title, "").status == INELIGIBLE


# --- the description settles it --------------------------------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "This programme is open to first and second year students.",
        "You must be in your first year of a three-year degree.",
        "We welcome students from any year of study. Apply early!",
        "Eligibility: 1st or 2nd year undergraduates",
        "Open to first, second or penultimate year students.",
        # Both kinds of phrase: first years are offered, so first years win.
        "Open to first year and penultimate year students.",
    ],
)
def test_internship_whose_description_offers_it_to_first_years(filters, description):
    verdict = check(filters, "summer_internship", "Software Engineer Intern", description)
    assert (verdict.status, verdict.source) == (ELIGIBLE, DESCRIPTION)
    assert verdict.basis.startswith('description: "')


@pytest.mark.parametrize(
    "description",
    [
        "Candidates must be in their penultimate year.",
        "Open to students in their final year of study.",
        "Rising seniors and juniors are encouraged to apply.",
        # A negated first-year phrase is not an offer.
        "This role is not open to first year students. Penultimate years only.",
    ],
)
def test_internship_restricted_to_later_years(filters, description):
    verdict = check(filters, "summer_internship", "Software Engineer Intern", description)
    assert verdict.status == INELIGIBLE
    assert verdict.reason.startswith("later years only: ")


def test_internship_that_does_not_say_is_unclear_and_one_not_yet_read_is_unknown(filters):
    title = "Software Engineer Intern"
    unclear = check(filters, "summer_internship", title, "A great summer with our team.")
    assert unclear.status == UNCLEAR
    assert unclear.reason == "the posting does not say first years can apply"
    assert check(filters, "summer_internship", title, None).status == UNKNOWN
    assert needs_description(filters.role_type("summer_internship"), title, filters)


def test_spring_week_is_eligible_unless_its_description_restricts_it(filters):
    title = "Spring Insight Programme 2027"
    plain = check(filters, "spring_week", title, "Spend a week with our technology teams.")
    assert (plain.status, plain.source) == (ELIGIBLE, PROGRAMME)
    assert plain.basis == "spring week; the posting does not say otherwise"

    not_read = check(filters, "spring_week", title, None)
    assert (not_read.status, not_read.basis) == (ELIGIBLE, "spring week; description not read")

    restricted = check(filters, "spring_week", title, "Open to penultimate year students only.")
    assert restricted.status == INELIGIBLE


def test_a_negation_far_before_the_phrase_does_not_count(filters):
    description = (
        "No prior experience is needed, and we would love to hear from you if you are a "
        "first year student."
    )
    verdict = check(filters, "summer_internship", "Software Engineer Intern", description)
    assert verdict.status == ELIGIBLE


def test_long_evidence_is_shortened_around_the_phrase(filters):
    description = "word " * 60 + "open to first year students " + "word " * 60
    verdict = check(filters, "summer_internship", "Software Engineer Intern", description)
    assert verdict.status == ELIGIBLE
    assert "first year" in verdict.evidence
    assert len(verdict.evidence) <= 92  # 90 plus the ellipses


# --- role types the check does not cover ------------------------------------------------


def test_a_role_type_in_no_list_is_not_checked(filters):
    graduate = filters.role_type("graduate")
    assert not needs_description(graduate, "Graduate Software Engineer", filters)
    verdict = assess(graduate, "Graduate Software Engineer", None, filters)
    assert (verdict.status, verdict.basis) == (NOT_APPLICABLE, "")


def test_label():
    assert label('title: "First Year"') == '✅ First-year eligible (title: "First Year")'
    assert label("") == ""
