import shutil

import pytest
import yaml

from conftest import OWNER, ROOT, make_company, make_profile
from jobtracker.config import KNOWN_ATS, ConfigError, load_config
from jobtracker.sources import ADAPTERS, adapter_for


def test_shipped_config_is_valid(config):
    assert config.companies
    assert config.profile_errors == {}  # every profile in the repo must load
    assert [profile.key for profile in config.profiles] == [OWNER]
    assert len({company.key for company in config.companies}) == len(config.companies)
    assert set(ADAPTERS) <= KNOWN_ATS
    for company in config.companies:
        assert company.ats in KNOWN_ATS


def test_confirmed_faang_plus_companies_are_on_the_owners_list(profile):
    confirmed = {
        "apple", "nvidia", "microsoft", "google", "amazon", "meta", "tesla", "amd", "palantir",
        "anthropic", "openai", "google-deepmind",
        "jane-street", "hudson-river-trading", "optiver", "g-research", "citadel",
        "citadel-securities",
        "goldman-sachs", "jpmorgan-chase", "morgan-stanley", "stripe", "revolut",
    }  # fmt: skip
    assert profile.faang_plus == confirmed


def test_owner_scope_is_the_seven_industries_the_list_was_built_from(config, profile):
    # Written out as industries rather than "everything", so that a company added for
    # somebody else (in a new industry, or marked opt_in) does not reach the owner.
    assert profile.industries == {
        "big_tech", "ai_lab", "quant", "finance", "fintech", "cybersecurity", "startup",
    }  # fmt: skip
    assert profile.companies == frozenset()
    watched = [company for company in config.companies if profile.watches(company)]
    assert len(watched) >= 421  # everything the single-user tracker was polling


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
    assert enabled == {"spring_week", "insight", "first_year", "summer_internship"}
    assert disabled == {"placement", "graduate"}
    # Internships are on, but only alert when the posting says first years can apply.
    assert filters.eligibility.explicit_only == {"summer_internship"}


def test_filters_hash_ignores_comments_but_not_rule_changes(tmp_path):
    for attempt in ("original", "comment", "changed"):
        shutil.copytree(ROOT / "config", tmp_path / attempt)
    comment = tmp_path / "comment" / OWNER_FILTERS
    comment.write_text("# a new comment\n" + comment.read_text())
    changed = tmp_path / "changed" / OWNER_FILTERS
    changed.write_text(changed.read_text().replace("enabled: false", "enabled: true", 1))

    def filters_hash(name):
        return load_config(tmp_path / name).profile(OWNER).filters.hash

    assert filters_hash("comment") == filters_hash("original")
    assert filters_hash("changed") != filters_hash("original")


OWNER_FILTERS = f"profiles/{OWNER}/filters.yaml"
OWNER_PROFILE = f"profiles/{OWNER}/profile.yaml"


def _config_with(tmp_path, filename, old, new):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    path = tmp_path / "config" / filename
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))
    return tmp_path / "config"


def test_tier_in_the_shared_company_list_is_rejected(tmp_path):
    # It would otherwise be ignored, and a "tier: dream" that does nothing is a trap.
    config_dir = _config_with(
        tmp_path, "companies.yaml", "- key: anthropic\n", "- key: anthropic\n  tier: dream\n"
    )
    with pytest.raises(ConfigError, match="anthropic: 'tier' is not set here any more"):
        load_config(config_dir)


def test_missing_board_is_rejected(tmp_path):
    config_dir = _config_with(tmp_path, "companies.yaml", "board: anthropic", "")
    with pytest.raises(ConfigError, match="anthropic: board is required"):
        load_config(config_dir)


def _companies_file(tmp_path, entry: str):
    """A config with a one-company list and a profile that watches just that company."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "config" / "companies.yaml").write_text(entry)
    (tmp_path / "config" / OWNER_PROFILE).write_text("name: Test\nscope: {companies: [acme]}\n")
    _drop_pauses(tmp_path / "config")
    return tmp_path / "config"


def _drop_pauses(config_dir):
    """Remove the pauses section, whose probe company the test's own list may not have."""
    path = config_dir / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data.pop("pauses", None)
    path.write_text(yaml.safe_dump(data))


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


