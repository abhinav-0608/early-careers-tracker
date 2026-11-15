"""End-to-end runs of the whole pipeline against a fake network, with one profile.

`FakeWeb` (in conftest) stands in for the three ATS APIs and for ntfy. Each test drives
one or more complete runs and checks what was sent and what was saved. What happens with
several profiles is covered in test_profiles.py.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from conftest import NOW, OWNER, changed_filters, make_company, make_profile
from jobtracker.http import HttpClient
from jobtracker.notify import Notifier
from jobtracker.pipeline import run
from jobtracker.state import CLOSED, OPEN, load_profile_state, load_sources

JOB_TOPIC = "secret-job-topic"
HEALTH_TOPIC = "secret-health-topic"
RUN = timedelta(minutes=15)

SPRING_INSIGHT_KEY = "greenhouse:point72:8845395002"


@pytest.fixture
def tracker(config, profile, settings, web, tmp_path):
    """Returns a function that performs one full run at a given time."""
    companies = (
        make_company("point72", name="Point72"),
        make_company("palantir", name="Palantir", ats="lever"),
        make_company("openai", name="OpenAI", ats="ashby"),
    )
    owner = make_profile(profile, OWNER, industries=("quant",), dream=("palantir", "openai"))
    test_config = replace(config, settings=settings, companies=companies, profiles=(owner,))

    def run_once(now=NOW, config=test_config):
        http = HttpClient(
            config.settings, transport=httpx.MockTransport(web.handle), sleep=lambda _: None
        )
        notifier = Notifier(http, config.settings.ntfy_server, {OWNER: JOB_TOPIC}, HEALTH_TOPIC)
        return run(config, tmp_path, http, notifier, now)

    run_once.state = lambda: load_profile_state(tmp_path, OWNER)
    run_once.config = test_config
    return run_once


def state_files(state_dir):
    return {
        str(path.relative_to(state_dir)): path.read_bytes()
        for path in sorted(state_dir.rglob("*"))
        if path.is_file()
    }


def test_first_run_baselines_without_individual_alerts(tracker, web, tmp_path):
    report = tracker()

    # The fixtures contain one role matching the enabled filters: the Point72 spring
    # insight programme. It is recorded silently and reported in a single summary.
    assert [message["title"] for message in web.published] == [
        "Baseline: 1 matching role currently open"
    ]
    assert web.published[0]["message"] == "Point72: 1"
    assert report.total("baselined") == 1
    assert report.total("new") == 0

    state = tracker.state()
    assert list(state.jobs) == [SPRING_INSIGHT_KEY]
    assert state.jobs[SPRING_INSIGHT_KEY].notified is True
    boards = {"greenhouse:point72", "lever:palantir", "ashby:openai"}
    assert set(state.baselines) == boards
    assert set(load_sources(tmp_path)) == boards


def test_new_job_on_a_later_run_gets_one_alert_and_is_never_repeated(tracker, web):
    tracker()
    web.published.clear()

    web.add_greenhouse_job(9000001, "Spring Week 2027 - Technology")
    tracker(NOW + RUN)

    (alert,) = web.published
    assert alert == {
        "topic": JOB_TOPIC,
        "title": "Point72 – Spring Week 2027 - Technology",
        "message": "London, UK\nSpring week · Posted 15 Nov 2026",
        "priority": 3,
        "tags": ["cherry_blossom"],
        "click": "https://boards.greenhouse.io/point72/jobs/9000001",
    }

    web.published.clear()
    tracker(NOW + 2 * RUN)
    assert web.published == []


def test_unchanged_run_leaves_state_files_untouched(tracker, tmp_path):
    tracker()
    before = state_files(tmp_path)
    assert set(before) == {"sources.json", f"profiles/{OWNER}.json"}
    tracker(NOW + RUN)
    assert state_files(tmp_path) == before


def test_more_than_five_new_jobs_are_batched_into_one_summary(tracker, web):
    tracker()
    web.published.clear()

    for number in range(6):
        web.add_greenhouse_job(9000000 + number, f"Insight Day {number}")
    report = tracker(NOW + RUN)

    (summary,) = web.published
    assert summary["title"] == "6 new roles"
    assert report.job_alerts_sent == 1
    assert all(job.notified for job in tracker.state().jobs.values())


def test_repost_under_a_new_id_is_not_alerted(tracker, web):
    tracker()
    web.published.clear()

    original = web.boards["boards-api.greenhouse.io"]["jobs"][0]
    assert original["id"] == 8845395002
    web.remove_greenhouse_job(8845395002)
    web.add_greenhouse_job(9000001, original["title"], original["location"]["name"])
    report = tracker(NOW + RUN)

    assert web.published == []
    assert report.total("reposts") == 1
    (record,) = tracker.state().jobs.values()
    assert record.key == "greenhouse:point72:9000001"
    assert record.aliases == [SPRING_INSIGHT_KEY]


def test_job_that_disappears_is_closed_after_two_runs(tracker, web):
    tracker()
    web.remove_greenhouse_job(8845395002)

    tracker(NOW + RUN)
    assert tracker.state().jobs[SPRING_INSIGHT_KEY].status == OPEN
    report = tracker(NOW + 2 * RUN)

    assert report.closed == 1
    assert tracker.state().jobs[SPRING_INSIGHT_KEY].status == CLOSED


def test_one_broken_source_does_not_stop_the_others(tracker, web):
    web.broken_hosts.add("api.lever.co")
    report = tracker()

    statuses = {result.company.key: result.status for result in report.results}
    assert statuses == {"point72": "ok", "palantir": "error", "openai": "ok"}
    assert report.total("baselined") == 1  # Point72 was still processed
    assert web.sent_to(HEALTH_TOPIC) == []  # a single failure is not yet alert-worthy

    web.published.clear()
    tracker(NOW + RUN)

    (alert,) = web.sent_to(HEALTH_TOPIC)
    assert alert["title"] == "Source failing: Palantir"
    assert "HTTP 500 from api.lever.co" in alert["message"]
    assert web.sent_to(JOB_TOPIC) == []  # failures never reach the job topic

    web.published.clear()
    tracker(NOW + 2 * RUN)
    assert web.published == []  # no repeat while it stays broken


def test_broken_source_recovers_and_is_baselined_then(tracker, web):
    web.broken_hosts.add("api.lever.co")
    tracker()
    tracker(NOW + RUN)
    web.published.clear()

    web.broken_hosts.clear()
    tracker(NOW + 2 * RUN)

    assert [message["title"] for message in web.sent_to(HEALTH_TOPIC)] == ["Sources recovered"]
    # Palantir's first successful fetch is its baseline, not a flood of alerts.
    assert [message["title"] for message in web.sent_to(JOB_TOPIC)] == [
        "Baseline: 0 matching roles currently open"
    ]


def test_board_that_goes_empty_raises_a_health_alert_and_closes_nothing(tracker, web):
    tracker()
    web.published.clear()
    web.boards["boards-api.greenhouse.io"] = {"jobs": [], "meta": {"total": 0}}

    for step in (1, 2, 3):
        report = tracker(NOW + step * RUN)

    assert report.results[0].status == "empty"
    assert tracker.state().jobs[SPRING_INSIGHT_KEY].status == OPEN
    (alert,) = web.sent_to(HEALTH_TOPIC)
    assert "returned 0 jobs after previously returning some" in alert["message"]


def test_alert_that_fails_to_send_is_retried_on_the_next_run(tracker, web):
    tracker()
    web.published.clear()
    web.add_greenhouse_job(9000001, "Spring Week 2027")

    web.ntfy_down = True
    report = tracker(NOW + RUN)
    assert report.job_alerts_failed == 1
    assert tracker.state().jobs["greenhouse:point72:9000001"].notified is False

    web.ntfy_down = False
    tracker(NOW + 2 * RUN)
    assert [message["title"] for message in web.published] == ["Point72 – Spring Week 2027"]
    assert tracker.state().jobs["greenhouse:point72:9000001"].notified is True


def test_enabling_a_role_type_baselines_the_newly_matching_jobs(tracker, web):
    tracker()
    web.published.clear()

    # Switch on internships, as editing filters.yaml would: the rules and their hash change.
    (owner,) = tracker.config.profiles
    owner = replace(owner, filters=changed_filters(owner.filters))
    config = replace(tracker.config, profiles=(owner,))
    report = tracker(NOW + RUN, config=config)

    # Newly matching: Point72 "Quantitative Researcher - Intern" (London) and Palantir
    # "Software Engineer, Internship" (London). One summary, no individual alerts.
    assert report.total("baselined") == 2
    assert [message["title"] for message in web.published] == [
        "Baseline: 2 roles newly match your filters"
    ]

    web.published.clear()
    web.add_greenhouse_job(9000001, "Software Engineer Intern")
    tracker(NOW + 2 * RUN, config=config)
    assert [message["title"] for message in web.published] == ["Point72 – Software Engineer Intern"]


def test_secret_topics_never_appear_in_logs(tracker, web, caplog):
    caplog.set_level("INFO")
    web.broken_hosts.add("api.lever.co")
    tracker()
    web.add_greenhouse_job(9000001, "Spring Week 2027")
    web.ntfy_down = True
    tracker(NOW + RUN)
    web.ntfy_down = False
    tracker(NOW + 2 * RUN)

    assert "run_summary" in caplog.text
    assert JOB_TOPIC not in caplog.text
    assert HEALTH_TOPIC not in caplog.text


def test_actions_run_page_gets_a_profile_table_and_a_source_table(tracker, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    tracker()

    lines = summary.read_text().splitlines()
    assert f"| {OWNER} | ok | 3 | 1 | 0 | 1 | 0 | 0 |" in lines
    assert any(line.startswith("| Point72 | greenhouse | ok | 3 |") for line in lines)
    assert any(line.endswith(f"| {OWNER} 1 |") for line in lines)  # who matched what


# --- moving from the single-user state files to a profile ------------------------------


def write_legacy_state(state_dir, filters_hash: str, notified: bool = True):
    """The version 1 files as the single-user tracker left them."""
    job = {
        "aliases": [],
        "closed_at": None,
        "company": "point72",
        "fields": [],
        "fingerprint": "point72|2027 point72 academy spring insight programme uk|london uk",
        "first_seen": "2026-11-15T01:46:12+00:00",
        "last_seen": "2026-11-15",
        "locations": ["London, UK"],
        "missing_runs": 0,
        "notified": notified,
        "posted_at": "2026-09-29",
        "region": "uk",
        "role_type": "spring_week",
        "source": "greenhouse",
        "status": "open",
        "title": "2027 Point72 Academy Spring Insight Programme – UK",
        "url": "https://boards.greenhouse.io/point72/jobs/8845395002?gh_jid=8845395002",
    }
    source = {
        "alerted": False,
        "consecutive_failures": 0,
        "empty_runs": 0,
        "failing_since": None,
        "filters_hash": filters_hash,
        "has_returned_jobs": True,
        "last_alert_at": None,
    }
    sources = {key: source for key in ("greenhouse:point72", "lever:palantir", "ashby:openai")}
    jobs = {"version": 1, "jobs": {SPRING_INSIGHT_KEY: job}}
    (state_dir / "jobs.json").write_text(json.dumps(jobs))
    (state_dir / "sources.json").write_text(json.dumps({"version": 1, "sources": sources}))


def test_first_run_on_the_old_state_files_sends_nothing_and_migrates_them(tracker, web, tmp_path):
    write_legacy_state(tmp_path, filters_hash=tracker.config.profiles[0].filters.hash)

    report = tracker()

    # Nothing is re-announced and nothing is baselined again: the owner carries on
    # exactly where the single-user tracker stopped.
    assert web.published == []
    assert report.total("new") == report.total("baselined") == 0
    assert set(state_files(tmp_path)) == {"sources.json", f"profiles/{OWNER}.json"}
    assert "filters_hash" not in (tmp_path / "sources.json").read_text()
    assert tracker.state().jobs[SPRING_INSIGHT_KEY].notified is True

    # ...and from then on behaves like any other run.
    web.add_greenhouse_job(9000001, "Spring Week 2027")
    tracker(NOW + RUN)
    assert [message["title"] for message in web.published] == ["Point72 – Spring Week 2027"]


def test_an_alert_still_owed_under_the_old_files_is_sent_after_migrating(tracker, web, tmp_path):
    hash_ = tracker.config.profiles[0].filters.hash
    write_legacy_state(tmp_path, filters_hash=hash_, notified=False)
    tracker()
    assert [message["title"] for message in web.published] == [
        "Point72 – 2027 Point72 Academy Spring Insight Programme – UK"
    ]
