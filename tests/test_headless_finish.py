"""`finish --headless-done` (v0.78.0): the limit message, the reviewer's flags, the report lines and the three alerts."""
import json
import logging
from datetime import timedelta

import pytest

from app import alerts, cli, db, documents, pipeline
from app.config import load_config
from tests.test_batch_results import NOW
from tests.test_documents import prepare, send
from tests.test_report import PASSWORD, FakeSMTP


@pytest.fixture(autouse=True)
def reset_smtp():
    FakeSMTP.sent, FakeSMTP.events, FakeSMTP.failures = [], [], []


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", PASSWORD)
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    c["email"]["retry_wait_seconds"] = 0
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def drafts(conn, cfg, scores=(80, 70)):
    ids = prepare(conn, cfg, list(scores))
    send(cfg, ids, list(scores), with_docs=ids)
    return ids


def findings(conn, job_id, kind):
    row = conn.execute("SELECT findings, status FROM documents WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone()
    return json.loads(row["findings"]), row["status"]


def test_the_reviewers_flags_become_findings_and_the_documents_stay_drafts(conn, cfg):
    ids = drafts(conn, cfg)
    done = {"status": "ok", "attention": [{"job_id": ids[0], "kind": "cover_letter", "note": "German claim could not be settled"}]}
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW, headless_done=done)
    found, status = findings(conn, ids[0], "cover_letter")
    flagged = [f for f in found if f["kind"] == "review_attention"]
    assert len(flagged) == 1 and flagged[0]["reason"] == "German claim could not be settled" and flagged[0]["where"] == "cover_letter"
    assert status == "draft" and documents.state(conn, ids[0])[0] == "ready"      # not blocked, not approved
    assert [f for f in findings(conn, ids[1], "cover_letter")[0] if f["kind"] == "review_attention"] == []
    assert "NEEDS ATTENTION: cover_letter: German claim could not be settled" in documents.notes(conn, ids[0])


def test_the_report_shows_needs_attention_and_the_review_summary(conn, cfg):
    ids = drafts(conn, cfg)
    done = {"status": "ok", "review_summary": "Job 1: no change. Job 2: one claim softened.", "review_file": "llm/reviews/run1.md",
            "attention": [{"job_id": ids[0], "kind": "cv", "note": "bullet 3 rests on a fact I could not confirm"}]}
    r = pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW, headless_done=done)
    assert r.reports == {"run1": "sent"}
    body = FakeSMTP.sent[0].get_body(("plain",)).get_content()
    assert "NEEDS ATTENTION: cv: bullet 3 rests on a fact I could not confirm" in body
    assert "Review by the headless doc-reviewer: Job 1: no change. Job 2: one claim softened." in body
    assert "Full review stored in exchange/llm/reviews/run1.md" in body


def test_finish_without_a_done_file_adds_nothing(conn, cfg):
    ids = drafts(conn, cfg)
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW)
    assert [f for f in findings(conn, ids[0], "cover_letter")[0] if f["kind"] == "review_attention"] == []
    assert "NEEDS ATTENTION" not in FakeSMTP.sent[0].get_body(("plain",)).get_content()
    assert "headless" not in FakeSMTP.sent[0].get_body(("plain",)).get_content()


def test_a_bad_attention_item_is_ignored(conn, cfg):
    ids = drafts(conn, cfg)
    done = {"status": "ok", "attention": [{"job_id": "x", "kind": "cv", "note": "n"}, {"job_id": ids[0], "kind": "diary", "note": "n"},
                                          {"job_id": 9999, "kind": "cv", "note": "n"}, "junk"]}
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW, headless_done=done)
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE findings LIKE '%review_attention%'").fetchone()[0] == 0


def test_the_limit_message_of_the_done_file_labels_the_postponed_jobs(conn, cfg):
    ids = prepare(conn, cfg, [80, 70, 60])
    from tests.test_documents import entries
    from tests.test_batch_results import results_file
    results_file(cfg, "run1", entries(ids[:1], [80]), schema_version="1.1")                # only one job scored, the rest are postponed
    done = {"status": "usage_limit", "limit_message": "You've hit your session limit · resets 3pm (Europe/Zurich)"}
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW, headless_done=done)
    reasons = {r["reason"] for r in conn.execute("SELECT reason FROM postponements")}
    assert reasons == {"usage_limit"}


