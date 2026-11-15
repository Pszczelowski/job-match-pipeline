import imaplib
import json
import os
import signal
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app import db, pipeline
from app.batch import exchange_dir
from app.config import load_config, resolve_path
from app.dedup import ingest_job
from app.mailbox import MailError
from app.models import NormalizedJob
from app.pipeline import RunLockError, finish, prepare, report_extra, run_lock, status
from app.travel import OsrmProvider, Providers, TransportProvider
from tests.test_location import Replay, TODAY
from tests.test_mailbox import FakeIMAP, four_alerts, make_mail
from tests.test_report import PASSWORD, FakeSMTP, score  # noqa: F401  (reset_smtp fixture is autouse in test_report only)

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
PROMPT = ("Score the jobs. (test prompt)", "v-test")


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


@pytest.fixture
def providers(cfg):
    replay = Replay()
    p = cfg["location"]["providers"]
    return Providers(TransportProvider(replay, p["transport_url"]), OsrmProvider(replay, p["osrm_url"]), today=TODAY)


def run_prepare(conn, cfg, providers, imap, run_id="run1", now=NOW):
    return prepare(conn, cfg, providers=providers, prompt=PROMPT, mail_client_factory=lambda c: imap, run_id=run_id, now=now)


def write_results(cfg, run_id, conn, overall=lambda n: 80 - 5 * n):
    ids = [r["job_id"] for r in conn.execute("SELECT job_id FROM batch_jobs WHERE batch_id = ? ORDER BY job_id", (run_id,))]
    entries = [score(i, overall(n)) for n, i in enumerate(ids)]
    for e in entries:
        e["subscores"] = {k: e["overall"] for k in e["subscores"]}
    path = exchange_dir(cfg) / "results" / f"{run_id}.json"
    path.write_text(json.dumps({"schema_version": "1.0", "run_id": run_id, "model": "m", "prompt_version": "v-test",
                                "results": entries, "top": []}), encoding="utf-8")
    return ids


# --- prepare --------------------------------------------------------------------------------------

def test_prepare_end_to_end_with_the_real_alerts(conn, cfg, providers):
    imap = FakeIMAP(four_alerts())
    r = run_prepare(conn, cfg, providers, imap)
    assert (r.mail.mails_ok, r.mail.found, r.mail.new_jobs) == (4, 10, 9)
    assert r.filters == {"pass": 7, "conditional": 0, "fail": 2}
    assert r.batch.job_count == 7 and r.batch.fresh_count == 7 and r.batch.path.exists()
    assert r.mail_error is None and r.warnings == []
    run = conn.execute("SELECT status, finished_at FROM runs WHERE id = 'run1'").fetchone()
    assert run["status"] == "ok" and run["finished_at"]
    adapter = conn.execute("SELECT adapter, found, new_jobs, error FROM adapter_runs").fetchone()
    assert tuple(adapter) == ("UBS alert mail", 10, 9, None)
    assert imap.log[-1] == ("logout",)


def test_location_rule_is_applied_in_prepare(conn, cfg, providers):
    run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    assert conn.execute("SELECT COUNT(*) FROM job_filter_results WHERE filter = 'location'").fetchone()[0] >= 7


def test_providers_are_required(conn, cfg):
    with pytest.raises(TypeError):
        prepare(conn, cfg, prompt=PROMPT, mail_client_factory=lambda c: FakeIMAP({}))


def test_mail_failure_is_recorded_and_the_run_goes_on(conn, cfg, providers):
    ingest_job(conn, NormalizedJob(source="s", external_id="1", title="Analyst", company="A", locations=["Zürich"]))

    def broken(cfg):
        raise MailError("Gmail refused the login; check the app password and that IMAP is enabled")
    r = prepare(conn, cfg, providers=providers, prompt=PROMPT, mail_client_factory=broken, run_id="run1", now=NOW)
    assert r.mail is None and "refused the login" in r.mail_error
    assert r.batch is not None and r.batch.job_count == 1               # jobs already in the database are still processed
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
    assert "refused the login" in conn.execute("SELECT error FROM adapter_runs").fetchone()[0]


