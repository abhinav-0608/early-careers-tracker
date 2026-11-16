# Decisions needed

Open questions that only the owner can settle (CLAUDE.md, MUST-stop items). Each has the
options and a recommendation. Answered questions move to CHANGELOG.md with the answer.

## 4. Oracle's terms: the Oracle adapter, and JPMorgan (opened 2026-11-16) — MUST-stop

**What was found.** Oracle's careers terms say use of Oracle Recruiting is subject to
the Oracle.com Terms of Use, which say: "You agree not to use any robot, spider, scraper
or other automated means to access the Site or any Oracle accounts, computer systems or
networks without Oracle's express written permission" (as reported by a web search;
oracle.com refused a direct read with 403). A customer's careers tenant
(`*.fa.*.oraclecloud.com`) runs on Oracle's systems, so the clause may cover it; it may
equally be governed only by the customer's own careers-site terms. That is unclear,
which under your rules stops the generic Oracle adapter (Schroders, Lazard, BNY, Oracle).

It also bears on a source already polled: **JPMorgan Chase 🔥** is read from
`jpmc.fa.oraclecloud.com` (custom/jpmorgan.py, since 2026-11-15).

**Options**

- A. Skip the Oracle adapter; keep JPMorgan as it is (its robots.txt publishes no rules,
  and its own careers terms were not found to forbid it).
- **B. Skip the Oracle adapter, switch JPMorgan off** (`polling.disabled: [jpmorgan]`,
  reversible in one line) and add JPMorgan to the manual email alert list.
- C. Ask Oracle or JPMorgan for written permission, and decide on the answer.

**Recommendation: B.** It is the reading that keeps to "respect terms" without guessing
who owns the clause. Its cost is one 🔥 source, covered by JPMorgan's own email alerts.
I have changed nothing yet: JPMorgan is still polled until you decide.


## 5. UBS (opened 2026-11-16)

Chosen with HSBC, BNP Paribas and NatWest. HSBC is built; NatWest and BNP Paribas refuse
the tracker outright (bot protection even for robots.txt) and stay unsupported, as your
rules require. UBS is unclear rather than refused:

- Its job board (`jobs.ubs.com`, Kenexa BrassRing) has no robots.txt (404).
- UBS's legal terms say nothing on automated access but allow no use of the material
  "other than personal viewing".
- The board serves results only through a session handshake its JavaScript performs
  (tokens and cookies), which is close to driving a browser.

**Options**

- **A. Skip UBS**; its Tomorrow's Talent spring programme goes on the manual email alert
  list.
- B. Build a BrassRing adapter that performs the session handshake, polling hourly.

**Recommendation: A.** A personal alert is arguably "personal viewing", but the session
handshake is the kind of imitation of a browser the rules steer away from.
