"""Load and validate the YAML config files.

    config/settings.yaml                 run-level knobs, shared
    config/companies.yaml                the company list, shared
    config/profiles/<key>/profile.yaml   one person's company scope and dream tier
    config/profiles/<key>/filters.yaml   that person's role types, fields and locations

Validation is hand-written so that a mistake in a config file produces a message naming
the file and the exact entry, rather than a stack trace from deep inside the pipeline.

A mistake in one profile's files disables that profile only. The others load and run as
if it were not there, which is what lets a profile be added or edited without any risk
to anyone else's alerts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobtracker.models import Company
from jobtracker.text import KeywordSet

LOCATION_MODES = {"onsite", "remote"}
FIELD_MATCH_MODES = {"lenient", "strict"}
COMPANY_TAGS = {"big_tech", "vc_backed", "yc"}

# Every value "ats" may take. A company whose ats has no adapter yet is loaded and
# validated like any other, and simply not polled until its adapter exists.
WORKDAY = "workday"
# The polling-interval key for the expensive half of the Workday adapter: the searches it
# runs on sites too large to read in full.
WORKDAY_SEARCH = "workday_search"
TALNET = "talnet"
UNSUPPORTED = "unsupported"
BOARD_ATS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "custom", TALNET}
KNOWN_ATS = BOARD_ATS | {WORKDAY, UNSUPPORTED}

# A profile's key is its folder name. It ends up in a file name and in the name of an
# environment variable, hence the narrow alphabet.
PROFILE_KEY = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


class ConfigError(Exception):
    """A config file is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class Settings:
    user_agent: str
    repo_url: str
    # http
    timeout_seconds: float
    retries: int
    per_host_delay_seconds: float
    max_workers: int
    robots_cache_hours: int
    # notify
    ntfy_server: str
    batch_threshold: int
    priority_dream: int
    priority_normal: int
    # state
    close_after_missing_runs: int
    repost_window_days: int
    prune_closed_after_days: int
    # health
    fail_after_consecutive: int
    fail_after_rate_limited: int
    reminder_hours: int
    accept_empty_after_runs: int
    # polling: minimum minutes between polls, by kind of source. Not listed = every run.
    poll_every_minutes: dict[str, int] = field(default_factory=dict)
    host_delay_seconds: dict[str, float] = field(default_factory=dict)
    # http: hosts checked by hand whose /robots.txt redirects to an ordinary page
    hosts_without_robots_txt: frozenset[str] = frozenset()
    # polling: kinds of source that are switched off (same names as poll_every_minutes)
    disabled_sources: frozenset[str] = frozenset()

    def poll_interval(self, kind: str | None) -> int:
        """Minimum minutes between polls of this kind of source; 0 means every run."""
        return self.poll_every_minutes.get(kind or "", 0)


@dataclass(frozen=True)
class RoleType:
    name: str
    label: str
    enabled: bool
    strict_field: bool
    include: KeywordSet
    exclude: KeywordSet
    ntfy_tags: tuple[str, ...]


@dataclass(frozen=True)
class FieldRule:
    name: str
    include: KeywordSet
    exclude: KeywordSet


@dataclass(frozen=True)
class Region:
    name: str
    label: str
    match: KeywordSet
    exclude: KeywordSet
    allow: frozenset[str]
    require_company_tags: frozenset[str]


@dataclass(frozen=True)
class Filters:
    role_types: tuple[RoleType, ...]  # in priority order
    global_exclude: KeywordSet
    fields: tuple[FieldRule, ...]
    field_exclude: KeywordSet
    regions: tuple[Region, ...]  # in priority order
    other_locations: KeywordSet
    remote_keywords: KeywordSet
    hash: str  # changes whenever the rules change; used to trigger a re-baseline

    def role_type(self, name: str) -> RoleType | None:
        return next((role for role in self.role_types if role.name == name), None)

    def region(self, name: str) -> Region | None:
        return next((region for region in self.regions if region.name == name), None)


