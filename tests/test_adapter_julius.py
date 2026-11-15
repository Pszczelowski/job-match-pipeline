"""Julius Baer career-site adapter (Workday JSON interface), on trimmed real answers (tests/fixtures/julius_site).

Real: the first two list pages (20 postings each, `total` 68 on the first and 0 on the second, one placeholder posting without
title and path) and the job page of the job that is also in the alert-mail fixture (tests/fixtures/julius). Longer lists are
built from the real postings. No test touches the network.
"""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import adapters, alerts, cli, db
from app.adapters import AdapterDisabled, PageError, julius
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.descriptions_fetch import FetchError, JsonPost, PageFetcher
from app.filters.runner import filter_new_jobs
from app.parsers import julius as alert_parser
from app.pipeline import prepare
from app.travel import OsrmProvider, Providers, TransportProvider
from tests.test_fetch_descriptions import _Resp
from tests.test_location import Replay, TODAY
from tests.test_mailbox import FakeIMAP

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
PAGE1, PAGE2, DETAIL = [(FIX / "julius_site" / n).read_text(encoding="utf-8") for n in ("list_page1.json", "list_page2.json", "detail.json")]
ALERT = (FIX / "julius" / "alert_2026-11-02.html").read_text(encoding="utf-8")
LIST_URL = "https://juliusbaer.wd3.myworkdayjobs.com/wday/cxs/juliusbaer/External/jobs"
FACET = "187134fccb084a0ea9b4b95f23890dbe"
MAIL_JOB_URL = "https://juliusbaer.wd3.myworkdayjobs.com/External/job/Geneva/Account-Manager--Turkey--100---f-m-d-_r-20213-1"
MAIL_JOB_DETAIL = "https://juliusbaer.wd3.myworkdayjobs.com/wday/cxs/juliusbaer/External/job/Geneva/Account-Manager--Turkey--100---f-m-d-_r-20213-1"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    c["adapters"]["julius"]["enabled"] = True
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.start_run(c, "run1")
    yield c
    c.close()


def synthetic(offset, total, first=1000):
    """A list answer as Workday gives it: `total` only on the first page, 20 postings per page."""
    postings = [{"title": f"Analyst {i} (f/m/d)", "externalPath": f"/job/Zurich/Analyst-{i}--f-m-d-_r-{i}-1", "locationsText": "Zurich",
                 "postedOn": "Posted Today", "bulletFields": [f"r-{i}"]} for i in range(first + offset, first + min(offset + 20, total))]
    return json.dumps({"total": total if offset == 0 else 0, "jobPostings": postings})


class Api:
    """The list as POSTs (JSON body) answered from `lists[offset]`, the job pages as GETs."""

    def __init__(self, lists=None, detail=DETAIL, fail=None, pages=None):
        self.lists = lists if lists is not None else {0: PAGE1, 20: PAGE2}
        self.detail, self.fail, self.pages, self.calls = detail, fail or {}, pages or {}, []

    def __call__(self, url, data=None):
        self.calls.append((url, data))
        key = data["offset"] if data is not None else url
        if key in self.fail:
            raise self.fail[key]
        if data is not None:
            return self.lists[data["offset"]]
        return self.pages.get(url, self.detail)


def three_pages(total=45):
    return Api(lists={o: synthetic(o, total) for o in range(0, total, 20)})


# --- the list parser --------------------------------------------------------------------------------

def test_the_real_first_page_gives_twenty_jobs_with_clean_urls_and_ids():
    jobs = julius.parse_list(PAGE1, {})
    assert len(jobs) == 20
    first = jobs[0]
    assert first.external_id == "r-20156-1" and first.reference == "R-20156" and first.locations == ["Zurich"]
    assert first.url == "https://juliusbaer.wd3.myworkdayjobs.com/External/job/Zurich/Strategic-Pricing---Revenue-Manager-100---Zurich-based--f-m-d-_r-20156-1"
    assert first.title == "Strategic Pricing & Revenue Manager 100%, Zurich-based (f/m/d)"
    assert (jobs[1].external_id, jobs[1].locations) == ("r-20213-1", ["Geneva"])
    assert jobs[2].external_id == "r-20236" and jobs[2].reference == "R-20236"           # a job without the "-1" suffix


