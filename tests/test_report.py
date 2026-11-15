import json
import smtplib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import db
from app.batch import build_batch, exchange_dir
from app.config import ConfigError, load_config, load_env
from app.dedup import ingest_job
from app.filters.runner import filter_new_jobs
from app.models import NormalizedJob
from app.parsers.ubs import parse_alert, to_job
from app.report import ReportError, band, build_message, collect, send_report
from app.results import import_results

UBS = Path(__file__).parent / "fixtures" / "ubs"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
PASSWORD = "app-pass-word-123456"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", PASSWORD)
    monkeypatch.delenv("GMAIL_USER", raising=False)
    c = load_config()
    c["paths"]["exchange"] = str(tmp_path / "exchange")
    c["email"]["retry_wait_seconds"] = 0
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def job(i, **kw):
    base = dict(source="s", external_id=str(i), title=f"Analyst {i}", company="Acme", url=f"https://example.com/job/{i}",
                locations=["Zürich"], description=f"Description {i}")
    base.update(kw)
    return NormalizedJob(**base)


def score(job_id, overall, **kw):
    entry = {"job_id": job_id, "overall": overall,
             "subscores": {"skills": overall, "experience": 50, "domain": 50, "seniority": 50, "location": 100},
             "matches": ["SQL reporting", "Requirements work", "Third point"], "gaps": ["Kotlin", "Spark", "Scala", "Go"],
             "red_flags": [], "confidence": "medium", "summary": f"Fits job {job_id}.", "cv_variant": "resume"}
    entry.update(kw)
    return entry


def pipeline(conn, cfg, jobs, scores, run_id="run1", extra_entries=()):
    """Ingest, filter, export and import; returns (ids, import result)."""
    db.start_run(conn, run_id)
    ids = [ingest_job(conn, j, run_id).job_id for j in jobs]
    filter_new_jobs(conn, cfg, run_id)
    build_batch(conn, cfg, "prompt", "v1", run_id=run_id, now=NOW)
    entries = [score(i, s) for i, s in zip(ids, scores) if s is not None] + list(extra_entries)
    doc = {"schema_version": "1.0", "run_id": run_id, "model": "m", "prompt_version": "v1", "results": entries, "top": []}
    path = exchange_dir(cfg) / "results" / f"{run_id}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return ids, import_results(conn, cfg, path, NOW)


