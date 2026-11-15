"""What the public-ATS readers (v0.70.0) share: the T2 gate, robots.txt at run time, job hosts, empty lists, the registry,
the config checks, and the job page of a job first seen on LinkedIn. Offline; the fake site serves robots.txt too."""
import re
from datetime import datetime, timezone

import pytest

from app import adapters, alerts, db
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.descriptions_fetch import FetchError
from app.filters.runner import filter_new_jobs
from app.models import NormalizedJob
from tests.ats_support import Site, fx, make_cfg, make_conn
from tests.test_adapter_csb import EY_DETAIL, EY_URL, ey_site

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
EY = next(s for s in adapters.REGISTRY if s.key == "ey")
PLACE = "Zurich, CH, 8005"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return make_cfg(tmp_path, monkeypatch, "ey")


@pytest.fixture
def conn(tmp_path):
    c = make_conn(tmp_path)
    yield c
    c.close()


def title(i):
    return f"(Senior) Consultant - Technology Consulting {2000000 + i} - AI & Data (80-100%)"


def linkedin_job(conn, i):
    ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id=f"li-{i}", title=title(i), company="EY",
                                   url=f"https://www.linkedin.com/jobs/view/{i}/", locations=[PLACE]), "run1")


# --- a job first seen on LinkedIn gets the page of the employer site (own step, decided 3 Nov 2026) -------------------

def test_a_job_first_seen_on_linkedin_and_listed_on_the_employer_site_gets_the_employer_page(conn, cfg):
    linkedin_job(conn, 0)
    adapters.fetch_list(conn, cfg, EY, ey_site(1), sleep=lambda s: None, run_id="run1")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1                 # the same job, linked by title and place
    filter_new_jobs(conn, cfg, "run1")
    todo = adapters.eligible_jobs(conn, EY)
    assert len(todo) == 1 and todo[0]["url"].startswith("https://careers.ey.com/ey/job/")      # the employer page, not LinkedIn
    site = Site(default=EY_DETAIL)
    s = adapters.fetch_details(conn, cfg, EY, site, sleep=site.sleep, now=NOW, run_id="run1")
    assert (s.fetched, s.failed, s.stopped) == (1, 0, None)
    assert [u for _, u in site.requests][0] == "https://careers.ey.com/robots.txt"
    assert not any("linkedin" in u for _, u in site.requests)
    assert conn.execute("SELECT description_missing FROM jobs").fetchone()[0] == 0


def test_a_linkedin_job_that_the_site_does_not_list_is_not_fetched(conn, cfg):
    linkedin_job(conn, 5)
    adapters.fetch_list(conn, cfg, EY, ey_site(1), sleep=lambda s: None, run_id="run1")      # lists job 0 only
    filter_new_jobs(conn, cfg, "run1")
    todo = adapters.eligible_jobs(conn, EY)
    assert len(todo) == 1 and todo[0]["url"].startswith("https://careers.ey.com/ey/job/")          # only the job of the site's own list


def test_the_pages_of_linkedin_first_jobs_count_against_the_daily_cap(conn, cfg):
    cfg["adapters"]["daily_cap"] = 1
    for i in (0, 1):
        linkedin_job(conn, i)
    adapters.fetch_list(conn, cfg, EY, ey_site(2), sleep=lambda s: None, run_id="run1")
    filter_new_jobs(conn, cfg, "run1")
    first = adapters.fetch_details(conn, cfg, EY, Site(default=EY_DETAIL), sleep=lambda s: None, now=NOW)
    assert first.fetched == 1
    again = Site(default=EY_DETAIL)
    second = adapters.fetch_details(conn, cfg, EY, again, sleep=lambda s: None, now=NOW)
    assert second.attempted == 0 and again.requests == []


# --- tier T2 -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["swissre", "ey"])
def test_a_t2_adapter_needs_a_written_risk_or_basis(tmp_path, monkeypatch, key):
    cfg = make_cfg(tmp_path, monkeypatch)
    spec = next(s for s in adapters.REGISTRY if s.key == key)
    assert spec.tier == "T2"
    cfg["adapters"][key].update(enabled=True, permission_note="", decision_note="  ")
    with pytest.raises(adapters.AdapterDisabled, match=f"tier T2: write the accepted risk in adapters.{key}.decision_note"):
        adapters.require_enabled(cfg, spec)
    cfg["adapters"][key]["decision_note"] = "3 Nov 2026: approved, immediately use it as T2"
    assert adapters.require_enabled(cfg, spec) is cfg["adapters"][key]
    cfg["adapters"][key].update(decision_note="", permission_note="written permission of 1 Nov")
    assert spec in adapters.enabled_adapters(cfg)