def test_the_job_of_the_alert_mail_and_the_site_are_the_same_job():
    mail = alert_parser.parse_alert(ALERT).jobs[0]
    site = next(j for j in julius.parse_list(PAGE1, {}) if j.external_id == mail.external_id)
    assert (site.external_id, site.url, site.title, site.locations, site.reference) == \
        (mail.external_id, mail.url, mail.title, [mail.location], mail.reference)


def test_a_posting_without_title_and_path_is_skipped_and_the_rest_is_read():
    postings = json.loads(PAGE2)["jobPostings"]
    assert [p for p in postings if "externalPath" not in p] == [{"bulletFields": ["r-20059"]}]       # as the site really answered
    jobs = julius.parse_list(PAGE2, {})
    assert len(jobs) == 19 and all(j.external_id != "r-20059" for j in jobs)


def test_several_places_in_the_list_take_the_main_place_of_the_path():
    page = json.loads(PAGE1)
    page["jobPostings"][1]["locationsText"] = "2 Locations"
    page["jobPostings"][0].pop("locationsText")
    jobs = julius.parse_list(json.dumps(page), {})
    assert jobs[0].locations == ["Zurich"] and jobs[1].locations == ["Geneva"]


def test_the_placeholder_is_not_a_job_when_it_is_the_only_thing_on_the_page():
    only = json.dumps({"total": 1, "jobPostings": [{"bulletFields": ["r-1"]}]})
    with pytest.raises(PageError, match="site changed"):
        julius.parse_list(only, {})


@pytest.mark.parametrize("page, message", [
    ("<html>Please verify you are human</html>", "not JSON"),
    ("[]", "not a JSON object"),
    ('{"jobs": []}', "no 'total' and 'jobPostings'"),
    ('{"total": "68", "jobPostings": []}', "no 'total' and 'jobPostings'"),
    ('{"total": 1, "jobPostings": [{"title": "Analyst", "externalPath": "/somewhere/else"}]}', "no usable title and path"),
    ('{"total": 1, "jobPostings": [{"externalPath": "/job/Zurich/x_r-1-1"}]}', "no usable title and path"),
    ('{"total": 1, "jobPostings": [{"title": "Analyst", "externalPath": "/job/Zurich/noid"}]}', "not a job path"),
    ('{"total": 1, "jobPostings": ["x"]}', "not an object"),
])
def test_a_changed_answer_is_an_error_never_an_empty_list(page, message):
    with pytest.raises(PageError, match=message):
        julius.parse_list(page, {})


def test_an_empty_list_is_a_result_and_not_an_error():
    assert julius.parse_list('{"total": 0, "jobPostings": []}', {}) == []


# --- the requests -----------------------------------------------------------------------------------

def test_the_first_request_is_the_json_post_of_the_career_page():
    url, body = julius.list_requests(LIST_URL, {"country_facet": FACET}, [])
    assert url == LIST_URL and isinstance(body, JsonPost)
    assert dict(body) == {"appliedFacets": {"Location_Country": [FACET]}, "limit": 20, "offset": 0, "searchText": ""}


def test_an_empty_country_facet_reads_every_country():
    assert dict(julius.list_requests(LIST_URL, {"country_facet": " "}, [])[1])["appliedFacets"] == {}


def test_the_offset_is_the_number_of_postings_read_and_the_total_is_the_one_of_the_first_page():
    s = {"country_facet": FACET}
    assert julius.list_requests(LIST_URL, s, [PAGE1])[1]["offset"] == 20
    assert julius.list_requests(LIST_URL, s, [PAGE1, PAGE2])[1]["offset"] == 40               # the second page says total 0
    assert julius.list_requests(LIST_URL, s, [synthetic(0, 40), synthetic(20, 40)]) is None


def test_a_list_that_ends_early_is_an_error():
    with pytest.raises(PageError, match="ended at 20 of 68"):
        julius.list_requests(LIST_URL, {"country_facet": FACET}, [PAGE1, json.dumps({"total": 0, "jobPostings": []})])
    with pytest.raises(PageError, match="ended at 0 of 5"):
        julius.list_requests(LIST_URL, {"country_facet": FACET}, [json.dumps({"total": 5, "jobPostings": []})])


# --- reading the whole list -------------------------------------------------------------------------

