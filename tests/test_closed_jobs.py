"""Jobs that disappear from an employer list are marked closed (goal.md 5.1): complete reads only, two misses in a row."""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import adapters, db, pipeline
from app.adapters import lgt, six, sygnum
from app.batch import build_batch
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.descriptions_fetch import FetchError
from app.filters.runner import filter_new_jobs
from app.models import NormalizedJob
from app.report import CLOSED, collect
from tests.test_report import job as mail_job, pipeline as report_pipeline

FIX = Path(__file__).parent / "fixtures"
SIX_LIST = (FIX / "six" / "list.html").read_text(encoding="utf-8")
LGT1, LGT2, LGT_EMPTY = [(FIX / "lgt" / n).read_text(encoding="utf-8") for n in ("list1.html", "list2.html", "list_empty.html")]
SYG1, SYG2 = [(FIX / "sygnum" / n).read_text(encoding="utf-8") for n in ("list1.html", "list2.html")]
SIX_URL = "https://jobs.six-group.com/search/?q&locationsearch&locale=en_US"
LGT_BASE = "https://www.lgt.com/ch-en/career/jobs/48662!jobSearch"
SYG_URL = "https://sygnumpeopleportal.my.salesforce-sites.com/recruit/fRecruit__ApplyJobList"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    c["adapters"]["six"]["enabled"] = True
    c["adapters"]["lgt"].update(enabled=True, places=[])
    c["adapters"]["sygnum"].update(enabled=True, permission_note="test note", countries=[])
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.start_run(c, "run1")
    yield c
    c.close()


def six_ids(page=SIX_LIST):
    return [j.external_id for j in six.parse_list(page, {"country_codes": []})]


def six_without(*ids):
    """The SIX list page with the rows of these job ids removed."""
    rows = six.ROW.findall(SIX_LIST)
    page = SIX_LIST
    for row in rows:
        if any(f"/{i}/" in row for i in ids):
            page = page.replace(f'<tr class="data-row">{row}</tr>', "")
    assert len(six_ids(page)) == len(rows) - len(ids)
    return page


def read_six(conn, cfg, page=SIX_LIST, fail=None):
    def fetch(url, data=None):
        if fail:
            raise fail
        return page
    return adapters.fetch_list(conn, cfg, six.SPEC, fetch, sleep=lambda s: None, run_id="run1")[0]


def closed_ids(conn):
    return {r["external_id"] for r in conn.execute(
        "SELECT s.external_id FROM jobs j JOIN job_sources s ON s.job_id = j.id WHERE j.closed_at IS NOT NULL")}


def missed(conn, ext):
    return conn.execute("SELECT missed_reads FROM job_sources WHERE external_id = ?", (ext,)).fetchone()[0]


# --- the rule: two complete reads in a row ----------------------------------------------------------

def test_a_job_missing_in_one_complete_read_is_not_closed_but_in_two_it_is(conn, cfg):
    read_six(conn, cfg)
    gone = six_ids()[0]
    s1 = read_six(conn, cfg, six_without(gone))
    assert s1.complete and s1.closed == 0 and closed_ids(conn) == set() and missed(conn, gone) == 1
    s2 = read_six(conn, cfg, six_without(gone))
    assert s2.closed == 1 and closed_ids(conn) == {gone}


def test_a_job_listed_in_between_starts_counting_again(conn, cfg):
    read_six(conn, cfg)
    gone = six_ids()[0]
    read_six(conn, cfg, six_without(gone))
    read_six(conn, cfg)                                              # listed again: the count starts over
    read_six(conn, cfg, six_without(gone))
    assert closed_ids(conn) == set() and missed(conn, gone) == 1