class FakeSMTP:
    sent = []
    events = []
    failures = []                    # exceptions to raise on successive login attempts

    def __init__(self, host, port, timeout=None):
        FakeSMTP.events.append(("connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.events.append(("starttls", context is not None))

    def login(self, user, password):
        FakeSMTP.events.append(("login", user, password))
        if FakeSMTP.failures:
            raise FakeSMTP.failures.pop(0)

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def reset_smtp():
    FakeSMTP.sent, FakeSMTP.events, FakeSMTP.failures = [], [], []


def report_for(conn, cfg, run_id="run1", **kw):
    return collect(conn, cfg, run_id, **kw)


# --- content --------------------------------------------------------------------------------------

def test_bands(cfg):
    assert [band(s, cfg) for s in (0, 49, 50, 69, 70, 100)] == ["low", "low", "medium", "medium", "high", "high"]


def test_top_jobs_are_listed_in_rank_order_with_score_and_band(conn, cfg):
    ids, _ = pipeline(conn, cfg, [job(1), job(2), job(3)], [55, 90, 20])
    r = report_for(conn, cfg)
    assert [(j.rank, j.job_id, j.score, j.band) for j in r.jobs] == [(1, ids[1], 90, "high"), (2, ids[0], 55, "medium"), (3, ids[2], 20, "low")]
    j = r.jobs[0]
    assert (j.title, j.company, j.locations, j.url, j.summary) == ("Analyst 2", "Acme", ["Zürich"], "https://example.com/job/2", f"Fits job {ids[1]}.")
    assert j.matches == ["SQL reporting", "Requirements work", "Third point"] and j.gaps == ["Kotlin", "Spark", "Scala"]   # limited by config


def test_text_and_html_contain_the_essentials(conn, cfg):
    pipeline(conn, cfg, [job(1)], [88])
    r = report_for(conn, cfg)
    for body in (r.text, r.html):
        assert "Analyst 1" in body and "Acme" in body and "88" in body and "Kotlin" in body
    assert "[HIGH 88]" in r.text and "https://example.com/job/1" in r.text and 'href="https://example.com/job/1"' in r.html


def test_matches_and_gaps_are_separate_labelled_lists(conn, cfg):
    pipeline(conn, cfg, [job(1)], [88])
    r = report_for(conn, cfg)
    assert "   MATCHES\n     + SQL reporting\n     + Requirements work\n     + Third point\n" in r.text
    assert "   GAPS\n     - Kotlin\n     - Spark\n     - Scala\n" in r.text
    assert r.html.count("<li ") == 6 and ">MATCHES<" in r.html and ">GAPS<" in r.html
    assert "; " not in r.text.split("MATCHES")[1].split("GAPS")[0]            # one item per line, not a run-on


def test_fewer_than_ten_lists_them_all_and_ten_caps(conn, cfg):
    pipeline(conn, cfg, [job(i) for i in range(1, 14)], list(range(40, 53)))
    r = report_for(conn, cfg)
    assert len(r.jobs) == 10 and r.subject.endswith("10 job matches for 2 Nov 2026")
    assert "TOP 10" in r.text


def test_subject_forms(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60], run_id="a")
    assert report_for(conn, cfg, "a").subject == "Recruitment: 1 job match for 2 Nov 2026"
    pipeline(conn, cfg, [job(2)], [None], run_id="b")
    assert report_for(conn, cfg, "b").subject == "Recruitment: no job matches for 2 Nov 2026"


def test_repost_and_conditional_markers(conn, cfg):
    db.start_run(conn, "run1")
    ingest_job(conn, job(1, source_date="2026-10-27"), "run1")
    ingest_job(conn, job(1, source_date="2026-10-28"), "run1")                       # same source, newer date: repost
    ingest_job(conn, job(2, description="Good German skills required."), "run1")
    ingest_job(conn, job(3, locations=[]), "run1")
    from app.filters.base import FilterResult
    from app.filters.runner import DEFAULT_FILTERS

    def no_place(j, c):          # stands in for the location filter, which needs the travel-time providers
        return [FilterResult("location", "conditional", "location_unspecified", "no place")] if "3" in j.title else []
    filter_new_jobs(conn, cfg, "run1", filters=[*DEFAULT_FILTERS, no_place])
    build_batch(conn, cfg, "p", "v1", run_id="run1", now=NOW)
    ids = [r["job_id"] for r in conn.execute("SELECT job_id FROM batch_jobs ORDER BY job_id")]
    path = exchange_dir(cfg) / "results" / "r.json"
    path.write_text(json.dumps({"schema_version": "1.0", "run_id": "run1", "model": "m", "prompt_version": "v1",
                                "results": [score(i, 70 - n) for n, i in enumerate(ids)], "top": []}), encoding="utf-8")
    import_results(conn, cfg, path, NOW)
    by_title = {j.title: j for j in report_for(conn, cfg).jobs}
    assert by_title["Analyst 1"].repost and by_title["Analyst 1"].notes == []
    assert by_title["Analyst 2"].notes == ["language required, level not stated"]
    assert by_title["Analyst 3"].notes == ["location unclear"]
    r = report_for(conn, cfg)
    assert "REPOST" in r.text and "LOCATION UNCLEAR" in r.text and ">location unclear<" in r.html


def test_outside_top_line_none_and_some(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60], run_id="a")
    assert "No job outside the top 10 scored 50 or more." in report_for(conn, cfg, "a").text
    pipeline(conn, cfg, [job(i) for i in range(10, 25)], [75, 74, 73, 72, 71, 70, 69, 68, 67, 66, 65, 49, 30, 20, 10], run_id="b")
    r = report_for(conn, cfg, "b")
    assert (r.outside_count, r.outside_best) == (1, 65)         # 65 is the 11th; 49 and below are low
    assert "1 job(s) outside the top 10 scored 50 or more (best: 65)" in r.text


