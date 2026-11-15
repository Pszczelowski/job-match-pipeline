"""Analytics and Runs/Health: the queries of `app/analytics.py` and the two read-only pages (goal.md 5.8, 5.9;
T-ANA-* and T-WEB-45 and up in docs/TEST_CASES.md).

Every test builds its own temporary database and folders; none uses the real config, the real database or the network.
"""
import json
import os
import re
from datetime import datetime, timezone

import pytest

from app import analytics, db, web
from tests.test_web import cfg, client, conn          # noqa: F401  (fixtures and helpers)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)      # a Monday, ISO week 2026-W45
SENTINEL = "SENTINEL-PRIVATE-7f3a"
ADDRESS = f"{SENTINEL}@example.org"


def add_job(conn, i, status="new", first_seen="2026-11-01T08:00:00Z", source="ubs", posted_on=None, score=None, company="Acme"):
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at, posted_on) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"key{i}", f"Analyst {i}", company, status, first_seen, first_seen, posted_on))
        jid = cur.lastrowid
        if source:
            conn.execute("INSERT INTO job_sources (job_id, source, external_id, source_date, seen_at) VALUES (?, ?, ?, '', ?)",
                         (jid, source, str(i), first_seen))
        for s in ([score] if isinstance(score, int) else (score or [])):
            conn.execute("INSERT INTO scores (job_id, prompt_version, model, overall, subscores, created_at) VALUES (?, 'v1', 'm', ?, '{}', ?)",
                         (jid, s, first_seen))
    return jid


