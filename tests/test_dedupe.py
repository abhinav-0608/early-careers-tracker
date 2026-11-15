"""Dedupe and state reconciliation. Each helper call below simulates one run."""

from dataclasses import replace
from datetime import timedelta

from conftest import MATCHED, NOW, make_job
from jobtracker.dedupe import BASELINED, KNOWN, NEW, REPOST, Reconciler, fingerprint
from jobtracker.state import CLOSED, OPEN, State

SOURCE = "greenhouse:acme"
DAY = timedelta(days=1)


def run(state, settings, now, jobs, baseline=False, fetched=True, configured=True):
    """One run: observe the given jobs, then close whatever is missing."""
    reconciler = Reconciler(state, now, settings)
    outcomes = [reconciler.observe(job, MATCHED, baseline) for job in jobs]
    closed = reconciler.close_missing(
        fetched_sources={SOURCE} if fetched else set(),
        configured_sources={SOURCE} if configured else set(),
    )
    reconciler.prune()
    return outcomes, closed


def open_then_close(state, settings, job, start=NOW):
    """Track a job, then let it go missing until it closes. Returns the closing time."""
    run(state, settings, start, [job])
    for missed in range(1, settings.close_after_missing_runs + 1):
        closed_at = start + missed * DAY
        run(state, settings, closed_at, [])
    assert state.jobs[job.key].status == CLOSED
    return closed_at


# --- primary key -----------------------------------------------------------------------


def test_first_sighting_is_new_and_awaits_an_alert(settings):
    state = State()
    outcomes, _ = run(state, settings, NOW, [make_job("1")])

    assert outcomes == [NEW]
    record = state.jobs["greenhouse:acme:1"]
    assert record.status == OPEN
    assert record.notified is False
    assert record.first_seen == "2026-11-15T12:00:00+00:00"
    assert record.last_seen == "2026-11-15"


def test_same_id_again_is_known_and_updates_last_seen_only(settings):
    state = State()
    run(state, settings, NOW, [make_job("1")])
    outcomes, _ = run(state, settings, NOW + 3 * DAY, [make_job("1")])

    assert outcomes == [KNOWN]
    record = state.jobs["greenhouse:acme:1"]
    assert record.first_seen == "2026-11-15T12:00:00+00:00"
    assert record.last_seen == "2026-11-18"


def test_edited_listing_keeps_its_identity(settings):
    state = State()
    run(state, settings, NOW, [make_job("1", title="Spring Week 2027")])
    edited = make_job("1", title="Spring Week 2027 - Technology", locations=("London", "Leeds"))
    outcomes, _ = run(state, settings, NOW + DAY, [edited])

    assert outcomes == [KNOWN]
    record = state.jobs["greenhouse:acme:1"]
    assert record.title == "Spring Week 2027 - Technology"
    assert record.locations == ["London", "Leeds"]
    assert record.fingerprint == fingerprint(edited)
    assert len(state.jobs) == 1


def test_baseline_records_jobs_as_already_notified(settings):
    state = State()
    outcomes, _ = run(state, settings, NOW, [make_job("1")], baseline=True)

    assert outcomes == [BASELINED]
    assert state.jobs["greenhouse:acme:1"].notified is True


# --- fallback fingerprint --------------------------------------------------------------


def test_fingerprint_ignores_case_punctuation_and_location_order():
    a = make_job("1", title="Spring Week 2027 – Technology", locations=("London, UK", "Leeds"))
    b = make_job("2", title="spring week 2027: technology", locations=("Leeds", "London UK"))
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_keeps_the_year_and_the_company():
    this_year = make_job("1", title="Spring Week 2027")
    assert fingerprint(this_year) != fingerprint(make_job("2", title="Spring Week 2028"))
    assert fingerprint(this_year) != fingerprint(make_job("1", company="other"))


def test_repost_under_a_new_id_while_still_open_is_not_alerted(settings):
    state = State()
    run(state, settings, NOW, [make_job("1")])
    outcomes, _ = run(state, settings, NOW + DAY, [make_job("2")])

    assert outcomes == [REPOST]
    assert list(state.jobs) == ["greenhouse:acme:2"]  # the record follows the newer posting
    record = state.jobs["greenhouse:acme:2"]
    assert record.aliases == ["greenhouse:acme:1"]
    assert record.url == "https://example.com/jobs/2"
    assert record.first_seen == "2026-11-15T12:00:00+00:00"


def test_old_and_new_ids_listed_together_stay_one_job(settings):
    state = State()
    run(state, settings, NOW, [make_job("1")])
    run(state, settings, NOW + DAY, [make_job("1"), make_job("2")])
    outcomes, closed = run(state, settings, NOW + 2 * DAY, [make_job("1"), make_job("2")])

    assert outcomes == [KNOWN, KNOWN]
    assert closed == []
    assert len(state.jobs) == 1


def test_old_id_alone_keeps_a_reposted_job_open(settings):
    state = State()
    run(state, settings, NOW, [make_job("1")])
    run(state, settings, NOW + DAY, [make_job("2")])
    for day in range(2, 6):
        _, closed = run(state, settings, NOW + day * DAY, [make_job("1")])
        assert closed == []
    assert state.jobs["greenhouse:acme:2"].status == OPEN


