import json
from dataclasses import replace

import httpx
import pytest

from conftest import make_company, make_profile
from jobtracker.http import HttpClient
from jobtracker.notify import (
    Notification,
    Notifier,
    baseline_notification,
    job_notification,
    plan_job_notifications,
)
from jobtracker.state import StoredJob

JOB_TOPIC = "secret-job-topic"
HEALTH_TOPIC = "secret-health-topic"


def stored_job(number: int, company: str = "normalco", **overrides) -> StoredJob:
    values = {
        "key": f"greenhouse:{company}:{number}",
        "source": "greenhouse",
        "company": company,
        "title": f"Spring Week {number}",
        "locations": ["London, UK"],
        "url": f"https://example.com/{company}/{number}",
        "role_type": "spring_week",
        "fields": ["software"],
        "region": "uk",
        "fingerprint": f"{company}|spring week {number}|london uk",
        "first_seen": "2026-11-15T12:00:00+00:00",
        "last_seen": "2026-11-15",
        "posted_at": "2026-11-14",
    }
    return StoredJob(**{**values, **overrides})


def with_companies(config):
    companies = (
        make_company("normalco", name="Normal Co"),
        make_company("dreamco", name="Dream Co"),
    )
    return replace(config, companies=companies)


@pytest.fixture
def me(profile):
    """A profile watching both test companies, for whom Dream Co is dream tier."""
    return make_profile(profile, "me", industries=("quant",), dream=("dreamco",))


# --- what gets sent --------------------------------------------------------------------


def test_job_notification_has_the_requested_shape(config, me):
    notification = job_notification(stored_job(1), me, with_companies(config))

    assert notification.title == "Normal Co – Spring Week 1"
    assert notification.message == "London, UK\nSpring week · Posted 14 Nov 2026"
    assert notification.click == "https://example.com/normalco/1"
    assert notification.priority == 3
    assert notification.tags == ("cherry_blossom",)
    assert notification.job_keys == ("greenhouse:normalco:1",)


def test_dream_tier_is_high_priority_and_starred(config, me):
    notification = job_notification(stored_job(1, company="dreamco"), me, with_companies(config))
    assert notification.priority == 5
    assert notification.tags == ("cherry_blossom", "star")


def test_message_falls_back_to_first_seen_and_flags_unclear_locations(config, me):
    job = stored_job(1, posted_at=None, region="unclear", locations=["Multiple Locations"])
    notification = job_notification(job, me, with_companies(config))
    assert notification.message == (
        "Multiple Locations (unclear)\nSpring week · First seen 15 Nov 2026"
    )


def test_dream_tier_belongs_to_the_profile_not_the_company(config, me, profile):
    # The same posting, for someone who has not put Dream Co on their dream list.
    someone_else = make_profile(profile, "someone-else", industries=("quant",))
    job = stored_job(1, company="dreamco")

    mine = job_notification(job, me, with_companies(config))
    theirs = job_notification(job, someone_else, with_companies(config))

    assert (mine.priority, mine.tags) == (5, ("cherry_blossom", "star"))
    assert (theirs.priority, theirs.tags) == (3, ("cherry_blossom",))


def test_long_location_lists_are_shortened(config, me):
    job = stored_job(1, locations=["London", "Leeds", "Bristol", "Glasgow", "Cardiff"])
    notification = job_notification(job, me, with_companies(config))
    assert notification.message.startswith("London; Leeds; Bristol +2 more\n")


# --- batching --------------------------------------------------------------------------


def test_five_new_jobs_are_sent_individually(config, me):
    pending = [stored_job(number) for number in range(5)]
    notifications = plan_job_notifications(pending, me, with_companies(config))

    assert len(notifications) == 5
    assert all(len(notification.job_keys) == 1 for notification in notifications)


def test_six_new_jobs_become_one_summary(config, me):
    config = with_companies(config)
    pending = [stored_job(number) for number in range(6)]
    (summary,) = plan_job_notifications(pending, me, config)

    assert summary.title == "6 new roles"
    assert summary.click == config.settings.repo_url
    assert len(summary.job_keys) == 6
    assert "Normal Co – Spring Week 0 (London, UK)" in summary.message