def test_all_pages_are_read_with_a_pause_and_stored_as_julius_baer_jobs(conn, cfg):
    api, pauses = three_pages(45), []
    s, jobs = adapters.fetch_list(conn, cfg, julius.SPEC, api, sleep=pauses.append, run_id="run1")
    assert (s.found, s.new, s.error, s.complete) == (45, 45, None, True)
    assert [d["offset"] for _, d in api.calls] == [0, 20, 40] and all(u == LIST_URL for u, _ in api.calls)
    assert all(d["limit"] == 20 and d["appliedFacets"] == {"Location_Country": [FACET]} for _, d in api.calls)
    assert pauses == [cfg["adapters"]["delay_seconds"]] * 2
    rows = conn.execute("SELECT j.company, s.source, s.reference, j.description_missing FROM jobs j JOIN job_sources s ON s.job_id = j.id").fetchall()
    assert {(r[0], r[1], r[3]) for r in rows} == {("Julius Baer", "julius_site", 1)} and len(rows) == 45
    assert all(r[2].startswith("R-") for r in rows)


def test_a_second_read_changes_nothing(conn, cfg):
    adapters.fetch_list(conn, cfg, julius.SPEC, three_pages(25), sleep=lambda s: None, run_id="run1")
    s, _ = adapters.fetch_list(conn, cfg, julius.SPEC, three_pages(25), sleep=lambda s: None, run_id="run1")
    assert (s.found, s.new, s.outcomes) == (25, 0, {"unchanged": 25})


def test_the_real_pages_are_read_with_the_placeholder_skipped(conn, cfg):
    api = Api(lists={0: PAGE1.replace('"total": 68', '"total": 40'), 20: PAGE2})          # the real second page says total 0
    s, _ = adapters.fetch_list(conn, cfg, julius.SPEC, api, sleep=lambda s: None, run_id="run1")
    assert (s.error, s.found, s.complete) == (None, 39, True) and len(api.calls) == 2
    # two pairs of real postings share a title and a place (r-20262-1 / r-20060-2, and a Remote Access Engineer pair): the
    # dedup key (company, title, places) makes each pair one job, marked as a repost, as it does for every source
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 37 and s.outcomes == {"new": 37, "seen_again": 2}


def test_a_dry_run_stores_nothing(conn, cfg):
    s, jobs = adapters.fetch_list(conn, cfg, julius.SPEC, three_pages(25), sleep=lambda s: None, store=False)
    assert s.found == 25 and len(jobs) == 25 and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_a_list_cut_short_by_max_list_pages_is_not_complete_and_closes_nothing(conn, cfg):
    cfg["adapters"]["max_list_pages"] = 2
    api = three_pages(45)
    s, _ = adapters.fetch_list(conn, cfg, julius.SPEC, api, sleep=lambda s: None, run_id="run1")
    assert (s.found, s.complete) == (40, False) and len(api.calls) == 2
    cfg["adapters"]["max_list_pages"] = 3
    assert adapters.fetch_list(conn, cfg, julius.SPEC, three_pages(45), sleep=lambda s: None, run_id="run1")[0].complete


def test_a_job_gone_from_two_complete_reads_is_marked_closed_and_listed_again_it_reopens(conn, cfg):
    full, shorter = three_pages(45), Api(lists={o: synthetic(o, 44) for o in (0, 20, 40)})
    adapters.fetch_list(conn, cfg, julius.SPEC, full, sleep=lambda s: None, run_id="run1")
    gone = "r-1044-1"                                              # the last posting of the full list
    for _ in range(2):
        s, _ = adapters.fetch_list(conn, cfg, julius.SPEC, shorter, sleep=lambda s: None, run_id="run1")
    assert s.complete and s.closed == 1
    assert conn.execute("SELECT j.closed_at FROM jobs j JOIN job_sources s ON s.job_id = j.id WHERE s.external_id = ?", (gone,)).fetchone()[0]
    s, _ = adapters.fetch_list(conn, cfg, julius.SPEC, full, sleep=lambda s: None, run_id="run1")
    assert s.reopened == 1


