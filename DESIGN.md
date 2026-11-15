# Job Alert Tracker — Design

Status: **approved 2026-11-15**. Stages 1 and 2 are built; sections below are kept in step with the code.

## 1. Goals and constraints

Poll company career pages and job boards every 15 minutes, detect new postings that match
config-driven filters, and push each one to a phone through ntfy.

| Constraint | Consequence for the design |
|---|---|
| £0 running cost | GitHub Actions on a public repo (free minutes), ntfy.sh public server, no paid APIs, no LLM calls |
| No database | State is JSON files committed back to the repo by the workflow |
| Instant alerts, no digests | One push per new job, sent in the same run that discovers it |
| One broken source must never fail the run | Every source runs inside its own error boundary and reports a result object |
| Explainable in an interview | Small modules, plain dataclasses, no framework, no clever abstractions |

**What "every 15 minutes" really means.** GitHub's scheduler is best-effort: runs are commonly
5–20 minutes late under load and are occasionally skipped. Realistic alert latency is 15–40
minutes after a job is posted. Nothing free does better. To reduce delay, the cron uses an
offset (`7,22,37,52 * * * *`) rather than `*/15`, because runs scheduled on the hour and
quarter-hour queue behind everybody else's. Same frequency, less contention.

## 2. Architecture

One run is a straight pipeline. Each step is a pure function where possible, so each can be
tested without the network.

```
config/*.yaml ──► load + validate
                      │
                      ▼
            ┌─ for each company/source (isolated, throttled per host) ─┐
            │   adapter.fetch()  ──►  list[Job]  or  SourceError       │
            └───────────────────────────────────────────────────────────┘
                      │
                      ▼
              filters.classify(job)      role type, fields, region, remote → match / no match
                      │
                      ▼
              state.reconcile(matches)   new / still open / reposted / missing → closed
                      │
              ┌───────┼────────────────┐
              ▼       ▼                ▼
       notify jobs   health alerts   render README.md
              └───────┼────────────────┘
                      ▼
          write state/*.json  →  git commit + push (only if something changed)
```

### Repository layout

```
config/
  settings.yaml        run-level knobs (timeouts, batch threshold, delays)
  filters.yaml         role types, fields, locations: keyword rules
  companies.yaml       company list: ATS type, board id, tags, tier
src/jobtracker/
  __main__.py          entry point: python -m jobtracker
  pipeline.py          orchestrates one run (the diagram above)
  models.py            Company, Job, SourceResult dataclasses
  config.py            YAML loading + validation
  http.py              shared HTTP client: User-Agent, timeouts, retries, per-host delay
  text.py              text normalisation shared by filters and dedupe
  filters.py           classification and matching
  state.py             StoredJob, SourceState; load/save the JSON state files
  dedupe.py            reconcile a run with state: new, repost, closed
  notify.py            ntfy job alerts, batching
  health.py            source health tracking + health alerts
  readme.py            README table generation
  log.py               structured (JSON lines) logging
  sources/
    __init__.py        adapter registry
    common.py          date / list parsing helpers shared by adapters
    greenhouse.py  lever.py  ashby.py  smartrecruiters.py  workable.py
    workday.py         one generic adapter, configured per company
    custom/            one module per dream-tier company with its own careers site
    boards/            grad boards / community repos (stage 4, only if permitted)
state/
  jobs.json            every matching posting seen, open or recently closed
  sources.json         per-source health and baseline flags
tests/
  fixtures/<source>/   recorded responses
  test_<module>.py
.github/workflows/
  poll.yml             the 15-minute cron
  ci.yml               pytest on push / PR
  keepalive.yml        prevents the 60-day auto-disable
```

### Dependencies

`httpx` (HTTP), `PyYAML` (config), `pytest` (tests). `selectolax` is added only if a custom
scraper has to parse HTML. Config validation is hand-written in `config.py` rather than using
pydantic: it is about 100 lines, gives clearer error messages, and is one less thing to
explain. No headless browser is planned; if a dream-tier site cannot be read without one, it
is marked `unsupported` and reported to you rather than adding Playwright quietly.

## 3. Data model

### Job (normalised, what every adapter returns)

