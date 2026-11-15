"""Ask the writer to fix: the recruitment side of a revision request (goal.md 5.10a, v0.85.0).

The web app only writes a `revision_requests` row (status `pending`): it has no exchange mount. `python -m app revisions` (one step
of the scheduler, under the run lock) does the rest, in this order:

1. `expire`: a relayed request without a done file after its deadline gets the cancel marker and a synthetic `timeout` done file,
   created exclusively, exactly as the scheduler does for the daily request;
2. `finish_done`: every `exchange/llm/done/rev-<id>.json` is stored (`documents.store_revision`), failed or marked stale, the user gets a
   mail, and the done file is renamed `.processed`. A late file (`rev-<id>.late.json`) is never stored (decided 12 Nov 2026, D9);
3. `relay` (not in the quiet window before the daily run): the next pending request is checked for staleness, the home address is
   scrubbed from every string of it (`documents.scrub_address`), a guard refuses to write the file when any trace is left, the row
   becomes `relayed` and is committed, and only then is `requests/rev-<id>.json` written. A crash between the two ends as a timeout,
   never as a second call for one press. One request is in flight at a time, so a deadline never runs while another one is working.

No retry anywhere: a new press is a new go. The home address never reaches a file, a log line or the llm container.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import alerts, db, documents, headless, report
from .config import resolve_path

log = logging.getLogger("recruitment")

OVERDUE_GRACE_MINUTES = 5                       # after the deadline of the request: the worker's margin, then the synthetic timeout
STAMP = "%Y-%m-%dT%H:%M:%SZ"
DESCRIPTION_MAX = 20000
FIELD_MAX = 300
NO_ADDRESS_LEFT = "the address could not be removed; nothing was sent"


@dataclass
class Result:
    lines: list[str] = field(default_factory=list)
    relayed: int = 0
    finished: int = 0
    auth: str | None = None                      # the message when a revision ended `auth`: the caller observes `headless_auth`
    auth_run: str = ""                           # the run id (rev-<id>) of that revision


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime(STAMP)


def _llm(cfg: dict):
    return resolve_path(cfg["paths"]["exchange"]) / "llm"


def _done_files(cfg: dict) -> list:
    folder = _llm(cfg) / "done"
    return sorted(folder.glob("rev-*.json")) if folder.is_dir() else []


def has_work(cfg: dict, now: datetime | None = None, relay: bool = True) -> bool:
    """A cheap read-only look for the scheduler tick: a done file waits, a relayed request is overdue, or (with `relay`) a request is
    pending and none is in flight (one at a time). A missing database or table counts as no work."""
    if _done_files(cfg):
        return True
    path = resolve_path(cfg["paths"]["database"])
    if not path.is_file():
        return False
    limit = _stamp((now or datetime.now(timezone.utc)) - timedelta(minutes=OVERDUE_GRACE_MINUTES))
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return False
    try:
        sql = "SELECT 1 FROM revision_requests WHERE (status = 'relayed' AND deadline < ?)" + (" OR (status = 'pending' AND NOT EXISTS (SELECT 1 FROM revision_requests WHERE status = 'relayed'))" if relay else "") + " LIMIT 1"
        return conn.execute(sql, (limit,)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


# ----------------------------------------------------------------------------- finishing

def expire(conn, cfg: dict, now: datetime) -> list[str]:
    """Overdue relayed requests without a done file: cancel marker and a synthetic timeout done file; `finish_done` fails the row."""
    out: list[str] = []
    limit = _stamp(now - timedelta(minutes=OVERDUE_GRACE_MINUTES))
    llm = _llm(cfg)
    for row in conn.execute("SELECT id FROM revision_requests WHERE status = 'relayed' AND deadline < ? ORDER BY id", (limit,)).fetchall():
        run_id = f"rev-{row['id']}"
        done_path = llm / "done" / f"{run_id}.json"
        if done_path.exists():
            continue                                                  # the answer arrived: finish_done reads it
        marker = llm / "cancel" / run_id
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("cancel", encoding="ascii")
        done_path.parent.mkdir(parents=True, exist_ok=True)
        try:                                                          # exclusive: a done file that just arrived is not replaced
            with done_path.open("x", encoding="utf-8") as fh:
                json.dump({"run_id": run_id, "status": "timeout", "message": "the llm service did not answer in time"}, fh, indent=1)
        except FileExistsError:
            continue
        log.info("event=revision_timeout id=%s", row["id"])
        out.append(f"revision {row['id']}: no answer in time, cancelled")
    return out


def _processed(path) -> None:
    target = path.with_name(path.name + ".processed")
    n = 1
    while target.exists():                                             # never overwrite an earlier .processed
        target = path.with_name(f"{path.name}.processed.{n}")
        n += 1
    try:
        path.replace(target)
    except OSError as exc:
        log.warning("event=revision_done_not_renamed file=%s error=%s", path.name, type(exc).__name__)


def _mail(conn, cfg: dict, request_id: int, done: dict | None, smtp_factory, sleep) -> None:
    """One mail per terminal request; a failure is stored and logged, never retried in a loop, the status stays."""
    row = conn.execute("SELECT * FROM revision_requests WHERE id = ?", (request_id,)).fetchone()
    try:
        report.send_revision_mail(conn, cfg, row, done, smtp_factory, sleep)
    except report.ReportError as exc:
        error = (exc.detail or str(exc))[:300]
        with conn:
            conn.execute("UPDATE revision_requests SET mail_error = ? WHERE id = ?", (error, request_id))
        log.warning("event=revision_mail_failed id=%s", request_id)
        return
    with conn:
        conn.execute("UPDATE revision_requests SET mailed_at = ?, mail_error = NULL WHERE id = ?", (db.utcnow(), request_id))


def _fail(conn, request_id: int, outcome: str, message: str) -> None:
    with conn:
        conn.execute("UPDATE revision_requests SET status = 'failed', outcome = ?, message = ?, finished_at = ? "
                     "WHERE id = ? AND status IN ('pending', 'relayed')", (outcome, message[:FIELD_MAX], db.utcnow(), request_id))


def finish_done(conn, cfg: dict, result: Result, smtp_factory, sleep) -> None:
    for path in _done_files(cfg):
        name = path.name
        late = name.endswith(".late.json")
        try:
            request_id = int(name[len("rev-"):-len(".late.json" if late else ".json")])
        except ValueError:
            _processed(path)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            data = {"status": "error", "message": "the done file cannot be read"}
        row = conn.execute("SELECT * FROM revision_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None or late or row["status"] == "pending":
            log.info("event=%s id=%s", "revision_late" if late and row is not None else "revision_orphan", request_id)
            _processed(path)                                           # a late result is ignored (D9); a file without a request is noise
            continue
        if row["status"] == "relayed":
            try:
                _finish_one(conn, cfg, row, data, result)
            except Exception as exc:                                   # one broken file must not stop the others, nor loop forever
                log.error("event=revision_finish_failed id=%s error=%s", request_id, type(exc).__name__)
                if conn.in_transaction:
                    conn.rollback()
                _fail(conn, request_id, "error", f"storing the result crashed: {type(exc).__name__}")
            result.finished += 1
            _mail(conn, cfg, request_id, data, smtp_factory, sleep)
        elif row["mailed_at"] is None and row["mail_error"] is None:   # a repeated done file after a crash before the mail
            _mail(conn, cfg, request_id, data, smtp_factory, sleep)
        _processed(path)


def _finish_one(conn, cfg: dict, row, data: dict, result: Result) -> None:
    rid, status = row["id"], data.get("status")
    if status == "ok":
        doc = data.get("document")
        if data.get("content_sha") != row["content_sha"] or not isinstance(doc, dict):
            _fail(conn, rid, "invalid", "the answer does not belong to this request")
            result.lines.append(f"revision {rid}: failed (invalid answer)")
            return
        notes = [a["note"] for a in data.get("attention", []) if isinstance(a, dict) and a.get("kind") == row["kind"] and a.get("note")]
        out = documents.store_revision(conn, cfg, rid, doc, notes)
        result.lines.append(f"revision {rid}: {out.status}" + (f" ({out.outcome})" if out.outcome else "")
                            + (f", {len(out.findings)} finding(s)" if out.status == "stored" and out.outcome == "blocked" else ""))
        return
    outcome = status if status in ("usage_limit", "auth", "timeout", "error") else "error"
    message = " ".join(str(data.get("message") or data.get("limit_message") or "").split())
    _fail(conn, rid, outcome, message)
    result.lines.append(f"revision {rid}: failed ({outcome})")
    if outcome == "auth":
        result.auth = message or "the headless Claude login was refused"
        result.auth_run = f"rev-{rid}"


# ----------------------------------------------------------------------------- relaying

def _request(conn, cfg: dict, row, doc, now: datetime) -> dict:
    """The contract of the request file (goal.md 5.10a): the document, the findings not accepted, the job, the instruction, the roles.
    Nothing is cut here: the texts are cut after the address scrub (`_cut`), so a cut can never leave a piece of the address behind."""
    hl = cfg["headless"]
    rev = hl["revision"]
    kind = row["kind"]
    writer = headless.revision_writer(kind)
    overrides = documents.load_overrides(conn, doc["id"])
    findings = [{"kind": f["kind"], "where": f.get("where") or "", "text": f.get("text") or "",
                 "reason": f.get("reason") or "", "warning": f["kind"] in documents.SHOWN_AS_WARNING}
                for f in json.loads(doc["findings"])
                if not (documents.overridable(f) and documents.finding_key(f) in overrides)][:headless.REVISION_FINDINGS_MAX]
    locations = [r["location_text"] for r in conn.execute("SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (doc["job_id"],))]
    role = lambda name: {"model": hl["roles"][name]["model"], "effort": hl["roles"][name]["effort"],           # noqa: E731
                         "timeout_minutes": rev["timeout_minutes"], "max_turns": rev["max_turns"]}
    return {"kind": "revision", "run_id": f"rev-{row['id']}", "revision_id": row["id"],
            "deadline": (now + timedelta(minutes=rev["wait_minutes"])).astimezone(timezone.utc).isoformat(timespec="seconds"),
            "document_kind": kind, "content_sha": row["content_sha"], "instruction": row["instruction"], "findings": findings,
            "job": {"job_id": doc["job_id"], "title": doc["title"], "company": doc["company"], "locations": locations,
                    "description": doc["description"] or "",
                    "german_required": documents.german_required(conn, doc["job_id"], doc["title"], doc["description"], cfg)},
            "document": json.loads(doc["content"]), "roles": {writer: role(writer), "review": role("review")}}


def _cut(request: dict) -> dict:
    """The size limits of the contract, applied to the scrubbed request."""
    for f in request["findings"]:
        f["text"], f["reason"] = f["text"][:FIELD_MAX], f["reason"][:FIELD_MAX]
    request["job"]["description"] = request["job"]["description"][:DESCRIPTION_MAX]
    return request


def _address_left(request: dict, home: str) -> bool:
    """The guard after the scrub: any trace of the address or its street part, in a string of the request or in the serialised file."""
    if documents._address_findings(request, home) or documents.address_trace(json.dumps(request, ensure_ascii=False), home):
        return True
    return any(documents.address_trace(s, home) for s in documents._strings(request))


def relay(conn, cfg: dict, now: datetime, result: Result, smtp_factory, sleep) -> None:
    """The next pending request, when none is in flight."""
    if conn.execute("SELECT 1 FROM revision_requests WHERE status = 'relayed' LIMIT 1").fetchone():
        return
    row = conn.execute("SELECT * FROM revision_requests WHERE status = 'pending' ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return
    rid = row["id"]
    doc = conn.execute("SELECT d.id, d.job_id, d.status, d.content, d.findings, j.title, j.company, j.description FROM documents d "
                       "JOIN jobs j ON j.id = d.job_id WHERE d.id = ?", (row["document_id"],)).fetchone()
    if doc is None or not documents.revisable(doc) or documents.content_sha(doc["content"]) != row["content_sha"]:
        with conn:
            documents.finish_revision_request(conn, rid, "stale", "", "the document changed before the request was sent; nothing was sent", db.utcnow())
        result.lines.append(f"revision {rid}: stale, not sent")
        result.finished += 1
        _mail(conn, cfg, rid, None, smtp_factory, sleep)
        return
    home = cfg["location"].get("car_origin_address") or ""
    request, scrubbed = documents.scrub_address(_request(conn, cfg, row, doc, now), home)
    request = _cut(request)
    if _address_left(request, home):                                   # refuse rather than leak
        _fail(conn, rid, "invalid", NO_ADDRESS_LEFT)
        log.warning("event=revision_refused id=%s reason=address_trace", rid)
        result.lines.append(f"revision {rid}: failed (the address could not be removed; nothing was sent)")
        result.finished += 1
        _mail(conn, cfg, rid, None, smtp_factory, sleep)
        return
    with conn:                                                         # relayed first, then the file: a crash in between times out
        done = conn.execute("UPDATE revision_requests SET status = 'relayed', relayed_at = ?, deadline = ?, scrubbed = ? "
                            "WHERE id = ? AND status = 'pending'", (_stamp(now), _stamp(now + timedelta(minutes=cfg["headless"]["revision"]["wait_minutes"])),
                                                                    scrubbed, rid)).rowcount
    if not done:
        return
    from .scheduler import _write_json
    try:
        _write_json(_llm(cfg) / "requests" / f"rev-{rid}.json", request)
    except OSError as exc:
        _fail(conn, rid, "error", f"the request file could not be written ({type(exc).__name__})")
        result.lines.append(f"revision {rid}: failed (the request file could not be written)")
        result.finished += 1
        _mail(conn, cfg, rid, None, smtp_factory, sleep)
        return
    log.info("event=revision_relayed id=%s scrubbed=%s", rid, scrubbed)       # the id and the count only, never a text
    result.relayed += 1
    result.lines.append(f"revision {rid}: sent to the llm service" + (f" ({scrubbed} address span(s) removed)" if scrubbed else ""))


def run(conn, cfg: dict, *, relay_requests: bool = True, now: datetime | None = None, smtp_factory=None, sleep=time.sleep) -> Result:
    """expire, finish every done file, then (unless quiet) relay the next pending request. The caller holds the run lock."""
    import smtplib
    smtp_factory = smtp_factory or smtplib.SMTP
    now = now or datetime.now(timezone.utc)
    result = Result()
    result.lines += expire(conn, cfg, now)
    finish_done(conn, cfg, result, smtp_factory, sleep)
    if relay_requests:
        relay(conn, cfg, now, result, smtp_factory, sleep)
    if result.auth:
        alerts.observe_headless(conn, cfg, {"run_id": result.auth_run, "status": "auth", "message": result.auth})
        alerts.deliver(conn, cfg, smtp_factory=smtp_factory)
    return result