def test_talnet_company_needs_tenant_board_and_quoted_sites(tmp_path):
    entry = "- {key: acme, name: Acme, ats: talnet, board: lang-en-GB/brand-4, sites: ['1']}"
    with pytest.raises(ConfigError, match="acme.*missing required key 'tenant'"):
        load_config(_companies_file(tmp_path / "no-tenant", entry))

    entry = "- {key: acme, name: Acme, ats: talnet, tenant: acme, sites: ['1']}"
    with pytest.raises(ConfigError, match="acme: board is required"):
        load_config(_companies_file(tmp_path / "no-board", entry))

    # An unquoted board number is a YAML integer, which cannot be put in a URL as it is.
    entry = "- {key: acme, name: Acme, ats: talnet, tenant: acme, board: b, sites: [1, 2]}"
    with pytest.raises(ConfigError, match="acme: a talnet company needs a list of sites"):
        load_config(_companies_file(tmp_path / "numbers", entry))

    entry = "- {key: acme, name: Acme, ats: talnet, tenant: acme, board: b, sites: ['1', '2']}"
    company = load_config(_companies_file(tmp_path / "valid", entry)).companies[0]
    assert (company.tenant, company.board, company.sites) == ("acme", "b", ("1", "2"))


def test_companies_are_queued_by_the_host_that_serves_them():
    """One queue per host is what lets every worker thread fetch from a different host."""
    groups = {
        make_company("a", ats="greenhouse").host_group,
        make_company("b", ats="greenhouse").host_group,
        make_company("c", ats="workday", tenant="acme", host="wd5", board=None).host_group,
        make_company("d", ats="workday", tenant="other", host="wd5", board=None).host_group,
        make_company("f", ats="workday", tenant="third", host="wd3", board=None).host_group,
        make_company("e", ats="talnet", tenant="acme", board="x").host_group,
        make_company("google", ats="custom", board="google").host_group,
        make_company("google-deepmind", ats="custom", board="google").host_group,
        make_company("jpmorgan-chase", ats="custom", board="jpmorgan").host_group,
    }
    assert groups == {
        "greenhouse",
        "workday:wd5",  # tenants of one Workday server share its queue
        "workday:wd3",
        "talnet:acme",
        "custom:google",
        "custom:jpmorgan",
    }


def test_every_polled_company_has_what_its_adapter_needs(config):
    """The shipped list: nothing an adapter reads from a company may be missing."""
    polled = [company for company in config.companies if adapter_for(company)]
    assert {company.ats for company in polled} == {
        "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "workday", "talnet",
        "custom",
    }  # fmt: skip
    for company in polled:
        if company.ats in ("workday", "talnet"):
            assert company.tenant and company.sites, company.key
        if company.ats in ("workday", "eightfold"):
            assert company.host, company.key
        else:
            assert company.board, company.key
    # The FAANG+ companies this stage set out to cover, and the ones it could not.
    by_key = {company.key: company for company in config.companies}
    for key in ("morgan-stanley", "google", "google-deepmind"):
        assert adapter_for(by_key[key]) is not None, key
    # JPMorgan: switched off 2026-11-16 (its terms; DESIGN.md decision 30). Qualcomm:
    # waits for its terms to be read (decision 29).
    for key in (
        "goldman-sachs",
        "citadel",
        "citadel-securities",
        "revolut",
        "jpmorgan-chase",
        "qualcomm",
    ):
        assert by_key[key].ats == "unsupported" and by_key[key].note, key
    # Stage 3b: four more big-tech sites and the three SmartRecruiters boards that are
    # served on SmartRecruiters' own job-board host. The other three SmartRecruiters
    # companies point their board at their own site and stay unsupported, as do Meta
    # and Tesla.
    for key in ("amazon", "apple", "microsoft", "amd", "canva", "servicenow", "armis"):
        assert adapter_for(by_key[key]) is not None, key
    for key in ("meta", "tesla", "wise", "legal-and-general", "check-point"):
        assert by_key[key].ats == "unsupported" and by_key[key].note, key
    # Workable has an adapter but is switched off until it has met the live API.
    assert sum(company.ats == "workable" for company in polled) == 6
    assert config.settings.disabled_sources == frozenset()  # Workable verified 2026-11-16
    assert "careers.smartrecruiters.com" in config.settings.hosts_without_robots_txt


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


def test_shipped_settings_cache_robots_txt_for_a_day_and_wait_longer_on_a_rate_limit(config):
    assert config.settings.robots_cache_hours == 24
    assert config.settings.fail_after_consecutive == 2
    assert config.settings.fail_after_rate_limited == 3


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("robots_cache_hours: 24", "robots_cache_hours: -1", "robots_cache_hours must be 0 or"),
        ("fail_after_rate_limited: 3", "fail_after_rate_limited: 1", "must be at least"),
    ],
)
def test_a_mistake_in_the_cache_or_rate_limit_settings_is_rejected(tmp_path, old, new, message):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    path = tmp_path / "config" / "settings.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path / "config")


