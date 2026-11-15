"""v0.85.0: app/revisions.py, the recruitment side of "Ask the writer to fix" (goal.md 5.10a): relay with the address scrub, finish, expire,
the mail, the CLI step. Fake SMTP, temporary databases, no network."""
import copy
import json
import logging
import smtplib
from datetime import datetime, timedelta, timezone

import pytest

from app import alerts, cli, db, documents, pipeline, revisions
from app.config import ConfigError, _validate_headless, load_config
from tests.test_approval import doc_row
from tests.test_documents import CV
from tests.test_findings_override import blocked
from tests.test_report import FakeSMTP, reset_smtp                                  # noqa: F401
from tests.test_documents import conn                                               # noqa: F401
from tests.test_cli import config_file                                              # noqa: F401

HOME = "Rosenweg 12, 8180 Exampletown"
NOW = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pass-word-123456")
    monkeypatch.delenv("GMAIL_USER", raising=False)
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), database=str(tmp_path / "t.db"), lock=str(tmp_path / "run.lock"),
                      logs=str(tmp_path / "logs"))
    c["email"]["retry_wait_seconds"] = 0
    c["headless"]["enabled"] = True
    c["location"]["car_origin_address"] = HOME
    return c


def press(conn, row, kind="cv", instruction="make it shorter", status="pending"):
    with conn:
        cur = conn.execute("INSERT INTO revision_requests (document_id, kind, instruction, content_sha, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                           (row["id"], kind, instruction, documents.content_sha(row["content"]), status, "2026-11-12T07:00:00Z"))
    return cur.lastrowid


def req(conn, rid):
    return dict(conn.execute("SELECT * FROM revision_requests WHERE id = ?", (rid,)).fetchone())


def llm(cfg):
    from pathlib import Path
    return Path(cfg["paths"]["exchange"]) / "llm"


def run(conn, cfg, **kw):
    return revisions.run(conn, cfg, now=kw.pop("now", NOW), smtp_factory=FakeSMTP, **kw)


def request_file(cfg, rid):
    return llm(cfg) / "requests" / f"rev-{rid}.json"


def write_done(cfg, rid, name=None, **data):
    folder = llm(cfg) / "done"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or f"rev-{rid}.json")
    path.write_text(json.dumps({"run_id": f"rev-{rid}", "revision_id": rid, **data}), encoding="utf-8")
    return path


def good_done(row, rid, **more):
    return {"status": "ok", "document_kind": "cv", "content_sha": documents.content_sha(row["content"]), "document": copy.deepcopy(CV),
            "attention": [{"job_id": row["job_id"], "kind": "cv", "note": "check the summary"}], "review_summary": "fine", **more}


# ---- relay -------------------------------------------------------------------------------------------------------

def test_the_relay_writes_the_contract_and_marks_relayed_before_the_file(conn, cfg, monkeypatch):
    jid, row, fs = blocked(conn, cfg)
    rid = press(conn, row)
    seen = {}
    from app import scheduler
    real = scheduler._write_json

    def spy(path, data):
        seen["status"] = req(conn, rid)["status"]                    # the row is relayed (and committed) when the file is written
        return real(path, data)
    monkeypatch.setattr(scheduler, "_write_json", spy)
    result = run(conn, cfg)
    assert seen["status"] == "relayed" and result.relayed == 1
    data = json.loads(request_file(cfg, rid).read_text(encoding="utf-8"))
    assert data["kind"] == "revision" and data["run_id"] == f"rev-{rid}" and data["revision_id"] == rid
    assert data["document_kind"] == "cv" and data["content_sha"] == documents.content_sha(row["content"]) and data["instruction"] == "make it shorter"
    assert set(data["roles"]) == {"scorer", "review"} and data["roles"]["scorer"]["timeout_minutes"] == 20 and data["roles"]["review"]["max_turns"] == 40
    assert data["roles"]["review"]["model"] == cfg["headless"]["roles"]["review"]["model"]
    assert data["job"]["job_id"] == jid and data["document"] == json.loads(row["content"])
    assert {f["kind"] for f in data["findings"]} >= {f["kind"] for f in fs} and all("warning" in f for f in data["findings"])
    assert data["deadline"] == "2026-11-12T09:15:00+00:00"          # now + headless.revision.wait_minutes (75)
    r = req(conn, rid)
    assert r["status"] == "relayed" and r["relayed_at"] == "2026-11-12T08:00:00Z" and r["deadline"] == "2026-11-12T09:15:00Z"
    from app.headless import validate_revision_request
    assert validate_revision_request(data, f"rev-{rid}") is None      # the worker accepts what the relay writes


