"""Reject one open job (goal.md 5.3.3, 5.7): the code the web button and `python -m app reject` share, so both have the same
effect. The job moves to the archive as stage `user`, reason `user_rejected`; approved PDFs stay on disk."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db

COMMENT_MAX = 300
DB_BUSY_TEXT = "The database could not be written to (it may be busy with a pipeline run). Nothing was changed; try again in a moment."
# Reject from any status before the application, and (decision of 10 Nov 2026) from applied; a job that was applied for and is
# further along is not archived by a button or by the command.
REJECT_FROM = ("new", "awaiting_llm", "scored", "not_selected", "shortlisted", "approved", "applied", "location_failed")


class RejectRefused(Exception):
    """The job cannot be rejected; nothing was changed. `gone`: the job is not open (any more)."""

    def __init__(self, message: str, gone: bool = False):
        super().__init__(message)
        self.gone = gone


@dataclass(frozen=True)
class Outcome:
    archive_id: int
    title: str
    company: str
    status: str
    approved_kinds: tuple[str, ...]


def clean_comment(text: str) -> str:
    """One line, no control characters."""
    return " ".join(text.split())


def refusal(job_id: int, status: str) -> str | None:
    """Why a job with this status is not rejected, or None when it can be."""
    if status in REJECT_FROM:
        return None
    return (f"Job {job_id} has the status '{status}'; a job that is further along is not rejected here. "
            "Nothing was changed.")


def approved_kinds(conn: sqlite3.Connection, job_id: int) -> tuple[str, ...]:
    """The kinds of the approved documents of the job (their PDFs stay on disk when it is archived)."""
    return tuple(sorted(r[0] for r in conn.execute("SELECT kind FROM documents WHERE job_id = ? AND status = 'approved'", (job_id,))))


def reject_job(conn: sqlite3.Connection, job_id: int, comment: str, approved_folder: str) -> Outcome:
    """Archive the open job as rejected by the user, under one write lock. `comment` is already cleaned and checked.
    Raises RejectRefused (nothing changed) or sqlite3.Error (the database is busy or failed)."""
    conn.execute("BEGIN IMMEDIATE")                           # the status is checked and the job moved under one write lock
    try:
        row = conn.execute("SELECT title, company, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise RejectRefused(f"Job {job_id} is not open any more. Nothing was changed.", gone=True)
        why = refusal(job_id, row["status"])
        if why:
            raise RejectRefused(why)
        kinds = approved_kinds(conn, job_id)
        archive_id = db.archive_job(conn, job_id, stage="user", reason_code="user_rejected", comment=comment or None,
                                    approved_folder=approved_folder)
        return Outcome(archive_id, row["title"], row["company"], row["status"], kinds)
    finally:
        if conn.in_transaction:
            conn.rollback()
