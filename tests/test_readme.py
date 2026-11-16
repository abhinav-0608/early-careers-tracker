"""The README's generated table of open roles."""

from dataclasses import replace
from datetime import date

import pytest

from conftest import make_company, make_profile
from jobtracker import readme
from jobtracker.state import CLOSED, ProfileState, StoredJob

TODAY = date(2026, 11, 16)


def stored(key: str, company: str, title: str, **overrides) -> StoredJob:
    values = {
        "key": key,
        "source": "greenhouse",
        "company": company,
        "title": title,
        "locations": ["London, UK"],
        "url": f"https://example.com/{key}",
        "role_type": "spring_week",
        "fields": ["software"],
        "region": "uk",
        "fingerprint": key,
        "first_seen": "2026-11-15T09:22:00+00:00",
        "last_seen": "2026-11-16",
    }
    return StoredJob(**{**values, **overrides})


@pytest.fixture
def world(config, profile):
    companies = (
        make_company("normalco", name="Normal Co"),
        make_company("fireco", name="Fire Co"),
        make_company("gone", name="Gone Ltd", ats="unsupported", note="needs a browser"),
        make_company("elsewhere", name="Elsewhere", industry="law", ats="unsupported", note="x"),
    )
    me = make_profile(profile, "me", industries=("quant",), faang_plus=("fireco",))
    return replace(config, companies=companies), me


def test_table_groups_by_region_then_role_type_with_faang_plus_first(world):
    config, me = world
    state = ProfileState(
        jobs={
            "a": stored("a", "normalco", "Spring Week 2027"),
            "b": stored("b", "fireco", "Spring Insight | Tech", eligibility='title: "x"'),
            "c": stored("c", "normalco", "Insight Day", role_type="insight"),
            "d": stored("d", "normalco", "US Week", region="us", locations=["NYC", "SF", "LA"]),
            "e": stored("e", "normalco", "Closed Week", status=CLOSED),
            "f": stored("f", "normalco", "Somewhere", region="unclear", locations=[]),
        }
    )
    text = readme.open_roles(state, me, config, TODAY)
    lines = text.splitlines()

    assert lines[0] == "## Open roles"
    headings = [line for line in lines if line.startswith("#")]
    assert headings == [
        "## Open roles",
        "### United Kingdom",
        "#### Spring week",
        "#### Insight programme",
        "### United States",
        "#### Spring week",
        "### Location unclear",
        "#### Spring week",
    ]
    rows = [line for line in lines if line.startswith("| ") and "---" not in line]
    assert rows[1] == (
        "| 🔥 **Fire Co** | Spring Insight / Tech ✅ | London, UK | 2026-11-15 "
        "| [Apply](https://example.com/b) |"
    )
    assert rows[2].startswith("| Normal Co | Spring Week 2027 |")
    assert "| NYC; SF +1 |" in text
    assert "| Unclear |" in text
    assert "Closed Week" not in text
    assert "✅ the posting says first-year students can apply." in lines
    assert "Last updated 2026-11-16." in lines
    # Only unsupported companies in this profile's scope are listed.
    assert "- **Gone Ltd**: needs a browser" in lines
    assert "Elsewhere" not in text
    assert "<summary>1 company in scope cannot be polled</summary>" in lines


def test_no_open_roles_says_so(world):
    config, me = world
    text = readme.open_roles(ProfileState(), me, config, TODAY)
    assert "No matching roles are open right now." in text
    assert "✅" not in text


def test_update_replaces_only_the_generated_part(tmp_path):
    path = tmp_path / "README.md"
    path.write_text(f"# Title\n\nHand-written.\n\n{readme.START}\nold\n{readme.END}\n\nFooter\n")
    assert readme.update(path, "## Open roles\nnew") is True
    assert path.read_text() == (
        f"# Title\n\nHand-written.\n\n{readme.START}\n## Open roles\nnew\n{readme.END}\n\nFooter\n"
    )
    # The same content again leaves the file alone, so an unchanged run commits nothing.
    assert readme.update(path, "## Open roles\nnew") is False


def test_update_adds_the_markers_at_the_end_when_missing(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("# Title\n\nHand-written.\n")
    readme.update(path, "table")
    assert path.read_text() == f"# Title\n\nHand-written.\n\n{readme.START}\ntable\n{readme.END}\n"
