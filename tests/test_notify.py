import json
from dataclasses import replace

import httpx
import pytest

from conftest import NOW, make_company, make_profile
from jobtracker.http import HttpClient
from jobtracker.notify import (
    MAX_MESSAGE_BYTES,
    Action,
    Notification,
    Notifier,
    baseline_notification,
    fit_lines,
    health_test_notification,
    job_notification,
    plan_job_notifications,
    role_line,
    summary_notification,
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
        make_company("fireco", name="Fire Co"),
    )
    return replace(config, companies=companies)


@pytest.fixture
def me(profile):
    """A profile watching both test companies, for whom Fire Co is FAANG+."""
    return make_profile(profile, "me", industries=("quant",), faang_plus=("fireco",))


# --- what gets sent --------------------------------------------------------------------


def test_job_notification_has_the_requested_shape(config, me):
    notification = job_notification(stored_job(1), me, with_companies(config))

    assert notification.title == "Normal Co – Spring Week 1"
    assert notification.message == "London, UK\nSpring week · Posted 14 Nov 2026"
    assert notification.click == "https://example.com/normalco/1"
    assert notification.priority == 3
    assert notification.tags == ("cherry_blossom",)
    assert notification.job_keys == ("greenhouse:normalco:1",)


def test_faang_plus_is_high_priority_and_marked_with_fire(config, me):
    notification = job_notification(stored_job(1, company="fireco"), me, with_companies(config))
    assert notification.priority == 5
    assert notification.tags == ("cherry_blossom", "fire")


def test_message_falls_back_to_first_seen_and_flags_unclear_locations(config, me):
    job = stored_job(1, posted_at=None, region="unclear", locations=["Multiple Locations"])
    notification = job_notification(job, me, with_companies(config))
    assert notification.message == (
        "Multiple Locations (unclear)\nSpring week · First seen 15 Nov 2026"
    )


def test_faang_plus_belongs_to_the_profile_not_the_company(config, me, profile):
    # The same posting, for someone who has not put Fire Co on their FAANG+ list.
    someone_else = make_profile(profile, "someone-else", industries=("quant",))
    job = stored_job(1, company="fireco")

    mine = job_notification(job, me, with_companies(config))
    theirs = job_notification(job, someone_else, with_companies(config))

    assert (mine.priority, mine.tags) == (5, ("cherry_blossom", "fire"))
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
    assert summary.click == config.settings.repo_url + "#open-roles"
    assert len(summary.job_keys) == 6
    assert "Normal Co — Spring Week 0 · London, UK" in summary.message.splitlines()


def test_faang_plus_jobs_get_their_own_alert_even_within_a_batch(config, me):
    pending = [stored_job(number) for number in range(6)]
    pending += [stored_job(number, company="fireco") for number in (100, 101)]
    notifications = plan_job_notifications(pending, me, with_companies(config))

    individual = [n for n in notifications if len(n.job_keys) == 1]
    summaries = [n for n in notifications if len(n.job_keys) > 1]
    assert [n.title for n in individual] == [
        "🔥 Fire Co – Spring Week 100",
        "🔥 Fire Co – Spring Week 101",
    ]
    assert all(n.priority == 5 for n in individual)
    assert len(summaries) == 1
    assert summaries[0].title == "6 new roles"
    # Every pending job is covered exactly once.
    covered = [key for n in notifications for key in n.job_keys]
    assert sorted(covered) == sorted(job.key for job in pending)


def test_summary_lists_every_job_while_it_fits(config, me):
    pending = [stored_job(number) for number in range(14)]
    (summary,) = plan_job_notifications(pending, me, with_companies(config))
    assert len(summary.message.splitlines()) == 14
    assert "more" not in summary.message


def test_a_summary_too_long_for_ntfy_is_cut_to_whole_lines_and_says_how_many_are_left(config, me):
    padding = "x" * 80
    pending = [stored_job(n, title=f"Spring Week {n} {padding}") for n in range(80)]
    (summary,) = plan_job_notifications(pending, me, with_companies(config))
    lines = summary.message.splitlines()
    assert len(summary.message.encode()) <= MAX_MESSAGE_BYTES
    assert lines[-1] == f"+{80 - (len(lines) - 1)} more"
    assert all(line.startswith("Normal Co — Spring Week") for line in lines[:-1])
    assert len(summary.job_keys) == 80  # every job is still marked as announced


def test_fit_lines_keeps_everything_that_fits_including_a_last_line_without_reserve():
    ten = ["a" * 10, "b" * 10, "c" * 10]
    assert fit_lines(["a", "b"], limit=20) == "a\nb"
    assert fit_lines(ten, limit=20) == "a" * 10 + "\n+2 more"
    assert len(fit_lines(ten, limit=20).encode()) <= 20
    assert fit_lines(ten[:2], limit=22) == "a" * 10 + "\n" + "b" * 10  # last line needs no reserve
    assert fit_lines([], limit=20) == ""