def test_a_cover_letter_request_names_the_letters_role(conn, cfg):
    from tests.test_approval import imported
    ids = imported(conn, cfg, mutate=lambda d: _spoil_letter(d))
    row = doc_row(conn, ids[0], "cover_letter")
    rid = press(conn, row, kind="cover_letter")
    run(conn, cfg)
    assert set(json.loads(request_file(cfg, rid).read_text(encoding="utf-8"))["roles"]) == {"letters", "review"}


def _spoil_letter(d):
    d["cover_letter"]["blocks"]["skills"] += " I speak fluent German."
    return d


def test_accepted_overrides_are_not_sent_as_findings(conn, cfg):
    from app import documents as d
    jid, row, fs = blocked(conn, cfg)
    d.add_override(conn, jid, "cv", *d.finding_key(fs[0]), "ok")
    conn.commit()
    rid = press(conn, row)
    run(conn, cfg)
    sent = json.loads(request_file(cfg, rid).read_text(encoding="utf-8"))["findings"]
    assert not any(f["text"] == fs[0]["text"] and f["kind"] == fs[0]["kind"] for f in sent)


def test_no_address_byte_reaches_the_request_file(conn, cfg, caplog):
    jid, row, fs = blocked(conn, cfg)
    content = json.loads(row["content"])
    content["summary"] = f"I live at {HOME} and like clear requirements."
    findings = json.loads(row["findings"]) + [{"kind": "number", "text": "see Rosenweg 12, 8180 Exampletown", "block": None, "where": "summary",
                                               "reason": "r", "block_text": ""}]
    with conn:
        conn.execute("UPDATE documents SET content = ?, findings = ? WHERE id = ?", (json.dumps(content, ensure_ascii=False), json.dumps(findings), row["id"]))
        conn.execute("UPDATE jobs SET description = ? WHERE id = ?", ("Our office is at ROSENWEG 12,\n8180 Exampletown near the station.", jid))
    row = doc_row(conn, jid, "cv")
    rid = press(conn, row, instruction="Do not mention rosenweg 12 8180 Exampletown, please.")
    caplog.set_level(logging.INFO, logger="recruitment")
    run(conn, cfg)
    raw = request_file(cfg, rid).read_bytes()
    text = raw.decode("utf-8")
    for needle in (HOME, "Rosenweg 12", "8180 Exampletown", "rosenweg 12, 8180 Exampletown", "Rosenweg 12, 8180 Exampletown"):
        assert needle.encode("utf-8") not in raw and needle.lower() not in text.lower()
    parsed = json.loads(text)
    assert not documents._address_findings(parsed, HOME) and not any(documents.address_trace(s, HOME) for s in documents._strings(parsed))
    assert parsed["document"]["summary"].count(documents.ADDRESS_MARKER) == 1 and documents.ADDRESS_MARKER in parsed["instruction"]
    assert documents.ADDRESS_MARKER in parsed["job"]["description"]
    assert req(conn, rid)["scrubbed"] == 4 and req(conn, rid)["status"] == "relayed"
    assert "Rosenweg" not in caplog.text and "rosenweg" not in caplog.text.lower() and "8180" not in caplog.text


def test_when_the_scrub_fails_no_file_is_written_and_the_request_fails(conn, cfg, monkeypatch):
    jid, row, _ = blocked(conn, cfg)
    content = json.loads(row["content"])
    content["summary"] = f"I live at {HOME}."
    with conn:
        conn.execute("UPDATE documents SET content = ? WHERE id = ?", (json.dumps(content, ensure_ascii=False), row["id"]))
    rid = press(conn, doc_row(conn, jid, "cv"))
    monkeypatch.setattr(documents, "scrub_address", lambda obj, home: (obj, 0))
    run(conn, cfg)
    r = req(conn, rid)
    assert not request_file(cfg, rid).exists() and (r["status"], r["outcome"]) == ("failed", "invalid") and "nothing was sent" in r["message"]
    assert len(FakeSMTP.sent) == 1 and "failed" in FakeSMTP.sent[0]["Subject"]
    assert "Rosenweg" not in FakeSMTP.sent[0].get_content()


