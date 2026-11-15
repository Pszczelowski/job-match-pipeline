"""The daily report email (goal.md 5.7).

`send_report` builds the report for one imported batch (top jobs with score and band, one-line reason, gaps,
link, repost and conditional markers, whether anything outside the top 10 scored above the low-match band,
and a run summary) and sends it through Gmail SMTP with the app password from the environment. The same
batch is never reported twice unless asked. Every attempt is recorded in the `reports` table.

All text that comes from job postings is HTML-escaped; links must be http or https. The report carries no CV
text and no home address (the documents of slice 3b appear only as a marker per job: ready, blocked, missing, or
below the cut-off); the only personal data is what the mailbox owner already has (job titles, companies,
scores). The app password is read from GMAIL_APP_PASSWORD, never written to a log, a table or an error message.
"""
from __future__ import annotations

import html
import json
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from zoneinfo import ZoneInfo

from . import db, descriptions, documents, postpone
from .filters.base import current_results
from .filters.language import posting_is_german

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LABELS = {
    "location_unspecified": "location unclear", "location_unresolved": "location unclear",
    "travel_time_unavailable": "location unclear", "travel_time_approximate": "location unclear (approximate)",
    "language_required_below_c1": "language required", "language_level_unspecified": "language required, level not stated",
    "language_implied_by_posting": "German posting", "language_optional": "language is a plus",
    # a job in Poland: remote or hybrid is a normal pass, shown like the Swiss location, no warning (goal.md 5.3.1)
    "poland_remote": "remote (Poland)", "poland_hybrid": "hybrid (Poland)",
    "poland_work_mode_unknown": "location unclear (Poland, work mode not stated)",
    "poland_hybrid_presence_unknown": "location unclear (Poland, hybrid, office days not stated)",
    "travel_time_work_mode_unknown": "location unclear (work mode not stated)",
    "location_hybrid": "hybrid (up to 2 hours)",
}
RESCORED = "scored again with the full description"
CLOSED = "no longer listed on the employer's site"
FACT_ID = re.compile(r"\s*\[[a-z0-9]+(?:-[a-z0-9]+)+\]")   # CV fact ids such as [exp-northwind-zurich-analysis]: for the checks, not the reader


class ReportError(Exception):
    """The report could not be built or sent.

    When it comes from `smtp_send`, `attempts` is the number of tries made and `detail` the scrubbed reason alone
    (no "report" wording), so the alert mails (app/alerts.py) can reuse it.
    """

    def __init__(self, message: str, *, attempts: int = 0, detail: str | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.detail = detail or message


class ReportAuthError(ReportError):
    """Gmail refused the SMTP login, or there is no app password to log in with. Never retried."""


@dataclass
class ReportJob:
    rank: int
    job_id: int
    title: str
    company: str
    locations: list[str]
    score: int
    band: str
    summary: str
    matches: list[str]
    gaps: list[str]
    repost: bool
    notes: list[str]
    url: str | None
    documents: str = ""          # marker only, never the text: documents ready / blocked (N findings) / missing / below cut-off
    description_note: str = ""   # set when the job was scored without a description (title only, capped)
    language_note: str = ""      # set when the posting is written in German: the score is capped (goal.md 5.4)
    document_notes: list[str] = field(default_factory=list)    # warnings of ready documents, e.g. "cover letter is 2 pages (limit 1)"
    posted: str = ""            # 'posted 13 Oct 2026' when the job page stated a date, else empty


@dataclass
class ReportMissing:
    """A job that would be in the top 10 but has no description (goal.md 5.5, v0.72.0); its title-only score is not shown."""
    job_id: int
    title: str
    company: str
    place: int                   # its place in the would-be top 10
    url: str | None = None
    tiers: list[str] = field(default_factory=list)      # "T2 workday: no_match": what the resolver tried


@dataclass
class ReportGivenUp:
    """An open job without a description that the resolver gave up on (v0.86.0); it is not one of the queued jobs."""
    job_id: int
    title: str
    company: str
    url: str | None = None
    tiers: list[str] = field(default_factory=list)      # "T2 jobcloud: no_match": what the resolver tried


@dataclass
class Report:
    batch_id: str
    date: str
    jobs: list[ReportJob]
    outside_count: int
    outside_best: int | None
    summary: list[str]
    waiting: int
    cutoff_line: str = ""        # set when no top job reached tailoring.min_score (goal.md 5.6)
    subject: str = ""
    text: str = ""
    html: str = ""
    web_url: str = ""
    missing: list[ReportMissing] = field(default_factory=list)     # queued jobs of this batch (v0.72.0)
    missing_url: str = ""        # <web_base_url>/missing, empty without a base URL
    given_up: list[ReportGivenUp] = field(default_factory=list)    # jobs the resolver gave up on, not in `missing` (v0.86.0)


NO_DESCRIPTION = "no description: scored from the title only, capped at {cap}"
NON_ENGLISH = "posting in German: score capped at {cap} ({band})"


def band(score: int, cfg: dict) -> str:
    sc = cfg["scoring"]
    return "low" if score < sc["band_low_below"] else "high" if score >= sc["band_high_from"] else "medium"


def clean(items: list[str]) -> list[str]:
    return [t for t in (FACT_ID.sub("", i).strip() for i in items) if t]


def _pretty_date(batch_created: str, tz: str) -> str:
    moment = datetime.strptime(batch_created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))
    return f"{moment.day} {MONTHS[moment.month - 1]} {moment.year}"


