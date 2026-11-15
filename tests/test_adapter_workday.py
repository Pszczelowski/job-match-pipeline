"""Workday readers (Swisscom, Vontobel, Temenos, v0.70.0) on trimmed real answers (tests/fixtures/workday, 4 Nov 2026).

Real: a few postings of each list (names of recruiters in the job pages are REDACTED) and one job page each of Swisscom and
Vontobel. Longer lists are built from the shape of the real postings. No test touches the network.
"""
import json
from datetime import datetime, timezone

import pytest

from app import adapters, db
from app.adapters import PageError, workday
from app.descriptions_fetch import FetchError, JsonPost
from app.filters.runner import filter_new_jobs
from tests.ats_support import Site, fx, make_conn, make_cfg

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
SWISSCOM = next(s for s in adapters.REGISTRY if s.key == "swisscom")
VONTOBEL = next(s for s in adapters.REGISTRY if s.key == "vontobel")
TEMENOS = next(s for s in adapters.REGISTRY if s.key == "temenos")
SC_LIST, SC_DETAIL = fx("workday", "swisscom_list.json"), fx("workday", "swisscom_detail.json")
VT_LIST, VT_ALL, VT_DETAIL = fx("workday", "vontobel_list.json"), fx("workday", "vontobel_list_all_countries.json"), fx("workday", "vontobel_detail.json")
TM_LIST = fx("workday", "temenos_list.json")
SC_URL = "https://swisscom.wd103.myworkdayjobs.com/wday/cxs/swisscom/SwisscomExternalCareers/jobs"
FACET = {"Location": ["187134fccb084a0ea9b4b95f23890dbe"]}
SWISS = {"countries": ["Switzerland"], "places": ["Zurich", "Zürich", "Bern", "S. Antonino"]}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return make_cfg(tmp_path, monkeypatch, "swisscom", "vontobel", "temenos")


@pytest.fixture
def conn(tmp_path):
    c = make_conn(tmp_path)
    yield c
    c.close()


def synthetic(offset, total, first=1000, foreign_every=0):
    """A list answer as Workday gives it: `total` only on the first page, 20 postings per page."""
    postings = []
    for i in range(first + offset, first + min(offset + 20, total)):
        far = bool(foreign_every) and i % foreign_every == 0
        postings.append({"title": f"Analyst {i}", "externalPath": f"/job/{'Hanoi' if far else 'Zurich'}/Analyst-{i}_R-{i}-1",
                         "locationsText": "Hanoi" if far else "Zurich", "postedOn": "Posted Today", "bulletFields": [f"R-{i}"]})
    return json.dumps({"total": total if offset == 0 else 0, "jobPostings": postings})


def lists(total, **kw):
    return {o: synthetic(o, total, **kw) for o in range(0, total, 20)}


# --- the list parser ------------------------------------------------------------------------------------

def test_real_swisscom_postings_are_kept_with_ids_urls_places_and_references():
    jobs = SWISSCOM.parse_list(SC_LIST, {**SWISS, "facets": {}})
    assert [j.external_id for j in jobs] == ["r-0006447-1", "r-0006622", "r-0005985", "r-0006879-1"]
    first = jobs[0]
    assert first.url == "https://swisscom.wd103.myworkdayjobs.com/SwisscomExternalCareers/job/Bern/Data-Engineer---Geo-Platform_R-0006447-1"
    assert (first.title, first.reference) == ("Engineer - Geo Platform", "R-0006447")
    assert first.locations == ["Bern"]                              # "2 Locations": the main place of the path
    assert jobs[1].locations == ["Zurich"] and jobs[2].locations == ["S. Antonino"]


def test_real_vontobel_postings_keep_the_place_text_and_the_requisition():
    jobs = VONTOBEL.parse_list(VT_LIST, {"countries": [], "places": []})
    assert [j.external_id for j in jobs] == ["9545-1", "9539-2", "9268-2"]
    assert jobs[0].locations == ["Zürich / Splügenstrasse 5"] and jobs[0].reference == "9545"
    assert jobs[2].locations == ["Zrich"]                       # "2 Locations": the path place (umlauts are lost in Workday's path)
    assert jobs[0].url.startswith("https://vontobel.wd3.myworkdayjobs.com/Vontobel_External_Career/job/")


