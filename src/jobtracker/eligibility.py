"""Is a posting open to first-year students?

The title filters decide what kind of programme a posting is. This module decides the
one thing a title often does not say: who may apply. It reads the title and, when one
was fetched, the description, against the `eligibility` section of a profile's
filters.yaml:

    first_year         phrases that say first years, or students of any year, may apply
    later_years_only   phrases that restrict a programme to later years
    by_title           role types that mean first years by themselves (first-year schemes)
    unless_restricted  role types aimed at first years anyway (spring weeks, insight
                       programmes): eligible unless the posting says later years only
    explicit_only      role types that alert only when the posting says first years may
                       apply (summer internships: most are for penultimate years)

An eligible posting that also names later years ("penultimate or final year ideal, though
all years are welcome to apply") carries a note saying so: "All years welcome · prefers
penultimate/final". It is still eligible; the note is shown beside the ✅.

The outcome is one of:

    eligible    the title or the description says so, or it is an `unless_restricted`
                programme and nothing in the posting says otherwise
    ineligible  the posting restricts itself to later years and never offers first years
    unclear     an `explicit_only` posting that does not say either way. Dropped.
    unknown     an `explicit_only` posting whose description could not be read this run
    n/a         the role type is in none of the lists (or the profile has no rules):
                nothing is checked, dropped or marked
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobtracker.config import Filters, RoleType
from jobtracker.text import KeywordSet, normalise

ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"
UNCLEAR = "unclear"
UNKNOWN = "unknown"
NOT_APPLICABLE = "n/a"
DROPPED = (INELIGIBLE, UNCLEAR)  # settled: the posting is not for first years

TITLE = "title"
DESCRIPTION = "description"
PROGRAMME = "programme"
REMEMBERED = "remembered"  # settled on an earlier run; `evidence` is the stored basis

MARK = "✅ First-year eligible"
MAX_EVIDENCE = 90

# A first-year phrase that closely follows one of these in the same sentence is being
# ruled out ("not open to first year students"), not offered.
_NEGATIONS = KeywordSet(
    [
        "not",
        "no",
        "cannot",
        "can t",
        "aren t",
        "isn t",
        "except",
        "excluding",
        "unless",
        "ineligible",
        "unable",
        "unfortunately",
        "rather than",
        "other than",
    ]
)
_NEGATION_REACH = 8  # words before the phrase in which a negation counts
# A first-year phrase with one of these words welcomes every year ("all years are
# welcome"), not just first years ("first or second year").
_ANY_YEAR = KeywordSet(["all", "any", "regardless", "no matter"])
# A later-years phrase in an eligible posting is a preference only when one of these is
# close to it ("penultimate or final year ideal", "final years preferred"). Without one,
# "open to first year and penultimate year students" is an equal offer, not a preference.
_PREFERENCE = KeywordSet(
    [
        "ideal",
        "ideally",
        "prefer*",
        "preference",
        "priority",
        "prioritise*",
        "prioritize*",
        "especially",
        "particularly",
        "primarily",
        "mainly",
        "typically",
        "usually",
        "best suited",
        "most suitable",
        "aimed at",
        "targeted at",
        "geared towards",
        "encouraged",
    ]
)
_PREFERENCE_REACH = 6  # words either side of the later-years phrase
_SENTENCE_END = re.compile(r"[.!?;\n\u2022]+")


@dataclass(frozen=True)
class Eligibility:
    status: str
    source: str | None = None  # TITLE, DESCRIPTION or PROGRAMME
    evidence: str = ""  # the words that decided it
    # Eligible, but the posting names the years it would rather have: "All years welcome
    # · prefers penultimate/final". "" otherwise.
    note: str = ""

    @property
    def eligible(self) -> bool:
        return self.status == ELIGIBLE

    @property
    def basis(self) -> str:
        """Why it is eligible, as stored with the job: 'title: "First Year"'. "" if n/a."""
        if self.status != ELIGIBLE:
            return ""
        if self.source in (PROGRAMME, REMEMBERED):
            return self.evidence
        return f'{self.source}: "{self.evidence}"'

    @property
    def reason(self) -> str:
        """Why a posting was dropped, for logs and `explain`."""
        if self.status == INELIGIBLE:
            return f'later years only: "{self.evidence}"'
        if self.status == UNCLEAR and self.evidence:
            return self.evidence
        if self.status == UNCLEAR:
            return "the posting does not say first years can apply"
        return "its description could not be read"


def label(basis: str, note: str = "") -> str:
    """The line shown on an alert: '✅ First-year eligible (title: "First Year")', or
    '✅ All years welcome · prefers penultimate/final (description: "…")'."""
    if not basis:
        return ""
    return f"✅ {note} ({basis})" if note else f"{MARK} ({basis})"


def mark(note: str = "") -> str:
    """The short form, for a table cell or a summary line: "✅" or "✅ All years welcome ·
    prefers penultimate/final"."""
    return f"✅ {note}" if note else "✅"


def needs_description(role: RoleType, title: str, filters: Filters) -> bool:
    """Would reading the description change anything for a posting with this title?

    Not when the title already settles it, and not when the role type is not checked.
    """
    if not filters.eligibility.applies_to(role.name):
        return False
    return _title_signal(role, title, filters) is None


