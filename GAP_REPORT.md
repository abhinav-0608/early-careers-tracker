# Coverage gap report

## Set up manual email alerts for these

The tracker cannot see these employers' roles, and will not work around what stops it
(DESIGN.md decision 12). Each one's own job alerts, by email, are the way to hear about
them. Sign up on the company's careers site for early careers, internships and insight
or spring programmes in the UK.

| Company | Why the tracker cannot see it |
|---|---|
| Goldman Sachs 🔥 | robots.txt disallows every page that lists roles |
| Meta 🔥 | robots.txt notice prohibits automated collection |
| Citadel 🔥 | Cloudflare browser challenge |
| Citadel Securities 🔥 | Cloudflare browser challenge |
| Revolut 🔥 | Browser security check; robots.txt disallows its API |
| Tesla 🔥 | Akamai "Access Denied" to anything but a browser |
| Arm (early careers) | Its iCIMS early-careers site disallows everything in robots.txt |
| Susquehanna (SIG) | Its iCIMS site disallows everything in robots.txt |
| Bloomberg | Its Avature careers host did not answer at all (twice, 2026-11-16) |
| Barclays, Lloyds Banking Group, Deutsche Bank, BlackRock, NVIDIA 🔥 | **While Workday is paused.** Every Workday server has answered with a Cloudflare challenge since 2026-11-16 07:00 UTC, so Workday polling is paused (settings.yaml, `pauses`). If it is still refused on 2026-11-23 it will be marked unsupported, and these alerts become permanent |

The grad boards whose terms forbid scraping (Trackr, Gradcracker) and the one behind a bot
challenge (Bright Network) also have their own email alerts (section 3).

Written 2026-11-16, in place of stage 4 (DESIGN.md section 13: grad boards and community
repos are not used). The tracker watches 422 companies, all in this profile's scope; 353
are polled and 69 are `unsupported`. This report says where the remaining coverage is, and
what each piece would take. **Nothing here has been built or added to `companies.yaml`.**
Each item needs a decision first.

How it was checked: everything in part 1 comes from `companies.yaml` and DESIGN.md. For
part 2, each candidate host's `robots.txt` was requested **once**, with the tracker's
User-Agent and without following redirects (one redirect to another `robots.txt` was then
followed, as the client does). Nothing else on those hosts was requested, so no board id
or endpoint below has been tried yet.

## 1. Gaps among the companies already listed

### FAANG+ 🔥 companies that cannot be polled (6 of 23)

| Company | Why | Way in |
|---|---|---|
| Goldman Sachs | robots.txt disallows every page that lists roles | None (decision 12). Their own email alerts |
| Meta | robots.txt notice prohibits automated collection | None (decision 15) |
| Citadel, Citadel Securities | Cloudflare browser challenge | None without a headless browser (decision 10) |
| Revolut | Browser security check; robots.txt disallows `/api/` and query strings | None |
| Tesla | Akamai "Access Denied" to non-browsers | None |

These are final under the rules in DESIGN.md. Their own job alerts (email) are the way to
hear about them.

### Polled, but only in part

| Company | What is not seen |
|---|---|
| Google, Google DeepMind | Anything past the first 20 results of each search (robots.txt), graduate roles, countries other than the UK, India and the US |
| JPMorgan Chase | Roles outside the "Program Analysts & Associate" job family |
| Amazon | Student roles outside the "Student Programs" category or the three countries |
| Apple | Graduate, retail and support programmes (only "Students: Internships") |
| Microsoft | Graduate ("Entry") roles |
| All 64 Workday tenants (NVIDIA and G-Research among them, and most UK banks) | Everything. Since 2026-11-16 07:00 UTC every Workday server answers with a Cloudflare challenge; polling is paused for 48 hours, then probed every 6 hours, and Workday is marked unsupported if still refused on 2026-11-23 (DESIGN.md section 5, Pauses) |

### Unsupported companies, by what would unlock them

Of the 63 unsupported companies that are not FAANG+ 🔥, most sit on a handful of
recruiting systems. Two of those systems already have a one-company adapter here, so the
cheapest gains are generalising those.