@dataclass(frozen=True)
class Profile:
    """One person's view of the tracker: what they watch and how it is filtered.

    Profiles share the company list and the fetched jobs, and nothing else. Each has
    its own filters, scope, dream tier, ntfy topic and state file.
    """

    key: str  # the folder name under config/profiles
    name: str
    filters: Filters
    industries: frozenset[str]  # scope: every company in these industries ...
    companies: frozenset[str]  # ... plus these company keys
    dream: frozenset[str]  # company keys that are dream tier for this person

    @property
    def topic_env(self) -> str:
        """The environment variable holding this profile's ntfy topic.

        Derived from the key rather than configured, so two profiles can never be
        pointed at the same variable by mistake.
        """
        return "NTFY_TOPIC_" + self.key.upper().replace("-", "_")

    def watches(self, company: Company) -> bool:
        """Is this company in the profile's scope?

        Named companies always are. An industry brings in its companies except those
        marked opt_in, which exist for the profiles that name them and nobody else.
        """
        if company.key in self.companies:
            return True
        return company.industry in self.industries and not company.opt_in

    def is_dream(self, company_key: str) -> bool:
        return company_key in self.dream


@dataclass(frozen=True)
class Config:
    settings: Settings
    companies: tuple[Company, ...]
    profiles: tuple[Profile, ...]  # the valid ones, in key order
    # Profiles whose files failed validation, as key -> message. They are skipped and
    # reported; every other profile runs normally.
    profile_errors: dict[str, str] = field(default_factory=dict)

    def company(self, key: str) -> Company | None:
        return next((company for company in self.companies if company.key == key), None)

    def profile(self, key: str) -> Profile | None:
        return next((profile for profile in self.profiles if profile.key == key), None)


def load_config(config_dir: Path | str = "config") -> Config:
    config_dir = Path(config_dir)
    settings = load_settings(config_dir / "settings.yaml")
    companies = load_companies(config_dir / "companies.yaml")
    _check_poll_kinds(settings, config_dir / "settings.yaml")
    profiles, errors = load_profiles(config_dir / "profiles", companies)
    if not profiles:
        # With nobody to alert there is nothing useful a run could do.
        problems = "; ".join(errors.values()) or f"{config_dir / 'profiles'}: no profiles found"
        raise ConfigError(problems)
    return Config(settings=settings, companies=companies, profiles=profiles, profile_errors=errors)


def _check_poll_kinds(settings: Settings, path: Path) -> None:
    """Reject a polling interval set for a source that does not exist.

    A misspelt key ("gogle: 60") would otherwise quietly leave its source on every run.
    """
    # Imported here because the adapters themselves import this module.
    from jobtracker.sources.custom import MODULES

    known = (KNOWN_ATS - {"custom", UNSUPPORTED}) | {WORKDAY_SEARCH} | set(MODULES)
    for kind in sorted(set(settings.poll_every_minutes) - known):
        raise ConfigError(
            f"{path}: polling.every_minutes: no source is called '{kind}' "
            f"(expected one of {sorted(known)})"
        )
    for kind in sorted(settings.disabled_sources - (known - {WORKDAY_SEARCH})):
        raise ConfigError(
            f"{path}: polling.disabled: no source is called '{kind}' "
            f"(expected one of {sorted(known - {WORKDAY_SEARCH})})"
        )


# --- settings.yaml ---------------------------------------------------------------------