def test_shipped_polling_intervals_slow_down_only_the_expensive_sources(config):
    settings = config.settings
    for every_run in ("greenhouse", "lever", "ashby", "talnet", "jpmorgan", "workday"):
        assert settings.poll_interval(every_run) == 0, every_run
    assert settings.poll_interval("workday_search") == 60
    assert settings.poll_interval("google") == 60
    assert settings.poll_interval("not-listed") == 0  # anything not listed: every run
    # Every polled company's interval is set explicitly, so none is on a default by accident.
    kinds = {company.poll_kind for company in config.companies if adapter_for(company)}
    assert kinds <= set(settings.poll_every_minutes)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("google: 60", "gogle: 60", "no source is called 'gogle'"),
        ("google: 60", "google: -5", "google must be 0 or more"),
        ("google: 60", "google: hourly", "google: expected int, got str"),
        ("workday: 0 ", "workday: 60", "workday must be 0; set 'workday_search'"),
        ("every_minutes:", "every_minute:", "polling: unknown key 'every_minute'"),
        ("disabled: []", "disabled: [workabel]", "disabled: no source is called 'workabel'"),
        ("disabled: []", "disabled: [workday_search]", "no source is called 'workday_search'"),
    ],
)
def test_a_mistake_in_the_polling_intervals_is_rejected(tmp_path, old, new, message):
    config_dir = _config_with(tmp_path, "settings.yaml", old, new)
    with pytest.raises(ConfigError, match=message):
        load_config(config_dir)


def test_custom_companies_take_their_interval_from_their_module_name():
    assert make_company("google-deepmind", ats="custom", board="google").poll_kind == "google"
    assert make_company("jpmorgan-chase", ats="custom", board="jpmorgan").poll_kind == "jpmorgan"
    assert make_company("nvidia", ats="workday", board=None).poll_kind == "workday"


def test_prune_window_shorter_than_repost_window_is_rejected(tmp_path):
    config_dir = _config_with(
        tmp_path, "settings.yaml", "prune_closed_after_days: 90", "prune_closed_after_days: 7"
    )
    with pytest.raises(ConfigError, match="prune_closed_after_days must be >="):
        load_config(config_dir)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_config(tmp_path)


# --- profiles --------------------------------------------------------------------------


def test_topic_variable_is_derived_from_the_profile_key(profile):
    assert profile.topic_env == "NTFY_TOPIC_ABHINAV"
    assert make_profile(profile, "a-friend-2").topic_env == "NTFY_TOPIC_A_FRIEND_2"


def test_scope_is_industries_plus_named_companies(profile):
    watcher = make_profile(profile, "watcher", industries=("quant",), companies=("stripe",))

    assert watcher.watches(make_company("jane-street", industry="quant"))
    assert watcher.watches(make_company("stripe", industry="fintech"))  # named
    assert not watcher.watches(make_company("revolut", industry="fintech"))


def test_opt_in_company_is_only_watched_by_profiles_that_name_it(profile):
    private = make_company("private", industry="quant", opt_in=True)
    by_industry = make_profile(profile, "by-industry", industries=("quant",))
    by_name = make_profile(profile, "by-name", companies=("private",))

    assert not by_industry.watches(private)
    assert by_name.watches(private)


def _add_profile(config_dir, key, profile_yaml, filters_from=OWNER):
    folder = config_dir / "profiles" / key
    folder.mkdir()
    (folder / "profile.yaml").write_text(profile_yaml)
    if filters_from:
        shutil.copy(config_dir / "profiles" / filters_from / "filters.yaml", folder)
    return config_dir


def _summary(profile):
    """Everything about a loaded profile that decides what it receives."""
    return (
        profile.key,
        profile.name,
        profile.filters.hash,
        profile.industries,
        profile.companies,
        profile.faang_plus,
        profile.topic_env,
    )


def test_second_profile_loads_alongside_the_first(tmp_path, config, profile):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    _add_profile(
        tmp_path / "config",
        "friend",
        "name: Friend\nscope: {industries: [quant], companies: [stripe]}\nfaang_plus: [stripe]\n",
    )
    loaded = load_config(tmp_path / "config")

    assert [p.key for p in loaded.profiles] == [OWNER, "friend"]
    assert loaded.profile_errors == {}
    friend = loaded.profile("friend")
    assert friend.faang_plus == {"stripe"}
    assert friend.topic_env == "NTFY_TOPIC_FRIEND"
    watched = {company.key for company in loaded.companies if friend.watches(company)}
    quant = {company.key for company in loaded.companies if company.industry == "quant"}
    assert watched == quant | {"stripe"}
    # The first profile is exactly what it was without the second.
    assert _summary(loaded.profile(OWNER)) == _summary(profile)
    assert loaded.companies == config.companies