def test_without_a_country_facet_foreign_postings_are_dropped_by_country_and_place():
    assert TEMENOS.parse_list(TM_LIST, SWISS) == []                                 # London, a remote US job, Singapore
    assert len(TEMENOS.parse_list(TM_LIST, {"countries": [], "places": []})) == 4    # both lists empty keep all
    page = json.loads(VT_ALL)                                                       # Vontobel's own list without the facet
    kept = [j.external_id for j in VONTOBEL.parse_list(VT_ALL, {"countries": ["Switzerland"], "places": ["Zürich"]})]
    assert kept == ["9545-1", "9268-2"] and len(page["jobPostings"]) == 4      # Düsseldorf and Hong Kong are dropped; "2 Locations" by its path place (Zrich)


def test_the_country_is_matched_in_the_text_of_the_location():
    page = json.dumps({"total": 2, "jobPostings": [
        {"title": "A", "externalPath": "/job/Somewhere/A_R-1", "locationsText": "Pfäffikon, Switzerland"},
        {"title": "B", "externalPath": "/job/Elsewhere/B_R-2", "locationsText": "Paris, France"}]})
    assert [j.external_id for j in SWISSCOM.parse_list(page, {"countries": ["Switzerland"], "places": []})] == ["r-1"]


def test_an_empty_list_is_a_result_and_not_an_error():
    assert SWISSCOM.parse_list('{"total": 0, "jobPostings": []}', {}) == []


@pytest.mark.parametrize("page, message", [
    ("<html>Please verify you are human</html>", "not JSON"),
    ("[]", "not a JSON object"),
    ('{"jobs": []}', "no 'total' and 'jobPostings'"),
    ('{"total": "5", "jobPostings": []}', "no 'total' and 'jobPostings'"),
    ('{"total": 1, "jobPostings": [{"title": "A", "externalPath": "/somewhere/else"}]}', "no usable title and path"),
    ('{"total": 1, "jobPostings": [{"title": "A", "externalPath": "/job/Zurich/noid"}]}', "not a job path"),
    ('{"total": 1, "jobPostings": ["x"]}', "not an object"),
    ('{"total": 1, "jobPostings": [{"bulletFields": ["R-1"]}]}', "no posting"),
])
def test_a_changed_answer_is_an_error_never_an_empty_list(page, message):
    with pytest.raises(PageError, match=message):
        SWISSCOM.parse_list(page, {})


def test_the_messages_name_the_tenant():
    with pytest.raises(PageError, match="Vontobel career site"):
        VONTOBEL.parse_list("<html></html>", {})


# --- the requests ----------------------------------------------------------------------------------------

def test_the_first_request_is_the_json_post_with_the_facets_of_the_tenant():
    url, body = VONTOBEL.list_requests("https://x/jobs", {"facets": FACET}, [])
    assert isinstance(body, JsonPost) and dict(body) == {"appliedFacets": FACET, "limit": 20, "offset": 0, "searchText": ""}
    assert dict(SWISSCOM.list_requests("https://x/jobs", {"facets": {}}, [])[1])["appliedFacets"] == {}


def test_the_offset_is_the_number_of_postings_read_and_the_total_is_the_one_of_the_first_page():
    s = {"facets": {}}
    assert SWISSCOM.list_requests("u", s, [synthetic(0, 45)])[1]["offset"] == 20
    assert SWISSCOM.list_requests("u", s, [synthetic(0, 45), synthetic(20, 45)])[1]["offset"] == 40       # page 2 says total 0
    assert SWISSCOM.list_requests("u", s, [synthetic(0, 40), synthetic(20, 40)]) is None


def test_a_list_that_ends_early_is_an_error_and_total_zero_is_an_empty_list():
    with pytest.raises(PageError, match="ended at 20 of 45"):
        SWISSCOM.list_requests("u", {"facets": {}}, [synthetic(0, 45), json.dumps({"total": 0, "jobPostings": []})])
    assert SWISSCOM.list_requests("u", {"facets": {}}, [json.dumps({"total": 0, "jobPostings": []})]) is None


def test_all_pages_are_read_with_the_facet_mode_and_stored(conn, cfg):
    site = Site(lists=lists(45))
    s, jobs = adapters.fetch_list(conn, cfg, VONTOBEL, site, sleep=site.sleep, run_id="run1")
    assert (s.found, s.new, s.error, s.complete) == (45, 45, None, True)
    posts = [e for e in site.events if e[0] == "post"]
    assert len(posts) == 3 and site.requests[0] == ("get", "https://vontobel.wd3.myworkdayjobs.com/robots.txt")
    assert {r[0] for r in conn.execute("SELECT source FROM job_sources")} == {"vontobel_site"}
    assert conn.execute("SELECT DISTINCT company FROM jobs").fetchone()[0] == "Vontobel"