def test_outside_line_with_only_low_scores(conn, cfg):
    pipeline(conn, cfg, [job(i) for i in range(1, 13)], [90] * 10 + [49, 10])
    r = report_for(conn, cfg)
    assert r.outside_count == 0 and "No job outside" in r.text


def test_run_summary(conn, cfg):
    pipeline(conn, cfg, [job(1), job(2, title="Head of Data")], [60],
             )
    r = report_for(conn, cfg, extra={"seen": 12, "new": 5, "adapters": [{"name": "UBS alert", "found": 4, "new": 2},
                                                                         {"name": "Sygnum", "error": "HTTP 503"}]})
    text = "\n".join(r.summary)
    assert "Jobs seen: 12; new: 5" in text and "UBS alert: 4 found, 2 new" in text and "Sygnum: failed (HTTP 503)" in text
    assert "Batch: 1 jobs (1 new, 0 carried over); scored: 1; waiting for a score: 0" in text
    assert "Rejected by the filters this run: 1 (seniority_title_pattern 1)" in text


def test_run_summary_shows_a_warning_row_as_warning_not_failed(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60])
    row = {"name": "Description resolver", "found": 4, "new": 0, "error": None, "warning": "3 jobs wait for the daily cap"}
    r = report_for(conn, cfg, extra={"adapters": [row]})
    line = next(s for s in r.summary if s.startswith("Description resolver"))
    assert line == "Description resolver: warning (3 jobs wait for the daily cap); 4 found, 0 new"
    assert "failed" not in line and "warning (3 jobs wait" in r.html
    r = report_for(conn, cfg, extra={"adapters": [{**row, "error": "HTTP 503"}]})
    assert "Description resolver: failed (HTTP 503)" in "\n".join(r.summary)


def test_jobs_still_waiting_are_mentioned(conn, cfg):
    pipeline(conn, cfg, [job(1), job(2)], [60, None])
    r = report_for(conn, cfg)
    assert r.waiting == 1 and "1 job(s) are still waiting for a score" in r.text


def test_zero_day_with_everything_waiting(conn, cfg):
    pipeline(conn, cfg, [job(1), job(2)], [None, None])
    r = report_for(conn, cfg)
    assert r.jobs == [] and "No job matches in this batch. 2 job(s) are still waiting" in r.text


def test_a_job_the_user_archived_is_left_out(conn, cfg):
    ids, _ = pipeline(conn, cfg, [job(1), job(2)], [80, 70])
    db.archive_job(conn, ids[0], "user", "user_rejected")
    assert [j.job_id for j in report_for(conn, cfg).jobs] == [ids[1]]


def test_web_link_only_when_configured(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60])
    assert "All jobs" not in report_for(conn, cfg).text
    cfg["report"]["web_base_url"] = "http://nas.local:8080"
    r = report_for(conn, cfg)
    assert "All jobs: http://nas.local:8080/" in r.text and 'href="http://nas.local:8080/"' in r.html


# --- safety ---------------------------------------------------------------------------------------

def test_job_text_is_escaped_in_html(conn, cfg):
    evil = job(1, title='<script>alert(1)</script> & "Co"', company="<b>Evil</b>")
    pipeline(conn, cfg, [evil], [60])
    entry_summary = '<img src=x onerror=alert(1)>'
    ids = [r["job_id"] for r in conn.execute("SELECT job_id FROM batch_jobs")]
    conn.execute("UPDATE scores SET details = ? WHERE job_id = ?", (json.dumps({"summary": entry_summary, "matches": ["<i>m</i>"], "gaps": ["a&b"]}), ids[0]))
    conn.commit()
    r = report_for(conn, cfg)
    assert "<script>" not in r.html and "<b>Evil</b>" not in r.html and "<img" not in r.html and "<i>m</i>" not in r.html
    assert "&lt;script&gt;" in r.html and "a&amp;b" in r.html
    assert "<script>alert(1)</script>" in r.text                      # plain text is not markup