def test_a_stale_request_is_not_sent(conn, cfg):
    jid, row, _ = blocked(conn, cfg)
    rid = press(conn, row)
    with conn:
        conn.execute("UPDATE documents SET content = content || ' ' WHERE id = ?", (row["id"],))
    run(conn, cfg)
    assert req(conn, rid)["status"] == "stale" and not request_file(cfg, rid).exists()
    assert len(FakeSMTP.sent) == 1 and "not stored" in FakeSMTP.sent[0]["Subject"]


def test_a_pending_request_waits_while_another_is_relayed(conn, cfg):
    jid, row, _ = blocked(conn, cfg)
    other = doc_row(conn, jid, "cover_letter")
    with conn:
        conn.execute("UPDATE documents SET status = 'blocked' WHERE id = ?", (other["id"],))
    other = doc_row(conn, jid, "cover_letter")
    a = press(conn, row)
    b = press(conn, other, kind="cover_letter")
    run(conn, cfg)
    assert req(conn, a)["status"] == "relayed" and req(conn, b)["status"] == "pending" and not request_file(cfg, b).exists()
    assert revisions.has_work(cfg, NOW, relay=True) is False         # nothing to do until the first one is answered


# ---- finish ------------------------------------------------------------------------------------------------------

def relayed(conn, cfg, **kw):
    jid, row, _ = blocked(conn, cfg)
    rid = press(conn, row, **kw)
    run(conn, cfg)
    assert req(conn, rid)["status"] == "relayed"
    return jid, row, rid