def safe_url(url: str | None) -> str | None:
    return url if url and url.lower().startswith(("http://", "https://")) and not any(c in url for c in " \r\n\"'<>") else None


def posted_line(posted_on: str | None) -> str:
    try:
        d = date.fromisoformat(posted_on or "")
    except ValueError:
        return ""
    return f"posted {d.day} {d.strftime('%b %Y')}"


def notes(conn, job_id: int) -> list[str]:
    notes: list[str] = []
    for r in current_results(conn, job_id):
        if r["reason_code"] in LABELS and r["outcome"] in ("conditional", "pass"):
            label = LABELS[r["reason_code"]]
            if label not in notes:
                notes.append(label)
    return notes


def document_marker(conn, cfg: dict, job_id: int, rank: int, score: int, description_missing: bool = False) -> str:
    tl = cfg["tailoring"]
    if rank > tl["documents_top_n"]:
        return f"no documents (outside the first {tl['documents_top_n']})" if tl["documents_top_n"] else "no documents (switched off)"
    if description_missing and tl["require_description"]:
        return "no documents (no description)"
    if score < tl["min_score"]:
        return "below cut-off, no documents"
    kind, n = documents.state(conn, job_id, cfg)
    if kind == "blocked":
        return f"documents blocked ({n} finding{'s' if n != 1 else ''})"
    if kind == "unrendered":
        return "documents not rendered"
    return f"documents {kind}"


ACTED = ("applied", "interview", "offer", "accepted", "rejected", "withdrawn")     # the web Top 10 hides these (v0.82.0)


