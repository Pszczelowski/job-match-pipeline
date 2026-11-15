"""Adding job descriptions by hand and scoring the job again (goal.md 5.1, 5.4).

A UBS alert mail carries no description, so such a job is scored from its title only and its overall score is
capped (config `scoring.no_description_cap`). The user can improve this job by job: open the job's link in the
browser, copy the text of the posting, and hand it over with `python -m app set-description` (or save it as
`exchange/descriptions/<job id>.txt` and run `import-descriptions`).

Two ways in. By hand (this module): reading a page in one's own browser and pasting it for one's own job search is
personal use. Automatically (`descriptions_fetch.py`): the Terms of use of ubs.com (checked 31 Oct 2026) prohibit
"systematic retrieval of content ... to create or compile ... a database ... whether through robots, spiders,
automatic devices or manual processes" without UBS's written permission, so that code runs only when the user, who
states that he holds this permission, has switched it on and written the basis into the config.

When a description is stored:
  * the language rule is run again on the new text (it was "not assessed" before); a C1 requirement archives the job;
  * for a job located only in Poland the Poland rule (remote / hybrid passes, on-site fails, `filters/poland.py`) is run
    again too, because a LinkedIn alert gives no description and the work mode may only be in the text; it needs no network.
    Any other job is not touched by this: the travel-time rule needs the web services and is not repeated here;
  * a job that already has a score, or waits for one, is marked `needs_rescore`; the next batch contains it again
    as a fresh, "re-evaluation" job, so it can enter the top 10 with its new score. The old score stays in `scores`.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import db
from .batch import exchange_dir
from .filters.base import load_context
from .filters.language import language, posting_is_german
from .filters.poland import poland_location
from .filters.runner import run_filters
from .location import location_checks, make_location_filter
from .travel import Providers

MIN_CHARS = 200
MAX_CHARS = 100_000
PAGE_START = "About the job"
PAGE_END = "Set alert for similar jobs"
RESCORE_STATUSES = ("scored", "not_selected", "awaiting_llm")


# One definition of "queued" (goal.md 5.5): the job is open and the batch row of its latest score has a queue place. A job whose
# text was pasted after that stays queued (its estimate is not shown or ranked) until the next batch scores it again; the
# queue page lists only the jobs that still wait for the text (`description_missing = 1`). `{j}` is the alias of the jobs table.
QUEUED = ("({j}.closed_at IS NULL AND EXISTS (SELECT 1 FROM scores qs JOIN batch_jobs qb ON qb.batch_id = qs.run_id AND qb.job_id = qs.job_id "
          "WHERE qs.job_id = {j}.id AND qs.id = (SELECT MAX(id) FROM scores WHERE job_id = {j}.id) AND qb.queue_rank IS NOT NULL))")


class DescriptionError(ValueError):
    """The description could not be stored."""


@dataclass
class DescriptionResult:
    job_id: int
    outcome: str                  # "stored" | "unchanged" | "archived"
    chars: int = 0
    rescore: bool = False
    language: str | None = None   # outcome of the language rule on the new text
    message: str = ""
    warning: str = ""             # manual paths: the parts not found (the text was stored anyway, v0.85.3)


def clean_text(text: str) -> str:
    """Normalise pasted text: line endings, control characters, trailing spaces, runs of blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def strip_page_clutter(text: str) -> str:
    """A whole LinkedIn page -> only the posting (v0.78.4). "Copy job text" falls back to the whole page when LinkedIn's page
    does not match its selectors. Both lines must stand alone and in this order, otherwise the text is returned as it came:
    all 16 stored page copies had them (copy of the NAS database, 10 Nov 2026), and no posting needs the words around them."""
    lines = text.split("\n")
    stripped = [line.strip() for line in lines]
    if PAGE_START not in stripped:
        return text
    start = stripped.index(PAGE_START) + 1
    if PAGE_END not in stripped[start:]:
        return text
    end = stripped.index(PAGE_END, start)
    while end > start and stripped[end - 1] in ("", "… more", "...more", "…more", "... more"):
        end -= 1
    return "\n".join(lines[start:end]).strip() if end > start else text


