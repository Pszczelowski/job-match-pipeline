"""run-report (v0.78.3, goal.md 5.9): one run on one screen, for pasting into chat.

Joins what the run left behind: the `runs` row, `adapter_runs`, the batch with its `imports`, `postponements` and `reports`, the
`alerts` of the run's window, the `description_resolutions` of the run's day and the headless done file
(exchange/llm/done/<run id>.json). The database is opened with `mode=ro` and `query_only`, never through `db.connect` (which
migrates), so the command is safe on the NAS while a run is going. No job or CV text is printed: no titles, no report subject, no
review text, no stderr tails. Every line passes `headless.redact`. A missing or unreadable done file is stated, not an error.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .headless import redact, redact_obj

TEXT_MAX = 300
LIST_MAX = 20
ALERT_WINDOW_DAYS = 1                                    # alerts count when last seen in [run start, run end + this many days]


class ReportError(Exception):
    pass


def open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ReportError("the database file does not exist yet")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _rows(conn, sql: str, args=()) -> list:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:                      # a table the older schema does not have yet
        return []


def _short(value, limit: int = TEXT_MAX):
    if value is None:
        return None
    return " ".join(str(value).split())[:limit]


def read_done(exchange: Path, run_id: str) -> tuple[dict | None, str]:
    """(done file as an object, its state): state is `ok`, `missing` or `unreadable`."""
    path = exchange / "llm" / "done" / f"{run_id}.json"
    if not path.is_file():
        return None, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unreadable"
    return (data, "ok") if isinstance(data, dict) else (None, "unreadable")


def _done_view(done: dict) -> dict:
    """Only counts, statuses, times and ids of the done file; the free texts (review summary, stderr) are left out."""
    roles = {}
    for name, role in (done.get("roles") or {}).items():
        if isinstance(role, dict):
            roles[str(name)] = {k: role.get(k) for k in ("status", "duration_s", "turns", "started", "ended", "reason") if role.get(k) is not None}
            if role.get("reason"):
                roles[str(name)]["reason"] = _short(role["reason"])
    dropped = [{"job_id": d.get("job_id"), "stage": d.get("stage"), "path": d.get("path")}
               for d in (done.get("dropped") or []) if isinstance(d, dict)]
    return {"status": done.get("status"), "message": _short(done.get("message")), "started": done.get("started"), "ended": done.get("ended"),
            "limit_message": _short(done.get("limit_message")), "roles": roles, "attention_count": len(done.get("attention") or []),
            "resumed": len(done.get("resumed") or []), "dropped": dropped[:LIST_MAX], "drops_total": len(dropped) + int(done.get("dropped_more") or 0),
            "dropped_more": max(0, len(dropped) - LIST_MAX) + int(done.get("dropped_more") or 0),
            "review_summary_chars": len(str(done.get("review_summary") or ""))}


def is_failed(row: sqlite3.Row, done: dict | None) -> bool:
    return row["status"] == "failed" or (done is not None and done.get("status") not in (None, "ok"))


def select_run(conn, exchange: Path, run_id: str | None, last: bool, last_failed: bool) -> sqlite3.Row:
    if run_id:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ReportError(f"no run {run_id!r} in the database")
        return row
    rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 200").fetchall()
    if not rows:
        raise ReportError("the database holds no run yet")
    if last:
        return rows[0]
    for row in rows:
        if is_failed(row, read_done(exchange, row["id"])[0]):
            return row
    raise ReportError("no failed run among the latest 200")


def collect(conn, exchange: Path, run: sqlite3.Row) -> dict:
    run_id = run["id"]
    day = str(run["started_at"] or "")[:10]
    done, done_state = read_done(exchange, run_id)
    out: dict = {"run_id": run_id, "status": run["status"], "started_at": run["started_at"], "finished_at": run["finished_at"],
                 "summary": _short(run["summary"]), "done_state": done_state,
                 "done_path": str(exchange / "llm" / "done" / f"{run_id}.json"), "done": _done_view(done) if done else None}
    out["adapters"] = [{"adapter": r["adapter"], "found": r["found"], "new_jobs": r["new_jobs"], "error": _short(r["error"]), "warning": _short(r["warning"])}
                       for r in _rows(conn, "SELECT * FROM adapter_runs WHERE run_id = ? ORDER BY adapter", (run_id,))]
    batch = _rows(conn, "SELECT * FROM batches WHERE id = ?", (run_id,))
    out["batch"] = ({"created_at": batch[0]["created_at"], "file_name": batch[0]["file_name"], "job_count": batch[0]["job_count"],
                     "fresh_count": batch[0]["fresh_count"]} if batch else None)
    out["imports"] = [{"file_name": r["file_name"], "status": r["status"], "reason": _short(r["reason"]), "summary": _short(r["summary"]),
                       "imported_at": r["imported_at"]}
                      for r in _rows(conn, "SELECT * FROM imports WHERE batch_id = ? ORDER BY imported_at, id", (run_id,))]
    out["postponements"] = {r["reason"]: r["n"] for r in _rows(
        conn, "SELECT reason, COUNT(*) AS n FROM postponements WHERE batch_id = ? GROUP BY reason ORDER BY reason", (run_id,))}
    out["reports"] = [{"status": r["status"], "attempts": r["attempts"], "error": _short(r["error"]), "created_at": r["created_at"], "sent_at": r["sent_at"]}
                      for r in _rows(conn, "SELECT * FROM reports WHERE batch_id = ?", (run_id,))]
    start = run["started_at"] or ""
    end = (run["finished_at"] or start)[:10]
    out["alerts"] = [{"key": r["key"], "first_seen": r["first_seen"], "last_seen": r["last_seen"], "resolved_at": r["resolved_at"],
                      "count": r["count"], "message": _short(r["message"])}
                     for r in _rows(conn, "SELECT * FROM alerts WHERE (message LIKE ? OR (last_seen >= ? AND first_seen <= date(?, ?))) "
                                          "ORDER BY first_seen, id LIMIT ?",
                                    (f"%{run_id}%", start, end, f"+{ALERT_WINDOW_DAYS + 1} day", LIST_MAX))]
    out["resolutions"] = {r["status"]: r["n"] for r in _rows(
        conn, "SELECT status, COUNT(*) AS n FROM description_resolutions WHERE day = ? GROUP BY status ORDER BY status", (day,))}
    out["resolutions_day"] = day
    out["timeline"] = _timeline(out)
    return redact_obj(out)


def _timeline(data: dict) -> list[dict]:
    events: list[tuple[str, str]] = []

    def add(stamp, text):
        if stamp:
            events.append((str(stamp), text))

    add(data["started_at"], "run started")
    add(data["finished_at"], f"run finished ({data['status']})")
    done = data["done"] or {}
    add(done.get("started"), "headless worker started")
    add(done.get("ended"), f"headless worker ended ({done.get('status')})")
    for name, role in (done.get("roles") or {}).items():
        add(role.get("started"), f"role {name} started")
        add(role.get("ended"), f"role {name} ended ({role.get('status')})")
    if data["batch"]:
        add(data["batch"]["created_at"], f"batch written ({data['batch']['job_count']} jobs)")
    for imp in data["imports"]:
        add(imp["imported_at"], f"import {imp['file_name']} {imp['status']}")
    for rep in data["reports"]:
        add(rep["created_at"], "report created")
        add(rep["sent_at"], "report sent")
    for alert in data["alerts"]:
        add(alert["first_seen"], f"alert {alert['key']} raised")
        add(alert["resolved_at"], f"alert {alert['key']} resolved")
    events.sort()
    return [{"at": at, "event": text} for at, text in events]


def render_text(data: dict) -> str:
    L: list[str] = [f"run {data['run_id']}  status {data['status']}  (all times UTC)"]
    if data["summary"]:
        L.append(f"summary: {data['summary']}")
    L.append("")
    L.append("timeline:")
    L += [f"  {e['at']}  {e['event']}" for e in data["timeline"]] or ["  (no timestamps)"]
    L.append("")
    L.append("sources:")
    if data["adapters"]:
        for a in data["adapters"]:
            note = "  ERROR " + a["error"] if a["error"] else ("  warning: " + a["warning"] if a["warning"] else "")
            L.append(f"  {a['adapter']}: found {a['found']}, new {a['new_jobs']}{note}")
    else:
        L.append("  (no source rows)")
    L.append("")
    batch = data["batch"]
    L.append("batch: " + (f"{batch['job_count']} jobs ({batch['fresh_count']} fresh), file {batch['file_name']}" if batch else "none"))
    for imp in data["imports"]:
        reason = f", reason: {imp['reason']}" if imp["reason"] else ""
        L.append(f"  import {imp['file_name']}: {imp['status']}{reason}" + (f"  {imp['summary']}" if imp["summary"] else ""))
    if data["postponements"]:
        L.append("  postponed: " + ", ".join(f"{k} {v}" for k, v in data["postponements"].items()))
    for rep in data["reports"]:
        L.append(f"  report mail: {rep['status']} after {rep['attempts']} attempt(s)" + (f", error: {rep['error']}" if rep["error"] else ""))
    L.append("")
    state = data["done_state"]
    if state != "ok":
        L.append(f"done file: {state} ({data['done_path']})")
    else:
        done = data["done"]
        L.append(f"done file: status {done['status']}" + (f", message: {done['message']}" if done["message"] else "") + f"  ({data['done_path']})")
        if done["limit_message"]:
            L.append(f"  limit message: {done['limit_message']}")
        for name, role in done["roles"].items():
            L.append(f"  role {name}: " + ", ".join(f"{k} {v}" for k, v in role.items()))
        L.append(f"  attention flags {done['attention_count']}, resumed runs {done['resumed']}, review summary {done['review_summary_chars']} chars (not printed)")
        L.append(f"drops: {done['drops_total']}")
        for d in done["dropped"]:
            L.append(f"  job {d['job_id']} at {d['stage']}: {d['path']}")
        if done["dropped_more"]:
            L.append(f"  and {done['dropped_more']} more")
    L.append("")
    L.append("warnings and alerts:")
    flagged = [f"  source {a['adapter']}: {a['warning']}" for a in data["adapters"] if a["warning"]]
    flagged += [f"  alert {a['key']} (x{a['count']}, first {a['first_seen']}, last {a['last_seen']}"
                + (f", resolved {a['resolved_at']}" if a["resolved_at"] else ", open") + f"): {a['message']}" for a in data["alerts"]]
    L += flagged or ["  none"]
    L.append("")
    res = data["resolutions"]
    L.append(f"description lookups on {data['resolutions_day']}: " + (", ".join(f"{k} {v}" for k, v in res.items()) if res else "none"))
    return redact("\n".join(L)) + "\n"


def run_report(conn, exchange: Path, run_id: str | None, last: bool, last_failed: bool, as_json: bool) -> str:
    data = collect(conn, exchange, select_run(conn, exchange, run_id, last, last_failed))
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n" if as_json else render_text(data)
