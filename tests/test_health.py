from datetime import timedelta

from conftest import NOW, make_company, make_job
from jobtracker import health
from jobtracker.models import EMPTY, ERROR, OK, SourceResult
from jobtracker.state import SourceState

COMPANY = make_company("acme")
RUN = timedelta(minutes=15)


def failed(error="HTTP 500 from example.com"):
    return SourceResult(company=COMPANY, status=ERROR, error=error)


def succeeded():
    return SourceResult(company=COMPANY, status=OK, jobs=[make_job()])


def one_run(source, result, now, settings, delivered=True):
    """Record one run's outcome and, as the pipeline does, mark alerts once delivered."""
    report = health.HealthReport()
    health.record_outcome(source, result, now, settings, report)
    if report.failing and delivered:
        health.mark_alerted({COMPANY.source_key: source}, report, now)
    return report


def test_a_single_failure_does_not_alert(settings):
    source = SourceState()
    report = one_run(source, failed(), NOW, settings)

    assert not report
    assert source.consecutive_failures == 1
    assert source.failing_since == "2026-11-15T12:00:00+00:00"


def test_second_consecutive_failure_alerts_once(settings):
    source = SourceState()
    one_run(source, failed(), NOW, settings)
    second = one_run(source, failed(), NOW + RUN, settings)
    third = one_run(source, failed(), NOW + 2 * RUN, settings)

    assert [result.company.key for result in second.failing] == ["acme"]
    assert not third  # already alerted; no repeat every run


def test_reminder_after_24_hours_while_still_broken(settings):
    source = SourceState()
    one_run(source, failed(), NOW, settings)
    one_run(source, failed(), NOW + RUN, settings)  # alert sent here

    almost_a_day = one_run(source, failed(), NOW + RUN + timedelta(hours=23), settings)
    a_day_later = one_run(source, failed(), NOW + RUN + timedelta(hours=24), settings)

    assert not almost_a_day
    assert a_day_later.failing


def test_alert_is_retried_if_it_could_not_be_delivered(settings):
    source = SourceState()
    one_run(source, failed(), NOW, settings)
    undelivered = one_run(source, failed(), NOW + RUN, settings, delivered=False)
    retried = one_run(source, failed(), NOW + 2 * RUN, settings)

    assert undelivered.failing
    assert retried.failing


def test_recovery_is_announced_once_and_resets_the_streak(settings):
    source = SourceState()
    one_run(source, failed(), NOW, settings)
    one_run(source, failed(), NOW + RUN, settings)

    recovered = one_run(source, succeeded(), NOW + 2 * RUN, settings)
    quiet = one_run(source, succeeded(), NOW + 3 * RUN, settings)

    assert [result.company.key for result in recovered.recovered] == ["acme"]
    assert not quiet
    assert source == SourceState()  # back to a clean slate


def test_recovery_without_a_prior_alert_is_silent(settings):
    source = SourceState()
    one_run(source, failed(), NOW, settings)
    assert not one_run(source, succeeded(), NOW + RUN, settings)


def test_long_outage_does_not_keep_changing_the_stored_state(settings):
    source = SourceState()
    for run in range(3):
        one_run(source, failed(), NOW + run * RUN, settings)
    snapshot = SourceState(**vars(source))
    for run in range(3, 20):
        one_run(source, failed(), NOW + run * RUN, settings)

    assert source == snapshot


def test_board_that_suddenly_returns_nothing_is_treated_as_failing(settings):
    source = SourceState()
    health.check_empty(source, succeeded(), settings)
    assert source.has_returned_jobs

    empty = SourceResult(company=COMPANY, status=OK, jobs=[])
    health.check_empty(source, empty, settings)

    assert empty.status == EMPTY
    assert empty.error == "returned 0 jobs after previously returning some"


def test_board_that_has_never_had_jobs_may_be_empty(settings):
    source = SourceState()
    empty = SourceResult(company=COMPANY, status=OK, jobs=[])
    health.check_empty(source, empty, settings)
    assert empty.status == OK


def test_persistently_empty_board_is_eventually_accepted(settings):
    source = SourceState(has_returned_jobs=True)
    statuses = []
    for _ in range(settings.accept_empty_after_runs + 1):
        result = SourceResult(company=COMPANY, status=OK, jobs=[])
        health.check_empty(source, result, settings)
        statuses.append(result.status)

    assert statuses == [EMPTY] * (settings.accept_empty_after_runs - 1) + [OK, OK]


def test_notification_lists_failures_and_recoveries(settings):
    other = make_company("other", ats="lever")
    report = health.HealthReport(
        failing=[failed()], recovered=[SourceResult(company=other, status=OK)]
    )
    sources = {COMPANY.source_key: SourceState(failing_since="2026-11-15T12:00:00+00:00")}
    notification = health.health_notification(report, sources)

    assert notification.title == "Source failing: Acme"
    assert notification.priority == 4
    assert notification.message.splitlines() == [
        "Acme (greenhouse): error, HTTP 500 from example.com",
        "  failing since 2026-11-15T12:00:00+00:00",
        "Recovered: Other (lever)",
    ]


def test_recovery_only_notification_is_low_priority(settings):
    report = health.HealthReport(recovered=[succeeded()])
    notification = health.health_notification(report, {})
    assert notification.title == "Sources recovered"
    assert notification.priority == 2