@pytest.mark.parametrize("url", ["javascript:alert(1)", "ftp://x/y", 'https://x/y"onmouseover="a', "https://x/y z", "data:text/html,x"])
def test_unsafe_links_are_dropped(conn, cfg, url):
    pipeline(conn, cfg, [job(1, url="")], [60])
    conn.execute("UPDATE job_sources SET url = ?", (url,))
    conn.commit()
    r = report_for(conn, cfg)
    assert r.jobs[0].url is None and "javascript:" not in r.html and "onmouseover" not in r.html


def test_no_home_address_password_or_cv_text_in_the_message(conn, cfg):
    cfg["location"]["car_origin_address"] = "Teststrasse 99, 8180 Buelach"
    pipeline(conn, cfg, [job(1)], [60])
    raw = build_message(cfg, report_for(conn, cfg)).as_string()
    assert "Teststrasse" not in raw and PASSWORD not in raw
    assert "Business Analyst / tester" not in raw          # variant labels, summaries and CV text are not sent


# --- the message ----------------------------------------------------------------------------------

def test_message_headers_and_parts(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60])
    msg = build_message(cfg, report_for(conn, cfg))
    assert msg["From"] == msg["To"] == "alex.morgan@example.com"
    assert msg["Subject"].startswith("Recruitment:") and msg["Message-ID"].endswith("@example.com>")
    assert msg["X-Recruitment-Batch"] == "run1" and msg["Date"]
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]
    assert "Analyst 1" in msg.get_body(("plain",)).get_content()


def test_non_ascii_survives_the_message(conn, cfg):
    pipeline(conn, cfg, [job(1, title="Analyst Zürich Łódź", locations=["Zürich"])], [60])
    msg = build_message(cfg, report_for(conn, cfg))
    assert "Analyst Zürich Łódź" in msg.get_body(("plain",)).get_content()
    assert "Zürich" in msg.get_body(("html",)).get_content()


# --- sending --------------------------------------------------------------------------------------

def test_send_uses_starttls_login_and_records_it(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60])
    assert send_report(conn, cfg, "run1", smtp_factory=FakeSMTP, now=NOW) == "sent"
    assert FakeSMTP.events == [("connect", "smtp.gmail.com", 587), ("starttls", True),
                               ("login", "alex.morgan@example.com", PASSWORD)]
    assert len(FakeSMTP.sent) == 1 and FakeSMTP.sent[0]["To"] == "alex.morgan@example.com"
    row = conn.execute("SELECT * FROM reports").fetchone()
    assert (row["status"], row["attempts"], row["error"], row["sent_at"]) == ("sent", 1, None, "2026-11-02T06:00:00Z")
    assert row["message_id"] == FakeSMTP.sent[0]["Message-ID"]


def test_same_batch_is_not_reported_twice_unless_forced(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60])
    send_report(conn, cfg, "run1", smtp_factory=FakeSMTP)
    assert send_report(conn, cfg, "run1", smtp_factory=FakeSMTP) == "already_sent"
    assert len(FakeSMTP.sent) == 1
    assert send_report(conn, cfg, "run1", smtp_factory=FakeSMTP, force=True) == "sent" and len(FakeSMTP.sent) == 2
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1


def test_missing_password_fails_without_connecting(conn, cfg, monkeypatch):
    pipeline(conn, cfg, [job(1)], [60])
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    with pytest.raises(ReportError, match="GMAIL_APP_PASSWORD"):
        send_report(conn, cfg, "run1", smtp_factory=FakeSMTP)
    assert FakeSMTP.events == []
    row = conn.execute("SELECT status, attempts, error FROM reports").fetchone()
    assert tuple(row) == ("failed", 0, "GMAIL_APP_PASSWORD is not set")


def test_login_refused_is_not_retried_and_hides_the_password(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60])
    FakeSMTP.failures = [smtplib.SMTPAuthenticationError(535, f"bad credentials {PASSWORD}".encode())]
    with pytest.raises(ReportError, match="check the app password") as exc:
        send_report(conn, cfg, "run1", smtp_factory=FakeSMTP)
    assert sum(e[0] == "login" for e in FakeSMTP.events) == 1
    row = conn.execute("SELECT * FROM reports").fetchone()
    assert row["status"] == "failed" and PASSWORD not in str(exc.value) and PASSWORD not in (row["error"] or "")


