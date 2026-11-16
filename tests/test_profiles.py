"""Profiles are isolated from each other. These tests are the proof.

The promise: adding, changing, removing or breaking one profile never changes what any
other profile receives. Most tests here check it the blunt way. The same eight-run story
is played twice, on two separate fake installations: once with Alice as the only profile
and once with somebody else alongside her. Alice's side of the two must be identical,
down to every notification published to her topic and every byte of her state file.

Companies in the story:

    point72    quant        Alice's industry; other people may watch it too
    palantir   big_tech     Alice's industry
    openai     ai_lab       Alice's industry
    consultco  consulting   an industry Alice does not watch
    privateco  quant        Alice's industry, but opt_in: added for somebody else
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from conftest import NOW, FakeWeb, changed_filters, make_company, make_profile
from jobtracker import pipeline
from jobtracker.http import HttpClient
from jobtracker.notify import Notifier
from jobtracker.state import CLOSED, OPEN, load_profile_state, profile_path

RUN = timedelta(minutes=15)
HEALTH_TOPIC = "secret-health-topic"
TOPICS = {
    "aaron": "secret-topic-aaron",
    "alice": "secret-topic-alice",
    "bob": "secret-topic-bob",
    "zoe": "secret-topic-zoe",
}

COMPANIES = (
    make_company("point72", name="Point72"),
    make_company("palantir", name="Palantir", ats="lever", industry="big_tech"),
    make_company("openai", name="OpenAI", ats="ashby", industry="ai_lab"),
    make_company("consultco", name="ConsultCo", industry="consulting"),
    make_company("privateco", name="PrivateCo", opt_in=True),
)

SPRING_INSIGHT = "greenhouse:point72:8845395002"  # open on Point72's board from the start


# --- the story: one change to the outside world before each run ------------------------


def nothing_changes(web):
    pass


def a_spring_week_opens(web):
    web.add_greenhouse_job(9000001, "Spring Week 2027 - Technology")


def a_busy_day(web):
    for number in range(6):  # more than the batch threshold
        web.add_greenhouse_job(9000100 + number, f"Insight Day {number}")
    web.add_greenhouse_job(9000200, "Software Engineer Intern")  # not a role type Alice has on
    web.add_greenhouse_job(7000001, "Spring Week 2027", board="consultco")
    web.add_greenhouse_job(8000001, "Spring Week 2027", board="privateco")


def the_original_is_reposted(web):
    original = web.boards["boards-api.greenhouse.io"]["jobs"][0]
    web.remove_greenhouse_job(original["id"])
    web.add_greenhouse_job(9000300, original["title"], original["location"]["name"])


def the_spring_week_is_withdrawn(web):
    web.remove_greenhouse_job(9000001)


def privateco_posts_again(web):
    web.add_greenhouse_job(8000002, "Insight Day", board="privateco")


STORY = [
    nothing_changes,  # run 0: first run, everything is baselined
    a_spring_week_opens,  # run 1: one new job -> one alert
    a_busy_day,  # run 2: a batch, plus jobs at the two companies Alice does not watch
    the_original_is_reposted,  # run 3: a repost -> no alert
    the_spring_week_is_withdrawn,  # run 4: missing once
    nothing_changes,  # run 5: missing twice -> closed
    privateco_posts_again,  # run 6
    nothing_changes,  # run 7
]


# --- the stage -------------------------------------------------------------------------


class World:
    """One installation of the tracker: its own fake internet and its own state folder."""

    def __init__(self, base_config, state_dir):
        self.base_config = base_config
        self.state_dir = state_dir
        self.web = FakeWeb()
        self.web.add_greenhouse_board("consultco")
        self.web.add_greenhouse_board("privateco")
        self.reports = []

    def run(self, profiles, now=NOW, topics=TOPICS, profile_errors=None):
        profiles = tuple(sorted(profiles, key=lambda profile: profile.key))
        config = replace(
            self.base_config, profiles=profiles, profile_errors=dict(profile_errors or {})
        )
        http = HttpClient(
            config.settings, transport=httpx.MockTransport(self.web.handle), sleep=lambda _: None
        )
        job_topics = {profile.key: topics.get(profile.key) for profile in profiles}
        notifier = Notifier(http, config.settings.ntfy_server, job_topics, HEALTH_TOPIC)
        report = pipeline.run(config, self.state_dir, http, notifier, now)
        self.reports.append(report)
        return report

    def play(self, cast, topics=TOPICS, watching="alice"):
        """Play the whole story. `cast(run_number)` says which profiles exist in that run.

        Returns one entry per run: everything published to the watched profile's topic
        so far, and the bytes of its state file.
        """
        seen = []
        for number, change in enumerate(STORY):
            change(self.web)
            self.run(cast(number), NOW + number * RUN, topics)
            seen.append(self.view(watching))
        return seen

    def view(self, key):
        path = profile_path(self.state_dir, key)
        return (
            list(self.web.sent_to(TOPICS[key])),
            path.read_bytes() if path.exists() else None,
        )

    def titles(self, key):
        return [message["title"] for message in self.web.sent_to(TOPICS[key])]

    def state(self, key):
        return load_profile_state(self.state_dir, key)


@pytest.fixture
def new_world(config, settings, tmp_path):
    counter = iter(range(100))

    def build():
        base = replace(config, settings=settings, companies=COMPANIES)
        return World(base, tmp_path / f"world-{next(counter)}")

    return build


@pytest.fixture
def alice(profile):
    """Watches three industries with the shipped filters. Palantir is her FAANG+ company."""
    return make_profile(
        profile, "alice", industries=("quant", "big_tech", "ai_lab"), faang_plus=("palantir",)
    )


@pytest.fixture
def bob(profile):
    """Overlaps Alice on Point72 and differs everywhere else: every role type switched on,
    two companies she does not watch, and his own FAANG+ list."""
    return make_profile(
        profile,
        "bob",
        industries=("quant", "consulting"),
        companies=("privateco",),
        faang_plus=("point72",),
        filters=changed_filters(profile.filters, hash="bob-rules"),
    )


@pytest.fixture
def alice_alone(new_world, alice):
    """The reference: Alice's side of the story when hers is the only profile."""
    return new_world().play(lambda run: (alice,))


