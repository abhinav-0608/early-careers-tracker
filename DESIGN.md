# Job Alert Tracker — Design

Status: **approved 2026-11-15**. Stages 1, 2, 3 (with 3b) and 5 are built, stage 4 was
skipped, and the tracker is profile-ready (section 2a). Stage 5 also brought the
first-year eligibility check (section 6a) and the new alert format (section 8). Sections
below are kept in step with the code.

## 1. Goals and constraints

Poll company career pages and job boards every 15 minutes, detect new postings that match
config-driven filters, and push each one to a phone through ntfy. The tracker serves one or
more **profiles** (people), each with its own filters, companies and ntfy topic; today there
is one, and friends can be added later without affecting it (section 2a).

| Constraint | Consequence for the design |
|---|---|
| £0 running cost | GitHub Actions on a public repo (free minutes), ntfy.sh public server, no paid APIs, no LLM calls |
| No database | State is JSON files committed back to the repo by the workflow |
| Instant alerts, no digests | One push per new job, sent in the same run that discovers it |
| One broken source must never fail the run | Every source runs inside its own error boundary and reports a result object |
| Explainable in an interview | Small modules, plain dataclasses, no framework, no clever abstractions |

**What "every 15 minutes" really means.** GitHub's own scheduler is best-effort, and in
practice far worse than "commonly 5–20 minutes late": in this repo's first 12 hours it fired
**1 of 49** scheduled slots. So the 15-minute beat comes from outside. A free external
scheduler (cron-job.org) calls GitHub's `workflow_dispatch` API every 15 minutes, which starts
a run within seconds; realistic alert latency is then 15–20 minutes after a job is posted.
GitHub's schedule stays in the workflow as a fallback, offset to `7,22,37,52 * * * *` so that
it lands between the external calls. Section 9 covers how the two coexist.

## 2. Architecture

One run is a straight pipeline. Each step is a pure function where possible, so each can be
tested without the network.

```
config/*.yaml ──► load + validate (a broken profile is left out, the rest load)
                      │
                      ▼
            ┌─ for each watched company, ONCE (isolated, throttled per host) ─┐
            │   adapter.fetch()  ──►  list[Job]  or  SourceError             │
            └─────────────────────────────────────────────────────────────────┘
                      │
                      ├──► board health ──► health alerts (one shared health topic)
                      │
                      ▼   shared, read-only from here on
            ┌─ for each profile (isolated) ──────────────────────────────────┐
            │  keep only the companies in the profile's scope                 │
            │  filters.classify(job)    the profile's role types, fields,     │
            │                           regions → match / no match            │
            │  eligibility.assess       may a first year apply? (title, then  │
            │                           the description, read once per post)  │
            │  dedupe.reconcile         against the profile's own state:      │
            │                           new / open / reposted / closed        │
            │  notify                   to the profile's own ntfy topic       │
            │  save                     state/profiles/<key>.json             │
            └─────────────────────────────────────────────────────────────────┘
                      │
                      ▼
          write state/sources.json; README.md table (one chosen profile)
                      →  git commit + push (only if something changed)
```

### Repository layout

```
config/
  settings.yaml        run-level knobs (timeouts, batch threshold, delays), shared
  companies.yaml       the one company list, shared: ATS type, board id, industry, tags
  profiles/
    <key>/             one folder per person; the folder name is the profile key
      profile.yaml     company scope and FAANG+ 🔥 list
      filters.yaml     role types, fields, locations: keyword rules
src/jobtracker/
  __main__.py          entry point: python -m jobtracker
  pipeline.py          orchestrates one run (the diagram above)
  models.py            Company, Job, SourceResult dataclasses
  config.py            YAML loading + validation
  http.py              shared HTTP client: User-Agent, timeouts, retries, per-host delay,
                       robots.txt (cached for a day), checked redirects, 429 back-off
  robots.py            robots.txt parsing and matching (wildcards, crawl delay)
  text.py              text normalisation shared by filters and dedupe
  filters.py           classification and matching
  eligibility.py       may a first-year student apply? (section 6a)
  state.py             StoredJob, ProfileState, SourceState; load/save the JSON state files
  dedupe.py            reconcile a run with one profile's state: new, repost, closed
  notify.py            ntfy job alerts (per profile topic), batching
  health.py            source health tracking + health alerts
  readme.py            README table of open roles (section 11)
  log.py               structured (JSON lines) logging
  sources/
    __init__.py        adapter registry
    common.py          date / list parsing helpers shared by adapters
    descriptions.py    reads one posting's description, for the eligibility check
    greenhouse.py  lever.py  ashby.py  smartrecruiters.py  workable.py
    workday.py         one generic adapter, configured per company
    talnet.py          one generic adapter for tal.net (Oleeo) campus boards
    custom/            one module per company with its own careers site
      amazon.py  amd.py  apple.py  google.py  jpmorgan.py  microsoft.py
state/
  sources.json         per-board health, shared
  hosts.json           the HTTP client's memory: each host's robots.txt rules (kept a
                       day) and any back-off a host asked for with a 429
  profiles/<key>.json  one profile's memory: its matching postings (open or recently
                       closed, with notified flags) and its per-board baselines
tests/
  fixtures/<source>/   recorded responses
  test_<module>.py
GAP_REPORT.md          employers worth adding, and what each would take (stage 4's
                       replacement)
.github/workflows/
  poll.yml             the 15-minute cron
  ci.yml               pytest on push / PR
  keepalive.yml        prevents the 60-day auto-disable
```

### Dependencies

`httpx` (HTTP), `PyYAML` (config), `pytest` (tests). The one adapter that reads HTML
(tal.net) uses the standard library's `html.parser`, so no HTML library was added. Config
validation is hand-written in `config.py` rather than using
pydantic: it is about 100 lines, gives clearer error messages, and is one less thing to
explain. No headless browser is planned; if a FAANG+ 🔥 site cannot be read without one, it
is marked `unsupported` and reported to you rather than adding Playwright quietly.

## 2a. Profiles

A profile is one person's view of the tracker. Profiles **share** the company list and the
jobs fetched from it, and nothing else:

| Shared (one copy) | Per profile |
|---|---|
| `companies.yaml`, `settings.yaml` | `filters.yaml`: role types, fields, locations |
| Fetching: each company once per run | Company scope: industries and/or named companies |
| Board health and health alerts | FAANG+ 🔥 list |
| | ntfy topic (its own secret) |
| | Dedupe and notified-state (its own state file) |

**The rule everything else follows from:** adding, changing, removing or breaking a profile
never changes what any other profile receives. It is enforced by structure rather than by
care:

- **Fetched jobs are read-only.** `Job` is a frozen dataclass and `SourceResult` holds only
  facts about the fetch. A profile has nowhere to write that another profile reads.
- **One state file per profile.** A profile's run loads and saves `state/profiles/<key>.json`
  and never opens another's.