def test_the_older_adapters_and_t1_need_no_note(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch)
    keys = ("swisscom", "vontobel", "temenos", "hitachienergy", "novartis", "deutschebank", "scandit", "getyourguide", "shares21", "julius", "six", "lgt")
    for key in keys:
        cfg["adapters"][key].update(enabled=True, permission_note="", decision_note="")
    assert set(keys) <= {s.key for s in adapters.enabled_adapters(cfg)}
    assert next(s for s in adapters.REGISTRY if s.key == "six").tier == ""


# --- robots.txt at run time ------------------------------------------------------------------------------

def test_a_disallowed_list_is_not_requested(conn, cfg):
    site = Site(robots="User-agent: *\nDisallow: /ey/\n", pages={EY_URL: "x"})
    s, jobs = adapters.fetch_list(conn, cfg, EY, site, sleep=site.sleep, run_id="run1")
    assert jobs == [] and "robots.txt of careers.ey.com disallows" in s.error and not s.complete
    assert [u for _, u in site.requests] == ["https://careers.ey.com/robots.txt"]


@pytest.mark.parametrize("failure, expect_list", [(FetchError("HTTP 404", 404), True), (FetchError("HTTP 410", 410), True), (FetchError("HTTP 401", 401), True),
                                                   (FetchError("HTTP 500", 500), False), (FetchError("HTTP 403", 403), False),
                                                   (FetchError("network error: timed out"), False)])
def test_a_missing_robots_file_allows_but_an_unreadable_one_stops(conn, cfg, failure, expect_list):
    site = ey_site(5, robots=failure)
    s, jobs = adapters.fetch_list(conn, cfg, EY, site, sleep=site.sleep, run_id="run1")
    if expect_list:
        assert s.error is None and s.found == 5
    else:
        assert jobs == [] and "robots.txt of careers.ey.com could not be read" in s.error
        assert [u for _, u in site.requests] == ["https://careers.ey.com/robots.txt"]


def test_the_crawl_delay_of_robots_txt_is_the_pause_when_it_is_longer(conn, cfg):
    site = ey_site(60, robots="User-agent: *\nCrawl-delay: 12\n")
    adapters.fetch_list(conn, cfg, EY, site, sleep=site.sleep, run_id="run1")
    assert site.pauses == [5, 12, 12, 12]                         # robots.txt itself: the configured pause; then the crawl-delay
    short = ey_site(30, robots="User-agent: *\nCrawl-delay: 2\n")
    adapters.fetch_list(conn, cfg, EY, short, sleep=short.sleep)
    assert short.pauses == [5, 5, 5]                              # never shorter than adapters.delay_seconds


def test_robots_txt_is_read_once_per_call(conn, cfg):
    site = ey_site(60)
    adapters.fetch_list(conn, cfg, EY, site, sleep=lambda s: None, run_id="run1")
    assert [u for _, u in site.requests].count("https://careers.ey.com/robots.txt") == 1
    filter_new_jobs(conn, cfg, "run1")
    details = Site(default=EY_DETAIL)
    adapters.fetch_details(conn, cfg, EY, details, sleep=lambda s: None, now=NOW, limit=3)
    assert [u for _, u in details.requests].count("https://careers.ey.com/robots.txt") == 1 and len(details.requests) == 4


def test_no_robots_request_when_there_is_nothing_to_fetch(conn, cfg):
    site = Site()
    s = adapters.fetch_details(conn, cfg, EY, site, sleep=site.sleep, now=NOW)
    assert (s.attempted, s.stopped) == (0, None) and site.events == []


