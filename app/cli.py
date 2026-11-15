"""Command line: `python -m app <command>`.

  prepare      read the alert mails, filter, write the batch for the Claude session
  finish       import the results files and send the report (--headless-done FILE: the llm service's done file)
  status       counts, warnings and the open alerts, no network
  check-mail   log in to Gmail and list the folder names (to check the label in the config)
  withdraw-batch RUN_ID   take back a batch that was not scored yet (jobs go into the next batch)
  recheck-language [--dry-run]   (--cap-scores [--apply]: lower stored German scores to the cap) run the language rule again on every stored description (after the rule or a description changed); --dry-run writes nothing
  recheck-poland [--dry-run]   run the Poland rule (remote / hybrid passes, on-site fails) again on the jobs located only in
                           Poland, without a network; changed jobs get a new location result, on-site ones are archived,
                           scored ones are marked to be scored again; --dry-run writes nothing
  recheck-travel [--dry-run]   run the location rule again, offline, from the cached places and travel times (the train time
                           estimated from the car time where the timetable gave none, goal.md 5.3.1); lists the jobs whose
                           result changes, archives new fails, marks scored ones to be scored again, skips jobs you work on;
                           --dry-run writes nothing
  recheck-archive-location [--dry-run] [--online]   run the location rule again, offline (caches only; --online looks up what is missing), on the archived rejections
                           location_too_far, poland_on_site and location_unresolved; restores the ones that now pass or are
                           conditional (status new), skips rows whose job is already open or whose place is not cached;
                           --dry-run lists them and writes nothing
  archive      list archived jobs (--reason CODE to narrow it down); restore-job ARCHIVE_ID puts one back
  reject JOB_ID [--comment TEXT] [--yes]   reject one open job like the web button (archive, reason user_rejected); without
                           --yes it only shows the job and changes nothing; no lock, no backup
  missing      list the jobs without a description, best first (open the link, copy the posting)
  set-description JOB_ID   store a pasted description (--clipboard or --file) and score the job again
  import-descriptions      store every exchange/descriptions/<job id>.txt
  merge-duplicates [--dry-run]   join open jobs whose company spelling differs only by a country or legal word (AXA Switzerland =
                           AXA) into one and rewrite the stored keys; own checked backup first (jobs_pre-merge_*.db, never
                           rotated); skips groups with two jobs you work on or one that waits for scores; --dry-run writes
                           nothing and makes no backup
  reselect [--apply]       find batches whose stored top list holds a job that now waits for a description (tailoring.require_description)
                           and show the selection before and after; --apply re-picks them (as `finish` does), a preview writes nothing
  release-closed [--apply]   jobs that left the employer's list but still wait for a score (awaiting_llm): list them and the state
                           they go back to (new, or scored with a new score due); --apply also resolves their open postponements
  rescore --job ID [--job ID ...] [--dry-run]   mark open jobs to be scored again: the next `prepare` puts them in the
                           batch as re-evaluation jobs; a job you already work on is skipped; --dry-run writes nothing
  fetch-descriptions       fetch the descriptions of your UBS jobs (only if switched on in the config; needs UBS permission)
  resolve-descriptions [--job ID] [--limit N] [--dry-run] [--discovery]   look for the complete description of jobs that have none on the
                           employer's career site (resolver.sources in the config; goal.md 5.1a; needs resolver.enabled);
                           --dry-run asks the site but writes nothing (with --discovery: nothing but the board-name guesses, which
                           the real run of the day then counts against new_companies_per_day); --discovery runs only the
                           same-job lookup on public job boards (resolver.discovery; works while resolver.discovery.enabled is still false)
  check-adapter NAME       read one employer job list without storing anything (adapters.NAME.enabled must be true)
  calibration-sheet        write a CSV of jobs for you to label blind (no scores in it); --count N, --out PATH
  calibration-report PATH  compare your filled CSV with the model's scores (--json for machine output); read-only
  check-document PATH      traceability check of a generated document (JSON or text) against profile/master_cv.yaml;
                           --company NAME (repeatable) is the employer applied to; exit 1 when a fact is not in the file;
                           touches neither the database nor the config
  documents [--job ID] [--json]   list the stored tailored documents (cover letter, CV) with status and findings;
                           --job prints the findings and the text of that job's documents; read-only
  recheck-documents [--apply] [--job ID]   run the checks again on the stored blocked documents; a preview without --apply
  render-documents [--job ID] [--out DIR]   build the PDFs (cover letter, CV) of the stored draft documents into
                           paths.documents (one folder per job); --out DIR only writes there and changes nothing else;
                           approved documents are left alone
  approve-document --job ID [--kind cv|cover_letter] [--undo]   approve the draft documents of a job (both, or one kind):
                           only a draft with its PDF on disk; the PDF is copied to paths.approved and the document is
                           frozen; when both are approved the job becomes `approved`. --undo takes an approval back
  backup                   copy the database to backup.dir (checked, rotated to backup.keep copies)
  verify-backup PATH       check a backup file (integrity, schema version, row counts); read-only
  restore-backup PATH --yes  replace the database with a backup (safety copy first; no run may be active)
  revisions [--no-relay]  "Ask the writer to fix" (v0.85.0): relays the pending revision requests of blocked documents to the llm
                           service, stores the finished ones and mails the outcome; one scheduler step under the run lock
                           (exit 2 when another run holds it); --no-relay only finishes (the quiet window before the daily run)
  llm-worker [--once] [--health] [--check-cli]   the headless Claude Code worker of the `llm` container (v0.78.0): takes requests
                           from exchange/llm/requests, runs scorer, letters and review, writes exchange/llm/done; refuses to start
                           (exit 3) when a paid API setting (ANTHROPIC_*) is present; needs no config, no .env, no database
  run-report [ID | --last | --last-failed] [--json]   one run on one screen for pasting into chat (v0.78.3): timeline in UTC, counts,
                           drops, warnings, the done file's path; reads the database read-only (no migration), prints no job or CV
                           text, every line redacted; a missing done file is stated, not an error
  watchdog [--cleanup]    the missed-schedule check (alert `missed_schedule` when the last successful prepare is over 30 hours old)
                           and, with --cleanup, the clean-up after a run that was cut off (its `running` row is marked failed,
                           a left-over lock is replaced); the scheduler runs it, the web supervisor runs it hourly without
                           --cleanup (v0.80.6); always exits 0
  schedule [--health] [--next]  the daily run inside the container: prepare at schedule.prepare_time (Europe/Zurich), then
                           backup; off unless schedule.enabled is true (then it only idles); finish is never scheduled
  web [--host H] [--port N]   start the web app (Today's Top 10, job detail, open jobs) on 127.0.0.1, without a login unless
                           web.login.username is set; in the NAS container (web.container_bind) with a login; Ctrl+C stops it
  set-web-password         ask for the web password twice and print the RECRUITMENT_WEB_PASSWORD_HASH=... line for the .env that
                           the web app reads (writes no file, never prints the password; at least 12 characters)

Secrets come from the environment or the untracked .env. Exit codes: 0 done, 1 a step failed, 2 another run is
in progress, 3 configuration or prompt problem. `prepare`, `finish` and `backup` record health alerts and mail them
(monitoring.alerts, goal.md 5.9); a failing alert mail never changes the exit code.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import smtplib
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import adapters, alerts, archive_recheck, approval, backup, calibration, db, descriptions, descriptions_fetch, documents, headless, mailbox, merge, pipeline, postpone, rejection, resolver, runreport, traceability, weblogin
from . import results as batch_results
from .resolver import discovery
from .batch import withdraw_batch
from .config import ConfigError, load_config, load_env, resolve_path
from .prompt import PromptError, load_prompt
from .travel import Providers


class RunIdFilter(logging.Filter):
    """Adds `run_id` to every record: the run id of the environment, `-` for the web app and for a command typed by hand."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = os.environ.get(headless.RUN_ID_ENV) or "-"
        return True


