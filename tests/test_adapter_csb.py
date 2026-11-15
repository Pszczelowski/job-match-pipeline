"""SuccessFactors career-site readers (Swiss Re, EY, tier T2, v0.70.0) on trimmed real pages (tests/fixtures/csb, 4 Nov 2026).

Real: three rows of each list with the real result line ("of 13", "of 141"), one job page each. Longer lists are built from
the real row of EY. No test touches the network.
"""
import re
from datetime import datetime, timezone

import pytest

from app import adapters
from app.adapters import AdapterDisabled, PageError, csb
from app.filters.runner import filter_new_jobs
from tests.ats_support import Site, fx, make_cfg, make_conn

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
SWISSRE = next(s for s in adapters.REGISTRY if s.key == "swissre")
EY = next(s for s in adapters.REGISTRY if s.key == "ey")
SR_LIST, SR_DETAIL = fx("csb", "swissre_list.html"), fx("csb", "swissre_detail.html")
EY_LIST, EY_LIST2, EY_DETAIL = fx("csb", "ey_list.html"), fx("csb", "ey_list_startrow25.html"), fx("csb", "ey_detail.html")
EY_URL = "https://careers.ey.com/ey/search/?locationsearch=Switzerland&locale=en_US"
CH = {"country_codes": ["CH"]}
ROW = re.search(r'<tr class="data-row">.*?</tr>', EY_LIST, re.S).group(0)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return make_cfg(tmp_path, monkeypatch, "swissre", "ey")


@pytest.fixture
def conn(tmp_path):
    c = make_conn(tmp_path)
    yield c
    c.close()


def row(i, place="Zurich, CH, 8005"):
    r = re.sub(r"/1264560901/", f"/{i}/", ROW)
    r = r.replace("Technology Consulting - AI", f"Technology Consulting {i} - AI").replace("Technology-Consulting-AI", f"Technology-Consulting-{i}-AI")
    return re.sub(r"Zurich, CH, 8005", place, r)


def page(first, count, total, place="Zurich, CH, 8005"):
    label = f'<span class="paginationLabel">Results <b>{first + 1} – {first + count}</b> of <b>{total}</b></span>'
    return f"<html><body>{label}<table>{''.join(row(2000000 + first + k, place) for k in range(count))}</table></body></html>"


def ey_site(total, size=25, **kw):
    """Pages of `size` rows keyed by their address: the first page has no startrow."""
    pages = {EY_URL: page(0, min(size, total), total)}
    for start in range(size, total, size):
        pages[f"{EY_URL}&startrow={start}"] = page(start, min(size, total - start), total)
    return Site(pages=pages, **kw)


# --- the list parser ------------------------------------------------------------------------------------

def test_real_swiss_re_rows_give_ids_urls_titles_and_places():
    jobs = SWISSRE.parse_list(SR_LIST, CH)
    assert len(jobs) == 3 and jobs[0].external_id == "1420961933"
    assert jobs[0].url == "https://careers.swissre.com/job/Zurich-Lehrstelle-Mediamatikerin-EFZ-2027-Zuri/1420961933/"
    assert jobs[0].locations == ["Zurich, Zurich, CH"]
    assert "&amp;" not in jobs[1].url and "&" in jobs[1].url and jobs[1].external_id == "1453815733"       # the address is unescaped


def test_real_ey_rows_use_the_ey_prefix_and_find_the_country_after_the_city():
    jobs = EY.parse_list(EY_LIST, CH)
    assert len(jobs) == 3 and [j.locations for j in jobs] == [["Zurich, CH, 8005"], ["Renens, CH, 1020"], ["Basel, CH, 4051"]]
    assert jobs[0].url.startswith("https://careers.ey.com/ey/job/Zurich-%28Senior%29-Consultant-")
    assert jobs[0].external_id == "1264560901" and jobs[0].title.startswith("(Senior) Consultant - Technology Consulting")


