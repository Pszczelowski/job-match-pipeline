"""Greenhouse board readers (Scandit, GetYourGuide, 21Shares, tier T1, v0.70.0) on trimmed real answers (tests/fixtures/greenhouse).

Real: four jobs of the Scandit board (a Zurich job, a job with a Tampere location and a Zurich office, a Warsaw job, a Boston
job) and the answer of the per-job interface for the Zurich job. No test touches the network.
"""
import json
from datetime import datetime, timezone

import pytest

from app import adapters
from app.adapters import PageError, greenhouse
from app.descriptions_fetch import FetchError
from app.filters.runner import filter_new_jobs
from tests.ats_support import Site, fx, make_cfg, make_conn

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
SCANDIT = next(s for s in adapters.REGISTRY if s.key == "scandit")
GYG = next(s for s in adapters.REGISTRY if s.key == "getyourguide")
SHARES = next(s for s in adapters.REGISTRY if s.key == "shares21")
LIST, JOB = fx("greenhouse", "scandit_list.json"), fx("greenhouse", "scandit_job.json")
S = {"countries": ["Switzerland", "CHE", "CH"], "places": ["Zurich", "Zürich"]}
API_JOB = "https://boards-api.greenhouse.io/v1/boards/scandit/jobs/7552679"
LIST_URL = "https://boards-api.greenhouse.io/v1/boards/scandit/jobs?content=true"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return make_cfg(tmp_path, monkeypatch, "scandit", "getyourguide", "shares21")


@pytest.fixture
def conn(tmp_path):
    c = make_conn(tmp_path)
    yield c
    c.close()


def board(n, office="Zürich, Zürich, Switzerland", name="Zurich", host="https://job-boards.greenhouse.io/scandit/jobs/"):
    return json.dumps({"jobs": [{"id": 9000 + i, "title": f"Engineer {i}", "location": {"name": name}, "requisition_id": f"P-{i}",
                                 "absolute_url": f"{host}{9000 + i}", "offices": [{"name": name, "location": office}],
                                 "content": "", "first_published": "2026-10-01T10:00:00-04:00"} for i in range(n)], "meta": {"total": n}})


# --- the list parser ------------------------------------------------------------------------------------

def test_the_swiss_jobs_of_the_real_board_are_kept_by_office_country_or_place():
    jobs = SCANDIT.parse_list(LIST, S)
    assert [j.external_id for j in jobs] == ["8276084", "7552679"]            # Tampere with a Zurich office, and Zurich; Warsaw and Boston are dropped
    zurich = jobs[1]
    assert zurich.locations == ["Zurich"] and zurich.reference == "P-25221" and zurich.title.strip()
    assert jobs[0].locations == ["Tampere"]                                    # the name of the location is stored, the office decided


def test_both_lists_empty_keep_every_job():
    assert len(SCANDIT.parse_list(LIST, {"countries": [], "places": []})) == 4


def test_a_job_is_kept_by_the_name_of_its_location_when_it_has_no_office():
    page = json.dumps({"jobs": [{"id": 1, "title": "A", "location": {"name": " Zürich "}, "offices": []},
                                {"id": 2, "title": "B", "location": {"name": "Remote"}, "offices": [{"location": None}]}]})
    assert [j.external_id for j in SCANDIT.parse_list(page, S)] == ["1"]


def test_the_own_site_address_is_kept_on_a_job_host_and_replaced_by_the_board_page_elsewhere():
    jobs = {j.external_id: j.url for j in SCANDIT.parse_list(LIST, S)}
    assert jobs["7552679"] == "https://www.scandit.com/careers/job-description/?gh_jid=7552679"
    other = SCANDIT.parse_list(board(1, host="https://evil.example.com/x/"), S)[0]
    assert other.url == "https://job-boards.greenhouse.io/scandit/jobs/9000"
    insecure = GYG.parse_list(board(1, host="http://job-boards.greenhouse.io/getyourguide/jobs/"), S)[0]
    assert insecure.url == "https://job-boards.greenhouse.io/getyourguide/jobs/9000"


def test_an_empty_board_is_a_valid_answer():
    assert SCANDIT.parse_list('{"jobs": [], "meta": {"total": 0}}', S) == []


@pytest.mark.parametrize("page, message", [
    ("<html>nope</html>", "not JSON"),
    ("[]", "no 'jobs' list"),
    ('{"message": "board not found"}', "no 'jobs' list"),
    ('{"jobs": {}}', "no 'jobs' list"),
    ('{"jobs": [{"title": "A"}]}', "no id and title"),
    ('{"jobs": [{"id": 1, "title": " "}]}', "no id and title"),
])
def test_a_changed_answer_is_an_error_never_an_empty_list(page, message):
    with pytest.raises(PageError, match=message):
        SCANDIT.parse_list(page, S)


# --- reading the list -----------------------------------------------------------------------------------

def test_the_list_is_one_request_after_robots_and_stored_as_board_jobs(conn, cfg):
    site = Site(pages={LIST_URL: LIST})
    s, jobs = adapters.fetch_list(conn, cfg, SCANDIT, site, sleep=site.sleep, run_id="run1")
    assert (s.found, s.new, s.error, s.complete) == (2, 2, None, True)
    assert [u for _, u in site.requests] == ["https://boards-api.greenhouse.io/robots.txt", LIST_URL]
    assert site.pauses == [5, 5]
    rows = conn.execute("SELECT j.company, s.source, s.url FROM jobs j JOIN job_sources s ON s.job_id = j.id").fetchall()
    assert {(r[0], r[1]) for r in rows} == {("Scandit", "scandit_site")}
    assert "https://www.scandit.com/careers/job-description?gh_jid=7552679" in {r[2] for r in rows}          # as the dedup stores it