def test_unknown_folder_is_a_mail_error(conn, cfg, providers):
    cfg["gmail"]["label_ubs"] = "nope"
    r = run_prepare(conn, cfg, providers, FakeIMAP({}))
    assert "check-mail" in r.mail_error and r.batch is None


def test_nothing_new_and_nothing_waiting_makes_no_batch(conn, cfg, providers):
    r = run_prepare(conn, cfg, providers, FakeIMAP({}))
    assert r.batch is None and r.filters == {"pass": 0, "conditional": 0, "fail": 0}
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "ok"


def test_a_second_run_carries_the_unprocessed_jobs_over(conn, cfg, providers):
    messages = four_alerts()
    run_prepare(conn, cfg, providers, FakeIMAP(messages), "run1")
    r = run_prepare(conn, cfg, providers, FakeIMAP(messages), "run2", NOW + timedelta(days=1))
    assert r.mail.mails_seen == 0 and r.batch.job_count == 7 and r.batch.fresh_count == 0


def test_repeated_empty_alert_mails_raise_a_warning(conn, cfg, providers):
    html = "These jobs matched the criteria specified for: Zurich <br><table><tr><th>Title</th><th>Job Reference #</th></tr></table>"
    messages = {str(n): make_mail(html, f"<z{n}@x>", date=f"Sat, 1{n} Nov 2026 03:00:00 +0000") for n in range(1, 4)}
    r = run_prepare(conn, cfg, providers, FakeIMAP(messages))
    assert len(r.warnings) == 1 and "contained no jobs" in r.warnings[0]
    assert "contained no jobs" in conn.execute("SELECT warning FROM adapter_runs").fetchone()[0]


# --- the run lock ---------------------------------------------------------------------------------

def test_lock_blocks_a_second_run_and_is_removed_afterwards(cfg):
    path = resolve_path(cfg["paths"]["lock"])
    with run_lock(cfg, NOW):
        assert path.exists()
        with pytest.raises(RunLockError, match="another run is in progress"):
            with run_lock(cfg, NOW + timedelta(minutes=5)):
                pass
        assert path.exists()                         # the failed second attempt did not remove the first lock
    assert not path.exists()


def test_lock_is_released_after_an_error(cfg):
    with pytest.raises(RuntimeError):
        with run_lock(cfg, NOW):
            raise RuntimeError("boom")
    assert not resolve_path(cfg["paths"]["lock"]).exists()


def test_a_stale_lock_is_replaced(cfg):
    path = resolve_path(cfg["paths"]["lock"])
    path.write_text(json.dumps({"pid": 1, "started": "2026-11-01T00:00:00Z"}), encoding="utf-8")
    with run_lock(cfg, NOW):                         # 30 hours old, limit is 6
        assert json.loads(path.read_text(encoding="utf-8"))["started"] == "2026-11-02T06:00:00Z"


@pytest.mark.parametrize("content", ["", "not json", "{}", '{"started": "garbage"}'])
def test_an_unreadable_lock_is_treated_as_left_over(cfg, content):
    path = resolve_path(cfg["paths"]["lock"])
    path.write_text(content, encoding="utf-8")
    with run_lock(cfg, NOW) as token:
        assert json.loads(path.read_text(encoding="utf-8")) == token         # the leftover was replaced by this run's lock
        assert token["started"] == "2026-11-02T06:00:00Z"
    assert not path.exists()


def test_prepare_refuses_while_another_run_holds_the_lock(conn, cfg, providers):
    with run_lock(cfg, NOW):
        with pytest.raises(RunLockError):
            run_prepare(conn, cfg, providers, FakeIMAP({}), now=NOW + timedelta(minutes=1))
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("content", ["null", "[]", '"text"', "12", '{"started": 5}'])
def test_a_lock_that_is_valid_json_but_no_token_is_left_over_not_a_crash(cfg, content):
    path = resolve_path(cfg["paths"]["lock"])
    path.write_text(content, encoding="utf-8")
    assert pipeline.lock_is_held(cfg, NOW) is True and pipeline.lock_status(cfg, NOW)["state"] == "unreadable"
    with run_lock(cfg, NOW) as token:
        assert json.loads(path.read_text(encoding="utf-8")) == token
    assert not path.exists()