```python
@dataclass(frozen=True)
class Job:
    source: str  # "greenhouse", "workday", "custom.amazon", ...
    company: str  # company key from companies.yaml, e.g. "jane-street"
    job_id: str  # the source's own ID, as a string
    title: str
    locations: tuple[str, ...]  # raw location strings, as published
    url: str  # apply / posting URL
    department: str | None  # team / department / category when the source provides it
    posted_at: date | None  # as published by the source; None if not provided
    remote_hint: bool | None  # explicit remote flag from the source, when there is one
```

Adapters do no filtering and no interpretation. They translate one source's shape into `Job`
and nothing else, which keeps them short and makes fixture tests exact.

### state/jobs.json

Only **matching** jobs are stored. Keyed by primary key, sorted, indented, so diffs are
readable and stable.

```json
{
  "version": 1,
  "jobs": {
    "greenhouse:jane-street:7654321": {
      "company": "jane-street",
      "title": "Spring Week 2027 – Trading",
      "locations": ["London, UK"],
      "url": "https://...",
      "role_type": "spring_week",
      "fields": ["quant"],
      "region": "uk",
      "fingerprint": "jane-street|spring week 2027 trading|london uk",
      "aliases": [],
      "posted_at": "2026-11-14",
      "first_seen": "2026-11-15T09:22:00+00:00",
      "last_seen": "2026-11-15",
      "missing_runs": 0,
      "status": "open",
      "closed_at": null,
      "notified": true
    }
  }
}
```

### state/sources.json

```json
{
  "version": 1,
  "sources": {
    "greenhouse:jane-street": {
      "filters_hash": "9f2c41d0a7b3e685",
      "has_returned_jobs": true,
      "empty_runs": 0,
      "consecutive_failures": 0,
      "failing_since": null,
      "alerted": false,
      "last_alert_at": null
    }
  }
}
```

`filters_hash` is the hash of the filter rules this board was last baselined against; it
replaces a plain `baselined` flag (see Baselines in section 7). Tier and company name are not
stored with a job: they are looked up from `companies.yaml` when needed, so changing a
company's tier takes effect immediately.

### Keeping commit volume sane

A naive design rewrites `last_seen` on every run, producing 96 commits a day (35,000 a year).
Instead:

- `last_seen` is stored at **date** granularity, so an unchanged job changes the file at most
  once a day.
- Per-source values are stored coarsely for the same reason: a "has returned jobs" flag
  rather than a job count, and a failure counter capped at the alert threshold.
- The workflow commits only if `git diff` is non-empty.

Result: one commit a day at minimum (which doubles as repo activity), plus one per run in
which something actually changed.

## 4. Config schema

### config/settings.yaml

```yaml
user_agent: "job-alert-tracker/1.0 (+https://github.com/abhinav-0608/early-careers-tracker)"
http:
  timeout_seconds: 20
  retries: 2                 # on 429 / 5xx / timeouts, with backoff; honours Retry-After
  per_host_delay_seconds: 1.0
  max_workers: 6             # hosts run in parallel; requests to one host are serial
notify:
  batch_threshold: 5         # more than this many new jobs in one run → one summary
  priority: {dream: 5, normal: 3}
state:
  close_after_missing_runs: 2
  repost_window_days: 30     # a closed job reappearing within this window is a repost
  prune_closed_after_days: 90
health:
  fail_after_consecutive: 2
  reminder_hours: 24
  accept_empty_after_runs: 8 # a board that stays empty this long is accepted as empty
```

### config/filters.yaml

Three independent dimensions. A job matches when it gets an enabled role type **and** passes
the field rule **and** passes the location rule.