def test_a_refusal_on_a_later_list_page_is_reported_and_stores_nothing(conn, cfg):
    s, jobs = adapters.fetch_list(conn, cfg, julius.SPEC, Api(lists={0: synthetic(0, 45), 20: "", 40: ""}, fail={20: FetchError("HTTP 429", 429)}),
                                  sleep=lambda s: None)
    assert jobs == [] and "HTTP 429" in s.error and not s.complete and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_a_check_page_instead_of_the_list_is_an_error(conn, cfg):
    s, jobs = adapters.fetch_list(conn, cfg, julius.SPEC, Api(lists={0: "<html>Checking your browser</html>"}), sleep=lambda s: None)
    assert jobs == [] and "not JSON" in s.error


def test_only_the_career_site_host_is_requested(conn, cfg):
    cfg["adapters"]["julius"]["list_url"] = "https://evil.example.com/wday/cxs/juliusbaer/External/jobs"
    api = Api()
    s, jobs = adapters.fetch_list(conn, cfg, julius.SPEC, api)
    assert api.calls == [] and jobs == [] and f"not a page on {julius.HOST}" in s.error


def test_it_is_off_until_it_is_switched_on_and_needs_no_permission_note(conn, cfg, monkeypatch):
    assert load_config()["adapters"]["julius"]["enabled"] is False and adapters.enabled_adapters(load_config()) == []
    cfg["adapters"]["julius"]["enabled"] = False
    api = Api()
    with pytest.raises(AdapterDisabled):
        adapters.fetch_list(conn, cfg, julius.SPEC, api)
    assert api.calls == []
    cfg["adapters"]["julius"].update(enabled=True, permission_note="", decision_note="")
    assert julius.SPEC in adapters.enabled_adapters(cfg)                  # the user's decision replaces the note


# --- the job page -----------------------------------------------------------------------------------

def test_the_address_of_the_job_page_is_the_json_address_of_the_public_page():
    assert julius.detail_url(MAIL_JOB_URL) == MAIL_JOB_DETAIL
    assert julius.detail_url(MAIL_JOB_URL.replace("/External/", "/en-US/External/")) == MAIL_JOB_DETAIL
    for bad in ("https://example.com/External/job/x/y_r-1", "https://juliusbaer.wd3.myworkdayjobs.com/External", "https://juliusbaer.wd3.myworkdayjobs.com/Other/job/x/y_r-1"):
        with pytest.raises(FetchError, match="not a Julius Baer job page"):
            julius.detail_url(bad)


def test_the_text_keeps_the_job_sections_and_drops_the_standard_paragraphs():
    d = julius.parse_detail(DETAIL)
    assert "YOUR CHALLENGE" in d.text and "YOUR PROFILE" in d.text and "Excellent language skills" in d.text
    assert "Client Administration & Management" in d.text                    # a heading written letter by letter
    assert "• Ensures that accurate" in d.text and "<" not in d.text and "&amp;" not in d.text
    assert "we celebrate and value" not in d.text and "looking forward to receiving" not in d.text and "job alert" not in d.text
    assert d.text.startswith("In addition to the Assistant Relationship Manager")
    assert d.posted == "2026-11-02"


def test_a_job_page_without_a_posting_date_has_none():
    info = json.loads(DETAIL)
    info["jobPostingInfo"].pop("startDate")
    assert julius.parse_detail(json.dumps(info)).posted == ""


@pytest.mark.parametrize("page, message", [
    ("<html>maintenance</html>", "not JSON"),
    ('{"jobs": []}', "no 'jobPostingInfo.jobDescription'"),
    ('{"jobPostingInfo": {"title": "x"}}', "no 'jobPostingInfo.jobDescription'"),
    ('{"jobPostingInfo": {"jobDescription": "<p>At Julius Baer, we celebrate it.</p>"}}', "empty description"),
])
def test_a_changed_job_page_is_an_error(page, message):
    with pytest.raises(PageError, match=message):
        julius.parse_detail(page)


def listed_and_filtered(conn, cfg, api=None):
    adapters.fetch_list(conn, cfg, julius.SPEC, api or three_pages(25), sleep=lambda s: None, run_id="run1")
    filter_new_jobs(conn, cfg, "run1")