def assess(role: RoleType, title: str, description: str | None, filters: Filters) -> Eligibility:
    """Decide eligibility. `description` is None when it was not, or could not be, read."""
    rules = filters.eligibility
    if not rules.applies_to(role.name):
        return Eligibility(NOT_APPLICABLE)
    signal = _title_signal(role, title, filters)
    if signal:
        return Eligibility(ELIGIBLE, TITLE, signal)

    sentences = _sentences(description or "")
    offered = _first_offer(rules.first_year, sentences)
    if offered:
        quoted, keyword, at = offered
        prefers = _preferred(rules.later_years_only, title, sentences, at)
        return Eligibility(ELIGIBLE, DESCRIPTION, quoted, note=_note(keyword, prefers))
    restricted = _first(rules.later_years_only, [title, *sentences], skip_negated=False)
    if restricted:
        return Eligibility(INELIGIBLE, DESCRIPTION, restricted)
    if role.name in rules.explicit_only:
        return Eligibility(UNKNOWN if description is None else UNCLEAR)
    if description is None:
        return Eligibility(ELIGIBLE, PROGRAMME, f"{role.label.lower()}; description not read")
    return Eligibility(
        ELIGIBLE, PROGRAMME, f"{role.label.lower()}; the posting does not say otherwise"
    )


def _title_signal(role: RoleType, title: str, filters: Filters) -> str | None:
    """The words in the title that say first years may apply, if any."""
    rules = filters.eligibility
    text = normalise(title)
    found = rules.first_year.search(text)
    if found and not _negated(text, found[1]):
        return _quote(title, found[0])
    if role.name in rules.by_title and not rules.later_years_only.find(text):
        # The role type is a first-year scheme: "First Year Programme", "Explore
        # Microsoft", "STEP Intern". The words that put it there are the evidence.
        return _quote(title, role.include.find(text) or "")
    return None


def _first(keywords: KeywordSet, sentences: list[str], skip_negated: bool) -> str | None:
    """The first sentence containing one of the keywords, shortened for display."""
    for sentence in sentences:
        text = normalise(sentence)
        found = keywords.search(text)
        if not found:
            continue
        if skip_negated and _negated(text, found[1]):
            continue
        return _shorten(sentence, found[0])
    return None


def _first_offer(
    keywords: KeywordSet, sentences: list[str]
) -> tuple[str, str, tuple[int, int, int]] | None:
    """The first sentence offering first years a place: (quote, keyword, (sentence index,
    start, end) of the phrase in the normalised sentence). Negated offers are skipped."""
    for index, sentence in enumerate(sentences):
        text = normalise(sentence)
        for keyword, start, end in keywords.find_all(text):
            if not _negated(text, start):
                return _shorten(sentence, keyword), keyword, (index, start, end)
    return None


def _preferred(
    keywords: KeywordSet, title: str, sentences: list[str], offer: tuple[int, int, int]
) -> list[str]:
    """The later years an eligible posting says it would rather have ("penultimate",
    "final"), in the order they appear. Only a phrase with preference wording close by
    counts ("penultimate or final year ideal"); one that is part of the offer itself
    ("first or penultimate") or negated ("not only final years") does not."""
    found: list[str] = []
    for index, sentence in enumerate([title, *sentences]):
        text = normalise(sentence)
        for keyword, start, end in keywords.find_all(text):
            inside = index - 1 == offer[0] and start < offer[2] and end > offer[1]
            if inside or _negated(text, start) or not _preference_near(text, start, end):
                continue
            name = _short(keyword)
            if name not in found:
                found.append(name)
    return found


def _preference_near(normalised_sentence: str, start: int, end: int) -> bool:
    before = normalised_sentence[:start].split()[-_PREFERENCE_REACH:]
    after = normalised_sentence[end:].split()[:_PREFERENCE_REACH]
    return _PREFERENCE.find(" ".join([*before, "|", *after])) is not None


def _note(keyword: str, prefers: list[str]) -> str:
    if not prefers:
        return ""
    who = "All years welcome" if _ANY_YEAR.find(normalise(keyword)) else "First years welcome"
    return f"{who} · prefers {'/'.join(prefers)}"


def _short(keyword: str) -> str:
    """ "final year*" -> "final", "junior year" -> "junior", "penultimate" as it is."""
    words = keyword.replace("*", "").split()
    return words[0] if len(words) == 2 and words[1] == "year" else " ".join(words)


def _negated(normalised_sentence: str, position: int) -> bool:
    before = normalised_sentence[:position].split()[-_NEGATION_REACH:]
    return _NEGATIONS.find(" ".join(before)) is not None


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]


def _quote(title: str, keyword: str) -> str:
    """The keyword as it is written in the title ("First-Year"), not as configured."""
    words = [re.escape(word.rstrip("*")) for word in keyword.split()]
    pattern = r"[^a-z0-9]+".join(word + r"[a-z0-9]*" for word in words)
    match = re.search(pattern, title, re.IGNORECASE) if words else None
    return match.group(0) if match else (keyword.replace("*", "") or title)[:MAX_EVIDENCE]


def _shorten(sentence: str, keyword: str) -> str:
    """Up to MAX_EVIDENCE characters of the sentence, centred on the keyword."""
    sentence = " ".join(sentence.split())
    if len(sentence) <= MAX_EVIDENCE:
        return sentence
    first_word = keyword.split()[0].rstrip("*")
    at = max(sentence.lower().find(first_word.lower()), 0)
    start = max(0, min(at - MAX_EVIDENCE // 3, len(sentence) - MAX_EVIDENCE))
    piece = sentence[start : start + MAX_EVIDENCE].strip()
    return ("…" if start else "") + piece + ("…" if start + MAX_EVIDENCE < len(sentence) else "")
