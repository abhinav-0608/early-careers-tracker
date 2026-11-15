"""Read a robots.txt file and answer "may this tracker fetch this path?".

The standard library's urllib.robotparser is not used because it compares paths as
plain prefixes and does not understand the `*` wildcard. Real files depend on it
(tal.net disallows `/*/ats/`), and a parser that ignores the wildcard would read that
rule as allowing everything, which is the wrong direction to be wrong in.

The rules followed are those of RFC 9309:

  - A file is a list of groups. A group starts with one or more `User-agent` lines and
    holds the `Allow` / `Disallow` lines that follow them.
  - The groups naming this tracker apply; if none does, the `*` groups apply.
  - Among the rules that match a path, the longest pattern wins, and `Allow` wins a tie.
  - In a pattern, `*` matches any run of characters and a trailing `$` anchors the end.

`Crawl-delay` is not part of the RFC, but sites that set it mean it, so it is read too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_BYTES = 500_000  # the RFC requires parsing at least 500 KiB; more is not needed


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class RobotsRules:
    """The rules one host's robots.txt gives this tracker."""

    rules: tuple[Rule, ...] = ()
    crawl_delay: float | None = None  # seconds between requests, when the site asks

    def allows(self, path: str) -> bool:
        """May `path` (path plus any ?query) be fetched?"""
        verdict = self.rule_for(path)
        return verdict is None or verdict.allow

    def rule_for(self, path: str) -> Rule | None:
        """The rule that decides `path`, or None when no rule mentions it."""
        matching = [rule for rule in self.rules if rule.regex.match(path)]
        if not matching:
            return None
        # Longest pattern wins; on equal length, Allow (True sorts above False) wins.
        return max(matching, key=lambda rule: (len(rule.pattern), rule.allow))


ALLOW_ALL = RobotsRules()


def parse(text: str, agent: str) -> RobotsRules:
    """Parse a robots.txt body for the crawler whose product token is `agent`."""
    agent = agent.lower()
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []  # (agents, lines)
    agents: list[str] = []
    lines: list[tuple[str, str]] = []
    in_rules = False  # a User-agent line after rules starts a new group

    for raw in text[:MAX_BYTES].splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if in_rules:
                groups.append((agents, lines))
                agents, lines, in_rules = [], [], False
            agents.append(value.lower())
        elif field in ("allow", "disallow", "crawl-delay"):
            in_rules = True
            if agents:  # rules before any User-agent line belong to nobody
                lines.append((field, value))
    groups.append((agents, lines))

    # Groups naming this tracker replace the `*` groups; they do not add to them.
    named = [lines for agents, lines in groups if agent in agents]
    chosen = named or [lines for agents, lines in groups if "*" in agents]

    rules: list[Rule] = []
    crawl_delay: float | None = None
    for field, value in (line for group in chosen for line in group):
        if field == "crawl-delay":
            try:
                crawl_delay = max(crawl_delay or 0.0, float(value))
            except ValueError:
                pass
        elif value:  # an empty Disallow means "nothing is disallowed"
            rules.append(Rule(allow=field == "allow", pattern=value, regex=_compile(value)))
    return RobotsRules(rules=tuple(rules), crawl_delay=crawl_delay)


def product_token(user_agent: str) -> str:
    """The name robots.txt files would use for us: "job-alert-tracker/1.0 (+...)" ->
    "job-alert-tracker"."""
    return user_agent.split("/", 1)[0].split()[0].lower() if user_agent.strip() else "*"


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))