def test_without_a_facet_every_page_is_read_and_foreign_postings_are_not_stored(conn, cfg):
    cfg["adapters"]["swisscom"].update(facets={}, countries=["Switzerland"], places=["Zurich"])       # the shipped Swisscom list needs no filter
    site = Site(lists=lists(45, foreign_every=5))
    s, jobs = adapters.fetch_list(conn, cfg, SWISSCOM, site, sleep=site.sleep, run_id="run1")
    assert s.error is None and s.complete and len(site.requests) == 4             # robots.txt + 3 pages
    assert s.found == 36 and len(jobs) == 36 and all(j.locations == ["Zurich"] for j in jobs)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 36


def test_a_job_that_the_country_filter_drops_still_counts_as_listed_for_closing(conn, cfg):
    cfg["adapters"]["swisscom"]["max_list_pages"] = 10
    adapters.fetch_list(conn, cfg, SWISSCOM, Site(lists=lists(25)), sleep=lambda s: None, run_id="run1")
    cfg["adapters"]["swisscom"]["places"] = ["Bern"]                              # now no stored job passes the filter
    for _ in range(3):
        s, _ = adapters.fetch_list(conn, cfg, SWISSCOM, Site(lists=lists(25)), sleep=lambda s: None, run_id="run1")
    assert s.complete and s.closed == 0 and s.found == 0


def test_the_page_cap_leaves_the_read_not_complete(conn, cfg):
    cfg["adapters"]["max_list_pages"] = 2
    site = Site(lists=lists(45))
    s, _ = adapters.fetch_list(conn, cfg, VONTOBEL, site, sleep=site.sleep, run_id="run1")
    assert (s.found, s.complete) == (40, False)
    assert len([e for e in site.events if e[0] == "post"]) == 2


def test_a_check_page_instead_of_the_list_is_an_error(conn, cfg):
    s, jobs = adapters.fetch_list(conn, cfg, SWISSCOM, Site(lists={0: "<html>Checking your browser</html>"}), sleep=lambda s: None)
    assert jobs == [] and "not JSON" in s.error


# --- the job page ----------------------------------------------------------------------------------------

def test_the_address_of_the_job_page_is_the_json_address_of_the_public_page():
    public = "https://swisscom.wd103.myworkdayjobs.com/SwisscomExternalCareers/job/Bern/Data-Engineer---Geo-Platform_R-0006447-1"
    api = "https://swisscom.wd103.myworkdayjobs.com/wday/cxs/swisscom/SwisscomExternalCareers/job/Bern/Data-Engineer---Geo-Platform_R-0006447-1"
    assert SWISSCOM.detail_url(public) == api
    assert SWISSCOM.detail_url(public.replace("/SwisscomExternalCareers/", "/en-US/SwisscomExternalCareers/")) == api
    for bad in ("https://example.com/SwisscomExternalCareers/job/x/y_R-1", "https://swisscom.wd103.myworkdayjobs.com/Other/job/x/y_R-1",
                "https://swisscom.wd103.myworkdayjobs.com/SwisscomExternalCareers"):
        with pytest.raises(FetchError, match="not a Swisscom career site job page"):
            SWISSCOM.detail_url(bad)


def test_a_job_url_of_another_workday_host_is_refused_at_the_detail_step(conn, cfg):
    with pytest.raises(FetchError):
        adapters.detail_address(SWISSCOM, "https://vontobel.wd3.myworkdayjobs.com/SwisscomExternalCareers/job/Bern/x_R-1")


def test_the_swisscom_page_gives_clean_text_without_the_recruiter_and_the_posting_date():
    d = SWISSCOM.parse_detail(SC_DETAIL)
    assert "Geo" in d.text and "<" not in d.text and "&#39;" not in d.text
    assert "to all recruitment agencies" not in d.text.lower() and "Femi" not in d.text and "+41" not in d.text
    assert d.posted == "2026-11-02"


def test_the_vontobel_page_gives_text_and_the_posting_date():
    d = VONTOBEL.parse_detail(VT_DETAIL)
    assert d.text.startswith("For our technology team in Zurich") and "&amp;" not in d.text and d.posted == "2026-11-03"