def test_baseline_summary_lists_each_role_faang_plus_first(config, me):
    jobs = [stored_job(1), stored_job(2), stored_job(3, company="fireco")]
    notification = baseline_notification(jobs, me, with_companies(config), filters_changed=False)

    assert notification.title == "Baseline: 3 matching roles currently open"
    assert notification.message.splitlines() == [
        "🔥 Fire Co — Spring Week 3 · London, UK",
        "Normal Co — Spring Week 1 · London, UK",
        "Normal Co — Spring Week 2 · London, UK",
    ]
    # Three roles or fewer: a button each, and the tap opens the first (FAANG+ first).
    assert notification.click == "https://example.com/fireco/3"
    assert notification.actions == (
        Action("Apply: Fire Co – Spring Week 3", "https://example.com/fireco/3"),
        Action("Apply: Normal Co – Spring Week 1", "https://example.com/normalco/1"),
        Action("Apply: Normal Co – Spring Week 2", "https://example.com/normalco/2"),
    )
    assert notification.job_keys == ()


def test_a_baseline_or_summary_of_nothing_is_not_built(config, me):
    assert baseline_notification([], me, with_companies(config), filters_changed=True) is None
    assert summary_notification([], me, with_companies(config)) is None


def test_eligibility_is_its_own_line_on_an_alert_and_a_tick_in_a_summary(config, me):
    config = with_companies(config)
    job = stored_job(1)
    job.eligibility = 'title: "First Year"'
    alert = job_notification(job, me, config)
    assert alert.message.splitlines()[-1] == '✅ First-year eligible (title: "First Year")'
    assert role_line(job, me, config) == "Normal Co — Spring Week 1 · London, UK · ✅"

    job.eligibility = ""
    assert "First-year" not in job_notification(job, me, config).message


def test_test_alert_says_nothing_is_wrong():
    notification = health_test_notification(NOW)
    assert notification.title == "Test health alert"
    assert "Nothing is wrong" in notification.message
    assert "2026-11-15 12:00" in notification.message


# --- sending ---------------------------------------------------------------------------


OTHER_TOPIC = "secret-topic-of-someone-else"


def make_notifier(settings, handler, **kwargs):
    http = HttpClient(settings, transport=httpx.MockTransport(handler))
    topics = {"me": JOB_TOPIC, "other": OTHER_TOPIC, "no-topic": None}
    return Notifier(http, settings.ntfy_server, topics, HEALTH_TOPIC, **kwargs)


NOTIFICATION = Notification(
    title="Acme – Spring Week", message="London", priority=5, tags=("fire",), click="https://x/1"
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
        "tags": ["fire"],
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


def test_a_summary_line_carries_the_preference_note(config, me):
    note = "All years welcome · prefers penultimate/final"
    job = stored_job(1, eligibility='description: "x"', eligibility_note=note)
    assert role_line(job, me, with_companies(config)).endswith(f"· ✅ {note}")


# --- buttons on summaries --------------------------------------------------------------


def test_a_longer_summary_opens_the_readme_and_gives_faang_plus_roles_buttons(config, me):
    config = with_companies(config)
    jobs = [stored_job(n) for n in range(1, 5)] + [
        stored_job(n, company="fireco") for n in range(5, 10)
    ]
    for notification in (
        baseline_notification(jobs, me, config, filters_changed=False),
        summary_notification(jobs, me, config),
    ):
        assert notification.click.endswith("#open-roles")
        assert notification.message.splitlines()[0].startswith("🔥 Fire Co")
        assert [action.url for action in notification.actions] == [
            "https://example.com/fireco/5",
            "https://example.com/fireco/6",
            "https://example.com/fireco/7",
        ]


def test_a_longer_summary_without_faang_plus_roles_has_no_buttons(config, me):
    jobs = [stored_job(n) for n in range(1, 7)]
    notification = summary_notification(jobs, me, with_companies(config))
    assert notification.click.endswith("#open-roles")
    assert notification.actions == ()


def test_a_short_summary_gives_every_role_a_button_and_opens_the_first(config, me):
    jobs = [stored_job(2), stored_job(1)]
    notification = summary_notification(jobs, me, with_companies(config))
    assert notification.click == "https://example.com/normalco/1"
    assert [action.label for action in notification.actions] == [
        "Apply: Normal Co – Spring Week 1",
        "Apply: Normal Co – Spring Week 2",
    ]


def test_a_long_title_is_cut_on_a_word_in_its_button(config, me):
    title = "Insight into Technology Apprenticeships at Morgan Stanley (Glasgow)"
    notification = baseline_notification(
        [stored_job(1, title=title)], me, with_companies(config), filters_changed=False
    )
    (action,) = notification.actions
    assert action.label == "Apply: Normal Co – Insight into Technology…"
    assert notification.message.startswith(f"Normal Co — {title}")  # the list is whole

    # A word that ends exactly at the cut is kept.
    notification = baseline_notification(
        [stored_job(1, title="Quant Research Spring Week 2027 (London)")],
        me,
        with_companies(config),
        filters_changed=False,
    )
    assert notification.actions[0].label == "Apply: Normal Co – Quant Research Spring Week 2027…"


def test_buttons_are_sent_as_ntfy_view_actions_but_never_logged(settings, caplog):
    caplog.set_level("INFO")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    with_buttons = replace(
        NOTIFICATION,
        actions=(Action("Apply: Acme – Spring Week", "https://careers.example/job/77"),),
    )
    assert make_notifier(settings, handler).send_job("me", with_buttons) is True
    (request,) = requests
    assert json.loads(request.content)["actions"] == [
        {
            "action": "view",
            "label": "Apply: Acme – Spring Week",
            "url": "https://careers.example/job/77",
        }
    ]
    assert "notification_sent" in caplog.text
    assert "careers.example/job/77" not in caplog.text
