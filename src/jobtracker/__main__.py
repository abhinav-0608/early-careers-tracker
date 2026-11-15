"""Command line entry point.

python -m jobtracker run                 poll everything, alert, save state
python -m jobtracker run --dry-run       log notifications instead of sending; save nothing
python -m jobtracker explain <company>   show why each live job did or did not match
python -m jobtracker explain --title "Spring Week - Technology"
                                         test the filters on a title, without fetching
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from jobtracker import log
from jobtracker.config import Config, ConfigError, load_config
from jobtracker.filters import classify, match_role_type
from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.notify import Notifier
from jobtracker.pipeline import fetch_company, run
from jobtracker.sources import ADAPTERS
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

    args = parser.parse_args(argv)
    log.setup_logging()
    try:
        config = load_config(args.config_dir)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "run":
        return command_run(config, args)
    return command_explain(config, args)


def command_run(config: Config, args: argparse.Namespace) -> int:
    job_topic = os.environ.get("NTFY_TOPIC")
    health_topic = os.environ.get("NTFY_HEALTH_TOPIC")
    if not args.dry_run and not (job_topic and health_topic):
        print("NTFY_TOPIC and NTFY_HEALTH_TOPIC must be set (or pass --dry-run).", file=sys.stderr)
        return 2

    http = HttpClient(config.settings)
    notifier = Notifier(
        http, config.settings.ntfy_server, job_topic, health_topic, dry_run=args.dry_run
    )
    try:
        run(
            config,
            state_dir=args.state_dir,
            http=http,
            notifier=notifier,
            now=datetime.now(UTC),
            only=set(args.only) if args.only else None,
            save=not args.dry_run,
        )
    finally:
        http.close()
    # Source failures are reported through health alerts, not the exit code: a broken
    # careers page should not mark the whole run as failed.
    return 0


def command_explain(config: Config, args: argparse.Namespace) -> int:
    if args.title:
        return explain_titles(config, args)
    if not args.company:
        print("Give a company key, or one or more --title options.", file=sys.stderr)
        return 2
    return explain_company(config, args)


def explain_titles(config: Config, args: argparse.Namespace) -> int:
    """Classify hand-typed titles. Nothing is fetched."""
    if args.company:
        company = config.company(args.company)
        if company is None:
            print(f"No company with key '{args.company}'.", file=sys.stderr)
            return 2
    else:  # a stand-in with no tags, so the India rule behaves as for an untagged company
        company = Company(key="example", name="Example", industry="", tier="normal", ats="")

    for title in args.title:
        job = Job(
            source="manual",
            company=company.key,
            job_id="0",
            title=title,
            url="",
            locations=(args.location,) if args.location else (),
        )
        print_decision(job, company, config)
    return 0


def explain_company(config: Config, args: argparse.Namespace) -> int:
    """Fetch one company's live jobs and show how each one classifies."""
    company = config.company(args.company)
    if company is None or company.ats not in ADAPTERS:
        print(f"No pollable company with key '{args.company}'.", file=sys.stderr)
        return 2

    http = HttpClient(config.settings)
    try:
        result = fetch_company(company, http)
    finally:
        http.close()
    if result.error:
        print(f"Fetch failed ({result.status}): {result.error}", file=sys.stderr)
        return 1

    shown = 0
    for job in result.jobs:
        decision = classify(job, company, config.filters)
        near_miss = match_role_type(normalise(job.title), config.filters) is not None
        if decision.matched or near_miss or args.all:
            shown += 1
            print_decision(job, company, config)
    print(f"\n{len(result.jobs)} jobs fetched, {shown} shown. Use --all to list every job.")
    return 0


def print_decision(job: Job, company: Company, config: Config) -> None:
    decision = classify(job, company, config.filters)
    mark = "MATCH" if decision.matched else "skip "
    print(f"{mark}  {job.title}  [{'; '.join(job.locations) or 'no location'}]")
    if decision.matched:
        fields = ", ".join(decision.fields) or "none named"
        print(f"       role: {decision.role_type} | fields: {fields} | {decision.reason}")
    else:
        print(f"       {decision.reason}")


if __name__ == "__main__":
    sys.exit(main())