def setup_logging(cfg: dict) -> None:
    """`2026-11-07T06:04:03Z INFO run=<id> message`: the stamp is UTC, and every line passes `headless.redact`."""
    log = logging.getLogger("recruitment")
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    fmt = headless.UtcFormatter("%(asctime)s %(levelname)s run=%(run_id)s %(message)s")
    logs = resolve_path(cfg["paths"]["logs"])
    logs.mkdir(parents=True, exist_ok=True)
    for handler in (RotatingFileHandler(logs / "recruitment.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"),
                    logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        handler.addFilter(RunIdFilter())
        log.addHandler(handler)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to config.yaml (default: config.yaml or $RECRUITMENT_CONFIG)")
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="ingest mail, filter, write the batch")
    prep.add_argument("--allow-draft-prompt", action="store_true",
                      help="use a prompt whose version ends in -draft (testing only; real scoring needs the approved prompt)")
    fin = sub.add_parser("finish", help="import results files and send the report")
    fin.add_argument("--limit-message", help="the exact usage-limit text Claude Code showed; labels the postponed jobs 'usage limit'")
    fin.add_argument("--headless-done", help="the done file of the llm service (exchange/llm/done/<run>.json): its limit message, the "
                                             "reviewer's flags and its summary are used, and the headless alerts are recorded")
    rv = sub.add_parser("revisions", help="relay and finish the 'Ask the writer to fix' requests of blocked documents")
    rv.add_argument("--no-relay", action="store_true", help="do not send pending requests to the llm service; only finish the answered ones")
    sub.add_parser("status", help="show counts and warnings")
    sub.add_parser("check-mail", help="list the Gmail folder names")
    rl = sub.add_parser("recheck-language", help="run the language rule again on stored descriptions")
    rl.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    rl.add_argument("--cap-scores", action="store_true",
                    help="instead of the language rule: lower stored scores of German postings to scoring.non_english_cap "
                         "(shows the list without --apply, writes nothing)")
    rl.add_argument("--apply", action="store_true", help="with --cap-scores: write the lowered scores")
    rp = sub.add_parser("recheck-poland", help="run the Poland rule (remote / hybrid / on-site) again on jobs located in Poland")
    rp.add_argument("--dry-run", action="store_true", help="show the counts and write nothing")
    ra = sub.add_parser("recheck-archive-location", help="run the location rule again, offline, on archived location rejections and restore those that now qualify")
    ra.add_argument("--dry-run", action="store_true", help="list the rows and write nothing (the caches may still fill with --online)")
    ra.add_argument("--online", action="store_true", help="look up places and travel times missing from the cache with the live services "
                    "(same providers and pacing as the daily filter, results cached); without it only the cache is used")
    md = sub.add_parser("merge-duplicates", help="join open jobs that only differ in the company spelling (AXA Switzerland = AXA)")
    md.add_argument("--dry-run", action="store_true", help="list the groups and write nothing (no backup, no migration)")
    rt = sub.add_parser("recheck-travel", help="run the location rule again, offline, from cached places and travel times")
    rt.add_argument("--dry-run", action="store_true", help="list the jobs whose result changes and write nothing")
    arch = sub.add_parser("archive", help="list archived jobs")
    arch.add_argument("--reason", help="only this reason code, for example language_c1_required")
    arch.add_argument("--limit", type=int, default=30)
    rest = sub.add_parser("restore-job", help="put an archived job back (with its earlier status if it was applied or later, else new)")
    rest.add_argument("archive_id", type=int)
    rej = sub.add_parser("reject", help="reject one open job like the web button (archive, reason user_rejected); --yes does it")
    rej.add_argument("job_id", type=int)
    rej.add_argument("--comment", default="", help=f"optional note, up to {rejection.COMMENT_MAX} characters")
    rej.add_argument("--yes", action="store_true", help="really reject; without it the job is only shown and nothing changes")
    miss = sub.add_parser("missing", help="list jobs without a description")
    miss.add_argument("--limit", type=int, default=20)
    setd = sub.add_parser("set-description", help="store a description you copied from the posting")
    setd.add_argument("job_id", type=int)
    src = setd.add_mutually_exclusive_group()
    src.add_argument("--clipboard", action="store_true", help="read the text from the Windows clipboard")
    src.add_argument("--file", help="read the text from this file (default: standard input)")
    sub.add_parser("import-descriptions", help="store every exchange/descriptions/<job id>.txt")
    rsel = sub.add_parser("reselect", help="re-pick the top list of batches that hold a job now waiting for a description")
    rsel.add_argument("--apply", action="store_true", help="really re-pick; without it only the selection before and after is shown")
    rcl = sub.add_parser("release-closed", help="end the awaiting_llm state of jobs that left the employer's list")
    rcl.add_argument("--apply", action="store_true", help="really change the jobs; without it only the list is shown")
    rs = sub.add_parser("rescore", help="mark open jobs to be scored again in the next batch")
    rs.add_argument("--job", type=int, action="append", required=True, help="the job id; repeatable")
    rs.add_argument("--dry-run", action="store_true", help="print what would change and write nothing")
    fetch = sub.add_parser("fetch-descriptions", help="fetch the descriptions of your UBS jobs")
    fetch.add_argument("--limit", type=int, help="fetch at most this many pages now")
    fetch.add_argument("--retry", action="store_true", help="also try pages that failed before")
    res = sub.add_parser("resolve-descriptions", help="look for the complete description of jobs without one on the employer's career site")
    res.add_argument("--job", type=int, help="only this job id")
    res.add_argument("--limit", type=int, help="try at most this many jobs now")
    res.add_argument("--dry-run", action="store_true", help="ask the site, print what would be stored, write nothing")
    res.add_argument("--discovery", action="store_true", help="only the same-job lookup on public job boards (resolver.discovery)")
    chk = sub.add_parser("check-adapter", help="read one employer job list without storing anything (for a first look)")
    chk.add_argument("name", help="one of: " + ", ".join(s.key for s in adapters.REGISTRY))
    chk.add_argument("--detail", action="store_true", help="also read the first job's page and show how much text it holds")
    cs = sub.add_parser("calibration-sheet", help="write a CSV of jobs to label by hand (no scores in it)")
    cs.add_argument("--count", type=int, default=calibration.DEFAULT_COUNT, help="how many jobs (default 30)")
    cs.add_argument("--out", help="file to write (default: exchange/calibration/labels_<date>.csv; never overwritten)")
    cr = sub.add_parser("calibration-report", help="compare your labelled CSV with the model's scores")
    cr.add_argument("path", help="the CSV you filled in")
    cr.add_argument("--json", action="store_true", help="print the result as JSON")
    dc = sub.add_parser("documents", help="list the stored tailored documents (read-only)")
    dc.add_argument("--job", type=int, help="show the findings and the text of this job's documents")
    dc.add_argument("--json", action="store_true", help="print the result as JSON")
    rb = sub.add_parser("recheck-documents", help="run the checks again on the stored blocked documents (preview unless --apply)")
    rb.add_argument("--apply", action="store_true", help="store the result and build the PDFs of the documents that now pass")
    rb.add_argument("--job", type=int, help="only this job")
    rd = sub.add_parser("render-documents", help="(re)build the PDFs of the stored draft documents")
    rd.add_argument("--job", type=int, help="only this job")
    rd.add_argument("--out", help="write the PDFs to this folder instead of paths.documents (the database is not changed)")
    ap = sub.add_parser("approve-document", help="approve the draft documents of a job (copies the PDFs to paths.approved)")
    ap.add_argument("--job", type=int, required=True, help="the job id")
    ap.add_argument("--kind", choices=documents.KINDS, help="only this document (default: both)")
    ap.add_argument("--undo", action="store_true", help="take the approval back (the document is a draft again, its approved copy is removed)")
    sub.add_parser("backup", help="copy the database to backup.dir, check the copy, rotate old copies")
    vb = sub.add_parser("verify-backup", help="check a backup file without changing anything")
    vb.add_argument("path")
    rb = sub.add_parser("restore-backup", help="replace the database with a backup (keeps a safety copy of the current one)")
    rb.add_argument("path")
    rb.add_argument("--yes", action="store_true", help="required: confirms that the current database is replaced")
    td = sub.add_parser("check-document", help="check that every fact in a document exists in the master CV file")
    td.add_argument("path", help="JSON document (title, company, blocks) or plain text")
    td.add_argument("--company", action="append", default=[], help="employer the document is addressed to (allowed by name); repeatable")
    td.add_argument("--generated-on", help="date the letter is generated, YYYY-MM-DD (that date and its year are allowed)")
    td.add_argument("--location", help="city (and address) of the employer, allowed in the letter header")
    td.add_argument("--contact", help="contact person named in the salutation")
    td.add_argument("--facts", help="fact file (default: profile/master_cv.yaml)")
    td.add_argument("--json", action="store_true", help="print the result as JSON")
    sub.add_parser("set-web-password", help="make the password hash for the web login (asks twice, prints one .env line, writes no file)")
    wb = sub.add_parser("web", help="start the web app on this computer (127.0.0.1); in the NAS container with a login (web.container_bind)")
    wb.add_argument("--host", help="127.0.0.1, localhost or ::1 (default: web.host); any other address is refused")
    wb.add_argument("--port", type=int, help="port (default: web.port)")
    wb.add_argument("--supervise", action="store_true", help="run the web app as a child and restart it when it dies (container)")
    sc = sub.add_parser("schedule", help="run the daily prepare and backup at schedule.prepare_time (container); off by default")
    sc.add_argument("--health", action="store_true", help="exit 0 if the scheduler's heartbeat is fresh (container health check)")
    sc.add_argument("--next", action="store_true", help="print when the next prepare is due and exit")
    wd = sub.add_parser("watchdog", help="the missed-schedule check and, with --cleanup, the clean-up after a cut-off run (v0.80.6)")
    wd.add_argument("--cleanup", action="store_true",
                    help="also mark a run that is still 'running' as failed and replace a left-over run lock (scheduler container only)")
    wd.add_argument("--startup", action="store_true",
                    help="with --cleanup, at the start of the scheduler: a lock of another PID namespace is a leftover of the stopped container")
    lw = sub.add_parser("llm-worker", help="the headless Claude Code worker (llm container; goal.md 5.10, v0.78.0)")
    lw.add_argument("--once", action="store_true", help="handle the waiting requests once and exit")
    lw.add_argument("--health", action="store_true", help="exit 0 if the worker's heartbeat is fresh (container health check)")
    lw.add_argument("--check-cli", action="store_true", help="run the role command line without a token and expect 'not logged in'")
    lw.add_argument("--command", dest="cli_command", default="claude", help="the CLI to run (default: claude)")
    lw.add_argument("--exchange", default="/app/exchange", help="the exchange folder (default: /app/exchange)")
    lw.add_argument("--secrets", default="/app/llm.env", help="the token file (default: /app/llm.env)")
    lw.add_argument("--home", default="/home/llm", help="the worker's home folder (default: /home/llm)")
    lw.add_argument("--app", default="/app", help="the folder with headless/agents, prompts and profile (default: /app)")
    rr = sub.add_parser("run-report", help="one run (timeline, counts, drops, warnings) for pasting into chat; read-only")
    rr.add_argument("run_id", nargs="?", help="the run id, for example 2026-11-07T043000Z")
    rr.add_argument("--last", action="store_true", help="the newest run")
    rr.add_argument("--last-failed", action="store_true", help="the newest run that failed (run status failed, or a done file that is not ok)")
    rr.add_argument("--json", action="store_true", help="machine output")
    wd = sub.add_parser("withdraw-batch", help="take back an unscored batch (for example after a prompt change)")
    wd.add_argument("run_id")
    return p


