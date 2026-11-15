"""tal.net (Oleeo) adapter, tested against job-board pages recorded from live tenants.

The fixtures are the results section of each page with all but a few postings removed.
Between them they cover the three ways a tenant lays a board out: a table (Morgan
Stanley), tiles (Jefferies), and both at once (Bank of America, Nomura).
"""

import httpx
import pytest

from conftest import load_text, make_company, mock_client
from jobtracker.http import RobotsDisallowedError
from jobtracker.sources import talnet

BOARD = "lang-en-GB/mobile-0/appcentre-ext/brand-2"
MORGAN_STANLEY = make_company(
    "morgan-stanley", ats="talnet", tenant="morganstanley", board=BOARD, sites=("1", "2")
)
PROGRAMS = f"/vx/{BOARD}/candidate/jobboard/vacancy/1/adv/"
EVENTS = f"/vx/{BOARD}/candidate/jobboard/vacancy/2/adv/"


def test_parse_a_table_board_maps_every_field():
    jobs = talnet.parse(MORGAN_STANLEY, load_text("talnet/morganstanley_programs_page1.html"))
    first = jobs[0]

    assert first.source == "talnet"
    assert first.company == "morgan-stanley"
    assert first.job_id == "22447"
    assert first.title == (
        "2027 Investment Management Off-cycle Internship - Product Development (London)"
    )
    assert first.url == (
        "https://morganstanley.tal.net/vx/lang-en-GB/mobile-0/brand-2/candidate/so/pm/1/pl/1/"
        "opp/22447-2027-Investment-Management-Off-cycle-Internship-Product-Development-London"
        "/en-GB"
    )
    assert first.locations == ("London",)  # the board's "City" column
    assert first.department is None
    assert first.posted_at is None  # boards do not publish one
    assert first.key == "talnet:morgan-stanley:22447"
    assert [job.locations for job in jobs] == [("London",), ("Sydney",), ("New York",)] + [
        ("New York",)
    ]


def test_parse_a_tile_board():
    """Jefferies shows tiles only, with no location field: the city is in the title."""
    jobs = talnet.parse(make_company("jefferies"), load_text("talnet/jefferies_campus.html"))

    assert [(job.job_id, job.title, job.locations) for job in jobs] == [
        ("1953", "2027 Quant Masters Summer Programme - London", ()),
        ("1792", "2027 Investment Banking Summer Internship Programme - London", ()),
        ("1927", "2027 Private Fund Advisory Summer Internship Programme - London", ()),
    ]
    assert all(job.url.startswith("https://jefferies.tal.net/vx/") for job in jobs)


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (
            "talnet/bankofamerica_programs.html",
            [
                ("15289", "2027 Mexico Rates and Currencies Intern Program", ("Mexico City",)),
                ("15283", "Investment Banking 2027 Full-time Analyst - Dubai", ("Dubai",)),
                ("15274", "Global Investment Banking Summer Associate - Tokyo - 2027", ("Tokyo",)),
            ],
        ),
        (
            "talnet/nomura_students.html",
            [
                (
                    "1558",
                    "2027 - Global Markets - Rates Structuring - Off Cycle Internship – Paris",
                    ("Paris",),
                ),
                (
                    "1505",
                    "2027 - Investment Banking Off Cycle Internship – Milan Technology",
                    ("Milan",),
                ),
                (
                    "1435",
                    "2027 - Investment Banking Off Cycle Internship – Madrid FIG Team",
                    ("Madrid",),
                ),
            ],
        ),
    ],
)
def test_parse_a_board_showing_table_and_tiles_lists_each_posting_once(fixture, expected):
    """Each posting is on the page two or three times; it must come out once."""
    jobs = talnet.parse(make_company("bank"), load_text(fixture))
    assert [(job.job_id, job.title, job.locations) for job in jobs] == expected


def test_parse_ignores_columns_that_are_not_locations():
    """An events board has "Address / Venue Details" and "Event Date" columns: free text."""
    jobs = talnet.parse(MORGAN_STANLEY, load_text("talnet/morganstanley_events.html"))
    assert len(jobs) == 2
    assert all(job.locations == () for job in jobs)


def test_parse_an_empty_board():
    assert talnet.parse(MORGAN_STANLEY, load_text("talnet/nomura_events_empty.html")) == []


@pytest.mark.parametrize(
    "page", ["", "<html><body><form action='/vx/ats/login'></form></body></html>"]
)
def test_parse_rejects_a_page_that_is_not_a_job_board(page):
    """A login or error page must be a failed fetch, never an empty board."""
    with pytest.raises(ValueError, match="not a tal.net job board"):
        talnet.parse(MORGAN_STANLEY, page)


def test_next_start_follows_the_next_link_and_stops_on_the_last_page():
    assert talnet.next_start(load_text("talnet/morganstanley_programs_page1.html")) == 50
    # Page 2 links only back to ?start=0.
    assert talnet.next_start(load_text("talnet/morganstanley_programs_page2.html"), 50) is None
    assert talnet.next_start(load_text("talnet/nomura_students.html")) is None


def test_fetch_reads_every_page_of_every_board(settings):
    requested = []
    pages = {
        (PROGRAMS, ""): "talnet/morganstanley_programs_page1.html",
        (PROGRAMS, "start=50"): "talnet/morganstanley_programs_page2.html",
        (EVENTS, ""): "talnet/morganstanley_events.html",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200, text=load_text(pages[request.url.path, request.url.query.decode()])
        )

    jobs = talnet.fetch(MORGAN_STANLEY, mock_client(settings, handler))

    host = "https://morganstanley.tal.net"
    assert requested == [host + PROGRAMS, host + PROGRAMS + "?start=50", host + EVENTS]
    assert len(jobs) == 4 + 2 + 2
    assert len({job.job_id for job in jobs}) == len(jobs)


def test_fetch_waits_out_the_crawl_delay_between_pages(settings):
    sleeps = []
    pages = iter(
        [
            "talnet/morganstanley_programs_page1.html",
            "talnet/morganstanley_programs_page2.html",
            "talnet/morganstanley_events.html",
        ]
    )
    http = mock_client(
        settings,
        lambda request: httpx.Response(200, text=load_text(next(pages))),
        robots_txt=load_text("robots/talnet.txt"),
        sleep=sleeps.append,
        clock=lambda: 0.0,  # a frozen clock: no time passes between requests
    )
    talnet.fetch(MORGAN_STANLEY, http)
    assert sleeps == [10.0, 10.0, 10.0]  # after robots.txt, and after each page but the last


def test_fetch_refuses_a_redirect_to_the_login_page(settings):
    """What a tenant does when a board is withdrawn: it redirects to /ats/login.

    robots.txt disallows that path, so the redirect is refused and never requested.
    """
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if "/ats/" in request.url.path:
            raise AssertionError("a disallowed path was requested")
        login = "https://morganstanley.tal.net/vx/lang-en-GB/xf-e7dd106cee79/ats/login"
        return httpx.Response(302, headers={"Location": login})

    http = mock_client(
        settings, handler, robots_txt=load_text("robots/talnet.txt"), sleep=lambda _: None
    )
    with pytest.raises(RobotsDisallowedError, match="ats/login"):
        talnet.fetch(MORGAN_STANLEY, http)
    assert requested == [PROGRAMS]