# --- the reference run is not trivially empty ------------------------------------------


def test_the_story_gives_alice_something_to_receive(new_world, alice):
    world = new_world()
    world.play(lambda run: (alice,))

    assert world.titles("alice") == [
        "Baseline: 1 matching role currently open",
        "Point72 – Spring Week 2027 - Technology",
        "6 new roles",
    ]
    jobs = world.state("alice").jobs
    assert jobs["greenhouse:point72:9000001"].status == CLOSED
    assert jobs["greenhouse:point72:9000300"].aliases == [SPRING_INSIGHT]  # the repost
    assert {job.company for job in jobs.values()} == {"point72"}


def test_and_gives_bob_something_different(new_world, alice, bob):
    world = new_world()
    world.play(lambda run: (alice, bob))

    assert world.titles("bob") == [
        # Run 0. His filters also match an internship on Point72's board that Alice's ignore.
        "Baseline: 2 matching roles currently open",
        "🔥 Point72 – Spring Week 2027 - Technology",
        # Run 2. Point72 is his FAANG+ company, so its jobs are never batched...
        *(f"🔥 Point72 – Insight Day {number}" for number in range(6)),
        "🔥 Point72 – Software Engineer Intern",
        # ...and the rest are: ConsultCo and PrivateCo, which Alice never hears about.
        "2 new roles",
        "PrivateCo – Insight Day",
    ]
    companies = {job.company for job in world.state("bob").jobs.values()}
    assert companies == {"point72", "consultco", "privateco"}


# --- adding or changing another profile changes nothing for Alice ----------------------


