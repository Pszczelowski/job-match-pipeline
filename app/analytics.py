"""Read-only numbers for the web pages Analytics and Runs/Health (goal.md 5.8, 5.9; web chunks 3 and 4).

Every function takes an open connection (the web app gives it a read-only one) and the config, runs plain SELECT
statements and returns plain dicts and lists. Nothing is written, there are no database views and no migration
(decided by the executor, goal.md 5.8). Times are UTC, as stored. `now` can be passed in tests.

What counts as a job here: the working jobs (`jobs`) and the archived ones (`job_archive`, rows that were restored are
history and are left out, because the restored job is a working job again). The archive keeps the score and the source
of a job, and its record holds `job.first_seen_at`, so archived jobs are included wherever the archive has the value.
The Health page reads only ids, times, counts and statuses: never a mail body, an address, a subject, an error text of
a report or an import, or the summary of a run.
"""
from __future__ import annotations

import os
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import backup, db, interviews, pipeline, postpone, report, scheduler, watchdog
from .config import resolve_path
from .statusflow import UNDO_NOTE

TOP_COMPANIES = 10
WEEKS = 12
DAYS = 14
UNKNOWN = "(unknown)"

# The status groups of "score band vs. outcome". Archived jobs are a group of their own (rejected by the filters or by the user).
GROUPS = (
    ("open", ("new", "awaiting_llm", "scored", "not_selected", "location_failed")),
    ("shortlisted / approved", ("shortlisted", "approved")),
    ("applied", ("applied",)),
    ("interview", ("interview",)),
    ("offer", ("offer",)),
    ("accepted", ("accepted",)),
    ("rejected by employer", ("rejected",)),
    ("withdrawn", ("withdrawn",)),
)
GROUP_NAMES = tuple(g for g, _ in GROUPS) + ("archived",)
BY_STATUS = {s: g for g, statuses in GROUPS for s in statuses}
APPLIED_EVER = ("applied", "interview", "offer", "accepted", "rejected", "withdrawn")      # statuses a job has after the application
RESPONDED = ("interview", "offer", "accepted", "rejected")                                   # the employer answered
BANDS = ("high", "medium", "low")

FIRST_SOURCE = "(SELECT job_id, source FROM job_sources WHERE id IN (SELECT MIN(id) FROM job_sources GROUP BY job_id))"


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def pct(value: float, maximum: float) -> int:
    """Bar width in whole percent of the largest value (at least 1 for a value above 0, so a small bar stays visible)."""
    if not maximum or value <= 0:
        return 0
    return max(1, min(100, round(100 * value / maximum)))


def _bars(rows: list[dict], key: str = "count") -> list[dict]:
    top = max((r[key] for r in rows), default=0)
    for r in rows:
        r["pct"] = pct(r[key], top)
    return rows


def _last_days(now: datetime, n: int = DAYS) -> list[str]:
    today = now.date()
    return [(today - timedelta(days=k)).isoformat() for k in range(n - 1, -1, -1)]


# --- Analytics ------------------------------------------------------------------------------------------------------

def funnel(conn: sqlite3.Connection) -> dict:
    """Working jobs per status in the order of `db.STATUSES`, and archived jobs per rejection stage."""
    have = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    stages = dict(conn.execute("SELECT rejection_stage, COUNT(*) FROM job_archive WHERE restored_at IS NULL GROUP BY rejection_stage").fetchall())
    working = [{"label": s, "count": have.get(s, 0)} for s in db.STATUSES]
    archived = [{"label": s, "count": stages.get(s, 0)} for s in db.ARCHIVE_STAGES]
    _bars(working + archived)                              # one scale for both lists
    return {"working": working, "archived": archived, "working_total": sum(r["count"] for r in working),
            "archived_total": sum(r["count"] for r in archived)}


