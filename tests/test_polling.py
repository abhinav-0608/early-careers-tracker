"""Polling intervals: sources that are polled less often than every run.

Two rules are tested from every side, because breaking either would be silent:

  - a source that is not due is skipped without a request, and that is not a failure;
  - what a skipped or partial poll did not see is never taken as "gone": nothing closes.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from conftest import NOW, OWNER, FakeWeb, changed_filters, make_company, make_profile
from jobtracker import pauses
from jobtracker.config import Pause
from jobtracker.http import HttpClient
from jobtracker.notify import Notifier
from jobtracker.pipeline import interval_elapsed, run
from jobtracker.sources import workday
from jobtracker.state import (
    CLOSED,
    OPEN,
    SourceState,
    load_hosts,
    load_profile_state,
    load_sources,
    save_hosts,
)

JOB_TOPIC = "secret-job-topic"
HEALTH_TOPIC = "secret-health-topic"
RUN = timedelta(minutes=15)
SPRING_INSIGHT = "greenhouse:point72:8845395002"  # the fixture board's matching role


POSTING_PAGE = (
    '<html><script type="application/ld+json">'
    '{"@type": "JobPosting", "description": "<p>Join our team for the summer.</p>"}'
    "</script><body>Apply now</body></html>"
)


class WorkdayWeb(FakeWeb):
    """FakeWeb plus one Workday tenant with a small campus site and a large main site.

    The large site reports 2000 postings. Its first page is `newest`; a search returns
    the postings in `searchable` whose title contains the term.
    """

    def __init__(self):
        super().__init__()
        self.campus: list[str] = ["Spring Week 2027"]
        self.newest: list[str] = ["Staff Engineer"]
        self.searchable: list[str] = ["Technology Insight Week 2027"]
        self.calls: list[tuple[str, str]] = []  # (site, search text)
        self.limits: list[int] = []  # the page size each call asked for

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("myworkdayjobs.com") and request.method == "GET":
            if request.url.path != "/robots.txt":
                # A posting's own page, read by the eligibility check. It says nothing
                # about years of study.
                return httpx.Response(200, text=POSTING_PAGE)
        if request.url.host in self.rate_limited:
            return super().handle(request)  # challenged: 429 for everything
        if not request.url.host.endswith("myworkdayjobs.com") or request.method != "POST":
            return super().handle(request)
        site = request.url.path.split("/")[-2]
        body = json.loads(request.content)
        search = body["searchText"]
        self.calls.append((site, search))
        self.limits.append(body["limit"])
        if site == "Campus":
            return httpx.Response(200, json=self._page(self.campus, len(self.campus)))
        if not search:
            return httpx.Response(200, json=self._page(self.newest, 2000))
        found = [title for title in self.searchable if search in title.lower()]
        return httpx.Response(200, json=self._page(found, len(found)))

    @staticmethod
    def _page(titles: list[str], total: int) -> dict:
        postings = [
            {
                "title": title,
                "externalPath": f"/job/London/{title.replace(' ', '-')}_{title.replace(' ', '')}",
                "locationsText": "London",
            }
            for title in titles
        ]
        return {"total": total, "jobPostings": postings}

    def searches(self) -> list[str]:
        return [search for _, search in self.calls if search]


@pytest.fixture
def tracker(config, profile, settings, tmp_path):
    """Returns make(web, companies, intervals) -> a function performing one run."""

    def make(web, companies, intervals):
        owner = make_profile(profile, OWNER, industries=("quant",))
        test_settings = replace(settings, poll_every_minutes=intervals)
        test_config = replace(
            config, settings=test_settings, companies=companies, profiles=(owner,)
        )

        def run_once(now=NOW, config=test_config):
            http = HttpClient(
                config.settings, transport=httpx.MockTransport(web.handle), sleep=lambda _: None
            )
            notifier = Notifier(http, config.settings.ntfy_server, {OWNER: JOB_TOPIC}, HEALTH_TOPIC)
            return run(config, tmp_path, http, notifier, now)

        run_once.state = lambda: load_profile_state(tmp_path, OWNER)
        run_once.sources = lambda: load_sources(tmp_path)
        run_once.config = test_config
        return run_once

    return make


def statuses(report) -> dict[str, str]:
    return {result.company.key: result.status for result in report.results}


# --- a whole source on an interval (Google, in the shipped settings) ---------------------

HOURLY = (make_company("point72", name="Point72"), make_company("openai", ats="ashby"))


def test_a_source_with_an_interval_is_polled_once_per_interval(tracker):
    web = FakeWeb()
    run_once = tracker(web, HOURLY, {"greenhouse": 60})

    polled = []
    for quarter in range(9):  # two hours and a bit, one run every 15 minutes
        web.fetches.clear()
        report = run_once(NOW + quarter * RUN)
        polled.append(statuses(report)["point72"])
        assert statuses(report)["openai"] == "ok"  # no interval: every run
        requests_to_greenhouse = [f for f in web.fetches if f.startswith("boards-api")]
        assert len(requests_to_greenhouse) == (1 if polled[-1] == "ok" else 0)

    assert polled == ["ok", "not_due", "not_due", "not_due"] * 2 + ["ok"]


def test_a_skipped_source_is_not_a_failure(tracker):
    web = FakeWeb()
    run_once = tracker(web, HOURLY, {"greenhouse": 60})
    run_once()
    web.published.clear()

    for quarter in (1, 2, 3):
        report = run_once(NOW + quarter * RUN)
        assert statuses(report)["point72"] == "not_due"
        assert not report.health_alert_sent
    assert web.sent_to(HEALTH_TOPIC) == []
    source = run_once.sources()["greenhouse:point72"]
    assert (source.consecutive_failures, source.failing_since, source.alerted) == (0, None, False)


def test_a_skipped_source_closes_nothing_even_if_the_job_has_gone(tracker):
    """Not looking is not the same as not finding."""
    web = FakeWeb()
    run_once = tracker(web, HOURLY, {"greenhouse": 60})
    run_once()
    assert run_once.state().jobs[SPRING_INSIGHT].status == OPEN

    web.remove_greenhouse_job(8845395002)
    for quarter in (1, 2, 3):  # three runs that do not look at the board
        run_once(NOW + quarter * RUN)
        job = run_once.state().jobs[SPRING_INSIGHT]
        assert (job.status, job.missing_runs) == (OPEN, 0)

    run_once(NOW + 4 * RUN)  # due again: missing once
    assert run_once.state().jobs[SPRING_INSIGHT].missing_runs == 1
    run_once(NOW + 8 * RUN)  # and missing on the next poll too: now it closes
    assert run_once.state().jobs[SPRING_INSIGHT].status == CLOSED


def test_a_skipped_source_does_not_recover_or_extend_a_failure_streak(tracker):
    """A source that fails is retried every run: only a successful poll starts the clock."""
    web = FakeWeb()
    run_once = tracker(web, HOURLY, {"greenhouse": 60})
    web.broken_hosts.add("boards-api.greenhouse.io")

    assert statuses(run_once())["point72"] == "error"
    assert statuses(run_once(NOW + RUN))["point72"] == "error"  # retried, not skipped
    assert len(web.sent_to(HEALTH_TOPIC)) == 1  # second failure in a row: alert

    web.broken_hosts.clear()
    assert statuses(run_once(NOW + 2 * RUN))["point72"] == "ok"
    assert statuses(run_once(NOW + 3 * RUN))["point72"] == "not_due"
    titles = [message["title"] for message in web.sent_to(HEALTH_TOPIC)]
    assert titles == ["Point72 (Greenhouse): failing (HTTP 500)", "Point72 (Greenhouse) recovered"]


def test_a_run_a_few_seconds_early_still_counts_as_the_hour(tracker):
    web = FakeWeb()
    run_once = tracker(web, HOURLY, {"greenhouse": 60})
    run_once(NOW + timedelta(seconds=40))
    report = run_once(NOW + timedelta(minutes=60, seconds=5))  # 59 min 25 s later
    assert statuses(report)["point72"] == "ok"


def test_only_sources_with_an_interval_add_a_timestamp_to_the_state_file(tracker, tmp_path):
    """Every-run sources must not make sources.json change on every run."""
    web = FakeWeb()
    run_once = tracker(web, HOURLY, {"greenhouse": 60, "ashby": 0})
    run_once()

    saved = json.loads((tmp_path / "sources.json").read_text())["sources"]
    assert saved["greenhouse:point72"]["interval_polled_at"] == "2026-11-15T12:00:00+00:00"
    assert "interval_polled_at" not in saved["ashby:openai"]

    before = (tmp_path / "sources.json").read_bytes()
    run_once(NOW + RUN)
    assert (tmp_path / "sources.json").read_bytes() == before


def test_a_source_never_polled_is_due_straight_away(tracker):
    assert interval_elapsed(None, 60, NOW)
    assert interval_elapsed(SourceState(), 60, NOW)
    polled = SourceState(interval_polled_at=NOW.isoformat())
    assert not interval_elapsed(polled, 60, NOW + timedelta(minutes=45))
    assert interval_elapsed(polled, 60, NOW + timedelta(minutes=60))
    assert interval_elapsed(polled, 0, NOW)  # no interval: always due


# --- Workday: small sites every run, large-site searches on an interval -------------------

TENANT = (
    make_company(
        "acme", name="Acme", ats="workday", tenant="acme", host="wd3", sites=("Campus", "Main")
    ),
)
INSIGHT = "workday:acme:TechnologyInsightWeek2027"
SPRING = "workday:acme:SpringWeek2027"


def test_workday_searches_run_once_per_interval_and_small_sites_every_run(tracker):
    web = WorkdayWeb()
    run_once = tracker(web, TENANT, {"workday_search": 60})

    searched = []
    for quarter in range(5):
        web.calls.clear()
        report = run_once(NOW + quarter * RUN)
        (result,) = report.results
        assert result.status == "ok"  # the tenant is polled every run
        assert ("Campus", "") in web.calls and ("Main", "") in web.calls
        searched.append((bool(web.searches()), result.partial))

    full, between = (True, False), (False, True)
    assert searched == [full, between, between, between, full]
    assert web.searches() == list(workday.SEARCH_TERMS)


def test_a_partial_workday_poll_closes_nothing_and_still_alerts_what_it_finds(tracker):
    web = WorkdayWeb()
    run_once = tracker(web, TENANT, {"workday_search": 60})
    run_once()  # complete: baselines the spring week and the insight week
    assert {key for key, job in run_once.state().jobs.items() if job.status == OPEN} == {
        SPRING,
        INSIGHT,
    }
    web.published.clear()

    # Between searches the insight week (found only by search) is simply not looked for,
    # while a new role on the campus site is alerted in the same run.
    web.campus.append("First Year Insight Day")
    for quarter in (1, 2, 3):
        run_once(NOW + quarter * RUN)
        insight = run_once.state().jobs[INSIGHT]
        assert (insight.status, insight.missing_runs) == (OPEN, 0)
    assert [message["title"] for message in web.sent_to(JOB_TOPIC)] == [
        "Acme – First Year Insight Day"
    ]
    assert web.sent_to(HEALTH_TOPIC) == []


def test_a_job_missing_from_two_complete_workday_polls_closes(tracker):
    web = WorkdayWeb()
    run_once = tracker(web, TENANT, {"workday_search": 60})
    run_once()
    web.searchable.clear()  # the insight week is taken down
    web.campus.clear()  # and so is the spring week, on the site read every run

    run_once(NOW + RUN)  # partial: neither can be shown to have gone
    assert run_once.state().jobs[SPRING].missing_runs == 0
    run_once(NOW + 4 * RUN)  # complete
    assert run_once.state().jobs[INSIGHT].missing_runs == 1
    run_once(NOW + 8 * RUN)  # complete
    assert run_once.state().jobs[INSIGHT].status == CLOSED
    assert run_once.state().jobs[SPRING].status == CLOSED


def test_a_baseline_waits_for_a_complete_workday_poll(tracker, filters):
    """A filter change during a partial poll must not baseline half a board.

    If it did, the roles the searches find an hour later would each be alerted.
    """
    web = WorkdayWeb()
    web.campus.append("Software Engineering Summer Internship 2027")
    web.searchable.append("Quant Trading Summer Internship 2027")
    run_once = tracker(web, TENANT, {"workday_search": 60})
    run_once()
    web.published.clear()

    owner = run_once.config.profiles[0]
    with_internships = replace(
        run_once.config, profiles=(replace(owner, filters=changed_filters(filters)),)
    )
    report = run_once(NOW + RUN, config=with_internships)  # partial
    assert report.total("baselined") == 0
    assert web.sent_to(JOB_TOPIC) == []

    report = run_once(NOW + 4 * RUN, config=with_internships)  # complete
    assert report.total("baselined") == 2 and report.total("new") == 0
    (summary,) = web.sent_to(JOB_TOPIC)  # one summary, no individual alerts
    assert summary["title"] == "Baseline: 2 roles newly match your filters"


def test_with_no_interval_workday_is_searched_every_run_and_keeps_no_timestamp(tracker, tmp_path):
    web = WorkdayWeb()
    run_once = tracker(web, TENANT, {})
    for quarter in range(2):
        web.calls.clear()
        (result,) = run_once(NOW + quarter * RUN).results
        assert web.searches() == list(workday.SEARCH_TERMS) and not result.partial
    saved = json.loads((tmp_path / "sources.json").read_text())["sources"]
    assert "interval_polled_at" not in saved["workday:acme"]


# --- a paused kind of source (settings.yaml, pauses) ------------------------------------

ACME_HOST = "acme.wd3.myworkdayjobs.com"
CHALLENGE = {"cf-mitigated": "challenge"}
PAUSE = Pause(kind="workday", since=NOW, reason="Cloudflare challenge", probe_company="acme")
HOUR = timedelta(hours=1)


@pytest.fixture
def paused(config, profile, settings, tmp_path):
    """A Workday tenant paused at NOW, in a world that keeps hosts.json between runs."""
    web = WorkdayWeb()
    owner = make_profile(profile, OWNER, industries=("quant",))
    plain = replace(
        config,
        settings=replace(settings, poll_every_minutes={"workday_search": 60}),
        companies=TENANT,
        profiles=(owner,),
    )
    with_pause = replace(plain, settings=replace(plain.settings, pauses={"workday": PAUSE}))

    def run_once(now, config=with_pause):
        http = HttpClient(
            config.settings,
            transport=httpx.MockTransport(web.handle),
            sleep=lambda _: None,
            hosts=load_hosts(tmp_path),
            now=now,
        )
        notifier = Notifier(http, config.settings.ntfy_server, {OWNER: JOB_TOPIC}, HEALTH_TOPIC)
        try:
            return run(config, tmp_path, http, notifier, now)
        finally:
            save_hosts(http.hosts_state(), tmp_path)

    # Before the pause: one ordinary run, which caches the tenant's robots.txt.
    run_once(NOW - RUN, config=plain)
    web.published.clear()
    web.calls.clear()
    web.limits.clear()
    web.fetches.clear()
    web.robots_requests.clear()
    run_once.web = web
    run_once.state_dir = tmp_path
    return run_once


def acme_requests(web):
    return (
        [f for f in web.fetches if f.startswith(ACME_HOST)]
        + [call for call in web.calls]
        + [host for host in web.robots_requests if host == ACME_HOST]
    )


def test_a_paused_source_is_not_asked_anything_for_48_hours(paused):
    web = paused.web
    web.rate_limited[ACME_HOST] = CHALLENGE
    for hours in (0, 1, 24, 47):
        report = paused(NOW + hours * HOUR)
        assert statuses(report) == {"acme": "paused"}
    assert acme_requests(web) == []
    assert web.published == []  # no health alert: a pause is not a failure
    assert not (paused.state_dir / "sources.json").read_text().count('"probed_at"')


def test_after_48_hours_one_request_every_6_hours_with_the_cached_robots_txt(paused):
    web = paused.web
    web.rate_limited[ACME_HOST] = CHALLENGE
    paused(NOW + 48 * HOUR)
    # Exactly one request, and robots.txt (cached two days ago) is not read again.
    assert web.fetches == [f"{ACME_HOST}/wday/cxs/acme/Campus/jobs"]
    assert ACME_HOST not in web.robots_requests
    assert web.published == []
    for hours in (48.25, 50, 53.5):
        assert statuses(paused(NOW + hours * HOUR)) == {"acme": "paused"}
    assert len(web.fetches) == 1  # nothing between probes
    paused(NOW + 54 * HOUR)
    assert len(web.fetches) == 2
    assert ACME_HOST not in web.robots_requests  # the 429s kept the cached copy


def test_a_probe_that_succeeds_resumes_polling_every_3_hours_with_small_searches(paused):
    web = paused.web
    report = paused(NOW + 48 * HOUR)
    # The probe (one posting) and then, in the same run, the tenant itself.
    assert web.limits[0] == 1 and web.calls[0] == ("Campus", "")
    assert statuses(report) == {"acme": "ok"}
    assert web.searches() == list(workday.SEARCH_TERMS)
    (announcement,) = web.sent_to(HEALTH_TOPIC)
    assert announcement["title"] == "Workday answers again"
    assert "at most every 3 h" in announcement["message"]

    web.calls.clear()
    for minutes in (15, 60, 165):
        assert statuses(paused(NOW + 48 * HOUR + minutes * RUN / 15)) == {"acme": "not_due"}
    assert web.calls == []
    assert statuses(paused(NOW + 51 * HOUR)) == {"acme": "ok"}
    assert len(web.sent_to(HEALTH_TOPIC)) == 1  # announced once


def test_still_refused_after_7_days_probing_stops_and_one_alert_asks_to_mark_it_unsupported(
    paused,
):
    web = paused.web
    web.rate_limited[ACME_HOST] = CHALLENGE
    for hours in range(48, 7 * 24, 6):
        paused(NOW + hours * HOUR)
    probes = len(web.fetches)
    assert probes == (7 * 24 - 48) // 6
    assert web.published == []

    report = paused(NOW + 7 * 24 * HOUR)
    assert statuses(report) == {"acme": "paused"}
    (alert,) = web.sent_to(HEALTH_TOPIC)
    assert alert["title"] == "Workday still refused after 7 days"
    assert "Mark the Workday companies unsupported in companies.yaml" in alert["message"]
    for days in (7.5, 8, 10):
        paused(NOW + days * 24 * HOUR)
    assert len(web.fetches) == probes  # no more probes
    assert len(web.sent_to(HEALTH_TOPIC)) == 1


def test_a_paused_source_closes_nothing(paused):
    web = paused.web
    stored = load_profile_state(paused.state_dir, OWNER).jobs
    assert SPRING in stored and stored[SPRING].status == OPEN
    web.campus.clear()  # the role has really gone, but nobody is looking
    web.rate_limited[ACME_HOST] = CHALLENGE
    for hours in range(0, 48, 6):
        paused(NOW + hours * HOUR)
    assert load_profile_state(paused.state_dir, OWNER).jobs[SPRING].status == OPEN


def test_stage_and_announcement_rules():
    memory: dict = {}
    assert pauses.stage(PAUSE, memory, NOW - HOUR) == pauses.PAUSED  # before it began
    assert pauses.stage(PAUSE, memory, NOW + 47 * HOUR) == pauses.PAUSED
    assert pauses.stage(PAUSE, memory, NOW + 48 * HOUR - timedelta(minutes=1)) == pauses.PROBE
    memory["probed_at"] = (NOW + 48 * HOUR).isoformat()
    assert pauses.stage(PAUSE, memory, NOW + 50 * HOUR) == pauses.PAUSED
    assert pauses.stage(PAUSE, memory, NOW + 54 * HOUR) == pauses.PROBE
    assert pauses.announcement(PAUSE, pauses.PAUSED, memory) is None
    assert pauses.stage(PAUSE, memory, NOW + 7 * 24 * HOUR) == pauses.GIVEN_UP
    assert pauses.announcement(PAUSE, pauses.GIVEN_UP, memory) is not None
    memory["announced"] = pauses.GIVEN_UP
    assert pauses.announcement(PAUSE, pauses.GIVEN_UP, memory) is None
