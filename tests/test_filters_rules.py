from pathlib import Path

import pytest

from app import db
from app.config import load_config
from app.dedup import ingest_job
from app.filters import rules
from app.filters.base import JobContext, load_context
from app.filters.runner import DEFAULT_FILTERS, filter_new_jobs, run_filters
from app.models import NormalizedJob
from app.parsers.ubs import parse_alert, to_job

FIX = Path(__file__).parent / "fixtures" / "ubs"


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    return load_config()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def ctx(title="Business Analyst", company="Acme", corporate_title="", **kw):
    raw = {"Corporate title": corporate_title} if corporate_title else {}
    return JobContext(id=1, title=title, company=company, description="", location_text="", raw=raw, **kw)


def only(results):
    assert len(results) == 1
    return results[0]


# --- seniority: UBS corporate title ---------------------------------------------------------

@pytest.mark.parametrize("code", [
    "VP - Vice President", "D - Director", "DI - Director", "ED - Executive Director", "MD - Managing Director",
])
def test_ubs_corporate_titles_above_ad_are_rejected(cfg, code):
    r = only(rules.seniority(ctx(corporate_title=code), cfg))
    assert (r.outcome, r.reason_code) == ("fail", "seniority_corporate_title") and code in r.comment


@pytest.mark.parametrize("code", [
    "AD - Associate Director", "AVP - Assistant Vice President", "M - Manager", "JM - Junior Manager",
    "SS - Senior Specialist", "S - Specialist", "JS - Junior Specialist", "Some Future Code",
])
def test_ubs_corporate_titles_up_to_ad_pass(cfg, code):
    assert only(rules.seniority(ctx(corporate_title=code), cfg)).outcome == "pass"


def test_corporate_title_match_ignores_case_and_spacing(cfg):
    assert only(rules.seniority(ctx(corporate_title="  di -   director "), cfg)).outcome == "fail"


def test_ubs_corporate_title_decides_even_if_title_says_head_of(cfg):
    # goal.md 5.3: for UBS the Corporate title column decides; the title patterns are for other sources
    assert only(rules.seniority(ctx(title="Head of Reporting", corporate_title="AD - Associate Director"), cfg)).outcome == "pass"


# --- seniority: title patterns for other sources --------------------------------------------

@pytest.mark.parametrize("title, hit", [
    ("Vice President, Data", "Vice President"), ("VP Finance - Vice President", "Vice President"),
    ("Director Data Governance", "Director"), ("Managing Director Trading", "Director"),
    ("Head of Reporting", "Head of"), ("head of analytics", "Head of"), ("Chief Data Officer", "Chief"),
    ("Associate Director and Head of Reporting", "Head of"),
])
def test_title_patterns_reject(cfg, title, hit):
    r = only(rules.seniority(ctx(title=title), cfg))
    assert r.outcome == "fail" and r.reason_code == "seniority_title_pattern" and hit.casefold() in r.comment.casefold()


def test_double_space_in_head_of_is_a_known_limit(cfg):
    # patterns are matched on the title as written; whitespace is not collapsed (listed in DEVELOPMENT.md)
    assert only(rules.seniority(ctx(title="Head  of Data"), cfg)).outcome == "pass"


@pytest.mark.parametrize("title", [
    "Assistant Vice President, Business Analyst", "Associate Director, Data Management",
    "Business Analyst", "Senior Business Analyst", "Reporting Analyst", "Automation Developer",
    "Business Manager, IB Unified Global Markets Distribution COO",     # real UBS title: 'COO' is not 'Chief'
    "Directory Services Engineer", "Chiefdom Analyst", "Requirements Engineer (m/f/d)",
])
def test_titles_that_must_pass(cfg, title):
    assert only(rules.seniority(ctx(title=title), cfg)).outcome == "pass"


def test_title_pattern_is_case_insensitive(cfg):
    assert only(rules.seniority(ctx(title="DIRECTOR OF DATA"), cfg)).outcome == "fail"


def test_config_change_takes_effect(cfg):
    cfg["filters"]["seniority_reject_title_patterns"].append("Principal")
    assert only(rules.seniority(ctx(title="Principal Engineer"), cfg)).outcome == "fail"
    cfg["filters"]["seniority_reject_title_patterns"] = []
    assert only(rules.seniority(ctx(title="Director of X"), cfg)).outcome == "pass"


# --- title include / exclude ----------------------------------------------------------------

def test_no_role_prefilter_by_default(cfg):
    assert only(rules.title(ctx(title="Pastry Chef"), cfg)).outcome == "pass"


def test_title_exclude(cfg):
    cfg["filters"]["title_exclude"] = ["intern(ship)?", "trader"]
    r = only(rules.title(ctx(title="Junior Crypto Trader"), cfg))
    assert (r.outcome, r.reason_code) == ("fail", "title_excluded")
    assert only(rules.title(ctx(title="Internal Auditor"), cfg)).outcome == "pass"   # whole words only


def test_title_include(cfg):
    cfg["filters"]["title_include"] = ["analyst", "engineer"]
    assert only(rules.title(ctx(title="Data Analyst"), cfg)).outcome == "pass"
    r = only(rules.title(ctx(title="Client Advisor"), cfg))
    assert (r.outcome, r.reason_code) == ("fail", "title_not_included")


