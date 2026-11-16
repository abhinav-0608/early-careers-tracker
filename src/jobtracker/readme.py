"""The README's table of open roles, regenerated each run.

Only the part between the two marker comments is rewritten, so the hand-written text
above it is never touched. If the markers are missing they are added at the end.

The table comes from one profile (settings: readme.profile), because the repo has one
README; each profile's alerts are unaffected by whose table is shown. Open roles are
grouped by region, then role type, FAANG+ 🔥 companies first within each group. The
footer gives the date of the last run (a date, not a time, so the file changes at most
once a day when no role does) and the companies in scope that cannot be polled.

Notification summaries link here (notify.OPEN_ROLES_ANCHOR), which is why the heading
is "Open roles": GitHub turns it into the anchor #open-roles.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from jobtracker.config import UNSUPPORTED, Config, Profile
from jobtracker.filters import UNCLEAR
from jobtracker.notify import FIRE, TICK
from jobtracker.state import OPEN, ProfileState, StoredJob

START = "<!-- open-roles:start (generated each run; edits here are overwritten) -->"
END = "<!-- open-roles:end -->"
HEADING = "## Open roles"


def open_roles(state: ProfileState, profile: Profile, config: Config, today: date) -> str:
    """The generated section, from the heading to the footer."""
    filters = profile.filters
    jobs = [job for job in state.jobs.values() if job.status == OPEN]
    lines = [HEADING, ""]
    if not jobs:
        lines += ["No matching roles are open right now.", ""]

    region_order = [region.name for region in filters.regions] + [UNCLEAR]
    region_labels = {region.name: region.label for region in filters.regions}
    region_labels[UNCLEAR] = "Location unclear"
    role_order = [role.name for role in filters.role_types]

    for region in sorted({job.region for job in jobs}, key=lambda r: _rank(region_order, r)):
        lines += [f"### {region_labels.get(region, region)}", ""]
        in_region = [job for job in jobs if job.region == region]
        for role_type in sorted(
            {job.role_type for job in in_region}, key=lambda r: _rank(role_order, r)
        ):
            role = filters.role_type(role_type)
            lines += [f"#### {role.label if role else role_type}", ""]
            lines += ["| Company | Role | Location | First seen | |", "|---|---|---|---|---|"]
            rows = [job for job in in_region if job.role_type == role_type]
            for job in sorted(rows, key=lambda job: _order(job, profile, config)):
                lines.append(_row(job, profile, config))
            lines.append("")

    if any(job.eligibility for job in jobs):
        lines += [f"{TICK} the posting says first-year students can apply.", ""]
    lines.append(f"Last updated {today.isoformat()}.")

    unsupported = sorted(
        (company for company in config.companies if company.ats == UNSUPPORTED),
        key=lambda company: company.name.lower(),
    )
    unsupported = [company for company in unsupported if profile.watches(company)]
    if unsupported:
        lines += [
            "",
            "<details>",
            f"<summary>{len(unsupported)} {'company' if len(unsupported) == 1 else 'companies'}"
            " in scope cannot be polled</summary>",
            "",
        ]
        lines += [f"- **{_cell(c.name)}**: {_cell(c.note or 'no reason')}" for c in unsupported]
        lines += ["", "</details>"]
    return "\n".join(lines)


def update(path: Path | str, section: str) -> bool:
    """Put `section` between the markers in the file. Returns True if the file changed."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    block = f"{START}\n{section}\n{END}"
    start, end = text.find(START), text.find(END)
    if start != -1 and end > start:
        new_text = text[:start] + block + text[end + len(END) :]
    else:
        new_text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block + "\n"
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def _row(job: StoredJob, profile: Profile, config: Config) -> str:
    company = config.company(job.company)
    name = _cell(company.name if company else job.company)
    if profile.is_faang_plus(job.company):
        name = f"{FIRE} **{name}**"
    title = _cell(job.title) + (f" {TICK}" if job.eligibility else "")
    location = _cell("; ".join(job.locations[:2]) or "Unclear")
    if len(job.locations) > 2:
        location += f" +{len(job.locations) - 2}"
    first_seen = datetime.fromisoformat(job.first_seen).date().isoformat()
    return f"| {name} | {title} | {location} | {first_seen} | [Apply]({job.url}) |"


def _order(job: StoredJob, profile: Profile, config: Config) -> tuple:
    company = config.company(job.company)
    name = (company.name if company else job.company).lower()
    return (not profile.is_faang_plus(job.company), name, job.title.lower(), job.key)


def _rank(order: list[str], name: str) -> tuple[int, str]:
    return (order.index(name) if name in order else len(order), name)


def _cell(text: str) -> str:
    """Text safe inside a Markdown table cell."""
    return " ".join(text.replace("|", "/").split())