def _finished_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _own_lock(cfg, pid, pidns, **extra):
    path = resolve_path(cfg["paths"]["lock"])
    path.write_text(json.dumps({"pid": pid, "started": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "pidns": pidns, **extra}), encoding="utf-8")
    return path


needs_pidns = pytest.mark.skipif(pipeline._pidns() is None, reason="no readable PID namespace (not Linux)")


@needs_pidns
def test_a_lock_of_a_finished_process_in_our_namespace_is_replaced(cfg):
    _own_lock(cfg, _finished_pid(), pipeline._pidns())
    assert pipeline.lock_status(cfg, NOW)["state"] == "left_over" and pipeline.lock_is_held(cfg, NOW) is False
    with run_lock(cfg, NOW + timedelta(minutes=5)) as token:
        assert token["pid"] == os.getpid()


@needs_pidns
def test_a_lock_of_a_live_process_in_our_namespace_is_kept(cfg):
    _own_lock(cfg, os.getpid(), pipeline._pidns())
    assert pipeline.lock_status(cfg, NOW)["state"] == "held"
    with pytest.raises(RunLockError):
        with run_lock(cfg, NOW + timedelta(minutes=5)):
            pass


def test_the_pid_rule_with_a_faked_namespace_and_a_faked_liveness(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "_pidns", lambda: "pid:[1]")
    monkeypatch.setattr(pipeline, "_pid_alive", lambda pid: pid != 4242)
    _own_lock(cfg, 4242, "pid:[1]")
    assert pipeline.lock_status(cfg, NOW)["state"] == "left_over" and "pid 4242" in pipeline.lock_status(cfg, NOW)["reason"]
    _own_lock(cfg, 4243, "pid:[1]")
    assert pipeline.lock_status(cfg, NOW)["state"] == "held"
    _own_lock(cfg, 4242, "pid:[2]")                         # another container: its pid numbers mean nothing here
    assert pipeline.lock_status(cfg, NOW)["state"] == "held"
    _own_lock(cfg, 4242, None)                              # a lock of an older version has no namespace
    assert pipeline.lock_status(cfg, NOW)["state"] == "held"


def test_a_fresh_lock_of_another_namespace_is_kept_whatever_its_pid(cfg):
    _own_lock(cfg, _finished_pid(), "pid:[4026539999]")
    assert pipeline.lock_status(cfg, NOW)["state"] == "held" and pipeline.lock_is_held(cfg, NOW + timedelta(minutes=5)) is True
    with pytest.raises(RunLockError):
        with run_lock(cfg, NOW + timedelta(minutes=5)):
            pass


def test_lock_status_free_held_and_old(cfg):
    assert pipeline.lock_status(cfg, NOW) == {"state": "free", "started": None, "pid": None, "reason": None}
    with run_lock(cfg, NOW):
        st = pipeline.lock_status(cfg, NOW + timedelta(minutes=1))
        assert st["state"] == "held" and st["started"] == "2026-11-02T06:00:00Z" and st["pid"] == os.getpid()
        old = pipeline.lock_status(cfg, NOW + timedelta(hours=7))
        assert old["state"] == "left_over" and "older than 6 hours" in old["reason"]


def test_the_lock_file_is_written_whole_and_leaves_no_temp_file(cfg):
    path = resolve_path(cfg["paths"]["lock"])
    with run_lock(cfg, NOW) as token:
        assert json.loads(path.read_text(encoding="utf-8")) == token and set(token) == {"pid", "started", "pidns"}
        assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_a_stop_signal_inside_the_lock_raises_interrupted_and_frees_the_lock(cfg):
    path = resolve_path(cfg["paths"]["lock"])
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(pipeline.Interrupted):
        with run_lock(cfg, NOW):
            handler = signal.getsignal(signal.SIGTERM)
            assert handler is not before
            handler(signal.SIGTERM, None)                    # what the signal does, without sending one
    assert not path.exists() and signal.getsignal(signal.SIGTERM) is before