def casts(alice, bob, base_profile):
    """Ways for other profiles to come, go and change while the story plays."""
    twin = replace(alice, key="bob")
    aaron = replace(bob, key="aaron")  # sorts, and so runs, before Alice
    zoe = replace(bob, key="zoe", industries=frozenset({"quant", "big_tech", "ai_lab"}))
    bob_new_filters = replace(bob, filters=changed_filters(base_profile.filters, hash="bob-v2"))
    bob_new_scope = replace(bob_new_filters, industries=frozenset({"consulting", "big_tech"}))
    bob_new_faang_plus = replace(bob_new_scope, faang_plus=frozenset({"palantir", "consultco"}))
    bob_off = replace(bob, filters=replace(base_profile.filters, role_types=(), hash="nothing"))

    def changing_bob(run):
        if run < 2:
            return (alice, bob)
        if run < 4:
            return (alice, bob_new_filters)
        if run < 6:
            return (alice, bob_new_scope)
        return (alice, bob_new_faang_plus)

    return {
        "an identical twin": lambda run: (alice, twin),
        "different filters, scope and FAANG+ list": lambda run: (alice, bob),
        "a profile that runs before hers": lambda run: (aaron, alice),
        "profiles before and after hers": lambda run: (aaron, alice, bob, zoe),
        "added part-way through": lambda run: (alice, bob) if run >= 3 else (alice,),
        "removed part-way through": lambda run: (alice, bob) if run < 3 else (alice,),
        "added, removed and added again": lambda run: (alice, bob) if run % 3 else (alice,),
        "edited between runs": changing_bob,
        "matches nothing at all": lambda run: (alice, bob_off),
    }


CAST_NAMES = [
    "an identical twin",
    "different filters, scope and FAANG+ list",
    "a profile that runs before hers",
    "profiles before and after hers",
    "added part-way through",
    "removed part-way through",
    "added, removed and added again",
    "edited between runs",
    "matches nothing at all",
]


@pytest.mark.parametrize("others", CAST_NAMES)
def test_alice_receives_exactly_the_same_whoever_else_has_a_profile(
    new_world, alice, bob, profile, alice_alone, others
):
    cast = casts(alice, bob, profile)[others]
    assert new_world().play(cast) == alice_alone


def test_every_cast_is_covered(alice, bob, profile):
    assert sorted(casts(alice, bob, profile)) == sorted(CAST_NAMES)


def test_it_holds_in_the_other_direction_too(new_world, alice, bob):
    bob_alone = new_world().play(lambda run: (bob,), watching="bob")
    assert new_world().play(lambda run: (alice, bob), watching="bob") == bob_alone


# --- a broken profile changes nothing for Alice either ---------------------------------


def test_profile_with_no_topic_is_skipped_and_alice_is_unaffected(
    new_world, alice, bob, alice_alone
):
    world = new_world()
    topics = {**TOPICS, "bob": None}
    assert world.play(lambda run: (alice, bob), topics=topics) == alice_alone

    report = world.reports[-1]
    assert report.profile("alice").status == "ok"
    assert report.profile("bob").status == "skipped"
    assert report.profile("bob").error == "NTFY_TOPIC_BOB is not set"
    # Nothing was recorded for Bob, so he starts cleanly once he has a topic.
    assert world.view("bob") == ([], None)
    assert all(
        message["topic"] in (TOPICS["alice"], HEALTH_TOPIC) for message in world.web.published
    )


def test_profile_that_did_not_run_is_flagged_on_the_actions_run_page(
    new_world, alice, bob, monkeypatch, capsys
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    new_world().run((alice, bob), topics={**TOPICS, "bob": None})

    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::")]
    assert warnings == ["::warning title=Profile 'bob' did not run::NTFY_TOPIC_BOB is not set"]


def test_profile_whose_config_is_invalid_is_reported_and_alice_is_unaffected(
    new_world, alice, alice_alone
):
    world = new_world()
    for number, change in enumerate(STORY):
        change(world.web)
        errors = {"bob": "config/profiles/bob/profile.yaml: unknown key 'dreams'"}
        report = world.run((alice,), NOW + number * RUN, profile_errors=errors)
        assert world.view("alice") == alice_alone[number]

    assert report.profile("bob").status == "invalid"
    assert report.profile("alice").status == "ok"
    assert report.any_profile_ran


def test_profile_whose_topic_rejects_messages_does_not_hold_up_alice(
    new_world, alice, bob, alice_alone
):
    world = new_world()
    world.web.rejected_topics.add(TOPICS["bob"])
    assert world.play(lambda run: (alice, bob)) == alice_alone
    assert world.web.sent_to(TOPICS["bob"]) == []
    assert world.reports[2].profile("bob").job_alerts_failed > 0


@pytest.mark.parametrize("crash_in", ["classify", "plan_job_notifications", "save_profile_state"])
def test_profile_that_crashes_is_contained_and_alice_is_unaffected(
    new_world, alice, bob, alice_alone, monkeypatch, crash_in
):
    original = getattr(pipeline, crash_in)

    def crash_for_bob(*args, **kwargs):
        if bob.filters in args or bob in args or "bob" in args:
            raise RuntimeError("something only Bob's profile trips over")
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, crash_in, crash_for_bob)
    for cast in (lambda run: (alice, bob), lambda run: (replace(bob, key="aaron"), alice, bob)):
        world = new_world()
        assert world.play(cast) == alice_alone

        report = world.reports[-1]
        assert report.profile("alice").status == "ok"
        assert report.profile("bob").status == "error"
        assert report.profile("bob").error.startswith("RuntimeError")
        assert world.view("bob")[1] is None  # nothing half-finished was saved for him


