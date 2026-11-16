from datetime import timedelta

import pytest

from conftest import NOW, make_company, make_job
from jobtracker import health
from jobtracker.models import BLOCKED, EMPTY, ERROR, OK, RATE_LIMITED, TIMEOUT, SourceResult
from jobtracker.state import SharedState, SourceState

COMPANY = make_company("acme")
RUN = timedelta(minutes=15)


def failed(error="HTTP 500 from example.com"):
    return SourceResult(company=COMPANY, status=ERROR, error=error)


def rate_limited():
    return SourceResult(company=COMPANY, status=RATE_LIMITED, error="example.com: HTTP 429")


def succeeded():
    return SourceResult(company=COMPANY, status=OK, jobs=[make_job()])


def one_run(source, result, now, settings, delivered=True, shared=None):
    """One run, as the pipeline does it. Returns the alert it would send, or None."""
    shared = shared if shared is not None else SharedState()
    report = health.HealthReport()
    health.record_outcome(source, result, now, settings, report)
    sources = {result.company.source_key: source}
    alert = health.plan_alert(report, sources, shared, now, settings)
    if alert and delivered:
        health.mark_alerted(sources, report, alert, shared, now)
    health.forget_quiet_groups(shared, now, settings)
    return alert


def several(results, sources, now, settings, shared):
    """One run over several sources at once."""
    report = health.HealthReport()
    for result in results:
        health.record_outcome(sources[result.company.source_key], result, now, settings, report)
    alert = health.plan_alert(report, sources, shared, now, settings)
    if alert:
        health.mark_alerted(sources, report, alert, shared, now)
    return alert


def test_a_single_failure_does_not_alert(settings):
    source = SourceState()
    assert one_run(source, failed(), NOW, settings) is None
    assert source.consecutive_failures == 1
    assert source.failing_since == "2026-11-15T12:00:00+00:00"


def test_second_consecutive_failure_alerts_once(settings):
    source, shared = SourceState(), SharedState()
    one_run(source, failed(), NOW, settings, shared=shared)
    second = one_run(source, failed(), NOW + RUN, settings, shared=shared)
    third = one_run(source, failed(), NOW + 2 * RUN, settings, shared=shared)

    assert second.notification.title == "Acme (Greenhouse): failing (HTTP 500)"
    assert third is None  # already alerted; no repeat every run


def test_reminder_after_24_hours_while_still_broken(settings):
    source, shared = SourceState(), SharedState()
    one_run(source, failed(), NOW, settings, shared=shared)
    one_run(source, failed(), NOW + RUN, settings, shared=shared)  # alert sent here

    almost = one_run(source, failed(), NOW + RUN + timedelta(hours=23), settings, shared=shared)
    a_day = one_run(source, failed(), NOW + RUN + timedelta(hours=24), settings, shared=shared)

    assert almost is None
    assert a_day is not None


def test_alert_is_retried_if_it_could_not_be_delivered(settings):
    source, shared = SourceState(), SharedState()
    one_run(source, failed(), NOW, settings, shared=shared)
    undelivered = one_run(source, failed(), NOW + RUN, settings, delivered=False, shared=shared)
    retried = one_run(source, failed(), NOW + 2 * RUN, settings, shared=shared)

    assert undelivered is not None
    assert retried is not None


def test_recovery_is_announced_once_and_resets_the_streak(settings):
    source = SourceState()
    one_run(source, failed(), NOW, settings)
    one_run(source, failed(), NOW + RUN, settings)

    recovered = one_run(source, succeeded(), NOW + 2 * RUN, settings)
    quiet = one_run(source, succeeded(), NOW + 3 * RUN, settings)

    assert recovered.notification.title == "Acme (Greenhouse) recovered"
    assert recovered.notification.priority == 2
    assert quiet is None
    assert source == SourceState()  # back to a clean slate


def test_recovery_without_a_prior_alert_is_silent(settings):
    source = SourceState()
    one_run(source, failed(), NOW, settings)
    assert one_run(source, succeeded(), NOW + RUN, settings) is None


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


def test_failures_are_grouped_by_platform_and_cause_in_one_alert(settings):
    workday = [make_company(f"bank{n}", name=f"Bank {n}", ats="workday") for n in range(64)]
    lever = make_company("other", name="Other", ats="lever")
    sources = {c.source_key: SourceState() for c in (*workday, lever)}
    shared = SharedState()
    challenge = "wd3.myworkdayjobs.com: HTTP 429, a Cloudflare challenge; next attempt ..."

    def run(now, lever_too=True):
        results = [SourceResult(company=c, status=RATE_LIMITED, error=challenge) for c in workday]
        if lever_too:
            results.append(SourceResult(company=lever, status=TIMEOUT, error="ReadTimeout"))
        return several(results, sources, now, settings, shared)

    # A 429 needs three refusals, a timeout two: both groups are due on the third run.
    assert run(NOW, lever_too=False) is None and run(NOW + RUN) is None
    alert = run(NOW + 2 * RUN)
    lines = alert.notification.message.splitlines()
    assert alert.notification.title == "65 sources failing"
    assert lines[0] == "Workday: 64 sources blocked (Cloudflare 429)"
    assert lines[1] == "  Bank 0, Bank 1, Bank 10, Bank 11, Bank 12, Bank 13 +58 more"
    assert lines[2].startswith("  failing since ")
    assert lines[3:6] == ["Lever: 1 source timing out", "  Other", "  ReadTimeout"]
    assert set(shared.alert_groups) == {
        "Workday: blocked (Cloudflare 429)",
        "Lever: timing out",
    }