def test_interrupted_is_not_swallowed_by_a_step_that_catches_exception(conn, cfg, providers):
    def factory(c):
        raise pipeline.Interrupted()
    with pytest.raises(pipeline.Interrupted):
        prepare(conn, cfg, providers=providers, prompt=PROMPT, mail_client_factory=factory, run_id="run1", now=NOW)
    assert not isinstance(pipeline.Interrupted(), Exception)


@pytest.mark.parametrize("step", ["_fetch_step", "_resolver_step"])
def test_interrupted_passes_through_steps_that_catch_exception(conn, cfg, monkeypatch, step):
    def boom(*a, **k):
        raise pipeline.Interrupted()
    monkeypatch.setattr(pipeline.descriptions_fetch, "fetch_descriptions", boom)
    monkeypatch.setattr(pipeline.resolver, "resolve_descriptions", boom)
    with pytest.raises(pipeline.Interrupted):
        getattr(pipeline, step)(conn, cfg, pipeline.PrepareResult("run1"), "run1", object(), NOW, sleep=lambda s: None)


def test_a_stop_signal_during_the_lock_release_still_frees_the_lock_and_is_raised(cfg, monkeypatch):
    path = resolve_path(cfg["paths"]["lock"])
    real_unlink = type(path).unlink

    def unlink_after_signal(self, *a, **k):
        if self == path:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)          # the signal arrives while the lock is released
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(type(path), "unlink", unlink_after_signal)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(pipeline.Interrupted):
        with run_lock(cfg, NOW):
            pass
    assert not path.exists() and signal.getsignal(signal.SIGTERM) is before


def test_lock_status_says_when_the_pid_cannot_be_checked(cfg):
    _own_lock(cfg, _finished_pid(), "pid:[4026539999]")
    st = pipeline.lock_status(cfg, NOW)
    assert st["state"] == "held" and st["pid_checkable"] is False


def test_lock_status_does_not_claim_a_checkable_pid_without_a_pid_namespace(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "_pidns", lambda: None)
    with run_lock(cfg, NOW):
        assert pipeline.lock_status(cfg, NOW)["pid_checkable"] is False


def test_a_late_stop_signal_is_raised_even_inside_an_outer_except_block(cfg, monkeypatch):
    path = resolve_path(cfg["paths"]["lock"])
    real_unlink = type(path).unlink

    def unlink_after_signal(self, *a, **k):
        if self == path:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(type(path), "unlink", unlink_after_signal)
    with pytest.raises(pipeline.Interrupted):
        try:
            raise ValueError("handled")
        except ValueError:
            with run_lock(cfg, NOW):
                pass
    assert not path.exists()


@pytest.mark.parametrize("exc, error", [(pipeline.Interrupted(), "interrupted by a stop signal"),
                                        (KeyboardInterrupt(), "interrupted (Ctrl+C)"), (ZeroDivisionError("x"), "ZeroDivisionError")])
def test_an_interrupted_or_crashed_prepare_marks_its_run_failed_and_frees_the_lock(conn, cfg, providers, exc, error):
    def factory(c):
        raise exc
    with pytest.raises(type(exc)):
        prepare(conn, cfg, providers=providers, prompt=PROMPT, mail_client_factory=factory, run_id="run1", now=NOW)
    row = conn.execute("SELECT status, finished_at, summary FROM runs WHERE id = 'run1'").fetchone()
    assert row["status"] == "failed" and row["finished_at"] and json.loads(row["summary"]) == {"error": error}
    assert not resolve_path(cfg["paths"]["lock"]).exists()


# --- finish ---------------------------------------------------------------------------------------