- **One error boundary per profile**, twice: when its config is loaded and when it is
  evaluated. A profile whose YAML is invalid, whose topic secret is missing, whose state file
  is corrupt, or whose evaluation raises is reported and left out of that run; the others
  run exactly as if it were not there. (This is the same idea as "one broken source must
  never fail the run", applied to people.)
- **One topic per profile**, read from an environment variable whose name is derived from
  the key (`NTFY_TOPIC_<KEY>`), so two profiles cannot be pointed at the same variable.

**Scope and opting in.** A profile watches whole industries plus any companies it names.
A company added to the shared list for one person is marked `opt_in: true` (or given an
industry nobody else watches): it is then outside every industry rule, and only profiles
that name it under `scope.companies` see it. That is what "opting in" means. A company that
enters a profile's scope is baselined for that profile (section 7); one that leaves has its
jobs closed for that profile.

**What is not isolated**, because it is shared by nature: the 12-minute run budget, the
per-host politeness delay (alerts to ntfy.sh queue behind each other), ntfy's per-IP rate
limit, and board health (a board is up or down for everyone). A profile watching many extra
companies makes the run longer for all. Also by nature: the repo is public, so every
profile's filters, scope and matched jobs are readable by anyone. Only topics are secret.

**A profile that cannot run** shows as a warning on the Actions run page and in the logs,
and the run still succeeds so that the other profiles' state is committed. The run fails
(exit 1, which triggers the existing failure alert) only when no profile got through. A
proper health alert for a skipped profile is a later idea (section 13a); today there is one
profile, for which "skipped" and "no profile got through" are the same thing.

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
    description: str | None  # only when the list response already carries it
```

`description` is filled only by the adapters whose list response already includes it
(Lever, Ashby, Amazon); it is left out of equality and `repr`. Every other posting's
description is read separately, and only when the eligibility check needs it (section 6a).

Adapters do no filtering and no interpretation. They translate one source's shape into `Job`
and nothing else, which keeps them short and makes fixture tests exact.

### state/profiles/<key>.json

One file per profile. Only jobs **matching that profile's filters** are stored, keyed by
primary key, sorted, indented, so diffs are readable and stable. `baselines` records, per
board, the hash of the profile's filter rules it was last baselined against (see Baselines
in section 7).

```json
{
  "version": 2,
  "baselines": {
    "greenhouse:jane-street": "9f2c41d0a7b3e685"
  },
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
      "notified": true,
      "eligibility": "spring week; the posting does not say otherwise"
    }
  },
  "eligibility_rules": "4b1e09c2d7a3f518",
  "eligibility": {
    "greenhouse:jane-street:7654321": {"status": "eligible", "basis": "spring week; ..."},
    "greenhouse:jane-street:7654400": {"status": "unclear"},
    "workday:nvidia:JR1999":          {"status": "unknown", "tries": 1}
  }
}
```

`eligibility` on a job is why a first year may apply, shown on its alert and in the README
("" when the role type is not checked). The top-level `eligibility` map remembers what each
posting's description settled, so a description is read once per posting rather than once
per run, including for postings that were dropped. It is cleared whenever the eligibility
rules change (`eligibility_rules` is their hash), so a rule change is applied to every open
posting; a verdict is forgotten when its posting leaves the board. Both keys are written
only for a profile that has eligibility rules.

`eligibility_note` on a job, and `note` on a remembered verdict, hold a stated preference
for later years ("All years welcome · prefers penultimate/final", section 6a). The job
field is written only when it is not empty; a verdict settled by a description always has
`note`, "" included, which shows the description was looked at for one.

### state/sources.json

Board health, shared by every profile: a board is fetched once and is up or down for
everyone.

```json
{
  "version": 2,
  "sources": {
    "greenhouse:jane-street": {
      "has_returned_jobs": true,
      "empty_runs": 0,
      "consecutive_failures": 0,
      "failing_since": null,
      "alerted": false,
      "last_alert_at": null
    },
    "custom:google": {
      "...": "...",
      "interval_polled_at": "2026-11-15T12:00:00+00:00"
    }
  }
}
```

`interval_polled_at` is written only for a source that has a polling interval (section 5),
when the interval-limited part of it last ran, to the minute.

Two optional top-level keys sit beside `sources`, written only when they hold something:

```json
{
  "pauses": {
    "workday": {"probed_at": "2026-11-18T17:07:00+00:00", "probe": "wd3...: HTTP 429, a Cloudflare challenge; ..."}
  },
  "alert_groups": {"Workday: blocked (Cloudflare 429)": "2026-11-16T15:01:11+00:00"}
}
```

`pauses` is the progress of a paused kind of source (section 5, Pauses): when it was last
probed and with what answer, and `resumed_at`, `gave_up_at` and `announced` once those
happen. `alert_groups` is when each group of failing sources was last announced (section
9); an entry is dropped once it is a day old, because it then holds nothing back.

FAANG+ 🔥 and company name are not stored with a job: the name is looked up from
`companies.yaml` and the tier from the profile's `faang_plus` list when needed, so changing either
takes effect immediately.

**Migration from version 1.** Before profiles there was a single `state/jobs.json`, and each
board's baseline hash sat in `sources.json` as `filters_hash`. The first run of the profile
code turns those into `state/profiles/abhinav.json` and a hash-free `sources.json`, and
deletes `jobs.json`. Every job keeps its notified flag and every board its baseline hash, and
the filter rules moved without changing (so their hash is the same), which means the first
run after the change alerts exactly what a run before it would have: no re-baseline, no
repeats. A dry run reads the old files the same way without writing anything.

### state/hosts.json

What the HTTP client remembers about hosts between runs (section 5, politeness). It is
a cache: deleting it costs one robots.txt request per host and forgets the back-offs.

```json
{
  "version": 1,
  "robots": {
    "nvidia.wd5.myworkdayjobs.com": {
      "fetched_at": "2026-11-16T00:15:00+00:00",
      "rules": ["Allow: /NVIDIAExternalCareerSite/", "Disallow: /talentcommunity/"],
      "crawl_delay": null
    }
  },
  "backoff": {
    "wd108.myworkdayjobs.com": {"retry_at": "2026-11-16T00:45:00+00:00", "strikes": 1}
  }
}
```

`robots` holds the rules that apply to this tracker's user agent, not the whole file.
An entry is dropped when a fetch from that host fails with an error status or a
timeout (so the file is re-read on the next run), but not on a 429, and after a week. `backoff` is keyed by server (one entry for all of a Workday
server's tenants) and is removed when the server answers again.

### Keeping commit volume sane

A naive design rewrites `last_seen` on every run, producing 96 commits a day (35,000 a year).
Instead:

- `last_seen` is stored at **date** granularity, so an unchanged job changes the file at most
  once a day.
- Per-source values are stored coarsely for the same reason: a "has returned jobs" flag
  rather than a job count, and a failure counter capped at the alert threshold.
- The workflow commits only if `git diff` is non-empty.
- `hosts.json` changes when a robots.txt is re-read (once a day per host; they were all
  first read in the same run, so that is roughly one commit a day) and when a host
  starts or ends a back-off.

- A poll timestamp is kept only for the sources that have a polling interval, never for the
  every-run ones, and all the hourly sources polled in one run share the same minute.

Result: one commit a day at minimum (which doubles as repo activity), plus one per run in
which something actually changed, plus **one an hour** for the hourly sources' timestamp.
That last one is the cost of the polling interval being exact and surviving a missed or
late run: the alternative (deciding by the clock which run of the hour polls them) needs no
state but silently skips an hour whenever that one run is late or lost.

## 4. Config schema

### config/settings.yaml

```yaml
user_agent: "job-alert-tracker/1.0 (+https://github.com/abhinav-0608/early-careers-tracker)"
http:
  timeout_seconds: 20
  retries: 2                 # on 5xx / timeouts, with backoff. A 429 is not retried
                             # blind: see "Rate limits" in section 5
  per_host_delay_seconds: 1.0
  max_workers: 8             # hosts run in parallel; requests to one host are serial
  robots_cache_hours: 24     # how long a host's robots.txt is trusted before a re-read
  host_delay_seconds:        # a longer gap for the hosts named here
    apply.workable.com: 5.0
  hosts_without_robots_txt:  # checked by hand: /robots.txt redirects to an ordinary page
    - careers.smartrecruiters.com
  descriptions_per_run: 60   # most posting descriptions read in one run (section 6a)
  descriptions_when_rules_change: 400  # the same, in the run eligibility rules change
readme:
  profile: abhinav           # whose open roles the README table shows (section 11)
pauses:                      # kinds of source taken off polling (section 5, Pauses)
  workday:
    since: "2026-11-16T17:00:00+00:00"
    reason: "Cloudflare challenge (HTTP 429) on every Workday server ..."
    probe_company: lloyds-banking-group
    probe_after_hours: 48
    probe_every_hours: 6
    resume_every_minutes: 180
    give_up_after_days: 7
polling:
  disabled: []               # kinds of source switched off: not polled, not reported
  every_minutes:             # minimum minutes between polls of a source; 0 = every run
    greenhouse: 0
    lever: 0
    ashby: 0
    talnet: 0
    jpmorgan: 0
    workday: 0               # Workday sites small enough to read in full
    workday_search: 60       # the searches run on Workday sites too large for that
    google: 60
    workable: 60
    smartrecruiters: 60
    amazon: 60
    apple: 60
    amd: 60
    microsoft: 0
notify:
  batch_threshold: 5         # more than this many new jobs in one run → one summary
  priority: {faang_plus: 5, normal: 3}
state:
  close_after_missing_runs: 2
  repost_window_days: 30     # a closed job reappearing within this window is a repost
  prune_closed_after_days: 90
health:
  fail_after_consecutive: 2
  fail_after_rate_limited: 3 # the same threshold when the failure is a 429
  reminder_hours: 24
  accept_empty_after_runs: 8 # a board that stays empty this long is accepted as empty
```

### config/profiles/<key>/profile.yaml

```yaml
name: Abhinav

scope:
  industries: [big_tech, ai_lab, quant, finance, fintech, cybersecurity, startup]
  companies: []            # plus these company keys, whatever their industry;
                           # also how a profile opts in to an opt_in company

faang_plus:                     # FAANG+ 🔥 for this person; everyone else is normal
  - jane-street
  - anthropic