def add_archive(conn, i, stage="filter", reason="salary_low", source="ubs", company="Acme", rejected_at="2026-11-01T08:00:00Z",
                first_seen="2026-11-01T08:00:00Z", score=None, restored=False):
    record = json.dumps({"job": {"first_seen_at": first_seen}})
    with conn:
        conn.execute(
            """INSERT INTO job_archive (dedup_key, source, title, company, record, rejection_stage, reason_code, score, rejected_at, restored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"a{i}", source, f"Old {i}", company, record, stage, reason, score, rejected_at, "2026-11-02T00:00:00Z" if restored else None))


def history(conn, jid, to_status, at):
    with conn:
        conn.execute("INSERT INTO status_history (job_id, from_status, to_status, changed_at) VALUES (?, NULL, ?, ?)", (jid, to_status, at))


def run(conn, run_id, started, status="ok", finished=None):
    with conn:
        conn.execute("INSERT INTO runs (id, started_at, finished_at, status, summary) VALUES (?, ?, ?, ?, ?)",
                     (run_id, started, finished, status, f"summary {SENTINEL}"))


def adapter_run(conn, run_id, adapter, found=3, new=1, error=None, warning=None):
    with conn:
        conn.execute("INSERT INTO adapter_runs (run_id, adapter, found, new_jobs, error, warning) VALUES (?, ?, ?, ?, ?, ?)",
                     (run_id, adapter, found, new, error, warning))


def label_counts(rows):
    return {r["label"]: r["count"] for r in rows}


# --- the empty database -----------------------------------------------------------------------------

def test_every_query_works_on_an_empty_database(conn, cfg):
    a = analytics.analytics(conn, cfg, NOW)
    assert a["funnel"]["working_total"] == 0 and a["funnel"]["archived_total"] == 0
    assert [r["label"] for r in a["funnel"]["working"]] == list(db.STATUSES) and all(r["count"] == 0 and r["pct"] == 0 for r in a["funnel"]["working"])
    assert a["rejections"]["total"] == 0 and a["rejections"]["reason"] == [] and len(a["rejections"]["week"]) == 12
    assert a["per_day"]["rows"] == [] and len(a["per_day"]["days"]) == 14 and a["per_day"]["total"] == 0
    assert a["score_source"]["rows"] == [] and a["bands"]["total"] == 0 and a["bands"]["rate"] is None
    assert a["apply"] == {"n": 0, "median": None, "shortest": None, "longest": None, "from_first_seen": 0}
    assert a["adapters"]["rows"] == []
    h = analytics.health(conn, cfg, NOW)
    assert h["runs"] == h["adapter_runs"] == h["alerts"] == h["imports"] == h["reports"] == h["fetches"] == []
    assert h["schema"] == {"version": len(db.MIGRATIONS), "expected": len(db.MIGRATIONS)}


# --- funnel -----------------------------------------------------------------------------------------

def test_funnel_counts_working_jobs_in_status_order_and_archived_jobs_by_stage(conn):
    for i, s in enumerate(["new", "scored", "scored", "scored", "applied"]):
        add_job(conn, i, s)
    add_archive(conn, 1, "filter")
    add_archive(conn, 2, "filter")
    add_archive(conn, 3, "user", "user_rejected")
    add_archive(conn, 4, "user", "user_rejected", restored=True)               # history only: not counted
    f = analytics.funnel(conn)
    assert [r["label"] for r in f["working"]] == list(db.STATUSES)
    assert label_counts(f["working"])["scored"] == 3 and label_counts(f["working"])["applied"] == 1 and f["working_total"] == 5
    assert label_counts(f["archived"]) == {"filter": 2, "user": 1, "not_selected": 0} and f["archived_total"] == 3
    assert {r["label"]: r["pct"] for r in f["working"]}["scored"] == 100 and {r["label"]: r["pct"] for r in f["working"]}["new"] == 33
    assert {r["label"]: r["pct"] for r in f["working"]}["offer"] == 0


def test_bar_width_is_at_least_one_percent_for_a_small_value():
    assert analytics.pct(1, 1000) == 1 and analytics.pct(0, 10) == 0 and analytics.pct(5, 0) == 0 and analytics.pct(10, 10) == 100


# --- rejections -------------------------------------------------------------------------------------

def test_rejections_by_reason_source_company_and_week(conn):
    add_archive(conn, 1, reason="salary_low", source="ubs", company="Acme", rejected_at="2026-11-02T05:00:00Z")      # 2026-W45
    add_archive(conn, 2, reason="salary_low", source="linkedin", company="Acme", rejected_at="2026-10-28T05:00:00Z")  # W44
    add_archive(conn, 3, reason="language", source="ubs", company="Beta", rejected_at="2026-10-28T05:00:00Z")
    add_archive(conn, 4, reason="language", source=None, company="Beta", rejected_at="2026-01-01T05:00:00Z")          # older than 12 weeks
    add_archive(conn, 5, reason="language", restored=True)
    r = analytics.rejections(conn, NOW)
    assert r["total"] == 4
    assert label_counts(r["reason"]) == {"salary_low": 2, "language": 2}
    assert label_counts(r["source"]) == {"ubs": 2, "linkedin": 1, "(unknown)": 1}
    assert label_counts(r["company"]) == {"Acme": 2, "Beta": 2}
    weeks = label_counts(r["week"])
    assert len(weeks) == 12 and list(weeks)[-1] == "2026-W45" and list(weeks)[0] == "2026-W34"
    assert weeks["2026-W45"] == 1 and weeks["2026-W44"] == 2 and sum(weeks.values()) == 3 and weeks["2026-W40"] == 0


def test_rejections_company_list_stops_at_ten_and_weeks_follow_iso_across_new_year(conn):
    for i in range(12):
        add_archive(conn, i, company=f"Company {i:02d}", rejected_at="2026-12-30T05:00:00Z")
    add_archive(conn, 99, company="Company 00", rejected_at="2027-01-04T05:00:00Z")
    r = analytics.rejections(conn, datetime(2027, 1, 5, 12, tzinfo=timezone.utc))
    assert len(r["company"]) == 10 and r["company"][0] == {"label": "Company 00", "count": 2, "pct": 100}
    weeks = label_counts(r["week"])
    assert list(weeks)[-2:] == ["2026-W53", "2027-W01"] and weeks["2026-W53"] == 12 and weeks["2027-W01"] == 1


# --- jobs per source per day ------------------------------------------------------------------------

def test_jobs_per_source_per_day_counts_working_and_archived_jobs_of_the_last_14_days(conn):
    add_job(conn, 1, first_seen="2026-11-02T01:00:00Z", source="ubs")
    add_job(conn, 2, first_seen="2026-11-02T09:00:00Z", source="ubs")
    add_job(conn, 3, first_seen="2026-10-20T09:00:00Z", source="ubs")                  # 13 days before: first day of the window
    add_job(conn, 4, first_seen="2026-10-19T09:00:00Z", source="ubs")                  # 14 days before: outside
    add_job(conn, 5, first_seen="2026-11-01T09:00:00Z", source=None)
    jid = add_job(conn, 6, first_seen="2026-11-01T10:00:00Z", source="six")
    with conn:                                                                         # a later sighting in another source does not move the job
        conn.execute("INSERT INTO job_sources (job_id, source, external_id, source_date, seen_at) VALUES (?, 'linkedin', 'x', '', '2026-11-02T00:00:00Z')", (jid,))
    add_archive(conn, 1, source="linkedin", first_seen="2026-11-01T07:00:00Z")
    add_archive(conn, 2, source="linkedin", first_seen="2026-11-01T07:00:00Z", restored=True)
    d = analytics.jobs_per_source_per_day(conn, NOW)
    assert d["days"][0] == "2026-10-20" and d["days"][-1] == "2026-11-02" and len(d["days"]) == 14
    rows = {r["label"]: r for r in d["rows"]}
    assert set(rows) == {"ubs", "six", "linkedin", "(unknown)"}
    assert rows["ubs"]["days"][-1] == 2 and rows["ubs"]["days"][0] == 1 and rows["ubs"]["count"] == 3
    assert rows["six"]["count"] == 1 and rows["linkedin"]["days"][-2] == 1 and rows["linkedin"]["count"] == 1 and rows["(unknown)"]["count"] == 1
    assert d["total"] == 6 and d["totals"][-1] == 2 and rows["ubs"]["pct"] == 100


# --- average score by source ------------------------------------------------------------------------

def test_average_score_by_source_uses_the_latest_score_and_includes_archived_scores(conn):
    add_job(conn, 1, source="ubs", score=[40, 80])                                     # latest = 80
    add_job(conn, 2, source="ubs", score=60)
    add_job(conn, 3, source="six", score=50)
    add_job(conn, 4, source="six")                                                     # no score: not counted
    add_archive(conn, 1, source="six", score=70)
    add_archive(conn, 2, source="six", score=None)
    add_archive(conn, 3, source="six", score=10, restored=True)
    s = analytics.score_by_source(conn)
    rows = {r["label"]: r for r in s["rows"]}
    assert rows["ubs"]["average"] == 70.0 and rows["ubs"]["count"] == 2
    assert rows["six"]["average"] == 60.0 and rows["six"]["count"] == 2 and rows["six"]["pct"] == 60
    assert s["total"] == 4 and [r["label"] for r in s["rows"]] == ["ubs", "six"]       # best average first


# --- score band and outcome -------------------------------------------------------------------------

def test_band_vs_outcome_uses_the_thresholds_of_the_config_and_the_response_rate(conn, cfg):
    cfg["scoring"]["band_low_below"], cfg["scoring"]["band_high_from"] = 30, 80
    add_job(conn, 1, "scored", score=85)                  # high, open
    add_job(conn, 2, "applied", score=80)                 # high (boundary), applied
    add_job(conn, 3, "interview", score=79)               # medium
    add_job(conn, 4, "rejected", score=30)                # medium (boundary)
    add_job(conn, 5, "applied", score=29)                 # low
    add_job(conn, 6, "approved", score=60)                # medium, shortlisted / approved
    add_job(conn, 7, "offer", score=95)                   # high
    add_job(conn, 8, "new")                               # no score: left out
    add_archive(conn, 1, score=20)                        # low, archived
    b = analytics.band_vs_outcome(conn, cfg)
    assert b["low_below"] == 30 and b["high_from"] == 80 and b["total"] == 8
    cell = {r["band"]: dict(zip(b["groups"], r["cells"])) for r in b["rows"]}
    assert cell["high"]["open"] == 1 and cell["high"]["applied"] == 1 and cell["high"]["offer"] == 1
    assert cell["medium"]["interview"] == 1 and cell["medium"]["rejected by employer"] == 1 and cell["medium"]["shortlisted / approved"] == 1
    assert cell["low"]["applied"] == 1 and cell["low"]["archived"] == 1
    assert b["applied"] == 5 and b["answered"] == 3 and b["rate"] == "60%"
    by_band = {r["band"]: (r["applied"], r["answered"], r["rate"]) for r in b["rows"]}
    assert by_band == {"high": (2, 1, "50%"), "medium": (2, 2, "100%"), "low": (1, 0, "0%")}


def test_response_rate_is_none_without_applications(conn, cfg):
    add_job(conn, 1, "scored", score=90)
    add_job(conn, 2, "shortlisted", score=70)
    b = analytics.band_vs_outcome(conn, cfg)
    assert b["total"] == 2 and b["applied"] == 0 and b["rate"] is None and all(r["rate"] is None for r in b["rows"])


# --- time from posting to application ---------------------------------------------------------------

def test_time_to_apply_median_and_range_use_the_posting_date_or_the_first_seen_date(conn):
    a = add_job(conn, 1, "applied", posted_on="2026-10-01", first_seen="2026-10-10T00:00:00Z")
    b = add_job(conn, 2, "interview", posted_on="2026-10-20", first_seen="2026-10-21T00:00:00Z")
    c = add_job(conn, 3, "applied", posted_on=None, first_seen="2026-10-25T10:00:00Z")           # first seen is used
    d = add_job(conn, 4, "shortlisted", posted_on="2026-10-01")                                  # never applied: left out
    history(conn, a, "applied", "2026-10-11T09:00:00Z")                                           # 10 days
    history(conn, a, "applied", "2026-10-30T09:00:00Z")                                           # a later row for the same job is ignored
    history(conn, b, "applied", "2026-10-22T09:00:00Z")                                           # 2 days
    history(conn, c, "applied", "2026-10-31T09:00:00Z")                                           # 6 days
    history(conn, d, "shortlisted", "2026-10-02T09:00:00Z")
    assert analytics.time_to_apply(conn) == {"n": 3, "median": 6, "shortest": 2, "longest": 10, "from_first_seen": 1}


def test_time_to_apply_with_an_even_number_gives_the_middle_value_and_never_goes_below_zero(conn):
    a = add_job(conn, 1, "applied", posted_on="2026-10-05")
    b = add_job(conn, 2, "applied", posted_on="2026-10-01")
    history(conn, a, "applied", "2026-10-03T09:00:00Z")                                           # before the posting date: 0
    history(conn, b, "applied", "2026-10-05T09:00:00Z")                                           # 4
    t = analytics.time_to_apply(conn)
    assert t["median"] == 2 and t["shortest"] == 0 and t["longest"] == 4


# --- adapter health ---------------------------------------------------------------------------------

def test_adapter_health_counts_ok_and_failed_runs_of_the_last_14_days(conn):
    run(conn, "r1", "2026-11-02T06:00:00Z")
    run(conn, "r2", "2026-11-01T06:00:00Z")
    run(conn, "r3", "2026-11-01T18:00:00Z")
    run(conn, "r0", "2026-10-01T06:00:00Z")                                                       # too old
    adapter_run(conn, "r1", "six")
    adapter_run(conn, "r2", "six", error="boom")
    adapter_run(conn, "r3", "six", warning="odd")                                                 # a warning is still ok
    adapter_run(conn, "r1", "lgt", found=0)
    adapter_run(conn, "r0", "lgt", error="old failure")
    h = analytics.adapter_health(conn, NOW)
    rows = {r["label"]: r for r in h["rows"]}
    assert set(rows) == {"six", "lgt"} and len(h["days"]) == 14
    assert rows["six"]["ok"] == 2 and rows["six"]["failed"] == 1 and rows["six"]["days"][-1] == (1, 0) and rows["six"]["days"][-2] == (1, 1)
    assert rows["lgt"]["ok"] == 1 and rows["lgt"]["failed"] == 0 and rows["six"]["ok_pct"] == 67 and rows["six"]["failed_pct"] == 33


# --- health -----------------------------------------------------------------------------------------

def test_health_lists_the_newest_rows_first_and_limits_them(conn, cfg):
    for i in range(25):
        run(conn, f"run{i:02d}", f"2026-10-{i + 1:02d}T06:00:00Z", status="failed" if i == 24 else "ok")
        adapter_run(conn, f"run{i:02d}", "six", error="x" if i % 2 else None, warning="w" if i == 3 else None)
    for i in range(12):
        with conn:
            conn.execute("INSERT INTO imports (batch_id, file_name, sha256, status, imported_at) VALUES (?, ?, ?, 'imported', ?)",
                         (f"b{i}", "f.json", "h", f"2026-10-{i + 1:02d}T07:00:00Z"))
            conn.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES (?, ?, 'f', 'h', 1, 1)", (f"b{i}", "2026-10-01T00:00:00Z"))
            conn.execute("INSERT INTO reports (batch_id, status, attempts, created_at, sent_at) VALUES (?, ?, 1, ?, ?)",
                         (f"b{i}", "sent" if i else "failed", f"2026-10-{i + 1:02d}T08:00:00Z", None))
    for i in range(22):
        with conn:
            conn.execute("INSERT INTO alerts (key, first_seen, last_seen, message, count) VALUES (?, '2026-10-01T00:00:00Z', ?, 'Backup failed', 2)",
                         (f"k{i}", f"2026-10-{i + 1:02d}T00:00:00Z"))
    h = analytics.health(conn, cfg, NOW)
    assert len(h["runs"]) == 20 and h["runs"][0]["id"] == "run24" and h["runs"][0]["status"] == "failed" and set(h["runs"][0]) == {"id", "started_at", "finished_at", "status"}
    assert len(h["adapter_runs"]) == 20 and h["adapter_runs"][0]["run_id"] == "run24" and h["adapter_runs"][0]["failed"] == 0
    assert len(h["alerts"]) == 20 and h["alerts"][0]["key"] == "k21"
    assert len(h["imports"]) == 10 and h["imports"][0]["batch_id"] == "b11" and set(h["imports"][0]) == {"id", "batch_id", "status", "imported_at"}
    assert len(h["reports"]) == 10 and h["reports"][0]["batch_id"] == "b11" and h["reports"][-1]["status"] == "sent"


def test_health_counts_description_fetches_of_the_last_7_days_by_outcome(conn, cfg):
    with conn:
        for i, (when, status) in enumerate([("2026-11-02T01:00:00Z", "ok"), ("2026-10-30T01:00:00Z", "ok"), ("2026-10-28T01:00:00Z", "error"),
                                            ("2026-10-26T01:00:00Z", "error"), ("2026-10-26T20:00:00Z", "ok")]):
            conn.execute("INSERT INTO description_fetches (job_id, attempted_at, status) VALUES (?, ?, ?)", (i + 1, when, status))
    h = analytics.health(conn, cfg, NOW)
    assert h["fetches"] == [{"status": "error", "count": 1}, {"status": "ok", "count": 3}]      # 2026-10-26T20 is inside 7 days, 10-26T01 outside


def test_newest_backup_reads_the_folder_only(tmp_path, cfg):
    folder = tmp_path / "bk"
    cfg["backup"]["dir"] = str(folder)
    assert analytics.newest_backup(cfg) is None                                                  # no folder yet
    folder.mkdir()
    assert analytics.newest_backup(cfg) is None                                                  # empty folder
    (folder / "jobs_20261101T060000Z.db").write_bytes(b"not a database, never opened")
    (folder / "jobs_20261102T060000Z.db").write_bytes(b"x" * 123)
    (folder / "jobs_pre-restore_20261103T060000Z.db").write_bytes(b"y")                          # not a rotation name
    (folder / "notes.txt").write_text("z")
    os.utime(folder / "jobs_20261102T060000Z.db", (1_793_000_000, 1_793_000_000))
    info = analytics.newest_backup(cfg)
    assert info["name"] == "jobs_20261102T060000Z.db" and info["size"] == 123 and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ", info["time"])


def test_health_data_holds_no_private_column(conn, cfg):
    run(conn, "run1", "2026-11-02T06:00:00Z", finished="2026-11-02T06:05:00Z")
    adapter_run(conn, "run1", "six", error=f"error {SENTINEL}", warning=f"warning {SENTINEL}")
    with conn:
        conn.execute("INSERT INTO mail_messages (message_id, source, status, error, handled_at) VALUES (?, 'ubs', 'error', ?, '2026-11-02T06:00:00Z')",
                     (f"<{ADDRESS}>", f"body {SENTINEL}"))
        conn.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES ('b1', '2026-11-02T00:00:00Z', 'f', 'h', 1, 1)")
        conn.execute("INSERT INTO imports (batch_id, file_name, sha256, status, reason, summary, imported_at) VALUES ('b1', ?, 'h', 'rejected', ?, ?, '2026-11-02T07:00:00Z')",
                     (f"{SENTINEL}.json", f"reason {SENTINEL}", f"summary {SENTINEL}"))
        conn.execute("INSERT INTO reports (batch_id, status, attempts, error, subject, message_id, created_at) VALUES ('b1', 'failed', 1, ?, ?, ?, '2026-11-02T08:00:00Z')",
                     (f"error {SENTINEL}", f"subject {SENTINEL}", f"<{ADDRESS}>"))
    assert SENTINEL not in repr(analytics.health(conn, cfg, NOW))


# --- the pages --------------------------------------------------------------------------------------

def assert_chart_page_is_csp_clean(r):
    """Only the JSON block, the vendored Chart.js and charts.js are scripts; no inline JavaScript, style attribute or handler; script-src 'self'."""
    text = r.text.lower()
    tags = re.findall(r"<script[^>]*>", text)
    assert sorted(tags) == sorted(['<script type="application/json" id="chart-data">', '<script src="/static/vendor/chart.umd.min.js" defer>',
                                   '<script src="/static/charts.js" defer>']), tags
    assert "<form" not in text and "style=" not in text and not re.search(r"\bon[a-z]+=", text) and "javascript:" not in text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "script-src 'none'" not in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp


def page_client(cfg, conn, tmp_path):
    cfg["backup"]["dir"] = str(tmp_path / "bk")
    return client(cfg, tmp_path)


def test_both_pages_are_in_the_navigation_of_every_page(cfg, conn, tmp_path):
    c = page_client(cfg, conn, tmp_path)
    for url in ("/", "/jobs", "/analytics", "/health"):
        body = c.get(url).text
        assert re.search(r'<a href="/analytics"[^>]*>Analytics</a>', body) and re.search(r'<a href="/health"[^>]*>Runs / Health</a>', body)


@pytest.mark.parametrize("url", ["/analytics", "/health"])
def test_page_on_an_empty_database_says_nothing_yet(cfg, conn, tmp_path, url):
    r = page_client(cfg, conn, tmp_path).get(url)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert r.text.count("nothing yet") >= 5
    assert_chart_page_is_csp_clean(r)


def test_empty_analytics_page_names_the_empty_states(cfg, conn, tmp_path):
    body = page_client(cfg, conn, tmp_path).get("/analytics").text
    assert "no applications yet" in body and "Not available yet" in body
    assert body.count("no applications yet") == 2                                          # the response rate and the time to apply


def test_analytics_page_shows_numbers_and_bars_for_every_block(cfg, conn, tmp_path):
    a = add_job(conn, 1, "applied", score=85, posted_on="2026-10-30", source="ubs", first_seen="2026-11-01T08:00:00Z")
    add_job(conn, 2, "scored", score=40, source="six", first_seen="2026-11-02T08:00:00Z")
    add_job(conn, 3, "interview", score=75, source="ubs", first_seen="2026-11-01T08:00:00Z")
    history(conn, a, "applied", "2026-11-02T09:00:00Z")
    add_archive(conn, 1, reason="salary_low", company="Zeta Corp", rejected_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                first_seen=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    run(conn, "r1", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    adapter_run(conn, "r1", "six")
    adapter_run(conn, "r1", "lgt", error="boom")
    r = page_client(cfg, conn, tmp_path).get("/analytics")
    body = r.text
    assert r.status_code == 200
    for heading in ("Funnel by status", "Rejections from the archive", "Jobs per source per day", "Average score by source", "Score band and outcome",
                    "Time from posting to application", "Adapter health, last 14 days"):
        assert heading in body
    assert 'class="w100"' in body and re.search(r'class="w\d+"', body)
    assert "salary_low" in body and "Zeta Corp" in body and "Response rate" in body and "Median" in body
    assert "low below 50, high from 70" in body
    assert "<form" not in body.lower() and "chart-rej-reason" in body and "Show table" in body


def test_health_page_shows_the_blocks_and_the_attribution_and_no_private_text(cfg, conn, tmp_path):
    run(conn, "run1", "2026-11-02T06:00:00Z", finished="2026-11-02T06:05:00Z")
    adapter_run(conn, "run1", "six", error=f"error {SENTINEL}", warning=f"warning {SENTINEL}")
    with conn:
        conn.execute("INSERT INTO alerts (key, first_seen, last_seen, message, count) VALUES ('backup_failed', '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z', 'The backup failed', 3)")
        conn.execute("INSERT INTO mail_messages (message_id, source, status, error, handled_at) VALUES (?, 'ubs', 'error', ?, '2026-11-02T06:00:00Z')",
                     (f"<{ADDRESS}>", f"body {SENTINEL}"))
        conn.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES ('b1', '2026-11-02T00:00:00Z', 'f', 'h', 1, 1)")
        conn.execute("INSERT INTO imports (batch_id, file_name, sha256, status, reason, summary, imported_at) VALUES ('b1', ?, 'h', 'rejected', ?, ?, '2026-11-02T07:00:00Z')",
                     (f"{SENTINEL}.json", f"reason {SENTINEL}", f"summary {SENTINEL}"))
        conn.execute("INSERT INTO reports (batch_id, status, attempts, error, subject, message_id, created_at) VALUES ('b1', 'sent', 1, ?, ?, ?, '2026-11-02T08:00:00Z')",
                     (f"error {SENTINEL}", f"subject {SENTINEL}", f"<{ADDRESS}>"))
        conn.execute("INSERT INTO description_fetches (job_id, attempted_at, status) VALUES (1, ?, 'ok')", (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    folder = tmp_path / "bk"
    folder.mkdir()
    (folder / "jobs_20261102T060000Z.db").write_bytes(b"not a database")
    cfg["backup"]["dir"] = str(folder)
    r = client(cfg, tmp_path).get("/health")
    body = r.text
    assert r.status_code == 200
    assert SENTINEL not in body and "example.org" not in body
    for expected in ("run1", "six", "backup_failed", "The backup failed", "b1", "jobs_20261102T060000Z.db", "opentransportdata.swiss",
                     f"Schema version: {len(db.MIGRATIONS)}", "failed", "warning"):
        assert expected in body
    assert "nothing yet" not in body.split("Job page reads")[0] and "<form" not in body.lower()


def test_health_page_without_a_backup_folder_says_the_folder_cannot_be_read(cfg, conn, tmp_path):
    body = page_client(cfg, conn, tmp_path).get("/health").text
    assert "the backup folder cannot be read in this container" in body and "./backups:/app/backups:ro" in body
    assert "opentransportdata.swiss" in body


def test_health_page_with_an_empty_backup_folder_says_nothing_yet(cfg, conn, tmp_path):
    (tmp_path / "bk").mkdir()
    body = page_client(cfg, conn, tmp_path).get("/health").text
    assert "Newest backup: nothing yet" in body and "cannot be read" not in body


NOW_HEALTH = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)


def daily_cfg(cfg, tmp_path):
    cfg["paths"].update(lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    cfg["backup"]["dir"] = str(tmp_path / "bk")
    return cfg


def test_health_shows_a_free_lock_the_last_successful_prepare_and_a_missing_heartbeat(cfg, conn, tmp_path):
    daily_cfg(cfg, tmp_path)
    run(conn, "run1", "2026-11-11T05:30:00Z", finished="2026-11-11T05:50:00Z")
    h = analytics.health(conn, cfg, NOW_HEALTH)
    assert h["lock"]["state"] == "free" and h["heartbeat"] is None and h["backup_folder"] == "missing"
    assert h["last_ok_prepare"] == {"time": "2026-11-11T05:50:00Z", "hours": 2.2, "late": False}
    body = client(cfg, tmp_path).get("/health").text
    assert "Run lock: free." in body and "Last successful prepare: 2026-11-11T05:50:00Z" in body and "Scheduler heartbeat: missing." in body


def test_health_shows_a_held_lock_and_a_fresh_heartbeat(cfg, conn, tmp_path):
    from app import pipeline, scheduler
    daily_cfg(cfg, tmp_path)
    scheduler.touch_heartbeat(cfg)
    with pipeline.run_lock(cfg):
        body = client(cfg, tmp_path).get("/health").text
    assert "Run lock: held since" in body and f"pid {os.getpid()}" in body
    assert re.search(r"Scheduler heartbeat: \d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ, \d+ s ago", body)


def test_health_marks_a_last_prepare_over_30_hours_old(cfg, conn, tmp_path):
    daily_cfg(cfg, tmp_path)
    run(conn, "run1", "2026-11-09T05:30:00Z", finished="2026-11-09T05:50:00Z")
    h = analytics.health(conn, cfg, NOW_HEALTH)
    assert h["last_ok_prepare"]["late"] is True
    assert "over 30 h" in client(cfg, tmp_path).get("/health").text


def test_health_shows_a_left_over_lock(cfg, conn, tmp_path):
    daily_cfg(cfg, tmp_path)
    (tmp_path / "run.lock").write_text(json.dumps({"pid": 7, "started": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    body = client(cfg, tmp_path).get("/health").text
    assert "left over" in body and "older than 6 hours" in body


def test_the_pages_write_nothing_and_take_no_post(cfg, conn, tmp_path):
    add_job(conn, 1, "scored", score=60)
    c = page_client(cfg, conn, tmp_path)
    before = [tuple(r) for r in conn.execute("SELECT * FROM jobs")], conn.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]
    for url in ("/analytics", "/health"):
        assert c.get(url).status_code == 200
        assert c.post(url, data={}).status_code == 405
        assert c.put(url).status_code == 405 and c.delete(url).status_code == 405
    assert ([tuple(r) for r in conn.execute("SELECT * FROM jobs")], conn.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]) == before


def test_a_database_with_another_schema_version_gives_the_error_page(cfg, conn, tmp_path):
    with conn:
        conn.execute("PRAGMA user_version = 3")
    c = page_client(cfg, conn, tmp_path)
    for url in ("/analytics", "/health"):
        r = c.get(url)
        assert r.status_code == 503 and "schema version" in r.text


def test_pages_stay_inside_the_phone_width(cfg, conn, tmp_path):
    add_job(conn, 1, "scored", score=60)
    body = page_client(cfg, conn, tmp_path).get("/analytics").text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in body
    assert body.count("<table") == body.count('<div class="tablewrap"><table')               # every table sits in a scrolling wrapper
    health = page_client(cfg, conn, tmp_path).get("/health").text
    assert health.count("<table") == health.count('<div class="tablewrap"><table')
    css = (web.HERE / "static" / "style.css").read_text(encoding="utf-8")
    assert ".tablewrap { overflow-x: auto; }" in css and ".w100 { width: 100%; }" in css and ".w0 { width: 0%; }" in css
    assert all(f".w{i} {{ width: {i}%; }}" in css for i in range(101))


# --- charts (v0.89.0) -------------------------------------------------------------------------------

def test_chart_data_on_an_empty_database_has_no_blocks(cfg, conn):
    assert analytics.chart_data(analytics.analytics(conn, cfg, NOW)) == {}
    assert analytics.health_chart_data(analytics.health(conn, cfg, NOW)) == {}


def seeded(conn):
    add_job(conn, 1, status="scored", score=80, source="ubs")
    add_job(conn, 2, status="applied", score=[55], source="ubs", first_seen="2026-10-30T08:00:00Z")
    add_job(conn, 3, status="interview", score=40, source="jobs.ch")
    add_job(conn, 4, status="new", source="jobs.ch")
    add_archive(conn, 1, reason="salary_low", source="ubs", score=75)
    add_archive(conn, 2, reason="title", source="jobs.ch", company="Zeta", rejected_at="2026-10-20T08:00:00Z")


def test_chart_data_shapes(cfg, conn):
    seeded(conn)
    charts = analytics.chart_data(analytics.analytics(conn, cfg, NOW))
    open_ = charts["chart-funnel-open"]
    assert open_["type"] == "bar" and open_["horizontal"] and open_["labels"] == list(db.STATUSES) and len(open_["datasets"][0]["data"]) == 12
    assert open_["datasets"][0]["color"] == "blue" and 0 in open_["datasets"][0]["data"]
    assert charts["chart-funnel-archived"]["datasets"][0]["color"] == "grey" and charts["chart-funnel-archived"]["labels"] == list(db.ARCHIVE_STAGES)
    assert charts["chart-rej-week"]["horizontal"] is False and len(charts["chart-rej-week"]["labels"]) == 12 and charts["chart-rej-week"]["datasets"][0]["color"] == "indigo"
    per_day = charts["chart-per-day"]
    assert per_day["stacked"] and len(per_day["labels"]) == 14 and [d["color"] for d in per_day["datasets"]] == ["blue", "green"]
    assert {d["label"] for d in per_day["datasets"]} == {"ubs", "jobs.ch"} and all(len(d["data"]) == 14 for d in per_day["datasets"])
    score = charts["chart-score-source"]
    assert score["horizontal"] and score["max"] == 100 and set(score["datasets"][0]["colors"]) <= {"green", "orange", "grey"}
    outcome = charts["chart-band-outcome"]
    assert outcome["stacked"] and outcome["horizontal"] and outcome["labels"] == list(analytics.BANDS) and len(outcome["datasets"]) == len(analytics.GROUP_NAMES)
    assert "chart-adapters" not in charts
    for spec in charts.values():                                       # colours are token names, never hex values
        names = [d["color"] for d in spec["datasets"]] + [c for d in spec["datasets"] for c in d.get("colors", [])]
        assert all(re.fullmatch(r"[a-z]+", n) for n in names)


def test_score_chart_bars_use_the_band_colour(cfg, conn):
    add_job(conn, 1, score=90, source="a")
    add_job(conn, 2, score=60, source="b")
    add_job(conn, 3, score=10, source="c")
    spec = analytics.chart_data(analytics.analytics(conn, cfg, NOW))["chart-score-source"]
    assert dict(zip(spec["labels"], spec["datasets"][0]["colors"])) == {"a": "green", "b": "orange", "c": "grey"}


def test_response_rate_chart_only_when_someone_applied(cfg, conn):
    add_job(conn, 1, status="scored", score=80)
    assert "chart-response-rate" not in analytics.chart_data(analytics.analytics(conn, cfg, NOW))
    add_job(conn, 2, status="interview", score=80)
    assert analytics.chart_data(analytics.analytics(conn, cfg, NOW))["chart-response-rate"]["datasets"][0]["data"] == [100, 0, 0]


def test_jobs_per_day_chart_is_zero_filled_and_uses_the_colour_order(cfg, conn):
    for i in range(12):
        add_job(conn, i, source=f"s{i:02d}", first_seen="2026-11-02T08:00:00Z")
    spec = analytics.chart_data(analytics.analytics(conn, cfg, NOW))["chart-per-day"]
    assert [d["color"] for d in spec["datasets"]][:10] == list(analytics.SOURCE_COLORS)
    assert spec["labels"][-1] == "11-02" and spec["datasets"][0]["data"] == [0] * 13 + [1]


def test_adapter_chart_sums_over_adapters(cfg, conn):
    with conn:
        conn.execute("INSERT INTO runs (id, started_at, status) VALUES ('r1', '2026-11-02T06:00:00Z', 'ok')")
        conn.execute("INSERT INTO adapter_runs (run_id, adapter, found, new_jobs, error) VALUES ('r1', 'a', 1, 1, NULL)")
        conn.execute("INSERT INTO adapter_runs (run_id, adapter, found, new_jobs, error) VALUES ('r1', 'b', 0, 0, 'boom')")
    spec = analytics.chart_data(analytics.analytics(conn, cfg, NOW))["chart-adapters"]
    assert spec["stacked"] and [(d["label"], d["color"], d["data"][-1], sum(d["data"])) for d in spec["datasets"]] == [("Ok", "green", 1, 1), ("Failed", "red", 1, 1)]


def test_run_minutes_and_colours_in_the_health_chart():
    h = {"runs": [{"id": "r3", "started_at": "2026-11-02T06:00:00Z", "finished_at": None, "status": "running"},
                  {"id": "r2", "started_at": "2026-11-01T06:00:00Z", "finished_at": "2026-11-01T06:12:30Z", "status": "failed"},
                  {"id": "r1", "started_at": "2026-10-31T06:00:00Z", "finished_at": "2026-10-31T06:03:00Z", "status": "ok"}],
         "fetches": [], "postponements": {"x": 0}}
    spec = analytics.health_chart_data(h)["chart-runs"]
    assert spec["labels"] == ["r1", "r2", "r3"] and spec["datasets"][0]["data"] == [3.0, 12.5, None]
    assert spec["datasets"][0]["colors"] == ["green", "red", "grey"] and not spec["horizontal"]
    assert set(analytics.health_chart_data(h)) == {"chart-runs"}
    h["runs"] = h["runs"][:1]
    assert analytics.health_chart_data(h) == {}                        # a run that is still running has no bar, so no chart


def test_health_chart_fetches_and_postponements():
    h = {"runs": [], "fetches": [{"status": "ok", "count": 4}], "postponements": {"travel": 2, "scoring": 0}}
    charts = analytics.health_chart_data(h)
    assert charts["chart-fetches"]["datasets"][0] == {"label": "Pages", "data": [4], "color": "blue"}
    assert charts["chart-postponed"]["datasets"][0]["color"] == "orange" and charts["chart-postponed"]["datasets"][0]["data"] == [2, 0]


def test_the_vendored_chartjs_matches_its_recorded_sha256():
    import hashlib
    from pathlib import Path
    folder = Path(web.HERE) / "static" / "vendor"
    recorded = re.search(r"\b([0-9a-f]{64})\b", (folder / "VENDOR.md").read_text(encoding="utf-8")).group(1)
    assert hashlib.sha256((folder / "chart.umd.min.js").read_bytes()).hexdigest() == recorded


def test_csp_script_src_per_route(cfg, conn, tmp_path):
    c = page_client(cfg, conn, tmp_path)
    for url in ("/analytics", "/health"):
        assert "script-src 'self'" in c.get(url).headers["content-security-policy"], url
    for url in ("/", "/jobs", "/missing", "/applied", "/static/style.css", "/static/charts.js", "/nope"):
        csp = c.get(url).headers["content-security-policy"]
        assert "script-src 'none'" in csp and "script-src 'self'" not in csp, url


def test_pages_with_data_show_canvases_json_and_folded_tables(cfg, conn, tmp_path, monkeypatch):
    real = analytics.analytics
    monkeypatch.setattr(analytics, "analytics", lambda c, g, now=None: real(c, g, NOW))       # the seeded days are 1 Nov 2026: the 14-day chart needs the clock of the data
    seeded(conn)
    r = page_client(cfg, conn, tmp_path).get("/analytics")
    assert_chart_page_is_csp_clean(r)
    assert re.search(r'<canvas class="chart h-l" id="chart-funnel-open" role="img" aria-label="[^"]*numbers in the table below">', r.text)
    assert "<details><summary>Show table</summary>" in r.text and 'id="chart-data"' in r.text and "chart-per-day" in r.text


def test_time_to_apply_is_three_tiles(cfg, conn, tmp_path):
    jid = add_job(conn, 1, status="applied", posted_on="2026-10-28")
    with conn:
        conn.execute("INSERT INTO status_history (job_id, from_status, to_status, changed_at) VALUES (?, 'approved', 'applied', '2026-11-01T08:00:00Z')", (jid,))
    body = page_client(cfg, conn, tmp_path).get("/analytics").text
    assert body.count('class="tile"') == 3 and "median days" in body and "Median" in body
