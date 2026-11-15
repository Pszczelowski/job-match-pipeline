"""The usage-limit failsafe (goal.md 5.10, v0.69.0).

When the Claude session stops in the middle of a batch (the usage limit, or anything else), `finish` imports what
the chunk files hold and records every job of those batches that is still waiting and got no error entry as
postponed (table `postponements`). A postponed job that was fresh stays fresh in the next batch for 72 hours
(UTC) from the postponement, so it can still enter the top 10 (`fresh_window_open`, used by `batch._still_fresh`).

The reason is `usage_limit` only when `finish --limit-message` got the text Claude Code shows at the limit;
otherwise it is the safe `no_result` ("no result received"). Nothing here reads or sends anything.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

LIMIT_RE = re.compile(r"hit your session limit\s*[·∙•.:|-]?\s*resets", re.IGNORECASE)
LABELS = {"usage_limit": "usage limit", "no_result": "no result received"}
FRESH_HOURS = 72
EVIDENCE_MAX = 300
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def limit_seen(text: str | None) -> bool:
    return bool(text and LIMIT_RE.search(text))


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(_FORMAT)


def record(conn, batch_ids, errored: set[int], now: datetime | None = None,
           limit_message: str | None = None) -> dict[str, tuple[str, list[int]]]:
    """Record the postponed jobs of the given (just imported) batches. Returns {batch: (label, [job ids])} for the
    batches that have open postponements. A job already recorded for the batch is left alone (once per batch); a
    matching limit message upgrades the open `no_result` rows of these batches."""
    stamp = _stamp(now)
    seen = limit_seen(limit_message)
    reason = "usage_limit" if seen else "no_result"
    evidence = (limit_message or "").strip()[:EVIDENCE_MAX] if seen else None
    out: dict[str, tuple[str, list[int]]] = {}
    with conn:
        for batch_id in dict.fromkeys(batch_ids):
            for r in conn.execute(
                    "SELECT bj.job_id, bj.fresh FROM batch_jobs bj JOIN jobs j ON j.id = bj.job_id "
                    "WHERE bj.batch_id = ? AND j.status = 'awaiting_llm' ORDER BY bj.job_id", (batch_id,)).fetchall():
                if r["job_id"] in errored:
                    continue
                conn.execute(
                    "INSERT INTO postponements (job_id, batch_id, fresh, reason, evidence, noted_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(job_id, batch_id) DO NOTHING", (r["job_id"], batch_id, r["fresh"], reason, evidence, stamp))
            if seen:
                conn.execute("UPDATE postponements SET reason = 'usage_limit', evidence = ? "
                             "WHERE batch_id = ? AND resolved_at IS NULL AND reason = 'no_result'", (evidence, batch_id))
            for job_id in closed_waiting_jobs(conn):                     # a closed job never gets its result: it is not postponed
                release_closed(conn, job_id, stamp)
            rows = conn.execute("SELECT p.job_id, p.reason FROM postponements p JOIN jobs j ON j.id = p.job_id "
                                "WHERE p.batch_id = ? AND p.resolved_at IS NULL ORDER BY p.job_id", (batch_id,)).fetchall()
            if rows:
                label = "usage_limit" if any(x["reason"] == "usage_limit" for x in rows) else "no_result"
                out[batch_id] = (LABELS[label], [x["job_id"] for x in rows])
    return out


def resolve(conn, job_id: int, stamp: str) -> None:
    """The job was scored: its open postponements are closed."""
    conn.execute("UPDATE postponements SET resolved_at = ? WHERE job_id = ? AND resolved_at IS NULL", (stamp, job_id))


def release_closed(conn, job_id: int, stamp: str | None = None, dry_run: bool = False) -> str | None:
    """A job that left the employer's list (`closed_at`) while it still waited for a score is no longer waiting (v0.90.0).

    Closed jobs are never exported again, so an `awaiting_llm` status and its open postponements would stay forever. The job goes
    back to the state before its batch (`scored` with a new score due if it has a score, else `new`; both come back into a batch
    if it is listed again), its open postponements are resolved and the change is written to the status history. A job that is
    in the newest batch, if its results are not imported yet, is left alone: its result may still arrive
    (an older batch that was never imported is not waited for). Returns the new status, or None
    if nothing applies (with `dry_run` nothing is written)."""
    job = conn.execute("SELECT status, closed_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or job["status"] != "awaiting_llm" or job["closed_at"] is None:
        return None
    if conn.execute(
            "SELECT 1 FROM batch_jobs bj JOIN batches b ON b.id = bj.batch_id WHERE bj.job_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM imports i WHERE i.batch_id = bj.batch_id AND i.status = 'imported') "
            "AND NOT EXISTS (SELECT 1 FROM batches n WHERE n.created_at > b.created_at OR (n.created_at = b.created_at AND n.id > b.id))",
            (job_id,)).fetchone():
        return None
    scored = conn.execute("SELECT 1 FROM scores WHERE job_id = ?", (job_id,)).fetchone() is not None
    target = "scored" if scored else "new"
    if not dry_run:
        stamp = stamp or _stamp(None)
        conn.execute("UPDATE jobs SET status = ?, needs_rescore = ? WHERE id = ?", (target, int(scored), job_id))
        conn.execute("INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) VALUES (?, 'awaiting_llm', ?, ?, ?)",
                     (job_id, target, "closed: no longer listed by the employer", stamp))
        resolve(conn, job_id, stamp)
    return target


def closed_waiting_jobs(conn) -> list[int]:
    """Closed jobs that are still `awaiting_llm` (what `release-closed` looks at)."""
    return [r[0] for r in conn.execute("SELECT id FROM jobs WHERE status = 'awaiting_llm' AND closed_at IS NOT NULL ORDER BY id")]


def fresh_window_open(conn, job_id: int, now: datetime) -> bool:
    """True while 72 hours (UTC) after the job's earliest fresh postponement are still ahead."""
    row = conn.execute("SELECT MIN(noted_at) AS first FROM postponements WHERE job_id = ? AND fresh = 1", (job_id,)).fetchone()
    if not row or not row["first"]:
        return False
    first = datetime.strptime(row["first"], _FORMAT).replace(tzinfo=timezone.utc)
    return first + timedelta(hours=FRESH_HOURS) > now


def open_counts(conn) -> dict[str, int]:
    """Open postponements by reason label (jobs, not rows)."""
    counts = {LABELS[k]: 0 for k in LABELS}
    for r in conn.execute("SELECT p.reason, COUNT(DISTINCT p.job_id) AS n FROM postponements p JOIN jobs j ON j.id = p.job_id "
                          "WHERE p.resolved_at IS NULL GROUP BY p.reason"):
        counts[LABELS[r["reason"]]] = r["n"]
    return counts
