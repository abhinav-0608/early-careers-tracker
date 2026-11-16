# Job Alert Tracker — Design

Status: **approved 2026-11-15**. Stages 1 and 2 are built, the tracker is profile-ready
(section 2a), and stage 3 is built, including stage 3b (section 13); sections below are
kept in step with the code.

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
            │  dedupe.reconcile         against the profile's own state:      │
            │                           new / open / reposted / closed        │
            │  notify                   to the profile's own ntfy topic       │
            │  save                     state/profiles/<key>.json             │
            └─────────────────────────────────────────────────────────────────┘
                      │
                      ▼
          write state/sources.json, render README.md
                      →  git commit + push (only if something changed)
```

### Repository layout

```
config/
  settings.yaml        run-level knobs (timeouts, batch threshold, delays), shared
  companies.yaml       the one company list, shared: ATS type, board id, industry, tags
  profiles/
    <key>/             one folder per person; the folder name is the profile key
      profile.yaml     company scope and dream-tier list
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
  state.py             StoredJob, ProfileState, SourceState; load/save the JSON state files
  dedupe.py            reconcile a run with one profile's state: new, repost, closed
  notify.py            ntfy job alerts (per profile topic), batching
  health.py            source health tracking + health alerts
  readme.py            README table generation
  log.py               structured (JSON lines) logging
  sources/
    __init__.py        adapter registry
    common.py          date / list parsing helpers shared by adapters
    greenhouse.py  lever.py  ashby.py  smartrecruiters.py  workable.py
    workday.py         one generic adapter, configured per company
    talnet.py          one generic adapter for tal.net (Oleeo) campus boards
    custom/            one module per company with its own careers site
      amazon.py  amd.py  apple.py  google.py  jpmorgan.py  microsoft.py
    boards/            grad boards / community repos (stage 4, only if permitted)
state/
  sources.json         per-board health, shared
  hosts.json           the HTTP client's memory: each host's robots.txt rules (kept a
                       day) and any back-off a host asked for with a 429
  profiles/<key>.json  one profile's memory: its matching postings (open or recently
                       closed, with notified flags) and its per-board baselines
tests/
  fixtures/<source>/   recorded responses
  test_<module>.py
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
explain. No headless browser is planned; if a dream-tier site cannot be read without one, it
is marked `unsupported` and reported to you rather than adding Playwright quietly.

## 2a. Profiles

A profile is one person's view of the tracker. Profiles **share** the company list and the
jobs fetched from it, and nothing else:

| Shared (one copy) | Per profile |
|---|---|
| `companies.yaml`, `settings.yaml` | `filters.yaml`: role types, fields, locations |
| Fetching: each company once per run | Company scope: industries and/or named companies |
| Board health and health alerts | Dream-tier list |
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
proper health alert for a skipped profile is part of stage 6; until then there is only one
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
```

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
      "notified": true
    }
  }
}
```

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