def test_transient_failure_is_retried(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60])
    cfg["email"]["retry_wait_seconds"] = 7
    FakeSMTP.failures = [smtplib.SMTPServerDisconnected("gone")]
    waits = []
    assert send_report(conn, cfg, "run1", smtp_factory=FakeSMTP, sleep=waits.append) == "sent"
    assert waits == [7] and conn.execute("SELECT attempts FROM reports").fetchone()[0] == 2


def test_all_attempts_fail_then_a_later_try_succeeds(conn, cfg):
    pipeline(conn, cfg, [job(1)], [60])
    FakeSMTP.failures = [TimeoutError(f"timeout talking to host with {PASSWORD}")] * 3
    with pytest.raises(ReportError, match="after 3 attempts") as exc:
        send_report(conn, cfg, "run1", smtp_factory=FakeSMTP, sleep=lambda s: None)
    row = conn.execute("SELECT * FROM reports").fetchone()
    assert (row["status"], row["attempts"]) == ("failed", 3) and PASSWORD not in row["error"] and PASSWORD not in str(exc.value)
    assert FakeSMTP.sent == []
    assert send_report(conn, cfg, "run1", smtp_factory=FakeSMTP) == "sent"             # failed rows are retried
    assert conn.execute("SELECT status FROM reports").fetchone()[0] == "sent"


def test_unknown_batch(conn, cfg):
    with pytest.raises(ReportError, match="unknown batch"):
        send_report(conn, cfg, "nope", smtp_factory=FakeSMTP)


def test_real_ubs_alerts_end_to_end(conn, cfg):
    cfg["tailoring"]["require_description"] = False        # the UBS alerts carry no description: without this all 7 would be queued (v0.72.0)
    db.start_run(conn, "run1")
    for p in sorted(UBS.glob("*.html")):
        for j in parse_alert(p.read_text(encoding="utf-8")).jobs:
            ingest_job(conn, to_job(j), "run1")
    filter_new_jobs(conn, cfg, "run1")
    build_batch(conn, cfg, "prompt", "v1", run_id="run1", now=NOW)
    ids = [r["job_id"] for r in conn.execute("SELECT job_id FROM batch_jobs ORDER BY job_id")]
    path = exchange_dir(cfg) / "results" / "r.json"
    path.write_text(json.dumps({"schema_version": "1.0", "run_id": "run1", "model": "m", "prompt_version": "v1",
                                "results": [score(i, 40 + 5 * n) for n, i in enumerate(ids)], "top": []}), encoding="utf-8")
    import_results(conn, cfg, path, NOW)
    r = report_for(conn, cfg)
    assert len(r.jobs) == 7 and "Opfikon" in r.text
    assert "Rejected by the filters this run: 2 (seniority_corporate_title 2)" in r.text
    assert send_report(conn, cfg, "run1", smtp_factory=FakeSMTP) == "sent"


def test_real_ubs_alerts_end_to_end_with_the_default_config_queues_every_title_only_job(conn, cfg):
    db.start_run(conn, "run1")
    for p in sorted(UBS.glob("*.html")):
        for j in parse_alert(p.read_text(encoding="utf-8")).jobs:
            ingest_job(conn, to_job(j), "run1")
    filter_new_jobs(conn, cfg, "run1")
    build_batch(conn, cfg, "prompt", "v1", run_id="run1", now=NOW)
    ids = [r["job_id"] for r in conn.execute("SELECT job_id FROM batch_jobs ORDER BY job_id")]
    path = exchange_dir(cfg) / "results" / "r.json"
    path.write_text(json.dumps({"schema_version": "1.0", "run_id": "run1", "model": "m", "prompt_version": "v1",
                                "results": [score(i, 40 + 5 * n) for n, i in enumerate(ids)], "top": []}), encoding="utf-8")
    import_results(conn, cfg, path, NOW)
    r = report_for(conn, cfg)
    assert r.jobs == [] and len(r.missing) == 7
    assert "WAITING FOR A DESCRIPTION (7)" in r.text
    assert r.subject.endswith("7 job(s) wait for a description (2 Nov 2026)") and "no job matches" not in r.subject
    assert send_report(conn, cfg, "run1", smtp_factory=FakeSMTP) == "sent"