def test_a_row_in_another_country_is_dropped_and_an_empty_filter_keeps_all():
    head, first, second, third = SR_LIST.split('<tr class="data-row">')
    mixed = '<tr class="data-row">'.join([head, first, second.replace("Zurich, Zurich, CH", "Munich, Bavaria, DE"), third])
    assert "Munich, Bavaria, DE" in mixed
    assert len(SWISSRE.parse_list(mixed, CH)) == 2
    assert len(SWISSRE.parse_list(mixed, {"country_codes": []})) == 3


def test_a_row_with_hidden_further_places_is_kept_by_country_rules():
    hidden = ROW.replace("Zurich, CH, 8005", "Munich, DE <small>+1 more</small>")
    assert len(EY.parse_list(f"<table>{hidden}</table>", CH)) == 1
    plain = ROW.replace("Zurich, CH, 8005", "Munich, DE")
    assert EY.parse_list(f"<table>{plain}</table>", CH) == []


def test_zero_results_are_a_valid_empty_list_but_a_page_without_rows_and_result_line_is_not():
    assert SWISSRE.parse_list('<span class="paginationLabel">Results <b>0</b> of <b>0</b></span>', CH) == []
    assert SWISSRE.parse_list("<html>No jobs matched your search</html>", CH) == []
    with pytest.raises(PageError, match="no rows and no result line"):
        SWISSRE.parse_list("<html>Please verify you are human</html>", CH)


def test_a_row_without_a_link_of_the_site_is_an_error():
    broken = ROW.replace('href="/ey/job/', 'href="/other/job/')
    with pytest.raises(PageError, match="no job link"):
        EY.parse_list(f"<table>{broken}</table>", CH)
    with pytest.raises(PageError, match="no job link"):
        SWISSRE.parse_list(f"<table>{ROW}</table>", CH)                   # the EY prefix is not a Swiss Re link


# --- paging ---------------------------------------------------------------------------------------------

def test_the_other_pages_come_from_the_total_and_the_rows_of_the_first_page():
    first = page(0, 25, 141)
    urls = EY.more_pages(EY_URL, first)
    assert urls == [f"{EY_URL}&startrow={n}" for n in (25, 50, 75, 100, 125)]
    assert EY.more_pages(EY_URL + "&startrow=25", first)[0].count("startrow=") == 1             # a startrow is replaced, not doubled
    assert SWISSRE.more_pages("https://careers.swissre.com/search/?locationsearch=Switzerland", SR_LIST.replace("of <b>13</b>", "of <b>3</b>")) == []
    assert EY.more_pages(EY_URL, page(0, 25, 25)) == [] and EY.more_pages(EY_URL, "<html>Results of <b>0</b></html>") == []


def test_the_total_may_have_separators_and_rows_without_a_total_are_an_error():
    assert len(EY.more_pages(EY_URL, page(0, 25, "1,041"))) == 41
    with pytest.raises(PageError, match="no total"):
        EY.more_pages(EY_URL, f"<table>{ROW}</table>")


def test_all_pages_are_read_with_robots_first_and_a_pause_before_every_request(conn, cfg):
    site = ey_site(60)
    s, jobs = adapters.fetch_list(conn, cfg, EY, site, sleep=site.sleep, run_id="run1")
    assert (s.found, s.new, s.error, s.complete) == (60, 60, None, True)
    assert [u for _, u in site.requests] == ["https://careers.ey.com/robots.txt", EY_URL, f"{EY_URL}&startrow=25", f"{EY_URL}&startrow=50"]
    assert site.events[0] == ("sleep", 5) and site.pauses == [5, 5, 5, 5]                    # also before robots.txt
    assert conn.execute("SELECT DISTINCT source FROM job_sources").fetchone()[0] == "ey_site"
    assert conn.execute("SELECT DISTINCT company FROM jobs").fetchone()[0] == "EY"


def test_a_second_read_changes_nothing(conn, cfg):
    adapters.fetch_list(conn, cfg, EY, ey_site(30), sleep=lambda s: None, run_id="run1")
    s, _ = adapters.fetch_list(conn, cfg, EY, ey_site(30), sleep=lambda s: None, run_id="run1")
    assert (s.found, s.new, s.outcomes) == (30, 0, {"unchanged": 30})


