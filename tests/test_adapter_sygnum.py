"""Sygnum adapter (Salesforce portal paged by form posts) and the fetcher's POST and cookie handling."""
import io
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import adapters, db, descriptions_fetch
from app.adapters import AdapterDisabled, PageError, sygnum
from app.config import ConfigError, load_config
from app.descriptions_fetch import FetchError, PageFetcher
from app.filters.runner import filter_new_jobs

FIX = Path(__file__).parent / "fixtures" / "sygnum"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
LIST1, LIST2, DETAIL = [(FIX / n).read_text(encoding="utf-8") for n in ("list1.html", "list2.html", "detail.html")]
LIST_URL = "https://sygnumpeopleportal.my.salesforce-sites.com/recruit/fRecruit__ApplyJobList"
NOTE = "Sygnum confirmed by e-mail on 31 Oct 2026 that reading the portal is OK (kept in the user's mailbox)"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    c["adapters"]["sygnum"].update(enabled=True, permission_note=NOTE)
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.start_run(c, "run1")
    yield c
    c.close()


class Pages:
    """GET returns the list or a job page; the post of the Next link returns the second list page."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, data=None):
        self.calls.append((url, data))
        if data is not None:
            return LIST2 if "VIEWSTATE1" in data.get("com.salesforce.visualforce.ViewState", "") else LIST1
        return LIST1 if url == LIST_URL else DETAIL


# --- parsers ----------------------------------------------------------------------------------------

def test_list_keeps_the_configured_countries():
    jobs = sygnum.parse_list(LIST1, {"countries": ["Switzerland", "Liechtenstein"]})
    assert [(j.external_id, j.locations) for j in jobs] == [("VN233", ["Zurich"]), ("VN297", ["Vaduz"])]       # Singapore dropped
    assert jobs[0].url == "https://sygnumpeopleportal.my.salesforce-sites.com/recruit/fRecruit__ApplyJob?vacancyNo=VN233"
    assert "&amp;" not in jobs[0].title and len(sygnum.parse_list(LIST1, {"countries": []})) == 3


def test_a_changed_layout_is_an_error_and_an_empty_list_is_not():
    with pytest.raises(PageError, match="layout changed"):
        sygnum.parse_list("<html>maintenance</html>", {"countries": []})
    assert sygnum.parse_list("<html><h2>Current Vacancies</h2>No vacancies</html>", {"countries": []}) == []


def test_next_page_is_the_form_post_of_the_next_link():
    url, form = sygnum.next_page(LIST_URL, LIST1)
    assert url == LIST_URL
    assert form["com.salesforce.visualforce.ViewState"] == "VIEWSTATE1" and form["com.salesforce.visualforce.ViewStateMAC"] == "MAC1"
    assert form["j_id0:j_id1:j_id52:j_id176"] == "j_id0:j_id1:j_id52:j_id176"
    assert form["j_id0:j_id1:j_id52:j_id176:j_id177:j_id223"] == "j_id0:j_id1:j_id52:j_id176:j_id177:j_id223"      # the Next link's own field
    assert "j_id0:j_id1:j_id52:j_id176:j_id177:j_id180" not in form                                                  # the search box is not sent


def test_the_last_page_has_no_next_and_a_missing_view_state_is_an_error():
    assert sygnum.next_page(LIST_URL, LIST2) is None
    with pytest.raises(PageError, match="view state"):
        sygnum.next_page(LIST_URL, LIST1.replace("com.salesforce.visualforce.ViewState\"", "x.y\""))


def test_job_page_drops_introduction_values_offer_and_facts():
    text = sygnum.parse_detail(DETAIL).text
    assert text.startswith("About the role\n") and "Our ideal candidate\n" in text and "Fluent English" in text
    for dropped in ("Company introduction", "Values text", "Offer text", "Permanent", "Switzerland"):
        assert dropped not in text


def test_a_job_page_without_description_rows_is_an_error():
    with pytest.raises(PageError, match="layout changed"):
        sygnum.parse_detail("<html><body>nothing</body></html>")


# --- switches ---------------------------------------------------------------------------------------

def test_sygnum_needs_the_written_basis(conn, cfg):
    cfg["adapters"]["sygnum"]["permission_note"] = "  "
    pages = Pages()
    with pytest.raises(AdapterDisabled, match="permission_note"):
        adapters.fetch_list(conn, cfg, sygnum.SPEC, pages)
    assert pages.calls == [] and sygnum.SPEC not in adapters.enabled_adapters(cfg)


def test_the_config_refuses_sygnum_on_without_a_note(monkeypatch, tmp_path):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    bad = tmp_path / "c.yaml"
    bad.write_text("adapters:\n  sygnum:\n    enabled: true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="permission_note"):
        load_config(bad)


# --- flow -------------------------------------------------------------------------------------------

def test_both_list_pages_are_read_with_a_pause_and_stored(conn, cfg):
    pages, pauses = Pages(), []
    s, jobs = adapters.fetch_list(conn, cfg, sygnum.SPEC, pages, sleep=pauses.append, run_id="run1")
    assert [c[1] is None for c in pages.calls] == [True, False] and pauses == [5]               # a GET, then the form post
    assert (s.found, s.new, s.error) == (4, 4, None)                                              # 2 + 2 (VN297 + VN233, then VN116 + VN217)
    assert {r["company"] for r in conn.execute("SELECT company FROM jobs")} == {"Sygnum"}


def test_job_pages_are_stored_after_the_filters(conn, cfg):
    adapters.fetch_list(conn, cfg, sygnum.SPEC, Pages(), sleep=lambda s: None, run_id="run1")
    filter_new_jobs(conn, cfg, "run1")
    s = adapters.fetch_details(conn, cfg, sygnum.SPEC, Pages(), sleep=lambda s: None, now=NOW, run_id="run1")
    assert s.fetched >= 1 and s.failed == 0
    assert conn.execute("SELECT description FROM jobs WHERE description_missing = 0 LIMIT 1").fetchone()["description"].startswith("About the role")


def test_a_refusal_on_the_second_list_page_is_reported(conn, cfg):
    class Refusing(Pages):
        def __call__(self, url, data=None):
            if data is not None:
                raise FetchError("HTTP 403", 403)
            return super().__call__(url, data)
    s, jobs = adapters.fetch_list(conn, cfg, sygnum.SPEC, Refusing(), sleep=lambda s: None)
    assert jobs == [] and "HTTP 403" in s.error and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# --- the fetcher: POST and cookies ------------------------------------------------------------------

class _Resp(io.BytesIO):
    headers = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Headers:
    def __init__(self, cookies):
        self.cookies = cookies

    def get_all(self, name):
        return self.cookies if name == "Set-Cookie" else None


def test_page_fetcher_posts_a_form_and_sends_back_the_cookies(monkeypatch):
    seen = []

    def fake_open(self, request, data=None, timeout=None):
        seen.append((request.get_method(), request.full_url, request.data, request.get_header("Cookie"), request.get_header("Content-type")))
        resp = _Resp(b"<html>ok</html>")
        resp.headers = _Headers(["BrowserId=abc; path=/; Secure", "other=1; HttpOnly"]) if len(seen) == 1 else None
        return resp
    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)
    fetch = PageFetcher("UA/1")
    fetch("https://portal.example/list")
    fetch("https://portal.example/list", {"a b": "c&d", "n": "1"})
    assert seen[0][0] == "GET" and seen[0][2] is None and seen[0][3] is None
    method, url, body, cookie, ctype = seen[1]
    assert method == "POST" and urllib.parse.parse_qs(body.decode()) == {"a b": ["c&d"], "n": ["1"]}
    assert cookie == "BrowserId=abc; other=1" and ctype == "application/x-www-form-urlencoded"
    fetch("https://other.example/x")
    assert seen[2][3] is None                                                                   # cookies stay with their host
