import json
import logging
from pathlib import Path

import pytest

from app import cli, db, mailbox
from app.batch import exchange_dir
from app.config import load_config
from app.travel import OsrmProvider, Providers, TransportProvider
from tests.test_location import Replay, TODAY
from tests.test_mailbox import FakeIMAP, four_alerts
from tests.test_report import PASSWORD, FakeSMTP, score


@pytest.fixture(autouse=True)
def clean_logger(monkeypatch):
    monkeypatch.setattr(cli, "load_env", lambda *a, **k: {})        # tests must never read the developer's real .env
    log = logging.getLogger("recruitment")
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    FakeSMTP.sent, FakeSMTP.events, FakeSMTP.failures = [], [], []
    yield
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", PASSWORD)
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        f"paths:\n  database: {(tmp_path / 'jobs.db').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
        f"  logs: {(tmp_path / 'logs').as_posix()}\n  lock: {(tmp_path / 'run.lock').as_posix()}\n"
        "email:\n  retry_wait_seconds: 0\n", encoding="utf-8")
    return path


def providers_factory(cfg):
    replay = Replay()
    p = cfg["location"]["providers"]
    return Providers(TransportProvider(replay, p["transport_url"]), OsrmProvider(replay, p["osrm_url"]), today=TODAY)


def run(config_file, *args, imap=None, smtp=None):
    argv = ["--config", str(config_file), *args]
    return cli.main(argv, mail_client_factory=lambda cfg: imap, providers_factory=providers_factory, smtp_factory=smtp)


def test_status_needs_no_network(config_file, capsys):
    assert run(config_file, "status") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["jobs_by_status"] == {} and out["last_run"] is None