def test_the_page_cap_leaves_the_read_not_complete(conn, cfg):
    cfg["adapters"]["max_list_pages"] = 2
    site = ey_site(100)
    s, _ = adapters.fetch_list(conn, cfg, EY, site, sleep=site.sleep, run_id="run1")
    assert (s.found, s.complete) == (50, False) and len(site.requests) == 3                    # robots.txt and two pages


def test_a_refusal_on_a_later_page_is_reported_and_stores_nothing(conn, cfg):
    from app.descriptions_fetch import FetchError
    site = ey_site(60)
    site.pages[f"{EY_URL}&startrow=25"] = FetchError("HTTP 429", 429)
    s, jobs = adapters.fetch_list(conn, cfg, EY, site, sleep=lambda s: None)
    assert jobs == [] and "HTTP 429" in s.error and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_the_services_path_is_never_requested_and_a_foreign_host_is_refused(conn, cfg):
    site = ey_site(30)
    adapters.fetch_list(conn, cfg, EY, site, sleep=lambda s: None)
    assert not any("/services/" in u for _, u in site.requests)
    cfg["adapters"]["ey"]["list_url"] = "https://www.careers.ey.com/ey/search/?locationsearch=Switzerland"
    s, _ = adapters.fetch_list(conn, cfg, EY, Site(), sleep=lambda s: None)
    assert "not a page on careers.ey.com" in s.error


# --- the job page ---------------------------------------------------------------------------------------

def test_the_swiss_re_page_gives_text_and_the_posting_date():
    d = SWISSRE.parse_detail(SR_DETAIL)
    assert "<" not in d.text and "Reference Code" in d.text and d.posted == "2026-10-25"


def test_the_ey_page_drops_the_standard_paragraphs():
    d = EY.parse_detail(EY_DETAIL)
    assert "Mobility" in d.text and d.posted == "2026-10-11"
    for gone in ("Building a better working world", "EY teams work across", "all in to shape your future", "Join EY and help"):
        assert gone not in d.text
    assert csb.SPECS[0].parse_detail is not EY.parse_detail


def test_a_phone_line_is_dropped():
    page_ = '<html><meta itemprop="datePosted" content="Sun Oct 11 02:00:00 UTC 2026"><span itemprop="description"><p>Role.</p><p>Call +41 44 123 45 67 now</p></span></html>'
    assert SWISSRE.parse_detail(page_).text == "Role."


@pytest.mark.parametrize("page_, message", [
    ("<html>maintenance</html>", "no description block"),
    ('<span itemprop="description"><p>Join EY and help to build a better working world.</p></span>', "empty description"),
])
def test_a_changed_or_empty_page_is_an_error(page_, message):
    with pytest.raises(PageError, match=message):
        EY.parse_detail(page_)


def test_job_pages_flow_with_robots_and_the_pause_before_each_request(conn, cfg):
    adapters.fetch_list(conn, cfg, EY, ey_site(30), sleep=lambda s: None, run_id="run1")
    filter_new_jobs(conn, cfg, "run1")
    site = Site(default=EY_DETAIL)
    s = adapters.fetch_details(conn, cfg, EY, site, sleep=site.sleep, now=NOW, run_id="run1", limit=2)
    assert (s.fetched, s.failed, s.stopped) == (2, 0, None)
    assert site.requests[0] == ("get", "https://careers.ey.com/robots.txt") and len(site.requests) == 3
    assert site.pauses == [5, 5, 5]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE description_missing = 0").fetchone()[0] >= 1


def test_t2_needs_the_accepted_risk_written_down(conn, cfg):
    cfg["adapters"]["ey"]["decision_note"] = ""
    with pytest.raises(AdapterDisabled, match="tier T2"):
        adapters.fetch_list(conn, cfg, EY, Site())
    assert EY not in adapters.enabled_adapters(cfg) and SWISSRE in adapters.enabled_adapters(cfg)