def test_without_the_done_file_the_postponed_jobs_are_no_result(conn, cfg):
    ids = prepare(conn, cfg, [80, 70, 60])
    from tests.test_documents import entries
    from tests.test_batch_results import results_file
    results_file(cfg, "run1", entries(ids[:1], [80]), schema_version="1.1")
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW)
    assert {r["reason"] for r in conn.execute("SELECT reason FROM postponements")} == {"no_result"}


# ---- alerts -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status, key", [("auth", "headless_auth"), ("usage_limit", "headless_usage_limit"), ("error", "headless_failed"),
                                         ("timeout", "headless_failed"), ("partial", "headless_failed")])
def test_each_failed_status_raises_its_alert(conn, cfg, status, key):
    alerts.observe_headless(conn, cfg, {"status": status, "message": "boom", "limit_message": "hit your session limit resets 3pm"}, NOW)
    open_keys = {a["key"] for a in alerts.open_alerts(conn)}
    assert open_keys == {key}


def test_a_good_run_closes_the_headless_alerts(conn, cfg):
    alerts.observe_headless(conn, cfg, {"status": "auth"}, NOW)
    alerts.observe_headless(conn, cfg, {"status": "ok"}, NOW)
    assert alerts.open_alerts(conn) == []


def test_a_usage_limit_after_an_auth_failure_closes_the_auth_alert(conn, cfg):
    alerts.observe_headless(conn, cfg, {"status": "auth"}, NOW)
    alerts.observe_headless(conn, cfg, {"status": "usage_limit", "limit_message": "hit your session limit"}, NOW)
    assert {a["key"] for a in alerts.open_alerts(conn)} == {"headless_usage_limit"}


def test_no_done_file_changes_no_alert(conn, cfg):
    alerts.observe_headless(conn, cfg, {"status": "auth"}, NOW)
    alerts.observe_headless(conn, cfg, None, NOW)
    assert {a["key"] for a in alerts.open_alerts(conn)} == {"headless_auth"}


def test_a_resumed_run_that_gave_up_raises_headless_failed(conn, cfg):
    alerts.observe_headless(conn, cfg, {"status": "ok", "resumed": [{"run_id": "r0", "status": "error", "message": "gave up"}]}, NOW)
    assert {a["key"] for a in alerts.open_alerts(conn)} == {"headless_failed"}


def test_the_auth_alert_has_a_hint_and_a_title_and_never_the_token(conn, cfg):
    assert "claude setup-token" in alerts.HINTS["headless_auth"] and "llm.env" in alerts.HINTS["headless_auth"]
    assert alerts.short_title("headless_auth") == "headless Claude login refused"
    assert alerts._hint("headless_usage_limit") and alerts._hint("headless_failed")


# ---- the command --------------------------------------------------------------------------------------------------

def test_finish_command_reads_the_done_file_and_raises_the_alert(conn, cfg, tmp_path, monkeypatch):
    config = tmp_path / "c.yaml"
    config.write_text(f"paths:\n  database: {(tmp_path / 'cli.db').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
                      f"  lock: {(tmp_path / 'run.lock').as_posix()}\n  logs: {(tmp_path / 'logs').as_posix()}\n", encoding="utf-8")
    done = tmp_path / "done.json"
    done.write_text(json.dumps({"status": "auth", "message": "x"}), encoding="utf-8")
    assert cli.main(["--config", str(config), "finish", "--headless-done", str(done)], smtp_factory=FakeSMTP) == 0
    c = db.connect(tmp_path / "cli.db")
    try:
        assert {a["key"] for a in alerts.open_alerts(c)} == {"headless_auth"}
    finally:
        c.close()


def test_finish_command_survives_an_unreadable_done_file(tmp_path, capsys):
    config = tmp_path / "c.yaml"
    config.write_text(f"paths:\n  database: {(tmp_path / 'cli.db').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
                      f"  lock: {(tmp_path / 'run.lock').as_posix()}\n  logs: {(tmp_path / 'logs').as_posix()}\n", encoding="utf-8")
    assert cli.main(["--config", str(config), "finish", "--headless-done", str(tmp_path / "missing.json")], smtp_factory=FakeSMTP) == 0
    assert "cannot be read" in capsys.readouterr().err


# ---- v0.78.2: alerts tied to the right run, and what finish logs ---------------------------------------------------------