def test_bad_config_is_exit_3(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("scoring:\n  top_n: 0\n", encoding="utf-8")
    assert cli.main(["--config", str(bad), "status"]) == 3
    assert "configuration error" in capsys.readouterr().err


def test_prepare_refuses_a_draft_prompt(config_file, capsys, tmp_path):
    draft = tmp_path / "draft.md"
    draft.write_text("# Scoring prompt\n\nPrompt version: 2.0.0-draft\n", encoding="utf-8")
    config_file.write_text(config_file.read_text(encoding="utf-8") + f"scoring:\n  prompt_file: {draft.as_posix()}\n", encoding="utf-8")
    assert run(config_file, "prepare", imap=FakeIMAP({})) == 3
    assert "draft" in capsys.readouterr().err


def test_prepare_with_the_draft_allowed(config_file, capsys):
    imap = FakeIMAP(four_alerts())
    assert run(config_file, "prepare", "--allow-draft-prompt", imap=imap) == 0
    out = capsys.readouterr().out
    assert "mails 4 ok / 0 failed" in out and "jobs found 10, new 9" in out and "(7 jobs)" in out
    assert len(list((exchange_dir(load_config(config_file)) / "batches").glob("batch_*.json"))) == 1


def test_prepare_exits_1_when_the_mail_step_fails(config_file, capsys):
    def broken(cfg):
        raise mailbox.MailError("Gmail refused the login")
    code = cli.main(["--config", str(config_file), "prepare", "--allow-draft-prompt"], mail_client_factory=broken,
                    providers_factory=providers_factory)
    captured = capsys.readouterr()
    assert code == 1 and "Gmail refused the login" in captured.err


def test_prepare_exits_2_when_another_run_is_active(config_file, capsys):
    from app.pipeline import run_lock
    cfg = load_config(config_file)
    with run_lock(cfg):
        assert run(config_file, "prepare", "--allow-draft-prompt", imap=FakeIMAP({})) == 2
    assert "another run is in progress" in capsys.readouterr().err


def test_an_interrupted_prepare_exits_143_raises_the_alert_and_sends_no_mail(config_file, capsys):
    from app import alerts, pipeline

    def stopped(cfg):
        raise pipeline.Interrupted()
    FakeSMTP.sent, FakeSMTP.events, FakeSMTP.failures = [], [], []
    code = cli.main(["--config", str(config_file), "prepare", "--allow-draft-prompt"], mail_client_factory=stopped,
                    providers_factory=providers_factory, smtp_factory=FakeSMTP)
    assert code == 143 and FakeSMTP.sent == [] and FakeSMTP.events == []
    conn = db.connect(load_config(config_file)["paths"]["database"])
    assert [a["key"] for a in alerts.open_alerts(conn)] == ["run_interrupted:prepare"]
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
    conn.close()
    assert not load_config(config_file)["paths"]["lock"] or not Path(load_config(config_file)["paths"]["lock"]).exists()
    assert run(config_file, "prepare", "--allow-draft-prompt", imap=FakeIMAP(four_alerts()), smtp=FakeSMTP) == 0        # a complete run closes it
    conn = db.connect(load_config(config_file)["paths"]["database"])
    assert alerts.open_alerts(conn) == []
    conn.close()


def test_finish_imports_and_reports(config_file, capsys):
    run(config_file, "prepare", "--allow-draft-prompt", imap=FakeIMAP(four_alerts()))
    cfg = load_config(config_file)
    conn = db.connect(cfg["paths"]["database"])
    run_id = conn.execute("SELECT id FROM batches").fetchone()[0]
    ids = [r[0] for r in conn.execute("SELECT job_id FROM batch_jobs ORDER BY job_id")]
    entries = []
    for n, i in enumerate(ids):
        e = score(i, 80 - 5 * n)
        e["subscores"] = {k: e["overall"] for k in e["subscores"]}
        entries.append(e)
    (exchange_dir(cfg) / "results" / "r.json").write_text(json.dumps(
        {"schema_version": "1.0", "run_id": run_id, "model": "m", "prompt_version": "v", "results": entries, "top": []}), encoding="utf-8")
    conn.close()
    capsys.readouterr()
    assert run(config_file, "finish", smtp=FakeSMTP) == 0
    out = capsys.readouterr().out
    assert f"results for {run_id}: imported, scored 7" in out and f"report {run_id}: sent" in out
    assert len(FakeSMTP.sent) == 1


def test_finish_exits_1_when_the_report_cannot_be_sent(config_file, capsys, monkeypatch):
    run(config_file, "prepare", "--allow-draft-prompt", imap=FakeIMAP(four_alerts()))
    cfg = load_config(config_file)
    conn = db.connect(cfg["paths"]["database"])
    run_id = conn.execute("SELECT id FROM batches").fetchone()[0]
    ids = [r[0] for r in conn.execute("SELECT job_id FROM batch_jobs")]
    conn.close()
    entries = [score(i, 60) for i in ids]
    for e in entries:
        e["subscores"] = {k: 60 for k in e["subscores"]}
    (exchange_dir(cfg) / "results" / "r.json").write_text(json.dumps(
        {"schema_version": "1.0", "run_id": run_id, "model": "m", "prompt_version": "v", "results": entries, "top": []}), encoding="utf-8")
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
    assert run(config_file, "finish", smtp=FakeSMTP) == 1
    assert "not sent" in capsys.readouterr().out


def test_check_mail_lists_folders_and_finds_the_label(config_file, capsys):
    assert run(config_file, "check-mail", imap=FakeIMAP({})) == 0
    out = capsys.readouterr().out
    assert "INBOX" in out and "[Gmail]/Sent Mail" in out and "found" in out and "NOT FOUND" not in out


def test_check_mail_flags_a_missing_label(config_file, capsys, tmp_path):
    config_file.write_text(config_file.read_text(encoding="utf-8") + "gmail:\n  label_ubs: other-label\n", encoding="utf-8")
    assert run(config_file, "check-mail", imap=FakeIMAP({})) == 1
    assert "NOT FOUND" in capsys.readouterr().out


def test_check_mail_reports_login_problems(config_file, capsys):
    def broken(cfg):
        raise mailbox.MailError("GMAIL_APP_PASSWORD is not set")
    assert cli.main(["--config", str(config_file), "check-mail"], mail_client_factory=broken) == 1
    assert "GMAIL_APP_PASSWORD" in capsys.readouterr().err


def test_the_log_file_holds_no_secret_and_no_job_text(config_file):
    run(config_file, "prepare", "--allow-draft-prompt", imap=FakeIMAP(four_alerts()))
    log_file = next(Path(load_config(config_file)["paths"]["logs"]).glob("*.log"))
    log_text = log_file.read_text(encoding="utf-8")
    assert "prepare started" in log_text and "prepare done" in log_text
    assert PASSWORD not in log_text and "Security Intelligence Analyst" not in log_text


def test_withdraw_batch_command(config_file, capsys):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    cfg = load_config(config_file)
    conn = db.connect(cfg["paths"]["database"])
    run_id = conn.execute("SELECT id FROM batches").fetchone()[0]
    conn.close()
    capsys.readouterr()
    assert run(config_file, "withdraw-batch", run_id) == 0
    assert "7 job(s) are back to new" in capsys.readouterr().out
    assert run(config_file, "withdraw-batch", run_id) == 1                  # already gone
    assert "no batch" in capsys.readouterr().err


# --- descriptions ---------------------------------------------------------------------------------------

POSTING = ("Key responsibilities\nYou analyse reporting requirements for the trading desks and document them for the "
           "developers. You test the reports and improve the daily data checks.\n\nYour skills and experience\n"
           "SQL, reporting tools, good communication in English, several years in a bank.")
POSTING += "\n\nAbout the team\n" + "We work in small teams and release every two weeks. " * 30      # complete: 1,500 characters or more (v0.72.0)


def first_job_id(config_file):
    cfg = load_config(config_file)
    conn = db.connect(cfg["paths"]["database"])
    job_id = conn.execute("SELECT id FROM jobs ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()
    return job_id


def test_missing_lists_jobs_with_links(config_file, capsys):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    capsys.readouterr()
    assert run(config_file, "missing", "--limit", "3") == 0
    out = capsys.readouterr().out
    assert out.count("https://jobs.ubs.com") == 3 and "score   -" in out


def test_set_description_from_a_file_then_it_is_scored_again(config_file, capsys, tmp_path):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    job_id = first_job_id(config_file)
    text = tmp_path / "posting.txt"
    text.write_text(POSTING, encoding="utf-8")
    capsys.readouterr()
    assert run(config_file, "set-description", str(job_id), "--file", str(text)) == 0
    assert "description stored" in capsys.readouterr().out
    assert run(config_file, "set-description", str(job_id), "--file", str(text)) == 0
    assert "already has exactly this description" in capsys.readouterr().out


def test_set_description_errors_exit_1(config_file, capsys, tmp_path):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    short = tmp_path / "short.txt"
    short.write_text("too short", encoding="utf-8")
    capsys.readouterr()
    assert run(config_file, "set-description", str(first_job_id(config_file)), "--file", str(short)) == 1
    assert "only 9 characters" in capsys.readouterr().err
    assert run(config_file, "set-description", "9999", "--file", str(short)) == 1
    assert run(config_file, "set-description", "1", "--file", str(tmp_path / "missing.txt")) == 1


def test_set_description_from_the_clipboard(config_file, capsys, monkeypatch):
    from app import descriptions
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    monkeypatch.setattr(descriptions, "read_clipboard", lambda: POSTING)
    capsys.readouterr()
    assert run(config_file, "set-description", str(first_job_id(config_file)), "--clipboard") == 0
    assert "description stored" in capsys.readouterr().out


def test_set_description_refuses_incomplete_clipboard_text(config_file, capsys, monkeypatch):
    from app import descriptions
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    monkeypatch.setattr(descriptions, "read_clipboard", lambda: POSTING[:400])
    capsys.readouterr()
    assert run(config_file, "set-description", str(first_job_id(config_file)), "--clipboard") == 1
    assert "the text is not complete" in capsys.readouterr().err


def test_import_descriptions_command(config_file, capsys):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    job_id = first_job_id(config_file)
    folder = exchange_dir(load_config(config_file)) / "descriptions"
    (folder / f"{job_id}.txt").write_text(POSTING, encoding="utf-8")
    capsys.readouterr()
    assert run(config_file, "import-descriptions") == 0
    out = capsys.readouterr().out
    assert f"{job_id}.txt: description stored" in out and "1 file(s) handled" in out


# --- fetching descriptions (only with the user's switch and permission note) ------------------------------

def enable_fetching(config_file):
    config_file.write_text(config_file.read_text(encoding="utf-8")
                           + "descriptions:\n  fetch_enabled: true\n  permission_note: 'test permission note'\n  delay_seconds: 1\n", encoding="utf-8")


class _Pages:
    def __init__(self):
        self.calls = []

    def __call__(self, url):
        from tests.test_fetch_descriptions import jid, make_page
        self.calls.append(url)
        return make_page(jid(url))


def test_fetch_descriptions_is_refused_while_switched_off(config_file, capsys):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    capsys.readouterr()
    assert cli.main(["--config", str(config_file), "fetch-descriptions"], page_fetcher=_Pages()) == 1
    assert "switched off" in capsys.readouterr().err


def test_fetch_descriptions_when_switched_on(config_file, capsys, monkeypatch):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    enable_fetching(config_file)
    monkeypatch.setattr("time.sleep", lambda s: None)
    pages = _Pages()
    capsys.readouterr()
    assert cli.main(["--config", str(config_file), "fetch-descriptions", "--limit", "3"], page_fetcher=pages) == 0
    out = capsys.readouterr().out
    assert len(pages.calls) == 3 and "3 fetched, 0 failed" in out
    assert cli.main(["--config", str(config_file), "fetch-descriptions"], page_fetcher=pages) == 0       # the rest, none twice
    assert len(set(pages.calls)) == len(pages.calls) == 7


def test_fetch_descriptions_stopped_by_a_refusal_exits_1(config_file, capsys, monkeypatch):
    from app.descriptions_fetch import FetchError
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    enable_fetching(config_file)

    def refuse(url):
        raise FetchError("HTTP 429", 429)
    capsys.readouterr()
    assert cli.main(["--config", str(config_file), "fetch-descriptions"], page_fetcher=refuse) == 1
    assert "STOPPED" in capsys.readouterr().err


# --- archive and restore commands -----------------------------------------------------------------------------

def test_archive_command_lists_rejected_jobs(config_file, capsys):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    capsys.readouterr()
    assert run(config_file, "archive") == 0
    out = capsys.readouterr().out
    assert out.count("seniority_corporate_title") == 2 and "2 entries shown" in out
    assert run(config_file, "archive", "--reason", "nonexistent") == 0
    assert "0 entries shown" in capsys.readouterr().out


def test_restore_job_command_names_the_restored_status(config_file, capsys):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    cfg = load_config(config_file)
    conn = db.connect(cfg["paths"]["database"])
    jid = conn.execute("SELECT id FROM jobs ORDER BY id LIMIT 1").fetchone()[0]
    conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (jid,)); conn.commit()
    aid = db.archive_job(conn, jid, "user", "user_rejected")
    conn.close()
    capsys.readouterr()
    assert run(config_file, "restore-job", str(aid)) == 0
    out = capsys.readouterr().out
    assert "(status applied)" in out and "filters run" not in out


def test_restore_job_command(config_file, capsys):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    cfg = load_config(config_file)
    conn = db.connect(cfg["paths"]["database"])
    archive_id = conn.execute("SELECT id FROM job_archive ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()
    capsys.readouterr()
    assert run(config_file, "restore-job", str(archive_id)) == 0
    assert "restored as job" in capsys.readouterr().out
    assert run(config_file, "restore-job", str(archive_id)) == 1
    assert "already restored" in capsys.readouterr().err
    assert run(config_file, "restore-job", "9999") == 1
    capsys.readouterr()
    run(config_file, "archive")
    assert "[restored]" in capsys.readouterr().out


def _set_status(config_file, job_id, status):
    conn = db.connect(load_config(config_file)["paths"]["database"])
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
    conn.close()


def _job_row(config_file, job_id):
    conn = db.connect(load_config(config_file)["paths"]["database"])
    try:
        return conn.execute("SELECT status, needs_rescore, title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()


def test_rescore_command_marks_prints_and_dry_run_writes_nothing(config_file, capsys):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    job_id = first_job_id(config_file)
    _set_status(config_file, job_id, "scored")
    capsys.readouterr()
    assert run(config_file, "rescore", "--job", str(job_id), "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"job {job_id} | " in out and "needs_rescore 0 -> 1" in out and "would mark 1 job(s)" in out and "nothing was written" in out
    assert _job_row(config_file, job_id)["needs_rescore"] == 0
    assert run(config_file, "rescore", "--job", str(job_id)) == 0
    out = capsys.readouterr().out
    row = _job_row(config_file, job_id)
    assert row["needs_rescore"] == 1 and f"{row['title']} | {row['company']} | needs_rescore 0 -> 1" in out and "marked 1 job(s)" in out


def test_rescore_command_refuses_unknown_and_skips_worked_on_jobs(config_file, capsys):
    run(config_file, "prepare", imap=FakeIMAP(four_alerts()))
    job_id = first_job_id(config_file)
    _set_status(config_file, job_id, "applied")
    capsys.readouterr()
    assert run(config_file, "rescore", "--job", "9999") == 1
    assert "not an open job" in capsys.readouterr().err
    assert run(config_file, "rescore", "--job", str(job_id)) == 0
    out = capsys.readouterr().out
    assert "skipped: you already work on it (status applied)" in out and "marked 0 job(s)" in out
    assert _job_row(config_file, job_id)["needs_rescore"] == 0


def test_recheck_poland_command(config_file, capsys):
    from app.dedup import ingest_job
    from app.filters.runner import filter_new_jobs
    from app.models import NormalizedJob
    cfg = load_config(config_file)
    off = load_config(config_file)
    off["location"]["poland"]["enabled"] = False                       # as if the job was filtered before the Poland rule
    conn = db.connect(Path(cfg["paths"]["database"]))
    ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id="1", title="Analyst", company="A", locations=["Poland"],
                                   raw={"work_mode": "Remote"}))
    filter_new_jobs(conn, off, providers=providers_factory(off))
    conn.close()
    assert run(config_file, "recheck-poland", "--dry-run") == 0
    assert capsys.readouterr().out.strip() == ("(dry run, nothing written) 1 job(s) in Poland checked, 1 changed, 0 archived (on-site), "
                                                "0 marked to be scored again, 0 skipped (you already work on them)")
    assert run(config_file, "recheck-poland") == 0
    assert capsys.readouterr().out.strip().startswith("1 job(s) in Poland checked, 1 changed, 0 archived")
    assert run(config_file, "recheck-poland") == 0
    assert capsys.readouterr().out.strip().startswith("1 job(s) in Poland checked, 0 changed")


def test_recheck_travel_command(config_file, capsys):
    from tests.test_recheck_travel import seed_old
    cfg = load_config(config_file)
    conn = db.connect(Path(cfg["paths"]["database"]))
    flip = seed_old(conn, "Flip", "scored", "Town", 72.8)
    seed_old(conn, "Mine", "applied", "Town", 72.8)
    conn.close()
    assert run(config_file, "recheck-travel", "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"job {flip} | Flip | Acme | train 40 -> 91 min | pass -> fail" in out
    assert "skipped: you already work on it (status applied)" in out
    assert out.strip().endswith("(dry run, nothing written)")
    assert _job_row(config_file, flip)["status"] == "scored"
    assert run(config_file, "recheck-travel") == 0
    out = capsys.readouterr().out
    assert "1 changed, 1 archived" in out and "2 job(s) checked" in out
    assert run(config_file, "recheck-travel") == 0
    assert "0 changed" in capsys.readouterr().out


def test_recheck_archive_location_command(config_file, capsys):
    assert run(config_file, "recheck-archive-location", "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.strip() == "0 row(s) checked, 0 to restore, 0 still fail, 0 skipped (not in the cache), 0 skipped (job already open) (dry run, nothing written)"
    assert run(config_file, "recheck-archive-location") == 0
    assert "0 restored" in capsys.readouterr().out


# --- reject command (0.82.0) ------------------------------------------------------------------------------------

def _seed_job(config_file, status="scored", key="rj1", approved_doc=False):
    conn = db.connect(load_config(config_file)["paths"]["database"])
    jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) "
                       "VALUES (?, 'Data Engineer', 'ACME', ?, 't', 't')", (key, status)).lastrowid
    if approved_doc:
        conn.execute("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at, approved_at, approved_pdf_path) "
                     "VALUES (?, 'cv', '{}', 'approved', 'd', 'c', '2026-11-01T10:00:00Z', ?)", (jid, f"{jid}_x/cv.pdf"))
    conn.commit()
    conn.close()
    return jid


def _dump(config_file):
    conn = db.connect(load_config(config_file)["paths"]["database"])
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_reject_without_yes_shows_the_job_and_changes_nothing(config_file, capsys):
    jid = _seed_job(config_file)
    before = _dump(config_file)
    capsys.readouterr()
    assert run(config_file, "reject", str(jid), "--comment", "x") == 0
    out = capsys.readouterr().out
    assert f"job {jid} | scored | Data Engineer (ACME)" in out and "Nothing changed; run again with --yes" in out and "WARNING" not in out
    assert _dump(config_file) == before


def test_reject_with_yes_archives_and_restore_job_brings_it_back(config_file, capsys):
    jid = _seed_job(config_file)
    capsys.readouterr()
    assert run(config_file, "reject", str(jid), "--comment", "  too\nfar ", "--yes") == 0
    out = capsys.readouterr().out
    assert "was rejected and moved to the archive" in out and "reason user_rejected" in out
    conn = db.connect(load_config(config_file)["paths"]["database"])
    a = conn.execute("SELECT id, rejection_stage, reason_code, comment FROM job_archive WHERE original_job_id = ?", (jid,)).fetchone()
    assert (a["rejection_stage"], a["reason_code"], a["comment"]) == ("user", "user_rejected", "too far")
    assert conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is None
    conn.close()
    assert f"restore-job {a['id']}" in out
    assert run(config_file, "restore-job", str(a["id"])) == 0
    assert "restored as job" in capsys.readouterr().out


def test_reject_refusals_exit_1_and_write_nothing(config_file, capsys):
    done = _seed_job(config_file, "interview", key="rj2")
    open_job = _seed_job(config_file, key="rj3")
    assert run(config_file, "reject", str(open_job), "--yes") == 0
    live = _seed_job(config_file, key="rj4")
    before = _dump(config_file)
    capsys.readouterr()
    assert run(config_file, "reject", "9999", "--yes") == 1
    assert "no open job 9999" in capsys.readouterr().err
    assert run(config_file, "reject", str(open_job), "--yes") == 1
    assert f"job {open_job} is already archived as entry" in capsys.readouterr().err
    for flags in ((), ("--yes",)):
        assert run(config_file, "reject", str(done), *flags) == 1
        assert "'interview'" in capsys.readouterr().err
        assert run(config_file, "reject", str(live), "--comment", "x" * 301, *flags) == 1
        assert "longer than 300" in capsys.readouterr().err
    assert _dump(config_file) == before


def test_reject_preview_warns_about_applied_and_approved_documents(config_file, capsys):
    applied = _seed_job(config_file, "applied", key="rj5")
    approved = _seed_job(config_file, "approved", key="rj6", approved_doc=True)
    capsys.readouterr()
    assert run(config_file, "reject", str(applied)) == 0
    assert "marked this job as applied" in capsys.readouterr().out
    assert run(config_file, "reject", str(approved)) == 0
    assert "approved documents (cv)" in capsys.readouterr().out
    assert run(config_file, "reject", str(approved), "--yes") == 0
    capsys.readouterr()
    conn = db.connect(load_config(config_file)["paths"]["database"])
    assert "approved documents kept on disk: cv" in conn.execute("SELECT comment FROM job_archive").fetchone()[0]
    conn.close()


def test_reject_on_a_busy_database_exits_1_with_the_busy_wording(config_file, capsys, monkeypatch):
    import sqlite3
    jid = _seed_job(config_file)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "archive_job", locked)
    before = _dump(config_file)
    capsys.readouterr()
    assert run(config_file, "reject", str(jid), "--yes") == 1
    assert "may be busy with a pipeline run" in capsys.readouterr().err
    assert _dump(config_file) == before
