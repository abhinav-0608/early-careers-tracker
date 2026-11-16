"""Command line entry point.

python -m jobtracker run                 poll everything, alert every profile, save state
python -m jobtracker run --dry-run       log notifications instead of sending; save nothing
python -m jobtracker run --profile KEY   evaluate just this profile (repeatable)
python -m jobtracker explain <company>   show why each live job did or did not match
python -m jobtracker explain --title "Spring Week - Technology"
                                         test the filters on a title, without fetching
python -m jobtracker test-alert          send a test message to the health topic

explain uses one profile's filters: the only profile if there is just one, otherwise
the one named with --profile.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from jobtracker import eligibility, log
from jobtracker.config import Config, ConfigError, Profile, load_config
from jobtracker.filters import classify, match_role_type
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.notify import Notifier, health_test_notification
from jobtracker.pipeline import fetch_company, run
from jobtracker.sources import adapter_for, descriptions
from jobtracker.state import load_hosts, save_hosts
from jobtracker.text import normalise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobtracker", description=__doc__.splitlines()[0])
    parser.add_argument("--config-dir", default="config")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="poll all companies and send alerts")
    run_parser.add_argument("--state-dir", default="state")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log notifications instead of sending them, and do not save state",
    )
    run_parser.add_argument(
        "--only", action="append", metavar="COMPANY", help="poll just this company key (repeatable)"
    )
    run_parser.add_argument(
        "--profile", action="append", metavar="KEY", help="run just this profile (repeatable)"
    )
    run_parser.add_argument(
        "--readme",
        default="README.md",
        help="the README whose open-roles table is regenerated (not written in a dry run)",
    )

    commands.add_parser(
        "test-alert", help="send a test message to the health topic (NTFY_HEALTH_TOPIC)"
    )

    explain_parser = commands.add_parser("explain", help="show how jobs classify, and why")
    explain_parser.add_argument("company", nargs="?", help="company key from companies.yaml")
    explain_parser.add_argument(
        "--all", action="store_true", help="include jobs that matched no role type at all"
    )
    explain_parser.add_argument(
        "--title",
        action="append",
        metavar="TITLE",
        help="classify this title instead of fetching live jobs (repeatable)",
    )
    explain_parser.add_argument(
        "--location", default="London, UK", help="location to use with --title"
    )
    explain_parser.add_argument("--profile", metavar="KEY", help="whose filters to use")

    args = parser.parse_args(argv)
    log.setup_logging()
    try:
        config = load_config(args.config_dir)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    # A profile whose files are invalid is left out; the rest carry on without it.
    for key, error in config.profile_errors.items():
        print(f"Profile '{key}' is invalid and will be skipped: {error}", file=sys.stderr)

    if args.command == "run":
        return command_run(config, args)
    if args.command == "test-alert":
        return command_test_alert(config)
    return command_explain(config, args)


def command_test_alert(config: Config) -> int:
    """Send one message to the health topic, to check that alerts reach the phone."""
    topic = os.environ.get("NTFY_HEALTH_TOPIC")
    if not topic:
        print("NTFY_HEALTH_TOPIC must be set.", file=sys.stderr)
        return 2
    http = HttpClient(config.settings)
    try:
        notifier = Notifier(http, config.settings.ntfy_server, {}, topic)
        sent = notifier.send_health(health_test_notification(datetime.now(UTC)))
    finally:
        http.close()
    # The topic is never printed: it is a secret, and this may run in a public log.
    print("Test alert sent to the health topic." if sent else "Test alert could not be sent.")
    return 0 if sent else 1


def command_run(config: Config, args: argparse.Namespace) -> int:
    wanted = set(args.profile) if args.profile else None
    for key in sorted(wanted or ()):
        if config.profile(key) is None:
            print(f"No valid profile with key '{key}'.", file=sys.stderr)
            return 2
    profiles = [profile for profile in config.profiles if not wanted or profile.key in wanted]

    # Each profile's topic comes from its own environment variable (NTFY_TOPIC_<KEY>).
    job_topics = {profile.key: os.environ.get(profile.topic_env) for profile in profiles}
    health_topic = os.environ.get("NTFY_HEALTH_TOPIC")
    if not args.dry_run:
        if not health_topic:
            print("NTFY_HEALTH_TOPIC must be set (or pass --dry-run).", file=sys.stderr)
            return 2
        if not any(job_topics.values()):
            # A profile without a topic is skipped, but with none at all there is no run.
            names = ", ".join(profile.topic_env for profile in profiles)
            print(f"No profile has a topic: set {names} (or pass --dry-run).", file=sys.stderr)
            return 2

    now = datetime.now(UTC)
    # The client remembers robots.txt rules and back-offs between runs (state/hosts.json).
    http = HttpClient(config.settings, hosts=load_hosts(args.state_dir), now=now)
    notifier = Notifier(
        http, config.settings.ntfy_server, job_topics, health_topic, dry_run=args.dry_run
    )
    try:
        report = run(
            config,
            state_dir=args.state_dir,
            http=http,
            notifier=notifier,
            now=now,
            only=set(args.only) if args.only else None,
            only_profiles=wanted,
            save=not args.dry_run,
            readme_path=args.readme,
        )
    finally:
        http.close()
        if not args.dry_run:
            # Saved even if the run crashed: a back-off a host asked for must not be lost.
            save_hosts(http.hosts_state(), args.state_dir)
    # Source failures are reported through health alerts, not the exit code: a broken
    # careers page should not mark the whole run as failed. Nor does one profile failing
    # while others ran, because their state still has to be committed. Only a run in
    # which no profile got through at all is a failed run.
    return 0 if report.any_profile_ran else 1


def pick_profile(config: Config, key: str | None) -> Profile | None:
    """The profile whose filters `explain` should use; prints why if there is none."""
    if key is None and len(config.profiles) == 1:
        return config.profiles[0]
    if key is None:
        keys = ", ".join(profile.key for profile in config.profiles)
        print(f"Several profiles exist; choose one with --profile ({keys}).", file=sys.stderr)
        return None
    profile = config.profile(key)
    if profile is None:
        print(f"No valid profile with key '{key}'.", file=sys.stderr)
    return profile


def command_explain(config: Config, args: argparse.Namespace) -> int:
    if not args.title and not args.company:
        print("Give a company key, or one or more --title options.", file=sys.stderr)
        return 2
    profile = pick_profile(config, args.profile)
    if profile is None:
        return 2
    if args.title:
        return explain_titles(config, profile, args)
    return explain_company(config, profile, args)


def explain_titles(config: Config, profile: Profile, args: argparse.Namespace) -> int:
    """Classify hand-typed titles. Nothing is fetched."""
    if args.company:
        company = config.company(args.company)
        if company is None:
            print(f"No company with key '{args.company}'.", file=sys.stderr)
            return 2
    else:  # a stand-in with no tags, so the India rule behaves as for an untagged company
        company = Company(key="example", name="Example", industry="", ats="")

    for title in args.title:
        job = Job(
            source="manual",
            company=company.key,
            job_id="0",
            title=title,
            url="",
            locations=(args.location,) if args.location else (),
        )
        print_decision(job, company, profile)
    return 0


def explain_company(config: Config, profile: Profile, args: argparse.Namespace) -> int:
    """Fetch one company's live jobs and show how each one classifies."""
    company = config.company(args.company)
    if company is None or adapter_for(company) is None:
        print(f"No pollable company with key '{args.company}'.", file=sys.stderr)
        return 2
    if not profile.watches(company):
        print(f"Note: '{company.key}' is outside the scope of profile '{profile.key}'.")

    http = HttpClient(config.settings)
    try:
        result = fetch_company(company, http)
    finally:
        http.close()
    if result.error:
        print(f"Fetch failed ({result.status}): {result.error}", file=sys.stderr)
        return 1

    shown = 0
    http = HttpClient(config.settings)  # for descriptions the eligibility check needs
    try:
        for job in result.jobs:
            decision = classify(job, company, profile.filters)
            near_miss = match_role_type(normalise(job.title), profile.filters) is not None
            if decision.matched or near_miss or args.all:
                shown += 1
                print_decision(job, company, profile, http)
    finally:
        http.close()
    print(f"\n{len(result.jobs)} jobs fetched, {shown} shown. Use --all to list every job.")
    return 0