def rejections(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Archive statistics: per reason code, per source, per company (top 10) and per ISO week (the last 12, empty weeks included)."""
    now = _now(now)
    live = "restored_at IS NULL"
    by_reason = _bars([{"label": r[0], "count": r[1]} for r in conn.execute(
        f"SELECT reason_code, COUNT(*) FROM job_archive WHERE {live} GROUP BY reason_code ORDER BY COUNT(*) DESC, reason_code")])
    by_source = _bars([{"label": r[0] or UNKNOWN, "count": r[1]} for r in conn.execute(
        f"SELECT source, COUNT(*) FROM job_archive WHERE {live} GROUP BY source ORDER BY COUNT(*) DESC, source")])
    by_company = _bars([{"label": r[0], "count": r[1]} for r in conn.execute(
        f"SELECT company, COUNT(*) FROM job_archive WHERE {live} GROUP BY company ORDER BY COUNT(*) DESC, company LIMIT ?", (TOP_COMPANIES,))])
    monday = now.date() - timedelta(days=now.date().weekday())
    starts = [monday - timedelta(weeks=k) for k in range(WEEKS - 1, -1, -1)]
    labels = [f"{s.isocalendar().year}-W{s.isocalendar().week:02d}" for s in starts]
    counts = dict.fromkeys(labels, 0)
    for (stamp,) in conn.execute(f"SELECT rejected_at FROM job_archive WHERE {live} AND rejected_at >= ?", (starts[0].isoformat(),)):
        try:
            iso = date.fromisoformat(stamp[:10]).isocalendar()
        except ValueError:
            continue
        label = f"{iso.year}-W{iso.week:02d}"
        if label in counts:
            counts[label] += 1
    by_week = _bars([{"label": k, "count": v} for k, v in counts.items()])
    return {"reason": by_reason, "source": by_source, "company": by_company, "week": by_week,
            "total": sum(r["count"] for r in by_reason)}


def jobs_per_source_per_day(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Jobs by the day they were first seen, per source (the source of the first sighting), for the last 14 days, working and archived."""
    now = _now(now)
    days = _last_days(now)
    first = days[0]
    cells: dict[str, dict[str, int]] = {}
    sql = f"""SELECT COALESCE(fs.source, ?) AS source, substr(j.first_seen_at, 1, 10) AS day, COUNT(*) FROM jobs j
              LEFT JOIN {FIRST_SOURCE} fs ON fs.job_id = j.id WHERE substr(j.first_seen_at, 1, 10) >= ? GROUP BY 1, 2"""
    rows = list(conn.execute(sql, (UNKNOWN, first)))
    rows += conn.execute(
        """SELECT COALESCE(source, ?), substr(json_extract(record, '$.job.first_seen_at'), 1, 10) AS day, COUNT(*) FROM job_archive
           WHERE restored_at IS NULL AND day >= ? GROUP BY 1, 2""", (UNKNOWN, first)).fetchall()
    for source, day, n in rows:
        cells.setdefault(source, {})
        cells[source][day] = cells[source].get(day, 0) + n
    table = []
    for source in sorted(cells):
        per_day = [cells[source].get(d, 0) for d in days]
        table.append({"label": source, "days": per_day, "count": sum(per_day)})
    _bars(table)
    totals = [sum(r["days"][i] for r in table) for i in range(len(days))]
    return {"days": days, "rows": table, "totals": totals, "total": sum(totals)}


def _scored_jobs(conn: sqlite3.Connection) -> list[dict]:
    """One entry per job that has a score: the latest score of a working job, or the score kept in the archive."""
    out = [{"source": r[0] or UNKNOWN, "score": r[1], "group": BY_STATUS[r[2]], "archived": False} for r in conn.execute(
        f"""SELECT fs.source, s.overall, j.status FROM jobs j JOIN scores s ON s.id = (SELECT MAX(id) FROM scores WHERE job_id = j.id)
            LEFT JOIN {FIRST_SOURCE} fs ON fs.job_id = j.id""")]
    out += [{"source": r[0] or UNKNOWN, "score": r[1], "group": "archived", "archived": True} for r in conn.execute(
        "SELECT source, score FROM job_archive WHERE score IS NOT NULL AND restored_at IS NULL")]
    return out


def score_by_source(conn: sqlite3.Connection) -> dict:
    """Average of the latest score and number of scored jobs per source (working jobs and archived jobs that kept a score)."""
    acc: dict[str, list[int]] = {}
    for j in _scored_jobs(conn):
        acc.setdefault(j["source"], []).append(j["score"])
    rows = [{"label": s, "count": len(v), "average": round(sum(v) / len(v), 1)} for s, v in sorted(acc.items())]
    rows.sort(key=lambda r: (-r["average"], r["label"]))
    for r in rows:
        r["pct"] = pct(r["average"], 100)                  # a score is out of 100
    return {"rows": rows, "total": sum(r["count"] for r in rows)}


def band_vs_outcome(conn: sqlite3.Connection, cfg: dict) -> dict:
    """Scored jobs by band (thresholds of `scoring.band_low_below` / `band_high_from`) and outcome group, and the response rate."""
    grid = {b: dict.fromkeys(GROUP_NAMES, 0) for b in BANDS}
    for j in _scored_jobs(conn):
        grid[report.band(j["score"], cfg)][j["group"]] += 1
    rows = []
    for b in BANDS:
        cells = grid[b]
        applied = sum(cells[g] for g in ("applied", "interview", "offer", "accepted", "rejected by employer", "withdrawn"))
        answered = sum(cells[g] for g in ("interview", "offer", "accepted", "rejected by employer"))
        rows.append({"band": b, "cells": [cells[g] for g in GROUP_NAMES], "total": sum(cells.values()),
                     "applied": applied, "answered": answered, "rate": _rate(answered, applied)})
    applied = sum(r["applied"] for r in rows)
    answered = sum(r["answered"] for r in rows)
    return {"groups": GROUP_NAMES, "rows": rows, "total": sum(r["total"] for r in rows), "applied": applied,
            "answered": answered, "rate": _rate(answered, applied),
            "low_below": cfg["scoring"]["band_low_below"], "high_from": cfg["scoring"]["band_high_from"]}


def _rate(answered: int, applied: int) -> str | None:
    return f"{round(100 * answered / applied)}%" if applied else None


def time_to_apply(conn: sqlite3.Connection) -> dict:
    """Days from the posting date (`jobs.posted_on`; the first-seen date when the job page gave none) to the first
    status_history row that set `applied`. Median, shortest and longest."""
    days, fallback = [], 0
    for posted, first_seen, changed in conn.execute(
            """SELECT j.posted_on, j.first_seen_at, MIN(h.changed_at) FROM status_history h JOIN jobs j ON j.id = h.job_id
               WHERE h.to_status = 'applied' GROUP BY j.id"""):
        try:
            start = date.fromisoformat(posted)
        except (TypeError, ValueError):
            start = date.fromisoformat(first_seen[:10])
            fallback += 1
        days.append(max(0, (date.fromisoformat(changed[:10]) - start).days))
    if not days:
        return {"n": 0, "median": None, "shortest": None, "longest": None, "from_first_seen": 0}
    return {"n": len(days), "median": statistics.median(days), "shortest": min(days), "longest": max(days), "from_first_seen": fallback}


def effective_path(history: list) -> list[tuple[str, str]]:
    """The statuses a job really went through, from its `status_history` rows in id order (each with `to_status`, `note`, `changed_at`;
    rows or mappings). A row with the note `web undo` takes the last step back; any other row adds a step. Returns [(status, changed_at)]."""
    path: list[tuple[str, str]] = []
    for h in history:
        if h["note"] == UNDO_NOTE:
            if path:
                path.pop()
        else:
            path.append((h["to_status"], h["changed_at"]))
    return path


def _outcome(path: list[tuple[str, str]]) -> str | None:
    """The one definition of the outcomes that follow an offer (v0.96.0, K5; `accepted` v0.98.0): `declined` = withdrawn straight after an
    offer, `offer_withdrawn` = rejected by the employer straight after an offer, `accepted` = accepted straight after an offer; None for every
    other path. A job accepted and then withdrawn or rejected ends in a path whose second-last step is `accepted`, so it counts as accepted
    only while that is its last step."""
    if len(path) >= 2 and path[-2][0] == "offer":
        return {"withdrawn": "declined", "rejected": "offer_withdrawn", "accepted": "accepted"}.get(path[-1][0])
    return None


def _gap_days(path: list[tuple[str, str]], start: str, ends: tuple[str, ...]) -> int | None:
    """Days from the latest `start` step before the first step with a status in `ends` to that step (None when the path has no such pair)."""
    for i, (status, at) in enumerate(path):
        if status in ends:
            begin = next((t for st, t in reversed(path[:i]) if st == start), None)
            if begin is None:
                return None
            return max(0, (date.fromisoformat(at[:10]) - date.fromisoformat(begin[:10])).days)
    return None


def _timing(days: list[int]) -> dict:
    if not days:
        return {"n": 0, "median": None, "shortest": None, "longest": None}
    return {"n": len(days), "median": statistics.median(days), "shortest": min(days), "longest": max(days)}


APP_STAGES = APPLIED_EVER                                                       # the statuses from the application on
APP_LABELS = {s: BY_STATUS[s] for s in APP_STAGES}                              # their group names ("rejected" is "rejected by employer")


def applications(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The applications block (v0.96.0): per stage the jobs now in it and the jobs that ever reached it, the interview / offer / employer
    rejection rates, three timings and the stage steps per ISO week (last 12). Working jobs only (archived jobs have no status
    history); undone steps are left out (`effective_path`). Read-only."""
    now = _now(now)
    paths: dict[int, list[tuple[str, str]]] = {}
    rows: dict[int, list] = {}
    for h in conn.execute("""SELECT h.job_id, h.to_status, h.note, h.changed_at FROM status_history h JOIN jobs j ON j.id = h.job_id
                             ORDER BY h.job_id, h.id"""):
        rows.setdefault(h["job_id"], []).append(h)
    for jid, hist in rows.items():
        paths[jid] = effective_path(hist)
    current = dict(conn.execute("SELECT id, status FROM jobs WHERE status IN ({})".format(",".join("?" * len(APP_STAGES))), APP_STAGES).fetchall())
    reached = {s: 0 for s in APP_STAGES}
    outcomes = {"declined": 0, "offer_withdrawn": 0, "accepted": 0}
    first_answer, to_interview, to_offer = [], [], []
    for jid in set(paths) | set(current):
        path = paths.get(jid, [])
        seen = {st for st, _ in path} | ({current[jid]} if jid in current else set())
        if not seen & set(APP_STAGES):
            continue
        for st in seen & set(APP_STAGES):
            reached[st] += 1
        if "applied" not in seen:                                  # a job that went further was applied for
            reached["applied"] += 1
        out = _outcome(path)
        if out:
            outcomes[out] += 1
        for bucket, start, ends in ((first_answer, "applied", ("interview", "rejected")), (to_interview, "applied", ("interview",)),
                                    (to_offer, "interview", ("offer",))):
            gap = _gap_days(path, start, ends)
            if gap is not None:
                bucket.append(gap)
    total = reached["applied"]
    monday = now.date() - timedelta(days=now.date().weekday())
    starts = [monday - timedelta(weeks=k) for k in range(WEEKS - 1, -1, -1)]
    labels = [f"{s.isocalendar().year}-W{s.isocalendar().week:02d}" for s in starts]
    per_week = {st: dict.fromkeys(labels, 0) for st in APP_STAGES}
    for path in paths.values():
        for st, at in path:
            if st in per_week and at[:10] >= starts[0].isoformat():
                try:
                    iso = date.fromisoformat(at[:10]).isocalendar()
                except ValueError:
                    continue
                label = f"{iso.year}-W{iso.week:02d}"
                if label in per_week[st]:
                    per_week[st][label] += 1
    stages = [{"label": APP_LABELS[st], "status": st, "now": sum(1 for v in current.values() if v == st), "reached": reached[st]} for st in APP_STAGES]
    return {
        "total": total, "stages": stages, "outcomes": outcomes,
        "rates": {"interview": _rate(reached["interview"], total), "offer": _rate(reached["offer"], reached["interview"]),
                  "accept": _rate(reached["accepted"], reached["offer"]),
                  "employer_rejection": _rate(reached["rejected"], total)},
        "timings": {"first_answer": _timing(first_answer), "interview": _timing(to_interview), "offer": _timing(to_offer)},
        "week": {"labels": labels, "series": {APP_LABELS[st]: [per_week[st][l] for l in labels] for st in APP_STAGES},
                 "total": sum(sum(v.values()) for v in per_week.values())},
    }


def interviews_block(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The interview rounds of working jobs (v0.99.0; archived rounds live in the archive JSON): rounds and jobs, rounds per kind, upcoming rounds
    (`starts_at` after `now`), rounds per job, days from the first application to the first round (the first `applied` step of the effective path;
    jobs without one are left out) and days between consecutive rounds of a job. Days are UTC dates like the other timings. Read-only."""
    stamp = _now(now).strftime("%Y-%m-%dT%H:%M:%SZ")
    per_job: dict[int, list[str]] = {}
    kinds = dict.fromkeys(interviews.KINDS, 0)
    upcoming = 0
    for r in conn.execute("SELECT job_id, starts_at, kind FROM interviews ORDER BY job_id, starts_at"):
        per_job.setdefault(r["job_id"], []).append(r["starts_at"])
        kinds[r["kind"]] += 1
        upcoming += r["starts_at"] > stamp
    history: dict[int, list] = {}
    for h in conn.execute("SELECT job_id, to_status, note, changed_at FROM status_history ORDER BY job_id, id"):
        if h["job_id"] in per_job:
            history.setdefault(h["job_id"], []).append(h)
    to_first, between = [], []
    for jid, starts in per_job.items():
        applied = next((at for st, at in effective_path(history.get(jid, [])) if st == "applied"), None)
        if applied is not None:
            to_first.append(max(0, (date.fromisoformat(starts[0][:10]) - date.fromisoformat(applied[:10])).days))
        between += [(date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days for a, b in zip(starts, starts[1:])]
    return {"rounds": sum(kinds.values()), "jobs": len(per_job), "kinds": kinds, "upcoming": upcoming,
            "per_job": _timing([len(v) for v in per_job.values()]), "to_first": _timing(to_first), "between": _timing(between)}


def adapter_health(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Per adapter and day (last 14 days, by the start of the run): runs that read fine and runs that failed (`error` set)."""
    now = _now(now)
    days = _last_days(now)
    cells: dict[str, dict[str, list[int]]] = {}
    for adapter, day, error in conn.execute(
            """SELECT a.adapter, substr(r.started_at, 1, 10), a.error IS NOT NULL FROM adapter_runs a JOIN runs r ON r.id = a.run_id
               WHERE substr(r.started_at, 1, 10) >= ?""", (days[0],)):
        cells.setdefault(adapter, {}).setdefault(day, [0, 0])[1 if error else 0] += 1
    rows = []
    for adapter in sorted(cells):
        per_day = [tuple(cells[adapter].get(d, (0, 0))) for d in days]
        ok, failed = sum(p[0] for p in per_day), sum(p[1] for p in per_day)
        rows.append({"label": adapter, "days": per_day, "ok": ok, "failed": failed, "count": ok + failed,
                     "ok_pct": pct(ok, ok + failed), "failed_pct": pct(failed, ok + failed)})
    return {"days": days, "rows": rows}


def analytics(conn: sqlite3.Connection, cfg: dict, now: datetime | None = None) -> dict:
    return {"funnel": funnel(conn), "rejections": rejections(conn, now), "per_day": jobs_per_source_per_day(conn, now),
            "score_source": score_by_source(conn), "bands": band_vs_outcome(conn, cfg), "apply": time_to_apply(conn),
            "applications": applications(conn, now), "interviews": interviews_block(conn, now), "adapters": adapter_health(conn, now)}


# --- Runs / Health -------------------------------------------------------------------------------------------------

def newest_backup(cfg: dict) -> dict | None:
    """Name, time and size of the newest `jobs_<timestamp>.db` in the backup folder (the folder is listed, no file is opened)."""
    folder = resolve_path(cfg["backup"]["dir"])
    newest = None
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.is_file() and backup.BACKUP_NAME.match(entry.name):
                    st = entry.stat()
                    if newest is None or entry.name > newest[0]:
                        newest = (entry.name, st.st_mtime, st.st_size)
    except OSError:
        return None
    if newest is None:
        return None
    return {"name": newest[0], "time": datetime.fromtimestamp(newest[1], timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"), "size": newest[2]}


def backup_folder_state(cfg: dict) -> str:
    """`ok` when the backup folder exists and can be listed, else `missing` (in the web container: the folder is not mounted)."""
    try:
        with os.scandir(resolve_path(cfg["backup"]["dir"])):
            return "ok"
    except OSError:
        return "missing"


def daily_run(conn: sqlite3.Connection, cfg: dict, now: datetime) -> dict:
    """The run lock, the last successful prepare and the scheduler heartbeat (v0.80.6), for the Daily run block."""
    last = conn.execute("SELECT MAX(finished_at) FROM runs WHERE status = 'ok' AND finished_at IS NOT NULL").fetchone()[0]
    last_ok = None
    if last:
        hours = (now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 3600
        last_ok = {"time": last, "hours": round(hours, 1), "late": hours > watchdog.MISSED_SCHEDULE_HOURS}
    beat = None
    try:
        mtime = scheduler.heartbeat_path(cfg).stat().st_mtime
        beat = {"time": datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
                "seconds": max(0, int(now.timestamp() - mtime))}
    except OSError:
        pass
    return {"lock": pipeline.lock_status(cfg, now), "last_ok_prepare": last_ok, "heartbeat": beat}


def health(conn: sqlite3.Connection, cfg: dict, now: datetime | None = None) -> dict:
    now = _now(now)
    since = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "runs": [dict(r) for r in conn.execute(
            "SELECT id, started_at, finished_at, status FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 20")],
        "adapter_runs": [dict(r) for r in conn.execute(
            """SELECT r.started_at, a.run_id, a.adapter, a.found, a.new_jobs, a.error IS NOT NULL AS failed, a.warning IS NOT NULL AS warned
               FROM adapter_runs a JOIN runs r ON r.id = a.run_id ORDER BY r.started_at DESC, a.id DESC LIMIT 20""")],
        "alerts": [dict(r) for r in conn.execute(
            """SELECT key, count, first_seen, last_seen, resolved_at, message FROM alerts ORDER BY last_seen DESC, id DESC LIMIT 20""")],
        "imports": [dict(r) for r in conn.execute("SELECT id, batch_id, status, imported_at FROM imports ORDER BY id DESC LIMIT 10")],
        "reports": [dict(r) for r in conn.execute(
            "SELECT batch_id, status, attempts, created_at, sent_at FROM reports ORDER BY created_at DESC, rowid DESC LIMIT 10")],
        "fetches": [dict(r) for r in conn.execute(
            "SELECT status, COUNT(*) AS count FROM description_fetches WHERE attempted_at >= ? GROUP BY status ORDER BY status", (since,))],
        "postponements": postpone.open_counts(conn),
        "backup": newest_backup(cfg),
        "backup_folder": backup_folder_state(cfg),
        **daily_run(conn, cfg, now),
        "schema": {"version": db.schema_version(conn), "expected": len(db.MIGRATIONS)},
    }


# --- Chart data (v0.89.0) --------------------------------------------------------------------------------------------
# Pure functions: the dicts of `analytics()` and `health()` in, one entry per chart out, keyed by the id of its canvas. Colours are
# token names of style.css (`--blue` ...), never hex values; `static/charts.js` reads the values from the CSS. A block with no data is
# left out, and the template shows its canvas only when the entry exists.
SOURCE_COLORS = ("blue", "green", "orange", "purple", "teal", "yellow", "pink", "mint", "brown", "cyan")
GROUP_COLORS = {"open": "grey", "shortlisted / approved": "blue", "applied": "indigo", "interview": "teal", "offer": "green", "accepted": "purple",
                "rejected by employer": "red", "withdrawn": "orange", "archived": "brown"}
BAND_COLORS = {"high": "green", "medium": "orange", "low": "grey"}
RUN_COLORS = {"ok": "green", "failed": "red"}


def _chart(kind: str, labels: list, datasets: list[dict], horizontal: bool = False, stacked: bool = False, **hints) -> dict:
    return {"type": kind, "labels": labels, "datasets": datasets, "horizontal": horizontal, "stacked": stacked, **hints}


def _one(label: str, data: list, color: str, **extra) -> dict:
    return {"label": label, "data": data, "color": color, **extra}


def _count_chart(rows: list[dict], unit: str, color: str, horizontal: bool = True) -> dict:
    return _chart("bar", [r["label"] for r in rows], [_one(unit, [r["count"] for r in rows], color)], horizontal=horizontal)


def chart_data(a: dict) -> dict:
    """The charts of the Analytics page from the dict of `analytics()`."""
    out: dict[str, dict] = {}
    f = a["funnel"]
    if f["working_total"]:
        out["chart-funnel-open"] = _count_chart(f["working"], "Jobs", "blue")
    if f["archived_total"]:
        out["chart-funnel-archived"] = _count_chart(f["archived"], "Jobs", "grey")
    r = a["rejections"]
    if r["total"]:
        out["chart-rej-reason"] = _count_chart(r["reason"], "Rejected", "blue")
        out["chart-rej-source"] = _count_chart(r["source"], "Rejected", "blue")
        out["chart-rej-company"] = _count_chart(r["company"], "Rejected", "blue")
        if any(w["count"] for w in r["week"]):
            out["chart-rej-week"] = _count_chart(r["week"], "Rejected", "indigo", horizontal=False)
    d = a["per_day"]
    if d["total"]:
        sets = [_one(row["label"], row["days"], SOURCE_COLORS[i % len(SOURCE_COLORS)]) for i, row in enumerate(d["rows"])]
        out["chart-per-day"] = _chart("bar", [day[5:] for day in d["days"]], sets, stacked=True)
    s = a["score_source"]
    if s["rows"]:
        b = a["bands"]
        colors = ["green" if row["average"] >= b["high_from"] else "grey" if row["average"] < b["low_below"] else "orange" for row in s["rows"]]
        out["chart-score-source"] = _chart("bar", [row["label"] for row in s["rows"]],
                                           [_one("Average score", [row["average"] for row in s["rows"]], "green", colors=colors)],
                                           horizontal=True, max=100)
    b = a["bands"]
    if b["total"]:
        sets = [_one(g, [row["cells"][i] for row in b["rows"]], GROUP_COLORS[g]) for i, g in enumerate(b["groups"])]
        out["chart-band-outcome"] = _chart("bar", [row["band"] for row in b["rows"]], sets, horizontal=True, stacked=True)
    if b["applied"]:
        rates = [round(100 * row["answered"] / row["applied"]) if row["applied"] else 0 for row in b["rows"]]
        out["chart-response-rate"] = _chart("bar", [row["band"] for row in b["rows"]], [_one("Response rate (%)", rates, "blue")],
                                            horizontal=True, max=100)
    w = a["applications"]["week"]
    if w["total"]:
        out["chart-app-week"] = _chart("bar", [label[5:] for label in w["labels"]],
                                       [_one(group, data, GROUP_COLORS[group]) for group, data in w["series"].items()], stacked=True)
    h = a["adapters"]
    if h["rows"]:
        ok = [sum(row["days"][i][0] for row in h["rows"]) for i in range(len(h["days"]))]
        failed = [sum(row["days"][i][1] for row in h["rows"]) for i in range(len(h["days"]))]
        out["chart-adapters"] = _chart("bar", [day[5:] for day in h["days"]],
                                       [_one("Ok", ok, "green"), _one("Failed", failed, "red")], stacked=True)
    return out


def _run_minutes(run: dict) -> float | None:
    """Length of a finished run in minutes; None while it is running (or when a time cannot be read)."""
    if not run["finished_at"]:
        return None
    try:
        start = datetime.strptime(run["started_at"], "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(run["finished_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (end - start).total_seconds()) / 60, 1)


def health_chart_data(h: dict) -> dict:
    """The charts of the Runs/Health page from the dict of `health()`."""
    out: dict[str, dict] = {}
    runs = list(reversed(h["runs"]))                          # `health()` lists the newest first; the chart reads left to right in time
    minutes = [_run_minutes(r) for r in runs]
    if any(m is not None for m in minutes):
        colors = [RUN_COLORS.get(r["status"], "grey") for r in runs]
        out["chart-runs"] = _chart("bar", [str(r["id"]) for r in runs], [_one("Minutes", minutes, "green", colors=colors)])
    if h["fetches"]:
        out["chart-fetches"] = _chart("bar", [f["status"] for f in h["fetches"]], [_one("Pages", [f["count"] for f in h["fetches"]], "blue")],
                                      horizontal=True)
    if any(h["postponements"].values()):
        out["chart-postponed"] = _chart("bar", list(h["postponements"]), [_one("Jobs", list(h["postponements"].values()), "orange")],
                                        horizontal=True)
    return out