def test_a_closed_job_that_is_listed_again_is_reopened_and_it_is_logged(conn, cfg, caplog):
    read_six(conn, cfg)
    gone = six_ids()[0]
    read_six(conn, cfg, six_without(gone))
    read_six(conn, cfg, six_without(gone))
    assert closed_ids(conn) == {gone}
    with caplog.at_level(logging.INFO, logger="recruitment"):
        s = read_six(conn, cfg)
    assert s.reopened == 1 and closed_ids(conn) == set() and missed(conn, gone) == 0
    assert conn.execute("SELECT last_listed_at FROM job_sources WHERE external_id = ?", (gone,)).fetchone()[0]
    assert "listed again; reopened" in caplog.text


def test_the_number_of_reads_is_a_setting(conn, cfg):
    cfg["adapters"]["closed_after_missing"] = 1
    read_six(conn, cfg)
    gone = six_ids()[0]
    read_six(conn, cfg, six_without(gone))
    assert closed_ids(conn) == {gone}
    cfg["adapters"]["closed_after_missing"] = 3
    other = six_ids()[1]
    read_six(conn, cfg)
    read_six(conn, cfg, six_without(other))
    read_six(conn, cfg, six_without(other))
    assert other not in closed_ids(conn)


def test_closing_changes_nothing_else_about_the_job(conn, cfg):
    read_six(conn, cfg)
    filter_new_jobs(conn, cfg, "run1")
    gone = six_ids()[0]
    before = dict(conn.execute("SELECT j.* FROM jobs j JOIN job_sources s ON s.job_id = j.id WHERE s.external_id = ?", (gone,)).fetchone())
    db.set_status(conn, before["id"], "applied")
    read_six(conn, cfg, six_without(gone))
    read_six(conn, cfg, six_without(gone))
    after = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (before["id"],)).fetchone())
    assert after["closed_at"] and after["status"] == "applied"
    assert {k: v for k, v in after.items() if k not in ("closed_at", "status")} == {k: v for k, v in before.items() if k not in ("closed_at", "status")}
    assert conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 0


# --- reads that do not count ------------------------------------------------------------------------

def test_a_failed_or_stopped_read_changes_nothing_and_resets_nothing(conn, cfg):
    read_six(conn, cfg)
    gone = six_ids()[0]
    read_six(conn, cfg, six_without(gone))                         # missing once
    for problem in (FetchError("HTTP 403", 403), FetchError("network error: timed out")):
        s = read_six(conn, cfg, fail=problem)
        assert s.error and not s.complete and closed_ids(conn) == set() and missed(conn, gone) == 1
    s = read_six(conn, cfg, "<html>new design</html>")             # a layout change is an error too
    assert s.error and missed(conn, gone) == 1
    read_six(conn, cfg, six_without(gone))                         # the count was kept: this is the second miss
    assert closed_ids(conn) == {gone}


def test_a_failed_read_does_not_reopen_a_closed_job(conn, cfg):
    read_six(conn, cfg)
    gone = six_ids()[0]
    read_six(conn, cfg, six_without(gone))
    read_six(conn, cfg, six_without(gone))
    read_six(conn, cfg, fail=FetchError("HTTP 429", 429))
    assert closed_ids(conn) == {gone}


def test_an_empty_list_is_not_taken_as_everything_closed(conn, cfg):
    read_six(conn, cfg)
    empty = "<html>Results 1 – 0 of 0 <table></table></html>"
    for _ in range(3):
        s = read_six(conn, cfg, empty)
        assert not s.complete
    assert closed_ids(conn) == set() and conn.execute("SELECT SUM(missed_reads) FROM job_sources").fetchone()[0] == 0