def print_decision(
    job: Job, company: Company, profile: Profile, http: HttpClient | None = None
) -> None:
    """One job's classification. With `http`, a description the verdict needs is read."""
    decision = classify(job, company, profile.filters)
    mark = "MATCH" if decision.matched else "skip "
    print(f"{mark}  {job.title}  [{'; '.join(job.locations) or 'no location'}]")
    if not decision.matched:
        print(f"       {decision.reason}")
        return
    fields = ", ".join(decision.fields) or "none named"
    print(f"       role: {decision.role_type} | fields: {fields} | {decision.reason}")
    line = explain_eligibility(job, company, profile, decision.role_type, http)
    if line:
        print(f"       first years: {line}")


def explain_eligibility(
    job: Job, company: Company, profile: Profile, role_type: str | None, http: HttpClient | None
) -> str:
    """The eligibility verdict as `explain` prints it; "" when it is not checked."""
    filters = profile.filters
    role = filters.role_type(role_type or "")
    if role is None or not filters.eligibility.applies_to(role.name):
        return ""
    text = None
    if eligibility.needs_description(role, job.title, filters):
        if http is None:  # --title: nothing is fetched
            if role.name in filters.eligibility.explicit_only:
                return "only if the description says first years can apply"
            return "eligible unless the description says later years only"
        try:
            text = descriptions.describe(company, job, http)
        except Exception as exc:  # noqa: BLE001 - explain must keep going
            return f"description could not be read ({type(exc).__name__})"
    verdict = eligibility.assess(role, job.title, text, filters)
    if verdict.eligible:
        return f"eligible ({verdict.basis})"
    return f"dropped: {verdict.reason}"


if __name__ == "__main__":
    sys.exit(main())