```

The folder name is the profile key (lowercase letters, digits, hyphens). It names the state
file and the topic variable (`abhinav` → `NTFY_TOPIC_ABHINAV`). Validation rejects an unknown
industry or company key, a FAANG+ 🔥 company outside the scope, a scope that matches nothing, and
any unknown key (so `faang:` cannot be silently ignored). A rejected profile is skipped; the
others still run.

### config/profiles/<key>/filters.yaml

Each profile has a complete copy of its own rules, so editing one person's keywords cannot
change another's matches. Three independent dimensions. A job matches when it gets an enabled role type **and** passes
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
    enabled: true               # but only alerts if open to first years (eligibility)
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

eligibility:                  # section 6a
  by_title: [first_year]                  # the title alone means first years
  unless_restricted: [spring_week, insight]  # eligible unless the posting says later years
  explicit_only: [summer_internship]      # alert only if the posting says first years
  first_year: ["first year student*", "first or second year*", "any year of", ...]
  later_years_only: ["penultimate", "final year*", "rising junior*", ...]
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
  opt_in: false          # true = only profiles naming this company watch it (section 2a)
  ats: greenhouse        # greenhouse | lever | ashby | smartrecruiters | workable |
                         # workday | talnet | custom | unsupported
  board: janestreet      # board id in the ATS's API (module name for custom)
  careers_url: https://www.janestreet.com/join-jane-street/
  verified: 2026-11-15   # date the board id last returned live postings
- key: lloyds-banking-group
  name: Lloyds Banking Group
  ats: workday
  tenant: lbg            # https://{tenant}.{host}.myworkdayjobs.com/{site}
  host: wd3
  sites: [LBG_Careers, Undergraduate_careers, Graduate_careers]
- key: morgan-stanley
  name: Morgan Stanley
  ats: talnet
  tenant: morganstanley  # https://{tenant}.tal.net/vx/{board}/candidate/jobboard/vacancy/{site}/adv/
  board: lang-en-GB/mobile-0/appcentre-ext/brand-2
  sites: ["1", "2"]      # job-board numbers: 1 = programmes, 2 = events
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

The list is shared by every profile and holds nothing personal. FAANG+ 🔥 used to be a
`tier:` field here; it now lives in each profile, and validation rejects a leftover `tier:`
rather than ignoring it.

tal.net entries carry `tenant` (the subdomain), `board` (the tenant's path prefix) and
`sites` (job-board numbers, quoted so that YAML reads them as text). A `custom` company
names its module under `sources/custom/` in `board`.

`unsupported` companies stay in the file with a `note` giving the reason (validation rejects
an unsupported entry without one) and are listed in the README, so nothing is dropped
silently. A company whose `ats` has no adapter yet, or a `custom` company whose module is
not written yet, is loaded and validated like any other and simply not polled.

A company has one `ats`. Where a firm's campus roles and experienced roles live on different
systems it gets two entries: `morgan-stanley` is the tal.net campus boards (and the one on
the FAANG+ 🔥 list), `morgan-stanley-experienced` its Workday board.

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
`httpx.MockTransport` for `fetch`, so no test touches the network. (Workday's `parse` also
takes the site name, which is part of each posting's URL.) `sources.adapter_for(company)`
returns the function that fetches a company, or `None` if it cannot be polled.

| Source | Endpoint (public, unauthenticated) | Notes |
|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{board}/jobs` | Whole board in one response. `?content=true` is not used: it adds departments but makes the response ~20x larger, so Greenhouse jobs are classified from the title alone |
| Lever | `GET api.lever.co/v0/postings/{board}?mode=json` | Whole board in one response |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{board}` | Remote is read from `workplaceType`; `isRemote` is unreliable (true on hundreds of hybrid jobs) |
| SmartRecruiters | `GET careers.smartrecruiters.com/{id}` (+ `/api/groups`, `/api/more`) | The HTML job board a candidate sees, grouped by location and paged. **Not** the postings API, which robots.txt disallows (see below). No date, no department |
| Workable | `GET apply.workable.com/api/v1/widget/accounts/{id}` | Whole board in one response. Polled hourly, five seconds between requests (see below) |
| Workday | `POST {tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | Paginated, 20 per page; `searchText` narrows large tenants. No usable date ("Posted 3 Days Ago") |
| tal.net (Oleeo) | `GET {tenant}.tal.net/vx/{board}/candidate/jobboard/vacancy/{site}/adv/` | The HTML job board a candidate sees; 50 per page (`?start=50`). No date. `Crawl-delay: 10` |
| Custom: JPMorgan | `GET jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions` | Oracle Recruiting Cloud, 200 per page, narrowed to one job family |
| Custom: Google | `GET www.google.com/about/careers/applications/jobs/results` | JSON embedded in the page; first page of each search only (robots.txt) |
| Custom: Amazon | `GET www.amazon.jobs/en/search.json` | 100 per page, each role with its full description (about 1 MB a page); narrowed to one category and three countries |
| Custom: Apple | `GET jobs.apple.com/en-gb/search` | JSON embedded in the page, 20 per page (about 320 KB a page); narrowed to one team |
| Eightfold | `GET {host}/api/pcsx/search?domain={board}` | 10 per page; narrowed to the "Intern" experience level. `host` and `board` (the site's domain) per company. Qualcomm; polled hourly |
| Custom: Microsoft | `GET apply.careers.microsoft.com/api/pcsx/search` | Eightfold on Microsoft's own hostname, read by the Eightfold adapter; kept a custom source so its job keys are unchanged |
| Custom: AMD | `GET careers.amd.com/api/jobs` | iCIMS Attract; 100 per page with full descriptions; one category and three keyword searches. `Crawl-delay: 5` |
| Boards | One module per board | Stage 4, only after a terms and robots.txt report |

Greenhouse, Lever, Ashby, SmartRecruiters, Workday, tal.net, JPMorgan, Google, Amazon,
Apple, Microsoft and AMD were verified against the live sites on 2026-11-15, and Workable
on 2026-11-16 (one board, below); their test fixtures are copies of those responses.

**SmartRecruiters: the public board, not the API.** SmartRecruiters publishes postings in
two places, and they are treated differently because their owners have said different
things about them (decision 12):

- **The postings API** (`api.smartrecruiters.com`) is what stage 2 planned to use. That
  host's robots.txt reads, in full, `User-agent: LinkedInBot / Allow: /v1/companies/` then
  `User-agent: * / Disallow: /`. That is an explicit disallow, so the API is never
  requested. (Stage 2 verified the board ids before the client enforced robots.txt.)
- **The public job board** (`careers.smartrecruiters.com/{id}`, where
  `jobs.smartrecruiters.com/{id}` redirects) is the page SmartRecruiters publishes for
  candidates. Nobody has disallowed it: neither job-board host has a robots.txt
  (`jobs.` answers 404; `careers.` redirects the request to the job-board home page,
  which is recorded in `http.hosts_without_robots_txt`). The adapter reads this.

The board lists jobs by location: the page holds the first groups, `/api/groups?page=N`
the rest, and a group of more than nine jobs is completed from `/api/more`. Measured
2026-11-15: Canva 33 jobs in 3 requests, Armis 2 in 1, ServiceNow 815 in about 70
(0.7 MB in all), so the source is polled hourly and one company is capped at 150 requests.

**Only three of the six SmartRecruiters companies can be read this way.** A company can
point its board at its own careers site, and then the board URL only redirects there:

| Company | Board | Outcome |
|---|---|---|
| Canva, ServiceNow, Armis | Served on careers.smartrecruiters.com | Polled |
| Wise | Redirects to `wise.jobs` | Unsupported. wise.jobs's robots.txt allows the site but disallows `/jobs?*`, which is every page of the job search after the first: 12 of 436 roles could be read |
| Legal & General | Redirects to `careers.legalandgeneral.com` | Unsupported for now: a separate site, not investigated, no adapter |
| Check Point | Redirects to `www.checkpoint.com/careers` | Unsupported for now, likewise |

The adapter refuses a page that is not a board, so a company that switches to its own
site later shows up as a failing source instead of silently reading the wrong thing.

**Workable rate limits.** `apply.workable.com` sits behind a rate limit, by IP address,
that blocked a few hundred requests made about a second apart during stage 2: 429 on
every path, `robots.txt` included, with a `Retry-After` of about seven hours. The
adapter was therefore written from Workable's documented response and shipped switched
off (`polling.disabled`). On 2026-11-16, after the time `Retry-After` named had passed,
it was checked: `robots.txt` allows everything (`User-agent: *`, an empty `Disallow`,
and `Content-Signal: search=yes, ai-input=yes, ai-train=no`), and one board (Hugging
Face, 6 roles) came back in exactly the shape the adapter expected, so it was switched
on. The other five board ids are first confirmed by the poll itself; a wrong one shows
up as a failing source. Workable is polled gently: five seconds between requests to
that host (`http.host_delay_seconds`) and at most once an hour, which is six requests
an hour (seven on the day robots.txt is re-read). A 429 backs the host off for as long
as it asks.

**Workday at scale.** Large tenants hold thousands of postings at 20 per page. Fetching all
of them every 15 minutes is rude and slow. A site with up to 200 postings is read in full
(31 of the 64 tenants today). A larger one is searched instead: its first page, then one
`searchText` query per term (`spring`, `insight`, `discovery`, `first year`, `explore`,
`intern`, `placement`, `graduate`), two pages each, merged by posting id. That is about 15
requests per large site, so the searches run at most once an hour (polling intervals,
below).

- The terms are a fixed list in `workday.py`, not derived from a profile's filters as first
  planned: a board is fetched once and shared by every profile, so the fetch cannot depend
  on one person's rules. The list covers every role type a profile can enable.
- The search reads descriptions as well as titles and ranks by relevance, so broad terms
  overflow two pages on most large tenants ("intern" on all 33). What is cut off is logged
  (`workday_search_truncated`). For the role types enabled today this loses little; if
  internships are enabled later, large tenants will need a deeper search or a per-tenant
  facet, and the log says where.

**tal.net (Oleeo).** Morgan Stanley, Bank of America, Jefferies and Nomura run campus
recruiting on tal.net, each on its own subdomain. `robots.txt` (identical on all four)
allows the candidate job boards, disallows `/*/ats/`, `/*/agent/` and `/*/channel/`, and
sets `Crawl-delay: 10`. The adapter reads the board page with `html.parser`: a board shows
each posting as a table row, a tile, or both, and the parser reads whichever is there and
merges by posting id. An Atom feed exists on some tenants but is switched off on Morgan
Stanley and Nomura, so the page is the one source that works for all. Jefferies publishes
no location field (the city is in the title), so its roles are labelled "unclear". A page
with no results section (a login page, an error, a redesign) raises instead of looking like
an empty board. A tenant's root URL redirects to `/ats/login`, which is disallowed; the
adapter never requests the root, and the client would refuse the redirect if it did.

**Custom scrapers (stage 3).** What was found for each FAANG+ 🔥 company investigated so
far (2026-11-15). A site that needs a headless browser, or whose robots.txt disallows the
pages that list roles, is `unsupported` with the reason in its `note`.

| Company | Outcome | Why |
|---|---|---|
| NVIDIA, G-Research | Polled | Generic Workday adapter |
| Morgan Stanley | Polled | Generic tal.net adapter (campus boards) |
| JPMorgan Chase | Polled | Oracle Recruiting Cloud's public API. The site holds about 7,000 postings, so the request uses the site's own "Program Analysts & Associate" job-family filter (about 250 analyst, associate, internship and programme roles): 2 requests per run instead of 35. A role filed under another job family is not seen. Its `/robots.txt` answers 403 to every client, which RFC 9309 treats as "no rules" |
| Google, Google DeepMind | Polled, with a limit | No API; the results page embeds its data as JSON. robots.txt allows the results page but disallows every paginated form of it, so only the first 20 results of a search may be read. The adapter therefore runs narrow searches, newest first: intern-and-apprentice level only; Google in the UK, India and the US (one search each), DeepMind worldwide. A search that overflows is logged (`google_results_truncated`). Graduate roles and other countries are not seen |
| Goldman Sachs | Unsupported | `higher.gs.com/robots.txt` allows only single role pages (`/roles/`) and disallows everything else, including every page that lists roles. The site's data API is on another host with no robots.txt, but as with Workday the owner's intent is clear |
| Citadel, Citadel Securities | Unsupported | Careers pages answer with a Cloudflare browser challenge (403 without JavaScript) |
| Revolut | Unsupported | Careers page answers with a browser security check (403 without JavaScript); robots.txt also disallows `/api/` and any URL with a query string |
| Amazon | Polled, narrowed | `www.amazon.jobs/en/search.json`; robots.txt disallows only `/internal`. The site holds tens of thousands of roles, so the request uses its "Student Programs" business category and the UK, India and the US: 126 roles, 2 requests of about 0.7 MB. A student role filed under another category (some AWS data-centre internships) or in another country is not seen |
| Apple | Polled, narrowed | The search page embeds its results as JSON. The site's "Students: Internships" team, worldwide: 133 listings (115 roles, since a role is listed once per location), 7 pages of 320 KB. Graduate, retail and support programmes are not seen. `jobs.apple.com/robots.txt` redirects to apple.com's not-found page, which means "no robots.txt" (see robots.txt redirects below) |
| Microsoft | Polled, narrowed | The careers site is an Eightfold site whose robots.txt disallows everything except a short list that includes `/api/pcsx` (the search API) and `/careers`. The "Intern" experience level, worldwide: 44 roles, 5 requests of 20 KB. Graduate ("Entry") roles are not seen |
| AMD | Polled, narrowed | `careers.amd.com/api/jobs`; robots.txt allows everything with `Crawl-delay: 5`. Reading all 1,300 roles would be 43 MB, so: the "Student / Intern / Temp" category plus keyword searches for intern, placement and graduate (AMD files many internships under Engineering): 105 roles, 4 requests, 2 MB. Applications go to `careers-amd.icims.com`, which disallows everything and is never requested |
| Meta | Unsupported | `www.metacareers.com/robots.txt` opens with a notice that automated collection of data is prohibited without written permission. Its Disallow lines do not name the job search, but the notice is the owner saying no in so many words (decision 15). Nothing beyond robots.txt was requested |
| Tesla | Unsupported | `www.tesla.com` answers every request, robots.txt included, with an Akamai "Access Denied" (403) unless it comes from a browser |

Counts are from 2026-11-15. Anthropic (Greenhouse), OpenAI (Ashby) and Palantir (Lever)
were already covered. Of the 23 FAANG+ 🔥 companies, 17 are polled and 6 are not
(Goldman Sachs, Citadel, Citadel Securities, Revolut, Meta, Tesla).

**Politeness, enforced in `http.py` so no adapter can skip it:**

- Honest User-Agent naming the project and linking to the repo.
- Requests to the same host are serialised with a delay between them. Companies are queued
  by host and each queue gets one thread of a small pool, so every thread is always working
  on a different host and ~350 companies finish in a few minutes without hammering
  anyone.
- **A shared server counts as one host.** Every Workday tenant has its own hostname
  (`nvidia.wd5.myworkdayjobs.com`) but they are served by a handful of servers (`wd5`),
  and the limits are the server's. So the queue, the one-second gap and the back-off
  below are per server (`wd5.myworkdayjobs.com`), not per tenant; robots.txt is still
  read per hostname. Until 2026-11-16 the gap was per tenant, which let eight tenants of
  one server be fetched at once. Tenants by server: wd1 22, wd5 19, wd3 16, wd108 3,
  wd12 2, wd103 2.
- **Rate limits (429).** Retries are capped at 2 and are for server errors and
  timeouts. A 429 is handled on its own terms:
  - A `Retry-After` of up to 30 seconds is waited out and the request tried again.
  - Otherwise the request is not retried. The host (the server, for Workday) is left
    alone for the rest of the run, and in later runs until the time `Retry-After` named
    (capped at 24 hours) or, when it named none, for 30 minutes, doubling with each
    further 429 in a row up to 6 hours. The back-off is kept in `state/hosts.json`.
  - The source's status is `rate_limited` in the run that got the 429 (every tenant of
    that server, asked or not) and `backing_off` in the runs where it was not asked.
    `backing_off` is not a failure: nothing is recorded, nothing closes. The first
    success clears the back-off.
  - A health alert needs **three** `rate_limited` runs in a row
    (`health.fail_after_rate_limited`), which with the back-off between attempts is at
    least an hour and a half of being refused. Other failures still alert on the second.
  - A 429 carrying `cf-mitigated: challenge` is Cloudflare's bot challenge, not a count
    of requests, and the error text says so. It is backed off the same way; if it
    persists it is the owner refusing automated clients, and the source should be
    marked unsupported (decision 12), not waited out forever.
- A host can be given a longer gap than the default (`http.host_delay_seconds`), for a
  site that publishes no Crawl-delay but is known to rate-limit.
- **robots.txt, for every adapter.** The client reads each host's `robots.txt` before the
  first request to it and checks every request against it (path and query). A
  disallowed path is never requested and the source is reported as `blocked`.
  - **Read once a day, not once a run.** The rules are kept in `state/hosts.json` for
    `http.robots_cache_hours` (24, the longest RFC 9309 suggests) and used without a
    request until then. They are re-read early only after a fetch from that host
    fails (an error status or a timeout), in case the failure is the rules having
    changed. A 429 does not count: it says how often to ask, not what may be asked,
    and re-reading robots.txt from a host that is refusing requests would only be one
    more refused request (changed 2026-11-16, with the Workday pause). If the re-read itself fails for a passing reason (5xx, timeout), the last
    copy is used for up to a week; with no copy, nothing is fetched. This removes about
    80 requests from every run, one per hostname.
  - Parsing is our own (`robots.py`, following RFC 9309) rather than
    `urllib.robotparser`, which ignores the `*` wildcard and would read tal.net's
    `Disallow: /*/ats/` as allowing everything.
  - A 4xx answer for `robots.txt` means the host has no rules. A 5xx or a timeout
    means the rules are unknown, and then nothing is fetched from that host for the run
    (unless a recent copy is held, above). A 429 starts a back-off like any other 429.
  - `robots.txt` is requested as text (`Accept: text/plain`). Until stage 3b it went out
    with the client's default `Accept: application/json`, which Workday refuses with 406,
    and the 406 was read as "no rules": no Workday tenant's file was actually being read,
    so the per-site check described above could never fire. With the header fixed, the
    files of the 57 tenants that answered were read and every configured site is allowed.
  - **robots.txt redirects.** A redirect to another `robots.txt` (the usual bare domain
    to `www`) is followed. A redirect to any other page on the *same* host is not,
    because with that host's rules unread the page cannot be known to be allowed; the
    rules count as unreadable. A redirect to a page on *another* host is followed as an
    ordinary request, checked against that host's own robots.txt, and only a 4xx at the
    end of it is taken to mean "no robots.txt", as RFC 9309 describes. This is what
    `jobs.apple.com` does (to `apple.com/pagenotfound`, a 404). A home page, a login page
    or an error at the end leaves the rules unreadable.
  - A host that has no robots.txt but redirects the request to an ordinary page would
    be unreadable by that rule. One that has been checked by hand can be listed in
    `http.hosts_without_robots_txt`; its redirect is then read as "no rules" and is not
    followed. A real robots.txt appearing on such a host is read and obeyed as usual.
    Today the list is `careers.smartrecruiters.com`.
  - The only request that skips the check is publishing to ntfy, which is not crawling.
- **Redirects are never followed blindly.** The client follows a redirect one hop at a time
  and puts each hop through the same checks as any request, so a redirect into a disallowed
  path (or to a never-scrape host) is refused before it is requested. A redirected POST is
  an error.
- **Crawl-delay is always honoured, per host.** The gap between requests to a host is the
  configured delay or the host's `Crawl-delay`, whichever is longer. A host asking for more
  than 60 seconds is not fetched at all, because a 12-minute run could not honour it.
- **Polling intervals.** `settings.yaml` gives each kind of source a minimum number of
  minutes between polls (`polling.every_minutes`, keyed by ATS name or custom module name;
  0 or absent means every run). Today the ATS APIs, tal.net, JPMorgan and small
  Workday sites are polled every run. SmartRecruiters, Workable, Amazon, Apple, AMD,
  Microsoft and Eightfold (Qualcomm) are held to once an hour as whole sources (Microsoft
  since it answered 429 twice on 2026-11-16 when polled every run) (their weight is in the tables above), and
  so are two more:
  - **Google** (whole source): each request is a 1.3 MB page. When it is not due it gets a
    `not_due` result and no request at all.
  - **Workday searches** (`workday_search`): a tenant is still polled every run, so its
    small sites (usually the campus ones) keep their 15-minute latency, but a large site's
    searches run once an hour. In between, a large site costs one request, its first
    page, which is its 20 newest postings; the fetch is then marked **partial**.
  - A source never polled before is due at once, so a new company is baselined on its
    first run. A source that fails is retried on the next run: only a successful poll
    starts the interval. An interval counts as elapsed two minutes early, so a run that
    starts a few seconds ahead of the hour does not push the source back by a whole run.
  - **Not looking is not the same as not finding.** A `not_due` source is not a failure
    (no health record is touched) and closes nothing. A partial fetch alerts whatever it
    does find, but nothing is counted as missing, and a baseline that is due (new board,
    changed filters) waits for the next complete fetch, so half a board is never
    baselined and the other half alerted.
- **Switching a source off.** A kind of source listed under `polling.disabled` (same names
  as the intervals) is not polled at all: no request, no result, no health record. It
  is how an adapter that is built but not yet proven ships dark (Workable, for a day).
- **Pauses** (`pauses.py`, settings `pauses`). A kind of source whose servers refuse the
  tracker outright, rather than asking it to slow down, is paused, never worked around
  (decision 12: no other hosts, proxies or headless browsers). A pause has four stages:

  | Stage | What is asked of the source |
  |---|---|
  | Paused, for `probe_after_hours` (48) after `since` | Nothing at all |
  | Probing | One request every `probe_every_hours` (6): the first posting of `probe_company`'s first site, using its cached robots.txt even if over a day old (up to a week). A host still backing off from the last 429 waits for the next run |
  | Resumed, after the first probe that succeeds | Polled again, at most every `resume_every_minutes` (180), small sites in full and one page per search term |
  | Given up, if no probe has succeeded `give_up_after_days` (7) after `since` | Nothing; probing stops |

  Throughout, every source of the kind reports `paused`, which like `not_due` is neither a
  failure nor a fetch: no health record changes, nothing closes, no baseline is taken, and
  `explain` will not fetch it either. Resuming and giving up are each announced once on
  the health topic; the second asks for the kind to be marked `unsupported` in
  `companies.yaml` (the workflow commits only state, so that edit is made by hand).
  Deleting the pause from `settings.yaml` resumes normal polling at once.

  **Workday** is paused from 2026-11-16 17:00 UTC: since 07:00 that day every Workday
  server (wd1, wd3, wd5 and the three smaller ones) has answered with a Cloudflare
  challenge, from GitHub's runners and from the development machine alike. Probing starts
  2026-11-18 17:00, with Lloyds Banking Group (wd3, whose robots.txt is cached); if no
  probe has succeeded by 2026-11-23 17:00 the tracker stops and asks for Workday to be
  marked unsupported.
- LinkedIn, Indeed and Glassdoor are refused by hostname in `http.py`, as a hard guard.

## 6. Filtering

`text.normalise()` lowercases, strips accents, turns punctuation and dashes into spaces and
collapses whitespace. Keywords are matched as whole-word phrases against the normalised text,
so `"spring week"` matches `Spring-Week 2027 (Technology)` and `"intern"` does not match
`internal`. A keyword ending in `*` is a prefix match (`program*` covers program, programme,
programmes).

1. **Role type** is decided from the **title** only. Descriptions mention "internship" and
   "graduate" in boilerplate too often to be usable. (They are read for one narrower
   question, who may apply: section 6a.)
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

## 6a. First-year eligibility

Who may apply is the one thing a title often does not say, and for a first-year student it
decides whether an alert is worth anything. Internships are the clearest case: most are for
penultimate years, so switching them on without a check would bury the few open to first
years. The check runs after a job has passed the three filters above, and only for role
types the profile lists under `eligibility` in its `filters.yaml`:

| List | Role types (shipped) | Rule |
|---|---|---|
| `by_title` | first_year | Eligible on the title alone, unless the title names later years |
| `unless_restricted` | spring_week, insight | Eligible unless the posting restricts itself to later years and never offers first years |
| `explicit_only` | summer_internship | Alerted **only** if the title or description says first years (or any year) can apply. A posting that does not say is dropped |

A role type in none of the lists (placements, graduate roles) is not checked and gets no mark.

**How a posting is read.** The title first: a `first_year` phrase in it settles the matter
with no request. Otherwise the description, split into sentences: a `first_year` phrase wins
(unless a negation such as "not" or "unless" comes within eight words before it in the same
sentence), then a `later_years_only` phrase ("penultimate", "final year", "rising junior").
When both appear, first years win: "open to first and penultimate year students" is an offer.

**Where the description comes from** (`sources/descriptions.py`), through the same
`HttpClient` as everything else, so robots.txt, crawl delays and back-off all apply:

1. Lever, Ashby and Amazon already include it in the list response: no request.
2. Greenhouse: its job endpoint, `GET boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}`
   (the list is fetched without `content=true`, section 5).
3. Everything else: the posting's own page. The schema.org `JobPosting` description that
   most job pages embed as JSON-LD if there is one, otherwise the page's visible text.

**Cost and safety.** A description is requested only for a posting that passed a profile's
filters and whose title does not settle it, once per posting (the verdict is kept in the
profile's state, section 3), shared between profiles within a run, and at most
`http.descriptions_per_run` (60) in a run; the rest wait for the next run. In the one run
in which a profile's eligibility rules are new or changed (every open posting is then
checked again), the limit is `http.descriptions_when_rules_change` (400) instead, so the
check finishes in that run and its baseline is a single summary (decision 25); per-host
spacing, robots.txt and back-off still apply to every read. A page robots.txt
disallows, or that is gone (404, 410), counts as saying nothing. A read that fails for a
passing reason is retried on the next two runs; after the third failure the posting counts
as saying nothing. Until then it is `unknown`: not alerted, not dropped for good, and a
board's **baseline waits** while any of its postings is unknown, for the same reason a
baseline waits for a complete fetch (section 7).

**What the alert says.** Every eligible alert carries its reason on its own line:
`✅ First-year eligible (title: "First Year")`, `(description: "Open to first and second
year students")`, or `(spring week; the posting does not say otherwise)`. Dropped postings
are counted (`ineligible` in `profile_source_summary`) and each is logged once, as
`eligibility_dropped` with its reason, in the run its verdict is reached; and
`python -m jobtracker explain` prints the verdict for each match.

**A stated preference for later years.** A posting can welcome first years and still say
whom it would rather have: "penultimate or final year ideal, though all years are welcome
to apply" (Addepar). It stays eligible, and its ✅ reads `✅ All years welcome · prefers
penultimate/final` everywhere it appears (alert, summary line, README); "First years
welcome" replaces "All years welcome" when the offer names first years rather than every
year. A later-years phrase counts as a preference only with preference wording within six
words of it ("ideal", "preferred", "ideally", "especially", "primarily" and a few more,
kept in `eligibility.py` beside the negation words), and not when it is part of the offer
("first or penultimate") or negated. Without that wording, "open to first year and
penultimate year students" is an equal offer and gets a plain ✅. Only a description can
carry a note: a title that settles eligibility means the description is never read.
Verdicts settled by a description before notes existed are read once more to find one.
That read only ever adds a note: the verdict stands whatever the description now says
(empty, disallowed, gone, or edited since), so it cannot close or drop an open role. A
read that keeps failing is given up after three runs, with no note.

**Known limits.** Keyword matching cannot read every phrasing: a posting that offers places
to "students graduating in 2029" says nothing a keyword list can tell is first-year (the
config shows where a graduation year can be added). A description edited after it was read
is not read again unless the rules change. A page that renders its text with JavaScript
and publishes no JSON-LD reads as saying nothing, which drops an `explicit_only` posting:
the cost of the rule falls on internships, where it was meant to.

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
- A source skipped by its polling interval, or fetched only in part (section 5), counts as
  not fetched here: nothing is marked missing because of it.
- A posting whose eligibility is still `unknown` (its description could not be read yet,
  section 6a) is neither alerted nor counted as missing.
- If the source returned zero jobs after previously returning some, nothing is closed and a
  health alert is raised instead (section 9). If it stays empty for `accept_empty_after_runs`
  runs (about two hours) it is accepted as genuinely empty and its jobs close normally.
- Jobs belonging to a company removed from `companies.yaml`, or from the profile's scope,
  are closed straight away (for that profile).

Closed jobs leave the README immediately and are pruned from state after
`prune_closed_after_days` (90). Config validation requires that to be at least
`repost_window_days`.

Accepted trade-off: two genuinely separate postings with identical company, title and
location are treated as one. The alternative is repost spam, which is worse.

### Baselines

All of the above is per profile: each has its own records, so the same posting is new,
reposted or closed for each profile independently.

Each profile stores, per board, the hash of its filter rules that board was last baselined
against. A board is baselined for a profile whenever that differs from the profile's current
rules, which covers every case with one mechanism:

- **First run, newly added company, new profile, or a company entering the profile's scope**
  (no stored hash): every matching job is recorded as already notified, and one summary is
  sent: "Baseline: N matching roles currently open" with a per-company breakdown. A friend
  who joins therefore gets one summary, not an alert for everything already open.
- **A baseline that finds nothing sends nothing.** The board is still baselined (its hash
  is stored), but "0 matching roles" is not announced: adding a company with nothing
  open, as most are on any given day, would otherwise buzz the phone for no reason.
- **Filter changes** (stored hash differs, for example you enable summer internships): jobs
  that newly match are baselined the same way, with one summary. Without this, flipping one
  flag would fire hundreds of alerts. The open roles are still visible in the README table.
- The hash is only stored after a **successful** fetch, so a company that errors during its
  first run or during a filter change is baselined on its next good fetch rather than
  flooding you. Likewise it waits while any of the board's postings has an `unknown`
  eligibility (section 6a): a baseline missing those postings would alert them later.

The hash is taken over the parsed rules, so editing comments or formatting in `filters.yaml`
does not trigger a baseline. Scope and FAANG+ 🔥 list are not part of it: a scope change is
handled board by board as above, and the FAANG+ 🔥 list only changes priority.

## 8. Notifications

Each profile's alerts go to that profile's own topic and nowhere else. Sent with ntfy's JSON
publishing: `POST https://ntfy.sh/` with the topic in the request **body**. The topic therefore never appears in a URL, so it cannot leak through an exception
message or request log. It also avoids header-encoding problems with "–" in titles.

| Field | Value |
|---|---|
| Title | `Company – Role`, starting `🔥 ` for a company on the profile's FAANG+ 🔥 list |
| Body | line 1: location(s); line 2: role type · posted date (or "First seen <date>"); line 3, when checked: `✅ First-year eligible (<why>)` (section 6a) |
| Click | apply URL |
| Tags | from the role type's `ntfy_tags`, plus `fire` if the company is on the profile's FAANG+ 🔥 list |
| Priority | FAANG+ 🔥 5, normal 3 |

**Order.** Within a run, FAANG+ 🔥 roles are sent first, then the rest, each by company name
and title, so the ones that matter most are at the top of the phone's list.

**Batching.** If a run has more than 5 new jobs: every FAANG+ 🔥 job still gets its own
alert, and the remaining normal jobs are sent as one summary, "N new roles". At 5 or fewer,
everything is individual.

**Summaries list every role**, one line each: `🔥 Company — Role · Location · ✅` (the 🔥
and ✅ only where they apply, the ✅ with its preference note when there is one, section
6a; a second location shows as "+1"). That holds for the "N new
roles" summary and for a baseline, which used to give a count per company. ntfy refuses a
message over 4,096 bytes, so a list is cut at 3,800 bytes on a whole line and ends
"+N more"; the click opens the README's open-roles table (`#open-roles`), which has them
all. A summary with no roles in it is never built. Health alerts use the same cut.

**Testing alerts.** `python -m jobtracker test-alert` sends one message to the health topic
(`NTFY_HEALTH_TOPIC`), to check that alerts reach the phone without waiting for a failure.

**Delivery guarantee: at-least-once, per profile.** A job is flagged `notified: true` in a
profile's state only after ntfy returns 2xx for that profile's topic. If a send fails, the job stays `notified: false` and is retried on the next run.
If the final git push fails after alerts were sent, the next run may alert again. A duplicate
is preferable to a missed spring week.

ntfy's free tier allows roughly 250 messages a day; batching and health-alert throttling keep
normal usage far below that.

## 9. Failure handling

**Isolation.** Each source runs inside `try/except` in the pipeline and returns a
`SourceResult(status, jobs, error, duration)`. Statuses: `ok`, `error` (HTTP error, bad JSON,
schema change), `timeout`, `empty_regression` (0 jobs after previously returning some),
`blocked` (robots.txt disallow, or a robots.txt that could not be read), `rate_limited` (the
host answered 429 this run), and two that are not failures: `not_due` (skipped by its
polling interval) and `backing_off` (not asked, because its host answered 429 in an
earlier run and is being left alone). The process exits 0 whenever the pipeline itself completed,
even if every source failed.

**Health alerts** go to `NTFY_HEALTH_TOPIC` only, which belongs to whoever runs the tracker.
Profiles never receive them, and a board watched by several profiles still produces one
alert:

- A source **counts as failing** after 2 consecutive failures (3 when the failure is a
  rate limit, and runs in which a backed-off or paused source was not asked count as
  neither failure nor success; section 5).
- **Grouped by root cause.** Failing sources are grouped by platform and cause, and each
  group is one line of one alert: "Workday: 64 sources blocked (Cloudflare 429)", then
  the first few names and when it started. A single failing source is named:
  "Palantir (Lever): failing (HTTP 500)". Causes: blocked (Cloudflare 429), rate limited
  (429), blocked by robots.txt, timing out, returning no jobs, failing (HTTP nnn).
- **At most once a day per group** (`health.reminder_hours`, 24), however many sources
  join it in between: a source that fails the same way as a group announced in the last
  day is covered by that alert. A group still failing a day later gets one reminder.
  (Before 2026-11-16 alerts were per source, and the Workday outage sent three in a day.)
- Recoveries are grouped by platform ("Workday: 64 sources recovered") and sent when
  they happen. A platform broken for a week sends about 8 messages, not 672, and not
  one per source.
- If the whole run crashes (bug, bad config, failed push), a final `if: failure()` workflow
  step sends a health alert with a link to the run. It only fires when the previous run
  succeeded, so a persistent crash sends one alert rather than one every 15 minutes; the
  dead-man's switch below covers a failure that persists.

**Two triggers, no double alerts.** The external scheduler and GitHub's schedule can fire at
the same moment, and either can arrive while a run is in progress. Three things keep that
safe:

- `concurrency: {group: poll, cancel-in-progress: false}`: runs queue rather than overlap, so
  only one run at a time reads and writes state. GitHub keeps one run waiting at most; a
  newer arrival replaces an older waiting one (shown as cancelled, which is harmless).
- Checkout names the branch (`ref: main`). By default a run checks out the commit that was
  current when it was *triggered*, so a run that waited in the queue would read the state
  from before the run ahead of it and alert the same new jobs again. Naming the branch makes
  every run start from the latest committed state, which already has those jobs marked
  notified.
- A scheduled run first checks whether a dispatched run started in the last 10 minutes and
  succeeded. If so it stops before fetching anything: the schedule is a fallback, not a
  second poll of every board. Dispatched runs (the external scheduler, or by hand) always go
  ahead.

Each run is named after its trigger ("Poll (cron-job.org)", "Poll (GitHub schedule)",
"Poll (manual)"), so the run history shows which trigger is doing the work.

**The external scheduler's token.** cron-job.org holds a fine-grained GitHub token limited to
this one repository with a single permission, Actions: read and write. It can start, cancel
and disable workflow runs here; it cannot read secrets, push code or touch any other repo.
If it expires or is revoked, cron-job.org's calls fail (it emails after repeated failures),
the fallback schedule carries on, and the dead-man's switch below reports any gap over an
hour.

**Git push conflicts.**

- The same concurrency group means two runs never push state at the same moment.
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

**Secrets.** Each profile's topic (`NTFY_TOPIC_<KEY>`) and `NTFY_HEALTH_TOPIC` are read from
the environment only, never written to state, logs or README. `poll.yml` maps one repo secret
to each variable (the original `NTFY_TOPIC` secret feeds `NTFY_TOPIC_ABHINAV`), so adding a
profile means adding one secret and one line there. Because the repo is public: workflow permissions are
limited to `contents: write` (plus `actions: write` for keep-alive), third-party actions are
pinned to commit SHAs, and no workflow uses `pull_request_target`, so a fork's PR can never
read the secrets. Since anyone who knows an ntfy topic can read and post to it, the topics
should be long random strings.

**CI noise.** Pushes made with the workflow's `GITHUB_TOKEN` do not trigger other workflows,
so the state commits do not start CI. `ci.yml` also ignores `state/**` and `README.md`.

## 10. Logging

JSON lines to stdout, one event per line (`event`, `source`, `company`, counts, `duration_ms`).
Each run ends with one line per source (the fetch), one per profile and board where
something matched or closed, one per profile, and one for the run:

```json
{"event":"source_summary","source":"greenhouse","company":"jane-street","status":"ok","fetched":41,"duration_ms":412}
{"event":"profile_source_summary","profile":"abhinav","source":"greenhouse:jane-street","matched":3,"ineligible":2,"new":1,"baselined":0,"reposts":0,"closed":0}
{"event":"profile_summary","profile":"abhinav","status":"ok","companies":269,"matched":57,"ineligible":40,"descriptions_read":3,"new":1,"closed":0,"job_alerts_sent":1}
{"event":"run_summary","sources_ok":336,"sources_failed":2,"sources_not_due":2,"fetched":33900,"profiles_ok":1,"profiles_not_run":0,"matched":57,"new":1,"closed":0}
```

The same summary is written to the GitHub Actions job summary as two tables (profiles,
sources). Profile keys appear in these public logs; topics never do.

## 11. README generation

Regenerated each run (`readme.py`) between two marker comments, so the hand-written
description at the top is never touched; if the markers are missing they are added at the
end. Open matching roles grouped by region, then role type; columns: company, role,
location, first seen, apply link. FAANG+ 🔥 rows come first in each group, marked 🔥 with
bold company names, and a role a first year may apply to ends with ✅ (and its preference
note, section 6a, with a line explaining "prefers …" under the table). A footer gives the
date of the last run (date granularity, to avoid a commit every run) and, collapsed, the
companies in scope that cannot be polled, each with its reason. The heading "Open roles"
is the `#open-roles` anchor that alert summaries link to.

The README is one file and there may be several profiles, so the table shows one of them,
named in `settings.yaml` (`readme.profile`, validated against the profile folders). It is
written after that profile's state is saved, and only then: a dry run writes nothing, and
a README that cannot be written is logged and costs the table one run, never the profile
its alerts. `poll.yml` commits `README.md` together with `state/`.

## 12. Testing

- **Adapters:** recorded fixture per source → exact expected `Job` list; plus malformed
  payload, empty board and pagination cases. tal.net has a fixture for each board layout
  (table, tiles, both) and for an empty board; Workday for a small site read in full and a
  large one searched; Google for a search that overflows its one page.
- **robots.txt and redirects** (`test_robots.py`, `test_http.py`): wildcard and `$`
  matching, group selection, longest-match; the real files from tal.net, Google, Goldman
  Sachs and a Workday tenant; a disallowed path is never requested; **a redirect into a
  disallowed path is refused** (generically, and for tal.net's redirect to `/ats/login`);
  Crawl-delay sets the gap for its own host only; an unreadable `robots.txt` stops the
  host; a `blocked` source is reported and the rest of the run carries on.
- **Filters:** a table of real title variants that must match ("Spring Week", "Spring Insight
  Programme", "Early Insight", "Discovery Day", "First Year Programme", "STEP Intern",
  "Explore Microsoft") and near-misses that must not ("Spring Boot Engineer", "Internal
  Audit"). Location rules including US-onsite rejection and the India tag gate.
- **Dedupe/state:** new, repeat, repost under a new ID, edit, disappear → close after 2 runs,
  reopen, failed source closes nothing, empty regression, baseline, filter-change baseline.
- **Notify:** batching threshold, FAANG+ 🔥 always individual and first, 🔥 in the title, a
  summary or baseline lists every role and is cut on a whole line under ntfy's limit with
  "+N more", no summary of nothing, the ✅ line, failed send is retried.
- **Pipeline:** one end-to-end test with fake sources where one raises, proving isolation.
- **Stage 3b adapters** (`test_workable.py`, `test_custom_bigtech.py`): for each of
  Amazon, Apple, Microsoft and AMD, a trimmed live response mapped field by field, an
  empty result, a malformed one, and the requests `fetch` makes (filters, paging, page
  limit and its log line). Apple: a role listed once per location is merged; the
  robots.txt redirect chain of the live site. AMD: the crawl delay between its requests;
  nothing points at the iCIMS host. Workable: the recorded Hugging Face board, plus a
  hand-written one for several locations and for no locations list; a rate-limited host
  left alone after one request, the five-second gap. The real
  robots.txt files of SmartRecruiters' API, Microsoft, Amazon and AMD.
- **SmartRecruiters** (`test_smartrecruiters.py`): the recorded board, groups page and
  "more" page; every page of groups is read and a long group is completed; a job under
  two locations is merged; the request budget; a board that redirects to the company's
  own site fails instead of being read; the board host is read although its robots.txt
  request redirects, and no other host gets that treatment; the API host is refused.
- **robots.txt cache, rate limits, shared servers** (`test_http.py`, `test_pipeline.py`,
  `test_health.py`, `test_state.py`): rules read in one run are used, and still obeyed,
  in the next, crawl delay included; re-read after a day and after a failed fetch; an
  old copy used when the re-read fails, but not one over a week old. A 429 is not
  retried, backs the host off across runs (doubling, or as long as `Retry-After` says),
  and does not hold up ntfy. Two Workday tenants of one server share its gap and its
  back-off; another server is untouched. A rate-limited source alerts on the third
  refusal, not on runs where it was not asked, closes nothing, and recovers.
- **Switched-off sources and empty baselines** (`test_pipeline.py`): a disabled source
  is not fetched, not reported and raises no health alert, and is baselined when
  switched on; a baseline with no matches sends nothing and later roles still alert.
- **Polling intervals** (`test_polling.py`): an hourly source is polled once in four runs
  and makes no request in the other three; a skipped source is never a failure and never
  closes a job, even one that really has gone; a failing source is retried every run; a
  partial Workday poll alerts a new campus role in the same run and closes nothing; a job
  closes after two *complete* polls; a baseline waits for a complete poll; every-run
  sources add no timestamp to the state file.
- **Profiles** (`test_profiles.py`): the same eight-run story is played on two separate fake
  installations, once with one profile alone and once with others alongside it, and that
  profile's notifications and state file must be identical byte for byte. The "others" cover
  an identical twin, different filters/scope/FAANG+ 🔥 list, profiles sorting before and after,
  a profile added, removed and edited between runs, and every way of being broken (invalid
  config, no topic, a topic that rejects messages, a crash at three different points, a
  corrupt state file). Plus: each company fetched once, opt-in companies, per-profile
  dedupe, retries and FAANG+ 🔥 priority, and the version 1 state migration.

- **First-year eligibility** (`test_eligibility.py`, `test_pipeline.py`): titles that
  settle it; descriptions that offer it to first years, restrict it to later years
  (including a negated offer), or do not say; a preference for later years noted only with
  preference wording close by, not for an equal offer, a phrase inside the offer or a
  negated one; the rule per list; then in the pipeline: an
  internship open to first years alerts with its reason and its description is read once;
  one that does not say is dropped and never re-read; an unreadable description is retried
  on two more runs and then given up on, or alerts if it becomes readable; a first baseline
  waits while a description is unknown; a rule change re-reads; the per-run budget; two
  profiles needing one description cost one request.
- **Descriptions** (`test_descriptions.py`): HTML to text, JSON-LD preferred over visible
  text (escaped or not), no request when the list carried it, Greenhouse's job endpoint,
  a posting page robots.txt disallows is never requested, the Lever, Ashby and Amazon
  fields.
- **README** (`test_readme.py`, `test_pipeline.py`): grouping and order, 🔥 and ✅, closed
  roles left out, unsupported companies in scope only, only the marked part replaced, an
  unchanged table leaves the file alone, written for the chosen profile and never in a
  dry run.
- **Commands** (`test_cli.py`): `explain` shows the eligibility verdict; `test-alert`
  needs the health topic, sends one message and never prints the topic.

No live network anywhere in the test suite. `ci.yml` runs pytest and ruff on push and PR.

## 13. Stages

As you specified, stopping after each for review:

0. This document.
1. Core pipeline: Job schema, Greenhouse/Lever/Ashby, filters, dedupe/state, ntfy job and
   health alerts, cron workflow. End-to-end with ~5 companies.
2. Research and verify `companies.yaml`; propose FAANG+ 🔥 picks for the non-big-tech
   industries for you to confirm. (Built: 421 companies, 269 polled today.)
3. SmartRecruiters, Workable, Workday, and custom scrapers for FAANG+ 🔥 companies,
   prioritising those running UK spring weeks and insight programmes. Also tal.net (Oleeo)
   as a generic adapter, since several UK banks host their spring weeks there.
   - **Built:** robots.txt, checked redirects and crawl delay in the shared client; the
     tal.net adapter (Morgan Stanley, Bank of America, Jefferies, Nomura); the Workday
     adapter (64 tenants); custom adapters for JPMorgan and Google (which covers DeepMind).
     340 companies are polled, up from 269.
   - **Investigated and unsupported:** Goldman Sachs, Citadel, Citadel Securities, Revolut
     (section 5).
   - **Measured** (2026-11-15, 340 companies): a run in which everything is due takes 3 min
     47 s and makes 1,037 requests; the other three runs of each hour take 2 min 53 s and
     582. The floor is Greenhouse: 150 boards on one host, one second apart.
   - **Stage 3b, built:** the SmartRecruiters adapter, reading the public board (Canva,
     ServiceNow, Armis); custom adapters for Amazon, Apple, Microsoft and AMD; the
     Workable adapter (6 companies), switched off for its first day and enabled on
     2026-11-16 once one board had been checked against the live API. **Unsupported:**
     Wise, Legal & General and Check Point (their SmartRecruiters boards redirect to
     their own sites), Meta and Tesla (section 5). 353 companies are polled, up from
     340; 69 are unsupported.
   - **Measured again** (2026-11-15, everything due, from the development machine): 3 min
     45 s and 1,021 requests to 79 hosts before SmartRecruiters was added, of which the
     four new custom sources are 22 (5.7 MB) plus 5 to apple.com for the robots.txt
     redirect. SmartRecruiters adds about 75 requests an hour on its own host (87 s,
     in parallel with the rest). The run is no longer: Greenhouse still sets the floor.
     In that measurement seven Workday tenants answered 429 for robots.txt, so its
     count is about 100 lower than a clean run's.
   - **Measured with per-server spacing** (2026-11-16, everything due, no robots.txt
     cached, development machine): **7 min 55 s**, 1,078 requests of which 77 are
     robots.txt. The run is now as long as the longest Workday server's queue: wd5 256
     requests in 474 s, wd1 258 in 434 s, wd3 165 in 280 s (about 1.8 s a request: the
     one-second gap plus the response). That is the hourly run, when the large-site
     searches are due; it took 3 min 45 s while tenants of one server ran in parallel.
     The other three runs of the hour are not affected: summing each server's tenants
     from a production log gives wd1 152 s, wd3 110 s, wd5 93 s, under Greenhouse's
     158 s. The workflow's timeout is 12 minutes.
   - **The Workday 429s of 2026-11-15** (FactSet, Postman, Expedia on wd108; Salesforce,
     Capital One on wd12; G-Research, IG on wd103; from 23:00 UTC, from GitHub's runners
     and from the development machine alike). These are every configured tenant on
     those three servers and no tenant on any other: the 57 tenants on wd1, wd3 and wd5
     had no 429 at all, although they receive 679 of the 700 Workday requests in a full
     run and the three affected servers 6 to 9 each. The 429 carries
     `cf-mitigated: challenge`, no `Retry-After` and no rate-limit headers, and comes
     and goes (Expedia answered again at 04:15 on the 16th). So this is Cloudflare
     challenging automated clients on those servers, not this tracker's volume, and
     there is no header from which to tell how close any server is to a limit. The
     back-off stops the tracker asking a refusing server every run; it cannot make the
     server answer. If the challenge stays, those seven belong in `unsupported`.
4. Grad boards and community repos: **skipped** (decided 2026-11-16, after the terms and
   robots.txt report). Nothing was built, and nothing will be:
   - **Trackr:** robots.txt allows crawling, but its Terms of Use (11.3a) forbid scraping,
     crawling or extracting content "by automated or systematic means without our written
     permission".
   - **Gradcracker:** robots.txt allows crawling, but its terms say "you're not allowed to
     download, distribute or scrape any part of this website".
   - **Bright Network:** answers even `robots.txt` with a Cloudflare bot challenge.
   - **Higherin** (formerly RateMyPlacement) does not disallow reading its insights
     listing, but of its 66 listings 6 were at companies already polled and none of the
     rest were in software, ML, quant, security or data (mostly accountancy and law).
   - **Community GitHub repos** (SimplifyJobs, vanshb03, workopia, mehek-builds,
     NovaIMario): readable, but every spring week, insight or first-year programme in
     them was at a company already polled.
   For the first three the way to get their listings is their own email alerts.
   Coverage is widened instead by adding employers directly (`GAP_REPORT.md`).
5. Cleanup (built 2026-11-16):
   - The README table of open roles (section 11), committed by `poll.yml` with the state.
   - `python -m jobtracker test-alert`, a test health alert.
   - `keepalive.yml`, re-enabling the poll weekly against GitHub's 60-day inactivity
     rule; and `ci.yml` (tests and ruff), both listed in section 2 since stage 1 but not
     written until now.
   - The one-off version 1 state migration removed, and FAANG+ 🔥 renamed throughout
     (`tier` → `faang_plus` in config, settings and code).
   - First-year eligibility (section 6a), which let internships be switched on.
   - The alert format (section 8): 🔥 titles, FAANG+ 🔥 first, every role listed in
     summaries and baselines within ntfy's size limit, taps opening the README table.
   - `GAP_REPORT.md`: the employers worth adding directly, in place of stage 4.

## 13a. Later ideas (not built, not planned)

The tracker is intentionally minimal: its only job is to notify when a relevant posting
appears. These have come up and are parked here so they are not lost. None is built, and
none should be without a decision to grow the scope.

- **Job detail pages:** a page per role with the full description and history.
- **Web view:** a browsable site of open roles beyond the README table.
- **Applied tracking:** marking roles as saved, applied, interviewing, rejected.
- **Programme calendar:** expected opening dates per company from previous years, with
  a reminder when a programme that opened by now last year has not appeared.
- **ATS drift audit:** a periodic check that each company is still on the ATS and board
  it is configured with, beyond what a failing source already shows.
- **Anomaly detection:** flagging a board whose posting count moves sharply.
- **Age column and archive:** how long each role has been open, and a list of closed ones.
- **Friend profiles UX:** the system is already profile-ready (section 2a); what is not
  built is the onboarding around it: a setup guide, a starter `filters.yaml`, a health
  alert when a profile is skipped, a check that two profiles do not share a topic, and
  a note that the repo, and so each profile's filters and matches, is public.
- **Company request form:** a way for someone to ask for a company to be added.
- **Company discovery:** finding new employers automatically rather than by hand.

## 14. Decisions (confirmed 2026-11-15)

1. **Dead-man's switch:** healthchecks.io, free tier, with a `HEALTHCHECK_URL` secret. Setup
   steps for the account are given when the workflow is built.
2. **Cron:** `7,22,37,52 * * * *`.
3. **Unclear locations** pass the filter and are labelled "unclear".
4. **Enabling a role type later** baselines the existing matches with one summary.
5. **Repo:** public, named `early-careers-tracker`. Nothing is created or pushed without
   asking first.
6. **FAANG+ 🔥** (confirmed after stage 2; held in `config/profiles/abhinav/profile.yaml`
   since profiles were introduced). Big tech: Apple, NVIDIA, Microsoft, Google,
   Amazon, Meta, Tesla, AMD, Palantir. AI labs: Anthropic, OpenAI, Google DeepMind. Quant:
   Jane Street, Hudson River Trading, Optiver, G-Research, Citadel, Citadel Securities. Banks:
   Goldman Sachs, JPMorgan, Morgan Stanley. Fintech: Stripe, Revolut. Everything else is normal tier.
7. **Workday sites disallowed by robots.txt stay unsupported** (Citi, RBC, PIMCO, Franklin
   Templeton, eBay, Zoom, Palo Alto Networks, Darktrace, and BlackRock's early-careers site).
8. **xAI is included** as a normal-tier AI lab, although its board is now named "SpaceXSI"
   and SpaceX itself stays excluded.
9. **Profiles** (2026-11-15, before stage 3). One shared company list fetched once per run;
   filters, scope, FAANG+ 🔥 list, topic and state per profile; a profile can never change what
   another receives (section 2a). The existing configuration became the first profile,
   `abhinav`. Friend onboarding is parked (section 13a).
10. **robots.txt is binding** (2026-11-15, stage 3). No adapter follows a redirect into a
    disallowed path, every host's Crawl-delay is honoured, and a source that needs a
    headless browser or that robots.txt disallows is marked `unsupported` with the reason
    rather than worked around. This is why Goldman Sachs, Citadel, Citadel Securities and
    Revolut are not polled although all four are on the FAANG+ 🔥 list, and why Google is
    limited to the first page of each search.
11. **Polling intervals** (2026-11-15). Per-source, in `settings.yaml`. ATS APIs, tal.net and
    JPMorgan every run; Google and the Workday large-site searches at most every 60
    minutes. A skipped or partial poll is never a failure and never closes a job.
12. **Never work around an explicit disallow** (2026-11-15; restated the same day after
    stage 3b). The rule is about what a site's owner has said, not about which host the
    data comes from:
    - Where the owner has explicitly disallowed something (a robots.txt Disallow, a
      notice in it, a bot challenge), the tracker does not get the same thing another
      way: no headless browser, and no fetching it from a second host that happens to
      have no rules. Goldman Sachs is the example: its careers site disallows every
      page that lists roles, so its data API on another host is not used either.
      Citadel, Citadel Securities and Revolut stay unsupported for the same reason.
    - It is **not** "never use another host". A page that nobody has disallowed may be
      read even when a sibling host is off limits. SmartRecruiters is the example: the
      postings API host disallows crawlers and is never requested, while the public job
      board, the official page the company chose to publish its roles on, has no
      robots.txt and is read. JPMorgan (Oracle's API, no rules) stays polled likewise.
13. **Apple's robots.txt redirect** (2026-11-15). A robots.txt redirect to another host's
    not-found page means "no robots.txt", when that other host's rules allow the hop.
14. **SmartRecruiters is read from its public job board** (2026-11-15), under decision 12.
    This covers Canva, ServiceNow and Armis. Wise, Legal & General and Check Point
    cannot be read that way (their boards redirect to their own sites) and stay
    unsupported until someone decides what to do about each of those sites.
15. **Meta is unsupported, citing the notice in its robots.txt** (2026-11-15), although
    no Disallow line covers its job search.
16. **Workable ships switched off** (2026-11-15): verify one live board, then enable.
    Verified and enabled on 2026-11-16.
17. **No baseline notification for zero matches** (2026-11-15).
18. **robots.txt is cached for 24 hours** in `state/hosts.json` (2026-11-16), re-read
    early only after a failed fetch from that host (not after a 429, since decision 26).
19. **A 429 is transient** (2026-11-16): not retried blind, the host backed off (per
    `Retry-After`, else 30 minutes doubling to 6 hours), and no health alert until three
    refusals in a row. Prompted by seven Workday tenants (wd12, wd103, wd108) answering
    429 from GitHub's runners from 23:00 UTC on 2026-11-15.
20. **Requests are spaced per shared server, not per tenant** (2026-11-16), for Workday.
21. **Grad boards and community repos are not used** (2026-11-16, stage 4 skipped). Trackr
    and Gradcracker forbid scraping in their terms, Bright Network challenges every
    request; their own email alerts are the way to get them. Coverage grows by adding
    employers directly (`GAP_REPORT.md`).
22. **Internships are on, for first years only** (2026-11-16). A posting is checked for who
    may apply (section 6a): spring weeks and insight programmes unless restricted,
    internships only when the posting says first years can apply, and every alert says
    why. Descriptions are read for this alone, once per posting, through the same polite
    client.
23. **Alerts list every role** (2026-11-16): FAANG+ 🔥 marked and first, summaries and
    baselines one line per role within ntfy's 4 KB, the README table one tap away.
24. **The README table shows one profile**, named by `readme.profile` (2026-11-16).
25. **The eligibility check completes in one run** (2026-11-16): in the run in which a
    profile's eligibility rules are new or changed, up to
    `descriptions_when_rules_change` (400) descriptions are read instead of 60, so its
    baseline is one summary, not one per run over several runs. Per-host spacing,
    robots.txt and back-off apply as always.
26. **Workday is paused, not worked around** (2026-11-16), after every Workday server
    answered with a Cloudflare challenge from 07:00 that day: nothing for 48 hours, then
    one probe every 6 hours with a cached robots.txt, resuming at most every 3 hours with
    small searches if a probe succeeds, and marked unsupported if still refused after 7
    days (section 5, Pauses). A 429 no longer discards a host's cached robots.txt.
27. **Health alerts are grouped by platform and cause, at most once a day per group**
    (2026-11-16, section 9).
28. **A ✅ shows a stated preference for later years** (2026-11-16): "✅ All years welcome ·
    prefers penultimate/final" instead of a plain ✅, still eligible. The preference
    words are kept in code, not in `filters.yaml`, so adding them did not change the
    rules hash and re-read every description; only verdicts already settled by a
    description are read once more (section 6a).
29. **A generic Eightfold adapter** (2026-11-16, owner's decision on the gap report):
    `ats: eightfold` with `host` and `board` (the domain), the intern-level search
    Microsoft's adapter already used, which Microsoft now shares. Qualcomm is polled with
    it. Netflix and Millennium answer the search API with 403 and stay unsupported (a
    refusal is not worked around); American Express's careers host could not be found
    (aexp.eightfold.ai answers 404) and stays unsupported. Eightfold's acceptable use
    policy has no clause on automated access.