def test_disallowed_job_pages_stop_the_detail_step_without_a_request(conn, cfg):
    adapters.fetch_list(conn, cfg, EY, ey_site(30), sleep=lambda s: None, run_id="run1")
    filter_new_jobs(conn, cfg, "run1")
    site = Site(robots="User-agent: *\nDisallow: /ey/job/\n", default=EY_DETAIL)
    s = adapters.fetch_details(conn, cfg, EY, site, sleep=site.sleep, now=NOW)
    assert s.fetched == 0 and s.attempted == 0 and "robots.txt of careers.ey.com disallows" in s.stopped
    assert [u for _, u in site.requests] == ["https://careers.ey.com/robots.txt"]
    assert conn.execute("SELECT COUNT(*) FROM description_fetches").fetchone()[0] == 0          # tried again once robots.txt allows


def test_an_unreadable_robots_file_stops_the_detail_step(conn, cfg):
    adapters.fetch_list(conn, cfg, EY, ey_site(30), sleep=lambda s: None, run_id="run1")
    filter_new_jobs(conn, cfg, "run1")
    site = Site(robots=FetchError("HTTP 503", 503), default=EY_DETAIL)
    s = adapters.fetch_details(conn, cfg, EY, site, sleep=site.sleep, now=NOW)
    assert s.attempted == 0 and "could not be read" in s.stopped and len(site.requests) == 1


def test_adapters_without_check_robots_keep_their_old_pacing(conn, tmp_path, monkeypatch):
    six = next(s for s in adapters.REGISTRY if s.key == "six")
    assert not six.check_robots
    cfg = make_cfg(tmp_path, monkeypatch, "six")
    pages = []

    def fetch(url, data=None):
        pages.append(url)
        return re.sub(r"<small[^>]*>[^<]*</small>", "", fx("six", "list.html"))     # no "+N more" row: no job page is read here
    adapters.fetch_list(conn, cfg, six, fetch, sleep=lambda s: None, run_id="run1")
    assert pages == [cfg["adapters"]["six"]["list_url"]]                  # no robots.txt request


# --- job hosts, empty lists, registry ---------------------------------------------------------------------

def test_a_stored_job_url_may_be_on_a_job_host_but_a_request_may_not():
    scandit = next(s for s in adapters.REGISTRY if s.key == "scandit")
    adapters._check_job_url(scandit, "https://www.scandit.com/careers/job-description/?gh_jid=1")
    adapters._check_job_url(scandit, "https://boards-api.greenhouse.io/x")
    for bad in ("http://www.scandit.com/x", "https://evil.example.com/x", "https://www.scandit.com.evil.example.com/x"):
        with pytest.raises(FetchError):
            adapters._check_job_url(scandit, bad)
    with pytest.raises(FetchError):
        adapters._check_url(scandit, "https://www.scandit.com/careers/")                       # requests stay on the spec host
    with pytest.raises(FetchError):
        adapters._check_job_url(EY, "https://www.scandit.com/x")


def add_run(conn, run_id, found, error=None, label="Scandit job board"):
    db.start_run(conn, run_id)
    with conn:
        conn.execute("INSERT INTO adapter_runs (run_id, adapter, found, new_jobs, error) VALUES (?, ?, ?, 0, ?)", (run_id, label, found, error))


def test_empty_reads_of_a_small_employer_raise_no_alert_but_failed_reads_do(conn, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, monkeypatch, "scandit")
    n = cfg["monitoring"]["zero_jobs_alert_after"]
    for i in range(n):
        add_run(conn, f"e{i}", 0)
    alerts.check_sources(conn, cfg, mail_read=False, now=NOW)
    assert [a for a in alerts.open_alerts(conn) if a["key"] == "adapter_zero:scandit"] == []
    assert alerts.adapter_problem(conn, "Scandit job board", n) is not None                 # the old rule would have fired
    for i in range(n):
        add_run(conn, f"f{i}", 0, error="Scandit job board: HTTP 500")
    alerts.check_sources(conn, cfg, mail_read=False, now=NOW)
    found = [a for a in alerts.open_alerts(conn) if a["key"] == "adapter_zero:scandit"]
    assert len(found) == 1 and "failed" in found[0]["message"] and "returned no jobs" not in found[0]["message"]


def test_a_mix_of_empty_and_failed_reads_is_not_an_alert_for_an_empty_ok_adapter(conn):
    add_run(conn, "a", 0)
    add_run(conn, "b", 0, error="x")
    add_run(conn, "c", 0)
    assert alerts.adapter_problem(conn, "Scandit job board", 3, empty_ok=True) is None
    assert alerts.adapter_problem(conn, "Scandit job board", 3) is not None