def test_job_pages_are_fetched_only_after_the_first_filters_at_the_json_address(conn, cfg):
    adapters.fetch_list(conn, cfg, julius.SPEC, three_pages(25), sleep=lambda s: None, run_id="run1")
    assert adapters.eligible_jobs(conn, julius.SPEC) == []                      # not filtered yet: nothing is requested
    filter_new_jobs(conn, cfg, "run1")
    todo = adapters.eligible_jobs(conn, julius.SPEC)
    assert len(todo) >= 1
    api, pauses = three_pages(25), []
    s = adapters.fetch_details(conn, cfg, julius.SPEC, api, sleep=pauses.append, now=NOW, run_id="run1", limit=2)
    assert s.fetched == 2 and s.failed == 0 and len(api.calls) == 2 and pauses == [cfg["adapters"]["delay_seconds"]]
    assert all(u.startswith("https://juliusbaer.wd3.myworkdayjobs.com/wday/cxs/juliusbaer/External/job/Zurich/") and d is None for u, d in api.calls)
    assert conn.execute("SELECT COUNT(*) FROM description_log WHERE source = 'julius_site'").fetchone()[0] == 2
    row = conn.execute("SELECT description, posted_on FROM jobs WHERE description_missing = 0 LIMIT 1").fetchone()
    assert row["description"].startswith("In addition to") and row["posted_on"] == "2026-11-02"


def test_each_job_page_is_requested_once(conn, cfg):
    listed_and_filtered(conn, cfg)
    adapters.fetch_details(conn, cfg, julius.SPEC, Api(), sleep=lambda s: None, now=NOW)
    again = Api()
    assert adapters.fetch_details(conn, cfg, julius.SPEC, again, sleep=lambda s: None, now=NOW).attempted == 0 and again.calls == []


def test_the_daily_cap_is_kept(conn, cfg):
    cfg["adapters"]["daily_cap"] = 2
    listed_and_filtered(conn, cfg)
    api = Api()
    s = adapters.fetch_details(conn, cfg, julius.SPEC, api, sleep=lambda s: None, now=NOW)
    assert s.fetched == 2 and "daily cap of 2" in s.stopped and len(api.calls) == 2


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_a_refusal_stops_the_run_with_no_retry(conn, cfg, status):
    listed_and_filtered(conn, cfg)
    urls = [r["url"] for r in conn.execute("SELECT url FROM job_sources ORDER BY id")]
    api = Api(fail={julius.detail_url(urls[1]): FetchError(f"HTTP {status}", status)})
    s = adapters.fetch_details(conn, cfg, julius.SPEC, api, sleep=lambda s: None, now=NOW)
    assert s.fetched == 1 and s.failed == 1 and f"HTTP {status}" in s.stopped and len(api.calls) == 2      # the third is not asked
    assert conn.execute("SELECT status, http_status FROM description_fetches WHERE status = 'error'").fetchone()[1] == status


def test_a_network_error_stops_the_run(conn, cfg):
    listed_and_filtered(conn, cfg)
    url = julius.detail_url(conn.execute("SELECT url FROM job_sources ORDER BY id").fetchone()["url"])
    api = Api(fail={url: FetchError("network error: URLError")})
    s = adapters.fetch_details(conn, cfg, julius.SPEC, api, sleep=lambda s: None, now=NOW)
    assert s.attempted == 1 and "network error" in s.stopped and len(api.calls) == 1


def test_a_closed_job_page_and_a_changed_page_do_not_stop_the_run(conn, cfg):
    listed_and_filtered(conn, cfg)
    urls = [julius.detail_url(r["url"]) for r in conn.execute("SELECT url FROM job_sources ORDER BY id")]
    api = Api(fail={urls[0]: FetchError("HTTP 404", 404)}, pages={urls[1]: "<html>redesigned</html>"})
    s = adapters.fetch_details(conn, cfg, julius.SPEC, api, sleep=lambda s: None, now=NOW, limit=3)
    assert (s.failed, s.fetched, s.stopped) == (2, 1, None) and len(api.calls) == 3


# --- mail and site are one job ----------------------------------------------------------------------

def mail_job():
    return alert_parser.to_job(alert_parser.parse_alert(ALERT).jobs[0], "<jb1@x>")


def test_a_job_from_the_mail_and_the_site_is_one_job_whichever_comes_first(conn, cfg):
    first = ingest_job(conn, mail_job(), "run1")
    s, _ = adapters.fetch_list(conn, cfg, julius.SPEC, Api(lists={0: PAGE1.replace('"total": 68', '"total": 20')}), sleep=lambda s: None, run_id="run1")
    assert s.outcomes.get("linked") == 1 and s.new == 18                                   # r-20213-1 is the job of the mail
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 19                   # 20 postings, one pair with the same title and place
    sources = conn.execute("SELECT source, external_id, url FROM job_sources WHERE job_id = ? ORDER BY id", (first.job_id,)).fetchall()
    assert [(r[0], r[1], r[2]) for r in sources] == [("julius_alert", "r-20213-1", MAIL_JOB_URL), ("julius_site", "r-20213-1", MAIL_JOB_URL)]
    assert conn.execute("SELECT repost FROM jobs WHERE id = ?", (first.job_id,)).fetchone()[0] == 0


