"""Employer career-site adapters: framework rules and the SIX adapter, on trimmed real pages (tests/fixtures/six)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import adapters, db
from app.adapters import AdapterDisabled, PageError, six
from app.config import ConfigError, load_config
from app.descriptions_fetch import FetchError
from app.filters.runner import filter_new_jobs
from app.pipeline import prepare
from app.travel import OsrmProvider, Providers, TransportProvider
from tests.test_location import Replay, TODAY
from tests.test_mailbox import FakeIMAP

FIX = Path(__file__).parent / "fixtures" / "six"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
LIST = (FIX / "list.html").read_text(encoding="utf-8")
DETAIL = (FIX / "detail.html").read_text(encoding="utf-8")
LIST_URL = "https://jobs.six-group.com/search/?q&locationsearch&locale=en_US"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x-test-pass")
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    c["adapters"]["six"]["enabled"] = True
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.start_run(c, "run1")
    yield c
    c.close()


class Pages:
    def __init__(self, pages=None, fail=None):
        self.pages, self.fail, self.calls = pages or {}, fail or {}, []

    def __call__(self, url):
        self.calls.append(url)
        if url in self.fail:
            raise self.fail[url]
        if url in self.pages:
            return self.pages[url]
        return LIST if url == LIST_URL else DETAIL


# --- the SIX page parsers ---------------------------------------------------------------------------

def test_list_keeps_swiss_jobs_and_drops_other_countries():
    jobs = six.parse_list(LIST, {"country_codes": ["CH"]})
    assert [j.locations for j in jobs] == [["Zurich, CH"], ["Olten, CH"], ["Zurich, CH"]]       # the Madrid job is dropped
    j = jobs[0]
    assert j.external_id.isdigit() and j.url == f"https://jobs.six-group.com/job/{j.url.split('/job/')[1]}"
    assert j.title and "&amp;" not in j.title


def test_list_with_no_country_filter_keeps_everything():
    assert len(six.parse_list(LIST, {"country_codes": []})) == 4


def test_list_row_with_more_places_is_not_dropped_by_country():
    page = '<table><tr class="data-row">' + LIST.split('<tr class="data-row">')[2].split("</tr>")[0].replace("Madrid, ES", "Madrid, ES <small class=\"nobr\">+1 more&hellip;</small>") + "</tr></table>"
    assert [j.locations for j in six.parse_list(page, {"country_codes": ["CH"]})] == [["Madrid, ES"]]


def test_a_changed_layout_is_an_error_and_an_empty_result_is_not():
    with pytest.raises(PageError, match="layout changed"):
        six.parse_list("<html><body>maintenance</body></html>", {"country_codes": []})
    assert six.parse_list("<html>Results 1 – 0 of 0 <table></table></html>", {"country_codes": []}) == []


def test_detail_text_posting_date_and_no_phone_numbers():
    d = six.parse_detail(DETAIL)
    assert "Write SQL queries" in d.text and "Fluent English is required" in d.text
    assert "+41" not in d.text and "Jane Doe" not in d.text                     # the recruiter's contact line is dropped
    assert "only accept direct applications" not in d.text
    assert d.posted == "2026-10-13"


def test_a_detail_page_without_the_description_block_is_an_error():
    with pytest.raises(PageError, match="layout changed"):
        six.parse_detail("<html><body>nothing here</body></html>")


TWO_PLACES = (FIX / "detail_two_places.html").read_text(encoding="utf-8")


def test_detail_reads_every_place():
    d = six.parse_detail(TWO_PLACES)
    assert d.locations == ["Warsaw, PL", "London, GB"]
    assert d.text == six.parse_detail(DETAIL).text


def test_detail_one_place():
    assert six.parse_detail(DETAIL).locations == ["Zurich, CH"]


def test_detail_three_places_synthetic_without_duplicates():
    third = '<span itemprop="address"><meta itemprop="streetAddress" content="Zurich, CH"></span>'
    page = TWO_PLACES.replace('content="London, GB"></span>', 'content="London, GB"></span>' + third + third, 1)
    assert six.parse_detail(page).locations == ["Warsaw, PL", "London, GB", "Zurich, CH"]


def test_detail_without_location_block():
    page = TWO_PLACES.replace('itemprop="jobLocation"', 'itemprop="other"')
    assert six.parse_detail(page).locations == []


def test_detail_unescapes_places():
    page = DETAIL.replace('content="Zurich, CH"', 'content="Z&uuml;rich, CH"')
    assert six.parse_detail(page).locations == ["Zürich, CH"]


# --- switches and limits ----------------------------------------------------------------------------

def test_every_adapter_is_off_by_default(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    assert adapters.enabled_adapters(load_config()) == []


def test_a_disabled_adapter_requests_nothing(conn, cfg):
    cfg["adapters"]["six"]["enabled"] = False
    pages = Pages()
    with pytest.raises(AdapterDisabled):
        adapters.fetch_list(conn, cfg, six.SPEC, pages)
    assert pages.calls == []


def test_the_config_is_checked(monkeypatch, tmp_path):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    bad = tmp_path / "c.yaml"
    bad.write_text("adapters:\n  six:\n    list_url: 'http://jobs.six-group.com/'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="https"):
        load_config(bad)


def test_only_the_employers_own_host_is_requested(conn, cfg):
    cfg["adapters"]["six"]["list_url"] = "https://evil.example.com/search/"
    pages = Pages()
    summary, jobs = adapters.fetch_list(conn, cfg, six.SPEC, pages)
    assert pages.calls == [] and jobs == [] and "not a page on jobs.six-group.com" in summary.error


# --- listing ----------------------------------------------------------------------------------------

def test_list_is_stored_once_and_a_second_run_changes_nothing(conn, cfg):
    s1, _ = adapters.fetch_list(conn, cfg, six.SPEC, Pages(), run_id="run1")
    s2, _ = adapters.fetch_list(conn, cfg, six.SPEC, Pages(), run_id="run1")
    assert (s1.found, s1.new, s1.error) == (3, 3, None) and (s2.found, s2.new) == (3, 0)
    assert s2.outcomes == {"unchanged": 3}
    rows = conn.execute("SELECT j.company, s.source, j.description_missing FROM jobs j JOIN job_sources s ON s.job_id = j.id").fetchall()
    assert {tuple(r) for r in rows} == {("SIX", "six_site", 1)}


def test_a_dry_run_stores_nothing(conn, cfg):
    s, jobs = adapters.fetch_list(conn, cfg, six.SPEC, Pages(), store=False)
    assert s.found == 3 and len(jobs) == 3
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_a_refusal_on_the_list_is_reported_not_raised(conn, cfg):
    s, jobs = adapters.fetch_list(conn, cfg, six.SPEC, Pages(fail={LIST_URL: FetchError("HTTP 403", 403)}))
    assert jobs == [] and "HTTP 403" in s.error and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_a_changed_list_layout_is_reported_not_raised(conn, cfg):
    s, jobs = adapters.fetch_list(conn, cfg, six.SPEC, Pages(pages={LIST_URL: "<html>new design</html>"}))
    assert jobs == [] and "layout changed" in s.error


# --- job pages --------------------------------------------------------------------------------------

def listed_and_filtered(conn, cfg):
    adapters.fetch_list(conn, cfg, six.SPEC, Pages(), run_id="run1")
    filter_new_jobs(conn, cfg, "run1")


def test_job_pages_are_fetched_only_for_jobs_that_passed_the_first_filters(conn, cfg):
    adapters.fetch_list(conn, cfg, six.SPEC, Pages(), run_id="run1")
    assert adapters.eligible_jobs(conn, six.SPEC) == []                       # not filtered yet: nothing is requested
    filter_new_jobs(conn, cfg, "run1")
    assert len(adapters.eligible_jobs(conn, six.SPEC)) == 3


def test_job_pages_store_the_description_once_with_a_pause(conn, cfg):
    listed_and_filtered(conn, cfg)
    pages, pauses = Pages(), []
    s = adapters.fetch_details(conn, cfg, six.SPEC, pages, sleep=pauses.append, now=NOW, run_id="run1")
    assert (s.fetched, s.failed, s.stopped) == (3, 0, None) and len(pages.calls) == 3
    assert pauses == [cfg["adapters"]["delay_seconds"]] * 2
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE description_missing = 0").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM description_log WHERE source = 'six_site'").fetchone()[0] == 3
    again = Pages()
    assert adapters.fetch_details(conn, cfg, six.SPEC, again, sleep=lambda s: None, now=NOW).attempted == 0 and again.calls == []


def test_the_daily_cap_is_kept(conn, cfg):
    cfg["adapters"]["daily_cap"] = 2
    listed_and_filtered(conn, cfg)
    pages = Pages()
    s = adapters.fetch_details(conn, cfg, six.SPEC, pages, sleep=lambda s: None, now=NOW)
    assert s.fetched == 2 and "daily cap of 2" in s.stopped and len(pages.calls) == 2


def test_a_refusal_stops_the_run_and_a_bad_page_does_not(conn, cfg):
    listed_and_filtered(conn, cfg)
    urls = [r["url"] for r in conn.execute("SELECT url FROM job_sources ORDER BY id")]
    pages = Pages(pages={urls[0]: "<html>redesigned</html>"}, fail={urls[1]: FetchError("HTTP 429", 429)})
    s = adapters.fetch_details(conn, cfg, six.SPEC, pages, sleep=lambda s: None, now=NOW)
    assert s.failed == 2 and s.fetched == 0 and "HTTP 429" in s.stopped and len(pages.calls) == 2        # the third is not asked
    errors = conn.execute("SELECT status, http_status FROM description_fetches ORDER BY job_id").fetchall()
    assert [tuple(e) for e in errors] == [("error", None), ("error", 429)]


def test_a_c1_requirement_in_the_fetched_text_archives_the_job(conn, cfg):
    listed_and_filtered(conn, cfg)
    c1 = DETAIL.replace("Fluent English is required; German language skills are an advantage.",
                        "Business fluent German (C1) and English are a must for this position.")
    adapters.fetch_details(conn, cfg, six.SPEC, Pages(pages={u: c1 for u in [r["url"] for r in conn.execute("SELECT url FROM job_sources")]}),
                           sleep=lambda s: None, now=NOW, limit=1)
    assert conn.execute("SELECT COUNT(*) FROM job_archive WHERE reason_code = 'language_c1_required'").fetchone()[0] == 1


# --- inside prepare ---------------------------------------------------------------------------------

@pytest.fixture
def providers(cfg):
    replay = Replay()
    p = cfg["location"]["providers"]
    return Providers(TransportProvider(replay, p["transport_url"]), OsrmProvider(replay, p["osrm_url"]), today=TODAY)


def run_prepare(conn, cfg, providers, fetch):
    return prepare(conn, cfg, providers=providers, prompt=("p", "v1"), mail_client_factory=lambda c: FakeIMAP({}),
                   page_fetcher=fetch, page_sleep=lambda s: None, run_id="run1", now=NOW)


def test_prepare_lists_filters_and_fetches_job_pages(conn, cfg, providers):
    page = LIST.replace("Zurich, CH", "Zürich, CH")
    pages = Pages(pages={LIST_URL: page})
    r = run_prepare(conn, cfg, providers, pages)
    row = conn.execute("SELECT adapter, found, new_jobs, error FROM adapter_runs WHERE adapter = 'SIX career site'").fetchone()
    assert tuple(row)[:3] == ("SIX career site", 3, 3)
    assert pages.calls[0] == LIST_URL and len(pages.calls) >= 2                # the list, then job pages of surviving jobs
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE description_missing = 0").fetchone()[0] >= 1
    assert r.batch is not None


def test_prepare_survives_a_site_that_fails(conn, cfg, providers):
    r = run_prepare(conn, cfg, providers, Pages(fail={LIST_URL: FetchError("HTTP 503", 503)}))
    assert any("HTTP 503" in w for w in r.warnings)
    assert "HTTP 503" in conn.execute("SELECT error FROM adapter_runs WHERE adapter = 'SIX career site'").fetchone()[0]


# --- "+N more" rows: the hidden Zurich place (decided 4 Nov 2026) -------------------------------------

def _more_row(place="Geneva, CH"):
    row = LIST.split('<tr class="data-row">')[2].split("</tr>")[0].replace("Madrid, ES", place + ' <small class="nobr">+1 more&hellip;</small>')
    return '<table><tr class="data-row">' + row + "</tr></table>"


def _page_with(*places):
    block = "".join(f'<span itemprop="address"><meta itemprop="streetAddress" content="{p}"></span>' for p in places)
    # replace the whole jobLocation block with the wanted places
    start = TWO_PLACES.index('<span itemprop="jobLocation"')
    end = TWO_PLACES.index('itemprop="description"')
    end = TWO_PLACES.rfind("<span", 0, end)
    return TWO_PLACES[:start] + '<span itemprop="jobLocation">' + block + "</span>" + TWO_PLACES[end:]


def _more_pages(detail):
    page = _more_row()
    job = six.parse_list(page, {"country_codes": ["CH"]})[0]
    return job, Pages(pages={LIST_URL: page, job.url: detail})


def test_a_new_more_row_gets_its_zurich_place_from_the_job_page(conn, cfg):
    job, pages = _more_pages(_page_with("Geneva, CH", "Zürich, CH"))
    assert job.more_places and _page_with("Geneva, CH", "Zürich, CH") != TWO_PLACES
    adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")
    places = [r[0] for r in conn.execute("SELECT location_text FROM job_locations ORDER BY id")]
    assert places == ["Geneva, CH", "Zürich, CH"] and pages.calls == [LIST_URL, job.url]


def test_zurich_and_zuerich_spellings_are_added_once(conn, cfg):
    job, pages = _more_pages(_page_with("Zurich, CH", "Zürich, CH", "Geneva, CH"))
    adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")
    places = [r[0] for r in conn.execute("SELECT location_text FROM job_locations ORDER BY id")]
    assert places == ["Geneva, CH", "Zurich, CH"]                       # no second Zurich, no other city added


def test_a_row_without_more_causes_no_page_request(conn, cfg):
    import re
    plain = re.sub(r"<small[^>]*>[^<]*</small>", "", LIST)               # the fixture's Olten row has a "+N more"
    pages = Pages(pages={LIST_URL: plain})
    adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")
    assert pages.calls == [LIST_URL]


def test_a_stored_more_row_is_not_read_again(conn, cfg):
    job, pages = _more_pages(_page_with("Geneva, CH", "Zurich, CH"))
    adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")
    adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")
    assert pages.calls.count(job.url) == 1


def test_a_failed_page_read_keeps_the_row_as_listed(conn, cfg):
    job, pages = _more_pages(DETAIL)
    pages.fail[job.url] = FetchError("HTTP 503", 503)
    s, _ = adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")
    assert s.new == 1 and [r[0] for r in conn.execute("SELECT location_text FROM job_locations")] == ["Geneva, CH"]


def test_a_more_row_whose_page_names_no_zurich_place_is_dropped(conn, cfg):
    job, pages = _more_pages(_page_with("Warsaw, PL", "London, GB"))
    adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert conn.execute("SELECT reason_code FROM job_archive").fetchone()[0] == "no_zurich_place"
    adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")    # not read again
    assert pages.calls.count(job.url) == 1 and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_a_more_row_whose_page_names_no_place_is_kept(conn, cfg):
    job, pages = _more_pages(_page_with())
    adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")
    assert pages.calls == [LIST_URL, job.url]
    assert conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_a_more_row_that_lists_zurich_makes_no_page_request(conn, cfg):
    page = _more_row("Zurich, CH")
    pages = Pages(pages={LIST_URL: page})
    adapters.fetch_list(conn, cfg, six.SPEC, pages, sleep=lambda s: None, run_id="run1")
    assert pages.calls == [LIST_URL]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