def _describe(info: backup.BackupInfo) -> str:
    counts = ", ".join(f"{t} {n}" for t, n in info.counts.items() if t in ("jobs", "job_archive", "scores", "job_sources"))
    return f"{info.path} ({info.size / 1024:.0f} KiB, schema version {info.version}; rows: {counts})"


def _set_web_password() -> int:
    """Ask for the web password twice and print the .env line with its hash. Writes no file; never prints the password."""
    first = getpass.getpass("New web password: ")
    second = getpass.getpass("Repeat the password: ")
    if first != second:
        print("ERROR: the two entries do not match; nothing was made.", file=sys.stderr)
        return 1
    if len(first) < weblogin.MIN_PASSWORD:
        print(f"ERROR: the password must have at least {weblogin.MIN_PASSWORD} characters; nothing was made.", file=sys.stderr)
        return 1
    print(f"RECRUITMENT_WEB_PASSWORD_HASH={weblogin.hash_password(first)}")
    print("Paste this line into the .env file that the web app reads (on the NAS: the file named by SECRETS_FILE), then restart the web app.")
    print("A new password ends every session that is open. The password itself is not stored anywhere.")
    return 0


def _llm_worker(args) -> int:
    """`llm-worker`: exit 3 when the guard refuses (a paid API setting, a bad token file). No config, no database."""
    import signal
    import threading
    from . import headless
    paths = headless.Paths.defaults(args.exchange, args.secrets, args.app, args.home)
    worker = headless.Worker(paths, args.cli_command, log=lambda msg: print(msg, flush=True))
    if args.health:
        ok = worker.healthy()
        print("ok" if ok else "worker heartbeat missing or too old")
        return 0 if ok else 1
    try:
        worker.check_start()
    except headless.GuardError as exc:
        print(f"llm-worker refuses to start: {exc}", file=sys.stderr)
        return 3
    if args.check_cli:
        ok, text = headless.check_cli(args.cli_command, paths)
        print(text)
        return 0 if ok else 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    sink = headless.WorkerLog(paths.llm / "llm-worker.log")
    worker.log = sink
    try:
        return worker.serve(stop, once=args.once)
    finally:
        sink.close()


