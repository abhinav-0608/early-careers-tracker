import pytest

from jobtracker.text import KeywordSet, compile_keyword, normalise


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Spring-Week 2027 (Technology)", "spring week 2027 technology"),
        ("  First‑Year   Programme – UK ", "first year programme uk"),
        ("Zürich / São Paulo", "zurich sao paulo"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_keyword_matches_whole_words_only():
    pattern = compile_keyword("intern")
    assert pattern.search(normalise("Software Intern, London"))
    assert not pattern.search(normalise("Internal Audit"))
    assert not pattern.search(normalise("International Sales"))


def test_keyword_star_allows_any_ending_on_any_word():
    pattern = compile_keyword("insight* program*")
    assert pattern.search(normalise("Early Insights Programme 2027"))
    assert pattern.search(normalise("Insight Program"))
    assert not pattern.search(normalise("Insight into Programming"))


def test_keyword_is_normalised_like_the_text():
    assert compile_keyword("First-Year").search(normalise("first year analyst"))


def test_keyword_without_letters_is_rejected():
    with pytest.raises(ValueError):
        compile_keyword("***")


def test_keyword_set_returns_the_keyword_that_matched():
    keywords = KeywordSet(["spring week*", "insight day*"])
    assert keywords.find(normalise("Technology Insight Days")) == "insight day*"
    assert keywords.find(normalise("Summer Internship")) is None
