import shutil

import pytest

from conftest import ROOT
from jobtracker.config import KNOWN_ATS, ConfigError, load_config
from jobtracker.sources import ADAPTERS


def test_shipped_config_is_valid(config):
    assert config.companies
    assert len({company.key for company in config.companies}) == len(config.companies)
    assert set(ADAPTERS) <= KNOWN_ATS
    for company in config.companies:
        assert company.ats in KNOWN_ATS


def test_confirmed_dream_tier_companies_are_marked_dream(config):
    confirmed = {
        "apple", "nvidia", "microsoft", "google", "amazon", "meta", "tesla", "amd", "palantir",
        "anthropic", "openai", "google-deepmind",
        "jane-street", "hudson-river-trading", "optiver", "g-research", "citadel",
        "citadel-securities",
        "goldman-sachs", "jpmorgan-chase", "morgan-stanley", "stripe", "revolut",
    }  # fmt: skip
    dream = {company.key for company in config.companies if company.tier == "dream"}
    assert dream == confirmed


def test_excluded_companies_are_not_in_the_list(config):
    excluded = ("mckinsey", "bcg", "bain", "deloitte", "pwc", "kpmg", "accenture", "spacex")
    names = {company.name.lower() for company in config.companies}
    keys = {company.key for company in config.companies}
    for word in (*excluded, "ey", "ernst"):
        assert word not in keys
        assert not any(word in name.split() for name in names)


def test_role_types_requested_now_are_enabled_and_later_ones_are_defined_but_off(filters):
    enabled = {role.name for role in filters.role_types if role.enabled}
    disabled = {role.name for role in filters.role_types if not role.enabled}
    assert enabled == {"spring_week", "insight", "first_year"}
    assert disabled == {"summer_internship", "placement", "graduate"}


def test_filters_hash_ignores_comments_but_not_rule_changes(tmp_path):
    for attempt in ("original", "comment", "changed"):
        shutil.copytree(ROOT / "config", tmp_path / attempt)
    comment = tmp_path / "comment" / "filters.yaml"
    comment.write_text("# a new comment\n" + comment.read_text())
    changed = tmp_path / "changed" / "filters.yaml"
    changed.write_text(changed.read_text().replace("enabled: false", "enabled: true", 1))

    original_hash = load_config(tmp_path / "original").filters.hash
    assert load_config(tmp_path / "comment").filters.hash == original_hash
    assert load_config(tmp_path / "changed").filters.hash != original_hash


def _config_with(tmp_path, filename, old, new):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    path = tmp_path / "config" / filename
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))
    return tmp_path / "config"


def test_unknown_tier_is_rejected(tmp_path):
    config_dir = _config_with(tmp_path, "companies.yaml", "tier: dream", "tier: legendary")
    with pytest.raises(ConfigError, match="tier must be one of"):
        load_config(config_dir)


def test_missing_board_is_rejected(tmp_path):
    config_dir = _config_with(tmp_path, "companies.yaml", "board: anthropic", "")
    with pytest.raises(ConfigError, match="anthropic: board is required"):
        load_config(config_dir)


def _companies_file(tmp_path, entry: str):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "config" / "companies.yaml").write_text(entry)
    return tmp_path / "config"


def test_unknown_ats_is_rejected(tmp_path):
    config_dir = _companies_file(tmp_path, "- {key: acme, name: Acme, ats: greenhose, board: a}")
    with pytest.raises(ConfigError, match="acme: ats must be one of"):
        load_config(config_dir)


def test_workday_company_needs_tenant_host_and_sites(tmp_path):
    entry = "- {key: acme, name: Acme, ats: workday, tenant: acme, sites: [Careers]}"
    with pytest.raises(ConfigError, match="acme.*missing required key 'host'"):
        load_config(_companies_file(tmp_path / "no-host", entry))

    entry = "- {key: acme, name: Acme, ats: workday, tenant: acme, host: wd5}"
    with pytest.raises(ConfigError, match="acme: a workday company needs a list of sites"):
        load_config(_companies_file(tmp_path / "no-sites", entry))

    entry = "- {key: acme, name: Acme, ats: workday, tenant: acme, host: wd3, sites: [A, B]}"
    company = load_config(_companies_file(tmp_path / "valid", entry)).companies[0]
    assert (company.tenant, company.host, company.sites) == ("acme", "wd3", ("A", "B"))


def test_unsupported_company_needs_a_note(tmp_path):
    config_dir = _companies_file(tmp_path, "- {key: acme, name: Acme, ats: unsupported}")
    with pytest.raises(ConfigError, match="acme: an unsupported company needs a note"):
        load_config(config_dir)


def test_misspelt_tag_is_rejected(tmp_path):
    entry = "- {key: acme, name: Acme, ats: lever, board: acme, tags: [vc-backed]}"
    with pytest.raises(ConfigError, match="acme: tags must be from"):
        load_config(_companies_file(tmp_path, entry))


def test_wrong_type_is_rejected_with_the_location_of_the_mistake(tmp_path):
    config_dir = _config_with(tmp_path, "settings.yaml", "retries: 2", "retries: lots")
    with pytest.raises(ConfigError, match="http: retries: expected int, got str"):
        load_config(config_dir)


def test_prune_window_shorter_than_repost_window_is_rejected(tmp_path):
    config_dir = _config_with(
        tmp_path, "settings.yaml", "prune_closed_after_days: 90", "prune_closed_after_days: 7"
    )
    with pytest.raises(ConfigError, match="prune_closed_after_days must be >="):
        load_config(config_dir)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_config(tmp_path)