def load_settings(path: Path) -> Settings:
    data = _read_yaml(path, dict)
    http = _get(data, "http", dict, path)
    notify = _get(data, "notify", dict, path)
    priority = _get(notify, "priority", dict, f"{path}: notify")
    state = _get(data, "state", dict, path)
    health = _get(data, "health", dict, path)
    host_delays = _get(http, "host_delay_seconds", dict, f"{path}: http", default={})
    for host, seconds in host_delays.items():
        _expect(seconds, (int, float), f"{path}: http.host_delay_seconds.{host}")
        if seconds < 0:
            raise ConfigError(f"{path}: http.host_delay_seconds.{host} must be 0 or more")
    no_robots = _get(http, "hosts_without_robots_txt", list, f"{path}: http", default=[])
    for host in no_robots:
        _expect(host, str, f"{path}: http.hosts_without_robots_txt")
    polling = _get(data, "polling", dict, path, default={})
    _only_keys(polling, {"every_minutes", "disabled"}, f"{path}: polling")
    disabled = _get(polling, "disabled", list, f"{path}: polling", default=[])
    for kind in disabled:
        _expect(kind, str, f"{path}: polling.disabled")
    every = _get(polling, "every_minutes", dict, f"{path}: polling", default={})
    for kind, minutes in every.items():
        _expect(minutes, int, f"{path}: polling.every_minutes.{kind}")
        if minutes < 0:
            raise ConfigError(f"{path}: polling.every_minutes.{kind} must be 0 or more")
    if every.get(WORKDAY, 0) > 0:
        # Workday is split in two: small sites every run, large-site searches on an
        # interval. One interval for both would hide which of them it applied to.
        raise ConfigError(
            f"{path}: polling.every_minutes.workday must be 0; "
            f"set '{WORKDAY_SEARCH}' to slow down the large Workday sites"
        )

    settings = Settings(
        user_agent=_get(data, "user_agent", str, path),
        repo_url=_get(data, "repo_url", str, path),
        timeout_seconds=float(_get(http, "timeout_seconds", (int, float), f"{path}: http")),
        retries=_get(http, "retries", int, f"{path}: http"),
        per_host_delay_seconds=float(
            _get(http, "per_host_delay_seconds", (int, float), f"{path}: http")
        ),
        max_workers=_get(http, "max_workers", int, f"{path}: http"),
        robots_cache_hours=_get(http, "robots_cache_hours", int, f"{path}: http"),
        ntfy_server=_get(notify, "server", str, f"{path}: notify").rstrip("/"),
        batch_threshold=_get(notify, "batch_threshold", int, f"{path}: notify"),
        priority_dream=_get(priority, "dream", int, f"{path}: notify.priority"),
        priority_normal=_get(priority, "normal", int, f"{path}: notify.priority"),
        close_after_missing_runs=_get(state, "close_after_missing_runs", int, f"{path}: state"),
        repost_window_days=_get(state, "repost_window_days", int, f"{path}: state"),
        prune_closed_after_days=_get(state, "prune_closed_after_days", int, f"{path}: state"),
        fail_after_consecutive=_get(health, "fail_after_consecutive", int, f"{path}: health"),
        fail_after_rate_limited=_get(health, "fail_after_rate_limited", int, f"{path}: health"),
        reminder_hours=_get(health, "reminder_hours", int, f"{path}: health"),
        accept_empty_after_runs=_get(health, "accept_empty_after_runs", int, f"{path}: health"),
        poll_every_minutes={str(kind): minutes for kind, minutes in every.items()},
        host_delay_seconds={str(host): float(seconds) for host, seconds in host_delays.items()},
        hosts_without_robots_txt=frozenset(no_robots),
        disabled_sources=frozenset(disabled),
    )

    if settings.max_workers < 1:
        raise ConfigError(f"{path}: http.max_workers must be at least 1")
    if settings.close_after_missing_runs < 1:
        raise ConfigError(f"{path}: state.close_after_missing_runs must be at least 1")
    if settings.fail_after_consecutive < 1:
        raise ConfigError(f"{path}: health.fail_after_consecutive must be at least 1")
    if settings.fail_after_rate_limited < settings.fail_after_consecutive:
        raise ConfigError(
            f"{path}: health.fail_after_rate_limited must be at least health.fail_after_consecutive"
        )
    if settings.robots_cache_hours < 0:
        raise ConfigError(f"{path}: http.robots_cache_hours must be 0 or more")
    for name in ("priority_dream", "priority_normal"):
        if not 1 <= getattr(settings, name) <= 5:
            raise ConfigError(f"{path}: notify.priority values must be between 1 and 5")
    if settings.prune_closed_after_days < settings.repost_window_days:
        # Pruning a closed job earlier than the repost window would make its repost
        # look new, silently shortening the window.
        raise ConfigError(
            f"{path}: state.prune_closed_after_days must be >= state.repost_window_days"
        )
    return settings


# --- profiles/<key>/ -------------------------------------------------------------------


