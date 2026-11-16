# Changelog

What was done without asking, under the autonomy rules in CLAUDE.md, and why. Newest
first. Changes the owner approved directly are noted as such.

## 2026-11-16

- **Owner's decisions on #4 and #5** (2026-11-16):
  - JPMorgan: check its own careers-site terms; keep polling only if they do not forbid
    automated access. jpmorgan.com's terms say "you will not use any robot, spider, other
    automatic device or manual process to monitor or copy our web pages, data or the
    content contained herein ... without our prior expressed written permission", and
    no separate careers-site terms were found, so it is at best unclear: **switched
    off** (`ats: unsupported`; no stored JPMorgan roles, so nothing closed) and added to
    the manual email alerts. The other Oracle companies are not added.
  - UBS skipped; UBS, NatWest and BNP Paribas added to the manual email alerts.
- **Qualcomm not switched on**: the reviewer blocked the Eightfold change because
  Qualcomm's own terms had not been checked. Its terms page renders only in a browser and
  no readable copy was found, so they are unclear: Qualcomm stays unsupported (and on the
  manual list); the adapter is kept for Microsoft.
- **Tooling** (owner's request): the reviewer agent inherits the session model; no
  setting selects Fable (the Fable entry in the user settings' `modelSettings` was
  removed); worktrees live under `.worktrees/`, gitignored.
- **Microsoft polled hourly, not every run** (autonomous): apply.careers.microsoft.com
  answered 429 twice on 2026-11-16 (run 43924111375 and earlier), so the tracker was
  asking too often. Fewer requests, no change to what is seen beyond up to an hour's delay.
- **Generic Eightfold adapter** (owner's decision 1), used by Microsoft. Netflix and
  Millennium refused the search API (403) and stay unsupported; Amex's careers host is
  unknown; Qualcomm waits on its terms (below).
- **Merged the preference note** (`0e8abe4`): the reviewer blocked the first attempt
  (eligibility widened by accident; a re-read could close a role), approved after the
  fixes. Manual run 43924111375: success in 4 min 36 s, no notifications, no health
  alert; Microsoft 429 and one Anduril timeout, both outside causes.
- **Owner's decisions** (answers to DECISIONS_NEEDED.md, 2026-11-16):
  1. New source types, option B: a generic Eightfold adapter, then a generic Oracle
     Recruiting Cloud adapter, then the own sites of HSBC, UBS, BNP Paribas and NatWest one
     at a time, spring week and insight programmes first. robots.txt and terms checked
     first; reviewer; merged one at a time; stop only if a site's rules are unclear.
  2. Candidates not added: as recommended. CMC Markets is added without asking if
     Workday resumes and its robots.txt allows the jobs API; Arrowstreet, government/
     defence and gaming are skipped.
  3. tal.net place names, option B: a place name in the title labels and groups a role
     with no location (UK instead of "unclear"); it never drops one.
- **Autonomous operation set up** (owner's request): CLAUDE.md (standing rules),
  `.claude/agents/reviewer.md` (pre-merge reviewer), this file and DECISIONS_NEEDED.md.
- **Already done before this, approved by the owner directly** (merged as `e304870`,
  manual run 43914870792: 5 min 30 s, no health alert, one baseline summary of 3 roles):
  first-run description budget (400 in the run eligibility rules change); Workday pause
  (48 h, then a probe every 6 h, resume at most every 3 h with small searches, unsupported
  after 7 days); health alerts grouped by platform and cause, at most once a day; "Set up
  manual email alerts for these" at the top of GAP_REPORT.md.
- **Preference note beside ✅** (owner approved: "Yes, commit and merge as before"):
  "✅ All years welcome · prefers penultimate/final" when an eligible posting names the
  later years it would rather have.