def test_the_boilerplate_lines_are_dropped():
    page = json.dumps({"jobPostingInfo": {"jobDescription": "<p>Role text.</p><p>To all recruitment agencies: no CVs.</p>"}})
    assert SWISSCOM.parse_detail(page).text == "Role text."
    assert VONTOBEL.parse_detail(page).text.endswith("no CVs.")                       # Vontobel has no such rule


def test_a_job_page_without_a_posting_date_has_none():
    info = json.loads(SC_DETAIL)
    info["jobPostingInfo"].pop("startDate")
    assert SWISSCOM.parse_detail(json.dumps(info)).posted == ""


@pytest.mark.parametrize("page, message", [
    ("<html>maintenance</html>", "not JSON"),
    ('{"jobs": []}', "no 'jobPostingInfo.jobDescription'"),
    ('{"jobPostingInfo": {"jobDescription": "<p>To all recruitment agencies: no.</p>"}}', "empty description"),
    ('{"jobPostingInfo": {"jobDescription": "  "}}', "empty description"),
])
def test_a_changed_or_empty_job_page_is_an_error(page, message):
    with pytest.raises(PageError, match=message):
        SWISSCOM.parse_detail(page)


def test_job_pages_flow_after_the_first_filters_at_the_json_address(conn, cfg):
    adapters.fetch_list(conn, cfg, SWISSCOM, Site(lists=lists(25)), sleep=lambda s: None, run_id="run1")
    assert adapters.eligible_jobs(conn, SWISSCOM) == []                            # not filtered yet
    filter_new_jobs(conn, cfg, "run1")
    site = Site(default=SC_DETAIL)
    s = adapters.fetch_details(conn, cfg, SWISSCOM, site, sleep=site.sleep, now=NOW, run_id="run1", limit=2)
    assert (s.fetched, s.failed, s.stopped) == (2, 0, None)
    gets = [u for _, u in site.requests]
    assert gets[0] == "https://swisscom.wd103.myworkdayjobs.com/robots.txt"
    assert all(u.startswith("https://swisscom.wd103.myworkdayjobs.com/wday/cxs/swisscom/SwisscomExternalCareers/job/Zurich/") for u in gets[1:])
    row = conn.execute("SELECT description, posted_on FROM jobs WHERE description_missing = 0 LIMIT 1").fetchone()
    assert row["posted_on"] == "2026-11-02" and "Geo" in row["description"]


def test_the_keys_sources_and_tiers_of_the_tenants():
    assert [(s.key, s.source, s.tier, s.check_robots) for s in workday.SPECS] == [
        ("swisscom", "swisscom_site", "T1", True), ("vontobel", "vontobel_site", "T1", True), ("temenos", "temenos_site", "T1", True),
        ("hitachienergy", "hitachienergy_site", "T1", True), ("novartis", "novartis_site", "T1", True), ("deutschebank", "deutschebank_site", "T1", True)]
    assert all(s.host.endswith(".myworkdayjobs.com") for s in workday.SPECS)


# --- review fixes (Opus review of 4 Nov 2026) ---------------------------------------------------------------------

@pytest.mark.parametrize("path_place, listed_place", [("S-Antonino", "S. Antonino"), ("St-Gallen", "St. Gallen"), ("Zrich", "Zürich"),
                                                      ("Zurich-Switzerland", "Zurich"), ("Geneva-Genve", "Genève")])
def test_a_several_locations_posting_is_matched_by_its_path_place_whatever_the_hyphens_and_umlauts(path_place, listed_place):
    page = json.dumps({"total": 1, "jobPostings": [{"title": "A", "externalPath": f"/job/{path_place}/A_R-1", "locationsText": "2 Locations"}]})
    assert [j.external_id for j in SWISSCOM.parse_list(page, {"countries": [], "places": [listed_place]})] == ["r-1"]
    assert SWISSCOM.parse_list(page, {"countries": [], "places": ["Bern"]}) == []


def test_the_shipped_place_lists_hold_no_ambiguous_german_names():
    from app.config import load_config
    ad = load_config()["adapters"]
    assert ad["swisscom"]["countries"] == [] and ad["swisscom"]["places"] == []             # all its postings are Swiss (4 Nov 2026)
    for name in ("Baden", "Freiburg", "Cham"):
        assert name not in ad["temenos"]["places"]


