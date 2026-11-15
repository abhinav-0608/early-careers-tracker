# Job Alert Tracker — Design

Status: **approved 2026-11-15**. Stages 1 and 2 are built, and the tracker is profile-ready
(section 2a); sections below are kept in step with the code.

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
  http.py              shared HTTP client: User-Agent, timeouts, retries, per-host delay
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
    custom/            one module per dream-tier company with its own careers site
    boards/            grad boards / community repos (stage 4, only if permitted)
state/
  sources.json         per-board health, shared
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

`httpx` (HTTP), `PyYAML` (config), `pytest` (tests). `selectolax` is added only if a custom
scraper has to parse HTML. Config validation is hand-written in `config.py` rather than using
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
    }
  }
}
```

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

The list is shared by every profile and holds nothing personal. Dream tier used to be a
`tier:` field here; it now lives in each profile, and validation rejects a leftover `tier:`
rather than ignoring it.

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
`blocked` (robots.txt disallow). The process exits 0 whenever the pipeline itself completed,
even if every source failed.

**Health alerts** go to `NTFY_HEALTH_TOPIC` only, which belongs to whoever runs the tracker.
Profiles never receive them, and a board watched by several profiles still produces one
alert:

- Raised on a **state change**, not every run: a source alerts after 2 consecutive failures,
  then at most once every 24 hours while it stays broken, then once more on recovery. A
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
{"event":"run_summary","sources_ok":267,"sources_failed":2,"fetched":18344,"profiles_ok":1,"profiles_not_run":0,"matched":57,"new":1,"closed":0}
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
  payload, empty board and pagination cases.
- **Filters:** a table of real title variants that must match ("Spring Week", "Spring Insight
  Programme", "Early Insight", "Discovery Day", "First Year Programme", "STEP Intern",
  "Explore Microsoft") and near-misses that must not ("Spring Boot Engineer", "Internal
  Audit"). Location rules including US-onsite rejection and the India tag gate.
- **Dedupe/state:** new, repeat, repost under a new ID, edit, disappear → close after 2 runs,
  reopen, failed source closes nothing, empty regression, baseline, filter-change baseline.
- **Notify:** batching threshold, dream tier always individual, failed send is retried.
- **Pipeline:** one end-to-end test with fake sources where one raises, proving isolation.
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
   prioritising those running UK spring weeks and insight programmes. Also investigate
   tal.net (Oleeo) as a generic adapter, since several UK banks (Morgan Stanley, Bank of
   America, Jefferies, Nomura) host their spring weeks there; its robots.txt is checked
   before anything is built.
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
