# CLAUDE.md: standing rules for this repo

Read this first, every session. DESIGN.md is the full design and its decision list
(section 14) is the record of why; this file is the short list of rules that bind every
change. If the two disagree, stop and ask (it is a MUST-stop item below).

## What the tracker is for

- **Minimal scope.** It does one thing: notify Abhinav when a relevant posting appears.
  No dashboards, no applications, no extra features unless asked.
- **Roles:** spring weeks, insight programmes, first-year programmes, and summer
  internships that **explicitly** say first-year students can apply. Spring weeks and
  insight programmes count unless the posting restricts them to later years.
  Placements and graduate roles are defined in `filters.yaml` but switched off.
- **Fields:** software engineering, ML/AI, quant/trading, cybersecurity, data.
- **Locations:** UK (on-site or remote); US remote only; India only for companies tagged
  big tech, VC-backed or YC. Unclear locations pass and are labelled "unclear".
- **Industries:** big tech, AI labs, quant, finance, fintech, cybersecurity, startups.
  **No consulting, no Big Four.**
- **FAANG+ 🔥** is exactly the `faang_plus` list in `config/profiles/abhinav/profile.yaml`
  (23 companies). FAANG+ roles are marked 🔥, listed first and sent at priority 5.
- Filters, eligibility rules and the FAANG+ list live in `config/profiles/abhinav/`.
  Changing any of them is a MUST-stop item.

## Access rules (never relaxed)

- **Obey robots.txt and site terms.** A path robots.txt disallows is never requested.
- **Never work around a block, a challenge or an explicit disallow**: no alternate hosts
  or mirrors, no proxies, no headless browsers, no spoofed user agents, no rotating IPs.
  A refused source is paused or marked `unsupported` (DESIGN.md decisions 10, 12, 26).
- **Never access Trackr, Gradcracker or Bright Network**, not even to look.
  LinkedIn, Indeed and Glassdoor are refused in code.
- **Per-host rate limits** (`settings.yaml`, `http`): requests to one host are spaced,
  Crawl-delay is honoured, a 429 backs the host off across runs. robots.txt is cached in
  `state/hosts.json` for 24 hours and kept through a 429.
- **Hand probes** (checking a new site) follow the same rules: read robots.txt first, no
  `curl -L` before a site's rules are known, the tracker's User-Agent, as few requests as
  possible.
- **Workday** is paused under the agreed plan (DESIGN.md section 5, Pauses): nothing until
  2026-11-18 17:00 UTC, then one probe of one tenant every 6 hours with its cached
  robots.txt; if a probe succeeds, polling resumes at most every 3 hours with one page per
  search; if still challenged on 2026-11-23 17:00 UTC, Workday is marked unsupported.
  Anything beyond this plan is a MUST-stop item.

## Secrets and money

- **Never print, log or commit a secret**: ntfy topics (`NTFY_TOPIC_*`,
  `NTFY_HEALTH_TOPIC`), `HEALTHCHECK_URL`, tokens. Never put the owner's email or
  personal details in a request to an outside service.
- **Nothing that costs money**: no paid services, no paid tiers, no usage credits, no
  switching to a credits-billed model. When a usage limit is reached, wait for the reset,
  then continue.

## How changes are made

- Python 3.12, `.venv`. Tests: `.venv/bin/python -m pytest -q`. Lint:
  `.venv/bin/ruff check src tests` and `.venv/bin/ruff format --check src tests`.
- Every behaviour change has tests and an updated DESIGN.md (section text, and a numbered
  decision in section 14 for anything a future reader would ask "why?" about).
- Commits end with the attribution lines the session gives. Commit on a branch; `main`
  changes only through the safe merge below.

### Dry run

Against a copy of production state, so nothing real is changed or sent:

```sh
git fetch origin
DRY=$(mktemp -d) && git archive origin/main state | tar -x -C "$DRY"
PYTHONPATH=src .venv/bin/python -m jobtracker run --dry-run --state-dir "$DRY/state" 2>&1 | tee "$DRY/dry-run.log"
```

Read from the log: every `notification_sent` (what would alert, with its ✅ reason and 🔥),
`run_summary` (sources ok/failed/paused, `health_alert_sent`), any `error` level line, and
the run time (first to last timestamp).

### Review

Every change is reviewed by the `reviewer` subagent (`.claude/agents/reviewer.md`) before
it is merged. The builder never approves its own work: no merge without an APPROVE from
the reviewer, given after it has seen the final diff, the test run and the dry-run log.

### Safe merge

1. `gh workflow disable poll.yml`, and wait until no `poll.yml` run is in progress.
2. `git fetch origin && git rebase origin/main` (the bot's commits touch only `state/`
   and `README.md`; a conflict elsewhere means stop).
3. Run tests and ruff again on the rebased branch.
4. `git push origin HEAD:main` (fast-forward only; never force-push `main`).
5. `gh workflow enable poll.yml`, then `gh workflow run poll.yml -f source=manual`.
6. Verify that run: it succeeds, its run time is well under the 12-minute timeout,
   `health_alert_sent` is false (unless the change is meant to alert), and the only
   notifications are the ones the dry run predicted.

If the workflow is disabled and the merge cannot go ahead, re-enable it before doing
anything else: polling must not stay off.

## Autonomy

Merging **without** asking is allowed only when all of these hold: tests and ruff pass,
the reviewer APPROVES, the dry run shows no errors and no unexpected alerts, and the
change touches nothing on the MUST-stop list.

**MUST stop and push-notify** for:

- a new source type (a new ATS adapter or custom site), or any site whose robots.txt or
  terms are unclear;
- anything involving a block or challenge beyond the agreed Workday plan;
- changing filters, eligibility rules or the FAANG+ list;
- deleting data (state, jobs, history, branches, artifacts);
- anything that costs money;
- anything irreversible (force-push, deleting a repo or workflow, rewriting history,
  changing secrets).

When stopped: write the question to `DECISIONS_NEEDED.md` (the options and a
recommendation), push-notify, and carry on with other work that is not blocked.

Record everything done without asking in `CHANGELOG.md`: what, why, and the evidence
(tests, reviewer verdict, run id). Push-notify only for MUST-stop items, plus one summary
when a backlog is finished.