def test_the_site_job_found_first_is_linked_by_the_mail(conn, cfg):
    adapters.fetch_list(conn, cfg, julius.SPEC, Api(lists={0: PAGE1.replace('"total": 68', '"total": 20')}), sleep=lambda s: None, run_id="run1")
    out = ingest_job(conn, mail_job(), "run1")
    assert out.outcome == "linked" and out.matched_by == "url"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 19
    assert conn.execute("SELECT COUNT(*) FROM job_sources WHERE job_id = ?", (out.job_id,)).fetchone()[0] == 2


def test_a_job_that_came_by_mail_gets_its_description_from_the_site_page(conn, cfg):
    out = ingest_job(conn, mail_job(), "run1")                                   # only in the mail: never listed
    filter_new_jobs(conn, cfg, "run1")
    assert [j["id"] for j in adapters.eligible_jobs(conn, julius.SPEC)] == [out.job_id]
    api = Api()
    s = adapters.fetch_details(conn, cfg, julius.SPEC, api, sleep=lambda s: None, now=NOW, run_id="run1")
    assert s.fetched == 1 and api.calls == [(MAIL_JOB_DETAIL, None)]
    job = conn.execute("SELECT description, description_missing FROM jobs WHERE id = ?", (out.job_id,)).fetchone()
    assert job["description_missing"] == 0 and "YOUR PROFILE" in job["description"]
    assert conn.execute("SELECT source FROM description_log WHERE job_id = ?", (out.job_id,)).fetchone()[0] == "julius_site"
    assert adapters.fetch_details(conn, cfg, julius.SPEC, Api(), sleep=lambda s: None, now=NOW).attempted == 0


def test_a_job_of_another_source_is_not_read_by_this_adapter(conn, cfg):
    from app.models import NormalizedJob
    ingest_job(conn, NormalizedJob(source="ubs_alert", external_id="1", title="Analyst", company="UBS", url="https://jobs.ubs.com/x", locations=["Zurich"]), "run1")
    filter_new_jobs(conn, cfg, "run1")
    assert adapters.eligible_jobs(conn, julius.SPEC) == []


def test_a_mail_job_missing_from_the_site_list_is_not_closed(conn, cfg):
    out = ingest_job(conn, mail_job(), "run1")
    for _ in range(3):
        adapters.fetch_list(conn, cfg, julius.SPEC, three_pages(25), sleep=lambda s: None, run_id="run1")
    assert conn.execute("SELECT closed_at FROM jobs WHERE id = ?", (out.job_id,)).fetchone()[0] is None


# --- inside prepare ---------------------------------------------------------------------------------

@pytest.fixture
def providers(cfg):
    replay = Replay()
    p = cfg["location"]["providers"]
    return Providers(TransportProvider(replay, p["transport_url"]), OsrmProvider(replay, p["osrm_url"]), today=TODAY)


def run_prepare(conn, cfg, providers, fetch, run_id="run1", now=NOW):
    return prepare(conn, cfg, providers=providers, prompt=("p", "v1"), mail_client_factory=lambda c: FakeIMAP({}),
                   page_fetcher=fetch, page_sleep=lambda s: None, run_id=run_id, now=now)


def test_prepare_lists_filters_and_fetches_job_pages(conn, cfg, providers):
    api = three_pages(25)
    r = run_prepare(conn, cfg, providers, api)
    row = conn.execute("SELECT adapter, found, new_jobs, error FROM adapter_runs WHERE adapter = 'Julius Baer career site'").fetchone()
    assert tuple(row) == ("Julius Baer career site", 25, 25, None)
    assert api.calls[0][1]["offset"] == 0 and any(d is None for _, d in api.calls)       # the list, then job pages of surviving jobs
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE description_missing = 0").fetchone()[0] >= 1
    assert r.batch is not None