def test_report_lists_given_up_jobs_with_tiers(conn, cfg):
    from app import descriptions
    db.start_run(conn, "run1")
    ids = [ingest_job(conn, job(i, description=""), "run1").job_id for i in (1, 2)]
    for jid, days in ((ids[0], 7), (ids[1], 2)):
        for d in range(days):
            conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) "
                         "VALUES (?, 'x', ?, 'T2', 'jobcloud', 'jobcloud', 'no_match')", (jid, f"2026-10-{10 + d}"))
    conn.commit()
    assert [g["job_id"] for g in descriptions.given_up_jobs(conn, 7)] == [ids[0]]
    filter_new_jobs(conn, cfg, "run1")
    build_batch(conn, cfg, "prompt", "v1", run_id="run1", now=NOW)
    r = report_for(conn, cfg)
    assert [g.job_id for g in r.given_up] == [ids[0]] and r.given_up[0].tiers == ["T2 jobcloud: no_match"]
    assert "GIVEN UP" in r.text and "Analyst 1" in r.text and "T2 jobcloud: no_match" in r.text
    assert "Analyst 2" not in r.text.split("GIVEN UP")[1].split("Run summary")[0]
    assert "Given up" in r.html and "Analyst 1" in r.html


# --- config ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, message", [
    ("email:\n  send_attempts: 0\n", "send_attempts"),
    ("email:\n  retry_wait_seconds: -1\n", "retry_wait_seconds"),
    ("report:\n  subject_prefix: ''\n", "subject_prefix"),
    ("report:\n  subject_prefix: \"a\\nBcc: x\"\n", "subject_prefix"),
    ("report:\n  web_base_url: javascript:x\n", "web_base_url"),
    ("report:\n  max_gaps: -1\n", "max_gaps"),
])
def test_bad_report_config(tmp_path, text, message):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(p)


def test_report_limits_default_to_three_matches_and_three_gaps(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("report:\n  subject_prefix: Recruitment\n", encoding="utf-8")     # no limits set: the defaults apply
    rep = load_config(p)["report"]
    assert (rep["max_matches"], rep["max_gaps"]) == (3, 3)


def test_load_env(tmp_path, monkeypatch):
    for k in ("A_KEY", "B_KEY", "C_KEY", "D_KEY"):
        monkeypatch.setenv(k, "x")     # registers the key so monkeypatch restores (removes) it afterwards
        monkeypatch.delenv(k)
    monkeypatch.setenv("C_KEY", "already")
    p = tmp_path / ".env"
    p.write_text("# comment\n\nA_KEY=one\nB_KEY = \"two words\"\nC_KEY=new\nD_KEY=\nbad line\n", encoding="utf-8")
    import os
    loaded = load_env(p)
    assert set(loaded) == {"A_KEY", "B_KEY"} and all(v == "" for v in loaded.values())     # values are never returned
    assert (os.environ["A_KEY"], os.environ["B_KEY"], os.environ["C_KEY"]) == ("one", "two words", "already")
    assert "D_KEY" not in os.environ
    assert load_env(tmp_path / "missing.env") == {}


def test_cv_fact_ids_are_not_shown_in_the_report(conn, cfg):
    ids, _ = pipeline(conn, cfg, [job(1)], [88])
    conn.execute("UPDATE scores SET details = ? WHERE job_id = ?", (json.dumps({
        "summary": "s", "matches": ["Reporting [exp-northwind-zurich-analysis]", "Build [exp-northwind-wroclaw-realestate] [edu-university]"],
        "gaps": ["No Kotlin [lang-en]", "[exp-x]"]}), ids[0]))
    conn.commit()
    r = report_for(conn, cfg, "run1")
    assert r.jobs[0].matches == ["Reporting", "Build"] and r.jobs[0].gaps == ["No Kotlin"]
    assert "exp-ubs" not in r.text and "exp-ubs" not in r.html and "[" not in r.html.split("MATCHES")[1].split("</ul>")[0]
