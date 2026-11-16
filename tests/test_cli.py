from conftest import OWNER, ROOT
from jobtracker.__main__ import main
from jobtracker.notify import Notifier

CONFIG = ["--config-dir", str(ROOT / "config")]


def test_explain_title_shows_the_decision_and_the_reason(capsys):
    titles = ["--title", "Technology Spring Week", "--title", "Spring Week – Finance"]
    assert main([*CONFIG, "explain", *titles]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "MATCH  Technology Spring Week  [London, UK]",
        "       role: spring_week | fields: software | location: uk (onsite)",
        "       first years: eligible unless the description says later years only",
        "skip   Spring Week – Finance  [London, UK]",
        "       field: 'finance' is not one of your fields",
    ]


def test_explain_title_uses_the_given_location(capsys):
    main([*CONFIG, "explain", "--title", "Spring Week", "--location", "New York, NY"])
    assert "us allows remote only" in capsys.readouterr().out


def test_explain_needs_a_company_or_a_title(capsys):
    assert main([*CONFIG, "explain"]) == 2


def test_explain_can_be_told_whose_filters_to_use(capsys):
    assert main([*CONFIG, "explain", "--profile", OWNER, "--title", "Spring Week"]) == 0
    assert capsys.readouterr().out.startswith("MATCH  Spring Week")

    assert main([*CONFIG, "explain", "--profile", "nobody", "--title", "Spring Week"]) == 2
    assert "No valid profile with key 'nobody'" in capsys.readouterr().err


def test_run_refuses_to_start_without_topics(monkeypatch, capsys):
    monkeypatch.delenv("NTFY_TOPIC_ABHINAV", raising=False)
    monkeypatch.delenv("NTFY_HEALTH_TOPIC", raising=False)
    assert main([*CONFIG, "run"]) == 2
    assert "NTFY_HEALTH_TOPIC must be set" in capsys.readouterr().err

    monkeypatch.setenv("NTFY_HEALTH_TOPIC", "health")
    assert main([*CONFIG, "run"]) == 2
    assert "No profile has a topic: set NTFY_TOPIC_ABHINAV" in capsys.readouterr().err


def test_run_rejects_an_unknown_profile(capsys):
    assert main([*CONFIG, "run", "--dry-run", "--profile", "nobody"]) == 2
    assert "No valid profile with key 'nobody'" in capsys.readouterr().err


def test_bad_config_is_reported_cleanly(tmp_path, capsys):
    assert main(["--config-dir", str(tmp_path), "explain", "--title", "x"]) == 2
    assert "Config error" in capsys.readouterr().err


def test_explain_title_shows_what_the_first_year_check_makes_of_it(capsys):
    titles = ["First Year Software Engineering Programme", "Software Engineer Intern"]
    assert main([*CONFIG, "explain", *(arg for t in titles for arg in ("--title", t))]) == 0
    out = capsys.readouterr().out
    assert 'first years: eligible (title: "First Year")' in out
    assert "first years: only if the description says first years can apply" in out


def test_test_alert_needs_the_health_topic(monkeypatch, capsys):
    monkeypatch.delenv("NTFY_HEALTH_TOPIC", raising=False)
    assert main([*CONFIG, "test-alert"]) == 2
    assert "NTFY_HEALTH_TOPIC must be set" in capsys.readouterr().err


def test_test_alert_sends_one_message_and_never_prints_the_topic(monkeypatch, capsys):
    sent = []
    monkeypatch.setenv("NTFY_HEALTH_TOPIC", "secret-health-topic")
    monkeypatch.setattr(Notifier, "send_health", lambda self, n: sent.append(n) or True)
    assert main([*CONFIG, "test-alert"]) == 0
    assert [n.title for n in sent] == ["Test health alert"]
    out = capsys.readouterr()
    assert "Test alert sent" in out.out
    assert "secret-health-topic" not in out.out + out.err


def test_explain_will_not_fetch_a_paused_kind_of_source(capsys):
    assert main([*CONFIG, "explain", "lloyds-banking-group"]) == 2
    assert "workday is paused since 2026-11-16 17:00 UTC" in capsys.readouterr().err