def test_the_registry_has_unique_keys_and_sources_and_the_new_readers():
    keys = [s.key for s in adapters.REGISTRY]
    assert len(set(keys)) == len(keys) and len({s.source for s in adapters.REGISTRY}) == len(keys) and len({s.label for s in adapters.REGISTRY}) == len(keys)
    assert keys == ["six", "lgt", "sygnum", "julius", "swisscom", "vontobel", "temenos", "hitachienergy", "novartis", "deutschebank", "swissre", "ey", "zurich", "scandit", "getyourguide", "shares21"]
    assert all(s.source == f"{s.key}_site" for s in adapters.REGISTRY)
    assert [s.key for s in adapters.REGISTRY if s.empty_is_normal] == ["temenos", "scandit", "getyourguide", "shares21"]
    assert [s.key for s in adapters.REGISTRY if s.tier == "T2"] == ["swissre", "ey", "zurich"]


SWITCHED_ON = ("swisscom", "vontobel", "hitachienergy", "novartis", "deutschebank")


def test_the_example_config_has_every_reader_off_except_the_five_workday_description_sources_with_a_list_on_its_host():
    import yaml
    from app.config import EXAMPLE_PATH
    shipped = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))["adapters"]      # the conftest switches the five readers off in load_config()
    cfg = load_config()
    for spec in adapters.REGISTRY:
        s = cfg["adapters"][spec.key]
        if spec.key in SWITCHED_ON:          # 4 Nov 2026: the user switched these on, their purpose is finding job descriptions
            assert shipped[spec.key]["enabled"] is True and s["permission_note"].startswith("T1:") and spec.tier == "T1"
        else:
            assert s["enabled"] is False and s["permission_note"] == ""
        if spec.key not in ("six", "lgt", "sygnum", "julius"):
            assert s["decision_note"] == "" and s["list_url"].startswith(f"https://{spec.host}/")
            assert all(key in s for key in spec.list_filters)
    assert cfg["adapters"]["vontobel"]["facets"] == {"Location": ["187134fccb084a0ea9b4b95f23890dbe"]}
    assert cfg["adapters"]["swisscom"]["facets"] == {} and cfg["adapters"]["temenos"]["facets"] == {}
    swiss = ["187134fccb084a0ea9b4b95f23890dbe"]
    assert cfg["adapters"]["hitachienergy"]["facets"] == {"locationCountry": swiss} and cfg["adapters"]["novartis"]["facets"] == {"locationCountry": swiss}
    assert cfg["adapters"]["deutschebank"]["facets"] == {"Country": swiss}
    assert cfg["adapters"]["ey"]["list_url"].startswith("https://careers.ey.com/ey/search/") and cfg["adapters"]["ey"]["country_codes"] == ["CH"]


# --- config checks ---------------------------------------------------------------------------------------

def write(tmp_path, text):
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("text, message", [
    ("adapters:\n  ey:\n    decision_note: 5\n", "adapters.ey.decision_note must be a string"),
    ("adapters:\n  swisscom:\n    facets: {Location: abc}\n", "adapters.swisscom.facets"),
    ("adapters:\n  swisscom:\n    facets: {Location: [1]}\n", "adapters.swisscom.facets"),
    ("adapters:\n  swisscom:\n    facets: [a]\n", "adapters.swisscom.facets"),
    ("adapters:\n  scandit:\n    countries: Switzerland\n", "adapters.scandit.countries must be a list of strings"),
    ("adapters:\n  scandit:\n    places: [1]\n", "adapters.scandit.places must be a list of strings"),
    ("adapters:\n  swissre:\n    country_codes: CH\n", "adapters.swissre.country_codes must be a list of strings"),
])
def test_a_wrong_reader_setting_is_refused(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_the_user_may_set_a_facet_on_a_tenant_that_has_none_and_clear_the_default(tmp_path):
    cfg = load_config(write(tmp_path, 'adapters:\n  swisscom:\n    facets: {Location: ["abc"], company: ["x"]}\n  vontobel:\n    facets: {}\n'))
    assert cfg["adapters"]["swisscom"]["facets"] == {"Location": ["abc"], "company": ["x"]}
    assert cfg["adapters"]["vontobel"]["facets"] == {}
