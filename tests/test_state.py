from conftest import MATCHED, NOW, make_job
from jobtracker.dedupe import Reconciler
from jobtracker.state import (
    HostsState,
    ProfileState,
    SourceState,
    load_hosts,
    load_profile_state,
    load_sources,
    profile_path,
    save_hosts,
    save_profile_state,
    save_sources,
)


def test_missing_state_directory_loads_as_empty(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert load_sources(missing) == {}
    assert load_profile_state(missing, "anyone") == ProfileState()


def test_state_round_trips_through_disk(tmp_path, settings):
    state = ProfileState(baselines={"greenhouse:acme": "abc"})
    Reconciler(state, NOW, settings).observe(make_job("1"), MATCHED, baseline=False)
    sources = {"greenhouse:acme": SourceState(has_returned_jobs=True)}

    save_profile_state(state, tmp_path, "someone")
    save_sources(sources, tmp_path)
    assert load_profile_state(tmp_path, "someone") == state
    assert load_sources(tmp_path) == sources


def test_each_profile_has_its_own_file(tmp_path, settings):
    state = ProfileState()
    Reconciler(state, NOW, settings).observe(make_job("1"), MATCHED, baseline=False)
    save_profile_state(state, tmp_path, "one")
    before = profile_path(tmp_path, "one").read_bytes()

    save_profile_state(ProfileState(baselines={"greenhouse:acme": "abc"}), tmp_path, "two")

    assert profile_path(tmp_path, "one").read_bytes() == before
    assert load_profile_state(tmp_path, "one") == state
    assert load_profile_state(tmp_path, "two").jobs == {}


def test_saving_unchanged_state_writes_identical_bytes(tmp_path, settings):
    # The workflow commits only when the files differ, so an uneventful run must not
    # change them.
    state = ProfileState()
    Reconciler(state, NOW, settings).observe(make_job("1"), MATCHED, baseline=False)
    save_profile_state(state, tmp_path, "someone")
    before = profile_path(tmp_path, "someone").read_bytes()

    reloaded = load_profile_state(tmp_path, "someone")
    later_the_same_day = NOW.replace(hour=23)
    Reconciler(reloaded, later_the_same_day, settings).observe(make_job("1"), MATCHED, False)
    save_profile_state(reloaded, tmp_path, "someone")

    assert profile_path(tmp_path, "someone").read_bytes() == before


# --- migration from the single-user (version 1) files ----------------------------------


def test_host_memory_round_trips_and_is_stable_on_disk(tmp_path):
    hosts = HostsState(
        robots={
            "example.com": {
                "fetched_at": "2026-11-15T12:00:00+00:00",
                "rules": ["Disallow: /private/"],
                "crawl_delay": 10.0,
            }
        },
        backoff={"wd5.myworkdayjobs.com": {"retry_at": "2026-11-15T12:30:00+00:00", "strikes": 1}},
    )
    save_hosts(hosts, tmp_path)
    first = (tmp_path / "hosts.json").read_bytes()

    assert load_hosts(tmp_path) == hosts
    save_hosts(load_hosts(tmp_path), tmp_path)
    assert (tmp_path / "hosts.json").read_bytes() == first


def test_missing_or_damaged_host_memory_is_just_an_empty_cache(tmp_path):
    assert load_hosts(tmp_path) == HostsState()
    (tmp_path / "hosts.json").write_text("{ not json", encoding="utf-8")
    assert load_hosts(tmp_path) == HostsState()