Dream tier and company name are not stored with a job: the name is looked up from
`companies.yaml` and the tier from the profile's `dream` list when needed, so changing either
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
An entry is dropped when a fetch from that host fails (so the file is re-read on the
next run) and after a week. `backoff` is keyed by server (one entry for all of a Workday
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
  priority: {dream: 5, normal: 3}
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

dream:                     # dream tier for this person; everyone else is normal tier
  - jane-street
  - anthropic
```

The folder name is the profile key (lowercase letters, digits, hyphens). It names the state
file and the topic variable (`abhinav` → `NTFY_TOPIC_ABHINAV`). Validation rejects an unknown
industry or company key, a dream company outside the scope, a scope that matches nothing, and
any unknown key (so `dreams:` cannot be silently ignored). A rejected profile is skipped; the
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

The list is shared by every profile and holds nothing personal. Dream tier used to be a
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
the dream list), `morgan-stanley-experienced` its Workday board.

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
| Custom: Microsoft | `GET apply.careers.microsoft.com/api/pcsx/search` | Eightfold; 10 per page; narrowed to one experience level |
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

**Custom scrapers (stage 3).** What was found for each dream-tier company investigated so
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
were already covered. Of the 23 dream-tier companies, 17 are polled and 6 are not
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
    fails (an error status, a timeout, a 429), in case the failure is the rules having
    changed. If the re-read itself fails for a passing reason (5xx, timeout), the last
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
  0 or absent means every run). Today the ATS APIs, tal.net, JPMorgan, Microsoft and small
  Workday sites are polled every run. SmartRecruiters, Workable, Amazon, Apple and AMD
  are held to once an hour as whole sources (their weight is in the tables above), and
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
- A source skipped by its polling interval, or fetched only in part (section 5), counts as
  not fetched here: nothing is marked missing because of it.
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
  flooding you.

The hash is taken over the parsed rules, so editing comments or formatting in `filters.yaml`
does not trigger a baseline. Scope and dream list are not part of it: a scope change is
handled board by board as above, and the dream list only changes priority.

## 8. Notifications

Each profile's alerts go to that profile's own topic and nowhere else. Sent with ntfy's JSON
publishing: `POST https://ntfy.sh/` with the topic in the request **body**. The topic therefore never appears in a URL, so it cannot leak through an exception
message or request log. It also avoids header-encoding problems with "–" in titles.

| Field | Value |
|---|---|
| Title | `Company – Role` |
| Body | location · role type · posted date (or "first seen today" if the source gives none) |
| Click | apply URL |
| Tags | from the role type's `ntfy_tags`, plus `star` if the company is on the profile's dream list |
| Priority | dream 5, normal 3 |

**Batching.** If a run has more than 5 new jobs: every dream-tier job still gets its own
alert, and the remaining normal-tier jobs are sent as one summary (count, first few titles,
Click → README table). At 5 or fewer, everything is individual.

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

- Raised on a **state change**, not every run: a source alerts after 2 consecutive failures
  (3 when the failure is a rate limit, and runs in which a backed-off host was not asked
  count as neither failure nor success; section 5), then at most once every 24 hours while it stays broken, then once more on recovery. A
  source broken for a week sends about 8 messages, not 672.
- Multiple sources failing in one run are combined into one message.
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
{"event":"profile_source_summary","profile":"abhinav","source":"greenhouse:jane-street","matched":3,"new":1,"baselined":0,"reposts":0,"closed":0}
{"event":"profile_summary","profile":"abhinav","status":"ok","companies":269,"matched":57,"new":1,"closed":0,"job_alerts_sent":1}
{"event":"run_summary","sources_ok":336,"sources_failed":2,"sources_not_due":2,"fetched":33900,"profiles_ok":1,"profiles_not_run":0,"matched":57,"new":1,"closed":0}
```

The same summary is written to the GitHub Actions job summary as two tables (profiles,
sources). Profile keys appear in these public logs; topics never do.

## 11. README generation

Regenerated each run between marker comments, so the hand-written description at the top is
never touched. Open matching roles grouped by region, then role type; columns: company, role,
location, first seen, apply link. Dream-tier rows are marked with ⭐ and bold company names.
A footer lists unsupported companies and the last successful run time (date granularity, to
avoid a commit every run). With several profiles the table still has to come from one of
them; which one (the owner's, most likely) is settled when the table is built in stage 5.

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
- **Notify:** batching threshold, dream tier always individual, failed send is retried.
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
  an identical twin, different filters/scope/dream list, profiles sorting before and after,
  a profile added, removed and edited between runs, and every way of being broken (invalid
  config, no topic, a topic that rejects messages, a crash at three different points, a
  corrupt state file). Plus: each company fetched once, opt-in companies, per-profile
  dedupe, retries and dream priority, and the version 1 state migration.

No live network anywhere in the test suite. `ci.yml` runs pytest and ruff on push and PR.

## 13. Stages

As you specified, stopping after each for review:

0. This document.
1. Core pipeline: Job schema, Greenhouse/Lever/Ashby, filters, dedupe/state, ntfy job and
   health alerts, cron workflow. End-to-end with ~5 companies.
2. Research and verify `companies.yaml`; propose dream-tier picks for the non-big-tech
   industries for you to confirm. (Built: 421 companies, 269 polled today.)
3. SmartRecruiters, Workable, Workday, and custom scrapers for dream-tier companies,
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
4. Grad boards and community repos: terms and robots.txt report first, build only what is
   allowed.
5. README table, keep-alive, full tests and CI, hardening.
6. Profiles for friends + setup guide. The system is already profile-ready (section 2a);
   this stage is the onboarding around it:
   - a setup guide: copy a profile folder, pick a topic, add the secret and the one line in
     `poll.yml`, dry-run just that profile (`run --dry-run --profile <key>`);
   - a starting-point `filters.yaml` to copy, and a decision on how friends submit changes
     (pull request, or sending the file);
   - a health alert, throttled like source alerts, when a profile is skipped (invalid config,
     missing topic, crash), so that it is not only a warning on the run page;
   - whether friends get any health or "your profile is broken" messages on their own topic;
   - a check that two profiles have not been given the same topic value;
   - a note for friends that the repo is public: their filters, scope and matched jobs are
     visible to anyone, and the profile key should be a name they are happy to publish.

(Your stage list did not place SmartRecruiters and Workable; I have put them in stage 3.)

## 14. Decisions (confirmed 2026-11-15)

1. **Dead-man's switch:** healthchecks.io, free tier, with a `HEALTHCHECK_URL` secret. Setup
   steps for the account are given when the workflow is built.
2. **Cron:** `7,22,37,52 * * * *`.
3. **Unclear locations** pass the filter and are labelled "unclear".
4. **Enabling a role type later** baselines the existing matches with one summary.
5. **Repo:** public, named `early-careers-tracker`. Nothing is created or pushed without
   asking first.
6. **Dream tier** (confirmed after stage 2; held in `config/profiles/abhinav/profile.yaml`
   since profiles were introduced). Big tech: Apple, NVIDIA, Microsoft, Google,
   Amazon, Meta, Tesla, AMD, Palantir. AI labs: Anthropic, OpenAI, Google DeepMind. Quant:
   Jane Street, Hudson River Trading, Optiver, G-Research, Citadel, Citadel Securities. Banks:
   Goldman Sachs, JPMorgan, Morgan Stanley. Fintech: Stripe, Revolut. Everything else is normal tier.
7. **Workday sites disallowed by robots.txt stay unsupported** (Citi, RBC, PIMCO, Franklin
   Templeton, eBay, Zoom, Palo Alto Networks, Darktrace, and BlackRock's early-careers site).
8. **xAI is included** as a normal-tier AI lab, although its board is now named "SpaceXSI"
   and SpaceX itself stays excluded.
9. **Profiles** (2026-11-15, before stage 3). One shared company list fetched once per run;
   filters, scope, dream list, topic and state per profile; a profile can never change what
   another receives (section 2a). The existing configuration became the first profile,
   `abhinav`. Friend onboarding is stage 6.
10. **robots.txt is binding** (2026-11-15, stage 3). No adapter follows a redirect into a
    disallowed path, every host's Crawl-delay is honoured, and a source that needs a
    headless browser or that robots.txt disallows is marked `unsupported` with the reason
    rather than worked around. This is why Goldman Sachs, Citadel, Citadel Securities and
    Revolut are not polled although all four are on the dream list, and why Google is
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
    early only after a failed fetch from that host.
19. **A 429 is transient** (2026-11-16): not retried blind, the host backed off (per
    `Retry-After`, else 30 minutes doubling to 6 hours), and no health alert until three
    refusals in a row. Prompted by seven Workday tenants (wd12, wd103, wd108) answering
    429 from GitHub's runners from 23:00 UTC on 2026-11-15.
20. **Requests are spaced per shared server, not per tenant** (2026-11-16), for Workday.