def test_repost_of_a_job_closed_within_the_window_reopens_it_without_an_alert(settings):
    state = State()
    closed_at = open_then_close(state, settings, make_job("1"))
    state.jobs["greenhouse:acme:1"].notified = True

    on_the_boundary = closed_at + settings.repost_window_days * DAY
    outcomes, _ = run(state, settings, on_the_boundary, [make_job("2")])

    assert outcomes == [REPOST]
    record = state.jobs["greenhouse:acme:2"]
    assert record.status == OPEN
    assert record.closed_at is None
    assert record.notified is True  # nothing new to announce


def test_fingerprint_match_older_than_the_window_is_a_new_job(settings):
    """Spring weeks reuse the same title every year: last year's closed record must not
    swallow this year's alert."""
    assert settings.repost_window_days == 30
    state = State()
    closed_at = open_then_close(state, settings, make_job("1", title="Spring Week"))
    state.jobs["greenhouse:acme:1"].notified = True

    one_day_past_the_window = closed_at + 31 * DAY
    outcomes, _ = run(
        state, settings, one_day_past_the_window, [make_job("2", title="Spring Week")]
    )

    assert outcomes == [NEW]
    new_record = state.jobs["greenhouse:acme:2"]
    assert new_record.notified is False  # will be alerted
    assert new_record.aliases == []
    assert new_record.first_seen == one_day_past_the_window.isoformat(timespec="seconds")
    assert state.jobs["greenhouse:acme:1"].status == CLOSED  # last year's record is untouched


def test_repost_window_is_configurable(settings):
    short_window = replace(settings, repost_window_days=7)
    state = State()
    closed_at = open_then_close(state, short_window, make_job("1"))

    outcomes, _ = run(state, short_window, closed_at + 8 * DAY, [make_job("2")])
    assert outcomes == [NEW]


def test_same_id_reopened_within_the_window_is_not_alerted(settings):
    state = State()
    closed_at = open_then_close(state, settings, make_job("1"))
    outcomes, _ = run(state, settings, closed_at + 5 * DAY, [make_job("1")])

    assert outcomes == [KNOWN]
    assert state.jobs["greenhouse:acme:1"].status == OPEN


def test_same_id_reappearing_after_the_window_is_a_new_job(settings):
    # Some companies reopen last year's requisition instead of creating a new one.
    state = State()
    closed_at = open_then_close(state, settings, make_job("1"))
    state.jobs["greenhouse:acme:1"].notified = True

    outcomes, _ = run(state, settings, closed_at + 31 * DAY, [make_job("1")])

    assert outcomes == [NEW]
    record = state.jobs["greenhouse:acme:1"]
    assert record.status == OPEN
    assert record.notified is False
    assert len(state.jobs) == 1


def test_two_identical_new_postings_in_one_run_alert_once(settings):
    state = State()
    outcomes, _ = run(state, settings, NOW, [make_job("1"), make_job("2")])

    assert outcomes == [NEW, REPOST]
    assert len(state.jobs) == 1


# --- closing ---------------------------------------------------------------------------


def test_job_closes_only_after_being_missing_for_two_successful_runs(settings):
    state = State()
    run(state, settings, NOW, [make_job("1")])

    _, closed = run(state, settings, NOW + DAY, [])
    assert closed == []
    assert state.jobs["greenhouse:acme:1"].missing_runs == 1

    _, closed = run(state, settings, NOW + 2 * DAY, [])
    assert [job.key for job in closed] == ["greenhouse:acme:1"]
    record = state.jobs["greenhouse:acme:1"]
    assert record.status == CLOSED
    assert record.closed_at == "2026-11-17"


def test_reappearing_after_one_missed_run_resets_the_count(settings):
    state = State()
    run(state, settings, NOW, [make_job("1")])
    run(state, settings, NOW + DAY, [])
    run(state, settings, NOW + 2 * DAY, [make_job("1")])
    _, closed = run(state, settings, NOW + 3 * DAY, [])

    assert closed == []
    assert state.jobs["greenhouse:acme:1"].missing_runs == 1


def test_failed_source_never_closes_anything(settings):
    state = State()
    run(state, settings, NOW, [make_job("1")])
    for day in range(1, 10):
        _, closed = run(state, settings, NOW + day * DAY, [], fetched=False)
        assert closed == []
    assert state.jobs["greenhouse:acme:1"].missing_runs == 0


def test_jobs_of_a_company_removed_from_config_close_immediately(settings):
    state = State()
    run(state, settings, NOW, [make_job("1")])
    _, closed = run(state, settings, NOW + DAY, [], fetched=False, configured=False)

    assert [job.key for job in closed] == ["greenhouse:acme:1"]


def test_closed_jobs_are_pruned_after_the_retention_period(settings):
    state = State()
    closed_at = open_then_close(state, settings, make_job("1"))

    run(state, settings, closed_at + settings.prune_closed_after_days * DAY, [])
    assert "greenhouse:acme:1" in state.jobs
    run(state, settings, closed_at + (settings.prune_closed_after_days + 1) * DAY, [])
    assert state.jobs == {}