def test_dream_jobs_get_their_own_alert_even_within_a_batch(config, me):
    pending = [stored_job(number) for number in range(6)]
    pending += [stored_job(number, company="dreamco") for number in (100, 101)]
    notifications = plan_job_notifications(pending, me, with_companies(config))

    individual = [n for n in notifications if len(n.job_keys) == 1]
    summaries = [n for n in notifications if len(n.job_keys) > 1]
    assert [n.title for n in individual] == [
        "Dream Co – Spring Week 100",
        "Dream Co – Spring Week 101",
    ]
    assert all(n.priority == 5 for n in individual)
    assert len(summaries) == 1
    assert summaries[0].title == "6 new roles"
    # Every pending job is covered exactly once.
    covered = [key for n in notifications for key in n.job_keys]
    assert sorted(covered) == sorted(job.key for job in pending)


def test_summary_lists_at_most_ten_jobs(config, me):
    pending = [stored_job(number) for number in range(14)]
    (summary,) = plan_job_notifications(pending, me, with_companies(config))
    assert summary.message.count("\n") == 10
    assert summary.message.endswith("...and 4 more")


def test_baseline_summary_counts_roles_per_company(config):
    jobs = [stored_job(1), stored_job(2), stored_job(3, company="dreamco")]
    notification = baseline_notification(jobs, with_companies(config), filters_changed=False)

    assert notification.title == "Baseline: 3 matching roles currently open"
    assert notification.message == "Dream Co: 1\nNormal Co: 2"
    assert notification.job_keys == ()


# --- sending ---------------------------------------------------------------------------


OTHER_TOPIC = "secret-topic-of-someone-else"


def make_notifier(settings, handler, **kwargs):
    http = HttpClient(settings, transport=httpx.MockTransport(handler))
    topics = {"me": JOB_TOPIC, "other": OTHER_TOPIC, "no-topic": None}
    return Notifier(http, settings.ntfy_server, topics, HEALTH_TOPIC, **kwargs)


NOTIFICATION = Notification(
    title="Acme – Spring Week", message="London", priority=5, tags=("star",), click="https://x/1"
)


def test_publishes_json_with_the_topic_in_the_body_not_the_url(settings):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    assert make_notifier(settings, handler).send_job("me", NOTIFICATION) is True

    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == "https://ntfy.sh"
    assert JOB_TOPIC not in str(request.url)
    assert json.loads(request.content) == {
        "topic": JOB_TOPIC,
        "title": "Acme – Spring Week",
        "message": "London",
        "priority": 5,
        "tags": ["star"],
        "click": "https://x/1",
    }


def test_health_alerts_go_to_the_health_topic_only(settings):
    topics = []

    def handler(request):
        topics.append(json.loads(request.content)["topic"])
        return httpx.Response(200, json={})

    make_notifier(settings, handler).send_health(NOTIFICATION)
    assert topics == [HEALTH_TOPIC]


def test_each_profiles_alerts_go_to_its_own_topic_only(settings):
    topics = []

    def handler(request):
        topics.append(json.loads(request.content)["topic"])
        return httpx.Response(200, json={})

    notifier = make_notifier(settings, handler)
    notifier.send_job("me", NOTIFICATION)
    notifier.send_job("other", NOTIFICATION)
    assert topics == [JOB_TOPIC, OTHER_TOPIC]


def test_profile_without_a_topic_cannot_be_reached_and_nothing_is_sent(settings):
    def handler(request):
        raise AssertionError("nothing may be sent for a profile with no topic")

    notifier = make_notifier(settings, handler)
    assert notifier.can_reach("me") is True
    assert notifier.can_reach("no-topic") is False
    assert notifier.can_reach("never-heard-of") is False
    assert notifier.send_job("no-topic", NOTIFICATION) is False
    assert notifier.send_job("never-heard-of", NOTIFICATION) is False


def test_failed_send_returns_false_and_never_logs_the_topic(settings, caplog):
    caplog.set_level("INFO")
    notifier = make_notifier(settings, lambda request: httpx.Response(500))

    assert notifier.send_job("me", NOTIFICATION) is False
    assert "notification_failed" in caplog.text
    assert JOB_TOPIC not in caplog.text


def test_network_failure_returns_false(settings):
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    assert make_notifier(settings, handler).send_job("me", NOTIFICATION) is False


def test_dry_run_sends_nothing(settings):
    def handler(request):
        raise AssertionError("dry run must not make requests")

    assert make_notifier(settings, handler, dry_run=True).send_job("me", NOTIFICATION) is True