BROKEN_PROFILES = {
    "not YAML": ("name: [unclosed", "invalid YAML"),
    "no scope": ("name: Friend\n", "missing required key 'scope'"),
    "empty scope": ("name: Friend\nscope: {}\n", "scope does not include any company"),
    "misspelt industry": (
        "name: Friend\nscope: {industries: [quants]}\n",
        "no company has industry 'quants'",
    ),
    "unknown company": (
        "name: Friend\nscope: {companies: [no-such-co]}\n",
        "scope.companies: no company with key 'no-such-co'",
    ),
    "unknown FAANG+ company": (
        "name: Friend\nscope: {industries: [quant]}\nfaang_plus: [no-such-co]\n",
        "faang_plus: no company with key 'no-such-co'",
    ),
    "faang_plus outside scope": (
        "name: Friend\nscope: {industries: [quant]}\nfaang_plus: [stripe]\n",
        "faang_plus: 'stripe' is outside this profile's scope",
    ),
    "misspelt key": (
        "name: Friend\nscope: {industries: [quant]}\ndreams: [optiver]\n",
        "unknown key 'dreams'",
    ),
    "tier left over": (
        "name: Friend\nscope: {industries: [quant], tier: dream}\n",
        "scope: unknown key 'tier'",
    ),
}


@pytest.mark.parametrize("case", BROKEN_PROFILES)
def test_broken_profile_is_reported_and_the_others_load_unchanged(tmp_path, profile, case):
    profile_yaml, message = BROKEN_PROFILES[case]
    shutil.copytree(ROOT / "config", tmp_path / "config")
    _add_profile(tmp_path / "config", "friend", profile_yaml)

    loaded = load_config(tmp_path / "config")

    assert [p.key for p in loaded.profiles] == [OWNER]
    assert _summary(loaded.profile(OWNER)) == _summary(profile)
    assert list(loaded.profile_errors) == ["friend"]
    assert message in loaded.profile_errors["friend"]
    assert "profiles/friend/" in loaded.profile_errors["friend"]  # says which file


def test_profile_with_broken_filters_is_reported_and_the_others_load(tmp_path, profile):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    config_dir = _add_profile(
        tmp_path / "config", "friend", "name: Friend\nscope: {industries: [quant]}\n"
    )
    filters = config_dir / "profiles" / "friend" / "filters.yaml"
    filters.write_text(filters.read_text().replace("field_match: lenient", "field_match: loose"))

    loaded = load_config(config_dir)
    assert _summary(loaded.profile(OWNER)) == _summary(profile)
    assert "field_match must be one of" in loaded.profile_errors["friend"]

    filters.unlink()
    assert "filters.yaml: file not found" in load_config(config_dir).profile_errors["friend"]


def test_profile_folder_name_must_be_a_plain_key(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    _add_profile(tmp_path / "config", "My_Friend", "name: Friend\nscope: {industries: [quant]}\n")
    errors = load_config(tmp_path / "config").profile_errors
    assert "lowercase letters, digits and hyphens" in errors["My_Friend"]


def test_config_with_no_usable_profile_is_an_error(tmp_path):
    config_dir = _config_with(tmp_path, OWNER_PROFILE, "name: Abhinav", "nom: Abhinav")
    with pytest.raises(ConfigError, match="unknown key 'nom'"):
        load_config(config_dir)

    shutil.rmtree(config_dir / "profiles" / OWNER)
    with pytest.raises(ConfigError, match="no profiles found"):
        load_config(config_dir)


# --- eligibility and the new settings ---------------------------------------------------


def test_eligibility_rules_load_and_their_hash_follows_the_rules(tmp_path, filters):
    rules = filters.eligibility
    assert rules.by_title == {"first_year"}
    assert rules.unless_restricted == {"spring_week", "insight"}
    assert rules.applies_to("summer_internship") and not rules.applies_to("graduate")
    assert rules.hash

    config_dir = _config_with(tmp_path, OWNER_FILTERS, "    - no matter what year\n", "")
    changed = load_config(config_dir).profile(OWNER).filters
    assert changed.eligibility.hash != rules.hash
    assert changed.hash != filters.hash


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "  explicit_only: [summer_internship]",
            "  explicit_only: [summer_internship, apprenticeship]",
            "explicit_only: no role type called apprenticeship",
        ),
        (
            "  explicit_only: [summer_internship]",
            "  explicit_only: [summer_internship, insight]",
            "insight is in two lists",
        ),
    ],
)
def test_eligibility_mistakes_disable_the_profile(tmp_path, old, new, message):
    config_dir = _config_with(tmp_path, OWNER_FILTERS, old, new)
    # With only one profile, a broken one leaves nothing to run: the config is an error.
    with pytest.raises(ConfigError, match=message):
        load_config(config_dir)


