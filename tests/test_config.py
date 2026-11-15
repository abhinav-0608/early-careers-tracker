import shutil

import pytest

from conftest import ROOT
from jobtracker.config import ConfigError, load_config
from jobtracker.sources import ADAPTERS


def test_shipped_config_is_valid(config):
    assert config.companies
    assert len({company.key for company in config.companies}) == len(config.companies)
    for company in config.companies:
        assert company.ats in ADAPTERS or company.ats == "unsupported"


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