def test_a_list_cut_short_by_max_list_pages_counts_nothing_lgt(conn, cfg):
    def full(url, data=None):
        return {LGT_BASE: LGT1, LGT_BASE + "?pageNum=2": LGT2, LGT_BASE + "?pageNum=3": LGT_EMPTY}[url]
    adapters.fetch_list(conn, cfg, lgt.SPEC, full, sleep=lambda s: None, run_id="run1")
    page2_jobs = {j.external_id for j in lgt.parse_list(LGT2, {"places": []})}
    assert page2_jobs
    cfg["adapters"]["max_list_pages"] = 2

    def cut(url, data=None):                                       # page 2 is empty; page 3 would hold the rest, but is not read
        return {LGT_BASE: LGT1, LGT_BASE + "?pageNum=2": LGT_EMPTY}[url]
    for _ in range(3):
        s = adapters.fetch_list(conn, cfg, lgt.SPEC, cut, sleep=lambda s: None, run_id="run1")[0]
        assert s.error is None and not s.complete
    assert closed_ids(conn) == set() and conn.execute("SELECT SUM(missed_reads) FROM job_sources").fetchone()[0] == 0
    cfg["adapters"]["max_list_pages"] = 10                         # the same pages, read completely, do count
    for _ in range(2):
        adapters.fetch_list(conn, cfg, lgt.SPEC, lambda url, data=None: LGT1 if url == LGT_BASE else LGT_EMPTY, sleep=lambda s: None, run_id="run1")
    assert closed_ids(conn) == page2_jobs


def test_a_list_cut_short_by_max_list_pages_counts_nothing_sygnum(conn, cfg):
    def fetch(url, data=None):
        if data is not None:
            return SYG2
        return SYG1
    adapters.fetch_list(conn, cfg, sygnum.SPEC, fetch, sleep=lambda s: None, run_id="run1")
    stored = {r[0] for r in conn.execute("SELECT external_id FROM job_sources")}
    assert len(stored) == len(sygnum.parse_list(SYG1, {"countries": []})) + len(sygnum.parse_list(SYG2, {"countries": []}))
    cfg["adapters"]["max_list_pages"] = 1                          # the Next link exists but is not followed
    for _ in range(3):
        s = adapters.fetch_list(conn, cfg, sygnum.SPEC, fetch, sleep=lambda s: None, run_id="run1")[0]
        assert not s.complete
    assert closed_ids(conn) == set()
    cfg["adapters"]["max_list_pages"] = 2                          # the last page is reached exactly at the cap: complete
    assert adapters.fetch_list(conn, cfg, sygnum.SPEC, fetch, sleep=lambda s: None, run_id="run1")[0].complete


# --- list filters and other sources -----------------------------------------------------------------

def test_a_job_dropped_by_the_list_filter_is_not_missing_but_an_absent_one_is(conn, cfg):
    cfg["adapters"]["six"]["country_codes"] = []
    read_six(conn, cfg)                                            # all 4 jobs stored, among them the one in Madrid
    madrid = next(j.external_id for j in six.parse_list(SIX_LIST, {"country_codes": []}) if j.locations == ["Madrid, ES"])
    cfg["adapters"]["six"]["country_codes"] = ["CH"]               # the filter now drops it from the stored list ...
    for _ in range(3):
        s = read_six(conn, cfg)
        assert s.found == 3 and s.complete
    assert closed_ids(conn) == set() and missed(conn, madrid) == 0          # ... but it is still on the site
    for _ in range(2):
        read_six(conn, cfg, six_without(madrid))                   # now it really is gone from the site
    assert closed_ids(conn) == {madrid}


def test_lists_filtered_by_places_and_countries_keep_unlisted_filtered_jobs_open(conn, cfg):
    adapters.fetch_list(conn, cfg, lgt.SPEC, lambda url, data=None: LGT1 if url == LGT_BASE else LGT_EMPTY, sleep=lambda s: None, run_id="run1")
    stored = {r[0] for r in conn.execute("SELECT external_id FROM job_sources")}
    cfg["adapters"]["lgt"]["places"] = ["Nowhere"]                 # nothing is kept any more, everything is still listed
    for _ in range(3):
        s = adapters.fetch_list(conn, cfg, lgt.SPEC, lambda url, data=None: LGT1 if url == LGT_BASE else LGT_EMPTY, sleep=lambda s: None, run_id="run1")[0]
        assert s.found == 0 and s.complete
    assert stored and closed_ids(conn) == set()

    adapters.fetch_list(conn, cfg, sygnum.SPEC, lambda url, data=None: SYG2 if data is not None else SYG1, sleep=lambda s: None, run_id="run1")
    cfg["adapters"]["sygnum"]["countries"] = ["Nowhere"]
    for _ in range(3):
        s = adapters.fetch_list(conn, cfg, sygnum.SPEC, lambda url, data=None: SYG2 if data is not None else SYG1, sleep=lambda s: None, run_id="run1")[0]
        assert s.found == 0 and s.complete
    assert closed_ids(conn) == set()