def test_finish_imports_the_results_and_sends_the_report(conn, cfg, providers):
    run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    write_results(cfg, "run1", conn)
    r = finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW)
    assert [i.status for i in r.imports] == ["imported"] and r.reports == {"run1": "sent"}
    body = FakeSMTP.sent[0].get_body(("plain",)).get_content()
    assert "Jobs seen: 10; new: 9" in body and "UBS alert mail: 10 found, 9 new" in body
    assert "Rejected by the filters this run: 2 (seniority_corporate_title 2)" in body
    assert pipeline.finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW).imports == []       # nothing left to do


def test_a_failed_report_is_retried_by_the_next_finish(conn, cfg, providers):
    run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    write_results(cfg, "run1", conn)
    FakeSMTP.failures = [smtplib.SMTPServerDisconnected("gone")] * 3
    first = finish(conn, cfg, smtp_factory=FakeSMTP, sleep=lambda s: None, now=NOW)
    assert first.reports["run1"].startswith("not sent") and FakeSMTP.sent == []
    second = finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW)
    assert second.imports == [] and second.reports == {"run1": "sent"} and len(FakeSMTP.sent) == 1


def test_finish_with_a_bad_results_file_sends_nothing(conn, cfg, providers):
    run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    (exchange_dir(cfg) / "results" / "bad.json").write_text("{", encoding="utf-8")
    r = finish(conn, cfg, smtp_factory=FakeSMTP, now=NOW)
    assert [i.status for i in r.imports] == ["rejected"] and r.reports == {} and FakeSMTP.sent == []


def test_report_extra(conn, cfg, providers):
    run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    extra = report_extra(conn, "run1")
    assert (extra["seen"], extra["new"]) == (10, 9)
    assert extra["adapters"] == [{"name": "UBS alert mail", "found": 10, "new": 9, "error": None, "warning": None}]
    assert report_extra(conn, "unknown") == {"adapters": []}


# --- status ---------------------------------------------------------------------------------------

def test_status_snapshot(conn, cfg, providers):
    s = status(conn, cfg, NOW)
    assert s["schema_version"] == len(db.MIGRATIONS)
    assert s["jobs_by_status"] == {} and s["last_run"] is None and s["oldest_awaiting_days"] is None
    run_prepare(conn, cfg, providers, FakeIMAP(four_alerts()))
    s = status(conn, cfg, NOW + timedelta(days=2))
    assert s["jobs_by_status"] == {"awaiting_llm": 7} and s["archived"] == 2
    assert s["oldest_awaiting_days"] == pytest.approx(2.0) and s["last_run"]["id"] == "run1"
    assert s["results_files_waiting"] == 0 and s["mail_warning"] is None


def test_expiration_warning_reaches_the_run_the_report_and_status(conn, cfg, providers):
    from tests.test_mailbox import notice_mail
    messages = {"1": notice_mail(date="Thu, 29 Oct 2026 03:00:00 +0000"), **{str(i + 2): m for i, m in enumerate(four_alerts().values())}}
    r = run_prepare(conn, cfg, providers, FakeIMAP(messages))
    assert r.mail.notices and r.mail.mails_ok == 4
    assert any("renew it in the UBS Talent Gateway" in w for w in r.warnings)
    assert "renew it" in conn.execute("SELECT warning FROM adapter_runs").fetchone()[0]
    assert "renew it" in status(conn, cfg, NOW)["search_agent_warning"]
    assert report_extra(conn, "run1")["adapters"][0]["warning"]          # shown in the report's run summary


def test_an_imap_abort_mid_session_is_recorded_and_the_run_goes_on(conn, cfg, providers):
    ingest_job(conn, NormalizedJob(source="s", external_id="1", title="Analyst", company="A", locations=["Zürich"]))
    imap = FakeIMAP({})

    def uid(command, *args):
        raise imaplib.IMAP4.abort("socket error: EOF")
    imap.uid = uid
    r = run_prepare(conn, cfg, providers, imap)
    assert r.mail is None and "the mailbox connection failed" in r.mail_error
    assert r.batch is not None and r.batch.job_count == 1
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"


