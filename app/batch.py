"""Batch export, the NAS side of option C (goal.md 5.10).

`build_batch` collects the jobs that passed (or conditionally passed) the filters, writes one JSON file to
`exchange/batches/` for the Claude session to process, marks the jobs `awaiting_llm`, and records the batch.
Jobs that were exported before and have no result yet are carried over, marked `fresh: false` (unless their batch
is from the same day and was never imported); only fresh jobs can enter the top 10 (goal.md 5.5). The file contains
no secrets.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, postpone
from .config import ROOT, resolve_path
from .filters.base import current_results, load_context
from .filters.language import posting_is_german
from .schemas import DOCUMENTS, RESULTS_SCHEMA, SCHEMA_VERSION

PROFILE_FILES = {"master_cv": "profile/master_cv.yaml", "variants": "profile/variants.yaml"}
SUBDIRS = ("batches", "results", "imported", "rejected", "withdrawn", "descriptions")


@dataclass
class BatchInfo:
    run_id: str
    path: Path
    job_count: int
    fresh_count: int
    created: bool = True          # False when the batch for this run id already existed
    held_back: list[int] = field(default_factory=list)      # jobs left out: their scores wait in exchange/results


def exchange_dir(cfg: dict) -> Path:
    """The exchange folder with its four subfolders, created if missing."""
    base = resolve_path(cfg["paths"]["exchange"])
    for name in SUBDIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def new_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H%M%SZ")


def _file_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _job_entry(conn, job_id: int, fresh: bool, reevaluation: bool = False, cfg: dict | None = None) -> dict:
    ctx = load_context(conn, job_id)
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    sources = [{k: r[k] for k in ("source", "external_id", "reference", "url", "source_date")}
               for r in conn.execute("SELECT * FROM job_sources WHERE job_id = ? ORDER BY id", (job_id,))]
    locations = [r["location_text"] for r in conn.execute(
        "SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (job_id,))]
    results = [{k: r[k] for k in ("filter", "outcome", "reason_code", "comment")}
               for r in current_results(conn, job_id)]         # a superseded location result is not exported
    entry = {
        "job_id": job_id,
        "fresh": fresh,
        "sources": sources,
        "title": ctx.title,
        "company": ctx.company,
        "locations": locations,
        "description": job["description"],
        "description_missing": bool(job["description_missing"]),
        "posted_on": job["posted_on"],
        "repost": bool(job["repost"]),
        "reevaluation": reevaluation,
        "previous_score": (conn.execute("SELECT overall FROM scores WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone() or [None])[0]
        if reevaluation else None,
        "filter_results": results,
        "conditional_reasons": sorted({r["reason_code"] for r in results if r["outcome"] == "conditional" and r["reason_code"]}),
    }
    if cfg is not None and posting_is_german(job["description"], cfg):
        entry["posting_language_english"] = False       # only German is detected in code; the prompt covers other languages
    return entry


def _local_date(stamp: str, tz: str):
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).date()


def _still_fresh(conn, job_id: int, today, tz: str, now: datetime | None = None) -> bool:
    """A carried-over job stays fresh when it was exported fresh today (local date) and no batch it was in has an
    imported result: `prepare` ran again the same day, for example after LinkedIn descriptions were added
    (docs/SCORING.md step 4a). A job from an earlier day's batch stays not fresh (no late responses, goal.md 5.5),
    unless it was postponed while fresh and the 72-hour window is still open (usage-limit failsafe, goal.md 5.10)."""
    if postpone.fresh_window_open(conn, job_id, now or datetime.now(timezone.utc)):
        return True
    if conn.execute("SELECT 1 FROM batch_jobs bj JOIN imports i ON i.batch_id = bj.batch_id AND i.status = 'imported' "
                    "WHERE bj.job_id = ?", (job_id,)).fetchone():
        return False
    return any(_local_date(r["created_at"], tz) == today for r in conn.execute(
        "SELECT b.created_at FROM batch_jobs bj JOIN batches b ON b.id = bj.batch_id WHERE bj.job_id = ? AND bj.fresh = 1",
        (job_id,)))


def _waiting_scored_ids(base: Path) -> set[int]:
    """Job ids that have a score in a results file still waiting in exchange/results (not imported yet). Such a job
    must not be queued again. Unreadable files are ignored (the import rejects them later)."""
    ids: set[int] = set()
    for p in (base / "results").glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            ids |= {e["job_id"] for e in data["results"] if isinstance(e, dict) and "overall" in e and isinstance(e.get("job_id"), int)}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return ids


def held_back_ids(conn, base: Path) -> list[int]:
    """Jobs still waiting for a score whose score sits in an unimported results file (left out of the next batch)."""
    return sorted(i for i in _waiting_scored_ids(base)
                  if conn.execute("SELECT 1 FROM jobs WHERE id = ? AND status = 'awaiting_llm'", (i,)).fetchone())


def candidate_jobs(conn, cap: int | None = None, now: datetime | None = None, tz: str = "UTC",
                   skip: set[int] | None = None) -> list[tuple[int, bool]]:
    """(job_id, fresh) for the next batch: carried-over jobs first, then jobs to score again (a description was
    added), then newly filtered ones, oldest first. Jobs to score again count as fresh, so they can enter the top 10.
    A carried-over job is not fresh, unless it came from a batch of the same day that was never imported
    (`_still_fresh`). A job marked closed (it left the employer's list) is not exported; it waits and comes back if
    it is listed again. Jobs in `skip` (their scores wait in a results file) are left out."""
    skip = skip or set()
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date()
    carried = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE status = 'awaiting_llm' AND needs_rescore = 0 AND closed_at IS NULL ORDER BY id")]
    again = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE needs_rescore = 1 AND status IN ('scored', 'not_selected', 'awaiting_llm') "
        "AND closed_at IS NULL ORDER BY id")]
    new = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE status = 'new' AND closed_at IS NULL "
        "AND EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id = jobs.id AND f.filter = 'seniority') ORDER BY id")]
    out = [(i, _still_fresh(conn, i, today, tz, now)) for i in carried] + [(i, True) for i in again] + [(i, True) for i in new]
    out = [(i, f) for i, f in out if i not in skip]
    return out[:cap] if cap else out


def build_batch(conn, cfg: dict, prompt_text: str, prompt_version: str, run_id: str | None = None,
                now: datetime | None = None) -> BatchInfo | None:
    """Write the batch file for this run. Returns None when there is nothing to process.

    Idempotent per run id: asking again for an existing run id returns the existing batch and changes nothing.
    """
    if not prompt_text.strip() or not prompt_version.strip():
        raise ValueError("a batch needs the prompt text and its version")
    now = now or datetime.now(timezone.utc)
    run_id = run_id or new_run_id(now)
    base = exchange_dir(cfg)

    existing = conn.execute("SELECT * FROM batches WHERE id = ?", (run_id,)).fetchone()
    if existing:
        return BatchInfo(run_id, base / "batches" / existing["file_name"], existing["job_count"],
                         existing["fresh_count"], created=False)

    skip = _waiting_scored_ids(base)
    held = held_back_ids(conn, base)
    candidates = candidate_jobs(conn, cfg["scoring"]["batch_cap"], now, cfg["timezone"], skip)
    if not candidates:
        return None

    again = {r["id"] for r in conn.execute("SELECT id FROM jobs WHERE needs_rescore = 1")}
    entries = [_job_entry(conn, job_id, fresh, job_id in again, cfg) for job_id, fresh in candidates]
    document = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prompt": {"version": prompt_version, "text": prompt_text},
        "profile": {name: {"path": rel, "sha256": _file_hash(ROOT / rel)} for name, rel in PROFILE_FILES.items()},
        "schemas": {"results": RESULTS_SCHEMA, "documents": DOCUMENTS},      # `documents` is part of a score record since 1.1
        # who gets tailored documents: fresh jobs among the top `documents_top_n` with an overall of at least `min_score`
        "tailoring": {"documents_top_n": cfg["tailoring"]["documents_top_n"], "min_score": cfg["tailoring"]["min_score"],
                      "require_description": cfg["tailoring"]["require_description"]},
        # the caps of the rubric, so the session knows them (they match the prompt; the importer enforces the non-English cap)
        "scoring": {"no_description_cap": cfg["scoring"]["no_description_cap"], "non_english_cap": cfg["scoring"]["non_english_cap"]},
        "jobs": entries,
    }
    payload = json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")
    file_name = f"batch_{run_id}.json"
    target = base / "batches" / file_name
    tmp = target.with_suffix(".json.tmp")
    tmp.write_bytes(payload)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_count = sum(1 for _, fresh in candidates if fresh)
    moved_file = False
    try:
        with conn:
            conn.execute("INSERT OR IGNORE INTO runs (id, started_at, status) VALUES (?, ?, 'ok')", (run_id, stamp))
            conn.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) "
                         "VALUES (?, ?, ?, ?, ?, ?)",
                         (run_id, stamp, file_name, hashlib.sha256(payload).hexdigest(), len(candidates), fresh_count))
            for job_id, fresh in candidates:
                conn.execute("INSERT INTO batch_jobs (batch_id, job_id, fresh, reevaluation, title_only) "
                             "VALUES (?, ?, ?, ?, (SELECT description_missing FROM jobs WHERE id = ?))",
                             (run_id, job_id, int(fresh), int(job_id in again), job_id))
                if fresh:
                    before = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]
                    if before == "awaiting_llm" and job_id not in again:
                        # carried over from a batch of the same day (`_still_fresh`): the job is fresh in one batch
                        # only, the newest, so it is never ranked twice
                        moved = [r["batch_id"] for r in conn.execute(
                            "SELECT batch_id FROM batch_jobs WHERE job_id = ? AND batch_id != ? AND fresh = 1", (job_id, run_id))]
                        conn.execute("UPDATE batch_jobs SET fresh = 0 WHERE job_id = ? AND batch_id != ?", (job_id, run_id))
                        for other in moved:
                            conn.execute("UPDATE batches SET fresh_count = fresh_count - 1 WHERE id = ?", (other,))
                    conn.execute("UPDATE jobs SET status = 'awaiting_llm', needs_rescore = 0 WHERE id = ?", (job_id,))
                    if before != "awaiting_llm":
                        conn.execute("INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) VALUES (?, ?, 'awaiting_llm', ?, ?)",
                                     (job_id, before, f"batch {run_id}" + (" (scored again with a description)" if job_id in again else ""), stamp))
            # the file is moved inside the transaction as its last step: a failure here rolls the rows back, and a
            # failed commit after the move removes the file again, so a batch row and its file always go together
            os.replace(tmp, target)
            moved_file = True
    except BaseException:                                    # also pipeline.Interrupted (a stop signal), which is not an Exception
        tmp.unlink(missing_ok=True)
        if moved_file:
            target.unlink(missing_ok=True)
        raise
    return BatchInfo(run_id, target, len(candidates), fresh_count, held_back=held)


def withdraw_batch(conn, cfg: dict, run_id: str) -> int:
    """Take back a batch that has not been scored, for example after the prompt changed.

    Only the newest batch can be withdrawn, and only while no result of it was imported. Its fresh jobs go
    back to `new` (so the next batch exports them again as fresh); the file moves to `exchange/withdrawn/`.
    Returns the number of jobs put back.
    """
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (run_id,)).fetchone()
    if batch is None:
        raise ValueError(f"no batch {run_id!r}")
    if conn.execute("SELECT 1 FROM scores WHERE run_id = ?", (run_id,)).fetchone() or        conn.execute("SELECT 1 FROM imports WHERE batch_id = ? AND status = 'imported'", (run_id,)).fetchone():
        raise ValueError(f"results of batch {run_id} were already imported; it cannot be withdrawn")
    newer = conn.execute("SELECT id FROM batches WHERE created_at > ? OR (created_at = ? AND id > ?)",
                         (batch["created_at"], batch["created_at"], run_id)).fetchone()
    if newer:
        raise ValueError(f"batch {newer['id']} is newer; only the newest batch can be withdrawn")
    stamp = db.utcnow()
    fresh = [(r["job_id"], bool(r["reevaluation"])) for r in conn.execute(
        "SELECT job_id, reevaluation FROM batch_jobs WHERE batch_id = ? AND fresh = 1", (run_id,))]
    put_back = 0
    with conn:
        for job_id, was_rescore in fresh:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            older = conn.execute(
                "SELECT bj.batch_id FROM batch_jobs bj JOIN batches b ON b.id = bj.batch_id WHERE bj.job_id = ? "
                "AND bj.batch_id != ? AND NOT EXISTS (SELECT 1 FROM imports i WHERE i.batch_id = bj.batch_id "
                "AND i.status = 'imported') ORDER BY b.created_at DESC, b.id DESC LIMIT 1", (job_id, run_id)).fetchone()
            if row and row["status"] == "awaiting_llm" and older and not was_rescore:
                # carried over fresh from an older batch of the same day: the flag goes back there, the job waits
                conn.execute("UPDATE batch_jobs SET fresh = 1 WHERE batch_id = ? AND job_id = ?", (older["batch_id"], job_id))
                conn.execute("UPDATE batches SET fresh_count = fresh_count + 1 WHERE id = ?", (older["batch_id"],))
            elif row and row["status"] == "awaiting_llm":
                # a job that had a score goes back to `scored` and is still marked to be scored again
                target = "scored" if was_rescore else "new"
                conn.execute("UPDATE jobs SET status = ?, needs_rescore = ? WHERE id = ?", (target, int(was_rescore), job_id))
                conn.execute("INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) "
                             "VALUES (?, 'awaiting_llm', ?, ?, ?)", (job_id, target, f"batch {run_id} withdrawn", stamp))
                put_back += 1
        conn.execute("DELETE FROM batch_jobs WHERE batch_id = ?", (run_id,))
        conn.execute("DELETE FROM batches WHERE id = ?", (run_id,))
    source = exchange_dir(cfg) / "batches" / batch["file_name"]
    if source.exists():
        source.replace(exchange_dir(cfg) / "withdrawn" / batch["file_name"])
    return put_back


def oldest_awaiting_age_days(conn, now: datetime | None = None) -> float | None:
    """Age in days of the oldest job still waiting for results (shown on the Health page); None if none wait."""
    row = conn.execute(
        "SELECT MIN(b.created_at) AS first FROM batch_jobs bj JOIN batches b ON b.id = bj.batch_id "
        "JOIN jobs j ON j.id = bj.job_id WHERE j.status = 'awaiting_llm'").fetchone()
    if not row or not row["first"]:
        return None
    first = datetime.strptime(row["first"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - first).total_seconds() / 86400