def _backup_command(args, cfg: dict, smtp_factory=smtplib.SMTP) -> int:
    """backup, verify-backup, restore-backup. These never run db.connect: no migration may touch a database that is
    being copied, checked or replaced."""
    log = logging.getLogger("recruitment")
    live = resolve_path(cfg["paths"]["database"])
    folder = resolve_path(cfg["backup"]["dir"])
    try:
        if args.command == "backup":
            info, rotated = backup.create_backup(live, folder, cfg["backup"]["keep"])
            log.info("backup written: %s (%d bytes, schema %d), %d old copy(ies) rotated", info.path.name, info.size, info.version, len(rotated))
            print(f"backup written and checked: {_describe(info)}")
            print(f"rotation: keeping the newest {cfg['backup']['keep']}, {len(rotated)} removed")
            alerts.report_backup(cfg, None, smtp_factory=smtp_factory)
            return 0
        if args.command == "verify-backup":
            info = backup.verify_backup(args.path)
            print(f"OK: {_describe(info)}")
            if info.version > len(db.MIGRATIONS):
                print(f"NOTE: schema version {info.version} is newer than this code ({len(db.MIGRATIONS)}); restore would be refused")
            return 0
        if not args.yes:
            print("ERROR: restore-backup replaces the current database; run it again with --yes to confirm "
                  "(a safety copy of the current database is made first)", file=sys.stderr)
            return 1
        restored = False
        try:
            with pipeline.run_lock(cfg):
                info, safety = backup.restore_backup(args.path, live, folder)
                restored = True
                log.info("database restored from %s (schema %d); safety copy: %s", Path(args.path).name, info.version, safety.name if safety else "none")
                print(f"restored: {_describe(info)}")
                print(f"safety copy of the previous database: {safety}" if safety else "no previous database existed, so no safety copy was made")
                if info.version < len(db.MIGRATIONS):
                    print(f"schema version {info.version} is older than this code ({len(db.MIGRATIONS)}); the migrations run the next time the database is opened")
        except pipeline.Interrupted:
            if restored:
                log.error("event=interrupted command=%s after the restore completed", args.command)
                print("restore completed, then interrupted by a stop signal", file=sys.stderr)
            raise
        return 0
    except pipeline.RunLockError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except pipeline.Interrupted:                             # SIGTERM while the run held the lock
        log.error("event=interrupted command=%s", args.command)
        return 143
    except (backup.BackupError, OSError) as exc:
        log.error("%s failed: %s", args.command, exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.command == "backup":
            alerts.report_backup(cfg, str(exc), smtp_factory=smtp_factory)
        return 1


def _run_report_command(args, cfg: dict) -> int:
    """run-report: exactly one of ID, --last, --last-failed. Exit 1 with a message when the database, the run or a failed run is missing."""
    chosen = [bool(args.run_id), args.last, args.last_failed]
    if sum(chosen) != 1:
        print("ERROR: give exactly one of RUN_ID, --last or --last-failed.", file=sys.stderr)
        return 1
    try:
        conn = runreport.open_read_only(resolve_path(cfg["paths"]["database"]))
        try:
            text = runreport.run_report(conn, resolve_path(cfg["paths"]["exchange"]), args.run_id, args.last, args.last_failed, args.json)
        finally:
            conn.close()
    except (runreport.ReportError, sqlite3.Error) as exc:
        print(f"ERROR: {headless.redact(str(exc))}", file=sys.stderr)
        return 1
    print(text, end="")
    return 0


def _merge_command(args, cfg: dict) -> int:
    """merge-duplicates. Dispatched before db.connect: the plan is read on a read-only connection (a pending migration is not applied,
    a dry run changes nothing), the real run holds the run lock, makes its own checked backup, and only then opens the database."""
    log = logging.getLogger("recruitment")
    live = resolve_path(cfg["paths"]["database"])
    if not live.is_file():
        print("ERROR: the database file does not exist yet.", file=sys.stderr)
        return 1

    def read_only():
        conn = sqlite3.connect(f"{live.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def show(p, header):
        print(header)
        for line in merge.format_plan(p):
            print(line)
        print(merge.summary(p))

    try:
        if args.dry_run:
            conn = read_only()
            try:
                show(merge.plan(conn, cfg), "(dry run, nothing written)")
            finally:
                conn.close()
            return 0
        with pipeline.run_lock(cfg):
            conn = read_only()
            try:
                first = merge.plan(conn, cfg)
            finally:
                conn.close()
            if first.empty:
                show(first, "nothing to merge or re-key; no backup made")
                return 0
            info, _ = backup.create_backup(live, resolve_path(cfg["backup"]["dir"]), cfg["backup"]["keep"], prefix="jobs_pre-merge")
            conn = db.connect(live)
            try:
                p = merge.plan(conn, cfg)
                show(p, f"backup before the merge: {info.path}")
                result = merge.apply(conn, cfg, p, info.path.name)
            finally:
                conn.close()
            for note in result.notes:
                print(f"warning: {note}")
            log.info("merge-duplicates: %d job(s) merged away, %d open key(s) and %d archive key(s) rewritten (backup %s)",
                     result.merged, result.rekeyed_open, result.rekeyed_archive, info.path.name)
            print(f"merged: {result.merged} job(s) removed, {result.rekeyed_open} open key(s) and {result.rekeyed_archive} archive key(s) rewritten; "
                  f"restore from {info.path.name} if needed")
            return 0
    except pipeline.RunLockError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except pipeline.Interrupted:                             # SIGTERM while the run held the lock
        log.error("event=interrupted command=merge-duplicates")
        return 143
    except (backup.BackupError, sqlite3.Error) as exc:
        log.error("merge-duplicates failed: %s", exc)
        print(f"ERROR: {exc}; nothing was merged" if isinstance(exc, backup.BackupError) else f"ERROR: {exc}", file=sys.stderr)
        return 1


def _documents_command(conn, cfg, args) -> int:
    """List the stored documents, or show those of one job. Read-only."""
    approved_root = documents.approved_dir(cfg)
    sql = ("SELECT d.job_id, d.kind, d.status, d.findings, d.content, d.generated_on, d.run_id, d.pdf_path, d.pages, "
           "d.approved_at, d.approved_pdf_path, j.title, j.company "
           "FROM documents d JOIN jobs j ON j.id = d.job_id " + ("WHERE d.job_id = ? " if args.job else "")
           + "ORDER BY d.job_id, d.kind")
    rows = conn.execute(sql, (args.job,) if args.job else ()).fetchall()
    if args.job and not rows:
        print(f"ERROR: job {args.job} has no stored documents", file=sys.stderr)
        return 1
    if args.json:
        out = [{"job_id": r["job_id"], "title": r["title"], "company": r["company"], "kind": r["kind"], "status": r["status"],
                "findings": json.loads(r["findings"]), "generated_on": r["generated_on"], "run_id": r["run_id"],
                "pdf_path": r["pdf_path"], "pages": r["pages"], "approved_at": r["approved_at"],
                "approved_pdf_path": r["approved_pdf_path"],
                **({"content": json.loads(r["content"])} if args.job else {})} for r in rows]
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    if not args.job:
        for r in rows:
            n = len(json.loads(r["findings"]))
            pdf = f"{r['pages']} page(s)" if r["pdf_path"] else "no PDF"
            print(f"{r['job_id']:>5} | {r['kind']:<12} | {r['status']:<8} | {n:>2} finding(s) | {pdf:<9} | {r['title'][:50]} ({r['company']})")
            if r["status"] == "approved":
                print(f"{'':>7}approved {r['approved_at']}: {approved_root / r['approved_pdf_path']}")
        print(f"{len(rows)} document(s)")
        return 0
    for r in rows:
        findings = json.loads(r["findings"])
        print(f"=== {r['kind']} for job {r['job_id']}: {r['title']} ({r['company']}) | {r['status']} | generated {r['generated_on']} ===")
        if r["status"] == "approved":
            print(f"approved {r['approved_at']}: {approved_root / r['approved_pdf_path']}")
        for f in findings:
            print(f"FINDING [{f['kind']}] \"{f['text']}\" ({f.get('where') or 'document'}): {f['reason']}")
        if not findings:
            print("no findings")
        print()
        print(documents.document_text(r["kind"], json.loads(r["content"])))
        print()
    return 0


def _render_documents_command(conn, cfg, args) -> int:
    """Build the PDFs of stored draft documents. Writes only the PDF files and the pdf columns of `documents`
    (nothing at all to the database with --out)."""
    if args.job and not conn.execute("SELECT 1 FROM documents WHERE job_id = ?", (args.job,)).fetchone():
        print(f"ERROR: job {args.job} has no stored documents", file=sys.stderr)
        return 1
    out_dir = Path(args.out) if args.out else None
    res = documents.render_stored(conn, cfg, [args.job] if args.job else None, out_dir=out_dir, write_db=out_dir is None)
    base = out_dir if out_dir else documents.documents_dir(cfg)
    for job_id, kind, path, pages in res.rendered:
        shown = path if out_dir else str(base / path)
        print(f"job {job_id} {kind:<12} {pages} page(s)  {shown}")
    for job_id, kind, pages, limit in res.over_limit:
        print(f"WARNING: job {job_id} {kind} has {pages} pages (limit {limit}); the PDF is kept", file=sys.stderr)
    for job_id, kind, reason in res.failed:
        print(f"ERROR: job {job_id} {kind}: {reason}", file=sys.stderr)
    for job_id, kind, why in res.skipped:
        print(f"skipped: job {job_id} {kind}: {why}")
    for w in res.warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    print(f"{len(res.rendered)} PDF(s) built, {len(res.skipped)} skipped, {len(res.failed)} failed")
    return 1 if res.failed else 0


def _approve_command(conn, cfg, args) -> int:
    """Approve (or, with --undo, take back the approval of) the documents of one job. Exit 1 when anything was refused."""
    kinds = [args.kind] if args.kind else None
    res = approval.undo(conn, cfg, args.job, kinds) if args.undo else approval.approve(conn, cfg, args.job, kinds)
    for level, text in approval.outcome_lines(res, args.undo):        # the same messages the web app shows
        print(text, file=sys.stderr if level in ("refused", "warning") else sys.stdout)
    return 1 if res.refused else 0


def _alert_after_prepare(conn, cfg: dict, r, smtp_factory) -> None:
    """Exit 1 of `prepare` is `run_failed:prepare`, a clean run clears it. A refused login is the alert `gmail_auth:imap`
    (recorded by `prepare` itself) and not a second one."""
    if r.mail_error is None:
        alerts.clear(conn, "run_failed:prepare")
        alerts.clear(conn, "run_interrupted:prepare")         # a complete run: the watchdog's mail about the cut-off one is resolved
    elif r.mail_login is not False:
        alerts.observe(conn, cfg, "run_failed:prepare", f"the mail step failed: {r.mail_error}")
    alerts.deliver(conn, cfg, smtp_factory=smtp_factory)


def _alert_after_finish(conn, cfg: dict, r, smtp_factory) -> None:
    """Exit 1 of `finish` is `run_failed:finish`, except a refused SMTP login (that is `gmail_auth:smtp`, recorded by `finish`)."""
    failed = {b: v for b, v in r.reports.items() if v.startswith("not sent")}
    other = [b for b in failed if b not in r.auth_failed]
    bad_updates = {b: x for b, v in r.updates.items() for x in v if x.startswith("not sent")}
    if other:
        alerts.observe(conn, cfg, "run_failed:finish", f"{len(other)} report(s) not sent: {failed[other[0]]}")
    elif bad_updates:
        alerts.observe(conn, cfg, "run_failed:finish", f"{len(bad_updates)} update mail(s) not sent: {next(iter(bad_updates.values()))}")
    elif not failed:
        alerts.clear(conn, "run_failed:finish")
        alerts.clear(conn, "run_interrupted:finish")
    alerts.deliver(conn, cfg, smtp_factory=smtp_factory)


def _alert_failure(conn, cfg: dict, command: str, message: str, smtp_factory) -> None:
    """A run that stopped with exit 3 or with an exception."""
    alerts.observe(conn, cfg, f"run_failed:{command}", message)
    alerts.deliver(conn, cfg, smtp_factory=smtp_factory)


def _reject_command(conn: sqlite3.Connection, cfg: dict, args, log: logging.Logger) -> int:
    """`reject JOB_ID`: the same effect as the web button (app/rejection.py). No run lock, no backup (decided 11 Nov 2026)."""
    comment = rejection.clean_comment(args.comment)
    if len(comment) > rejection.COMMENT_MAX:
        print(f"ERROR: the comment is longer than {rejection.COMMENT_MAX} characters. Nothing was changed.", file=sys.stderr)
        return 1

    def not_open() -> str:
        arch = conn.execute("SELECT id FROM job_archive WHERE original_job_id = ? AND restored_at IS NULL ORDER BY id DESC LIMIT 1",
                            (args.job_id,)).fetchone()
        return (f"job {args.job_id} is already archived as entry {arch['id']}" if arch else f"no open job {args.job_id}") + ". Nothing was changed."

    try:
        if not args.yes:
            row = conn.execute("SELECT title, company, status FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
            if row is None:
                print(f"ERROR: {not_open()}", file=sys.stderr)
                return 1
            why = rejection.refusal(args.job_id, row["status"])
            if why:
                print(f"ERROR: {why}", file=sys.stderr)
                return 1
            print(f"job {args.job_id} | {row['status']} | {row['title']} ({row['company']})")
            if row["status"] == "applied":
                print("WARNING: you marked this job as applied. Rejecting it archives the application: it leaves the Applied page and no longer "
                      "counts in the Analytics response rate; restore-job brings it back as applied.")
            kinds = rejection.approved_kinds(conn, args.job_id)
            if kinds:
                print(f"WARNING: approved documents ({', '.join(kinds)}) exist; their PDFs stay on disk and the archive entry notes it.")
            print("Nothing changed; run again with --yes to reject it.")
            return 0
        done = rejection.reject_job(conn, args.job_id, comment, cfg["paths"]["approved"])
    except rejection.RejectRefused as exc:
        print(f"ERROR: {not_open() if exc.gone else exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        log.warning("cli: reject on job %s failed: %s", args.job_id, exc.__class__.__name__)
        print(f"ERROR: {rejection.DB_BUSY_TEXT}", file=sys.stderr)
        return 1
    log.info("cli: job %s archived as rejected by the user (archive entry %s)", args.job_id, done.archive_id)
    print(f"Job {args.job_id} ({done.title}, {done.company}) was rejected and moved to the archive (archive entry {done.archive_id}, "
          f"reason user_rejected). It is gone from the working lists. To bring it back: python -m app restore-job {done.archive_id}")
    return 0


def main(argv: list[str] | None = None, *, mail_client_factory=mailbox.connect, providers_factory=Providers.live,
         smtp_factory=None, page_fetcher=None, web_runner=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check-document":                      # needs no config, no database, no network
        try:
            doc = traceability.load_document(args.path)
            facts = traceability.load_facts(args.facts)
            generated_on = traceability.parse_date(args.generated_on)
        except traceability.TraceabilityError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        allowed = list(args.company) + ([doc.company] if doc.company else [])
        result = traceability.check_document(doc, facts, allowed_employers=allowed, generated_on=generated_on,
                                             employer_location=args.location, contact=args.contact)
        print(json.dumps(result.to_dict(), indent=1, ensure_ascii=False) if args.json else result.render_text())
        return 0 if result.ok else 1
    if args.command == "set-web-password":                    # needs no config, no database, no network
        return _set_web_password()
    if args.command == "llm-worker":                          # before load_env, load_config and setup_logging: the llm container has no
        return _llm_worker(args)                              # .env, no config.yaml and a read-only log folder (goal.md 5.10, v0.78.0)
    load_env()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 3
    setup_logging(cfg)
    log = logging.getLogger("recruitment")
    if args.command == "schedule":                             # no database, no network; runs the other commands as processes
        from . import scheduler
        return scheduler.command(cfg, health_only=args.health, show_next=args.next)
    if args.command == "watchdog":                             # opens the database without a migration; never raises, exits 0
        from . import watchdog
        try:
            return watchdog.run(cfg, cleanup=args.cleanup, startup=args.startup, smtp_factory=smtp_factory or smtplib.SMTP)
        except pipeline.Interrupted:                           # stopped while it held the lock for the clean-up
            return 143
    if args.command == "web":                                  # reads the database read-only; never db.connect (no migration)
        if args.supervise:                                     # slim supervisor (v0.77.0): restarts `web` as a child process
            import signal
            import threading
            from . import websuper
            stop = threading.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            extra = (["--config", args.config] if args.config else []) + []
            tail = (["--host", args.host] if args.host else []) + (["--port", str(args.port)] if args.port is not None else [])

            def hourly_watchdog() -> int:                       # H27 from the web container (v0.80.6): mails `missed_schedule` only
                return subprocess.run([sys.executable, "-m", "app", *extra, "watchdog"], timeout=600).returncode
            return websuper.supervise(cfg, stop, extra, child_tail=tail, watchdog=hourly_watchdog)
        from . import web                                      # imported here: the other commands do not need FastAPI
        try:
            return web.serve(cfg, args.host, args.port, run=web_runner)
        except ConfigError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 3
    if args.command in ("backup", "verify-backup", "restore-backup"):
        return _backup_command(args, cfg, smtp_factory or smtplib.SMTP)
    if args.command == "merge-duplicates":
        return _merge_command(args, cfg)
    if args.command == "run-report":                           # read-only connection, never db.connect (no migration)
        return _run_report_command(args, cfg)
    conn = db.connect(resolve_path(cfg["paths"]["database"]))
    smtp = smtp_factory or smtplib.SMTP
    try:
        if args.command == "status":
            print(json.dumps(pipeline.status(conn, cfg), indent=1, ensure_ascii=False))
            return 0

        if args.command == "documents":
            return _documents_command(conn, cfg, args)

        if args.command == "approve-document":
            return _approve_command(conn, cfg, args)

        if args.command == "recheck-documents":
            results = documents.recheck_blocked(conn, cfg, apply=args.apply, job_id=args.job)
            for o in results:
                left = ", ".join(f"{f['kind']}: {f['text']}" for f in o["findings"])
                print(f"job {o['job_id']} {o['kind']:<12} {o['before']} finding(s) before -> {o['status']}" + (f" ({left})" if left else ""))
            passed = sum(o["status"] == "draft" for o in results)
            print(f"{'Done' if args.apply else 'Preview, nothing changed'}: {passed} of {len(results)} blocked document(s) pass")
            return 0

        if args.command == "render-documents":
            return _render_documents_command(conn, cfg, args)

        if args.command in ("calibration-sheet", "calibration-report"):
            try:
                if args.command == "calibration-sheet":
                    target = Path(args.out) if args.out else calibration.default_sheet_path(cfg)
                    s = calibration.build_sheet(conn, target, args.count)
                    print(f"{s['written']} of {s['scored']} scored job(s) written to {s['path']}\n"
                          "Fill in the your_label column (0-100, or low / medium / high) without looking at the report, "
                          "then run: python -m app calibration-report <that file>")
                    return 0
                r = calibration.build_report(conn, cfg, Path(args.path))
            except calibration.CalibrationError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            print(json.dumps(r, indent=1, ensure_ascii=False) if args.json else calibration.render_text(r, cfg))
            return 0

        if args.command == "fetch-descriptions":
            try:
                s = descriptions_fetch.fetch_descriptions(
                    conn, cfg, page_fetcher or descriptions_fetch.PageFetcher(cfg["descriptions"]["user_agent"], cfg["descriptions"]["timeout_seconds"],
                                                                               descriptions_fetch.ALLOWED_HOST),
                    limit=args.limit, retry=args.retry)
            except descriptions_fetch.FetchDisabled as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            for job_id, message in s.items:
                print(f"job {job_id}: {message}")
            print(f"{s.fetched} fetched, {s.failed} failed, {s.remaining_today} page(s) left for today")
            if s.stopped:
                print(f"STOPPED: {s.stopped}", file=sys.stderr)
            return 1 if s.stopped and s.fetched == 0 else 0

        if args.command == "resolve-descriptions":
            if not cfg["resolver"]["enabled"]:
                print("ERROR: the resolver is switched off (resolver.enabled is false); switch it on in config.yaml", file=sys.stderr)
                return 1
            run = discovery.discover_descriptions if args.discovery else resolver.resolve_descriptions
            s = run(
                conn, cfg, page_fetcher or descriptions_fetch.PageFetcher(resolver.user_agent(cfg), cfg["resolver"]["timeout_seconds"]),
                only_job=args.job, dry_run=args.dry_run, limit=args.limit)
            for job_id, message in s.items:
                print(f"job {job_id}: {message}")
            note = "(dry run, nothing written" + (" except the board-name guesses" if args.discovery else "") + ") "
            looked = f"{s.looked_up} job(s) looked up, " if args.discovery else ""
            print(f"{note if args.dry_run else ''}{looked}{s.attempted} job(s) tried, {s.resolved} resolved")
            if s.stopped:
                print(f"STOPPED: {s.stopped}", file=sys.stderr)
            return 1 if s.stopped and s.resolved == 0 else 0

        if args.command == "check-adapter":
            spec = next((s for s in adapters.REGISTRY if s.key == args.name), None)
            if spec is None:
                print(f"ERROR: unknown adapter {args.name!r}; known: {', '.join(s.key for s in adapters.REGISTRY)}", file=sys.stderr)
                return 1
            fetch = page_fetcher or descriptions_fetch.PageFetcher(cfg["adapters"]["user_agent"], cfg["adapters"]["timeout_seconds"])
            try:
                summary, jobs = adapters.fetch_list(conn, cfg, spec, fetch, store=False)
            except adapters.AdapterDisabled as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            if summary.error:
                print(f"ERROR: {summary.error}", file=sys.stderr)
                return 1
            print(f"{spec.label}: {summary.found} job(s) listed (nothing stored)")
            for j in jobs[:5]:
                print(f"  {j.external_id} | {', '.join(j.locations)} | {j.title[:70]}")
            if args.detail and jobs:
                try:
                    detail = spec.parse_detail(adapters.read_page(cfg, spec, fetch, jobs[0].url))
                except (descriptions_fetch.FetchError, adapters.PageError) as exc:
                    print(f"ERROR: first job page: {exc}", file=sys.stderr)
                    return 1
                print(f"first job page: {len(detail.text)} characters, posted {detail.posted or 'not stated'}")
            return 0

        if args.command == "archive":
            sql =("SELECT id, title, company, reason_code, rejection_stage, substr(rejected_at, 1, 10) AS day, restored_at "
                   "FROM job_archive " + ("WHERE reason_code = ? " if args.reason else "") + "ORDER BY id DESC LIMIT ?")
            rows = conn.execute(sql, ((args.reason, args.limit) if args.reason else (args.limit,))).fetchall()
            for r in rows:
                print(f"{r['id']:>4} | {r['day']} | {r['rejection_stage']:<10} | {r['reason_code']:<28} | {r['title'][:50]} ({r['company']})"
                      + (" [restored]" if r["restored_at"] else ""))
            print(f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'} shown")
            return 0

        if args.command == "restore-job":
            try:
                new_id = db.restore_job(conn, args.archive_id)
            except (KeyError, ValueError) as exc:
                print(f"ERROR: {exc.args[0] if exc.args else exc}", file=sys.stderr)
                return 1
            status = conn.execute("SELECT status FROM jobs WHERE id = ?", (new_id,)).fetchone()[0]
            print(f"archive entry {args.archive_id} restored as job {new_id} (status {status})"
                  + ("; the filters run on it in the next prepare (or run `recheck-language` for the language rule now)"
                     if status == "new" else ""))
            return 0

        if args.command == "reject":
            return _reject_command(conn, cfg, args, log)

        if args.command == "recheck-language" and args.cap_scores:
            rows = descriptions.cap_german_scores(conn, cfg, apply=args.apply and not args.dry_run)
            for r in rows:
                print(f"{r['job_id']}	{r['title']}	{r['old']} -> {r['new']}")
            print(f"{'' if args.apply and not args.dry_run else '(dry run, nothing written) '}{len(rows)} German posting(s) above "
                  f"the cap of {cfg['scoring']['non_english_cap']}{', lowered' if args.apply and not args.dry_run else ''}")
            return 0

        if args.command == "recheck-language":
            c = descriptions.recheck_language(conn, cfg, dry_run=args.dry_run)
            print(f"{'(dry run, nothing written) ' if args.dry_run else ''}{c['checked']} job(s) checked, {c['changed']} changed, "
                  f"{c['archived']} archived (language requirement of C1 or above), {c.get('skipped', 0)} skipped (you already work on them)")
            return 0

        if args.command == "recheck-poland":
            c = descriptions.recheck_poland(conn, cfg, dry_run=args.dry_run)
            print(f"{'(dry run, nothing written) ' if args.dry_run else ''}{c['checked']} job(s) in Poland checked, {c['changed']} changed, "
                  f"{c['archived']} archived (on-site), {c['rescore']} marked to be scored again, {c['skipped']} skipped (you already work on them)")
            return 0

        if args.command == "recheck-travel":
            r = descriptions.recheck_travel(conn, cfg, dry_run=args.dry_run)
            for i in r.items:
                old = f"{i.old_minutes:.0f}" if i.old_minutes is not None else "-"
                new = f"{i.new_minutes:.0f}" if i.new_minutes is not None else "-"
                print(f"job {i.job_id} | {i.title} | {i.company} | train {old} -> {new} min | {i.old_outcome} -> {i.new_outcome}"
                      + (f" | skipped: {i.skipped}" if i.skipped else f" | {i.action}" if i.action else ""))
            print(f"{r.checked} job(s) checked, {r.changed} changed, {r.archived} archived, {r.rescore} marked to be scored again, "
                  f"{r.skipped_user} skipped (you already work on them), {r.skipped_uncached} skipped (not in the cache)"
                  + (" (dry run, nothing written)" if args.dry_run else ""))
            return 0

        if args.command == "recheck-archive-location":
            r = archive_recheck.recheck_archive_location(conn, cfg, dry_run=args.dry_run,
                                                          providers=providers_factory(cfg) if args.online else None)
            for i in r.items:
                print(f"archive {i.archive_id} | {i.title} | {i.company} | {'; '.join(i.locations) or '-'} | {i.old_reason} -> "
                      f"{i.new_outcome}{' ' + i.new_reason if i.new_reason else ''}"
                      + (f" | skipped: {i.skipped}" if i.skipped else f" | restore{f' as job {i.restored_job_id}' if i.restored_job_id else ''}" if i.action else ""))
            print(f"{r.checked} row(s) checked, {r.restored} {'to restore' if args.dry_run else 'restored'}, {r.still_failing} still fail, "
                  f"{r.skipped_uncached} skipped (not in the cache), {r.skipped_open} skipped (job already open)"
                  + (" (dry run, nothing written)" if args.dry_run else ""))
            return 0

        if args.command == "missing":
            rows = descriptions.jobs_needing_description(conn, args.limit)
            if not rows:
                print("every open job has a description")
            for r in rows:
                shown = "waits (top 10)" if r["queued"] else f"score {r['score'] if r['score'] is not None else '-':>3}"
                print(f"{r['id']:>5} | {r['status']:<13} | {shown} | "
                      f"{r['title'][:60]} ({r['company']})\n        {r['url'] or 'no link'}")
            return 0

        if args.command in ("set-description", "import-descriptions"):
            try:
                if args.command == "import-descriptions":
                    results = descriptions.import_description_files(conn, cfg)
                    for name, message in results:
                        print(f"{name}: {message}")
                    print(f"{len(results)} file(s) handled")
                    return 0
                if args.clipboard:
                    text = descriptions.read_clipboard()
                elif args.file:
                    text = open(args.file, encoding="utf-8", errors="replace").read()
                else:
                    print("Paste the posting, then press Ctrl+Z and Enter:", file=sys.stderr)
                    text = sys.stdin.read()
                result = descriptions.apply_description(conn, cfg, args.job_id, text, source="manual",
                                                       require_complete=True)
            except (descriptions.DescriptionError, OSError) as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            print(f"job {result.job_id}: {result.message} ({result.chars} characters; language rule: {result.language or '-'})")
            return 0

        if args.command == "reselect":
            hold, top_n = cfg["tailoring"]["require_description"], cfg["scoring"]["top_n"]
            batches = batch_results.stale_selection_batches(conn, cfg)
            for batch_id in batches:
                before = [(r["job_id"], r["rank"]) for r in conn.execute(
                    "SELECT job_id, rank FROM batch_jobs WHERE batch_id = ? AND selected = 1 ORDER BY rank", (batch_id,))]
                chosen, queued = batch_results.compute_selection(conn, batch_id, top_n, hold)
                print(f"batch {batch_id}")
                print("  before: " + (", ".join(f"{r}:{j}" for j, r in before) or "-") + "   (rank:job)")
                print("  after:  " + (", ".join(f"{r}:{j}" for r, j in enumerate(chosen, start=1)) or "-")
                      + ("   queued (place:job) " + ", ".join(f"{p}:{j}" for p, j in queued) if queued else ""))
                if args.apply:
                    batch_results.select_top(conn, batch_id, top_n, hold)
            print(f"{len(batches)} batch(es) " + ("re-picked" if args.apply else "would be re-picked (preview, nothing written; --apply does it)"))
            return 0

        if args.command == "release-closed":
            waiting = postpone.closed_waiting_jobs(conn)
            changed = 0
            for job_id in waiting:
                job = conn.execute("SELECT title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
                open_p = conn.execute("SELECT COUNT(*) FROM postponements WHERE job_id = ? AND resolved_at IS NULL", (job_id,)).fetchone()[0]
                with conn:
                    target = postpone.release_closed(conn, job_id, dry_run=not args.apply)
                if target is None:
                    print(f"job {job_id} | {job['title']} | {job['company']} | stays: in the newest batch, whose results are not imported yet")
                    continue
                changed += 1
                print(f"job {job_id} | {job['title']} | {job['company']} | awaiting_llm -> {target} | {open_p} open postponement(s) "
                      + ("resolved" if args.apply else "would be resolved"))
            print(f"{len(waiting)} closed job(s) waiting for a score, {changed} " + ("released" if args.apply else "would be released (preview, nothing written; --apply does it)"))
            return 0

        if args.command == "rescore":
            try:
                items = descriptions.mark_rescore(conn, args.job, dry_run=args.dry_run)
            except descriptions.DescriptionError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            for it in items:
                line = f"job {it.job_id} | {it.title} | {it.company} | needs_rescore {it.old} -> {it.new}"
                print(line + (f" | skipped: {it.skipped}" if it.skipped else ""))
            marked = sum(1 for it in items if not it.skipped and it.old != it.new)
            print(f"{'would mark' if args.dry_run else 'marked'} {marked} job(s) for scoring again; "
                  f"{sum(1 for it in items if it.skipped)} skipped"
                  + ("; nothing was written (--dry-run)" if args.dry_run else "; the next `prepare` puts them in the batch"))
            return 0

        if args.command == "withdraw-batch":
            try:
                n = withdraw_batch(conn, cfg, args.run_id)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            print(f"batch {args.run_id} withdrawn; {n} job(s) are back to new and go into the next batch")
            return 0

        if args.command == "check-mail":
            try:
                client = mail_client_factory(cfg)
                try:
                    names = mailbox.list_folders(client)
                finally:
                    client.logout()
            except mailbox.MailError as exc:
                print(f"mail error: {exc}", file=sys.stderr)
                return 1
            for name in names:
                print(name)
            print()
            all_found = True
            for source in mailbox.SOURCES:
                wanted = cfg["gmail"][source.label_key]
                all_found = all_found and wanted in names
                print(f"configured {source.name} label {wanted!r}: " + ("found" if wanted in names else "NOT FOUND in the list above"))
            return 0 if all_found else 1

        try:
            if args.command == "revisions":
                from . import revisions
                with pipeline.run_lock(cfg):
                    r = revisions.run(conn, cfg, relay_requests=not args.no_relay, smtp_factory=smtp_factory)
                for line in r.lines:
                    print(line)
                if not r.lines:
                    print("no revision request to handle")
                return 0
            if args.command == "prepare":
                prompt = load_prompt(cfg, allow_draft=args.allow_draft_prompt)
                inherited = os.environ.get(headless.RUN_ID_ENV, "")          # the scheduler's run id (v0.78.2); a typed command makes its own
                r = pipeline.prepare(conn, cfg, providers=providers_factory(cfg), prompt=prompt,
                                     mail_client_factory=mail_client_factory, page_fetcher=page_fetcher,
                                     run_id=inherited if headless.RUN_ID_RE.fullmatch(inherited) else None)
                mail = r.mail
                mails = list(r.mails.values())
                print(f"run {r.run_id}: mails {sum(m.mails_ok for m in mails)} ok / {sum(m.mails_failed for m in mails)} failed, "
                      f"jobs found {sum(m.found for m in mails)}, new {sum(m.new_jobs for m in mails)}; filters {r.filters}; "
                      + (f"batch {r.batch.path} ({r.batch.job_count} jobs)" if r.batch else "no batch (nothing to score)"))
                for w in r.warnings:
                    print(f"WARNING: {w}")
                if r.mail_error:
                    print(f"ERROR: {r.mail_error}", file=sys.stderr)
                _alert_after_prepare(conn, cfg, r, smtp)
                return 1 if r.mail_error else 0
            if args.command == "finish":
                kwargs = {"smtp_factory": smtp_factory} if smtp_factory else {}
                headless_done = None
                if args.headless_done:
                    try:
                        headless_done = json.loads(Path(args.headless_done).read_text(encoding="utf-8"))
                        if not isinstance(headless_done, dict):
                            raise ValueError("not an object")
                    except (OSError, ValueError) as exc:
                        print(f"WARNING: the headless done file cannot be read ({type(exc).__name__}); finishing without it", file=sys.stderr)
                        headless_done = {"status": "error", "message": "the done file cannot be read"}
                    headless_done.setdefault("run_id", Path(args.headless_done).stem)         # names the run in the alert (v0.78.2)
                r = pipeline.finish(conn, cfg, limit_message=args.limit_message, headless_done=headless_done, **kwargs)
                for item in r.imports:
                    print(f"results for {item.batch_id}: {item.status}"
                          + (f" ({item.reason})" if item.reason else f", scored {len(item.scored)}, top {item.top}"))
                    for w in item.warnings:
                        print(f"  WARNING: {w}")
                for batch_id, (label, job_ids) in r.postponed.items():
                    print(f"postponed for {batch_id}: {len(job_ids)} job(s) ({label})")
                for batch_id, outcomes in r.updates.items():
                    print(f"update mail {batch_id}: {', '.join(outcomes)}")
                for batch_id, outcome in r.reports.items():
                    print(f"report {batch_id}: {outcome}")
                alerts.observe_headless(conn, cfg, headless_done)
                _alert_after_finish(conn, cfg, r, smtp)
                return 1 if any(v.startswith("not sent") for v in r.reports.values()) or any(
                    x.startswith("not sent") for v in r.updates.values() for x in v) else 0
        except pipeline.RunLockError as exc:
            log.error("%s", exc)
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2                                             # another run is active: nothing failed, no alert
        except pipeline.Interrupted:                             # SIGTERM while the run held the lock (container stop, redeploy, step limit)
            log.error("event=interrupted command=%s", args.command)
            alerts.observe(conn, cfg, f"run_interrupted:{args.command}",
                           f"{args.command} was stopped by a stop signal at {datetime.now(timezone.utc):%H:%M} UTC (container stop, restart, "
                           "redeploy or the 5-hour step limit); " + ("its run is marked failed" if args.command == "prepare" else "the next finish continues"))
            return 143                                           # no mail now: the container is stopping; the watchdog or the next run delivers it
        except PromptError as exc:
            print(f"prompt error: {exc}", file=sys.stderr)
            _alert_failure(conn, cfg, "prepare", f"the scoring prompt cannot be used: {exc}", smtp)
            return 3
        except Exception as exc:                                 # a crash: note it, mail it, and let it end the program as before
            log.error("%s failed with %s", args.command, type(exc).__name__)
            _alert_failure(conn, cfg, args.command, f"unexpected error: {type(exc).__name__}", smtp)
            raise
    finally:
        conn.close()
    return 0