def test_marking_a_run_failed_never_overwrites_a_finished_run(conn):
    db.start_run(conn, "run1")
    with conn:
        conn.execute("UPDATE runs SET status = 'ok', finished_at = '2026-11-02T06:10:00Z' WHERE id = 'run1'")
    pipeline._mark_run_failed(conn, "run1", pipeline.Interrupted())
    r = conn.execute("SELECT status, finished_at FROM runs WHERE id = 'run1'").fetchone()
    assert (r["status"], r["finished_at"]) == ("ok", "2026-11-02T06:10:00Z")



def test_a_stale_lock_replaced_by_another_run_meanwhile_is_put_back_and_refused(cfg, monkeypatch):
    from app import pipeline
    path = _own_lock(cfg, 1, None)
    path.write_text(json.dumps({"pid": 1, "started": (NOW - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "pidns": None}), encoding="utf-8")
    fresh = {"pid": 7, "started": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "pidns": None}
    real = pipeline._read_lock
    reads = []

    def read(p):
        reads.append(p)
        if len(reads) == 2:                                  # the re-read of the renamed file: another run's lock now
            return fresh
        return real(p)
    monkeypatch.setattr(pipeline, "_read_lock", read)
    with pytest.raises(RunLockError):
        with run_lock(cfg, NOW):
            pass
    assert path.exists() and not list(path.parent.glob("*.stale"))


def test_a_startup_cleanup_treats_a_lock_of_another_pid_namespace_as_left_over(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "_pidns", lambda: "pid:[2]")
    _own_lock(cfg, 4242, "pid:[1]", scheduler=True)
    assert pipeline.lock_status(cfg, NOW)["state"] == "held"
    assert pipeline.lock_status(cfg, NOW, startup=True)["state"] == "left_over"
    with run_lock(cfg, NOW + timedelta(minutes=5), startup=True) as token:
        assert token["pidns"] == "pid:[2]"


def test_review_lines_go_only_to_the_report_of_their_own_batch():
    done = {"run_id": "b1", "status": "ok", "review_summary": "one", "resumed": [
        {"run_id": "b2", "status": "ok", "review_summary": "two"}, {"run_id": "old", "status": "ok", "review_summary": "orphan"}]}
    batches = ["b1", "b2"]
    first, second = (" ".join(pipeline.review_lines(done, b, batches)) for b in batches)
    assert "one" in first and "orphan" in first and "two" not in first
    assert "two" in second and "one" not in second and "orphan" not in second
    assert " ".join(pipeline.review_lines(done)).count("two") == 1                  # without batches: all parts as before


def test_a_startup_cleanup_keeps_a_lock_of_another_pid_namespace_that_is_no_scheduler(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "_pidns", lambda: "pid:[2]")
    _own_lock(cfg, 4242, "pid:[1]", scheduler=False)         # e.g. a live `docker compose run` restore-backup container
    assert pipeline.lock_status(cfg, NOW, startup=True)["state"] == "held"
    _own_lock(cfg, 4242, "pid:[1]")
    assert pipeline.lock_status(cfg, NOW, startup=True)["state"] == "held"


def test_the_put_back_of_a_displaced_lock_never_overwrites_a_new_lock(cfg, monkeypatch):
    path = _own_lock(cfg, 1, None)
    other = {"pid": 7, "started": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "pidns": None}
    grave = path.with_name("run.lock.x.stale")
    grave.write_text(json.dumps({"pid": 9, "started": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "pidns": None}), encoding="utf-8")
    path.write_text(json.dumps(other), encoding="utf-8")
    pipeline._put_back(grave, path)
    assert json.loads(path.read_text(encoding="utf-8")) == other and not grave.exists()


def test_an_interrupt_after_the_rename_puts_the_stale_lock_back(cfg, monkeypatch):
    path = _own_lock(cfg, 1, None)
    seen = pipeline._read_lock(path)

    def boom(p):
        raise pipeline.Interrupted()
    monkeypatch.setattr(pipeline, "_read_lock", boom)
    with pytest.raises(pipeline.Interrupted):
        pipeline._remove_stale(path, seen)
    assert path.exists() and not list(path.parent.glob("*.stale"))