def test_alert_names_run_and_done_path_after_the_cut(conn, cfg):
    done = {"run_id": "2026-11-07T060403Z", "status": "error", "message": "boom " * 200,
            "resumed": [{"run_id": "2026-11-06T060403Z", "status": "error", "message": "gave up"}]}
    alerts.observe_headless(conn, cfg, done, NOW)
    (alert,) = alerts.open_alerts(conn)
    prefix = "run 2026-11-07T060403Z (exchange/llm/done/2026-11-07T060403Z.json): "
    assert alert["key"] == "headless_failed" and alert["message"].startswith(prefix) and len(alert["message"]) <= alerts.MAX_MESSAGE
    assert alert["count"] == 1                                                         # one sighting per key and finish, not one per run
    body = alerts.raised_message(cfg, conn.execute("SELECT * FROM alerts").fetchone()).get_content()
    assert "Last seen:" in body and prefix in body


def test_alert_log_says_reminder_and_origin_run(conn, cfg, caplog):
    caplog.set_level(logging.INFO, logger="recruitment")
    alerts.observe_headless(conn, cfg, {"run_id": "r7", "status": "auth"}, NOW)
    alerts.deliver(conn, cfg, smtp_factory=FakeSMTP, sleep=lambda s: None, now=NOW)
    alerts.observe_headless(conn, cfg, {"run_id": "r8", "status": "auth"}, NOW + timedelta(days=1))
    alerts.deliver(conn, cfg, smtp_factory=FakeSMTP, sleep=lambda s: None, now=NOW + timedelta(days=1))
    sent = [r.getMessage() for r in caplog.records if "event=alert_mail" in r.getMessage()]
    assert len(sent) == 2 and "kind=new" in sent[0] and "origin_run=r7" in sent[0] and "key=headless_auth" in sent[0]
    assert "kind=reminder" in sent[1] and "origin_run=r8" in sent[1]


def test_finish_logs_each_file_and_run_document_counts(conn, cfg, caplog):
    caplog.set_level(logging.INFO, logger="recruitment")
    drafts(conn, cfg)
    done = {"run_id": "run1", "status": "partial", "dropped": [{"job_id": 3, "stage": "scorer", "path": "results/2/documents/cv"}]}
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW, headless_done=done)
    lines = [r.getMessage() for r in caplog.records]
    imported = [m for m in lines if m.startswith("event=import ")]
    assert len(imported) == 1 and "file=results_run1.json" in imported[0] and "status=imported" in imported[0]
    assert "scored=2" in imported[0] and "skipped=0" in imported[0]
    (summary,) = [m for m in lines if m.startswith("event=run_summary ")]
    assert "files=1" in summary and "scored=2" in summary and "documents_written=4" in summary and "documents_missing=0" in summary
    assert "dropped=1" in summary


def test_the_report_lists_the_jobs_the_worker_dropped(conn, cfg):
    drafts(conn, cfg)
    done = {"run_id": "run1", "status": "partial", "dropped": [{"job_id": 3, "stage": "scorer", "path": "results/2/documents/cv"}]}
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW, headless_done=done)
    body = FakeSMTP.sent[0].get_body(("plain",)).get_content()
    assert "dropped by the headless checks" in body and "job 3 (scorer, results/2/documents/cv)" in body


def test_the_reviewers_flags_and_summary_of_a_resumed_run_are_kept(conn, cfg):
    """A no-batch request resumes a run: its flags and summary sit under `resumed`, not at the top of the done file (v0.78.9)."""
    ids = drafts(conn, cfg)
    done = {"status": "ok", "attention": [], "review_summary": "", "resumed": [
        {"run_id": "r0", "status": "ok", "review_summary": "Job 1: letter shortened.", "review_file": "llm/reviews/r0.md",
         "attention": [{"job_id": ids[0], "kind": "cover_letter", "note": "word count may be under 290"}]}]}
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW, headless_done=done)
    found, status = findings(conn, ids[0], "cover_letter")
    assert [f["reason"] for f in found if f["kind"] == "review_attention"] == ["word count may be under 290"]
    assert status == "draft"
    body = FakeSMTP.sent[0].get_body(("plain",)).get_content()
    assert "NEEDS ATTENTION: cover_letter: word count may be under 290" in body
    assert "Review by the headless doc-reviewer: Job 1: letter shortened." in body
    assert "Full review stored in exchange/llm/reviews/r0.md" in body