def test_filters_without_an_eligibility_section_check_nothing(tmp_path):
    config_dir = _config_with(tmp_path, OWNER_FILTERS, "\neligibility:\n", "\nnot_eligibility:\n")
    text = (config_dir / OWNER_FILTERS).read_text()
    (config_dir / OWNER_FILTERS).write_text(text.split("\nnot_eligibility:\n")[0] + "\n")
    rules = load_config(config_dir).profile(OWNER).filters.eligibility
    assert not rules.applies_to("summer_internship") and rules.hash == ""


def test_readme_profile_and_description_budget_are_read_and_checked(tmp_path, config):
    assert config.settings.readme_profile == OWNER
    assert config.settings.descriptions_per_run == 60

    config_dir = _config_with(tmp_path, "settings.yaml", "profile: abhinav", "profile: nobody")
    with pytest.raises(ConfigError, match="readme.profile: no profile called 'nobody'"):
        load_config(config_dir)


def test_settings_without_a_readme_section_write_no_table(tmp_path):
    config_dir = _config_with(
        tmp_path, "settings.yaml", "readme:\n  profile: abhinav", "readme: {}"
    )
    assert load_config(config_dir).settings.readme_profile is None


# --- pauses --------------------------------------------------------------------------------


def test_the_shipped_workday_pause_loads(config):
    pause = config.settings.pauses["workday"]
    assert pause.since.isoformat() == "2026-11-16T17:00:00+00:00"
    assert (pause.probe_after_hours, pause.probe_every_hours) == (48, 6)
    assert (pause.resume_every_minutes, pause.give_up_after_days) == (180, 7)
    assert config.company(pause.probe_company).ats == "workday"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("probe_company: lloyds-banking-group", "probe_company: anthropic", "no workday company"),
        ('since: "2026-11-16T17:00:00+00:00"', 'since: "2026-11-16T17:00:00"', "needs a time zone"),
        ('since: "2026-11-16T17:00:00+00:00"', 'since: "soon"', "not an ISO date"),
        ("probe_every_hours: 6", "probe_every_hours: 0", "at least 1"),
        ("probe_every_hours: 6", "probe_evry_hours: 6", "unknown key 'probe_evry_hours'"),
        ("  workday:\n    since", "  greenhouse:\n    since", r"only \['workday'\] can be paused"),
    ],
)
def test_pause_mistakes_are_rejected(tmp_path, old, new, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_config_with(tmp_path, "settings.yaml", old, new))


def test_the_description_budget_for_changed_rules_is_at_least_the_usual_one(tmp_path, config):
    assert config.settings.descriptions_when_rules_change == 400
    config_dir = _config_with(
        tmp_path,
        "settings.yaml",
        "descriptions_when_rules_change: 400",
        "descriptions_when_rules_change: 10",
    )
    with pytest.raises(ConfigError, match="at least descriptions_per_run"):
        load_config(config_dir)


def test_an_eightfold_company_needs_its_host_and_domain(tmp_path):
    entry = "- {key: acme, name: Acme, ats: eightfold, board: acme.com}"
    with pytest.raises(ConfigError, match="host"):
        load_config(_companies_file(tmp_path / "a", entry))
    entry = "- {key: acme, name: Acme, ats: eightfold, host: acme.eightfold.ai}"
    with pytest.raises(ConfigError, match="board is required"):
        load_config(_companies_file(tmp_path / "b", entry))


def test_a_private_company_may_be_watched_by_one_profile_only(tmp_path):
    entry = "- {key: acme, name: Acme, industry: quant, ats: greenhouse, board: a, private: true}"
    config_dir = _companies_file(tmp_path, entry)
    assert load_config(config_dir).company("acme").private

    owner_dir = (config_dir / OWNER_PROFILE).parent
    shutil.copytree(owner_dir, owner_dir.parent / "someone-else")
    with pytest.raises(ConfigError, match="acme is private .* all watch it"):
        load_config(config_dir)
