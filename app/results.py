"""Results import, the return path of option C (goal.md 5.10, 5.4, 5.5).

`import_results` reads one results file written by the Claude session, validates it, stores the scores,
selects the top 10, updates statuses and moves the file to `exchange/imported/` or `exchange/rejected/`.

Rejected as a whole (nothing stored, reason recorded): not JSON, wrong schema or version, unknown run id,
duplicate job ids, job ids that are not in the batch, an unknown CV variant.
Handled per job (the rest is stored, the reason is recorded): a job that is no longer waiting (already
scored, archived), a job the session could not score (its `error` is recorded and the job stays
`awaiting_llm` for the next batch). Jobs absent from the file stay `awaiting_llm` and are reported as missing.

Idempotent: a file with the same SHA-256 as an imported one changes nothing (status `duplicate`).

The top 10 is chosen here, by the rules of goal.md 5.5, not taken from the file: among the batch's fresh jobs,
highest `overall` first, ties broken by the `skills` sub-score and then by the lower job id. The file's own
`top` is compared and a difference is reported as a warning.

Schema 1.1 (goal.md 5.6): a score record may carry `documents` (cover letter, CV). After the selection is
recomputed, `app/documents.py` decides which jobs qualify (NAS-selected, fresh, rank within
`tailoring.documents_top_n`, `overall` at least `tailoring.min_score`), checks the text (traceability check, fact
ids, contact person, home address) and stores it as `draft` or `blocked`. Documents for other jobs are ignored and
counted, qualifying jobs without documents are listed as missing, both as warnings. A document of a kind that is
approved already (slice 3e) is not replaced: it is listed as frozen, in the summary and as a warning. A 1.0 file is
imported as before and may not carry `documents`.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import db, documents, postpone
from .batch import PROFILE_FILES, exchange_dir
from .config import ROOT
from .filters.language import posting_is_german
from .prompt import weighted_overall
from .schemas import problems as _problems                 # moved to app/schemas.py in v0.78.2 (the headless worker uses it too)


@dataclass
class ImportResult:
    status: str                                   # "imported" | "rejected" | "duplicate"
    batch_id: str | None = None
    reason: str | None = None                     # why a file was rejected
    file_name: str | None = None                  # the results file this is about (v0.78.2: the log names it)
    scored: list[int] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)     # jobs the session could not score
    missing: list[int] = field(default_factory=list)                # batch jobs with no entry in the file
    warnings: list[str] = field(default_factory=list)
    top: list[int] = field(default_factory=list)
    documents: list[tuple[int, str, str, int]] = field(default_factory=list)       # job, kind, status, findings
    documents_ignored: list[tuple[int, str]] = field(default_factory=list)         # job, reason
    documents_missing: list[tuple[int, list[str]]] = field(default_factory=list)   # job, missing kinds
    documents_rendered: list[tuple[int, str, int]] = field(default_factory=list)   # job, kind, pages of the PDF
    documents_frozen: list[tuple[int, str]] = field(default_factory=list)          # job, kind: approved already, new text not stored


def variant_names() -> set[str]:
    path = ROOT / PROFILE_FILES["variants"]
    if not path.exists():
        return set()
    return set((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("variants", {}))


def _unique_target(folder: Path, name: str) -> Path:
    target = folder / name
    n = 1
    while target.exists():
        target = folder / f"{Path(name).stem}.{n}{Path(name).suffix}"
        n += 1
    return target


def _finish(conn, cfg, path: Path, sha: str, result: ImportResult, now: str) -> ImportResult:
    """Record the outcome in `imports` and move the file."""
    base = exchange_dir(cfg)
    result.file_name = path.name
    summary = {"scored": result.scored, "skipped": result.skipped, "errors": result.errors,
               "missing": result.missing, "warnings": result.warnings, "top": result.top,
               "documents": result.documents, "documents_ignored": result.documents_ignored,
               "documents_missing": result.documents_missing, "documents_rendered": result.documents_rendered,
               "documents_frozen": result.documents_frozen}
    with conn:
        cur = conn.execute("INSERT INTO imports (batch_id, file_name, sha256, status, reason, summary, imported_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (result.batch_id, path.name, sha, result.status, result.reason,
                            json.dumps(summary, ensure_ascii=False), now))
        for job_id, kind, _status, _n in result.documents:
            conn.execute("UPDATE documents SET import_id = ? WHERE job_id = ? AND kind = ?", (cur.lastrowid, job_id, kind))
    folder = base / ("rejected" if result.status == "rejected" else "imported")
    target = _unique_target(folder, path.name if result.status != "duplicate" else path.stem + ".duplicate" + path.suffix)
    shutil.move(str(path), str(target))
    if result.status == "rejected":
        target.with_name(target.name + ".reason.txt").write_text(result.reason or "", encoding="utf-8")
    return result


def _reject(conn, cfg, path, sha, now, reason, batch_id=None) -> ImportResult:
    return _finish(conn, cfg, path, sha, ImportResult("rejected", batch_id, reason=reason), now)


def _ranked(conn, batch_id: str) -> list:
    """The batch's scored fresh jobs in the order of goal.md 5.5 (overall, then skills, then the lower job id), with
    `description_missing` of the job."""
    rows = conn.execute(
        """SELECT bj.job_id, bj.fresh, s.overall, s.subscores, j.description_missing, sb.title_only
           FROM batch_jobs bj JOIN scores s ON s.id = (SELECT MAX(id) FROM scores WHERE job_id = bj.job_id)
           JOIN jobs j ON j.id = bj.job_id
           LEFT JOIN batch_jobs sb ON sb.batch_id = s.run_id AND sb.job_id = bj.job_id
           WHERE bj.batch_id = ? AND (s.run_id = bj.batch_id OR j.status != 'awaiting_llm')""", (batch_id,)).fetchall()
    return sorted((r for r in rows if r["fresh"]),
                  key=lambda r: (-r["overall"], -json.loads(r["subscores"]).get("skills", 0), r["job_id"]))


def would_be_top(conn, batch_id: str, top_n: int) -> list[int]:
    """The top list by the session's rule (prompt 1.15.0): the best `top_n` fresh jobs by score, title-only ones included."""
    return [r["job_id"] for r in _ranked(conn, batch_id)[:top_n]]


