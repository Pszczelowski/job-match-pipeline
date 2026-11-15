"""Interview rounds and notes of a job (v0.98.0, migration 25; edit of a round v0.99.0): validation, add, edit, delete and list for the tables `interviews` and `job_notes`.

A round is stored as UTC `YYYY-MM-DDTHH:MM:SSZ` like every other stamp (db.utcnow); the user enters date and time in the config
`timezone` (Europe/Zurich) and sees them in it. A round has no number: the number is the position by `starts_at`. A note keeps the job
status at the time (`status_at`). A round can be added, edited and deleted, a note added and deleted (not edited). The text of notes and places is the user's own: it is
shown on the job page (autoescaped) and printed by `interviews --job`, and never logged.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db

KINDS = ("phone", "video", "onsite", "other")
PLACE_MAX = 300
ROUND_NOTES_MAX = 2000
NOTE_MAX = 2000
MIN_YEAR, MAX_YEAR = 2000, 2100


class Refused(ValueError):
    """The input was not accepted; the message is shown to the user as it is, and nothing was changed."""


_SPACES = str.maketrans({"\xa0": " ", "\u2007": " ", "\u202f": " "})       # non-breaking spaces of pasted mail text become plain spaces


def _clean(text: str, limit: int, what: str, multiline: bool) -> str:
    text = (text or "").translate(_SPACES)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if any(not (c.isprintable() or (multiline and c in "\n\t")) for c in text):
        raise Refused(f"The {what} holds a character that is not allowed. Nothing was changed.")
    if len(text) > limit:
        raise Refused(f"The {what} is {len(text)} characters long; the limit is {limit}. Nothing was changed.")
    return text


def to_utc(day: str, clock: str, tz_name: str) -> str:
    """`YYYY-MM-DD` and `HH:MM` read in `tz_name` -> UTC stamp. Refuses a bad date or time and a time the clock change skips."""
    try:
        local = datetime.strptime(f"{day.strip()} {clock.strip()}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise Refused("Enter the date as YYYY-MM-DD and the time as HH:MM. Nothing was changed.") from None
    if not MIN_YEAR <= local.year <= MAX_YEAR:
        raise Refused(f"The year must be between {MIN_YEAR} and {MAX_YEAR}. Nothing was changed.")
    zone = ZoneInfo(tz_name)
    aware = local.replace(tzinfo=zone)
    utc = aware.astimezone(timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != local:
        raise Refused("That local time does not exist (the clock changes then). Nothing was changed.")
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_local(starts_at: str, tz_name: str) -> tuple[str, str]:
    """UTC stamp -> (`YYYY-MM-DD`, `HH:MM`) in `tz_name`."""
    when = datetime.strptime(starts_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
    return when.strftime("%Y-%m-%d"), when.strftime("%H:%M")


def _job_status(conn: sqlite3.Connection, job_id: int) -> str | None:
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row["status"] if row else None


def parse_round(day: str, clock: str, kind: str, place: str, notes: str, tz_name: str) -> dict:
    """All validation of a round, no database: the values to store. Raises Refused (before anything is written)."""
    if kind not in KINDS:
        raise Refused("Choose phone, video, on site or other. Nothing was changed.")
    starts = to_utc(day, clock, tz_name)
    place = _clean(place, PLACE_MAX, "place", multiline=False)
    notes = _clean(notes, ROUND_NOTES_MAX, "notes", multiline=True)
    return {"starts_at": starts, "kind": kind, "place": place or None, "notes": notes or None}


def insert_round(conn: sqlite3.Connection, job_id: int, parsed: dict) -> int:
    """Insert a parsed round; does not commit (the caller's transaction decides)."""
    return conn.execute("INSERT INTO interviews (job_id, starts_at, kind, place, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (job_id, parsed["starts_at"], parsed["kind"], parsed["place"], parsed["notes"], db.utcnow())).lastrowid


def add_round(conn: sqlite3.Connection, job_id: int, day: str, clock: str, kind: str, place: str, notes: str, tz_name: str) -> int | None:
    """Store one round; returns its id, or None when the job is not open. Raises Refused on bad input (before anything is written)."""
    parsed = parse_round(day, clock, kind, place, notes, tz_name)
    with conn:
        if _job_status(conn, job_id) is None:
            return None
        return insert_round(conn, job_id, parsed)


def update_round(conn: sqlite3.Connection, job_id: int, round_id: int, day: str, clock: str, kind: str, place: str, notes: str, tz_name: str) -> bool:
    """Change one round of this job (validated first, nothing written when refused); False when the round does not exist."""
    parsed = parse_round(day, clock, kind, place, notes, tz_name)
    with conn:
        return conn.execute("UPDATE interviews SET starts_at = ?, kind = ?, place = ?, notes = ?, updated_at = ? WHERE id = ? AND job_id = ?",
                            (parsed["starts_at"], parsed["kind"], parsed["place"], parsed["notes"], db.utcnow(), round_id, job_id)).rowcount == 1


def delete_round(conn: sqlite3.Connection, job_id: int, round_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM interviews WHERE id = ? AND job_id = ?", (round_id, job_id)).rowcount == 1


def list_rounds(conn: sqlite3.Connection, job_id: int, tz_name: str) -> list[dict]:
    """The rounds of a job in the order of their start, with the number (position), local date and time."""
    out = []
    for n, r in enumerate(conn.execute("SELECT * FROM interviews WHERE job_id = ? ORDER BY starts_at, id", (job_id,)), start=1):
        day, clock = to_local(r["starts_at"], tz_name)
        out.append({"id": r["id"], "number": n, "date": day, "time": clock, "kind": r["kind"], "place": r["place"] or "", "notes": r["notes"] or ""})
    return out


def get_round(conn: sqlite3.Connection, job_id: int, round_id: int, tz_name: str) -> dict | None:
    """One round of this job as `list_rounds` shows it (the number is its position), or None."""
    return next((r for r in list_rounds(conn, job_id, tz_name) if r["id"] == round_id), None)


def get_note(conn: sqlite3.Connection, job_id: int, note_id: int, tz_name: str) -> dict | None:
    """One note of this job as `list_notes` shows it, or None."""
    return next((n for n in list_notes(conn, job_id, tz_name) if n["id"] == note_id), None)


def next_rounds(conn: sqlite3.Connection, job_ids: list[int], now_utc: str, tz_name: str) -> dict[int, str]:
    """For each job with a round after `now_utc` (a UTC stamp): its earliest such round as local `YYYY-MM-DD HH:MM`. A past round never counts."""
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    return {r[0]: " ".join(to_local(r[1], tz_name)) for r in conn.execute(
        f"SELECT job_id, MIN(starts_at) FROM interviews WHERE starts_at > ? AND job_id IN ({marks}) GROUP BY job_id", (now_utc, *job_ids))}


def add_note(conn: sqlite3.Connection, job_id: int, text: str) -> int | None:
    """Store one note with the job's status at this moment; returns its id, or None when the job is not open."""
    text = _clean(text, NOTE_MAX, "note", multiline=True)
    if not text:
        raise Refused("The note is empty. Nothing was changed.")
    with conn:
        status = _job_status(conn, job_id)
        if status is None:
            return None
        return conn.execute("INSERT INTO job_notes (job_id, status_at, text, created_at) VALUES (?, ?, ?, ?)",
                            (job_id, status, text, db.utcnow())).lastrowid


def delete_note(conn: sqlite3.Connection, job_id: int, note_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM job_notes WHERE id = ? AND job_id = ?", (note_id, job_id)).rowcount == 1


def list_notes(conn: sqlite3.Connection, job_id: int, tz_name: str) -> list[dict]:
    """The notes of a job, newest first; `created_at` is local date and time (`YYYY-MM-DD HH:MM`), like the rounds."""
    return [{"id": r["id"], "status_at": r["status_at"], "text": r["text"], "created_at": " ".join(to_local(r["created_at"], tz_name))}
            for r in conn.execute("SELECT * FROM job_notes WHERE job_id = ? ORDER BY id DESC", (job_id,))]
