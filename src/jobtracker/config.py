"""Load and validate the three YAML config files.

Validation is hand-written so that a mistake in a config file produces a message naming
the file and the exact entry, rather than a stack trace from deep inside the pipeline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jobtracker.models import Company
from jobtracker.text import KeywordSet

TIERS = {"dream", "normal"}
LOCATION_MODES = {"onsite", "remote"}
FIELD_MATCH_MODES = {"lenient", "strict"}
COMPANY_TAGS = {"big_tech", "vc_backed", "yc"}

# Every value "ats" may take. A company whose ats has no adapter yet is loaded and
# validated like any other, and simply not polled until its adapter exists.
WORKDAY = "workday"
UNSUPPORTED = "unsupported"
BOARD_ATS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "custom"}
KNOWN_ATS = BOARD_ATS | {WORKDAY, UNSUPPORTED}


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
    reminder_hours: int
    accept_empty_after_runs: int


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
class Config:
    settings: Settings
    filters: Filters
    companies: tuple[Company, ...]

    def company(self, key: str) -> Company | None:
        return next((company for company in self.companies if company.key == key), None)


def load_config(config_dir: Path | str = "config") -> Config:
    config_dir = Path(config_dir)
    return Config(
        settings=load_settings(config_dir / "settings.yaml"),
        filters=load_filters(config_dir / "filters.yaml"),
        companies=load_companies(config_dir / "companies.yaml"),
    )


# --- settings.yaml ---------------------------------------------------------------------


def load_settings(path: Path) -> Settings:
    data = _read_yaml(path, dict)
    http = _get(data, "http", dict, path)
    notify = _get(data, "notify", dict, path)
    priority = _get(notify, "priority", dict, f"{path}: notify")
    state = _get(data, "state", dict, path)
    health = _get(data, "health", dict, path)

    settings = Settings(
        user_agent=_get(data, "user_agent", str, path),
        repo_url=_get(data, "repo_url", str, path),
        timeout_seconds=float(_get(http, "timeout_seconds", (int, float), f"{path}: http")),
        retries=_get(http, "retries", int, f"{path}: http"),
        per_host_delay_seconds=float(
            _get(http, "per_host_delay_seconds", (int, float), f"{path}: http")
        ),
        max_workers=_get(http, "max_workers", int, f"{path}: http"),
        ntfy_server=_get(notify, "server", str, f"{path}: notify").rstrip("/"),
        batch_threshold=_get(notify, "batch_threshold", int, f"{path}: notify"),
        priority_dream=_get(priority, "dream", int, f"{path}: notify.priority"),
        priority_normal=_get(priority, "normal", int, f"{path}: notify.priority"),
        close_after_missing_runs=_get(state, "close_after_missing_runs", int, f"{path}: state"),
        repost_window_days=_get(state, "repost_window_days", int, f"{path}: state"),
        prune_closed_after_days=_get(state, "prune_closed_after_days", int, f"{path}: state"),
        fail_after_consecutive=_get(health, "fail_after_consecutive", int, f"{path}: health"),
        reminder_hours=_get(health, "reminder_hours", int, f"{path}: health"),
        accept_empty_after_runs=_get(health, "accept_empty_after_runs", int, f"{path}: health"),
    )

    if settings.max_workers < 1:
        raise ConfigError(f"{path}: http.max_workers must be at least 1")
    if settings.close_after_missing_runs < 1:
        raise ConfigError(f"{path}: state.close_after_missing_runs must be at least 1")
    if settings.fail_after_consecutive < 1:
        raise ConfigError(f"{path}: health.fail_after_consecutive must be at least 1")
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


# --- filters.yaml ----------------------------------------------------------------------


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

        tier = _get(raw, "tier", str, where, default="normal")
        if tier not in TIERS:
            raise ConfigError(f"{where}: tier must be one of {sorted(TIERS)}")

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
            if not sites or not all(isinstance(site, str) for site in sites):
                raise ConfigError(f"{where}: a workday company needs a list of sites")
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
                tier=tier,
                ats=ats,
                board=str(board) if board else None,
                tenant=raw.get("tenant"),
                host=raw.get("host"),
                sites=sites,
                tags=tags,
                regions=tuple(_get(raw, "regions", list, where, default=[])),
                careers_url=raw.get("careers_url"),
                note=raw.get("note"),
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


def _expect(value: Any, expected_type: Any, where: Any) -> None:
    # bool is a subclass of int in Python, so "retries: true" needs rejecting explicitly.
    wrong_bool = isinstance(value, bool) and expected_type is not bool
    if wrong_bool or not isinstance(value, expected_type):
        names = expected_type if isinstance(expected_type, tuple) else (expected_type,)
        expected = " or ".join(t.__name__ for t in names)
        raise ConfigError(f"{where}: expected {expected}, got {type(value).__name__}")