```yaml
role_types:
  spring_week:
    enabled: true
    ntfy_tags: [cherry_blossom]
    field_match: lenient        # see section 6
    include:
      - "spring week"
      - "spring insight"
      - "spring programme"
      - "spring into"
    exclude: ["spring boot", "spring framework"]
  insight:
    enabled: true
    ntfy_tags: [bulb]
    field_match: lenient
    include: ["insight day", "insight week", "insight programme", "insight event",
              "early insight", "insights programme", "discovery day", "discovery programme",
              "open day", "taster"]
  first_year:
    enabled: true
    ntfy_tags: [seedling]
    field_match: lenient
    include: ["first year", "1st year", "first-year", "freshman", "sophomore",
              "discovery", "explore", "step intern", "underclassmen"]
  summer_internship:
    enabled: false
    field_match: strict
    include: ["summer intern", "summer analyst", "internship", "intern"]
    exclude: ["internal", "international"]
  placement:
    enabled: false
    field_match: strict
    include: ["industrial placement", "placement year", "12 month", "year in industry",
              "sandwich"]
  graduate:
    enabled: false
    field_match: strict
    include: ["graduate", "new grad", "university grad", "early career", "campus hire"]

global_exclude:               # applied to every role type
  - "senior"  - "staff"  - "principal"  - "director"  - "manager"  - "phd"

fields:
  software:  {include: ["software", "engineer", "developer", "technology", "swe", ...]}
  ml_ai:     {include: ["machine learning", "ml", "ai", "research engineer", ...]}
  quant:     {include: ["quant", "trading", "trader", "strats", "markets", ...]}
  security:  {include: ["security", "cyber", "infosec", ...]}
  data:      {include: ["data scien", "data engineer", "analytics", ...]}
field_exclude: ["legal", "tax", "audit", "human resources", "marketing", "sales", ...]

locations:
  uk:    {match: ["united kingdom", "uk", "london", "edinburgh", ...], allow: [onsite, remote]}
  us:    {match: ["united states", "usa", "new york", ...],            allow: [remote]}
  india: {match: ["india", "bengaluru", "bangalore", "hyderabad", ...],
          allow: [onsite, remote], require_company_tags: [big_tech, vc_backed, yc]}
remote_keywords: ["remote", "work from home", "distributed", "anywhere"]
```

(Lists above are abbreviated; the real file is exhaustive and commented.) The role types are
evaluated in file order and the first match wins, so the specific ones sit above the generic
ones. Enabling a later role type is flipping `enabled: true`. No code changes.

### config/companies.yaml

```yaml
- key: jane-street
  name: Jane Street
  industry: quant
  tags: []
  regions: [uk, us]
  tier: dream            # dream | normal
  ats: greenhouse        # greenhouse | lever | ashby | smartrecruiters | workable |
                         # workday | custom | unsupported
  board: janestreet      # board id in the ATS's API (module name for custom)
  careers_url: https://www.janestreet.com/join-jane-street/
  verified: 2026-11-15   # date the board id last returned live postings
- key: lloyds-banking-group
  name: Lloyds Banking Group
  ats: workday
  tenant: lbg            # https://{tenant}.{host}.myworkdayjobs.com/{site}
  host: wd3
  sites: [LBG_Careers, Undergraduate_careers, Graduate_careers]
- key: some-company
  name: Some Company
  ats: unsupported
  careers_url: https://...
  note: "Careers site is client-rendered behind bot protection"
```

Workday entries carry `tenant`, `host` (e.g. `wd5`) and a list of `sites` instead of `board`.
One tenant often runs separate sites for experienced hires, campus and graduates, and the
early-careers programmes this tracker exists for live on the campus ones, so a company lists
every site worth polling.

`unsupported` companies stay in the file with a `note` giving the reason (validation rejects
an unsupported entry without one) and are listed in the README, so nothing is dropped
silently. A company whose `ats` has no adapter yet is loaded and validated like any other
and simply not polled.

**Tags** drive the India location rule, so validation rejects any tag outside this set:

| Tag | Meaning |
|---|---|
| `big_tech` | Large public tech company, or a subsidiary of one |
| `vc_backed` | Venture-funded and still private, or listed within about the last two years |
| `yc` | Went through Y Combinator (kept after an IPO) |

**How the list was built (stage 2).** Each candidate was probed against the public endpoints
below, and a board id was accepted only if it returned live postings that were recognisably
that company's (several plausible ids belong to an unrelated firm of the same name). Workday
tenants were resolved from their own `robots.txt`, which lists the tenant's public sites.
Every Greenhouse, Lever and Ashby entry was then fetched through the real pipeline in a dry
run. `verified` records the date; an entry without it carries a note saying why.

## 5. Source adapters

One interface. Each adapter is a module exposing two functions, registered by name in
`sources/__init__.py`:

