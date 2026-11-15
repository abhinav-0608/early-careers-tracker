"""Text normalisation and keyword matching, shared by the filters and by dedupe."""

from __future__ import annotations

import re
import unicodedata

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def normalise(text: str | None) -> str:
    """Reduce text to lowercase words separated by single spaces.

    "Spring-Week 2027 (Zürich)" -> "spring week 2027 zurich". Accents are stripped,
    and every run of punctuation or whitespace becomes one space.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALPHANUMERIC.sub(" ", without_accents.lower()).strip()


def compile_keyword(keyword: str) -> re.Pattern[str]:
    """Turn a config keyword into a regex that runs against normalised text.

    The keyword matches whole words only. A trailing * on a word allows any ending, so
    "insight* program*" matches "insights programme".
    """
    parts = []
    for word in keyword.split():
        any_ending = word.endswith("*")
        normalised = normalise(word)
        if not normalised:
            raise ValueError(f"keyword {keyword!r} has no letters or digits in {word!r}")
        parts.append(re.escape(normalised) + ("[a-z0-9]*" if any_ending else ""))
    if not parts:
        raise ValueError("empty keyword")
    return re.compile(r"(?<![a-z0-9])" + " ".join(parts) + r"(?![a-z0-9])")


class KeywordSet:
    """A list of keywords that can be searched for in normalised text."""

    def __init__(self, keywords: list[str] | tuple[str, ...] = ()):
        self.keywords = tuple(keywords)
        self._patterns = [(keyword, compile_keyword(keyword)) for keyword in self.keywords]

    def find(self, normalised_text: str) -> str | None:
        """Return the first keyword found in the text, or None."""
        for keyword, pattern in self._patterns:
            if pattern.search(normalised_text):
                return keyword
        return None

    def __bool__(self) -> bool:
        return bool(self.keywords)
