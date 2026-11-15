"""LGT adapter, on trimmed real list pages and a job page with synthetic text (tests/fixtures/lgt)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import adapters, db
from app.adapters import PageError, lgt
from app.config import load_config
from app.filters.runner import filter_new_jobs

FIX = Path(__file__).parent / "fixtures" / "lgt"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
LIST1, LIST2, EMPTY, DETAIL = [(FIX / n).read_text(encoding="utf-8") for n in ("list1.html", "list2.html", "list_empty.html", "detail.html")]
BASE = "https://www.lgt.com/ch-en/career/jobs/48662!jobSearch"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    c["adapters"]["lgt"]["enabled"] = True
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.start_run(c, "run1")
    yield c
    c.close()


class Pages:
    def __init__(self):
        self.calls = []
        self.pages = {BASE: LIST1, BASE + "?pageNum=2": LIST2, BASE + "?pageNum=3": EMPTY}

    def __call__(self, url):
        self.calls.append(url)
        return self.pages.get(url, DETAIL)


def test_list_keeps_the_configured_places():
    jobs = lgt.parse_list(LIST1, {"places": ["Zurich", "Vaduz"]})
    assert [(j.locations, j.title[:18]) for j in jobs] == [(["Vaduz"], "Operational Pricin")]
    assert jobs[0].external_id == "374430" and jobs[0].url == "https://www.lgt.com/ch-en/career/jobs/operational-pricing-manager-80-100-f-m-d--374430"


def test_empty_places_keeps_everything_and_places_match_case_insensitively():
    assert len(lgt.parse_list(LIST1, {"places": []})) == 2
    assert len(lgt.parse_list(LIST1, {"places": ["vaduz"]})) == 1


def test_a_result_page_without_jobs_is_empty_but_a_changed_layout_is_an_error():
    assert lgt.parse_list(EMPTY, {"places": []}) == []
    with pytest.raises(PageError, match="layout changed"):
        lgt.parse_list("<html><body>maintenance</body></html>", {"places": []})


def test_the_other_list_pages_come_from_the_pagination_links():
    assert lgt.more_pages(BASE, LIST1) == [BASE + "?pageNum=2", BASE + "?pageNum=3"]


def test_job_page_keeps_only_the_job_sections_and_the_posting_date():
    d = lgt.parse_detail(DETAIL)
    assert d.text.startswith("Your challenge\n") and "Your profile\n" in d.text
    assert "Support audits" in d.text and "nested box" in d.text and "fluent English" in d.text
    for dropped in ("Company introduction", "Benefits text", "Jane Doe", "+41"):
        assert dropped not in d.text
    assert d.posted == "2025-11-19"


def test_a_job_page_without_job_sections_is_an_error():
    with pytest.raises(PageError, match="layout changed"):
        lgt.parse_detail("<html><body><h2 class=\"lgt-intro__title\" id=\"1-jobintro\"><span class=\"lgt-intro__title-block\">About us</span></h2></body></html>")


def test_all_list_pages_are_read_with_a_pause_and_the_jobs_are_stored(conn, cfg):
    cfg["adapters"]["lgt"]["places"] = ["Zurich", "Vaduz"]
    pages, pauses = Pages(), []
    s, jobs = adapters.fetch_list(conn, cfg, lgt.SPEC, pages, sleep=pauses.append, run_id="run1")
    assert pages.calls == [BASE, BASE + "?pageNum=2", BASE + "?pageNum=3"] and pauses == [5, 5]
    assert (s.found, s.new, s.error) == (2, 2, None)
    assert {r["company"] for r in conn.execute("SELECT company FROM jobs")} == {"LGT"}
    assert adapters.fetch_list(conn, cfg, lgt.SPEC, Pages(), sleep=lambda s: None)[0].new == 0           # a second run adds nothing


def test_max_list_pages_limits_the_requests(conn, cfg):
    cfg["adapters"]["max_list_pages"] = 2
    pages = Pages()
    adapters.fetch_list(conn, cfg, lgt.SPEC, pages, sleep=lambda s: None, store=False)
    assert len(pages.calls) == 2


def test_job_pages_of_listed_jobs_are_stored_after_the_filters(conn, cfg):
    cfg["adapters"]["lgt"]["places"] = ["Zurich", "Vaduz"]
    adapters.fetch_list(conn, cfg, lgt.SPEC, Pages(), sleep=lambda s: None, run_id="run1")
    filter_new_jobs(conn, cfg, "run1")
    s = adapters.fetch_details(conn, cfg, lgt.SPEC, Pages(), sleep=lambda s: None, now=NOW, run_id="run1")
    assert (s.fetched, s.failed) == (2, 0)
    row = conn.execute("SELECT description FROM jobs WHERE description_missing = 0 LIMIT 1").fetchone()
    assert row["description"].startswith("Your challenge")