```python
def fetch(company: Company, http: HttpClient) -> list[Job]: ...
def parse(company: Company, payload) -> list[Job]: ...
```

`fetch` makes one HTTP call (or a short pagination loop) and `parse` is a pure translation. Tests feed recorded fixtures into `parse`, and use
`httpx.MockTransport` for `fetch`, so no test touches the network.

| Source | Endpoint (public, unauthenticated) | Notes |
|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{board}/jobs` | Whole board in one response. `?content=true` is not used: it adds departments but makes the response ~20x larger, so Greenhouse jobs are classified from the title alone |
| Lever | `GET api.lever.co/v0/postings/{board}?mode=json` | Whole board in one response |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{board}` | Remote is read from `workplaceType`; `isRemote` is unreliable (true on hundreds of hybrid jobs) |
| SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/{id}/postings` | Paginated (`offset`/`limit`) |
| Workable | `GET apply.workable.com/api/v1/widget/accounts/{id}` | Whole board in one response |
| Workday | `POST {tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | Paginated, 20 per page; `searchText` narrows large tenants |
| Custom | One module per company | Plain HTTP/JSON endpoints the site's own front end uses |
| Boards | One module per board | Stage 4, only after a terms and robots.txt report |

Greenhouse, Lever and Ashby were verified against live boards on 2026-11-15 and their test
fixtures are trimmed copies of those responses. The remaining endpoints are from memory of
the public documentation and are verified the same way in the stage that builds them.

**Workday and robots.txt.** Each tenant publishes a `robots.txt` naming its sites. Where it
disallows a site (Citi, RBC, PIMCO, Zoom, eBay, Palo Alto Networks, Darktrace and a few
others disallow their main one), that company is marked `unsupported` rather than polled.
The JSON endpoint sits on a different path and is not literally covered by the rule, but the
owner's intent is clear enough to respect.

**Workable rate limits.** `apply.workable.com` sits behind a Cloudflare rate limit that
blocked a few hundred requests made about a second apart during stage 2. The handful of
Workable boards must be polled gently (stage 3: a longer per-host delay, and not every run).

**Workday at scale.** Large tenants hold thousands of postings at 20 per page. Fetching all
of them every 15 minutes is rude and slow, so the Workday adapter issues a few targeted
searches (`searchText` values derived from the enabled role types, configurable per company)
and merges results by job ID.

**Custom scrapers (stage 3).** Expected approach per dream-tier company, to be confirmed
then: NVIDIA via the generic Workday adapter; Anthropic via Greenhouse; OpenAI via Ashby;
Palantir via Lever; Amazon via its public `search.json`; Apple, Microsoft, Google, Meta, Tesla
and AMD (iCIMS) need individual investigation. Google DeepMind has left Greenhouse and now
lists its roles on Google Careers, so it is covered by the Google adapter. Meta and Tesla are the most likely to end up
`unsupported` (GraphQL with rotating tokens, and aggressive bot protection respectively). I
will report what is and is not feasible rather than force it.

**Politeness, enforced in `http.py` so no adapter can skip it:**

- Honest User-Agent naming the project and linking to the repo.
- Requests to the same host are serialised with a delay between them. Different hosts run in
  a small thread pool, so ~300 companies finish in a few minutes without hammering anyone.
- `429` and `Retry-After` are honoured. Retries are capped at 2.
- (Stage 3, with the first non-API source.) For custom scrapers and boards, `robots.txt` is fetched and checked with
  `urllib.robotparser` before any request; a disallowed path makes the source fail with a
  health alert instead of fetching.
- (Stage 4.) Per-source `poll_every_minutes` lets boards and scrapers be polled hourly rather than every
  run.
- LinkedIn, Indeed and Glassdoor are refused by hostname in `http.py`, as a hard guard.

## 6. Filtering

`text.normalise()` lowercases, strips accents, turns punctuation and dashes into spaces and
collapses whitespace. Keywords are matched as whole-word phrases against the normalised text,
so `"spring week"` matches `Spring-Week 2027 (Technology)` and `"intern"` does not match
`internal`. A keyword ending in `*` is a prefix match (`program*` covers program, programme,
programmes).

1. **Role type** is decided from the **title** only. Descriptions mention "internship" and
   "graduate" in boilerplate too often to be usable.
2. **Field** is decided from title plus department.
   - `strict` (internships, placements, grad roles): at least one field must match.
   - `lenient` (spring weeks, insight, first-year): pass unless a `field_exclude` term
     matches. These programmes are often titled just "Spring Insight Programme 2027" with no
     discipline, and dropping them would be exactly the false negative you want to avoid.
3. **Location**: each location string is classified into a region and onsite/remote. The job
   passes if any of its locations satisfies a region's `allow` list and tag requirement.
   - A location that cannot be classified at all (empty, "Multiple locations") **passes** and
     is labelled "unclear" in the alert and the README. A known non-target location (e.g.
     Singapore) is dropped.

Role types are checked in file order and the first match wins, **including disabled ones**:
a job that lands on a disabled type is dropped rather than falling through to a later, more
generic type. Otherwise "Industrial Placement Intern" would leak through as an internship
while placements are switched off.

`classify()` returns the decision **and the reason** ("role: no role type matched", "location:
'New York, NY' is onsite, us allows remote only"). `python -m jobtracker explain <company>`
prints these for a company's live jobs so a false negative can be debugged in seconds, and
the tests include a table of real title variants that must match.

One known ambiguity: "spring internship". The contiguous phrase ("Spring Internship
Programme") is how some UK firms title a spring week, so it maps to `spring_week`. US
spring-semester roles are usually titled "Intern, Spring 2027", which does not contain that
phrase and maps to `summer_internship`; any that do match are in-person US roles and fail the
location rule anyway.

## 7. Dedupe and state reconciliation

**Primary key:** `source:company:job_id`.

**Fallback fingerprint:** `normalise(company) | normalise(title) | sorted normalised locations`.
Digits are kept, so "Spring Week 2027" and "Spring Week 2028" are different jobs.

A stored record is **recent** if it is still open, or was closed within
`repost_window_days` (default 30). Only recent records can absorb a posting. For each matching
job in a successful fetch:

1. Primary key (or a recorded alias) belongs to a recent record → the same job. Update
   `last_seen`, reset `missing_runs`, refresh title/URL if edited. If it had been closed,
   reopen it silently.
2. Else, fingerprint equals a recent record's → a **repost**. The record moves to the new
   key, the old key is kept under `aliases`, no alert.
3. Else → **new**. Add with `notified: false`.

A match against a record closed **more than** `repost_window_days` ago is therefore a new job
and is alerted. Spring weeks and similar programmes reuse an identical title every year, and
last year's closed record must not swallow this year's opening. The same rule applies when the
same job ID reappears after the window, because some companies reopen last year's requisition
rather than creating a new one.

For jobs in state that were **not** returned:

- Only considered if that source's fetch succeeded this run. A failed source never closes
  anything.
- `missing_runs` is incremented; the job closes when it reaches 2. This absorbs pagination
  hiccups and brief unpublish/republish edits.
- If the source returned zero jobs after previously returning some, nothing is closed and a
  health alert is raised instead (section 9). If it stays empty for `accept_empty_after_runs`
  runs (about two hours) it is accepted as genuinely empty and its jobs close normally.
- Jobs belonging to a company removed from `companies.yaml` are closed straight away.

Closed jobs leave the README immediately and are pruned from state after
`prune_closed_after_days` (90). Config validation requires that to be at least
`repost_window_days`.

Accepted trade-off: two genuinely separate postings with identical company, title and
location are treated as one. The alternative is repost spam, which is worse.

### Baselines

Each source stores the hash of the filter rules it was last baselined against. A source is
baselined whenever that differs from the current rules, which covers every case with one
mechanism:

- **First run or newly added company** (no stored hash): every matching job is recorded as
  already notified, and one summary is sent: "Baseline: N matching roles currently open" with
  a per-company breakdown.
- **Filter changes** (stored hash differs, for example you enable summer internships): jobs
  that newly match are baselined the same way, with one summary. Without this, flipping one
  flag would fire hundreds of alerts. The open roles are still visible in the README table.
- The hash is only stored after a **successful** fetch, so a company that errors during its
  first run or during a filter change is baselined on its next good fetch rather than
  flooding you.

The hash is taken over the parsed rules, so editing comments or formatting in `filters.yaml`
does not trigger a baseline.

## 8. Notifications

Sent with ntfy's JSON publishing: `POST https://ntfy.sh/` with the topic in the request
**body**. The topic therefore never appears in a URL, so it cannot leak through an exception
message or request log. It also avoids header-encoding problems with "–" in titles.

| Field | Value |
|---|---|
| Title | `Company – Role` |
| Body | location · role type · posted date (or "first seen today" if the source gives none) |
| Click | apply URL |
| Tags | from the role type's `ntfy_tags`, plus `star` for dream tier |
| Priority | dream 5, normal 3 |

**Batching.** If a run has more than 5 new jobs: every dream-tier job still gets its own
alert, and the remaining normal-tier jobs are sent as one summary (count, first few titles,
Click → README table). At 5 or fewer, everything is individual.

**Delivery guarantee: at-least-once.** A job is flagged `notified: true` only after ntfy
returns 2xx. If a send fails, the job stays `notified: false` and is retried on the next run.
If the final git push fails after alerts were sent, the next run may alert again. A duplicate
is preferable to a missed spring week.

ntfy's free tier allows roughly 250 messages a day; batching and health-alert throttling keep
normal usage far below that.

## 9. Failure handling

**Isolation.** Each source runs inside `try/except` in the pipeline and returns a
`SourceResult(status, jobs, error, duration)`. Statuses: `ok`, `error` (HTTP error, bad JSON,
schema change), `timeout`, `empty_regression` (0 jobs after previously returning some),
`blocked` (robots.txt disallow). The process exits 0 whenever the pipeline itself completed,
even if every source failed.

**Health alerts** go to `NTFY_HEALTH_TOPIC` only:

- Raised on a **state change**, not every run: a source alerts after 2 consecutive failures,
  then at most once every 24 hours while it stays broken, then once more on recovery. A
  source broken for a week sends about 8 messages, not 672.
- Multiple sources failing in one run are combined into one message.
- If the whole run crashes (bug, bad config, failed push), a final `if: failure()` workflow
  step sends a health alert with a link to the run. It only fires when the previous run
  succeeded, so a persistent crash sends one alert rather than one every 15 minutes; the
  dead-man's switch below covers a failure that persists.

**Git push conflicts.**

- `concurrency: {group: poll, cancel-in-progress: false}` so runs queue rather than overlap.
- The commit step does `git pull --rebase` then push, with 3 attempts, in case you push a
  config change mid-run.
- `timeout-minutes: 12` so a hung run cannot block the queue for long.

**The 60-day auto-disable.** Three layers, because the failure mode is silence:

1. The poll workflow itself commits at least daily (section 3), which is repo activity.
2. `keepalive.yml` runs weekly and calls the GitHub API to re-enable the poll workflow
   (`PUT /repos/{repo}/actions/workflows/poll.yml/enable`), which resets the timer even if
   commits somehow stop counting.
3. **External dead-man's switch.** Nothing running inside Actions can tell you that Actions
   has stopped. So each successful run pings a healthchecks.io check (free tier); if no ping
   arrives for an hour, healthchecks.io notifies your ntfy health topic directly. This also
   catches GitHub outages and a workflow you disabled by accident. It needs a free account
   and one extra secret (`HEALTHCHECK_URL`). The ping is the last successful step of
   `poll.yml`, so a run that crashes or cannot push state does not ping either. Check
   settings: period 15 minutes, grace 45 minutes.

**Secrets.** `NTFY_TOPIC` and `NTFY_HEALTH_TOPIC` are read from the environment only, never
written to state, logs or README. Because the repo is public: workflow permissions are
limited to `contents: write` (plus `actions: write` for keep-alive), third-party actions are
pinned to commit SHAs, and no workflow uses `pull_request_target`, so a fork's PR can never
read the secrets. Since anyone who knows an ntfy topic can read and post to it, the topics
should be long random strings.

**CI noise.** Pushes made with the workflow's `GITHUB_TOKEN` do not trigger other workflows,
so the state commits do not start CI. `ci.yml` also ignores `state/**` and `README.md`.

## 10. Logging

JSON lines to stdout, one event per line (`event`, `source`, `company`, counts, `duration_ms`).
Each run ends with one summary line per source and one for the run:

```json
{"event":"source_summary","source":"greenhouse","company":"jane-street","status":"ok","fetched":41,"matched":3,"new":1,"closed":0,"duration_ms":412}
{"event":"run_summary","sources_ok":212,"sources_failed":2,"fetched":18344,"matched":57,"new":1,"closed":0,"alerts_sent":1}
```

The same summary is written to the GitHub Actions job summary as a table.

## 11. README generation

Regenerated each run between marker comments, so the hand-written description at the top is
never touched. Open matching roles grouped by region, then role type; columns: company, role,
location, first seen, apply link. Dream-tier rows are marked with ⭐ and bold company names.
A footer lists unsupported companies and the last successful run time (date granularity, to
avoid a commit every run).

## 12. Testing

- **Adapters:** recorded fixture per source → exact expected `Job` list; plus malformed
  payload, empty board and pagination cases.
- **Filters:** a table of real title variants that must match ("Spring Week", "Spring Insight
  Programme", "Early Insight", "Discovery Day", "First Year Programme", "STEP Intern",
  "Explore Microsoft") and near-misses that must not ("Spring Boot Engineer", "Internal
  Audit"). Location rules including US-onsite rejection and the India tag gate.
- **Dedupe/state:** new, repeat, repost under a new ID, edit, disappear → close after 2 runs,
  reopen, failed source closes nothing, empty regression, baseline, filter-change baseline.
- **Notify:** batching threshold, dream tier always individual, failed send is retried.
- **Pipeline:** one end-to-end test with fake sources where one raises, proving isolation.

No live network anywhere in the test suite. `ci.yml` runs pytest and ruff on push and PR.

## 13. Stages

As you specified, stopping after each for review:

0. This document.
1. Core pipeline: Job schema, Greenhouse/Lever/Ashby, filters, dedupe/state, ntfy job and
   health alerts, cron workflow. End-to-end with ~5 companies.
2. Research and verify `companies.yaml`; propose dream-tier picks for the non-big-tech
   industries for you to confirm. (Built: 421 companies, 269 polled today.)
3. SmartRecruiters, Workable, Workday, and custom scrapers for dream-tier companies,
   prioritising those running UK spring weeks and insight programmes. Also investigate
   tal.net (Oleeo) as a generic adapter, since several UK banks (Morgan Stanley, Bank of
   America, Jefferies, Nomura) host their spring weeks there; its robots.txt is checked
   before anything is built.
4. Grad boards and community repos: terms and robots.txt report first, build only what is
   allowed.
5. README table, keep-alive, full tests and CI, hardening.

(Your stage list did not place SmartRecruiters and Workable; I have put them in stage 3.)

## 14. Decisions (confirmed 2026-11-15)

1. **Dead-man's switch:** healthchecks.io, free tier, with a `HEALTHCHECK_URL` secret. Setup
   steps for the account are given when the workflow is built.
2. **Cron:** `7,22,37,52 * * * *`.
3. **Unclear locations** pass the filter and are labelled "unclear".
4. **Enabling a role type later** baselines the existing matches with one summary.
5. **Repo:** public, named `early-careers-tracker`. Nothing is created or pushed without
   asking first.
6. **Dream tier** (confirmed after stage 2). Big tech: Apple, NVIDIA, Microsoft, Google,
   Amazon, Meta, Tesla, AMD, Palantir. AI labs: Anthropic, OpenAI, Google DeepMind. Quant:
   Jane Street, Hudson River Trading, Optiver, G-Research, Citadel. Banks: Goldman Sachs,
   JPMorgan, Morgan Stanley. Fintech: Stripe, Revolut. Everything else is normal tier.
7. **Workday sites disallowed by robots.txt stay unsupported** (Citi, RBC, PIMCO, Franklin
   Templeton, eBay, Zoom, Palo Alto Networks, Darktrace, and BlackRock's early-careers site).
8. **xAI is included** as a normal-tier AI lab, although its board is now named "SpaceXSI"
   and SpaceX itself stays excluded.
