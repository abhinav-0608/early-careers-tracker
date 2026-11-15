from conftest import MATCHED, NOW, make_job
from jobtracker.dedupe import Reconciler
from jobtracker.state import SourceState, State, load_state, save_state


def test_missing_state_directory_loads_as_empty(tmp_path):
    state = load_state(tmp_path / "does-not-exist")
    assert state.jobs == {}
    assert state.sources == {}


def test_state_round_trips_through_disk(tmp_path, settings):
    state = State()
    Reconciler(state, NOW, settings).observe(make_job("1"), MATCHED, baseline=False)
    state.sources["greenhouse:acme"] = SourceState(filters_hash="abc", has_returned_jobs=True)

    save_state(state, tmp_path)
    assert load_state(tmp_path) == state


def test_saving_unchanged_state_writes_identical_bytes(tmp_path, settings):
    # The workflow commits only when the files differ, so an uneventful run must not
    # change them.
    state = State()
    Reconciler(state, NOW, settings).observe(make_job("1"), MATCHED, baseline=False)
    save_state(state, tmp_path)
    before = (tmp_path / "jobs.json").read_bytes()

    reloaded = load_state(tmp_path)
    later_the_same_day = NOW.replace(hour=23)
    Reconciler(reloaded, later_the_same_day, settings).observe(make_job("1"), MATCHED, False)
    save_state(reloaded, tmp_path)

    assert (tmp_path / "jobs.json").read_bytes() == before
