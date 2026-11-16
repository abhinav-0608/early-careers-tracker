"""robots.txt parsing and matching, following RFC 9309."""

import pytest

from conftest import load_text
from jobtracker import robots

AGENT = "job-alert-tracker"


def rules(text: str) -> robots.RobotsRules:
    return robots.parse(text, AGENT)


def test_no_rules_allows_everything():
    assert robots.ALLOW_ALL.allows("/anything")
    assert rules("").allows("/anything")
    assert rules("User-agent: *\nDisallow:\n").allows("/anything")  # empty = nothing


def test_disallow_is_a_prefix_match():
    parsed = rules("User-agent: *\nDisallow: /private/\n")
    assert not parsed.allows("/private/page")
    assert parsed.allows("/private")  # the rule has a trailing slash
    assert parsed.allows("/public/page")


def test_wildcard_matches_any_run_of_characters():
    """The rule tal.net uses. A parser without wildcards would allow all of these."""
    parsed = rules("User-agent: *\nDisallow: /*/ats/\n")
    assert not parsed.allows("/vx/ats/login")
    assert not parsed.allows("/vx/lang-en-GB/xf-e7dd106cee79/ats/login")
    assert parsed.allows("/vx/lang-en-GB/candidate/jobboard/vacancy/1/adv/")
    assert parsed.allows("/vx/stats/")  # "ats/" must be a whole path segment


def test_dollar_anchors_the_end():
    parsed = rules("User-agent: *\nDisallow: /*.json$\n")
    assert not parsed.allows("/data/jobs.json")
    assert parsed.allows("/data/jobs.json.html")


def test_longest_matching_rule_wins_and_allow_wins_a_tie():
    parsed = rules("User-agent: *\nDisallow: /\nAllow: /roles/\n")
    assert parsed.allows("/roles/123")
    assert not parsed.allows("/results")

    tie = rules("User-agent: *\nDisallow: /jobs\nAllow: /jobs\n")
    assert tie.allows("/jobs")


def test_a_group_naming_the_tracker_replaces_the_star_group():
    text = """
User-agent: *
Disallow: /

User-agent: Job-Alert-Tracker
Disallow: /admin/
"""
    parsed = rules(text)
    assert parsed.allows("/jobs")
    assert not parsed.allows("/admin/x")


def test_rules_for_other_crawlers_are_ignored():
    text = """
User-agent: *
Disallow: /search

User-agent: GPTBot
User-agent: ClaudeBot
Disallow: /
"""
    parsed = rules(text)
    assert parsed.allows("/jobs")
    assert not parsed.allows("/search")


def test_several_user_agent_lines_share_one_group():
    parsed = rules("User-agent: Yandex\nUser-agent: *\nDisallow: /private\n")
    assert not parsed.allows("/private")


def test_comments_case_and_stray_lines_are_tolerated():
    text = "# hello\nUSER-AGENT: *   # everyone\ndisallow: /x  # no\nSitemap: https://e/s.xml\nnonsense\n"
    assert not rules(text).allows("/x/y")


def test_crawl_delay_is_read_from_the_matching_group():
    text = """
User-agent: *
Disallow: /*/ats/
Crawl-delay: 10

User-agent: Slowbot
Crawl-delay: 99
"""
    assert rules(text).crawl_delay == 10.0
    assert rules("User-agent: *\nCrawl-delay: soon\n").crawl_delay is None
    assert rules("Crawl-delay: 10\nUser-agent: *\nDisallow: /x\n").crawl_delay is None


def test_product_token_comes_from_the_user_agent():
    agent = "job-alert-tracker/1.0 (+https://github.com/someone/somewhere)"
    assert robots.product_token(agent) == "job-alert-tracker"


# --- the files the adapters actually meet, recorded 2026-11-15 ---------------------------


def test_talnet_allows_job_boards_and_disallows_the_login_pages():
    parsed = rules(load_text("robots/talnet.txt"))
    board = "/vx/lang-en-GB/mobile-0/appcentre-ext/brand-2/candidate/jobboard/vacancy/1/adv/"
    assert parsed.allows(board)
    assert parsed.allows(board + "?start=50")
    assert not parsed.allows("/vx/lang-en-GB/xf-e7dd106cee79/ats/login")
    assert not parsed.allows("/vx/lang-en-GB/mobile-0/brand-2/agent/jobs")
    assert not parsed.allows("/vx/lang-en-GB/mobile-0/channel/1/x")
    assert parsed.crawl_delay == 10.0


def test_google_allows_a_first_results_page_and_disallows_every_later_one():
    parsed = rules(load_text("robots/google.txt"))
    results = "/about/careers/applications/jobs/results"
    first = "?company=Google&target_level=INTERN_AND_APPRENTICE&sort_by=date"
    assert parsed.allows(results + first)
    assert not parsed.allows(results + "?page=2")
    assert not parsed.allows(results + first + "&page=2")
    assert not parsed.allows(results + "/?page=2")


def test_goldman_sachs_allows_only_single_role_pages():
    """Why Goldman Sachs is unsupported: the pages that list roles are all disallowed."""
    parsed = rules(load_text("robots/goldman-sachs.txt"))
    assert parsed.allows("/roles/153412")
    assert not parsed.allows("/results?EXPERIENCE_LEVEL=Analyst")
    assert not parsed.allows("/")


def test_smartrecruiters_api_disallows_every_crawler_but_one():
    """Why the SmartRecruiters adapter reads the public job board and not this API."""
    text = load_text("robots/smartrecruiters-api.txt")
    assert not rules(text).allows("/v1/companies/Wise/postings")
    assert not rules(text).allows("/v1/companies/Canva/postings?offset=100&limit=100")
    assert robots.parse(text, "linkedinbot").allows("/v1/companies/Wise/postings")


def test_amazon_jobs_allows_its_search_and_amd_asks_for_five_seconds():
    amazon = rules(load_text("robots/amazon-jobs.txt"))
    assert amazon.allows("/en/search.json?business_category[]=studentprograms&offset=100")
    assert not amazon.allows("/en/internal/jobs/1")
    amd = rules(load_text("robots/amd.txt"))
    assert amd.allows("/api/jobs?page=1&limit=100&keywords=intern")
    assert amd.crawl_delay == 5.0


@pytest.mark.parametrize(
    ("site", "allowed"),
    [("/Undergraduate_careers/", True), ("/Graduate_careers/", True), ("/refreshFacet/", False)],
)
def test_workday_tenant_lists_the_sites_it_allows(site, allowed):
    assert rules(load_text("robots/workday-lbg.txt")).allows(site) is allowed


@pytest.mark.parametrize(
    "fixture", ["amazon-jobs.txt", "amd.txt", "microsoft-apply.txt", "smartrecruiters-api.txt"]
)
def test_rules_survive_being_cached_as_plain_lines(fixture):
    """The cache in state keeps the rules chosen for this tracker, not the whole file."""
    rules = robots.parse(load_text(f"robots/{fixture}"), AGENT)
    restored = robots.RobotsRules.from_lines(rules.to_lines(), rules.crawl_delay)

    assert restored.to_lines() == rules.to_lines()
    assert restored.crawl_delay == rules.crawl_delay
    for path in ("/", "/en/search.json?x=1", "/api/jobs", "/api/pcsx/search", "/v1/companies/a"):
        was, now = rules.rule_for(path), restored.rule_for(path)
        assert (was and (was.allow, was.pattern)) == (now and (now.allow, now.pattern))