def load_profiles(
    profiles_dir: Path, companies: tuple[Company, ...]
) -> tuple[tuple[Profile, ...], dict[str, str]]:
    """Load every profile folder. Returns (valid profiles, {key: error} for the rest).

    Each folder is loaded inside its own error boundary: a broken profile is reported
    and left out, and can never stop another profile from loading.
    """
    if not profiles_dir.is_dir():
        raise ConfigError(f"{profiles_dir}: folder not found")
    profiles: list[Profile] = []
    errors: dict[str, str] = {}
    for path in sorted(profiles_dir.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            profiles.append(load_profile(path, companies))
        except ConfigError as exc:
            errors[path.name] = str(exc)
    return tuple(profiles), errors


def load_profile(path: Path, companies: tuple[Company, ...]) -> Profile:
    """Load one profile folder, checking its scope and dream list against the companies."""
    if not PROFILE_KEY.fullmatch(path.name):
        raise ConfigError(
            f"{path}: a profile folder name may only use lowercase letters, digits and hyphens"
        )
    file = path / "profile.yaml"
    data = _read_yaml(file, dict)
    _only_keys(data, {"name", "scope", "dream"}, file)
    scope = _get(data, "scope", dict, file)
    _only_keys(scope, {"industries", "companies"}, f"{file}: scope")

    profile = Profile(
        key=path.name,
        name=_get(data, "name", str, file),
        filters=load_filters(path / "filters.yaml"),
        industries=frozenset(_strings(scope, "industries", f"{file}: scope")),
        companies=frozenset(_strings(scope, "companies", f"{file}: scope")),
        dream=frozenset(_strings(data, "dream", file)),
    )

    # A misspelt industry or company key would otherwise just quietly watch nothing.
    by_key = {company.key: company for company in companies}
    unknown = sorted(profile.industries - {company.industry for company in companies})
    if unknown:
        raise ConfigError(f"{file}: scope.industries: no company has industry '{unknown[0]}'")
    unknown = sorted(profile.companies - set(by_key))
    if unknown:
        raise ConfigError(f"{file}: scope.companies: no company with key '{unknown[0]}'")
    if not any(profile.watches(company) for company in companies):
        raise ConfigError(f"{file}: scope does not include any company")
    for key in sorted(profile.dream):
        if key not in by_key:
            raise ConfigError(f"{file}: dream: no company with key '{key}'")
        if not profile.watches(by_key[key]):
            raise ConfigError(f"{file}: dream: '{key}' is outside this profile's scope")
    return profile


# --- profiles/<key>/filters.yaml -------------------------------------------------------


def load_filters(path: Path) -> Filters:
    data = _read_yaml(path, dict)

    role_types = []
    for name, raw in _get(data, "role_types", dict, path).items():
        where = f"{path}: role_types.{name}"
        _expect(raw, dict, where)
        field_match = _get(raw, "field_match", str, where)
        if field_match not in FIELD_MATCH_MODES:
            raise ConfigError(f"{where}: field_match must be one of {sorted(FIELD_MATCH_MODES)}")
        role_types.append(
            RoleType(
                name=name,
                label=_get(raw, "label", str, where),
                enabled=_get(raw, "enabled", bool, where),
                strict_field=field_match == "strict",
                include=_keywords(raw, "include", where, required=True),
                exclude=_keywords(raw, "exclude", where),
                ntfy_tags=tuple(_get(raw, "ntfy_tags", list, where, default=[])),
            )
        )

    fields = []
    for name, raw in _get(data, "fields", dict, path).items():
        where = f"{path}: fields.{name}"
        _expect(raw, dict, where)
        fields.append(
            FieldRule(
                name=name,
                include=_keywords(raw, "include", where, required=True),
                exclude=_keywords(raw, "exclude", where),
            )
        )

    regions = []
    for name, raw in _get(data, "regions", dict, path).items():
        where = f"{path}: regions.{name}"
        _expect(raw, dict, where)
        allow = frozenset(_get(raw, "allow", list, where))
        if not allow or not allow <= LOCATION_MODES:
            raise ConfigError(f"{where}: allow must be a non-empty subset of [onsite, remote]")
        regions.append(
            Region(
                name=name,
                label=_get(raw, "label", str, where),
                match=_keywords(raw, "match", where, required=True),
                exclude=_keywords(raw, "exclude", where),
                allow=allow,
                require_company_tags=frozenset(
                    _get(raw, "require_company_tags", list, where, default=[])
                ),
            )
        )

    return Filters(
        role_types=tuple(role_types),
        global_exclude=_keywords(data, "global_exclude", path),
        fields=tuple(fields),
        field_exclude=_keywords(data, "field_exclude", path),
        regions=tuple(regions),
        other_locations=_keywords(data, "other_locations", path),
        remote_keywords=_keywords(data, "remote_keywords", path),
        hash=_content_hash(data),
    )


def _content_hash(data: Any) -> str:
    """Hash of the parsed rules, so comments and formatting changes do not count."""
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _keywords(mapping: dict, key: str, where: Any, required: bool = False) -> KeywordSet:
    values = _get(mapping, key, list, where, default=None if required else [])
    try:
        return KeywordSet([str(value) for value in values])
    except ValueError as exc:
        raise ConfigError(f"{where}: {key}: {exc}") from exc


# --- companies.yaml --------------------------------------------------------------------


def load_companies(path: Path) -> tuple[Company, ...]:
    entries = _read_yaml(path, list)
    companies = []
    seen_keys: set[str] = set()

    for index, raw in enumerate(entries):
        where = f"{path}: entry {index + 1}"
        _expect(raw, dict, where)
        key = _get(raw, "key", str, where)
        where = f"{path}: {key}"
        if key in seen_keys:
            raise ConfigError(f"{where}: duplicate company key")
        if ":" in key or "|" in key:
            raise ConfigError(f"{where}: key must not contain ':' or '|'")
        seen_keys.add(key)

        if "tier" in raw:
            # Dream tier is personal, so it moved out of the shared list. Rejected
            # rather than ignored: a "tier: dream" that silently did nothing would be
            # a missed priority alert.
            raise ConfigError(
                f"{where}: 'tier' is not set here any more; "
                "list the company under 'dream' in a profile.yaml instead"
            )

        ats = _get(raw, "ats", str, where)
        if ats not in KNOWN_ATS:
            raise ConfigError(f"{where}: ats must be one of {sorted(KNOWN_ATS)}")
        board = raw.get("board")
        if ats in BOARD_ATS and not board:
            raise ConfigError(f"{where}: board is required when ats is '{ats}'")
        sites = tuple(_get(raw, "sites", list, where, default=[]))
        if ats == WORKDAY:
            for part in ("tenant", "host"):
                _get(raw, part, str, f"{where} (workday)")
        if ats == TALNET:
            # https://{tenant}.tal.net/vx/{board}/candidate/jobboard/vacancy/{site}/adv/
            _get(raw, "tenant", str, f"{where} (talnet)")
        if ats in (WORKDAY, TALNET):
            # Quoted in YAML: a bare 1 would be a number, and sites are parts of a URL.
            if not sites or not all(isinstance(site, str) for site in sites):
                raise ConfigError(f"{where}: a {ats} company needs a list of sites (strings)")
        if ats == UNSUPPORTED and not raw.get("note"):
            # An unsupported company is a known gap; the note says why, for the README.
            raise ConfigError(f"{where}: an unsupported company needs a note saying why")

        # The India location rule depends on tags, so a misspelt tag must not pass quietly.
        tags = tuple(_get(raw, "tags", list, where, default=[]))
        if not set(tags) <= COMPANY_TAGS:
            raise ConfigError(f"{where}: tags must be from {sorted(COMPANY_TAGS)}")

        companies.append(
            Company(
                key=key,
                name=_get(raw, "name", str, where),
                industry=_get(raw, "industry", str, where, default="unknown"),
                ats=ats,
                board=str(board) if board else None,
                tenant=raw.get("tenant"),
                host=raw.get("host"),
                sites=sites,
                tags=tags,
                regions=tuple(_get(raw, "regions", list, where, default=[])),
                careers_url=raw.get("careers_url"),
                note=raw.get("note"),
                opt_in=_get(raw, "opt_in", bool, where, default=False),
            )
        )
    return tuple(companies)


# --- helpers ---------------------------------------------------------------------------

_MISSING = object()


def _read_yaml(path: Path, expected_type: type) -> Any:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: file not found") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    _expect(data, expected_type, path)
    return data


def _get(mapping: dict, key: str, expected_type: Any, where: Any, default: Any = _MISSING) -> Any:
    """Read mapping[key], checking it exists and has the expected type."""
    if key not in mapping or mapping[key] is None:
        if default is _MISSING or default is None:
            raise ConfigError(f"{where}: missing required key '{key}'")
        return default
    value = mapping[key]
    _expect(value, expected_type, f"{where}: {key}")
    return value


def _strings(mapping: dict, key: str, where: Any) -> list[str]:
    """Read an optional list of strings."""
    values = _get(mapping, key, list, where, default=[])
    for value in values:
        _expect(value, str, f"{where}: {key}")
    return values


def _only_keys(mapping: dict, allowed: set[str], where: Any) -> None:
    """Reject unknown keys, so a typo ("dreams:") cannot be silently ignored."""
    unknown = sorted(str(key) for key in set(mapping) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key '{unknown[0]}' (expected {sorted(allowed)})")


def _expect(value: Any, expected_type: Any, where: Any) -> None:
    # bool is a subclass of int in Python, so "retries: true" needs rejecting explicitly.
    wrong_bool = isinstance(value, bool) and expected_type is not bool
    if wrong_bool or not isinstance(value, expected_type):
        names = expected_type if isinstance(expected_type, tuple) else (expected_type,)
        expected = " or ".join(t.__name__ for t in names)
        raise ConfigError(f"{where}: expected {expected}, got {type(value).__name__}")
