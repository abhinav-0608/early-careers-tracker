"""Reading one posting's description, for the checks that a title cannot settle.

Whether a role is open to first-year students is usually said in the body of the
posting, not its title. Descriptions are only read for jobs that have already passed a
profile's title, field and location filters, so this is a handful of requests when a
posting first appears, not one per posting on a board.

Where the text comes from, in order:

  1. the list response, when it already carries it (Lever, Ashby, Amazon): no request;
  2. Greenhouse's job endpoint, GET /v1/boards/{board}/jobs/{id};
  3. the posting's own page (tal.net, Workday, SmartRecruiters, Workable and the custom
     sites): the JobPosting description a page publishes as JSON-LD if it has one,
     otherwise the page's visible text.

Every request goes through HttpClient, so robots.txt, crawl delays and back-off apply
exactly as they do to the board itself.
"""

from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser

from jobtracker.http import HttpClient
from jobtracker.models import Company, Job
from jobtracker.sources import greenhouse

GREENHOUSE_JOB = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
MAX_LENGTH = 60_000  # characters kept; a posting is a few thousand

_TAG = re.compile(r"(?s)<[^>]+>")
_SPACE = re.compile(r"[ \t\r\f\v]+")


def describe(company: Company, job: Job, http: HttpClient) -> str:
    """Return the posting's text. Raises what the request raises; "" if the page has none."""
    if job.description:
        return plain_text(job.description)
    if job.source == greenhouse.NAME:
        payload = http.get_json(GREENHOUSE_JOB.format(board=company.board, job_id=job.job_id))
        # Greenhouse escapes the HTML of the description, so it is unescaped twice over:
        # once here and once more for the entities inside the text itself.
        return plain_text(html.unescape(payload.get("content") or ""))
    return page_text(http.get_text(job.url))


def plain_text(markup: str) -> str:
    """HTML or plain text -> text with one space between words and line breaks kept."""
    text = html.unescape(_TAG.sub(" ", markup.replace("<br", "\n<br").replace("</p>", "\n")))
    lines = [_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_LENGTH]


def page_text(page: str) -> str:
    """The description on a posting's page.

    A page that publishes a schema.org JobPosting as JSON-LD (Workday, SmartRecruiters,
    Workable and many careers sites do, for search engines) gives the description
    exactly. Otherwise the visible text of the page is used, which for a server-rendered
    page (tal.net) is the posting plus a little navigation.
    """
    reader = _PageReader()
    reader.feed(page)
    for block in reader.json_ld:
        description = _job_posting_description(block)
        if description:
            # Some sites escape the HTML inside the JSON as well ("&lt;p&gt;"), so it is
            # unescaped once before the tags are removed, as for Greenhouse.
            return plain_text(html.unescape(description))
    return plain_text("\n".join(reader.text))


def _job_posting_description(block: str) -> str | None:
    try:
        data = json.loads(block)
    except ValueError:
        return None
    for item in data if isinstance(data, list) else [data]:
        if isinstance(item, dict) and item.get("@type") == "JobPosting":
            description = item.get("description")
            if isinstance(description, str) and description.strip():
                return description
    return None


class _PageReader(HTMLParser):
    """Collects a page's visible text and its JSON-LD blocks."""

    _HIDDEN = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.json_ld: list[str] = []
        self._hidden: list[str] = []
        self._in_json_ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._HIDDEN:
            self._hidden.append(tag)
            self._in_json_ld = tag == "script" and dict(attrs).get("type") == "application/ld+json"

    def handle_endtag(self, tag: str) -> None:
        if self._hidden and tag == self._hidden[-1]:
            self._hidden.pop()
            self._in_json_ld = False

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self.json_ld.append(data)
        elif not self._hidden and data.strip():
            self.text.append(data)
