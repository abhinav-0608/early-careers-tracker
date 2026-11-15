from conftest import ROOT
from jobtracker.__main__ import main

CONFIG = ["--config-dir", str(ROOT / "config")]


def test_explain_title_shows_the_decision_and_the_reason(capsys):
    titles = ["--title", "Technology Spring Week", "--title", "Spring Week – Finance"]
    assert main([*CONFIG, "explain", *titles]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "MATCH  Technology Spring Week  [London, UK]",
        "       role: spring_week | fields: software | location: uk (onsite)",
        "skip   Spring Week – Finance  [London, UK]",
        "       field: 'finance' is not one of your fields",
    ]


def test_explain_title_uses_the_given_location(capsys):
    main([*CONFIG, "explain", "--title", "Spring Week", "--location", "New York, NY"])
    assert "us allows remote only" in capsys.readouterr().out


def test_explain_needs_a_company_or_a_title(capsys):
    assert main([*CONFIG, "explain"]) == 2


def test_run_refuses_to_start_without_topics(monkeypatch, capsys):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_HEALTH_TOPIC", raising=False)
    assert main([*CONFIG, "run"]) == 2
    assert "NTFY_TOPIC and NTFY_HEALTH_TOPIC must be set" in capsys.readouterr().err


def test_bad_config_is_reported_cleanly(tmp_path, capsys):
    assert main(["--config-dir", str(tmp_path), "explain", "--title", "x"]) == 2
    assert "Config error" in capsys.readouterr().err