def test_profile_with_a_corrupt_state_file_is_contained(new_world, alice, bob, alice_alone):
    world = new_world()
    path = profile_path(world.state_dir, "bob")
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")

    assert world.play(lambda run: (alice, bob)) == alice_alone
    assert world.reports[-1].profile("bob").status == "error"
    assert path.read_text() == "{ not json"  # left for a human to look at


def test_run_counts_as_failed_only_when_no_profile_got_through(new_world, alice, bob, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "plan_job_notifications", crash)
    assert new_world().run((alice, bob)).any_profile_ran is False
    monkeypatch.undo()
    assert new_world().run((alice, bob), topics={**TOPICS, "bob": None}).any_profile_ran is True


# --- shared: fetching and health -------------------------------------------------------


def test_each_company_is_fetched_once_however_many_profiles_watch_it(new_world, alice, bob):
    world = new_world()
    twin = replace(alice, key="zoe")
    world.run((alice, bob, twin))

    assert sorted(world.web.fetches) == sorted(
        [
            "boards-api.greenhouse.io/v1/boards/point72/jobs",  # watched by all three
            "boards-api.greenhouse.io/v1/boards/consultco/jobs",
            "boards-api.greenhouse.io/v1/boards/privateco/jobs",
            "api.lever.co/v0/postings/palantir",
            "api.ashbyhq.com/posting-api/job-board/openai",
        ]
    )


def test_company_nobody_watches_is_not_fetched(new_world, alice):
    world = new_world()
    world.run((alice,))
    assert not any("consultco" in url or "privateco" in url for url in world.web.fetches)
    assert len(world.web.fetches) == 3


def test_health_alert_is_sent_once_to_the_health_topic_whoever_watches_the_board(
    new_world, alice, bob
):
    world = new_world()
    world.web.broken_hosts.add("boards-api.greenhouse.io")  # Point72: watched by both
    world.run((alice, bob), NOW)
    world.run((alice, bob), NOW + RUN)

    # One message, not one per profile. (Three boards share the broken host.)
    health = world.web.sent_to(HEALTH_TOPIC)
    assert [message["title"] for message in health] == ["Greenhouse: 3 sources failing (HTTP 500)"]
    assert "Point72" in health[0]["message"]
    # Neither profile's own topic hears about it.
    assert world.titles("alice") == []
    assert world.titles("bob") == []


# --- scope: a company added for one profile reaches nobody else ------------------------


def test_company_added_for_bob_never_alerts_alice(new_world, alice, bob):
    world = new_world()
    world.play(lambda run: (alice, bob))

    sent_to_alice = " ".join(m["title"] + m["message"] for m in world.web.sent_to(TOPICS["alice"]))
    assert "ConsultCo" not in sent_to_alice
    assert "PrivateCo" not in sent_to_alice
    alices_companies = {job.company for job in world.state("alice").jobs.values()}
    assert alices_companies.isdisjoint({"consultco", "privateco"})
    assert not any("privateco" in key for key in world.state("alice").baselines)
    # ...while Bob, who named it, does hear about it.
    assert "PrivateCo – Insight Day" in world.titles("bob")