def test_prepare_survives_a_site_that_fails_and_raises_the_adapter_alert_after_three_runs(conn, cfg, providers):
    for i in range(3):
        r = run_prepare(conn, cfg, providers, Api(lists={0: ""}, fail={0: FetchError("HTTP 503", 503)}), f"run{i}", NOW + timedelta(days=i))
        assert any("HTTP 503" in w for w in r.warnings)
        assert [a["key"] for a in alerts.open_alerts(conn)] == (["adapter_zero:julius"] if i == 2 else [])


# --- the fetcher: JSON ------------------------------------------------------------------------------

def test_page_fetcher_posts_json_and_asks_for_json(monkeypatch):
    seen = []

    def fake_open(self, request, data=None, timeout=None):
        seen.append((request.get_method(), request.data, request.get_header("Content-type"), request.get_header("Accept")))
        return _Resp(b"{}")
    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)
    fetch = PageFetcher("UA/1")
    fetch(LIST_URL, JsonPost({"appliedFacets": {"Location_Country": [FACET]}, "limit": 20, "offset": 0, "searchText": ""}))
    fetch(MAIL_JOB_DETAIL)
    fetch("https://jobs.six-group.com/x")
    fetch("https://portal.example/list", {"a": "b"})
    method, body, ctype, accept = seen[0]
    assert method == "POST" and ctype == "application/json" and accept == "application/json"
    assert json.loads(body) == {"appliedFacets": {"Location_Country": [FACET]}, "limit": 20, "offset": 0, "searchText": ""}
    assert seen[1][0] == "GET" and seen[1][3] == "application/json"                    # the job page of the JSON interface
    assert seen[2][3] == "text/html" and seen[3][2] == "application/x-www-form-urlencoded" and seen[3][3] == "text/html"
    assert urllib.parse.parse_qs(seen[3][1].decode()) == {"a": ["b"]}


# --- config and command -----------------------------------------------------------------------------

def test_the_defaults_are_off_with_the_swiss_facet():
    j = load_config()["adapters"]["julius"]
    assert j["enabled"] is False and j["country_facet"] == FACET and j["list_url"] == LIST_URL and j["decision_note"] == ""


@pytest.mark.parametrize("key, value, message", [("country_facet", 5, "country_facet must be a string"),
                                                 ("decision_note", ["x"], "decision_note must be a string"),
                                                 ("list_url", "http://juliusbaer.wd3.myworkdayjobs.com/x", "https"),
                                                 ("enabled", "yes", "enabled must be true or false")])
def test_the_julius_settings_are_checked(monkeypatch, tmp_path, key, value, message):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    bad = tmp_path / "c.yaml"
    bad.write_text(f"adapters:\n  julius:\n    {key}: {json.dumps(value)}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(bad)


def test_the_decision_note_is_accepted(monkeypatch, tmp_path):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    ok = tmp_path / "c.yaml"
    ok.write_text("adapters:\n  julius:\n    enabled: true\n    decision_note: \"2 Nov 2026: switched on by the user\"\n", encoding="utf-8")
    assert load_config(ok)["adapters"]["julius"]["decision_note"].startswith("2 Nov 2026")


def test_the_alert_key_and_the_report_line_name_the_adapter():
    assert julius.SPEC.key == "julius" and julius.SPEC.label == "Julius Baer career site" and julius.SPEC in adapters.REGISTRY


def test_check_adapter_reads_the_list_and_one_job_page_and_stores_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(f"paths:\n  database: {(tmp_path / 'jobs.db').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
                    f"  logs: {(tmp_path / 'logs').as_posix()}\n  lock: {(tmp_path / 'run.lock').as_posix()}\n"
                    "adapters:\n  julius:\n    enabled: true\n", encoding="utf-8")
    api = Api(lists={0: synthetic(0, 5)})
    assert cli.main(["--config", str(path), "check-adapter", "julius", "--detail"], page_fetcher=api) == 0
    out = capsys.readouterr().out
    assert "Julius Baer career site: 5 job(s) listed (nothing stored)" in out and "r-1000-1" in out and "first job page:" in out
    assert api.calls[-1][0].startswith("https://juliusbaer.wd3.myworkdayjobs.com/wday/cxs/juliusbaer/External/job/Zurich/Analyst-1000")
    assert db.connect(tmp_path / "jobs.db").execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
