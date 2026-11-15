"""The two scheduled steps of option C (goal.md 4, 5.9, 5.10).

`prepare`  (the NAS, daily): read the alert mails (UBS, LinkedIn, Julius Baer), store and deduplicate jobs, run the filters, write the batch.
`finish`   (the NAS, after the user ran the Claude session): import the results files, send the report(s).
Between the two the user's Claude session scores the batch (docs/SCORING.md).

Both take the run lock, so two runs never overlap. Everything external is passed in (mail client factory,
travel-time providers, SMTP factory), so tests run without a network. `prepare` requires `providers`
explicitly: without them the location rule would silently not run.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import smtplib
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import adapters, alerts, db, descriptions_fetch, documents, mailbox, postpone, resolver, websuper
from .resolver import discovery
from .batch import BatchInfo, build_batch, exchange_dir, held_back_ids, new_run_id, oldest_awaiting_age_days
from .config import resolve_path
from .filters.runner import filter_new_jobs
from .mailbox import MailAuthError, MailError, MailResult, ingest_mail, search_agent_warning, zero_job_warning
from .report import ReportAuthError, ReportError, send_report, send_update
from .results import ImportResult, process_results_dir, refresh_selection

log = logging.getLogger("recruitment")


class RunLockError(Exception):
    """Another run holds the lock."""


class Interrupted(BaseException):
    """A stop signal (SIGTERM: a container stop, restart or redeploy, or the scheduler's step limit) arrived while this
    process held the run lock. A BaseException on purpose: the `except Exception` of the steps must not swallow it, so the
    lock is released and `prepare` marks its run failed on the way out."""


LOCK_TIME = "%Y-%m-%dT%H:%M:%SZ"


def _pidns() -> str | None:
    """The PID namespace of this process (Linux), None where it cannot be read (Windows, no /proc). A pid number means
    something only inside one namespace: another container has its own."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except (OSError, AttributeError):
        return None


def _in_scheduler() -> bool:
    """True when this process runs in the scheduler container (PID 1 is `python -m app schedule`); False on any error."""
    try:
        with open("/proc/1/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", "replace")
    except (OSError, AttributeError):
        return False
    return "app" in cmd and "schedule" in cmd


def _pid_alive(pid) -> bool:
    """False only when the process is known to be gone. Never calls os.kill on Windows (there it would end the process)."""
    if os.name == "nt" or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:                                          # PermissionError: it exists, it is not ours
        return True
    return True


def _read_lock(path) -> dict | None:
    """The lock token, or None when the file cannot be read as one (empty, half-written, not an object, no valid start)."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        datetime.strptime(info["started"], LOCK_TIME)
        return info
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _left_over(info: dict, now: datetime, cfg: dict, startup: bool = False) -> str | None:
    """Why a readable lock is left over from a crash, or None while its run may be alive: it is older than
    `monitoring.lock_stale_hours`, or it was taken in this PID namespace by a process that is gone."""
    started = datetime.strptime(info["started"], LOCK_TIME).replace(tzinfo=timezone.utc)
    hours = cfg["monitoring"]["lock_stale_hours"]
    if (now - started).total_seconds() > hours * 3600:
        return f"older than {hours} hours"
    mine = _pidns()
    if mine is not None and info.get("pidns") == mine and not _pid_alive(info.get("pid")):
        return f"its process (pid {info.get('pid')}) is gone"
    if startup and mine is not None and info.get("scheduler") is True and info.get("pidns") and info.get("pidns") != mine:
        return "it was taken by an earlier start of the scheduler container (another PID namespace)"   # a restart kills every exec'd process
    return None


def lock_status(cfg: dict, now: datetime | None = None, startup: bool = False) -> dict:
    """The run lock as /health and the watchdog show it: `state` is free, held, left_over or unreadable, with the start,
    the pid and the reason. Reads only; takes no lock."""
    now = now or datetime.now(timezone.utc)
    path = resolve_path(cfg["paths"]["lock"])
    if not path.exists():
        return {"state": "free", "started": None, "pid": None, "reason": None}
    info = _read_lock(path)
    if info is None:
        return {"state": "unreadable", "started": None, "pid": None, "reason": "the lock file cannot be read"}
    reason = _left_over(info, now, cfg, startup)
    if reason:
        return {"state": "left_over", "started": info["started"], "pid": info.get("pid"), "reason": reason}
    mine = _pidns()
    checkable = mine is not None and info.get("pidns") == mine   # the pid is only ever checked inside one known PID namespace
    return {"state": "held", "started": info["started"], "pid": info.get("pid"), "reason": None,
            "pid_checkable": checkable}


def _write_lock(path, token: dict) -> None:
    """Create the lock with its content in one step: write a private file, then hard-link it to the lock name (fails when the
    lock exists), so the lock file is never seen half-written. A share without hard links gets the plain exclusive create."""
    payload = json.dumps(token)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.link(tmp, path)
        return
    except FileExistsError:
        raise RunLockError(f"another run took the lock first ({path})") from None
    except OSError:
        pass
    finally:
        tmp.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RunLockError(f"another run took the lock first ({path})") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)


def _remove_stale(path, seen: dict | None) -> None:
    """Remove the lock that was judged left over. Rename it to a private name first and read it again: when another run
    replaced it meanwhile (the content differs from what was judged), put it back and refuse instead of deleting a live lock."""
    grave = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.stale")
    try:
        os.replace(path, grave)
    except FileNotFoundError:
        return
    handled = False
    try:
        if _read_lock(grave) != seen:
            _put_back(grave, path)
            handled = True
            raise RunLockError(f"another run took the lock first ({path})")
        grave.unlink(missing_ok=True)
        handled = True
    finally:
        if not handled:                                      # an Interrupted between the rename and the end: never drop a lock
            _put_back(grave, path)


def _put_back(grave, path) -> None:
    """Return a renamed lock to its name without overwriting: a hard link fails when the name exists again."""
    try:
        os.link(grave, path)
    except FileExistsError:
        log.error("a run lock was renamed aside and a new one exists at %s; the displaced lock is dropped", path)
    except OSError:                                          # no hard links on this share: the plain check
        if not path.exists():
            os.replace(grave, path)
            return
        log.error("a run lock was renamed aside and a new one exists at %s; the displaced lock is dropped", path)
    grave.unlink(missing_ok=True)


def _on_sigterm(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)            # a second signal during the clean-up changes nothing
    raise Interrupted()


@contextmanager
def run_lock(cfg: dict, now: datetime | None = None, startup: bool = False):
    """Exclusive lock file. A lock is a leftover and is replaced when it is older than `monitoring.lock_stale_hours`, when
    its process is gone (same PID namespace), or when it cannot be read. While the lock is held (main thread), SIGTERM
    raises `Interrupted`, so the lock is removed and the caller can record the stop."""
    now = now or datetime.now(timezone.utc)
    path = resolve_path(cfg["paths"]["lock"])
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if threading.current_thread() is threading.main_thread():
        try:
            previous = signal.signal(signal.SIGTERM, _on_sigterm)
        except (ValueError, OSError):
            previous = None
    try:
        if path.exists():
            info = _read_lock(path)
            reason = "it cannot be read" if info is None else _left_over(info, now, cfg, startup)
            if reason is None:
                raise RunLockError(f"another run is in progress (lock {path}, started {info.get('started')}, pid {info.get('pid')})")
            log.warning("replacing a left-over run lock (%s): %s", path, reason)
            _remove_stale(path, info)
        token = {"pid": os.getpid(), "started": now.strftime(LOCK_TIME), "pidns": _pidns()}
        if _in_scheduler():
            token["scheduler"] = True                        # only a lock of the scheduler container is freed at its restart
        _write_lock(path, token)
        ok = False
        late = []
        try:
            yield token
            ok = True
        finally:
            # a SIGTERM arriving during the release must not leave the lock: defer it, release, then raise it
            try:
                if previous is not None:
                    try:
                        signal.signal(signal.SIGTERM, lambda signum, frame: late.append(signum))
                    except (ValueError, OSError):
                        pass
            finally:
                try:
                    if json.loads(path.read_text(encoding="utf-8")) == token:
                        path.unlink()
                except (OSError, ValueError):
                    pass
        if late and ok:
            raise Interrupted()
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def lock_is_held(cfg: dict, now: datetime | None = None) -> bool:
    """True while a pipeline run holds a fresh lock. Reads the lock file with the rules of `run_lock` and takes
    no lock (the web paste page refuses while a run is in progress, v0.72.0)."""
    now = now or datetime.now(timezone.utc)
    path = resolve_path(cfg["paths"]["lock"])
    if not path.exists():
        return False
    info = _read_lock(path)
    if info is None:
        return True                                          # an existing lock file that cannot be read counts as held (a run may be writing it)
    return _left_over(info, now, cfg) is None


@dataclass
class PrepareResult:
    run_id: str
    mail: MailResult | None = None          # the UBS mails (kept for the callers that only know UBS)
    mails: dict[str, MailResult] = field(default_factory=dict)   # every source that could be read, by source key
    mail_error: str | None = None
    mail_login: bool | None = None          # True: the IMAP login worked; False: Gmail refused it; None: not known
    filters: dict[str, int] = field(default_factory=dict)
    batch: BatchInfo | None = None
    warnings: list[str] = field(default_factory=list)


def _counts(values) -> str:
    """`success:3,no_match:1` (sorted), `-` when there is nothing: the count per outcome or per tier for the log."""
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return ",".join(f"{k}:{n}" for k, n in sorted(counts.items())) or "-"


def _record_adapter(conn, run_id: str, name: str, found: int, new: int, error: str | None, warning: str | None) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO adapter_runs (run_id, adapter, found, new_jobs, error, warning) VALUES (?, ?, ?, ?, ?, ?)",
                     (run_id, name, found, new, error, warning))


def prepare(conn, cfg: dict, *, providers, prompt: tuple[str, str], mail_client_factory=mailbox.connect,
            page_fetcher=None, page_sleep=time.sleep, run_id: str | None = None, now: datetime | None = None) -> PrepareResult:
    """Ingest mail, filter, write the batch. `prompt` is (text, version) from `load_prompt`."""
    now = now or datetime.now(timezone.utc)
    run_id = run_id or new_run_id(now)
    result = PrepareResult(run_id)
    with run_lock(cfg, now):
        db.start_run(conn, run_id)
        try:
            _prepare_steps(conn, cfg, result, run_id, now, providers=providers, prompt=prompt,
                           mail_client_factory=mail_client_factory, page_fetcher=page_fetcher, page_sleep=page_sleep)
        except BaseException as exc:                           # a stop signal, Ctrl+C or a crash: the run must not stay `running`
            _mark_run_failed(conn, run_id, exc)
            raise
    return result


def _mark_run_failed(conn, run_id: str, exc: BaseException) -> None:
    """The run row of a prepare that ended without finishing. A database error here is logged, never raised."""
    error = ("interrupted by a stop signal" if isinstance(exc, Interrupted) else
             "interrupted (Ctrl+C)" if isinstance(exc, KeyboardInterrupt) else type(exc).__name__)
    try:
        with conn:
            conn.execute("UPDATE runs SET finished_at = ?, status = 'failed', summary = ? WHERE id = ? AND status = 'running'",
                         (db.utcnow(), json.dumps({"error": error}), run_id))
    except sqlite3.Error as e:
        log.error("run %s: the failed run could not be recorded: %s", run_id, type(e).__name__)


def _prepare_steps(conn, cfg: dict, result: PrepareResult, run_id: str, now: datetime, *, providers, prompt: tuple[str, str],
                   mail_client_factory, page_fetcher, page_sleep) -> None:
    log.info("run %s: prepare started", run_id)
    client = None
    source_errors: dict[str, str] = {}                     # a folder that cannot be opened, by source key
    try:
        client = mail_client_factory(cfg)
        result.mail_login = True
        for source in mailbox.SOURCES:                     # one source failing never stops the others
            try:
                result.mails[source.key] = ingest_mail(conn, cfg, client, run_id, now, source)
            except MailError as exc:
                source_errors[source.key] = str(exc)
                log.error("run %s: mail step failed for %s: %s", run_id, source.name, exc)
    except MailError as exc:
        result.mail_error = str(exc)
        if isinstance(exc, MailAuthError):
            result.mail_login = False
        log.error("run %s: mail step failed: %s", run_id, exc)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:                              # a failed logout must not hide the real result
                pass
    result.mail = result.mails.get(mailbox.UBS.key)
    if source_errors:
        only_one = len(source_errors) == 1
        result.mail_error = "; ".join(msg if only_one else f"{name}: {msg}" for name, msg in
                                      ((s.name, source_errors[s.key]) for s in mailbox.SOURCES if s.key in source_errors))
    _record_mail_rows(conn, cfg, result, run_id, now, source_errors)

    _adapter_list_step(conn, cfg, result, run_id, page_fetcher, page_sleep)
    if cfg["descriptions"]["fetch_enabled"]:
        _fetch_step(conn, cfg, result, run_id, page_fetcher, now, page_sleep)
    if cfg["resolver"]["enabled"]:
        _resolver_step(conn, cfg, result, run_id, page_fetcher, now, page_sleep)
    result.filters = filter_new_jobs(conn, cfg, run_id, providers=providers)
    _adapter_detail_step(conn, cfg, result, run_id, page_fetcher, now, page_sleep)
    if cfg["resolver"]["enabled"] and cfg["resolver"]["discovery"]["enabled"]:
        _discovery_step(conn, cfg, result, run_id, page_fetcher, now, page_sleep)
    result.batch = build_batch(conn, cfg, prompt[0], prompt[1], run_id=run_id, now=now)
    held = result.batch.held_back if result.batch else held_back_ids(conn, exchange_dir(cfg))
    if held:
        result.warnings.append(f"{len(held)} job(s) held back: their scores wait in exchange/results; run finish")
    _record_alerts(conn, cfg, result, now)
    summary = {"mail_error": result.mail_error, "filters": result.filters, "warnings": result.warnings,
               "batch_jobs": result.batch.job_count if result.batch else 0}
    with conn:
        conn.execute("UPDATE runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
                     (db.utcnow(), "failed" if result.mail_error else "ok", json.dumps(summary), run_id))
    log.info("run %s: prepare done: %s", run_id, summary)


def _record_mail_rows(conn, cfg: dict, result: PrepareResult, run_id: str, now: datetime, source_errors: dict[str, str]) -> None:
    """Zero-job and expiry warnings per source, and one row per source in the run statistics. UBS always has a row;
    another source has one when its folder gave mails or a problem (a quiet day adds no line to the report)."""
    n = cfg["monitoring"]["zero_jobs_alert_after"]
    for source in mailbox.SOURCES:
        found = [zero_job_warning(conn, source.key, n)]
        if source.service_notices:
            found.append(search_agent_warning(conn, source.key, now))
        found = [w for w in found if w]
        result.warnings += found
        mail = result.mails.get(source.key)
        error = source_errors.get(source.key)
        if error is None and mail and mail.mails_failed:
            error = "; ".join(mail.errors[:3])
        if source is mailbox.UBS:
            error = result.mail_error if result.mail is None and not source_errors else error
        elif not ((mail and mail.mails_seen) or error or found):
            continue
        _record_adapter(conn, run_id, source.adapter_label, mail.found if mail else 0, mail.new_jobs if mail else 0,
                        error, "; ".join(found) or None)


def _record_alerts(conn, cfg: dict, result: PrepareResult, now: datetime) -> None:
    """Note the conditions this run showed (goal.md 5.9). The mails are sent later by the command. Never fails the run."""
    try:
        if result.mail_login is False:
            alerts.observe(conn, cfg, alerts.IMAP_AUTH_KEY, f"Gmail refused the IMAP login or the app password is missing: {result.mail_error}", now)
        elif result.mail_login:
            alerts.clear(conn, alerts.IMAP_AUTH_KEY, now)
        alerts.check_sources(conn, cfg, mail_read=set(result.mails), now=now)
    except Exception as exc:
        log.error("run %s: alerts could not be recorded: %s", result.run_id, type(exc).__name__)


def _fetch_step(conn, cfg: dict, result: PrepareResult, run_id: str, page_fetcher, now: datetime, sleep=time.sleep) -> None:
    """Fetch the descriptions of jobs that have none (only when the user switched it on). Never fails the run."""
    try:
        fetcher = page_fetcher or descriptions_fetch.PageFetcher(cfg["descriptions"]["user_agent"], cfg["descriptions"]["timeout_seconds"],
                                                                      descriptions_fetch.ALLOWED_HOST)
        summary = descriptions_fetch.fetch_descriptions(conn, cfg, fetcher, sleep=sleep, run_id=run_id, now=now)
        error = summary.stopped or (f"{summary.failed} page(s) not fetched" if summary.failed else None)
        _record_adapter(conn, run_id, "UBS job pages", summary.fetched, 0, error, None)
        if summary.stopped:
            result.warnings.append(summary.stopped)
        log.info("run %s: descriptions fetched %d, failed %d", run_id, summary.fetched, summary.failed)
    except descriptions_fetch.FetchDisabled as exc:
        result.warnings.append(str(exc))
    except Exception as exc:                              # a problem here must not stop the batch
        result.warnings.append(f"fetching descriptions failed: {type(exc).__name__}")
        log.error("run %s: description fetch failed: %s", run_id, type(exc).__name__)


def _resolver_step(conn, cfg: dict, result: PrepareResult, run_id: str, page_fetcher, now: datetime, sleep=time.sleep) -> None:
    """Look for the description of jobs that have none on the employers' career sites (goal.md 5.1a). Never fails the run."""
    try:
        rc = cfg["resolver"]
        fetcher = page_fetcher or descriptions_fetch.PageFetcher(resolver.user_agent(cfg), rc["timeout_seconds"])
        log.info("event=resolver_start sources=%d accept_t2_risk=%s daily_cap_per_host=%s", len(rc["sources"]), rc["accept_t2_risk"],
                 rc["daily_cap_per_host"])
        began = time.monotonic()
        summary = resolver.resolve_descriptions(conn, cfg, fetcher, sleep=sleep, now=now, run_id=run_id)
        failed = [o for o in summary.outcomes if o.status in ("error", "refused")]
        error = summary.stopped or (f"{len(failed)} job(s) not resolved because of an error or a refusal" if failed else None)
        warning = resolver.run_warning(summary, rc["max_attempt_days"])
        _record_adapter(conn, run_id, "Description resolver", summary.resolved, 0, error, warning)
        if summary.stopped:
            result.warnings.append(summary.stopped)
        if warning:
            result.warnings.append(warning)
        log.info("run %s: descriptions resolved %d of %d jobs", run_id, summary.resolved, summary.attempted)
        log.info("event=resolver_end resolved=%d attempted=%d outcomes=%s tiers=%s stopped=%s duration_s=%.1f", summary.resolved,
                 summary.attempted, _counts(o.status for o in summary.outcomes), _counts(o.tier for o in summary.outcomes),
                 " ".join(summary.stopped.split())[:200].replace(" ", "_") if summary.stopped else "-", time.monotonic() - began)
    except Exception as exc:                              # a problem here must not stop the batch
        result.warnings.append(f"resolving descriptions failed: {type(exc).__name__}")
        log.error("run %s: description resolver failed: %s", run_id, type(exc).__name__)


def _discovery_step(conn, cfg: dict, result: PrepareResult, run_id: str, page_fetcher, now: datetime, sleep=time.sleep) -> None:
    """Look for the same job on public job boards, after the filters (goal.md 5.1a (a), v0.73.0). Never fails the run."""
    try:
        fetcher = page_fetcher or descriptions_fetch.PageFetcher(resolver.user_agent(cfg), cfg["resolver"]["timeout_seconds"])
        summary = discovery.discover_descriptions(conn, cfg, fetcher, sleep=sleep, now=now, run_id=run_id)
        failed = [o for o in summary.outcomes if o.status in ("error", "refused")]
        parts = [summary.stopped, f"{len(failed)} job(s) not looked up because of an error or a refusal" if failed else None, *summary.refusals]
        error = "; ".join(p for p in parts if p) or None
        _record_adapter(conn, run_id, "Description discovery", summary.resolved, 0, error, None)
        if summary.stopped:
            result.warnings.append(summary.stopped)
        log.info("run %s: discovery found %d of %d jobs", run_id, summary.resolved, summary.attempted)
    except Exception as exc:                              # a problem here must not stop the batch
        result.warnings.append(f"description discovery failed: {type(exc).__name__}")
        log.error("run %s: description discovery failed: %s", run_id, type(exc).__name__)


def _adapter_fetcher(cfg: dict, page_fetcher):
    a = cfg["adapters"]
    return page_fetcher or descriptions_fetch.PageFetcher(a["user_agent"], a["timeout_seconds"])


def _adapter_list_step(conn, cfg: dict, result: PrepareResult, run_id: str, page_fetcher, sleep=time.sleep) -> None:
    """Read the job lists of the employer sites the user switched on. A site that fails never stops the run."""
    for spec in adapters.enabled_adapters(cfg):
        try:
            summary, _ = adapters.fetch_list(conn, cfg, spec, _adapter_fetcher(cfg, page_fetcher), sleep=sleep, run_id=run_id)
        except Exception as exc:
            summary = adapters.ListSummary(error=f"{spec.label}: failed ({type(exc).__name__})")
            log.error("run %s: adapter %s failed: %s", run_id, spec.key, type(exc).__name__)
        _record_adapter(conn, run_id, spec.label, summary.found, summary.new, summary.error, None)
        if summary.error:
            result.warnings.append(summary.error)
        log.info("run %s: %s: %d listed, %d new, %d closed, %d listed again", run_id, spec.label, summary.found, summary.new,
                 summary.closed, summary.reopened)


def _adapter_detail_step(conn, cfg: dict, result: PrepareResult, run_id: str, page_fetcher, now: datetime, sleep=time.sleep) -> None:
    """Fetch the job pages of listed jobs that passed the filters, so the language rule and the scorer see the text."""
    for spec in adapters.enabled_adapters(cfg):
        try:
            summary = adapters.fetch_details(conn, cfg, spec, _adapter_fetcher(cfg, page_fetcher), sleep=sleep, run_id=run_id, now=now)
        except Exception as exc:
            problem = f"{spec.label}: fetching job pages failed ({type(exc).__name__})"
            log.error("run %s: %s", run_id, problem)
        else:
            problem = summary.stopped or (f"{spec.label}: {summary.failed} job page(s) not fetched" if summary.failed else None)
            log.info("run %s: %s job pages fetched %d, failed %d", run_id, spec.label, summary.fetched, summary.failed)
        if problem:
            result.warnings.append(problem)
            with conn:
                conn.execute("UPDATE adapter_runs SET warning = ? WHERE run_id = ? AND adapter = ?", (problem, run_id, spec.label))


@dataclass
class FinishResult:
    imports: list[ImportResult] = field(default_factory=list)
    reports: dict[str, str] = field(default_factory=dict)       # batch id -> "sent", "already_sent" or an error text
    auth_failed: set[str] = field(default_factory=set)          # batch ids whose report failed because Gmail refused the SMTP login
    postponed: dict[str, tuple[str, list[int]]] = field(default_factory=dict)   # batch id -> (label, job ids) still without a score
    updates: dict[str, list[str]] = field(default_factory=dict)  # batch id -> outcome per update mail (a file after the report)


def report_extra(conn, batch_id: str) -> dict:
    """Run statistics for the report, from the adapter rows of the batch's run."""
    rows = conn.execute("SELECT * FROM adapter_runs WHERE run_id = ? ORDER BY id", (batch_id,)).fetchall()
    extra: dict = {"adapters": []}
    if rows:
        extra["seen"] = sum(r["found"] for r in rows)
        extra["new"] = sum(r["new_jobs"] for r in rows)
    for r in rows:
        extra["adapters"].append({"name": r["adapter"], "found": r["found"], "new": r["new_jobs"], "error": r["error"], "warning": r["warning"]})
    return extra


def review_lines(done: dict | None, batch_id: str | None = None, batches: list[str] | None = None) -> list[str]:
    """The lines of the headless review for the report (plain text; the HTML report escapes them). With `batch_id` and the
    `batches` that get a report, only the parts whose run_id is that batch; parts that match no batch go to the first report."""
    if not done:
        return []
    lines = []
    for part in done_parts(done):
        if batch_id is not None and batches:
            if part.get("run_id") in batches:
                if part.get("run_id") != batch_id:
                    continue
            elif batch_id != batches[0]:
                continue
        lines += _review_lines_of(part)
    return lines


def done_parts(done: dict | None) -> list[dict]:
    """The done file and the runs it resumed: a resumed run's reviewer output sits under `resumed` (v0.78.9)."""
    if not done:
        return []
    resumed = done.get("resumed")
    return [done] + [r for r in (resumed if isinstance(resumed, list) else []) if isinstance(r, dict)]


def _review_lines_of(done: dict) -> list[str]:
    lines = []
    dropped = [d for d in done.get("dropped") or [] if isinstance(d, dict)]
    if dropped:
        shown = ", ".join(f"job {d.get('job_id')} ({d.get('stage')}, {d.get('path')})" for d in dropped[:20])
        more = int(done.get("dropped_more") or 0) + max(0, len(dropped) - 20)
        lines.append(f"{len(dropped) + int(done.get('dropped_more') or 0)} documents entries dropped by the headless checks, their documents are "
                     f"missing (job, stage, field): {shown}" + (f" and {more} more" if more else ""))
    if not (done.get("review_summary") or done.get("review_file")):
        return lines
    if done.get("review_summary"):
        lines.append("Review by the headless doc-reviewer: " + " ".join(str(done["review_summary"]).split()))
    if done.get("review_file"):
        lines.append(f"Full review stored in exchange/{done['review_file']}")
    return lines


def finish(conn, cfg: dict, *, smtp_factory=smtplib.SMTP, sleep=time.sleep, now: datetime | None = None,
           limit_message: str | None = None, headless_done: dict | None = None) -> FinishResult:
    """Import every results file (a run's chunk files first), record the postponed jobs, then send the report for each
    newly imported batch and retry failed reports. A file for a batch whose report was sent before gets a short update
    mail with the jobs it scored and the documents it stored (a failed update mail raises an alert, see the CLI). `limit_message` is the text Claude Code showed at the usage limit (app/postpone.py)."""
    out = FinishResult()
    if headless_done and not limit_message:
        limit_message = headless_done.get("limit_message") or None        # the text of the headless run (D1 regex unchanged)
    with run_lock(cfg, now):
        reported = {row["batch_id"] for row in conn.execute("SELECT batch_id FROM reports WHERE status = 'sent'")}
        out.imports = process_results_dir(conn, cfg, now)
        attention = [i for part in done_parts(headless_done) if isinstance(part.get("attention"), list) for i in part["attention"]]
        for item in attention:                                           # the reviewer's flags; the document's status does not change
            if isinstance(item, dict) and isinstance(item.get("job_id"), int) and isinstance(item.get("kind"), str)                     and isinstance(item.get("note"), str):
                documents.add_review_attention(conn, item["job_id"], item["kind"], item["note"][:200])
        refresh_selection(conn, cfg)                     # a job archived since the import leaves no empty place in a top 10
        imported = [r.batch_id for r in out.imports if r.status == "imported" and r.batch_id]
        errored = {j for r in out.imports for j, _ in r.errors}
        out.postponed = postpone.record(conn, imported, errored, now, limit_message)
        for r in out.imports:
            if r.status == "imported" and r.batch_id in reported and (r.scored or r.documents):
                try:
                    out.updates.setdefault(r.batch_id, []).append(
                        send_update(conn, cfg, r.batch_id, r.scored, smtp_factory=smtp_factory, sleep=sleep,
                                    documents=r.documents))
                except ReportError as exc:
                    out.updates.setdefault(r.batch_id, []).append(f"not sent: {exc}")
                    log.error("update mail for %s not sent: %s", r.batch_id, exc)
        for r in out.imports:
            log.info("event=import file=%s batch=%s status=%s scored=%d skipped=%d errors=%d missing=%d warnings=%d documents=%d documents_missing=%d",
                     r.file_name or "-", r.batch_id or "-", r.status, len(r.scored), len(r.skipped), len(r.errors), len(r.missing),
                     len(r.warnings), len(r.documents), sum(len(kinds) for _, kinds in r.documents_missing))
        if headless_done:
            for d in (headless_done.get("dropped") or [])[:50]:
                if isinstance(d, dict):
                    log.info("event=dropped job=%s stage=%s path=%s", d.get("job_id"), d.get("stage"), d.get("path"))
        log.info("event=run_summary files=%d scored=%d postponed=%d documents_written=%d documents_missing=%d dropped=%d",
                 len(out.imports), sum(len(r.scored) for r in out.imports), sum(len(ids) for _, ids in out.postponed.values()),
                 sum(len(r.documents) for r in out.imports), sum(len(kinds) for r in out.imports for _, kinds in r.documents_missing),
                 len((headless_done or {}).get("dropped") or []) + int((headless_done or {}).get("dropped_more") or 0))
        batches = [r.batch_id for r in out.imports if r.status == "imported" and r.batch_id]
        batches += [row["batch_id"] for row in conn.execute("SELECT batch_id FROM reports WHERE status = 'failed'")]
        for batch_id in dict.fromkeys(batches):
            try:
                extra = report_extra(conn, batch_id)
                extra["review_lines"] = review_lines(headless_done, batch_id, batches)
                out.reports[batch_id] = send_report(conn, cfg, batch_id, extra,
                                                    smtp_factory=smtp_factory, sleep=sleep, now=now)
                if out.reports[batch_id] == "sent":
                    alerts.clear(conn, alerts.SMTP_AUTH_KEY, now)     # the SMTP login works
            except ReportError as exc:
                out.reports[batch_id] = f"not sent: {exc}"
                log.error("report for %s not sent: %s", batch_id, exc)
                if isinstance(exc, ReportAuthError):
                    out.auth_failed.add(batch_id)
                    alerts.observe(conn, cfg, alerts.SMTP_AUTH_KEY, f"Gmail SMTP login failed while sending the report: {exc.detail}", now)
    return out


def status(conn, cfg: dict, now: datetime | None = None) -> dict:
    """A snapshot for the `status` command (the Health page later)."""
    jobs = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
    last_run = conn.execute("SELECT id, status, finished_at, summary FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
    last_report = conn.execute("SELECT batch_id, status, attempts, error FROM reports ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    results_dir = resolve_path(cfg["paths"]["exchange"]) / "results"
    return {
        "schema_version": db.schema_version(conn),
        "jobs_by_status": jobs,
        "archived": conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0],
        "closed_jobs": conn.execute("SELECT COUNT(*) FROM jobs WHERE closed_at IS NOT NULL").fetchone()[0],
        "oldest_awaiting_days": oldest_awaiting_age_days(conn, now),
        "last_run": dict(last_run) if last_run else None,
        "last_report": dict(last_report) if last_report else None,
        "postponed_open": postpone.open_counts(conn),
        "results_files_waiting": len(list(results_dir.glob("*.json"))) if results_dir.exists() else 0,
        "mail_warning": zero_job_warning(conn, "ubs_alert", cfg["monitoring"]["zero_jobs_alert_after"]),
        "linkedin_mail_warning": zero_job_warning(conn, "linkedin_alert", cfg["monitoring"]["zero_jobs_alert_after"]),
        "julius_mail_warning": zero_job_warning(conn, "julius_alert", cfg["monitoring"]["zero_jobs_alert_after"]),
        "jobsch_mail_warning": zero_job_warning(conn, "jobsch_alert", cfg["monitoring"]["zero_jobs_alert_after"]),
        "search_agent_warning": search_agent_warning(conn, "ubs_alert", now),
        "alerts_enabled": cfg["monitoring"]["alerts"],
        "open_alerts": alerts.open_alerts(conn),
        "web_supervisor": websuper.read_state(cfg),          # None when `web --supervise` never ran here (v0.77.0)
    }