def test_an_ok_done_file_is_stored_and_mailed_and_renamed(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    path = write_done(cfg, rid, **good_done(row, rid))
    run(conn, cfg)
    r = req(conn, rid)
    assert (r["status"], r["outcome"]) == ("stored", "draft") and r["mailed_at"] and not r["mail_error"]
    assert doc_row(conn, jid, "cv")["status"] == "draft" and not path.exists() and path.with_name(path.name + ".processed").exists()
    mail = FakeSMTP.sent[-1]
    assert "document fixed" in mail["Subject"] and "stored as a draft" in mail.get_content() and "check the summary" in mail.get_content()


def test_a_still_blocked_revision_is_stored_blocked_and_the_mail_lists_what_is_left(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    bad = copy.deepcopy(CV)
    bad["experience"][0]["bullets"][1]["text"] = "Cut 7 million CHF of costs at Northwind."
    write_done(cfg, rid, **good_done(row, rid, document=bad))
    run(conn, cfg)
    assert (req(conn, rid)["status"], req(conn, rid)["outcome"]) == ("stored", "blocked") and doc_row(conn, jid, "cv")["status"] == "blocked"
    body = FakeSMTP.sent[-1].get_content()
    assert "still blocked" in FakeSMTP.sent[-1]["Subject"] and "still blocked" in body and "finding(s) left" in body


def test_the_mail_says_when_the_address_was_removed(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    with conn:
        conn.execute("UPDATE revision_requests SET scrubbed = 2 WHERE id = ?", (rid,))
    write_done(cfg, rid, **good_done(row, rid))
    run(conn, cfg)
    assert "The home address was removed before sending." in FakeSMTP.sent[-1].get_content()
    assert "Rosenweg" not in FakeSMTP.sent[-1].get_content()


@pytest.mark.parametrize("status", ["usage_limit", "auth", "timeout", "error"])
def test_each_failed_status_is_stored_and_mailed_with_the_reason(conn, cfg, status):
    jid, row, rid = relayed(conn, cfg)
    write_done(cfg, rid, status=status, message="boom reason", limit_message="resets 3pm")
    run(conn, cfg)
    r = req(conn, rid)
    assert (r["status"], r["outcome"]) == ("failed", status) and r["message"] and r["mailed_at"]
    assert doc_row(conn, jid, "cv")["status"] == "blocked" and doc_row(conn, jid, "cv")["content"] == row["content"]
    assert any("failed" in m["Subject"] and revisions.report.REVISION_REASONS[status] in m.get_content() for m in FakeSMTP.sent)
    keys = [x["key"] for x in conn.execute("SELECT key FROM alerts")]
    assert (alerts.HEADLESS_AUTH_KEY in keys) == (status == "auth")


def test_a_done_file_for_another_document_state_is_stale(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    with conn:
        conn.execute("UPDATE documents SET content = content || ' ' WHERE id = ?", (row["id"],))
    write_done(cfg, rid, **good_done(row, rid))
    run(conn, cfg)
    assert req(conn, rid)["status"] == "stale" and "not stored" in FakeSMTP.sent[-1]["Subject"]


def test_a_done_file_with_the_wrong_sha_is_failed(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    write_done(cfg, rid, **good_done(row, rid, content_sha="b" * 64))
    run(conn, cfg)
    assert (req(conn, rid)["status"], req(conn, rid)["outcome"]) == ("failed", "invalid")
    assert doc_row(conn, jid, "cv")["content"] == row["content"]


def test_a_mail_failure_is_stored_and_the_status_stays(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    FakeSMTP.failures = [smtplib.SMTPException("down")] * 10
    write_done(cfg, rid, **good_done(row, rid))
    run(conn, cfg)
    r = req(conn, rid)
    assert r["status"] == "stored" and r["mail_error"] and not r["mailed_at"]


def test_repeated_late_and_orphan_done_files_are_renamed_and_ignored(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    write_done(cfg, rid, **good_done(row, rid))
    run(conn, cfg)
    sent = len(FakeSMTP.sent)
    before = doc_row(conn, jid, "cv")
    repeated = write_done(cfg, rid, **good_done(row, rid))                     # the same file again
    late = write_done(cfg, rid, name=f"rev-{rid}.late.json", status="error", message="late")
    orphan = write_done(cfg, 9999, status="ok")
    run(conn, cfg)
    for p in (repeated, late, orphan):
        assert not p.exists() and p.with_name(p.name + ".processed").exists()
    assert doc_row(conn, jid, "cv") == before and req(conn, rid)["status"] == "stored" and len(FakeSMTP.sent) == sent


def test_a_repeated_done_file_sends_the_mail_only_when_none_was_sent(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    FakeSMTP.failures = [smtplib.SMTPException("down")] * 10
    write_done(cfg, rid, **good_done(row, rid))
    run(conn, cfg)
    assert req(conn, rid)["mail_error"]
    write_done(cfg, rid, **good_done(row, rid))
    run(conn, cfg)
    assert req(conn, rid)["mail_error"] and not req(conn, rid)["mailed_at"]       # mail_error set: no second try (no retry loop)


def test_an_unreadable_done_file_fails_the_request(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    path = llm(cfg) / "done" / f"rev-{rid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    run(conn, cfg)
    assert (req(conn, rid)["status"], req(conn, rid)["outcome"]) == ("failed", "error")


# ---- expire ------------------------------------------------------------------------------------------------------

def test_an_overdue_request_gets_the_cancel_marker_and_a_synthetic_timeout(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    assert revisions.has_work(cfg, NOW + timedelta(minutes=70)) is False
    late = NOW + timedelta(minutes=75 + 6)
    assert revisions.has_work(cfg, late) is True
    run(conn, cfg, now=late)
    r = req(conn, rid)
    assert (r["status"], r["outcome"]) == ("failed", "timeout") and (llm(cfg) / "cancel" / f"rev-{rid}").exists()
    assert "did not answer in time" in FakeSMTP.sent[-1].get_content()
    assert (llm(cfg) / "done" / f"rev-{rid}.json.processed").exists()


def test_an_answer_that_arrives_just_before_the_timeout_is_not_replaced(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    write_done(cfg, rid, **good_done(row, rid))
    run(conn, cfg, now=NOW + timedelta(minutes=200))
    assert req(conn, rid)["status"] == "stored" and not (llm(cfg) / "cancel" / f"rev-{rid}").exists()


# ---- has_work ----------------------------------------------------------------------------------------------------

def test_has_work_looks_without_writing(conn, cfg, tmp_path):
    assert revisions.has_work(cfg, NOW) is False                                 # an empty database
    jid, row, _ = blocked(conn, cfg)
    assert revisions.has_work(cfg, NOW) is False
    rid = press(conn, row)
    assert revisions.has_work(cfg, NOW, relay=True) is True and revisions.has_work(cfg, NOW, relay=False) is False
    run(conn, cfg)
    assert revisions.has_work(cfg, NOW, relay=True) is False                     # relayed and not overdue
    write_done(cfg, rid, status="error")
    assert revisions.has_work(cfg, NOW, relay=False) is True


def test_has_work_with_no_database_or_no_table(cfg, tmp_path):
    assert revisions.has_work(cfg, NOW) is False
    import sqlite3
    sqlite3.connect(cfg["paths"]["database"]).close()
    assert revisions.has_work(cfg, NOW) is False


# ---- the CLI step ------------------------------------------------------------------------------------------------

def test_the_revisions_command_on_an_empty_database_says_there_is_nothing_to_do(config_file, capsys):
    assert cli.main(["--config", str(config_file), "revisions"]) == 0
    assert "no revision request to handle" in capsys.readouterr().out


def test_the_revisions_command_exits_2_while_the_run_lock_is_held_and_changes_nothing(config_file, capsys):
    cfg = load_config(config_file)
    with pipeline.run_lock(cfg):
        assert cli.main(["--config", str(config_file), "revisions"]) == 2
    assert "another run" in capsys.readouterr().err


# ---- config ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    lambda r: r.update(wait_minutes=54), lambda r: r.update(wait_minutes=300), lambda r: r.update(timeout_minutes=0),
    lambda r: r.update(max_turns="40"), lambda r: r.pop("quiet_minutes"), lambda r: r.update(extra=1)])
def test_a_bad_revision_block_is_refused(cfg, change):
    hl = copy.deepcopy(cfg["headless"])
    change(hl["revision"])
    with pytest.raises(ConfigError):
        _validate_headless(hl)


def test_the_default_revision_block_is_valid(cfg):
    _validate_headless(copy.deepcopy(cfg["headless"]))
    assert cfg["headless"]["revision"] == {"wait_minutes": 75, "quiet_minutes": 60, "timeout_minutes": 20, "max_turns": 40}


def test_an_address_straddling_a_size_limit_is_scrubbed_before_the_cut(conn, cfg):
    jid, row, _ = blocked(conn, cfg)
    findings = json.loads(row["findings"]) + [{"kind": "number", "text": "x" * 290 + " Rosenweg 12, 8180 Exampletown", "block": None,
                                               "where": "summary", "reason": "y" * 295 + " Rosenweg 12", "block_text": ""}]
    with conn:
        conn.execute("UPDATE documents SET findings = ? WHERE id = ?", (json.dumps(findings), row["id"]))
        conn.execute("UPDATE jobs SET description = ? WHERE id = ?", ("d" * 19990 + " Rosenweg 12, 8180 Exampletown and more", jid))
    rid = press(conn, doc_row(conn, jid, "cv"))
    run(conn, cfg)
    text = request_file(cfg, rid).read_text(encoding="utf-8")
    assert "Rosenw" not in text and "Rosen" not in text and "8180" not in text
    data = json.loads(text)
    assert len(data["job"]["description"]) == 20000 and all(len(f["text"]) <= 300 and len(f["reason"]) <= 300 for f in data["findings"])


def test_an_auth_failure_names_the_revision_in_the_alert(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    write_done(cfg, rid, status="auth", message="refused")
    run(conn, cfg)
    assert f"rev-{rid}" in conn.execute("SELECT message FROM alerts WHERE key = ?", (alerts.HEADLESS_AUTH_KEY,)).fetchone()["message"]


def test_a_processed_file_is_never_overwritten(conn, cfg):
    jid, row, rid = relayed(conn, cfg)
    old = llm(cfg) / "done" / f"rev-{rid}.json.processed"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("keep me", encoding="utf-8")
    write_done(cfg, rid, **good_done(row, rid))
    run(conn, cfg)
    assert old.read_text(encoding="utf-8") == "keep me"