def test_a_second_read_changes_nothing(conn, cfg):
    for _ in range(2):
        s, _ = adapters.fetch_list(conn, cfg, SCANDIT, Site(pages={LIST_URL: LIST}), sleep=lambda s: None, run_id="run1")
    assert (s.found, s.new, s.outcomes) == (2, 0, {"unchanged": 2})


def test_an_empty_board_is_not_trusted_as_everything_closed(conn, cfg):
    adapters.fetch_list(conn, cfg, SCANDIT, Site(pages={LIST_URL: LIST}), sleep=lambda s: None, run_id="run1")
    empty = '{"jobs": [], "meta": {"total": 0}}'
    s, _ = adapters.fetch_list(conn, cfg, SCANDIT, Site(pages={LIST_URL: empty}), sleep=lambda s: None, run_id="run1")
    assert (s.found, s.error) == (0, None) and not s.complete                  # an empty list is not trusted as "closed everything"
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE closed_at IS NOT NULL").fetchone()[0] == 0


def test_the_three_boards_have_their_own_tokens_and_hosts():
    assert [(s.key, s.source, s.tier, s.empty_is_normal) for s in greenhouse.SPECS] == [
        ("scandit", "scandit_site", "T1", True), ("getyourguide", "getyourguide_site", "T1", True), ("shares21", "shares21_site", "T1", True)]
    assert {s.host for s in greenhouse.SPECS} == {"boards-api.greenhouse.io"}
    assert SCANDIT.job_hosts == ("www.scandit.com", "job-boards.greenhouse.io") and GYG.job_hosts == ("job-boards.greenhouse.io",)
    assert SHARES.detail_url("https://job-boards.greenhouse.io/21shares/jobs/55") == "https://boards-api.greenhouse.io/v1/boards/21shares/jobs/55"


def test_a_foreign_list_address_is_refused(conn, cfg):
    cfg["adapters"]["scandit"]["list_url"] = "https://job-boards.greenhouse.io/scandit"
    site = Site()
    s, _ = adapters.fetch_list(conn, cfg, SCANDIT, site)
    assert site.events == [] and "not a page on boards-api.greenhouse.io" in s.error


# --- the job page ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://www.scandit.com/careers/job-description/?gh_jid=7552679",
                                 "https://www.scandit.com/careers/job-description?gh_jid=7552679",
                                 "https://www.scandit.com/careers/job-description/?x=1&gh_jid=7552679&y=2",
                                 "https://job-boards.greenhouse.io/scandit/jobs/7552679"])
def test_the_address_of_the_job_page_is_the_per_job_interface(url):
    assert SCANDIT.detail_url(url) == API_JOB


@pytest.mark.parametrize("url", ["https://www.scandit.com/careers/", "https://evil.example.com/?gh_jid=1",
                                 "https://job-boards.greenhouse.io/scandit/jobs/abc"])
def test_a_page_without_an_id_or_on_a_foreign_host_has_no_address(url):
    with pytest.raises(FetchError, match="not a Scandit job board job page"):
        SCANDIT.detail_url(url)


def test_the_job_host_itself_is_never_requested(conn, cfg):
    with pytest.raises(FetchError):
        adapters.detail_address(GYG, "https://www.scandit.com/careers/job-description/?gh_jid=1")      # not a GetYourGuide job host
    assert adapters.detail_address(SCANDIT, "https://www.scandit.com/careers/job-description/?gh_jid=7552679") == API_JOB


def test_the_escaped_html_content_becomes_clean_text_with_the_first_posting_date():
    d = SCANDIT.parse_detail(JOB)
    assert d.text.startswith("Imagine the What. Build the How.") and "&lt;" not in d.text and "<p>" not in d.text and "&amp;" not in d.text
    assert d.posted == json.loads(JOB)["first_published"][:10]


@pytest.mark.parametrize("page, message", [
    ("<html>maintenance</html>", "not JSON"),
    ('{"message": "not found"}', "no 'content'"),
    ('{"content": "   "}', "empty description"),
    ('{"content": "&lt;p&gt; &lt;/p&gt;"}', "empty description"),
])
def test_a_changed_or_empty_job_page_is_an_error(page, message):
    with pytest.raises(PageError, match=message):
        SCANDIT.parse_detail(page)


def test_job_pages_flow_from_the_own_site_address_to_the_api(conn, cfg):
    adapters.fetch_list(conn, cfg, SCANDIT, Site(pages={LIST_URL: LIST}), sleep=lambda s: None, run_id="run1")
    filter_new_jobs(conn, cfg, "run1")
    todo = adapters.eligible_jobs(conn, SCANDIT)
    assert len(todo) >= 1
    site = Site(default=JOB)
    s = adapters.fetch_details(conn, cfg, SCANDIT, site, sleep=site.sleep, now=NOW, run_id="run1")
    assert (s.failed, s.stopped) == (0, None) and s.fetched == len(todo)
    assert site.requests[0] == ("get", "https://boards-api.greenhouse.io/robots.txt")
    assert all(u.startswith("https://boards-api.greenhouse.io/v1/boards/scandit/jobs/") for _, u in site.requests[1:])
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE description_missing = 0").fetchone()[0] == len(todo)
    again = Site(default=JOB)
    assert adapters.fetch_details(conn, cfg, SCANDIT, again, sleep=again.sleep, now=NOW).attempted == 0 and again.requests == []
