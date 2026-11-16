import json

from conftest import MATCHED, NOW, make_job
from jobtracker.dedupe import Reconciler
from jobtracker.state import (
    LEGACY_OWNER,
    HostsState,
    ProfileState,
    SourceState,
    load_hosts,
    load_profile_state,
    load_sources,
    migrate_legacy,
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


def write_legacy(state_dir, settings):
    """Version 1 layout: one jobs.json, and the baseline hash inside sources.json."""
    state = ProfileState()
    Reconciler(state, NOW, settings).observe(make_job("1"), MATCHED, baseline=True)
    save_profile_state(state, state_dir, "scratch")  # borrow the job serialisation
    jobs = json.loads(profile_path(state_dir, "scratch").read_text())["jobs"]
    profile_path(state_dir, "scratch").unlink()
    profile_path(state_dir, "scratch").parent.rmdir()

    source = {
        "alerted": True,
        "consecutive_failures": 2,
        "empty_runs": 0,
        "failing_since": "2026-11-15T11:30:00+00:00",
        "filters_hash": "abc123",
        "has_returned_jobs": True,
        "last_alert_at": "2026-11-15T11:45:00+00:00",
    }
    never_fetched = {**source, "filters_hash": None}
    sources = {"greenhouse:acme": source, "lever:other": never_fetched}
    (state_dir / "jobs.json").write_text(json.dumps({"version": 1, "jobs": jobs}))
    (state_dir / "sources.json").write_text(json.dumps({"version": 1, "sources": sources}))
    return state.jobs


def test_legacy_files_are_read_as_the_owners_state_before_migrating(tmp_path, settings):
    jobs = write_legacy(tmp_path, settings)

    # As a dry run sees it: nothing on disk has changed yet.
    owner = load_profile_state(tmp_path, LEGACY_OWNER)
    assert owner.jobs == jobs
    assert owner.jobs["greenhouse:acme:1"].notified is True
    assert owner.baselines == {"greenhouse:acme": "abc123"}
    assert load_sources(tmp_path)["greenhouse:acme"] == SourceState(
        has_returned_jobs=True,
        consecutive_failures=2,
        failing_since="2026-11-15T11:30:00+00:00",
        alerted=True,
        last_alert_at="2026-11-15T11:45:00+00:00",
    )
    assert (tmp_path / "jobs.json").exists()


def test_nobody_else_inherits_the_legacy_files(tmp_path, settings):
    write_legacy(tmp_path, settings)
    assert load_profile_state(tmp_path, "a-friend") == ProfileState()


def test_migration_moves_everything_and_loses_nothing(tmp_path, settings):
    write_legacy(tmp_path, settings)
    owner_before = load_profile_state(tmp_path, LEGACY_OWNER)
    sources_before = load_sources(tmp_path)

    assert migrate_legacy(tmp_path) is True

    assert not (tmp_path / "jobs.json").exists()
    assert profile_path(tmp_path, LEGACY_OWNER).exists()
    assert load_profile_state(tmp_path, LEGACY_OWNER) == owner_before
    assert load_sources(tmp_path) == sources_before
    assert "filters_hash" not in (tmp_path / "sources.json").read_text()


def test_migration_runs_once(tmp_path, settings):
    write_legacy(tmp_path, settings)
    migrate_legacy(tmp_path)
    after_first = {path.name: path.read_bytes() for path in tmp_path.rglob("*.json")}

    assert migrate_legacy(tmp_path) is False
    assert {path.name: path.read_bytes() for path in tmp_path.rglob("*.json")} == after_first


def test_migration_interrupted_half_way_finishes_next_time(tmp_path, settings):
    write_legacy(tmp_path, settings)
    owner_before = load_profile_state(tmp_path, LEGACY_OWNER)
    # The owner's file was written, then the run died before the old files were dealt with.
    save_profile_state(owner_before, tmp_path, LEGACY_OWNER)

    assert migrate_legacy(tmp_path) is True
    assert load_profile_state(tmp_path, LEGACY_OWNER) == owner_before
    assert not (tmp_path / "jobs.json").exists()


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