def collect(conn, cfg: dict, batch_id: str, extra: dict | None = None, hide_acted: bool = False) -> Report:
    batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ReportError(f"unknown batch {batch_id!r}")
    from . import results
    rep_cfg = cfg["report"]
    jobs: list[ReportJob] = []
    rows = conn.execute("SELECT job_id, rank, reevaluation FROM batch_jobs WHERE batch_id = ? AND selected = 1 ORDER BY rank", (batch_id,)).fetchall()
    if hide_acted:
        # web view only: jobs the user already acted on drop out and the next-ranked ones (the order of results._ranked: score,
        # skills, job id; not queued for a description) move up; `rank` shows the new place, the document marker keeps the stored one
        top_n = cfg["scoring"]["top_n"]
        hold = cfg["tailoring"]["require_description"]
        have = {r["job_id"] for r in rows}
        rows = [dict(r) for r in rows]
        for c in results._ranked(conn, batch_id):
            if c["job_id"] in have or (hold and (c["description_missing"] or c["title_only"] == 1)):
                continue
            q = conn.execute("SELECT queue_rank FROM batch_jobs WHERE batch_id = ? AND job_id = ?", (batch_id, c["job_id"])).fetchone()
            if q["queue_rank"] is None:
                rows.append({"job_id": c["job_id"], "rank": len(rows) + 1, "reevaluation": 0})
        shown = 0
        kept = []
        for r in rows:
            st = conn.execute("SELECT status FROM jobs WHERE id = ?", (r["job_id"],)).fetchone()
            if st is None or st["status"] in ACTED or not conn.execute("SELECT 1 FROM scores WHERE job_id = ?", (r["job_id"],)).fetchone():
                continue                               # acted on, archived (job row gone) or unscored: the next-ranked one takes the slot
            if shown < top_n:
                shown += 1
                kept.append(r)
        rows = kept
    for r in rows:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (r["job_id"],)).fetchone()
        score = conn.execute("SELECT * FROM scores WHERE job_id = ? ORDER BY id DESC LIMIT 1", (r["job_id"],)).fetchone()
        if job is None or score is None:
            continue                                   # archived by the user since the import
        details = json.loads(score["details"] or "{}")
        stored = conn.execute("SELECT title_only FROM batch_jobs WHERE batch_id = ? AND job_id = ?", (score["run_id"], r["job_id"])).fetchone()
        title_only = bool(job["description_missing"] or (stored and stored["title_only"] == 1))     # the score was made from the title only
        url = next((safe_url(s["url"]) for s in conn.execute(
            "SELECT url FROM job_sources WHERE job_id = ? ORDER BY id", (r["job_id"],)) if safe_url(s["url"])), None)
        jobs.append(ReportJob(
            rank=len(jobs) + 1 if hide_acted else r["rank"], job_id=r["job_id"], title=job["title"], company=job["company"],
            locations=[x["location_text"] for x in conn.execute(
                "SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (r["job_id"],))],
            score=score["overall"], band=band(score["overall"], cfg), summary=details.get("summary", ""),
            matches=clean(details.get("matches", []))[:rep_cfg["max_matches"]], gaps=clean(details.get("gaps", []))[:rep_cfg["max_gaps"]],
            repost=bool(job["repost"]), posted=posted_line(job["posted_on"]), notes=notes(conn, r["job_id"]) + ([RESCORED] if r["reevaluation"] else [])
            + ([CLOSED] if job["closed_at"] else []), url=url,
            documents=document_marker(conn, cfg, r["job_id"], r["rank"], score["overall"], title_only),
            description_note=NO_DESCRIPTION.format(cap=cfg["scoring"]["no_description_cap"]) if title_only else "",
            language_note=NON_ENGLISH.format(cap=cfg["scoring"]["non_english_cap"], band=band(cfg["scoring"]["non_english_cap"], cfg)) if posting_is_german(job["description"], cfg) else "",
            document_notes=documents.notes(conn, r["job_id"])))

    low = cfg["scoring"]["band_low_below"]
    hold = cfg["tailoring"]["require_description"]
    shown = {j.job_id for j in jobs}
    outside = []
    for c in results._ranked(conn, batch_id):        # the fresh scored jobs the selection and the web promotion rank (goal.md 5.5)
        if c["job_id"] in shown or c["overall"] < low or (hold and (c["description_missing"] or c["title_only"] == 1)):
            continue                                  # never ranked without a description (queued ones are listed as waiting)
        if hide_acted and conn.execute("SELECT status FROM jobs WHERE id = ?", (c["job_id"],)).fetchone()["status"] in ACTED:
            continue                                  # the web view hides acted jobs; the mail keeps the frozen batch
        outside.append(c["overall"])
    missing: list[ReportMissing] = []
    for q in conn.execute(
            """SELECT bj.job_id, bj.queue_rank, j.title, j.company FROM batch_jobs bj JOIN jobs j ON j.id = bj.job_id
               WHERE bj.batch_id = ? AND bj.queue_rank IS NOT NULL AND j.description_missing = 1
                 AND """ + descriptions.QUEUED.format(j="j") + """ ORDER BY bj.queue_rank""", (batch_id,)):
        link = next((safe_url(s["url"]) for s in conn.execute(
            "SELECT url FROM job_sources WHERE job_id = ? ORDER BY id", (q["job_id"],)) if safe_url(s["url"])), None)
        tiers = [f"{t['tier']} {t['source']}: {t['status']}" for t in descriptions.tiers_tried(conn, q["job_id"])]
        missing.append(ReportMissing(q["job_id"], q["title"], q["company"], q["queue_rank"], link, tiers))

    queued_ids = {m.job_id for m in missing}
    given_up: list[ReportGivenUp] = []
    for g in descriptions.given_up_jobs(conn, cfg["resolver"]["max_attempt_days"]):
        if g["job_id"] in queued_ids:
            continue
        link = next((safe_url(s["url"]) for s in conn.execute(
            "SELECT url FROM job_sources WHERE job_id = ? ORDER BY id", (g["job_id"],)) if safe_url(s["url"])), None)
        given_up.append(ReportGivenUp(g["job_id"], g["title"], g["company"], link,
                                      [f"{t['tier']} {t['source']}: {t['status']}" for t in g["tiers"]]))

    waiting = conn.execute(
        "SELECT COUNT(*) FROM batch_jobs bj JOIN jobs j ON j.id = bj.job_id WHERE bj.batch_id = ? AND j.status = 'awaiting_llm'",
        (batch_id,)).fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM batch_jobs bj JOIN scores s ON s.job_id = bj.job_id AND s.run_id = bj.batch_id WHERE bj.batch_id = ?",
        (batch_id,)).fetchone()[0]
    rejected = conn.execute(
        "SELECT reason_code, COUNT(*) AS n FROM job_archive WHERE run_id = ? AND rejection_stage = 'filter' "
        "GROUP BY reason_code ORDER BY n DESC, reason_code", (batch_id,)).fetchall()

    summary = []
    extra = extra or {}
    if "seen" in extra or "new" in extra:
        summary.append(f"Jobs seen: {extra.get('seen', '?')}; new: {extra.get('new', '?')}")
    for a in extra.get("adapters", []):
        counts = f"{a.get('found', 0)} found, {a.get('new', 0)} new"
        if a.get("error"):
            summary.append(f"{a['name']}: failed ({a['error']})")
        elif a.get("warning"):
            summary.append(f"{a['name']}: warning ({a['warning']}); {counts}")
        else:
            summary.append(f"{a['name']}: {counts}")
    summary += extra.get("review_lines", [])
    summary.append(f"Batch: {batch['job_count']} jobs ({batch['fresh_count']} new, {batch['job_count'] - batch['fresh_count']} carried over); scored: {scored}; waiting for a score: {waiting}")
    held = conn.execute("SELECT p.reason, COUNT(*) AS n FROM postponements p JOIN jobs j ON j.id = p.job_id "
                        "WHERE p.batch_id = ? AND p.resolved_at IS NULL GROUP BY p.reason", (batch_id,)).fetchall()
    if held:
        n_held = sum(r["n"] for r in held)
        label = postpone.LABELS["usage_limit" if any(r["reason"] == "usage_limit" for r in held) else "no_result"]
        # the window of a job runs from its first fresh postponement in any batch (postpone.fresh_window_open); the line
        # names the earliest end among this batch's open jobs
        until = conn.execute(
            "SELECT MIN((SELECT MIN(q.noted_at) FROM postponements q WHERE q.job_id = p.job_id AND q.fresh = 1)) "
            "FROM postponements p JOIN jobs j ON j.id = p.job_id WHERE p.batch_id = ? AND p.resolved_at IS NULL", (batch_id,)).fetchone()[0]
        when = ""
        if until:
            end = datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(hours=postpone.FRESH_HOURS)
            when = f"; they stay in the top-10 race until {end.astimezone(ZoneInfo(cfg['timezone'])).strftime('%d %b %Y %H:%M')} and go into the next batch"
        summary.append(f"Postponed: {n_held} job(s) ({label}){when}")
    if rejected:
        total = sum(r["n"] for r in rejected)
        summary.append(f"Rejected by the filters this run: {total} (" + ", ".join(f"{r['reason_code']} {r['n']}" for r in rejected) + ")")
    else:
        summary.append("Rejected by the filters this run: 0")

    tz = cfg["timezone"]
    report = Report(batch_id=batch_id, date=_pretty_date(batch["created_at"], tz), jobs=jobs, outside_count=len(outside),
                    outside_best=max(outside) if outside else None, summary=summary, waiting=waiting)
    base = rep_cfg["web_base_url"].rstrip("/")
    report.web_url = f"{base}/" if base else ""
    report.missing, report.missing_url = missing, f"{base}/missing" if base else ""
    report.given_up = given_up
    n = len(jobs)
    report.subject = f"{rep_cfg['subject_prefix']}: {n} job match{'es' if n != 1 else ''} for {report.date}" if n else \
        f"{rep_cfg['subject_prefix']}: {len(missing)} job(s) wait for a description ({report.date})" if missing else \
        f"{rep_cfg['subject_prefix']}: no job matches for {report.date}"
    tl = cfg["tailoring"]
    if jobs and tl["documents_top_n"] and not any(j.score >= tl["min_score"] for j in jobs):
        report.cutoff_line = f"No job reached the cut-off of {tl['min_score']}; no CV or cover letter was written."
    report.text, report.html = render(report, cfg)
    return report


def outside_line(r: Report, cfg: dict) -> str:
    low = cfg["scoring"]["band_low_below"]
    if r.outside_count:
        return (f"{r.outside_count} job(s) outside the top {cfg['scoring']['top_n']} scored {low} or more "
                f"(best: {r.outside_best}). They are stored as not selected.")
    return f"No job outside the top {cfg['scoring']['top_n']} scored {low} or more."


def render(r: Report, cfg: dict) -> tuple[str, str]:
    """(plain text, HTML). Job text is escaped in the HTML."""
    text = [f"Recruitment report for {r.date}", f"Batch {r.batch_id}", ""]
    if r.cutoff_line:
        text += [r.cutoff_line, ""]
    if r.jobs:
        text.append(f"TOP {len(r.jobs)}" if len(r.jobs) < cfg["scoring"]["top_n"] else f"TOP {cfg['scoring']['top_n']}")
    else:
        text.append("No job matches in this batch." + (f" {r.waiting} job(s) are still waiting for a score." if r.waiting else ""))
    for j in r.jobs:
        flags = (["REPOST"] if j.repost else []) + [n.upper() for n in j.notes] + ([j.documents.upper()] if j.documents else [])
        text += ["", f"{j.rank}. [{j.band.upper()} {j.score}] {j.title}, {j.company}"
                     + (f" ({', '.join(j.locations)})" if j.locations else "") + (f"  <{'; '.join(flags)}>" if flags else "")]
        if j.posted:
            text += ["", f"   {j.posted}"]
        if j.description_note:
            text += ["", f"   Note: {j.description_note}"]
        if j.language_note:
            text += ["", f"   Note: {j.language_note}"]
        if j.document_notes:
            text += ["", "   Documents: " + "; ".join(j.document_notes)]
        if j.summary:
            text += ["", f"   {j.summary}"]
        if j.matches:
            text += ["", "   MATCHES"] + [f"     + {m}" for m in j.matches]
        if j.gaps:
            text += ["", "   GAPS"] + [f"     - {g}" for g in j.gaps]
        if j.url:
            text += ["", f"   Link: {j.url}"]
        text.append("   " + "-" * 40)
    if r.missing:
        text += ["", f"WAITING FOR A DESCRIPTION ({len(r.missing)}): these would be in the top 10; add the text"
                 + (f" at {r.missing_url}" if r.missing_url else " (python -m app missing, or the web app, Missing descriptions)")]
        for m in r.missing:
            text += [f"  - {m.title}, {m.company} (would-be place {m.place})" + (f"  {m.url}" if m.url else "")
                     + (f"  [tried: {'; '.join(m.tiers)}]" if m.tiers else "")]
    if r.given_up:
        text += ["", f"GIVEN UP ({len(r.given_up)}): the resolver found no description in {cfg['resolver']['max_attempt_days']} days of trying; add the text"
                 + (f" at {r.missing_url}" if r.missing_url else " (python -m app missing, or the web app, Missing descriptions)")]
        for g in r.given_up:
            text += [f"  - {g.title}, {g.company}" + (f"  {g.url}" if g.url else "") + (f"  [tried: {'; '.join(g.tiers)}]" if g.tiers else "")]
    text += ["", outside_line(r, cfg), "", "Run summary"] + [f"- {s}" for s in r.summary]
    if r.waiting and r.jobs:
        text.append(f"- {r.waiting} job(s) are still waiting for a score")
    if r.web_url:
        text += ["", f"All jobs: {r.web_url}"]

    e = html.escape
    parts = ['<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;color:#222;line-height:1.5">',
             f'<h2 style="margin:0 0 4px">Recruitment report for {e(r.date)}</h2>',
             f'<div style="color:#666;font-size:13px">Batch {e(r.batch_id)}</div>']
    if r.cutoff_line:
        parts.append(f'<p style="margin:12px 0"><b>{e(r.cutoff_line)}</b></p>')
    if not r.jobs:
        parts.append("<p>No job matches in this batch." + (f" {r.waiting} job(s) are still waiting for a score." if r.waiting else "") + "</p>")
    colours = {"high": "#1a7f37", "medium": "#9a6700", "low": "#8a8a8a"}
    def bullets(label: str, items: list[str], colour: str) -> str:
        lis = "".join(f'<li style="margin:0 0 4px">{e(i)}</li>' for i in items)
        return (f'<div style="margin:10px 0 0;font-size:12px;font-weight:bold;letter-spacing:.5px;color:{colour}">{label}</div>'
                f'<ul style="margin:4px 0 0;padding-left:20px;font-size:14px;color:#333">{lis}</ul>')

    for j in r.jobs:
        title = e(j.title)
        if j.url:
            title = f'<a href="{e(j.url, quote=True)}" style="color:#0b57d0;text-decoration:none">{title}</a>'
        flags = "".join(f'<span style="display:inline-block;background:#eef;border-radius:3px;padding:1px 6px;margin:0 6px 0 0;font-size:12px">{e(f)}</span>'
                        for f in (["repost"] if j.repost else []) + j.notes + ([j.documents] if j.documents else []))
        where = f' &middot; {e(", ".join(j.locations))}' if j.locations else ""
        parts.append(
            f'<div style="border:1px solid #ddd;border-left:5px solid {colours[j.band]};border-radius:4px;padding:12px 14px;margin:16px 0">'
            f'<div style="font-size:12px;font-weight:bold;color:{colours[j.band]};text-transform:uppercase">'
            f'#{j.rank} &middot; {j.score} &middot; {e(j.band)}</div>'
            f'<div style="font-size:17px;font-weight:bold;margin:2px 0">{title}</div>'
            f'<div style="font-size:14px;color:#555">{e(j.company)}{where}</div>'
            + (f'<div style="font-size:13px;color:#777">{e(j.posted)}</div>' if j.posted else "")
            + (f'<div style="margin:6px 0 0">{flags}</div>' if flags else ""))
        if j.description_note:
            parts.append(f'<p style="margin:8px 0 0;font-size:13px;color:#9a6700">{e(j.description_note.capitalize())}</p>')
        if j.language_note:
            parts.append(f'<p style="margin:8px 0 0;font-size:13px;color:#9a6700">{e(j.language_note[:1].upper() + j.language_note[1:])}</p>')
        if j.document_notes:
            parts.append(f'<p style="margin:8px 0 0;font-size:13px;color:#9a6700">Documents: {e("; ".join(j.document_notes))}</p>')
        if j.summary:
            parts.append(f'<p style="margin:10px 0 0;font-size:14px">{e(j.summary)}</p>')
        if j.matches:
            parts.append(bullets("MATCHES", j.matches, "#1a7f37"))
        if j.gaps:
            parts.append(bullets("GAPS", j.gaps, "#9a6700"))
        parts.append("</div>")
    if r.missing:
        def item(m: ReportMissing) -> str:
            name = f'<a href="{e(m.url, quote=True)}">{e(m.title)}</a>' if m.url else e(m.title)
            tried = e(" [tried: " + "; ".join(m.tiers) + "]") if m.tiers else ""
            return f"<li>{name}, {e(m.company)} (would-be place {m.place}){tried}</li>"
        lines = "".join(item(m) for m in r.missing)
        where = (f' at <a href="{e(r.missing_url, quote=True)}">{e(r.missing_url)}</a>' if r.missing_url else "")
        parts.append(f'<div style="border:1px solid #e0c36a;border-radius:4px;padding:10px 14px;margin:16px 0;font-size:14px">'
                     f'<b>Waiting for a description ({len(r.missing)})</b>: these would be in the top 10; add the text{where}.'
                     f'<ul style="margin:4px 0">{lines}</ul></div>')
    if r.given_up:
        def given(g: ReportGivenUp) -> str:
            name = f'<a href="{e(g.url, quote=True)}">{e(g.title)}</a>' if g.url else e(g.title)
            tried = e(" [tried: " + "; ".join(g.tiers) + "]") if g.tiers else ""
            return f"<li>{name}, {e(g.company)}{tried}</li>"
        parts.append(f'<div style="border:1px solid #d0a0a0;border-radius:4px;padding:10px 14px;margin:16px 0;font-size:14px">'
                     f'<b>Given up ({len(r.given_up)})</b>: the resolver found no description in {cfg["resolver"]["max_attempt_days"]} days of trying; '
                     f'add the text.<ul style="margin:4px 0">{"".join(given(g) for g in r.given_up)}</ul></div>')
    parts.append(f'<p style="border-top:1px solid #ddd;padding-top:10px">{e(outside_line(r, cfg))}</p>')
    parts.append('<div style="font-size:13px;color:#555"><b>Run summary</b><ul style="margin:4px 0">'
                 + "".join(f"<li>{e(s)}</li>" for s in r.summary)
                 + (f"<li>{r.waiting} job(s) are still waiting for a score</li>" if r.waiting and r.jobs else "") + "</ul></div>")
    if r.web_url:
        parts.append(f'<p><a href="{e(r.web_url, quote=True)}">All jobs</a></p>')
    parts.append("</div>")
    return "\n".join(text) + "\n", "\n".join(parts)


def new_message(cfg: dict, subject: str) -> EmailMessage:
    """An empty message from the configured sender to the configured recipient (the report and the alert mails)."""
    em = cfg["email"]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, em["sender"], em["recipient"]
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=em["sender"].split("@", 1)[1])
    return msg