def test_alice_gets_the_company_once_she_opts_in_and_bob_notices_nothing(new_world, alice, bob):
    opted_in = replace(alice, companies=frozenset({"privateco"}))

    def cast(run):
        return (opted_in if run >= 4 else alice, bob)

    world = new_world()
    world.play(cast)

    # Opting in baselines what is already open there (one summary), then alerts as usual.
    assert world.titles("alice")[-2:] == [
        "Baseline: 1 matching role currently open",
        "PrivateCo – Insight Day",
    ]
    baseline = world.web.sent_to(TOPICS["alice"])[-2]
    assert baseline["message"] == "PrivateCo — Spring Week 2027 · London, UK · ✅"

    bob_without_her_opting_in = new_world().play(lambda run: (alice, bob), watching="bob")
    assert world.view("bob") == bob_without_her_opting_in[-1]


def test_company_leaving_one_profiles_scope_closes_its_jobs_for_that_profile_only(
    new_world, alice, bob
):
    world = new_world()
    world.run((alice, bob), NOW)
    assert world.state("alice").jobs[SPRING_INSIGHT].status == OPEN
    assert world.state("bob").jobs[SPRING_INSIGHT].status == OPEN

    bob_without_quant = replace(bob, industries=frozenset({"consulting"}))
    world.run((alice, bob_without_quant), NOW + RUN)

    assert world.state("bob").jobs[SPRING_INSIGHT].status == CLOSED
    assert "greenhouse:point72" not in world.state("bob").baselines
    assert world.state("alice").jobs[SPRING_INSIGHT].status == OPEN


# --- dedupe, notified-state and FAANG+ are per profile -----------------------------


def test_same_job_is_alerted_to_each_profile_once_at_that_profiles_priority(new_world, alice, bob):
    world = new_world()
    world.run((alice, bob), NOW)
    world.web.published.clear()

    world.web.add_greenhouse_job(9000001, "Spring Week 2027")
    world.run((alice, bob), NOW + RUN)
    world.run((alice, bob), NOW + 2 * RUN)

    (to_alice,) = world.web.sent_to(TOPICS["alice"])
    (to_bob,) = world.web.sent_to(TOPICS["bob"])
    assert to_alice["title"] == "Point72 – Spring Week 2027"
    assert to_bob["title"] == "🔥 Point72 – Spring Week 2027"
    # Point72 is on Bob's FAANG+ list and not on Alice's.
    assert (to_alice["priority"], to_alice["tags"]) == (3, ["cherry_blossom"])
    assert (to_bob["priority"], to_bob["tags"]) == (5, ["cherry_blossom", "fire"])


def test_alert_one_profile_missed_is_retried_for_that_profile_alone(new_world, alice, bob):
    world = new_world()
    world.run((alice, bob), NOW)
    world.web.published.clear()
    world.web.add_greenhouse_job(9000001, "Spring Week 2027")

    world.web.rejected_topics.add(TOPICS["bob"])
    world.run((alice, bob), NOW + RUN)
    key = "greenhouse:point72:9000001"
    assert world.state("alice").jobs[key].notified is True
    assert world.state("bob").jobs[key].notified is False

    world.web.rejected_topics.clear()
    world.run((alice, bob), NOW + 2 * RUN)
    assert world.titles("alice") == ["Point72 – Spring Week 2027"]  # once, not twice
    assert world.titles("bob") == ["🔥 Point72 – Spring Week 2027"]
    assert world.state("bob").jobs[key].notified is True


def test_new_profile_is_baselined_on_what_others_were_already_alerted_about(new_world, alice, bob):
    world = new_world()
    world.run((alice,), NOW)
    world.web.add_greenhouse_job(9000001, "Spring Week 2027")
    world.run((alice,), NOW + RUN)
    alice_before = world.view("alice")

    world.run((alice, bob), NOW + 2 * RUN)

    # Bob joins: one summary of what is open, no flood, and no second alert for Alice.
    assert world.titles("bob") == ["Baseline: 3 matching roles currently open"]
    assert all(job.notified for job in world.state("bob").jobs.values())
    assert world.view("alice") == alice_before


def test_changing_one_profiles_filters_baselines_that_profile_only(new_world, alice, bob):
    world = new_world()
    world.run((alice, bob), NOW)
    world.web.published.clear()

    alice_wider = replace(alice, filters=changed_filters(alice.filters, hash="alice-v2"))
    world.run((alice_wider, bob), NOW + RUN)

    assert world.titles("alice") == ["Baseline: 2 roles newly match your filters"]
    assert world.titles("bob") == []
    assert set(world.state("bob").baselines.values()) == {"bob-rules"}