def test_temenos_may_have_no_vacancy_in_switzerland_and_that_is_no_alert(conn):
    from app import alerts
    assert TEMENOS.empty_is_normal and not SWISSCOM.empty_is_normal and not VONTOBEL.empty_is_normal
    for i in range(3):
        db.start_run(conn, f"r{i}x")
        with conn:
            conn.execute("INSERT INTO adapter_runs (run_id, adapter, found, new_jobs, error) VALUES (?, ?, 0, 0, NULL)", (f"r{i}x", TEMENOS.label))
    assert alerts.adapter_problem(conn, TEMENOS.label, 3, TEMENOS.empty_is_normal) is None


def test_check_adapter_detail_reads_the_job_page_with_robots_and_a_pause(tmp_path, monkeypatch, capsys):
    import time
    from app import cli
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(f"paths:\n  database: {(tmp_path / 'jobs.db').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
                    f"  logs: {(tmp_path / 'logs').as_posix()}\n  lock: {(tmp_path / 'run.lock').as_posix()}\n"
                    "adapters:\n  delay_seconds: 1\n  swisscom:\n    enabled: true\n", encoding="utf-8")
    site = Site(lists={0: synthetic(0, 3)}, default=SC_DETAIL)
    monkeypatch.setattr(time, "sleep", site.sleep)               # the pause before the job page (the list keeps its own, 1 s each)
    assert cli.main(["--config", str(path), "check-adapter", "swisscom", "--detail"], page_fetcher=site) == 0
    assert "first job page:" in capsys.readouterr().out
    assert site.events[-4:-1] == [("sleep", 1), ("get", "https://swisscom.wd103.myworkdayjobs.com/robots.txt"), ("sleep", 1)]
    assert site.events[-1][1].startswith("https://swisscom.wd103.myworkdayjobs.com/wday/cxs/swisscom/SwisscomExternalCareers/job/Zurich/")


def test_read_page_without_robots_pauses_before_the_request(cfg):
    from app.adapters import six
    pauses = []
    out = adapters.read_page(cfg, next(s for s in adapters.REGISTRY if s.key == "six"), lambda u, d=None: "page",
                             "https://jobs.six-group.com/job/x/1/", sleep=pauses.append)
    assert out == "page" and pauses == [cfg["adapters"]["delay_seconds"]]


# --- Hitachi Energy, Novartis, Deutsche Bank (4 Nov 2026) ---------------------------------------------------------

NEW_TENANTS = [("hitachienergy", "hitachi.wd1.myworkdayjobs.com", "hitachi", "hitachi", "locationCountry"),
               ("novartis", "novartis.wd3.myworkdayjobs.com", "novartis", "Novartis_Careers", "locationCountry"),
               ("deutschebank", "db.wd3.myworkdayjobs.com", "db", "DBWebsite", "Country")]


@pytest.mark.parametrize("key, host, tenant, site, facet_key", NEW_TENANTS)
def test_the_new_tenants_ask_their_own_list_with_the_swiss_facet_and_read_the_job_page_like_the_others(key, host, tenant, site, facet_key):
    from app.config import load_config
    spec = next(s for s in adapters.REGISTRY if s.key == key)
    s = load_config()["adapters"][key]
    assert s["list_url"] == f"https://{host}/wday/cxs/{tenant}/{site}/jobs" and spec.host == host
    url, body = spec.list_requests(s["list_url"], s, [])
    assert url == s["list_url"] and isinstance(body, JsonPost)
    assert body["appliedFacets"] == {facet_key: ["187134fccb084a0ea9b4b95f23890dbe"]}
    page = json.dumps({"total": 1, "jobPostings": [{"title": "Analyst", "externalPath": "/job/Basel-City/Analyst_R-77-1", "locationsText": "Basel"}]})
    jobs = spec.parse_list(page, s)
    assert [(j.external_id, j.url) for j in jobs] == [("r-77-1", f"https://{host}/{site}/job/Basel-City/Analyst_R-77-1")]
    assert spec.detail_url(jobs[0].url) == f"https://{host}/wday/cxs/{tenant}/{site}/job/Basel-City/Analyst_R-77-1"
    detail = json.dumps({"jobPostingInfo": {"jobDescription": "<p>Build things.</p>", "startDate": "2026-11-03"}})
    d = spec.parse_detail(detail)
    assert d.text == "Build things." and d.posted == "2026-11-03"