def test_mail_jobs_are_never_closed_by_a_list_read(conn, cfg):
    mail = ingest_job(conn, mail_job(1, source="ubs_alert", external_id=six_ids()[0], url="https://jobs.ubs.com/x"), "run1").job_id
    read_six(conn, cfg)
    for _ in range(3):
        read_six(conn, cfg, six_without(six_ids()[0]))             # the SIX job with the same id is gone ...
    assert conn.execute("SELECT closed_at FROM jobs WHERE id = ?", (mail,)).fetchone()[0] is None      # ... the mail job is untouched
    assert conn.execute("SELECT missed_reads, last_listed_at FROM job_sources WHERE source = 'ubs_alert'").fetchone()[:] == (0, None)
    assert len(closed_ids(conn)) == 1                              # only the SIX one


def test_a_dry_run_records_nothing(conn, cfg):
    read_six(conn, cfg)
    adapters.fetch_list(conn, cfg, six.SPEC, lambda url, data=None: six_without(six_ids()[0]), sleep=lambda s: None, store=False)
    assert conn.execute("SELECT SUM(missed_reads) FROM job_sources").fetchone()[0] == 0


# --- what the marker does ---------------------------------------------------------------------------

def test_a_closed_job_is_left_out_of_the_next_batch_and_comes_back_when_listed_again(conn, cfg):
    read_six(conn, cfg)
    filter_new_jobs(conn, cfg, "run1")
    gone = six_ids()[0]
    read_six(conn, cfg, six_without(gone))
    read_six(conn, cfg, six_without(gone))
    info = build_batch(conn, cfg, "prompt", "v1", run_id="run2", now=NOW)
    exported = {r[0] for r in conn.execute("SELECT s.external_id FROM batch_jobs b JOIN job_sources s ON s.job_id = b.job_id WHERE b.batch_id = 'run2'")}
    assert info.job_count == 2 and gone not in exported and info.fresh_count == 2
    assert conn.execute("SELECT status FROM jobs j JOIN job_sources s ON s.job_id = j.id WHERE s.external_id = ?", (gone,)).fetchone()[0] == "new"
    read_six(conn, cfg)
    again = build_batch(conn, cfg, "prompt", "v1", run_id="run3", now=NOW + timedelta(days=1))
    assert (again.job_count, again.fresh_count) == (3, 1)           # the two carried over (next day) and the reopened one


def test_job_pages_of_closed_jobs_are_not_requested(conn, cfg):
    read_six(conn, cfg)
    filter_new_jobs(conn, cfg, "run1")
    gone = six_ids()[0]
    read_six(conn, cfg, six_without(gone))
    read_six(conn, cfg, six_without(gone))
    assert len(adapters.eligible_jobs(conn, six.SPEC)) == 2


def test_status_shows_the_number_of_closed_jobs(conn, cfg):
    assert pipeline.status(conn, cfg, NOW)["closed_jobs"] == 0
    read_six(conn, cfg)
    two = [r[0] for r in conn.execute("SELECT external_id FROM job_sources ORDER BY id LIMIT 2")]
    read_six(conn, cfg, six_without(*two))
    read_six(conn, cfg, six_without(*two))
    assert pipeline.status(conn, cfg, NOW)["closed_jobs"] == 2


