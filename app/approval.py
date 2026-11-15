"""The approval step for tailored documents (goal.md 5.6, Phase 3 slice 3e).

A tailored document is a draft until the user approves it. `approve` freezes a document: it must be a `draft` whose
PDF exists in `paths.documents`; the PDF is copied to `paths.approved/<job id>_<company-slug>/<same file name>` and the
row becomes `approved` (with `approved_at` and `approved_pdf_path`, relative to `paths.approved`). Later re-renders never
touch the copy, and a later results file does not replace an approved document (`documents.import_documents`).
When both documents of a job are approved, the job's status becomes `approved` (goal.md 5.7); one alone changes nothing.
`undo` takes an approval back: the document is a draft again, its copy is removed (never the original), and a job
that had become `approved` goes back to the status it had before.

The user decides at the command line (`python -m app approve-document`); the web app of Phase 4 takes this over. Nothing
here uses the network, writes outside `paths.documents`/`paths.approved`, or logs document text.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import db, documents
from .documents import KINDS, WARNING_KINDS

log = logging.getLogger("recruitment")

# A job in one of these statuses moves to `approved` when both documents are approved; a job that is further along
# (applied, interview, offer, rejected, withdrawn, location_failed) keeps its status.
BEFORE_APPROVED = ("new", "awaiting_llm", "scored", "not_selected", "shortlisted")
FALLBACK_STATUS = "scored"


@dataclass
class Item:
    kind: str
    outcome: str                                           # approved | already | undone | refused
    message: str = ""
    path: Path | None = None                               # the approved copy
    warnings: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    job_id: int
    items: list[Item] = field(default_factory=list)
    job_status: str | None = None                          # set when the job's status was changed
    notes: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return any(i.outcome == "refused" for i in self.items)


def _inside(root: Path, target: Path) -> bool:
    return root.resolve() in target.resolve().parents


def _same_bytes(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()


def _select(kinds) -> tuple[str, ...]:
    return tuple(k for k in KINDS if kinds is None or k in kinds)


def _finding_lines(findings: list[dict]) -> str:
    return "; ".join(f"[{f['kind']}] {f.get('where') or 'document'}: {f['reason']}" for f in findings)


def approve(conn, cfg: dict, job_id: int, kinds=None, stamp: str | None = None) -> Outcome:
    """Approve the documents of one job (`kinds` None = both). Each kind is decided on its own: one that cannot be
    approved is refused with the reason and does not hold back the other."""
    out = Outcome(job_id)
    stamp = stamp or db.utcnow()
    src_root, dst_root = documents.documents_dir(cfg), documents.approved_dir(cfg)
    for kind in _select(kinds):
        row = conn.execute("SELECT * FROM documents WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone()
        if row is None:
            out.items.append(Item(kind, "refused", f"job {job_id} has no stored {kind}"))
            continue
        if row["status"] == "approved":
            copy = dst_root / row["approved_pdf_path"] if row["approved_pdf_path"] else None
            item = Item(kind, "already", f"already approved on {row['approved_at']}", copy)
            if copy is None or not copy.is_file():
                item.warnings.append("the approved copy is missing from the approved folder; undo the approval and approve again")
            out.items.append(item)
            continue
        findings = json.loads(row["findings"])
        if row["status"] == "blocked":
            out.items.append(Item(kind, "refused", "blocked by the checks, it cannot be approved. Findings: " + _finding_lines(findings)))
            continue
        relative = row["pdf_path"]
        source = (src_root / relative) if relative else None
        if source is None or not source.is_file() or not _inside(src_root, source):
            out.items.append(Item(kind, "refused", "it has no PDF on disk; run `python -m app render-documents --job "
                                                   f"{job_id}` first, then approve it"))
            continue
        target = dst_root / relative
        if not _inside(dst_root, target):
            out.items.append(Item(kind, "refused", f"the PDF path {relative!r} points outside the approved folder"))
            continue
        if target.exists() and not (target.is_file() and _same_bytes(source, target)):
            out.items.append(Item(kind, "refused", f"a different file is already at {target}; move it away or remove it, then approve again"))
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + ".part")
            shutil.copyfile(source, partial)
            os.replace(partial, target)
        except OSError as exc:
            out.items.append(Item(kind, "refused", f"the PDF could not be copied to {target}: {exc}"))
            continue
        with conn:
            conn.execute("UPDATE documents SET status = 'approved', approved_at = ?, approved_pdf_path = ? WHERE id = ?",
                         (stamp, relative, row["id"]))
        item = Item(kind, "approved", "approved", target)
        item.warnings = [f["reason"] for f in findings if f["kind"] in WARNING_KINDS]
        out.items.append(item)
        log.info("job %s %s approved", job_id, kind)
    _update_job(conn, out)
    return out


def outcome_lines(res: Outcome, undo: bool = False) -> list[tuple[str, str]]:
    """The messages for the outcome of `approve` or `undo`, as (level, text) pairs in the order they are shown. Level is
    `ok`, `refused`, `warning`, `note` or `summary`. The command line and the web app both use this, so the messages are
    the same (the command line prints `refused` and `warning` lines to standard error, the rest to standard output)."""
    lines: list[tuple[str, str]] = []
    for item in res.items:
        label = f"job {res.job_id} {item.kind}"
        if item.outcome == "refused":
            lines.append(("refused", f"REFUSED: {label}: {item.message}"))
            continue
        where = f"  {item.path}" if item.path and item.outcome != "undone" else ""
        lines.append(("ok", f"{label}: {item.message}{where}"))
        for w in item.warnings:
            lines.append(("warning", f"WARNING: {label}: {w}"))
    if res.job_status:
        lines.append(("ok", f"job {res.job_id} is now '{res.job_status}'"))
    for note in res.notes:
        lines.append(("note", f"note: {note}"))
    done = sum(i.outcome in ("approved", "undone") for i in res.items)
    lines.append(("summary", f"{done} document(s) {'taken back to draft' if undo else 'approved'}, "
                             f"{sum(i.outcome == 'already' for i in res.items)} already approved, "
                             f"{sum(i.outcome == 'refused' for i in res.items)} refused"))
    return lines


def _update_job(conn, out: Outcome) -> None:
    """The job becomes `approved` once both of its documents are."""
    states = {r["kind"]: r["status"] for r in conn.execute("SELECT kind, status FROM documents WHERE job_id = ?", (out.job_id,))}
    if not all(states.get(k) == "approved" for k in KINDS):
        return
    job = conn.execute("SELECT status FROM jobs WHERE id = ?", (out.job_id,)).fetchone()
    if job["status"] == "approved":
        return
    if job["status"] not in BEFORE_APPROVED:
        out.notes.append(f"both documents are approved; the job stays '{job['status']}' (it is already further along)")
        return
    db.set_status(conn, out.job_id, "approved", "both documents approved")
    out.job_status = "approved"


def _previous_status(conn, job_id: int) -> str:
    """The status the job had before it became `approved` (from status_history), else the fallback."""
    marks = ",".join("?" * len(BEFORE_APPROVED))                     # a later applied -> approved (web undo) row is not "before"
    row = conn.execute(f"SELECT from_status FROM status_history WHERE job_id = ? AND to_status = 'approved' "
                       f"AND from_status IN ({marks}) ORDER BY id DESC LIMIT 1", (job_id, *BEFORE_APPROVED)).fetchone()
    previous = row["from_status"] if row else None
    return previous if previous and previous != "approved" else FALLBACK_STATUS


def undo(conn, cfg: dict, job_id: int, kinds=None) -> Outcome:
    """Take an approval back: the document is a draft again, its approved copy is removed (never the original in
    `paths.documents`). A document that is not approved is refused. A job that had become `approved` goes back."""
    out = Outcome(job_id)
    dst_root = documents.approved_dir(cfg)
    for kind in _select(kinds):
        row = conn.execute("SELECT * FROM documents WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone()
        if row is None:
            out.items.append(Item(kind, "refused", f"job {job_id} has no stored {kind}"))
            continue
        if row["status"] != "approved":
            out.items.append(Item(kind, "refused", f"it is not approved (status {row['status']}); nothing to undo"))
            continue
        copy = dst_root / row["approved_pdf_path"] if row["approved_pdf_path"] else None
        if copy is not None and not _inside(dst_root, copy):
            out.items.append(Item(kind, "refused", f"the recorded copy {row['approved_pdf_path']!r} lies outside the approved folder; nothing was removed"))
            continue
        # the database first: if the update fails the approved copy is still there and the row still says approved
        with conn:
            conn.execute("UPDATE documents SET status = 'draft', approved_at = NULL, approved_pdf_path = NULL WHERE id = ?", (row["id"],))
        item = Item(kind, "undone", "approval taken back, the document is a draft again", copy)
        if copy is not None:
            try:
                copy.unlink(missing_ok=True)
                try:
                    copy.parent.rmdir()                      # the job's folder, only when it is empty now
                except OSError:
                    pass
            except OSError as exc:
                item.warnings.append(f"the approved copy {copy} could not be removed: {exc}; delete it by hand")
        out.items.append(item)
        log.info("job %s %s approval undone", job_id, kind)
    job = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is not None and job["status"] == "approved" and any(i.outcome == "undone" for i in out.items):
        back = _previous_status(conn, job_id)
        db.set_status(conn, job_id, back, "approval of a document undone")
        out.job_status = back
    return out