def _waits(r, hold: bool) -> bool:
    """A job waits when it has no description now, or when its latest score was made from the title only (stored at export:
    a description pasted since then does not make that estimate rankable); NULL (an older batch): the current state."""
    return hold and bool(r["description_missing"] or r["title_only"] == 1)


def compute_selection(conn, batch_id: str, top_n: int, hold: bool = False) -> tuple[list[int], list[tuple[int, int]]]:
    """The selection `select_top` would store, without storing it: (chosen job ids in rank order, [(queue place, job id)])."""
    ranked = _ranked(conn, batch_id)
    queued = [(place, r["job_id"]) for place, r in enumerate(ranked[:top_n], start=1) if _waits(r, hold)]
    chosen = [r["job_id"] for r in ranked if not _waits(r, hold)][:top_n]
    return chosen, queued


def select_top(conn, batch_id: str, top_n: int, hold: bool = False) -> list[int]:
    """Apply goal.md 5.5 to a batch: rank its scored fresh jobs, store `selected` and `rank`, set statuses.

    With `hold` (config `tailoring.require_description`, v0.72.0) a job without a description is never ranked: if it
    is in the would-be top `top_n` (the best `top_n` by score, title-only estimates included) it is queued
    (`queue_rank` = its place there) and the next described jobs move up into the places it leaves; a title-only job
    outside the would-be list keeps its score and is simply not selected. The queue is recomputed on every import.

    Statuses are changed only for jobs that are `scored` or `not_selected`; a job the user has already moved
    on (shortlisted, applied, ...) keeps its status but still counts in the ranking.
    """
    chosen, queued = compute_selection(conn, batch_id, top_n, hold)
    rows = conn.execute("SELECT job_id FROM batch_jobs WHERE batch_id = ?", (batch_id,)).fetchall()
    stamp = db.utcnow()
    with conn:
        conn.execute("UPDATE batch_jobs SET selected = 0, rank = NULL, queue_rank = NULL WHERE batch_id = ?", (batch_id,))
        for position, job_id in enumerate(chosen, start=1):
            conn.execute("UPDATE batch_jobs SET selected = 1, rank = ? WHERE batch_id = ? AND job_id = ?",
                         (position, batch_id, job_id))
        for place, job_id in queued:
            conn.execute("UPDATE batch_jobs SET queue_rank = ? WHERE batch_id = ? AND job_id = ?", (place, batch_id, job_id))
        for r in rows:
            job = conn.execute("SELECT status FROM jobs WHERE id = ?", (r["job_id"],)).fetchone()
            if job is None or job["status"] not in ("scored", "not_selected"):
                continue
            wanted = "scored" if r["job_id"] in chosen else "not_selected"
            if job["status"] != wanted:
                conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (wanted, r["job_id"]))
                conn.execute("INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) "
                             "VALUES (?, ?, ?, ?, ?)", (r["job_id"], job["status"], wanted, f"batch {batch_id} selection", stamp))
    return chosen