def build_message(cfg: dict, r: Report) -> EmailMessage:
    msg = new_message(cfg, r.subject)
    msg["X-Recruitment-Batch"] = r.batch_id
    msg.set_content(r.text)
    msg.add_alternative(r.html, subtype="html")
    return msg


def _scrub(message: str, secret: str | None) -> str:
    return message.replace(secret, "***") if secret else message


def smtp_send(cfg: dict, msg: EmailMessage, smtp_factory=smtplib.SMTP, sleep=time.sleep) -> int:
    """Send one message through Gmail SMTP with the app password from the environment. Returns the number of tries.

    Authentication errors are not retried; other failures are retried up to `email.send_attempts` times with
    `email.retry_wait_seconds` between tries. The app password never appears in an error. Raises `ReportAuthError`
    (login refused, or no password) or `ReportError`. Shared by the report and the alert mails.
    """
    em = cfg["email"]
    user = os.environ.get("GMAIL_USER") or em["sender"]
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        raise ReportAuthError("GMAIL_APP_PASSWORD is not set; the report was not sent", detail="GMAIL_APP_PASSWORD is not set")
    last = ""
    for attempt in range(1, em["send_attempts"] + 1):
        try:
            with smtp_factory(em["smtp_host"], em["smtp_port"], timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(user, password)
                smtp.send_message(msg)
            return attempt
        except smtplib.SMTPAuthenticationError as exc:
            last = _scrub(f"Gmail refused the login ({exc.smtp_code}); check the app password", password)
            raise ReportAuthError(last, attempts=attempt) from None
        except (smtplib.SMTPException, OSError) as exc:
            last = _scrub(f"{type(exc).__name__}: {exc}", password)
            if attempt < em["send_attempts"]:
                sleep(em["retry_wait_seconds"])
    raise ReportError(f"report not sent after {em['send_attempts']} attempts: {last}", attempts=em["send_attempts"], detail=last)


def send_update(conn, cfg: dict, batch_id: str, job_ids: list[int], smtp_factory=smtplib.SMTP,
                sleep=time.sleep, documents: list[tuple] | None = None) -> str:
    """A short update mail for a results file that arrived after the batch's report was sent (goal.md 5.10, decided
    3 Nov 2026): only the jobs that file scored and the documents it stored (`documents`: (job, kind, status, findings)
    rows of the import), once per file (an imported file is never imported again). Plain text
    only, so job text is never rendered as HTML. Raises ReportError like `send_report`."""
    batch = conn.execute("SELECT created_at FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise ReportError(f"unknown batch {batch_id!r}")
    date_text = _pretty_date(batch["created_at"], cfg["timezone"])
    lines = [f"Update for the report of {date_text}", f"Batch {batch_id}", "",
             (f"{len(job_ids)} more job(s) were scored after the report was sent:" if job_ids
              else "Documents were added after the report was sent:")]
    for job_id in job_ids:
        job = conn.execute("SELECT title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
        score = conn.execute("SELECT overall, details FROM scores WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if job is None or score is None:
            continue
        rank = conn.execute("SELECT rank FROM batch_jobs WHERE batch_id = ? AND job_id = ? AND selected = 1", (batch_id, job_id)).fetchone()
        url = next((safe_url(s["url"]) for s in conn.execute("SELECT url FROM job_sources WHERE job_id = ? ORDER BY id", (job_id,))
                    if safe_url(s["url"])), None)
        summary = json.loads(score["details"] or "{}").get("summary", "")
        lines += ["", f"[{band(score['overall'], cfg).upper()} {score['overall']}] {job['title']}, {job['company']}"
                      + (f"  <TOP 10, rank {rank['rank']}>" if rank else "")]
        if summary:
            lines.append(f"   {summary}")
        if url:
            lines.append(f"   Link: {url}")
    docs = [d for d in (documents or [])]
    if docs:
        lines += ["", "Documents stored (job, kind, status):"]
        for job_id, kind, status, _findings in docs:
            job = conn.execute("SELECT title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
            lines.append(f"   {job['title']}, {job['company']}: {kind} ({status})" if job else f"   job {job_id}: {kind} ({status})")
    base = cfg["report"]["web_base_url"].rstrip("/")
    if base:
        lines += ["", f"All jobs: {base}/"]
    what = f"{len(job_ids)} more job(s) scored" if job_ids else "documents added"
    msg = new_message(cfg, f"{cfg['report']['subject_prefix']}: update, {what} for {date_text}")
    msg["X-Recruitment-Batch"] = batch_id
    msg.set_content("\n".join(lines) + "\n")
    smtp_send(cfg, msg, smtp_factory, sleep)
    return "sent"


REVISION_REASONS = {
    "usage_limit": "the Claude usage limit was reached", "auth": "the headless Claude login was refused",
    "timeout": "the llm service did not answer in time", "error": "the writer or the reviewer did not finish",
    "invalid": "the revised document was not valid or the request could not be sent",
    "unchanged": "the writer changed nothing",
}


def send_revision_mail(conn, cfg: dict, row, done: dict | None = None, smtp_factory=smtplib.SMTP, sleep=time.sleep) -> str:
    """The mail after a revision request ended (v0.85.0, goal.md 5.10a): stored as a draft (with or without its PDF), stored but still
    blocked (the findings left), failed (the reason) or stale. Plain text only, no document text and never the home address.
    `row` is the finished `revision_requests` row. Raises ReportError like `send_report`."""
    doc = conn.execute("SELECT d.id, d.job_id, d.status, d.findings, d.pdf_path, j.title, j.company FROM documents d "
                       "JOIN jobs j ON j.id = d.job_id WHERE d.id = ?", (row["document_id"],)).fetchone()
    what = row["kind"].replace("_", " ")
    who = f"{doc['title']}, {doc['company']}" if doc else "a job that is gone"
    lines = [f"Ask the writer to fix: the {what} of {who}", ""]
    if row["status"] == "stored" and row["outcome"] == "draft":
        lines.append("The writer's fix passed the checks and is stored as a draft."
                     + (" The PDF was built." if doc and doc["pdf_path"] else " No PDF could be built; run render-documents or check the job page."))
        subject = f"document fixed, {what}"
    elif row["status"] == "stored":
        left = [f for f in json.loads(doc["findings"]) if f["kind"] not in documents.SHOWN_AS_WARNING
                and not (documents.overridable(f) and documents.finding_key(f) in documents.load_overrides(conn, doc["id"]))] if doc else []
        lines.append(f"The writer's fix is stored, but the document is still blocked: {len(left)} finding(s) left.")
        lines += [f"   {f['where']}: {f['reason']}"[:300] for f in left[:10]]
        subject = f"document still blocked, {what}"
    elif row["status"] == "stale":
        lines.append("The document changed while the writer worked (or it is not blocked any more). Nothing was stored.")
        subject = f"document fix not stored, {what}"
    else:
        reason = REVISION_REASONS.get(row["outcome"] or "", "the revision failed")
        lines += [f"The fix failed: {reason}.", *( [row["message"]] if row["message"] else [])]
        subject = f"document fix failed, {what}"
    if row["scrubbed"]:
        lines += ["", "The home address was removed before sending."]
    notes = [a.get("note") for a in (done or {}).get("attention", []) if isinstance(a, dict) and a.get("note")]
    if row["status"] == "stored" and notes:
        lines += ["", "The reviewer asks you to look at:"] + [f"   {n}" for n in notes[:5]]
    base = cfg["report"]["web_base_url"].rstrip("/")
    if base and doc:
        lines += ["", f"Job page: {base}/jobs/{doc['job_id']}"]
    msg = new_message(cfg, f"{cfg['report']['subject_prefix']}: {subject}")
    msg.set_content("\n".join(lines) + "\n")
    smtp_send(cfg, msg, smtp_factory, sleep)
    return "sent"


def send_report(conn, cfg: dict, batch_id: str, extra: dict | None = None, smtp_factory=smtplib.SMTP,
                sleep=time.sleep, now: datetime | None = None, force: bool = False) -> str:
    """Send the report for a batch. Returns 'sent' or 'already_sent'; raises ReportError if it could not be sent
    (`ReportAuthError` when Gmail refused the login or the app password is missing).

    The outcome is stored in `reports`; see `smtp_send` for the retries.
    """
    done = conn.execute("SELECT status FROM reports WHERE batch_id = ?", (batch_id,)).fetchone()
    if done and done["status"] == "sent" and not force:
        return "already_sent"
    report = collect(conn, cfg, batch_id, extra)
    msg = build_message(cfg, report)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def record(status: str, attempts: int, error: str | None) -> None:
        with conn:
            conn.execute(
                "INSERT INTO reports (batch_id, status, attempts, error, subject, message_id, created_at, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(batch_id) DO UPDATE SET status = excluded.status, "
                "attempts = excluded.attempts, error = excluded.error, subject = excluded.subject, "
                "message_id = excluded.message_id, sent_at = excluded.sent_at",
                (batch_id, status, attempts, error, report.subject, msg["Message-ID"], stamp, stamp if status == "sent" else None))

    try:
        attempts = smtp_send(cfg, msg, smtp_factory, sleep)
    except ReportError as exc:
        record("failed", exc.attempts, exc.detail)
        raise
    record("sent", attempts, None)
    return "sent"