| System | Companies | robots.txt (checked 2026-11-16) | Work | Verdict |
|---|---|---|---|---|
| **Eightfold** | Netflix, Qualcomm, Millennium, American Express | Netflix, Qualcomm, Millennium: `Disallow: /` but `Allow: /api/pcsx` and `/careers`, the same as Microsoft's. American Express: no robots.txt (404) | **Built 2026-11-16** (DESIGN.md decision 29) | **Qualcomm is now polled.** Netflix and Millennium refuse the search API (403) and stay unsupported; American Express's real careers host is unknown |
| **Oracle Cloud HCM** | Schroders, Lazard, BNY, Oracle (also Uber, Fortinet; hosts not recorded) | No robots.txt on any of the four hosts (404), as with JPMorgan's (403) | Turn `custom/jpmorgan.py` into a generic Oracle Recruiting Cloud adapter (host, site number, optional filter per company) | **Recommended.** 4 to 6 companies, three of them banks or asset managers |
| **iCIMS, newer "globalcareers" hosts** | Atlassian, MSCI | `/jobs/` search allowed; only referral, login, candidate and intro paths are disallowed | A new iCIMS adapter reading the job search pages | Possible; 2 companies |
| iCIMS, older hosts | Arm (early careers), Booking.com, GitHub, Susquehanna (SIG) | `Disallow: /` on every one | None (decision 12) | Stay unsupported. Arm and SIG run early-careers programmes, so their own alerts are worth signing up for |
| Pinpoint | NCC Group | Allowed, apart from a list of individual postings | A small adapter for Pinpoint's public postings | Possible; 1 company. Disallowed postings would be skipped one by one |
| Gem | Groq, Retool | `jobs.gem.com` has no robots.txt (404) | A small adapter for Gem's public job board | Low priority: neither is likely to run first-year programmes |
| Rippling ATS | Rippling | Only `/internal/` disallowed | A small adapter | Low priority, likewise |
| Taleo | Societe Generale, Standard Chartered | `socgen.taleo.net`: no robots.txt (404) | Taleo has no clean public API; the pages are form-driven | Not recommended without looking closer |
| Avature | Bloomberg, Two Sigma | **No answer** from either host, twice (25-second timeout) | None while robots.txt cannot be read: the client fetches nothing from such a host | Stay unsupported for now. Bloomberg runs UK student programmes, so this gap matters |
| SAP SuccessFactors | SAP, Moody's | Not checked | Not checked | Later |
| Phenom | Brevan Howard, Circle | Not checked | Not checked | Later |
| Own sites | HSBC, UBS, BNP Paribas, NatWest, IBM, BAE Systems Digital Intelligence, D. E. Shaw and others | Not checked here | One custom adapter each, as in stage 3b | Worth it only for the banks with UK spring weeks (HSBC, UBS, BNP Paribas, NatWest), one at a time |
| Disallowed Workday sites | Citi, RBC, PIMCO, Franklin Templeton, eBay, Zoom, Palo Alto Networks, Darktrace | Disallowed | None (decision 7) | Stay unsupported |

**Suggested order:** a generic Eightfold adapter (4 companies), then a generic Oracle adapter
(4 to 6), then the HSBC, UBS, BNP Paribas and NatWest sites one at a time, checking each
one's robots.txt and terms first. That would take the unsupported count from 69 to the mid-50s,
and cover most of the banks with UK spring weeks.

## 2. Employers not on the list

The list was built for seven industries (big tech, AI labs, quant, finance, fintech,
cybersecurity, startups) and excludes consulting and the Big Four by decision. Within those
industries the obvious UK early-careers names are already present. The candidates below
are **unverified**: none has been checked for a first-year programme, an ATS or a
robots.txt. Each would be researched the way stage 2 did it before being added.

| Candidate | Industry | Why it might matter |
|---|---|---|
| Aspect Capital, Arrowstreet Capital | quant | London quant firms with student intakes |
| Bank of England | finance | Runs student programmes in London; public body |
| CMC Markets, Plus500 | fintech | London trading platforms with technology teams |
| MathWorks, Imagination Technologies | big_tech | UK engineering employers with student intakes |
| GCHQ / NCSC (CyberFirst), Thales, QinetiQ | cybersecurity (or a new government/defence industry) | CyberFirst is aimed at first-year students in security. Government roles are posted on Civil Service Jobs, which would need its own terms and robots.txt check |
| Sky Betting & Gaming (Flutter), bet365, Entain | none yet (gaming technology) | Large UK software employers; would need a new industry in scope |

## 3. Not pursued

- **Trackr, Gradcracker:** their terms forbid scraping (DESIGN.md decision 21). **Bright
  Network:** bot challenge on every request. Use their email alerts.
- **Higherin, community GitHub repos:** checked in stage 4; they added nothing that is not
  already polled.