def test_the_report_marks_a_closed_job_in_the_top_list(conn, cfg):
    cfg["email"]["retry_wait_seconds"] = 0
    ids, _ = report_pipeline(conn, cfg, [mail_job(1), mail_job(2)], [80, 70])
    assert all(CLOSED not in j.notes for j in collect(conn, cfg, "run1").jobs)
    with conn:
        conn.execute("UPDATE jobs SET closed_at = '2026-11-02T06:00:00Z' WHERE id = ?", (ids[0],))
    r = collect(conn, cfg, "run1")
    by_id = {j.job_id: j for j in r.jobs}
    assert CLOSED in by_id[ids[0]].notes and CLOSED not in by_id[ids[1]].notes
    assert CLOSED == "no longer listed on the employer's site"
    assert "NO LONGER LISTED ON THE EMPLOYER'S SITE" in r.text and "no longer listed on the employer&#x27;s site" in r.html
    assert r.text.count("NO LONGER LISTED") == 1


# --- schema ----------------------------------------------------------------------------------------

def test_the_migration_keeps_existing_rows_and_closes_nothing(tmp_path, cfg):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    for number, script in enumerate(db.MIGRATIONS[:9], start=1):          # the schema as of v0.26.6
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    raw.execute("INSERT INTO runs (id, started_at) VALUES ('r', '2026-10-31T00:00:00Z')")
    raw.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('k', 'Analyst', 'SIX', 'scored', 'a', 'b')")
    raw.execute("INSERT INTO job_sources (job_id, source, external_id, url, seen_at) VALUES (1, 'six_site', '777', 'https://jobs.six-group.com/job/x/777/', 'a')")
    raw.execute("INSERT INTO job_sources (job_id, source, external_id, seen_at) VALUES (1, 'ubs_alert', 'u1', 'a')")
    raw.commit()
    raw.close()
    c = db.connect(path)
    assert db.schema_version(c) == len(db.MIGRATIONS) >= 14
    assert c.execute("SELECT title, status, closed_at FROM jobs").fetchone()[:] == ("Analyst", "scored", None)
    assert [tuple(r) for r in c.execute("SELECT source, missed_reads, last_listed_at FROM job_sources ORDER BY id")] == [("six_site", 0, None), ("ubs_alert", 0, None)]
    db.start_run(c, "run1")
    s = read_six(c, cfg, six_without())                                      # the first complete read after the upgrade: one miss only
    assert s.complete and closed_ids(c) == set() and missed(c, "777") == 1
    c.close()


def test_the_setting_has_a_default_and_is_checked(monkeypatch, tmp_path):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    assert load_config()["adapters"]["closed_after_missing"] == 2
    bad = tmp_path / "c.yaml"
    bad.write_text("adapters:\n  closed_after_missing: 0\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="closed_after_missing"):
        load_config(bad)


def test_closed_jobs_waiting_for_a_score_or_a_new_score_are_not_carried_into_a_batch(conn, cfg):
    read_six(conn, cfg)
    filter_new_jobs(conn, cfg, "run1")
    assert build_batch(conn, cfg, "prompt", "v1", run_id="run2", now=NOW).job_count == 3          # all three now wait for a score
    gone, other = [r[0] for r in conn.execute("SELECT external_id FROM job_sources ORDER BY id LIMIT 2")]
    with conn:
        conn.execute("UPDATE jobs SET status = 'scored', needs_rescore = 1 WHERE id = (SELECT job_id FROM job_sources WHERE external_id = ?)", (other,))
    read_six(conn, cfg, six_without(gone, other))
    read_six(conn, cfg, six_without(gone, other))
    assert closed_ids(conn) == {gone, other}
    again = build_batch(conn, cfg, "prompt", "v1", run_id="run3", now=NOW)
    assert again.job_count == 1                                     # only the job that is still listed