def stale_selection_batches(conn, cfg: dict) -> list[str]:
    """Batches whose stored selection holds a job that now waits for a description (config `tailoring.require_description`):
    a selected job that is `scored` or `not_selected` and has no description or only a title-only score. `reselect` re-picks them."""
    hold = cfg["tailoring"]["require_description"]
    if not hold:
        return []
    out = []
    for batch_id in [r[0] for r in conn.execute("SELECT DISTINCT batch_id FROM batch_jobs WHERE selected = 1 ORDER BY batch_id")]:
        selected = {r[0] for r in conn.execute("SELECT job_id FROM batch_jobs WHERE batch_id = ? AND selected = 1", (batch_id,))}
        for r in _ranked(conn, batch_id):
            if r["job_id"] in selected and _waits(r, hold) and conn.execute(
                    "SELECT status FROM jobs WHERE id = ?", (r["job_id"],)).fetchone()["status"] in ("scored", "not_selected"):
                out.append(batch_id)
                break
    return out


def refresh_selection(conn, cfg: dict) -> list[str]:
    """Pick the top places again in every batch that lost a selected job to the archive (goal.md 5.5).

    An archived job loses its scores but keeps its `selected` row, so its place would stay empty and the ranks
    would have a gap. `select_top` ranks only jobs that still have a score, so the next best job moves up.
    Returns the batches that were re-picked; a batch where nothing was lost is left alone.
    """
    lost = [r[0] for r in conn.execute(
        """SELECT DISTINCT bj.batch_id FROM batch_jobs bj LEFT JOIN jobs j ON j.id = bj.job_id
           WHERE bj.selected = 1 AND j.id IS NULL ORDER BY bj.batch_id""")]
    for batch_id in lost:
        select_top(conn, batch_id, cfg["scoring"]["top_n"], cfg["tailoring"]["require_description"])
    return lost