def _same_text_elsewhere(conn, job_id: int, company: str | None, cleaned: str) -> str | None:
    """A job of another company that already has exactly this posting: the sign of a text saved under the wrong job
    (job 767 had the text of job 729, found on 9 Nov 2026). Stored copies are compared without the page around them."""
    mine = (company or "").strip().casefold()
    rows = conn.execute("SELECT id, company, description FROM jobs WHERE id != ? AND description IS NOT NULL AND description != ''", (job_id,))
    for row in rows:
        other = (row["company"] or "").strip()
        if mine and other and other.casefold() != mine and strip_page_clutter(row["description"].strip()) == cleaned:
            return f"job {row['id']} ({other})"
    return None


def normalise_posted(value: str | None) -> str | None:
    """A posting date as the job pages state it -> ISO 'YYYY-MM-DD', or None when it cannot be read.

    Reads the SIX form 'Tue Oct 13 00:00:00 UTC 2026' and the LGT/ISO form '2025-11-19T01:00:00+01:00' (also a bare
    date). The date is taken as written, without a time-zone conversion. Never raises.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ]|$)", text)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        return datetime.strptime(text.replace(" UTC", ""), "%a %b %d %H:%M:%S %Y").date().isoformat()
    except ValueError:
        return None


def _store_posted(conn, job_id: int, posted_on: str | None) -> None:
    """Keep the first posting date: a stored date is never replaced, and None never overwrites anything."""
    iso = normalise_posted(posted_on)
    if iso:
        with conn:
            conn.execute("UPDATE jobs SET posted_on = ? WHERE id = ? AND posted_on IS NULL", (iso, job_id))


def _incomplete(text: str) -> list[str]:
    from .resolver.complete import manual_problems   # imported here: the resolver package imports this module
    return manual_problems(text)


def _missing_parts(text: str) -> list[str]:
    from .resolver.complete import missing_parts
    return missing_parts(text)


def apply_description(conn, cfg: dict, job_id: int, text: str, source: str = "manual", run_id: str | None = None,
                      check_language: bool = True, posted_on: str | None = None,
                      require_complete: bool = False) -> DescriptionResult:
    """Store a description. `require_complete` (manual paths): under 500 characters is refused; a missing tasks or
    requirements part is stored with a warning in `message` and a log event (v0.85.3, decided by the user on 13 Nov 2026)."""
    result = _apply_description(conn, cfg, job_id, text, source, run_id, check_language, posted_on, require_complete)
    if require_complete and result.outcome == "stored":
        if parts := _missing_parts(strip_page_clutter(clean_text(text or ""))):
            logging.getLogger(__name__).warning("event=description_parts_missing job=%s missing=%s", job_id, len(parts))
            result.warning = "; ".join(parts)
            result.message += "; warning: " + result.warning
    return result


def _apply_description(conn, cfg: dict, job_id: int, text: str, source: str, run_id: str | None,
                       check_language: bool, posted_on: str | None, require_complete: bool) -> DescriptionResult:
    job = conn.execute("SELECT status, description, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise DescriptionError(f"job {job_id} is not an open job (unknown or already archived)")
    cleaned = clean_text(text or "")
    if require_complete:                                             # manual paths only: the posting, whole, and for this job
        cleaned = strip_page_clutter(cleaned)
        if found := _incomplete(cleaned):                            # v0.72.0
            raise DescriptionError("the text is not complete: " + "; ".join(found))
        if other := _same_text_elsewhere(conn, job_id, job["company"], cleaned):
            logging.getLogger(__name__).warning("event=same_description job=%s other=%s", job_id, other)   # allowed since v0.78.10
    if len(cleaned) < MIN_CHARS:
        raise DescriptionError(f"the text has only {len(cleaned)} characters; paste the whole posting (at least {MIN_CHARS})")
    if len(cleaned) > MAX_CHARS:
        raise DescriptionError(f"the text has {len(cleaned)} characters; the limit is {MAX_CHARS}")
    _store_posted(conn, job_id, posted_on)
    if (job["description"] or "").strip() == cleaned:
        return DescriptionResult(job_id, "unchanged", len(cleaned), message="the job already has exactly this description")

    rescore = job["status"] in RESCORE_STATUSES
    with conn:
        conn.execute("UPDATE jobs SET description = ?, description_missing = 0, needs_rescore = ? WHERE id = ?",
                     (cleaned, 1 if rescore else 0, job_id))
        conn.execute("INSERT INTO description_log (job_id, source, chars, previous_status, rescore, set_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (job_id, source, len(cleaned), job["status"], int(rescore), db.utcnow()))

    if job["status"] in USER_STATUSES:                                # the user works on it: the text is stored, no rule archives it
        return DescriptionResult(job_id, "stored", len(cleaned), rescore, None,
                                 f"description stored; the job is {job['status']}, so the language and Poland rules were not run")
    if not check_language:        # a job that has not been through the filters yet: they run on the new text later
        return DescriptionResult(job_id, "stored", len(cleaned), rescore, None, "description stored; the filters run on it next")
    # archives the job if the text needs C1 or if it is an on-site job in Poland; poland_location returns [] for every other job
    outcome = run_filters(conn, job_id, cfg, run_id, filters=[language, poland_location])
    if outcome == "fail":
        from .results import refresh_selection
        refresh_selection(conn, cfg)
        why = conn.execute("SELECT reason_code FROM job_archive WHERE original_job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        message = ("the description shows an on-site job in Poland: the job was archived"
                   if why and why["reason_code"] == "poland_on_site" else
                   "the description shows a language requirement of C1 or above: the job was archived")
        return DescriptionResult(job_id, "archived", len(cleaned), False, outcome, message)
    note = "it will be scored again in the next batch" if rescore else "it is scored once it is in a batch"
    poland = poland_location(load_context(conn, job_id), cfg)
    where = f"; Poland rule: {poland[0].reason_code}" if poland else ""
    return DescriptionResult(job_id, "stored", len(cleaned), rescore, outcome, f"description stored{where}; {note}")


@dataclass
class RescoreItem:
    job_id: int
    title: str
    company: str
    status: str
    old: int                      # needs_rescore before
    new: int                      # needs_rescore after (the same as `old` when the job is skipped)
    skipped: str = ""             # why the job was left alone, empty when it is marked


def mark_rescore(conn, job_ids: list[int], dry_run: bool = False) -> list[RescoreItem]:
    """Mark open jobs `needs_rescore`, so the next batch contains them again as "re-evaluation" jobs (the same path a
    new description takes, `apply_description` and `batch.candidate_jobs`). Same rule as there: only a job that is
    `scored`, `not_selected` or `awaiting_llm` is marked; a job the user already works on (shortlisted, approved,
    applied, ...) or a new one is skipped with a reason. An unknown or archived id raises DescriptionError and nothing
    is changed. With `dry_run` nothing is written."""
    wanted = list(dict.fromkeys(job_ids))
    rows = {}
    for job_id in wanted:
        row = conn.execute("SELECT id, title, company, status, needs_rescore, closed_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            archived = conn.execute("SELECT 1 FROM job_archive WHERE original_job_id = ?", (job_id,)).fetchone()
            raise DescriptionError(f"job {job_id} is archived and cannot be scored again (restore it first)" if archived
                                   else f"job {job_id} is not an open job (unknown id)")
        rows[job_id] = row
    items: list[RescoreItem] = []
    for job_id in wanted:
        r = rows[job_id]
        old = int(r["needs_rescore"])
        if r["status"] not in RESCORE_STATUSES:
            why = (f"it is not scored yet (status {r['status']}); it goes into a batch by itself, if at all"
                   if r["status"] in ("new", "location_failed") else f"you already work on it (status {r['status']})")
            items.append(RescoreItem(job_id, r["title"], r["company"], r["status"], old, old, why))
            continue
        items.append(RescoreItem(job_id, r["title"], r["company"], r["status"], old, 1))
    if not dry_run:
        with conn:
            for it in items:
                if not it.skipped and it.old != it.new:
                    conn.execute("UPDATE jobs SET needs_rescore = 1 WHERE id = ?", (it.job_id,))
    return items


def recheck_language(conn, cfg: dict, run_id: str | None = None, dry_run: bool = False) -> dict[str, int]:
    """Run the language rule again on every open job that has a description, for example after the rule changed.

    A job whose language result is the same as its latest stored one is left alone, so running this twice changes
    nothing. A job that now needs C1 is archived, unless the user already works on it (`USER_STATUSES`): that job is
    only counted as skipped, as in recheck_poland. With `dry_run` nothing is written. Returns counts: checked,
    changed, archived, and skipped (only present when a job was skipped, so the older counts stay as they were).
    """
    counts = {"checked": 0, "changed": 0, "archived": 0}
    ids = [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE description_missing = 0 ORDER BY id")]
    for job_id in ids:
        counts["checked"] += 1
        new = sorted((r.outcome, r.reason_code) for r in language(load_context(conn, job_id), cfg))
        latest_run = conn.execute("SELECT MAX(id) FROM job_filter_results WHERE job_id = ? AND filter = 'language'", (job_id,)).fetchone()[0]
        old = sorted((r["outcome"], r["reason_code"]) for r in conn.execute(
            "SELECT outcome, reason_code FROM job_filter_results WHERE job_id = ? AND filter = 'language' AND created_at = "
            "(SELECT created_at FROM job_filter_results WHERE id = ?)", (job_id, latest_run))) if latest_run else None
        if new == old:
            continue
        if conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"] in USER_STATUSES:
            counts["checked"] -= 1
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        counts["changed"] += 1
        if dry_run:
            counts["archived"] += any(outcome == "fail" for outcome, _ in new)
            continue
        if run_filters(conn, job_id, cfg, run_id, filters=[language]) == "fail":
            counts["archived"] += 1
    if counts["archived"] and not dry_run:
        from .results import refresh_selection
        refresh_selection(conn, cfg)
    return counts


def cap_german_scores(conn, cfg: dict, apply: bool = False) -> list[dict]:
    """List the stored scores of German postings above `scoring.non_english_cap` (the latest score of each job).

    With `apply` they are lowered to the cap in one transaction; without it nothing is written. The German test is
    the one of the importer (`posting_is_german`) on the stored description.
    """
    cap = cfg["scoring"]["non_english_cap"]
    rows = []
    for r in conn.execute("SELECT j.id AS job_id, j.title, j.description, s.id AS score_id, s.overall AS old FROM jobs j "
                          "JOIN scores s ON s.id = (SELECT MAX(id) FROM scores WHERE job_id = j.id) "
                          "WHERE s.overall > ? ORDER BY j.id", (cap,)):
        if posting_is_german(r["description"], cfg):
            rows.append({"job_id": r["job_id"], "title": r["title"], "old": r["old"], "new": cap, "score_id": r["score_id"]})
    if apply and rows:
        with conn:
            conn.executemany("UPDATE scores SET overall = ? WHERE id = ?", [(cap, r["score_id"]) for r in rows])
    return rows


# a job the user already works on is never archived or sent to be scored again by a recheck
USER_STATUSES = ("shortlisted", "approved", "applied", "interview", "offer", "accepted", "rejected", "withdrawn")


def recheck_poland(conn, cfg: dict, run_id: str | None = None, dry_run: bool = False) -> dict[str, int]:
    """Run the Poland rule again on every open job located only in Poland that has already been through the filters.

    Why: the rule came after some jobs were filtered (a LinkedIn job "Poland, Remote" was a conditional
    `location_unspecified`), or the config patterns changed. No network is used. A job whose result (outcome and
    reason) is the same as its latest stored `location` result is left alone, so running this twice changes nothing.
    A changed result is stored as a new `location` row (the old rows stay as the audit trail); an on-site job is
    archived (`poland_on_site`); a job that is `scored`, `not_selected` or `awaiting_llm` is marked `needs_rescore`,
    because its location sub-score changes (prompt 1.8.0); a job the user already works on is only counted as skipped.
    With `dry_run` nothing is written. Returns counts: checked, changed, archived, rescore, skipped.
    """
    counts = {"checked": 0, "changed": 0, "archived": 0, "rescore": 0, "skipped": 0}
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id = jobs.id AND f.filter = 'location') "
        "ORDER BY id")]
    for job_id in ids:
        new = poland_location(load_context(conn, job_id), cfg)
        if not new:
            continue
        counts["checked"] += 1
        old = conn.execute("SELECT outcome, reason_code FROM job_filter_results WHERE job_id = ? AND filter = 'location' "
                           "ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if old and (old["outcome"], old["reason_code"]) == (new[0].outcome, new[0].reason_code):
            continue
        status = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]
        if status in USER_STATUSES:
            counts["skipped"] += 1
            continue
        counts["changed"] += 1
        if dry_run:
            counts["archived"] += new[0].outcome == "fail"
            counts["rescore"] += new[0].outcome != "fail" and status in RESCORE_STATUSES
            continue
        if run_filters(conn, job_id, cfg, run_id, filters=[poland_location]) == "fail":
            counts["archived"] += 1
        elif status in RESCORE_STATUSES:
            with conn:
                conn.execute("UPDATE jobs SET needs_rescore = 1 WHERE id = ?", (job_id,))
            counts["rescore"] += 1
    if counts["archived"] and not dry_run:
        from .results import refresh_selection
        refresh_selection(conn, cfg)
    return counts


@dataclass
class TravelItem:
    job_id: int
    title: str
    company: str
    status: str
    old_minutes: float | None     # best (lowest) newest train time stored for the job, None if there is none
    new_minutes: float | None
    old_outcome: str
    new_outcome: str
    action: str = ""              # "archive" | "rescore" | "" (nothing to do for this status)
    skipped: str = ""             # why the job is left alone, empty when it is handled


@dataclass
class TravelRecheck:
    items: list[TravelItem]
    checked: int = 0
    changed: int = 0              # changed result, job handled (not skipped)
    archived: int = 0
    rescore: int = 0
    skipped_user: int = 0         # changed, but you already work on the job
    skipped_uncached: int = 0     # a place or time is not in the cache, so nothing can be said offline


def _stored_train_minutes(conn, job_id: int) -> tuple[float | None, set[str]]:
    """(lowest newest train time over the job's locations, destinations whose newest train row is an exact timetable time)."""
    rows = conn.execute(
        "SELECT t.minutes, t.approximate, t.destination FROM job_travel_checks t JOIN job_locations l ON l.id = t.location_id "
        "WHERE l.job_id = ? AND t.mode = 'train' AND t.id = (SELECT MAX(x.id) FROM job_travel_checks x "
        "WHERE x.location_id = t.location_id AND x.mode = 'train')", (job_id,)).fetchall()
    minutes = [r["minutes"] for r in rows if r["minutes"] is not None]
    exact = {r["destination"] for r in rows if r["minutes"] is not None and not r["approximate"]}
    return (min(minutes) if minutes else None), exact


def recheck_travel(conn, cfg: dict, run_id: str | None = None, dry_run: bool = False) -> TravelRecheck:
    """Run the location rule again on every open job that has a stored location result, from cached data only.

    Why: since v0.68.0 a train time the timetable could not give is estimated from the exact car time (goal.md 5.3.1),
    so jobs stored with a straight-line approximation can change. No service is called: places and times come from the
    caches, expired entries included. A job whose place or car time is not cached is skipped and listed; so is a job
    whose stored train time was an exact timetable time that is no longer cached (it must not turn into an estimate).
    A job whose result (outcome, reason, comment) is the same as its latest stored `location` result is left alone, so
    running this twice changes nothing. A changed result is stored as a new `location` row and new travel checks (the
    old rows stay); a fail archives the job; a job that is `scored`, `not_selected` or `awaiting_llm` and no longer fails
    is marked `needs_rescore`; a job the user already works on is only listed. Poland, remote and no-location jobs are
    not touched. With `dry_run` nothing is written."""
    providers = Providers.offline()
    result = TravelRecheck([])
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id = jobs.id AND f.filter = 'location') "
        "ORDER BY id")]
    for job_id in ids:
        providers.transport.calls.clear()
        providers.osrm.calls.clear()
        new, checks = location_checks(conn, providers, cfg, load_context(conn, job_id), datetime.now(timezone.utc), persist=False)
        if not checks:
            continue
        result.checked += 1
        job = conn.execute("SELECT title, company, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        old_minutes, exact_places = _stored_train_minutes(conn, job_id)
        new_minutes = min((c.train.minutes for c in checks if c.train and c.train.minutes is not None), default=None)
        old = conn.execute("SELECT outcome, reason_code, comment FROM job_filter_results WHERE job_id = ? AND filter = 'location' "
                           "ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        item = TravelItem(job_id, job["title"], job["company"], job["status"], old_minutes, new_minutes,
                          old["outcome"], new[0].outcome)
        missing = ""
        if any(kind == "find" for kind, _ in providers.transport.calls):
            missing = "a place is not in the cache"
        elif providers.osrm.calls:
            missing = "the car time is not in the cache"
        elif any(kind == "train" and name in exact_places for kind, name in providers.transport.calls):
            missing = "the timetable time is not in the cache"
        if missing:
            item.skipped = missing
            result.skipped_uncached += 1
            result.items.append(item)
            continue
        if (old["outcome"], old["reason_code"], old["comment"]) == (new[0].outcome, new[0].reason_code, new[0].comment):
            continue
        if job["status"] in USER_STATUSES:
            item.skipped = f"you already work on it (status {job['status']})"
            result.skipped_user += 1
            result.items.append(item)
            continue
        result.changed += 1
        item.action = "archive" if new[0].outcome == "fail" else ("rescore" if job["status"] in RESCORE_STATUSES else "")
        result.items.append(item)
        result.archived += item.action == "archive"
        result.rescore += item.action == "rescore"
        if dry_run:
            continue
        run_filters(conn, job_id, cfg, run_id, filters=[make_location_filter(conn, providers)])
        if item.action == "rescore":
            with conn:
                conn.execute("UPDATE jobs SET needs_rescore = 1 WHERE id = ?", (job_id,))
    if result.archived and not dry_run:
        from .results import refresh_selection
        refresh_selection(conn, cfg)
    return result


def jobs_needing_description(conn, limit: int | None = None) -> list[dict]:
    """Open jobs without a description, best (highest latest score) first, then unscored jobs by id. `queued` marks a job
    in the missing-description queue (it would be in the top 10, goal.md 5.5): its title-only score is not shown."""
    rows = conn.execute(
        """SELECT j.id, j.status, j.title, j.company,
                  (SELECT overall FROM scores s WHERE s.job_id = j.id ORDER BY s.id DESC LIMIT 1) AS score,
                  (SELECT url FROM job_sources u WHERE u.job_id = j.id AND u.url IS NOT NULL ORDER BY u.id LIMIT 1) AS url,
                  (""" + QUEUED.format(j="j") + """) AS queued
           FROM jobs j WHERE j.description_missing = 1
           ORDER BY (score IS NULL), score DESC, j.id""").fetchall()
    out = [dict(r) | {"queued": bool(r["queued"])} for r in rows]
    return out[:limit] if limit else out


def partial_text(conn, job_id: int) -> tuple[str, str, str | None, str] | None:
    """The text of the newest partial posting the resolver found for a job: (text, source key, posting id, day), or None.
    It is only offered on the paste page; it is never stored as the description."""
    r = conn.execute("SELECT text, source_key, posting_id, day FROM description_resolutions "
                     "WHERE job_id = ? AND status = 'partial' AND text IS NOT NULL AND text != '' ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    return (r["text"], r["source_key"], r["posting_id"], r["day"]) if r else None


def tiers_tried(conn, job_id: int) -> list[dict]:
    """The latest outcome of the resolver per source for a job: [{tier, source, status, day}], oldest source first."""
    return [dict(r) for r in conn.execute(
        """SELECT tier, source_key AS source, status, day FROM description_resolutions d
           WHERE job_id = ? AND id = (SELECT MAX(id) FROM description_resolutions x WHERE x.job_id = d.job_id AND x.source_key = d.source_key)
           ORDER BY id""", (job_id,))]


def given_up_jobs(conn, max_days: int) -> list[dict]:
    """Open jobs without a description that the resolver gave up on: `max_days` or more days with an attempt on a named source
    or job board (the discovery step has its own days, like `resolver._attempt_days`). [{job_id, title, company, tiers}], by id."""
    from .resolver.common import DISCOVERY_PLATFORMS
    marks = ", ".join("?" for _ in DISCOVERY_PLATFORMS)
    rows = conn.execute(
        f"""SELECT j.id, j.title, j.company FROM jobs j
            WHERE j.description_missing = 1 AND j.closed_at IS NULL
              AND (SELECT COUNT(DISTINCT d.day) FROM description_resolutions d
                   WHERE d.job_id = j.id AND d.platform NOT IN ({marks})) >= ?
            ORDER BY j.id""", (*DISCOVERY_PLATFORMS, max_days)).fetchall()
    return [{"job_id": r["id"], "title": r["title"], "company": r["company"], "tiers": tiers_tried(conn, r["id"])} for r in rows]


def queued_jobs(conn) -> list[dict]:
    """The missing-description queue (goal.md 5.5, v0.72.0): open jobs without a description that were queued by the
    newest import of a batch (`batch_jobs.queue_rank`), newest batch first, then by place. A job leaves the queue when its
    description arrives, when it is archived or rejected (the row is deleted) or when it is closed; there is no expiry."""
    rows = conn.execute(
        """SELECT j.id, j.title, j.company, j.status, j.first_seen_at, bj.batch_id, bj.queue_rank AS place
           FROM batch_jobs bj JOIN jobs j ON j.id = bj.job_id JOIN batches b ON b.id = bj.batch_id
           WHERE bj.queue_rank IS NOT NULL AND j.description_missing = 1 AND """ + QUEUED.format(j="j") + """
             AND bj.batch_id = (SELECT run_id FROM scores WHERE job_id = j.id ORDER BY id DESC LIMIT 1)
           ORDER BY b.created_at DESC, b.id DESC, bj.queue_rank, j.id""").fetchall()
    seen: set[int] = set()
    out = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(dict(r))
    return out


def read_clipboard() -> str:
    """Text of the Windows clipboard (PowerShell)."""
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"],
            capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DescriptionError(f"cannot read the clipboard: {type(exc).__name__}") from None
    if done.returncode != 0:
        raise DescriptionError("cannot read the clipboard")
    return done.stdout.decode("utf-8", "replace")


def import_description_files(conn, cfg: dict, run_id: str | None = None) -> list[tuple[str, str]]:
    """Store every `exchange/descriptions/<job id>.txt`. Done files go to `done/`, failed ones to `failed/` with a reason.

    Returns (file name, message) pairs.
    """
    folder = exchange_dir(cfg) / "descriptions"
    out: list[tuple[str, str]] = []
    for path in sorted(folder.glob("*.txt")):
        try:
            if not path.stem.isdigit():
                raise DescriptionError("the file name must be the job id, for example 12.txt")
            result = apply_description(conn, cfg, int(path.stem), path.read_text(encoding="utf-8", errors="replace"), source="file",
                                       require_complete=True)
            target_dir, message = "done", result.message
        except DescriptionError as exc:
            target_dir, message = "failed", str(exc)
        target = folder / target_dir
        target.mkdir(exist_ok=True)
        dest = target / path.name
        n = 1
        while dest.exists():
            dest = target / f"{path.stem}.{n}{path.suffix}"
            n += 1
        shutil.move(str(path), str(dest))
        if target_dir == "failed":
            dest.with_name(dest.name + ".reason.txt").write_text(message, encoding="utf-8")
        out.append((path.name, message))
    return out