def test_a_bad_resumed_entry_is_ignored(conn, cfg):
    ids = drafts(conn, cfg)
    for resumed in ("x", ["x", None, {"attention": "x"}, {"attention": [{"job_id": "x"}]}]):
        assert pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW, headless_done={"status": "ok", "resumed": resumed}) is not None
    assert findings(conn, ids[0], "cover_letter")[1] == "draft"


def test_a_reviewers_flag_is_kept_on_a_blocked_document_and_it_stays_blocked(conn, cfg):
    ids = drafts(conn, cfg)
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW)
    conn.execute("UPDATE documents SET status = 'blocked' WHERE job_id = ? AND kind = 'cover_letter'", (ids[0],))
    conn.commit()
    assert documents.add_review_attention(conn, ids[0], "cover_letter", "word count may be under 290") is True
    found, status = findings(conn, ids[0], "cover_letter")
    assert status == "blocked" and any(f["kind"] == "review_attention" for f in found)


# ----------------------------------------------------------------------------- recheck of stored blocked documents (v0.78.11)

def blocked_docs(conn, cfg):
    ids = drafts(conn, cfg)
    pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW)
    old = json.dumps([{"kind": "skill", "text": "Old", "block": 0, "where": "opening", "reason": "r", "block_text": ""},
                      {"kind": "review_attention", "text": "", "block": None, "where": "cv", "reason": "keep me", "block_text": ""}])
    conn.execute("UPDATE documents SET status = 'blocked', findings = ?, pdf_path = NULL WHERE job_id = ?", (old, ids[0]))
    conn.commit()
    return ids


def test_the_preview_of_a_recheck_changes_nothing(conn, cfg):
    ids = blocked_docs(conn, cfg)
    before = conn.execute("SELECT status, findings, pdf_path FROM documents ORDER BY id").fetchall()
    results = documents.recheck_blocked(conn, cfg)
    assert {(o["job_id"], o["kind"], o["status"]) for o in results} == {(ids[0], "cover_letter", "draft"), (ids[0], "cv", "draft")}
    assert [tuple(r) for r in conn.execute("SELECT status, findings, pdf_path FROM documents ORDER BY id")] == [tuple(r) for r in before]


def test_a_recheck_unblocks_letters_and_cvs_builds_the_pdf_and_keeps_the_flag(conn, cfg):
    ids = blocked_docs(conn, cfg)
    documents.recheck_blocked(conn, cfg, apply=True)
    for kind in ("cover_letter", "cv"):
        found, status = findings(conn, ids[0], kind)
        assert status == "draft" and [f["reason"] for f in found if f["kind"] == "review_attention"] == ["keep me"]
        assert not [f for f in found if f["kind"] == "skill"]
        assert conn.execute("SELECT pdf_path FROM documents WHERE job_id = ? AND kind = ?", (ids[0], kind)).fetchone()[0]


def test_a_recheck_keeps_a_real_claim_blocked(conn, cfg):
    ids = blocked_docs(conn, cfg)
    content = json.loads(conn.execute("SELECT content FROM documents WHERE job_id = ? AND kind = 'cover_letter'", (ids[0],)).fetchone()[0])
    content["blocks"]["opening"] = "I built Kotlin services for years."
    conn.execute("UPDATE documents SET content = ? WHERE job_id = ? AND kind = 'cover_letter'", (json.dumps(content), ids[0]))
    conn.commit()
    documents.recheck_blocked(conn, cfg, apply=True)
    found, status = findings(conn, ids[0], "cover_letter")
    assert status == "blocked" and any(f["text"] == "Kotlin" for f in found)
    assert not conn.execute("SELECT pdf_path FROM documents WHERE job_id = ? AND kind = 'cover_letter'", (ids[0],)).fetchone()[0]


def test_a_usage_limit_of_a_resumed_run_gives_the_alert_its_reset_time(conn, cfg):
    done = {"run_id": "run1", "status": "ok", "resumed": [
        {"run_id": "run0", "status": "usage_limit", "limit_message": "hit your session limit resets 3pm"}]}
    alerts.observe_headless(conn, cfg, done, NOW)
    (a,) = alerts.open_alerts(conn)
    assert a["key"] == "headless_usage_limit" and "resets 3pm" in a["message"]