def import_results(conn, cfg: dict, path: str | Path, now: datetime | None = None) -> ImportResult:
    path = Path(path)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()

    seen = conn.execute("SELECT batch_id FROM imports WHERE sha256 = ? AND status = 'imported'", (sha,)).fetchone()
    if seen:
        return _finish(conn, cfg, path, sha, ImportResult("duplicate", seen["batch_id"], reason="same file already imported"), stamp)

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _reject(conn, cfg, path, sha, stamp, f"not valid JSON: {exc}")

    problems = _problems(data)
    if problems:
        shown = "; ".join(f"{where}: {message}" for where, message in problems[:8])
        more = f" (and {len(problems) - 8} more)" if len(problems) > 8 else ""
        return _reject(conn, cfg, path, sha, stamp, f"schema check failed: {shown}{more}",
                       data.get("run_id") if isinstance(data, dict) and isinstance(data.get("run_id"), str) else None)

    batch_id = data["run_id"]
    if not conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone():
        return _reject(conn, cfg, path, sha, stamp, f"unknown run_id {batch_id!r}: no such batch", batch_id)

    ids = [e["job_id"] for e in data["results"]]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        return _reject(conn, cfg, path, sha, stamp, f"duplicate job ids in the file: {duplicated}", batch_id)
    in_batch = {r["job_id"] for r in conn.execute("SELECT job_id FROM batch_jobs WHERE batch_id = ?", (batch_id,))}
    foreign = sorted(set(ids) - in_batch)
    if foreign:
        return _reject(conn, cfg, path, sha, stamp, f"job ids not in batch {batch_id}: {foreign}", batch_id)
    if data["schema_version"] == "1.0" and any("documents" in e for e in data["results"]):
        return _reject(conn, cfg, path, sha, stamp, "documents need schema_version 1.1; this file declares 1.0", batch_id)
    known = variant_names()
    bad_variants = sorted({e["cv_variant"] for e in data["results"] if e.get("cv_variant") and e["cv_variant"] not in known})
    if bad_variants:
        return _reject(conn, cfg, path, sha, stamp, f"unknown cv_variant {bad_variants}; known: {sorted(known)}", batch_id)

    result = ImportResult("imported", batch_id)
    off: list[tuple[int, int, int]] = []
    clamped: list[tuple[int, int, int]] = []          # (job, given, stored): non-English posting, overall cut to the cap
    with conn:
        for entry in data["results"]:
            job_id = entry["job_id"]
            job = conn.execute("SELECT status, description_missing, description FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                result.skipped.append((job_id, "job is no longer open (archived)"))
                continue
            if job["status"] != "awaiting_llm":
                result.skipped.append((job_id, f"not waiting for a score (status {job['status']})"))
                continue
            if "error" in entry:
                result.errors.append((job_id, entry["error"]))
                continue
            details = {k: entry[k] for k in ("matches", "gaps", "red_flags", "summary")}
            # a posting that is not written in English is at most `non_english_cap` (goal.md 5.4, decided 1 Nov 2026);
            # unlike the cap without a description this is enforced here, so top 10 and documents see the clamped value
            overall = entry["overall"]
            language_cap = cfg["scoring"]["non_english_cap"] if posting_is_german(job["description"], cfg) else None
            if language_cap is not None and overall > language_cap:
                clamped.append((job_id, overall, language_cap))
                overall = language_cap
            conn.execute(
                "INSERT INTO scores (job_id, run_id, prompt_version, model, overall, subscores, details, cv_variant, "
                "confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, batch_id, data["prompt_version"], data["model"], overall,
                 json.dumps(entry["subscores"]), json.dumps(details, ensure_ascii=False), entry.get("cv_variant"),
                 entry["confidence"], stamp))
            conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (job_id,))
            conn.execute("INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) "
                         "VALUES (?, 'awaiting_llm', 'scored', ?, ?)", (job_id, f"batch {batch_id}", stamp))
            postpone.resolve(conn, job_id, stamp)
            result.scored.append(job_id)
            caps = (([cfg["scoring"]["no_description_cap"]] if job["description_missing"] else [])
                    + ([language_cap] if language_cap is not None else []))
            part_time = any(f.lower().startswith("part-time") for f in entry["red_flags"])      # prompt 1.12.0: 5 off after the caps
            expected = weighted_overall(entry["subscores"], cfg["scoring"]["weights"], min(caps) if caps else None, part_time)
            if abs(overall - expected) > cfg["scoring"]["overall_tolerance"]:
                off.append((job_id, overall, expected))

    if off:
        result.warnings.append("overall differs from the weighted sub-scores (job, given, expected): "
                               + ", ".join(f"{j}: {g} vs {e}" for j, g, e in off))
    if clamped:
        result.warnings.append("posting not in English: overall cut to the cap (job, given, stored): "
                               + ", ".join(f"{j}: {g} -> {c}" for j, g, c in clamped))
    result.missing = sorted(i for i in in_batch - set(ids)
                            if (j := conn.execute("SELECT status FROM jobs WHERE id = ?", (i,)).fetchone())
                            and j["status"] == "awaiting_llm")
    result.top = select_top(conn, batch_id, cfg["scoring"]["top_n"], cfg["tailoring"]["require_description"])
    # the session's list is the would-be top (title-only jobs included, prompt 1.15.0); the NAS selection skips a job
    # that waits for a description (v0.72.0), so the two are compared as the session wrote it
    session_rule = would_be_top(conn, batch_id, cfg["scoring"]["top_n"])
    if data["top"] != session_rule and not CHUNK_FILE.match(path.name):       # a chunk file carries no top list
        result.warnings.append(f"the file's top list {data['top']} differs from the rules of goal.md 5.5; "
                               f"the NAS selection {result.top} is used")
    if result.missing:
        result.warnings.append(f"{len(result.missing)} job(s) have no result and stay awaiting_llm")
    if data["schema_version"] != "1.0":
        moment = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(cfg["timezone"]))
        out = documents.import_documents(conn, cfg, batch_id, data["results"], moment.date(), stamp)
        result.documents, result.documents_ignored, result.documents_missing = out.stored, out.ignored, out.missing
        result.documents_frozen = out.frozen
        if out.frozen:
            result.warnings.append("documents not stored because the document is approved already and stays as approved (job, kind): "
                                   + ", ".join(f"{j} {k}" for j, k in out.frozen))
        blocked = [(j, k, n) for j, k, s, n in out.stored if s == "blocked"]
        if blocked:
            result.warnings.append("documents blocked by the checks (job, kind, findings): "
                                   + ", ".join(f"{j} {k} {n}" for j, k, n in blocked))
        if out.ignored:
            result.warnings.append(f"documents ignored for {len(out.ignored)} job(s) that do not qualify (job: reason): "
                                   + "; ".join(f"{j}: {why}" for j, why in out.ignored))
        if out.rendered:
            result.documents_rendered = [(j, k, n) for j, k, _p, n in out.rendered.rendered]
            if out.rendered.over_limit:
                result.warnings.append("PDFs over the page limit, kept as drafts (job, kind, pages, limit): "
                                       + ", ".join(f"{j} {k} {n} > {lim}" for j, k, n, lim in out.rendered.over_limit))
            if out.rendered.line_endings:
                result.warnings.append("documents with a last line of one or two words or under a third full, kept as drafts "
                                       "(job, kind, blocks; fix the wording, see the Line endings rule of the prompt): "
                                       + ", ".join(f"{j} {k} {n}" for j, k, n in out.rendered.line_endings))
            if out.rendered.failed:
                result.warnings.append("PDFs that could not be built (job, kind, reason): "
                                       + "; ".join(f"{j} {k}: {why}" for j, k, why in out.rendered.failed))
            result.warnings += out.rendered.warnings
        if out.missing and not CHUNK_FILE.match(path.name):         # a chunk file carries scores only
            result.warnings.append("documents missing for job(s) that qualify (job: kinds): "
                                   + "; ".join(f"{j}: {', '.join(kinds)}" for j, kinds in out.missing))
    return _finish(conn, cfg, path, sha, result, stamp)


def process_results_dir(conn, cfg: dict, now: datetime | None = None) -> list[ImportResult]:
    """Import every *.json file in exchange/results/, by run; within a run the chunk files (.pNN) first, then the rest."""
    folder = exchange_dir(cfg) / "results"
    return [import_results(conn, cfg, p, now) for p in sorted(folder.glob("*.json"), key=_file_order)]


CHUNK_FILE = re.compile(r"^(?P<run>.+)\.p(?P<nn>\d+)\.json$")


def _file_order(path: Path) -> tuple:
    """Per run: the chunk files (.pNN) by number, then the other files (the documents file), then by name."""
    m = CHUNK_FILE.match(path.name)
    if m:
        return (m["run"], 0, int(m["nn"]), path.name)
    return (path.stem, 1, 0, path.name)