def test_a_group_is_announced_at_most_once_a_day_however_many_sources_join(settings):
    companies = [make_company(f"c{n}", ats="workday") for n in range(3)]
    sources = {c.source_key: SourceState() for c in companies}
    shared = SharedState()

    def fail(subset, now):
        results = [SourceResult(company=c, status=ERROR, error="HTTP 503") for c in subset]
        return several(results, sources, now, settings, shared)

    fail(companies[:1], NOW)
    first = fail(companies[:1], NOW + RUN)
    assert first.notification.title == "C0 (Workday): failing (HTTP 503)"
    # Two more Workday boards fail the same way an hour later: the group is not new.
    fail(companies, NOW + 4 * RUN)
    assert fail(companies, NOW + 5 * RUN) is None
    assert all(source.alerted for source in sources.values())  # covered by that alert
    # A day after the group's alert, one reminder for all of them.
    reminder = fail(companies, NOW + RUN + timedelta(hours=24))
    assert reminder.notification.title == "Workday: 3 sources failing (HTTP 503)"


def test_a_group_alerted_per_source_before_grouping_is_not_announced_again(settings):
    """State written before alerts were grouped: every source already alerted today."""
    companies = [make_company(f"c{n}", ats="workday") for n in range(2)]
    stamp = (NOW - timedelta(hours=2)).isoformat()
    sources = {
        c.source_key: SourceState(consecutive_failures=3, alerted=True, last_alert_at=stamp)
        for c in companies
    }
    results = [SourceResult(company=c, status=ERROR, error="HTTP 503") for c in companies]
    assert several(results, sources, NOW, settings, SharedState()) is None


def test_recoveries_are_grouped_by_platform(settings):
    companies = [make_company(f"c{n}", ats="workday") for n in range(3)]
    sources = {c.source_key: SourceState(alerted=True) for c in companies}
    results = [SourceResult(company=c, status=OK, jobs=[make_job()]) for c in companies]
    alert = several(results, sources, NOW, settings, SharedState())
    assert alert.notification.title == "Workday: 3 sources recovered"
    assert alert.notification.message == "Recovered: Workday: 3 sources"


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (RATE_LIMITED, "x: HTTP 429, a Cloudflare challenge; next", "blocked (Cloudflare 429)"),
        (RATE_LIMITED, "x: HTTP 429, rate limited; next", "rate limited (429)"),
        (BLOCKED, "robots.txt on x disallows /", "blocked by robots.txt"),
        (TIMEOUT, "ReadTimeout", "timing out"),
        (EMPTY, "returned 0 jobs", "returning no jobs"),
        (ERROR, "HTTP 502 from x", "failing (HTTP 502)"),
        (ERROR, "KeyError: 'jobs'", "failing (error)"),
    ],
)
def test_cause(status, error, expected):
    assert health.cause(SourceResult(company=COMPANY, status=status, error=error)) == expected


def test_platform_names_a_custom_site_after_its_company():
    assert health.platform(make_company("x", ats="talnet")) == "tal.net"
    assert health.platform(make_company("google", name="Google", ats="custom")) == "Google"


def test_quiet_group_records_are_forgotten_after_a_day(settings):
    shared = SharedState(alert_groups={"Lever: timing out": NOW.isoformat()})
    health.forget_quiet_groups(shared, NOW + timedelta(hours=23), settings)
    assert shared.alert_groups
    health.forget_quiet_groups(shared, NOW + timedelta(hours=24), settings)
    assert shared.alert_groups == {}


def test_a_rate_limit_alerts_on_the_third_refusal_in_a_row_not_the_second(settings):
    source, shared = SourceState(), SharedState()
    first = one_run(source, rate_limited(), NOW, settings, shared=shared)
    second = one_run(source, rate_limited(), NOW + RUN, settings, shared=shared)
    third = one_run(source, rate_limited(), NOW + 2 * RUN, settings, shared=shared)
    fourth = one_run(source, rate_limited(), NOW + 3 * RUN, settings, shared=shared)

    assert first is None and second is None
    assert third.notification.title == "Acme (Greenhouse): rate limited (429)"
    assert fourth is None  # alerted once
    assert source.consecutive_failures == 3  # and capped, so the state stops changing


def test_a_rate_limit_that_clears_before_the_third_attempt_never_alerts(settings):
    source = SourceState()
    one_run(source, rate_limited(), NOW, settings)
    one_run(source, rate_limited(), NOW + RUN, settings)
    report = one_run(source, succeeded(), NOW + 2 * RUN, settings)

    assert report is None  # nothing was announced, so there is no recovery to announce
    assert source.consecutive_failures == 0


def test_other_failures_still_alert_on_the_second_run(settings):
    source = SourceState()
    one_run(source, rate_limited(), NOW, settings)
    report = one_run(source, failed(), NOW + RUN, settings)

    assert report.notification.title == "Acme (Greenhouse): failing (HTTP 500)"
