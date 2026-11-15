# Early Careers Tracker

A personal job-alert tracker for early-careers roles in software engineering, ML/AI,
quant/trading, cybersecurity and data: spring weeks, insight programmes and first-year
schemes, with internships, placements and graduate roles defined and ready to switch on.
Every 15 minutes a GitHub Actions workflow polls company career boards (Greenhouse, Lever
and Ashby so far), filters postings with keyword rules held in YAML, dedupes them against
JSON state committed back to this repo, and pushes each new match to my phone through
[ntfy](https://ntfy.sh). There is no database, no server and no paid service; the design
and its trade-offs are written up in [DESIGN.md](DESIGN.md).

**Live table of currently open roles: coming soon.**