def test_exclude_beats_include(cfg):
    cfg["filters"].update(title_include=["analyst"], title_exclude=["junior"])
    assert only(rules.title(ctx(title="Junior Analyst"), cfg)).outcome == "fail"


# --- company --------------------------------------------------------------------------------

def test_excluded_company_matches_after_normalisation(cfg):
    cfg["filters"]["excluded_companies"] = ["Acme AG"]
    for name in ("ACME", "Acme AG", "acmé"):
        r = only(rules.company(ctx(company=name), cfg))
        assert (r.outcome, r.reason_code) == ("fail", "company_excluded")
    assert only(rules.company(ctx(company="Acme Holding"), cfg)).outcome == "pass"


def test_empty_exclude_list_and_blank_entries(cfg):
    assert only(rules.company(ctx(company="Anyone"), cfg)).outcome == "pass"
    cfg["filters"]["excluded_companies"] = ["", "  "]
    assert only(rules.company(ctx(company=""), cfg)).outcome == "pass"


# --- runner: audit trail, archive, idempotency ----------------------------------------------

def add(conn, **kw):
    base = dict(source="s", external_id="1", title="Business Analyst", company="Acme", url="", locations=["Zürich"])
    base.update(kw)
    return ingest_job(conn, NormalizedJob(**base)).job_id


def results_of(conn, job_id):
    return [tuple(r) for r in conn.execute(
        "SELECT filter, outcome, reason_code FROM job_filter_results WHERE job_id=? ORDER BY id", (job_id,))]


def test_passing_job_stays_and_every_filter_is_recorded(conn, cfg):
    jid = add(conn)
    assert run_filters(conn, jid, cfg) == "pass"
    assert results_of(conn, jid) == [("seniority", "pass", None), ("title", "pass", None), ("company", "pass", None),
                              ("language", "pass", "language_not_assessed")]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_failing_job_is_archived_with_reason_and_audit_trail(conn, cfg):
    jid = add(conn, title="Director Data Governance")
    assert run_filters(conn, jid, cfg, run_id=None) == "fail"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    row = conn.execute("SELECT * FROM job_archive").fetchone()
    assert (row["rejection_stage"], row["reason_code"]) == ("filter", "seniority_title_pattern")
    assert "seniority:" in row["comment"]
    import json
    trail = json.loads(row["record"])["filter_results"]
    assert [(t["filter"], t["outcome"]) for t in trail] == [
        ("seniority", "fail"), ("title", "pass"), ("company", "pass"), ("language", "pass")]


def test_several_failures_are_all_named(conn, cfg):
    cfg["filters"]["excluded_companies"] = ["Acme"]
    jid = add(conn, title="Head of Data")
    run_filters(conn, jid, cfg)
    row = conn.execute("SELECT reason_code, comment FROM job_archive").fetchone()
    assert row["reason_code"] == "seniority_title_pattern"       # first failing filter
    assert "seniority:" in row["comment"] and "company:" in row["comment"]


def test_conditional_result_keeps_the_job(conn, cfg):
    jid = add(conn)
    from app.filters.base import FilterResult
    cond = lambda job, cfg: [FilterResult("location", "conditional", "location_unspecified", "no place named")]
    assert run_filters(conn, jid, cfg, filters=[*DEFAULT_FILTERS, cond]) == "conditional"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert ("location", "conditional", "location_unspecified") in results_of(conn, jid)


def test_filter_new_jobs_is_idempotent(conn, cfg):
    add(conn, external_id="1")
    add(conn, external_id="2", title="Head of X", locations=["Bern"])
    assert filter_new_jobs(conn, cfg) == {"pass": 1, "conditional": 0, "fail": 1}
    assert filter_new_jobs(conn, cfg) == {"pass": 0, "conditional": 0, "fail": 0}     # nothing filtered twice
    # the passing job keeps its 4 rows; the rejected job's rows moved into its archive record
    assert conn.execute("SELECT COUNT(*) FROM job_filter_results").fetchone()[0] == 4


def test_rejected_job_arriving_again_is_not_new(conn, cfg):
    jid = add(conn, title="Head of Data")
    run_filters(conn, jid, cfg)
    again = ingest_job(conn, NormalizedJob(source="s", external_id="1", title="Head of Data", company="Acme",
                                           locations=["Zürich"], source_date="2026-11-01"))
    assert again.outcome == "archived"


def test_load_context_reads_ubs_corporate_title(conn):
    j = parse_alert((FIX / "alert_2026-10-24.html").read_text(encoding="utf-8")).jobs[0]
    jid = ingest_job(conn, to_job(j)).job_id
    c = load_context(conn, jid)
    assert c.corporate_title == "DI - Director" and c.sources == ["ubs_alert"]
    with pytest.raises(KeyError):
        load_context(conn, 999)


def test_real_alerts_end_to_end(conn, cfg):
    for name in sorted(p.name for p in FIX.glob("*.html")):
        for j in parse_alert((FIX / name).read_text(encoding="utf-8")).jobs:
            ingest_job(conn, to_job(j, message_id=name))
    counts = filter_new_jobs(conn, cfg)
    assert counts == {"pass": 7, "conditional": 0, "fail": 2}
    archived = {r["external_id"]: r["reason_code"] for r in conn.execute("SELECT external_id, reason_code FROM job_archive")}
    assert archived == {"347642": "seniority_corporate_title", "352530": "seniority_corporate_title"}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 7
