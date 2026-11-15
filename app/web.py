"""The web app (goal.md 5.7, Phase 4): Today's Top 10, job detail and the list of open jobs (chunk 1, read-only), and
since chunk 2 (v0.46.0) the first changes to the database: approving documents, Shortlist, Applied and Reject; since
v0.49.0 also the undo of an approval and of Shortlisted / Applied.

`python -m app web` starts it on 127.0.0.1 with no login. Since v0.74.0 (goal.md 5.9a) it also runs as its own service in the
NAS container: `web.container_bind` makes it listen on 0.0.0.0 inside the container, which `check_bind` allows only with the
env RECRUITMENT_CONTAINER=1, a Host allow-list (`web.allowed_hosts`), a user name and a password hash; the login (`app/weblogin.py`:
scrypt hash from RECRUITMENT_WEB_PASSWORD_HASH, signed stateless cookie, throttle) is then required for every page except
`/login` and `/static`. A user name in `web.login.username` turns the login on at 127.0.0.1 as well; without it the PC has none.
Every GET opens the database with `mode=ro` and `PRAGMA query_only`; GET never changes anything. The only writes are the
ten POST routes `/jobs/<id>/approve`, `/shortlist`, `/applied`, `/reject`, `/undo-approval`, `/undo-status`, (v0.72.0)
`/description`, (v0.83.0) `/recheck` and `/findings/accept` (both refused while a pipeline run holds the lock) and (v0.85.0) `/revise`
("Ask the writer to fix": it only stores a request row, the scheduler sends it; a held run lock queues it, it does not refuse it), each with its own write connection (`mode=rw`, never creating a file). The web app never runs a migration (a database with another schema version gets an
error page).

Write protection, kept also with the login (decided by the user on 1 Nov 2026, "do as recommended"): (1) POST only,
answered with a 303 redirect (Post/Redirect/Get); (2) a CSRF token in every form: `HMAC-SHA256(secret, "<action>:<job id>")`
with a secret made by `secrets.token_bytes` when the app starts, so a token is valid for one action on one job until the
server restarts; it is checked with `hmac.compare_digest` and a missing or wrong one gives 403; (3) the Host header must be
local (also for POST), and a POST with an Origin or Referer header that is not exactly this app's own origin gets 403
(Origin `null` too); (4) the `--host` flag and `web.host` are 127.0.0.1, localhost or ::1 only (`check_host`; the container bind is the separate `check_bind`); (5) no CORS headers, the
Content-Security-Policy allows forms to this app only and no script except on /analytics and /health (charts), and the form body must be
`application/x-www-form-urlencoded` and small.

Since v0.47.0 two more read-only pages: Analytics `/analytics` and Runs/Health `/health` (numbers from `app/analytics.py`; bars are
HTML and CSS classes, the policy allows no script except on /analytics and /health (charts) and no inline style).

Since v0.72.0 the missing-description queue (goal.md 5.5, 5.7): `/missing` lists the jobs that would be in the top 10 but have no
description, `/jobs/<id>/description` is the paste page (its POST has a body limit of 1 MB, the other routes keep 10 kB, refuses an
incomplete text and refuses while a pipeline run holds the lock), and a bookmarklet copies the text of the page the user is
reading to the clipboard (it sends nothing anywhere; the user pastes it into the box). A queued job's title-only score is not shown.

The top 10 is built by `report.collect`, the same code as the daily email, so the rank, band, markers and notes
cannot differ from the mail. Job text is shown through Jinja autoescape only (no `|safe` anywhere), job links are
rendered only for http and https, and every response carries a Content-Security-Policy that forbids scripts except on /analytics and /health (charts). The page
shows the text of the stored documents and their findings (with the quoted text, escaped) on the job page, but no home address
and nothing from `.env` or the config file.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import jinja2
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import analytics, approval, db, descriptions, documents, interviews, pipeline, rejection, report, statusflow, weblogin
from .config import ConfigError, resolve_path
from .documents import KINDS, approved_dir, document_text, documents_dir, finding_key, overridable, SHOWN_AS_WARNING
from .filters.language import posting_is_german
from .resolver.complete import manual_problems as completeness_problems

log = logging.getLogger("recruitment")
DOC_LABELS = {"cover_letter": "Cover letter", "cv": "CV"}       # headings of the documents on the job page

HERE = Path(__file__).resolve().parent
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")      # the only bind addresses of this chunk, and the only Host headers served
CONTAINER_BIND_HOST = "0.0.0.0"                          # only inside the container, only through check_bind
LOGIN_MAX_WAITING = 3                                    # login attempts checking or queued at once; more get 429 at once
HASH_ENV = "RECRUITMENT_WEB_PASSWORD_HASH"               # in the untracked .env
PAGE_SIZE = 200
SUBSCORES = ("skills", "experience", "domain", "seniority", "location")
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",      # not "no-referrer": with that a browser sends `Origin: null` on a form POST, which the origin check refuses
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
# The two chart pages (v0.89.0) load the vendored Chart.js and static/charts.js from this app: script-src 'self', never unsafe-inline or unsafe-eval.
CHART_PATHS = ("/analytics", "/health")
CHART_CSP = HEADERS["Content-Security-Policy"].replace("script-src 'none'", "script-src 'self'")
# The job page (v0.99.0) loads static/confirm.js (the native confirm() of a delete): script-src 'self' on exactly `/jobs/<digits>`, the rest of the policy unchanged.
JOB_PAGE = re.compile(r"/jobs/[0-9]+")


MAX_REVISE_BODY = 16_000                                # the revise form: 1000 characters of four bytes each, url-encoded
MAX_INTERVIEW_BODY = 40_000                              # a round or a note: up to 2300 characters of four bytes each, url-encoded (12 bytes each in the worst case)
MAX_BODY = 10_000                                       # bytes of a form body; the forms hold a token, a choice and a short comment
# the paste page: MAX_CHARS (100,000) characters, percent-encoded up to 9 bytes each in the worst case, so a large posting can
# be a few hundred kB; only that route has this limit
MAX_DESCRIPTION_BODY = 1_000_000
# The bookmarklet (v0.72.0, R5): it copies the selection (200 characters or more), else the description container, else the page
# text, to the clipboard and tells the user how many characters. It sends nothing anywhere: no address, no request, no storage.
BOOKMARKLET = (
    "javascript:(function(){var s=String(window.getSelection?window.getSelection():'');var t=s.length>=200?s:'';if(!t){var q=['.jobs-description__content','#job-details','.show-more-less-html__markup','[itemprop=description]','main','article'];for(var i=0;i<q.length&&!t;i++){var e=document.querySelector(q[i]);if(e&&e.innerText&&e.innerText.trim())t=e.innerText}}if(!t)t=document.body.innerText;var n=t.length;function f(){var a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';document.body.appendChild(a);a.select();var k=false;try{k=document.execCommand('copy')}catch(x){}document.body.removeChild(a);return k}function d(){alert('Copied '+n+' characters. Paste them into the Recruitment page (Ctrl+V)')}function z(){alert('Copy failed. Select the text and press Ctrl+C')}function g(){if(f()){d()}else{z()}}if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(d,g)}else{g()}})()"
)
COMMENT_MAX = rejection.COMMENT_MAX                         # shared with `python -m app reject`
REVISE_INSTRUCTION_MAX = 1000                               # characters of the optional instruction of "Ask the writer to fix"
NEXT_PAGES = ("top", "job", "list") + statusflow.APPLICATION_PAGES                   # where a button sends the user back to (never a free URL)

# The bulk route (v0.96.0): one token per action, the jobs of a selection are changed one by one, each in its own transaction.
BULK_ACTIONS = tuple(statusflow.FORWARD) + ("undo", "reject")
BULK_PAGES = tuple(statusflow.PAGE_ACTIONS) + ("job",)         # `job`: the job page, exactly one job
MAX_BULK = 200                                                  # jobs per request (= PAGE_SIZE)
PAGE_PATHS = {"top": "/", "list": "/jobs", **{p: f"/{p}" for p in statusflow.APPLICATION_PAGES}}      # where a button sends the user back to (NEXT_PAGES)
BULK_PATHS = {**PAGE_PATHS, "missing": "/missing"}
# The 8 list pages (v0.97.0) load static/select.js: script-src 'self' on exactly these paths (the same tuple decides the script tag), never unsafe-inline or unsafe-eval.
SCRIPT_PATHS = tuple(PAGE_PATHS.values()) + ("/missing",)
BULK_PAGED = ("list",) + statusflow.APPLICATION_PAGES      # the pages that have `?page=N`
BULK_NOTE = "web bulk"
BULK_LISTED = 10                                                # jobs named in one flash line


HIDDEN_FROM_OPEN = report.ACTED                            # not on /jobs: jobs from the application on have their own pages; the /missing queue is hidden too (job_list)


# The status rules (button availability, refusal texts, undo) live in app/statusflow.py; the old names stay importable from here.
SHORTLIST_FROM = statusflow.SHORTLIST_FROM
APPLIED_FROM = statusflow.APPLIED_FROM
REJECT_FROM = statusflow.REJECT_FROM
LABELS = statusflow.LABELS
UNDO_STATUSES = statusflow.UNDO_STATUSES
UNDO_NOTE = statusflow.UNDO_NOTE
allowed_actions = statusflow.allowed_actions
refusal = statusflow.refusal
undo_target = statusflow.undo_target
undo_info = statusflow.undo_info


class WebDbError(Exception):
    """The database cannot be shown: missing, unreadable, or a schema version other than the code's."""


def check_host(host: str) -> str:
    """The bind address if it is a local one, else a ConfigError (this chunk never listens on the network)."""
    if host not in ALLOWED_HOSTS:
        raise ConfigError(f"web.host {host!r} is not allowed: this version listens on 127.0.0.1 (localhost, ::1) only; "
                          "LAN and ZeroTier binding come with the NAS in Phase 5")
    return host


def check_bind(cfg: dict, host: str, environ=None) -> tuple[str, "weblogin.WebLogin | None"]:
    """(the address to bind, the login or None). container_bind needs all four: RECRUITMENT_CONTAINER=1, web.allowed_hosts,
    web.login.username and a parseable hash in RECRUITMENT_WEB_PASSWORD_HASH; a user name alone (the PC opt-in) needs the hash.
    Otherwise a ConfigError, before anything starts. `host` is the already checked local address."""
    environ = os.environ if environ is None else environ
    web = cfg["web"]
    username, container = web["login"]["username"], web["container_bind"]
    if container:
        if environ.get("RECRUITMENT_CONTAINER") != "1":
            raise ConfigError("web.container_bind is true, but this is not the container (the environment variable "
                              "RECRUITMENT_CONTAINER=1 is set by the Docker image); the web app will not listen on all interfaces here")
        if not web["allowed_hosts"]:
            raise ConfigError("web.container_bind is true, but web.allowed_hosts is empty (name the Host the app is reached by, "
                              "for example the ZeroTier address of the NAS)")
        if not username:
            raise ConfigError("web.container_bind is true, but web.login.username is empty (a login is required in the container)")
    elif environ.get("RECRUITMENT_CONTAINER") == "1":
        raise ConfigError("this is the container, but web.container_bind is false: the app would listen on the container's "
                          "127.0.0.1 only and be unreachable (set web.container_bind: true with a login)")
    if not (container or username):
        return host, None
    try:
        login = weblogin.WebLogin(username, environ.get(HASH_ENV, ""), web["login"]["stay_days"])
    except ValueError:
        raise ConfigError(f"the web login needs {HASH_ENV} in the .env that the web app reads, in the form made by "
                          "`python -m app set-web-password` (it is missing or malformed)") from None
    return (CONTAINER_BIND_HOST if container else host), login


def check_port(port) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError("web.port must be an integer from 1 to 65535")
    return port


def _open(path: str | Path, writable: bool) -> sqlite3.Connection:
    p = Path(path)
    if not p.is_file():
        raise WebDbError("The database file does not exist yet. Run `python -m app prepare` first.")
    try:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode={'rw' if writable else 'ro'}", uri=True, timeout=10 if writable else 5)
        conn.row_factory = sqlite3.Row
        if writable:
            conn.execute("PRAGMA foreign_keys = ON")           # archiving deletes the job row, its dependents go with it
            conn.execute("PRAGMA busy_timeout = 10000")
        else:
            conn.execute("PRAGMA query_only = ON")
        version = db.schema_version(conn)
    except sqlite3.Error:
        raise WebDbError("The database could not be opened for reading.") from None
    if version > len(db.MIGRATIONS):
        conn.close()
        again = ("Restart the container (`docker compose up -d` or `docker compose restart`)" if os.environ.get("RECRUITMENT_CONTAINER") == "1"
                 else "Stop it (Ctrl+C) and start `python -m app web` again")
        raise WebDbError(f"The database has schema version {version}, newer than the {len(db.MIGRATIONS)} this web app knows: "
                         f"this web app was started before an update. {again}; "
                         "the web app never migrates the database itself.")
    if version != len(db.MIGRATIONS):
        conn.close()
        raise WebDbError(f"The database has schema version {version}, this version of the app needs {len(db.MIGRATIONS)}. "
                         "Run any `python -m app` command that opens the database (for example `status`) to migrate it, "
                         "or update the app; the web app never migrates the database itself.")
    return conn


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open the database for reading only and check that its schema is the one this code knows. Never migrates."""
    return _open(path, writable=False)


def open_writable(path: str | Path) -> sqlite3.Connection:
    """Open an existing database for the few changes the buttons make (same schema check; never creates a file, never migrates)."""
    return _open(path, writable=True)


def latest_batch(conn: sqlite3.Connection) -> str | None:
    """The newest batch whose results were imported (its top 10 is today's), or None."""
    row = conn.execute(
        "SELECT b.id FROM batches b WHERE EXISTS (SELECT 1 FROM imports i WHERE i.batch_id = b.id AND i.status = 'imported') "
        "ORDER BY b.created_at DESC, b.id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def _json(text: str | None, default):
    try:
        value = json.loads(text or "")
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _texts(items) -> list[str]:
    return report.clean([x for x in items if isinstance(x, str)]) if isinstance(items, list) else []


def is_queued(conn: sqlite3.Connection, job_id: int) -> bool:
    """True for a queued job (`descriptions.QUEUED`): its estimate is not shown. After a paste it stays queued until it is scored again."""
    return conn.execute(f"SELECT 1 FROM jobs j WHERE j.id = ? AND {descriptions.QUEUED.format(j='j')}", (job_id,)).fetchone() is not None


def missing_rows(conn: sqlite3.Connection) -> list[dict]:
    """The rows of the /missing page: the queued jobs with place, age, locations, safe links and the tiers the resolver tried."""
    now = datetime.now(timezone.utc)
    out = []
    for q in descriptions.queued_jobs(conn):
        try:
            seen = datetime.strptime(q["first_seen_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            days = max(0, (now - seen).days)
        except (TypeError, ValueError):
            days = None
        links = []
        for s in conn.execute("SELECT url FROM job_sources WHERE job_id = ? ORDER BY id", (q["id"],)):
            link = report.safe_url(s["url"])
            if link and link not in links:
                links.append(link)
        scored = conn.execute("SELECT 1 FROM scores WHERE job_id = ? LIMIT 1", (q["id"],)).fetchone() is not None
        out.append({**q, "days": days, "links": links, "tiers": descriptions.tiers_tried(conn, q["id"]),
                    "actions": allowed_actions(q["status"], scored), "undo": undo_info(conn, q["id"], q["status"], scored),
                    "bulk": bulk_names("missing", q["status"], scored, None),
                    "locations": [r["location_text"] for r in conn.execute(
                        "SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (q["id"],))]})
    return out


def job_detail(conn: sqlite3.Connection, cfg: dict, job_id: int) -> dict | None:
    """Everything the detail page shows for one open job, or None when there is no such job (archived jobs are not open)."""
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        return None
    sc = conn.execute("SELECT * FROM scores WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    queued = is_queued(conn, job_id)
    score = None
    if sc is not None and not queued:             # a queued job's title-only estimate is not shown (goal.md 5.5)
        details = _json(sc["details"], {})
        subs = _json(sc["subscores"], {})
        summary = details.get("summary", "")
        score = {
            "overall": sc["overall"], "band": report.band(sc["overall"], cfg), "confidence": sc["confidence"] or "",
            "cv_variant": sc["cv_variant"] or "", "prompt_version": sc["prompt_version"], "model": sc["model"], "created_at": sc["created_at"],
            "subscores": [(k, subs[k]) for k in SUBSCORES if k in subs],
            "summary": summary if isinstance(summary, str) else "",
            "matches": _texts(details.get("matches")), "gaps": _texts(details.get("gaps")), "red_flags": _texts(details.get("red_flags")),
        }
    sources = [{"source": s["source"], "reference": s["reference"] or "", "date": s["source_date"], "url": s["url"] or "", "link": report.safe_url(s["url"])}
               for s in conn.execute("SELECT * FROM job_sources WHERE job_id = ? ORDER BY id", (job_id,))]
    docs = []       # kind, status, findings, file names and the stored text (shown on the job page only, never in the top 10, never logged)
    for d in conn.execute("SELECT id, kind, status, findings, pdf_path, pages, approved_pdf_path, content FROM documents WHERE job_id = ? ORDER BY kind", (job_id,)):
        try:
            text = document_text(d["kind"], json.loads(d["content"]))
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):       # unreadable content: show no text rather than fail the page
            text = ""
        accepted = {(r["kind"], r["finding_where"], r["text"]): r["comment"] for r in conn.execute(
            "SELECT kind, finding_where, text, comment FROM finding_overrides WHERE document_id = ?", (d["id"],))}
        items = [{"kind": f.get("kind", ""), "where": f.get("where", ""), "text": f.get("text", ""), "reason": f.get("reason", ""), "block_text": f.get("block_text", ""),
                  "warning": f.get("kind") in SHOWN_AS_WARNING, "comment": accepted.get(finding_key(f)),
                  "can_accept": d["status"] == "blocked" and overridable(f) and finding_key(f) not in accepted}
                 for f in _json(d["findings"], []) if isinstance(f, dict)]
        rev = conn.execute("SELECT status, outcome, message, created_at, relayed_at, finished_at FROM revision_requests "
                           "WHERE document_id = ? ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()
        active = rev is not None and rev["status"] in ("pending", "relayed")
        docs.append({"revision": dict(rev) if rev is not None else None,
                     "can_revise": bool(cfg["headless"]["enabled"]) and d["status"] == "blocked" and not active,
                     "kind": d["kind"].replace("_", " "), "label": DOC_LABELS.get(d["kind"], d["kind"]), "raw_kind": d["kind"], "status": d["status"], "findings": len(_json(d["findings"], [])), "rows": items,
                     "pdf": d["pdf_path"] or "", "pages": d["pages"], "approved_pdf": d["approved_pdf_path"] or "", "text": text})
    return {
        "id": job["id"], "title": job["title"], "company": job["company"], "status": job["status"], "repost": bool(job["repost"]),
        "queued": queued, "added": queued and not job["description_missing"], "can_paste": bool(job["description_missing"]),
        "partial_chars": len(partial[0]) if job["description_missing"] and (partial := descriptions.partial_text(conn, job_id)) else 0,
        "actions": allowed_actions(job["status"], sc is not None), "more": more_actions(job["status"], sc is not None, "offers" if job["status"] == "offer" else "job"), "can_approve": any(d["status"] != "approved" for d in docs),
        "undo": undo_info(conn, job_id, job["status"], sc is not None), "approved_kinds": [d["raw_kind"] for d in docs if d["status"] == "approved"],
        "posted": report.posted_line(job["posted_on"]), "first_seen": job["first_seen_at"], "last_seen": job["last_seen_at"],
        "locations": [r["location_text"] for r in conn.execute("SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (job_id,))],
        "notes": report.notes(conn, job_id) + ([report.CLOSED] if job["closed_at"] else []),
        "rounds": interviews.list_rounds(conn, job_id, cfg["timezone"]), "job_notes": interviews.list_notes(conn, job_id, cfg["timezone"]), "round_kinds": interviews.KINDS,
        "sources": sources, "score": score, "documents": docs, "description": job["description"] or "",
        "description_note": report.NO_DESCRIPTION.format(cap=cfg["scoring"]["no_description_cap"]) if job["description_missing"] else "",
        "language_note": report.NON_ENGLISH.format(cap=cfg["scoring"]["non_english_cap"], band=report.band(cfg["scoring"]["non_english_cap"], cfg)) if posting_is_german(job["description"], cfg) else "",
        "filters": [{"filter": f["filter"], "outcome": f["outcome"], "reason": f["reason_code"] or "", "comment": f["comment"] or ""}
                    for f in conn.execute("SELECT filter, outcome, reason_code, comment FROM job_filter_results WHERE job_id = ? ORDER BY id", (job_id,))],
    }


def _job_page(conn: sqlite3.Connection, cfg: dict, where: str, order: str, page: int, params: tuple = ()) -> tuple[list[dict], int]:
    """(rows of one page, number of pages) of the jobs that match `where` (code constants only), in `order`; the page is capped at the last."""
    total = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {where}", params).fetchone()[0]
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, page), pages)
    rows = conn.execute(
        f"""SELECT j.id, j.title, j.company, j.status, j.repost, s.overall,
                   (SELECT MAX(h.changed_at) FROM status_history h WHERE h.job_id = j.id AND h.to_status = 'applied') AS applied_at,
                   (SELECT h.changed_at FROM status_history h WHERE h.job_id = j.id AND h.to_status = j.status ORDER BY h.id DESC LIMIT 1) AS stage_at
            FROM jobs j LEFT JOIN scores s ON s.id = (SELECT MAX(id) FROM scores WHERE job_id = j.id)
            WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""", (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
    queued = {r["id"]: r["description_missing"] for r in conn.execute(
        f"SELECT j.id, j.description_missing FROM jobs j WHERE {descriptions.QUEUED.format(j='j')}")}
    out = [{"id": r["id"], "title": r["title"], "company": r["company"], "status": r["status"], "repost": bool(r["repost"]),
            "queued": r["id"] in queued, "added": r["id"] in queued and not queued[r["id"]],
            "score": None if r["id"] in queued else r["overall"], "applied_at": r["applied_at"], "stage_at": r["stage_at"],
            "band": "" if r["id"] in queued or r["overall"] is None else report.band(r["overall"], cfg)} for r in rows]
    return out, pages


def bulk_names(page: str, status: str, scored: bool, undo: dict | None) -> str:
    """The actions of the selection bar that this job can take on this page, space separated (the `data-actions` of its row)."""
    names = [a for a in statusflow.PAGE_ACTIONS[page] if (undo is not None if a == "undo" else statusflow.can(a, status, scored))]
    return " ".join(names)


def more_actions(status: str, scored: bool, page: str) -> list[tuple[str, str]]:
    """(action, label) of the stage actions a job can take that have no per-job button: they link to the confirmation page."""
    return [(a, statusflow.label(a, page)) for a in statusflow.available_actions(status, scored) if a not in statusflow.BUTTON_ACTIONS]


def _row_actions(conn: sqlite3.Connection, r: dict, page: str) -> bool:
    """Add `actions`, `undo`, `more` and `bulk` to a list row; returns whether the job has a score."""
    scored = conn.execute("SELECT 1 FROM scores WHERE job_id = ? LIMIT 1", (r["id"],)).fetchone() is not None
    r["actions"] = allowed_actions(r["status"], scored)
    r["undo"] = undo_info(conn, r["id"], r["status"], scored)
    r["more"] = more_actions(r["status"], scored, page)
    r["bulk"] = bulk_names(page, r["status"], scored, r["undo"])
    return scored


def job_list(conn: sqlite3.Connection, cfg: dict, page: int) -> tuple[list[dict], int]:
    """(rows of one page, number of pages) of the open jobs that are not applied (or further) and not queued on /missing (waiting for a pasted
    description), best latest score first, jobs without a score last. Other not_selected jobs are listed (decision of 11 Nov 2026)."""
    where = ("j.status NOT IN ({}) AND NOT (j.description_missing = 1 AND ".format(",".join(f"'{s}'" for s in HIDDEN_FROM_OPEN))
             + descriptions.QUEUED.format(j="j") + ")")
    rows, pages = _job_page(conn, cfg, where, "s.overall IS NULL, s.overall DESC, j.id", page)
    for r in rows:
        _row_actions(conn, r, "list")
    return rows, pages


STAGE_STATUSES = {page: status for status, page in statusflow.STAGE_PAGES.items()}      # page -> status


def stage_list(conn: sqlite3.Connection, cfg: dict, status: str, page: int) -> tuple[list[dict], int]:
    """(rows of one page, number of pages) of the jobs in a stage after the application, newest in the stage first (no recorded
    date last), with the buttons, the days in the stage and the approved PDFs of each."""
    page_key = statusflow.STAGE_PAGES[status]
    rows, pages = _job_page(conn, cfg, "j.status = ?", "stage_at IS NULL, stage_at DESC, j.id", page, (status,))
    now = datetime.now(timezone.utc)
    upcoming = (interviews.next_rounds(conn, [r["id"] for r in rows], now.strftime("%Y-%m-%dT%H:%M:%SZ"), cfg["timezone"])
                if page_key in statusflow.NEXT_INTERVIEW_PAGES else {})
    for r in rows:
        _row_actions(conn, r, page_key)
        r["next_interview"] = upcoming.get(r["id"])
        try:
            r["days"] = max(0, (now - datetime.strptime(r["stage_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days)
        except (TypeError, ValueError):
            r["days"] = None
        r["pdfs"] = [{"kind": d["kind"], "label": DOC_LABELS.get(d["kind"], d["kind"])} for d in conn.execute(
            "SELECT kind FROM documents WHERE job_id = ? AND status = 'approved' AND COALESCE(approved_pdf_path, '') <> '' ORDER BY kind",
            (r["id"],))]
    return rows, pages


def stage_count(conn: sqlite3.Connection, status: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)).fetchone()[0]


def applied_list(conn: sqlite3.Connection, cfg: dict, page: int) -> tuple[list[dict], int]:
    """(rows of one page, number of pages) of the applied jobs, newest application first (no recorded date last), with the
    buttons and the approved PDFs of each."""
    return stage_list(conn, cfg, "applied", page)


def _host_of(header: str) -> str:
    header = header.strip().lower()
    if header.startswith("["):                                  # [::1]:8000
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def _origin_ok(request: Request) -> bool:
    """A POST must come from this app's own pages: an Origin or Referer header, when the browser sends one, must be exactly
    http://<the Host header> (Origin `null`, another port, another host or userinfo tricks all fail)."""
    host = request.headers.get("host", "").strip().lower()
    origin = request.headers.get("origin")
    if origin is not None and origin.strip().lower() != f"http://{host}":
        return False
    referer = request.headers.get("referer")
    if referer is not None:
        try:
            parts = urlsplit(referer)
        except ValueError:
            return False
        if parts.scheme != "http" or parts.netloc.lower() != host:
            return False
    return True


DB_BUSY = [("refused", rejection.DB_BUSY_TEXT)]
TOKEN_REFUSED = ("The request was refused: the form is out of date or did not come from this app (the page was opened before the "
                 "server was restarted, or the token is missing). Reload the page and try again. Nothing was changed.")


def parse_bulk_ids(raw: list[str]) -> tuple[list[int], str | None]:
    """(the job ids in order, duplicates dropped, None) or ([], why not) for the ids of a selection (a list of comma-joined strings)."""
    ids: list[int] = []
    for part in ",".join(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if not (part.isascii() and part.isdigit()) or len(part) > 12 or int(part) < 1:
            return [], "The selection holds something that is not a job number. Nothing was changed."
        if int(part) not in ids:
            ids.append(int(part))
    if not ids:
        return [], "No job is selected. Nothing was changed."
    if len(ids) > MAX_BULK:
        return [], f"Too many jobs are selected (at most {MAX_BULK} at once). Nothing was changed."
    return ids, None


def bulk_page_no(raw: str | None) -> int:
    return int(raw) if raw and raw.isascii() and raw.isdigit() and 0 < int(raw) < 10**6 else 1


def bulk_path(nxt: str, page: int, ids: list[int], gone: bool = False) -> str:
    """The page a bulk change sends the user back to (never a free URL), with `?page=N` kept on the pages that have it."""
    if nxt == "job":
        return "/jobs" if gone or len(ids) != 1 else f"/jobs/{ids[0]}"
    path = BULK_PATHS[nxt]
    return f"{path}?page={page}" if nxt in BULK_PAGED and page > 1 else path


def bulk_label(action: str, nxt: str) -> str:
    return {"undo": "Undo", "reject": "Reject"}.get(action) or statusflow.label(action, nxt)


def _names(items: list[str]) -> str:
    shown = items[:BULK_LISTED]
    return ", ".join(shown) + (f", and {len(items) - BULK_LISTED} more" if len(items) > BULK_LISTED else "")


def create_app(cfg: dict, db_path: str | Path | None = None, allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS,
               login: "weblogin.WebLogin | None" = None, demo: bool = False) -> FastAPI:
    """Build the app. `db_path` defaults to `paths.database`; `allowed_hosts` are the Host headers served (a defence
    against DNS rebinding). `login` None: no login (the PC); a WebLogin: every page but /login and /static needs the cookie.
    `demo` (v0.100.0, scripts/freeze_demo.py): the templates leave out every form, button and script that writes, and every request but GET and HEAD gets 405."""
    path = Path(db_path) if db_path else resolve_path(cfg["paths"]["database"])
    app = FastAPI(title="Recruitment", docs_url=None, redoc_url=None, openapi_url=None)
    secret = secrets.token_bytes(32)                      # new at every start: forms of an earlier run are refused

    def csrf(purpose: str) -> str:
        return hmac.new(secret, purpose.encode(), hashlib.sha256).hexdigest()

    flashes: collections.OrderedDict[str, list] = collections.OrderedDict()   # messages shown once after a redirect; only the server fills it

    def put_flash(lines: list) -> str:
        key = secrets.token_urlsafe(9)
        flashes[key] = lines
        while len(flashes) > 20:
            flashes.popitem(last=False)
        return key

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
    env.filters["upfirst"] = lambda s: s[:1].upper() + s[1:]      # unlike |capitalize it leaves the rest of the text alone
    env.globals.update(csrf=csrf, labels=LABELS, login_on=login is not None, demo=demo, page_actions=statusflow.PAGE_ACTIONS, round_kinds=interviews.KINDS, action_label=bulk_label,
                       script_paths=SCRIPT_PATHS, swipe=statusflow.SWIPE, careful=statusflow.CAREFUL, comment_max=COMMENT_MAX,
                       application_pages={p: statusflow.STAGE_PAGE_TEXT[p][0] for p in statusflow.APPLICATION_PAGES},
                       stage_empty={p: t[1] for p, t in statusflow.STAGE_PAGE_TEXT.items()})
    templates = Jinja2Templates(env=env)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.state.open_db = lambda: open_readonly(path)
    app.state.open_rw = lambda: open_writable(path)
    app.state.csrf = csrf
    app.state.cfg = cfg
    app.state.login = login

    def logged_in(request: Request) -> bool:
        return login is None or login.read_cookie(request.cookies.get(weblogin.COOKIE_NAME))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if _host_of(request.headers.get("host", "")) not in allowed_hosts:
            response = PlainTextResponse("Bad Host header", status_code=400)
        elif demo and request.method not in ("GET", "HEAD"):
            response = PlainTextResponse("Read-only demo. Nothing was changed.", status_code=405, headers={"Allow": "GET, HEAD"})
        elif request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_ok(request):
            response = PlainTextResponse("Foreign origin refused", status_code=403)
        elif path == "/logout" and request.method == "POST" and login is not None and not logged_in(request):
            response = RedirectResponse("/login", status_code=303)       # an expired or bad cookie: nothing to end, clear it (v0.75.0)
            response.delete_cookie(weblogin.COOKIE_NAME, path="/", httponly=True, samesite="lax")
        elif not logged_in(request) and path != "/login" and not path.startswith("/static/"):
            if request.method in ("GET", "HEAD"):
                target = path + ("?" + request.url.query if request.url.query else "")
                response = RedirectResponse("/login?next=" + quote(target, safe="/"), status_code=303)
            else:
                response = PlainTextResponse("Login required. Nothing was changed.", status_code=403)
        else:
            response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers[name] = value
        if path in CHART_PATHS or path in SCRIPT_PATHS or (JOB_PAGE.fullmatch(path) and response.status_code == 200):
            response.headers["Content-Security-Policy"] = CHART_CSP
        return response

    def render(request: Request, name: str, status: int = 200, **ctx):
        key = request.query_params.get("flash")
        ctx.setdefault("flash", flashes.pop(key, None) if key else None)
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def go(path: str, lines: list | None = None) -> RedirectResponse:
        """Answer a POST: 303 to a page of this app, with the messages kept on the server and fetched once by `flash`."""
        if lines:
            path += ("&" if "?" in path else "?") + "flash=" + put_flash(lines)
        return RedirectResponse(path, status_code=303)

    def back(form: dict, job_id: int, lines: list, gone: bool = False) -> RedirectResponse:
        nxt = form.get("next") if form.get("next") in NEXT_PAGES else "job"
        if gone:                                           # the job is not open any more: its page would be a 404
            return go(PAGE_PATHS.get(nxt, "/jobs"), lines)
        return go({**PAGE_PATHS, "job": f"/jobs/{job_id}"}[nxt], lines)

    async def read_form(request: Request, max_body: int = MAX_BODY) -> dict:
        """The fields of a small urlencoded POST body (415, 413 and 400 on a bad one)."""
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            raise StarletteHTTPException(415, "The form must be sent as application/x-www-form-urlencoded")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_body:
                raise StarletteHTTPException(413, "The form is too large")
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
        except (UnicodeDecodeError, ValueError):
            raise StarletteHTTPException(400, "The form could not be read") from None
        return {k: v[0] for k, v in fields.items()}

    async def verified(request: Request, action: str, job_id: str, max_body: int = MAX_BODY) -> tuple[int, dict]:
        """The job id and the form fields of a POST whose CSRF token is right for this action on this job, else 403."""
        if not (job_id.isascii() and job_id.isdigit()) or len(job_id) > 12:
            raise StarletteHTTPException(404)
        form = await read_form(request, max_body)
        jid = int(job_id)
        if not hmac.compare_digest(form.get("csrf", "").encode(), csrf(f"{action}:{jid}").encode()):
            raise StarletteHTTPException(403, TOKEN_REFUSED)
        return jid, form

    @app.exception_handler(WebDbError)
    async def db_error(request: Request, exc: WebDbError):
        return render(request, "error.html", 503, heading="The database cannot be shown", message=str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return render(request, "error.html", 404, heading="Not found",
                          message="There is no such page, or the job is not open (archived jobs are not shown here).")
        if exc.status_code == 403:
            return render(request, "error.html", 403, heading="Request refused", message=str(exc.detail))
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    # --- the login (only when it is on; otherwise these routes do not exist and answer 404) ---------------------------------

    if login is not None:
        login_lock = asyncio.Lock()
        login_waiting = [0]                                           # attempts that are checking or queued

        def login_page(request: Request, nxt: str, status: int = 200, message: str = ""):
            return render(request, "login.html", status, nxt=weblogin.safe_next(nxt), message=message, stay_days=login.stay_days)

        @app.get("/login", response_class=HTMLResponse)
        def login_form(request: Request):
            return login_page(request, request.query_params.get("next", "/"))

        @app.post("/login")
        async def login_submit(request: Request):
            form = await read_form(request)
            if not hmac.compare_digest(form.get("csrf", "").encode(), csrf("login").encode()):
                raise StarletteHTTPException(403, TOKEN_REFUSED)
            ip = request.client.host if request.client else "unknown"

            def too_many(wait: int):
                response = login_page(request, form.get("next", "/"), 429, f"Too many failed logins. Try again in {-(-wait // 60) or 1} minutes.")
                response.headers["Retry-After"] = str(wait)
                return response

            if login_waiting[0] >= LOGIN_MAX_WAITING:                 # a flood of attempts: no queue, no scrypt
                return too_many(60)
            wait = login.throttle.check(ip)
            if wait:
                return too_many(wait)
            login_waiting[0] += 1
            try:
                async with login_lock:                                # check, scrypt and record are one step, else
                    wait = login.throttle.check(ip)                   # concurrent guesses all pass the check first
                    if wait:
                        return too_many(wait)
                    ok = await run_in_threadpool(login.check, form.get("username", ""), form.get("password", ""))
                    if not ok:
                        login.throttle.record_failure(ip)
            finally:
                login_waiting[0] -= 1
            if not ok:
                log.warning("web: login failed from %s", ip)
                return login_page(request, form.get("next", "/"), 401, "Wrong user name or password.")
            login.throttle.record_success(ip)
            log.info("web: login from %s", ip)
            value, max_age = login.make_cookie(form.get("stay") == "on")
            response = RedirectResponse(weblogin.safe_next(form.get("next")), status_code=303)
            response.set_cookie(weblogin.COOKIE_NAME, value, max_age=max_age, path="/", httponly=True, samesite="lax")
            return response

        @app.post("/logout")
        async def logout(request: Request):
            form = await read_form(request)
            if not hmac.compare_digest(form.get("csrf", "").encode(), csrf("logout").encode()):
                raise StarletteHTTPException(403, TOKEN_REFUSED)
            response = RedirectResponse("/login", status_code=303)
            response.delete_cookie(weblogin.COOKIE_NAME, path="/", httponly=True, samesite="lax")
            return response

    @app.get("/", response_class=HTMLResponse)
    def top10(request: Request):
        with contextlib.closing(app.state.open_db()) as conn:
            batch_id = latest_batch(conn)
            rep = report.collect(conn, cfg, batch_id, hide_acted=True) if batch_id else None
            actions, undo, bulk = {}, {}, {}
            if rep and rep.jobs:                           # every job of a top 10 has a score
                ids = [j.job_id for j in rep.jobs]
                for r in conn.execute(f"SELECT id, status FROM jobs WHERE id IN ({','.join('?' * len(ids))})", ids):
                    actions[r["id"]] = allowed_actions(r["status"], True)
                    undo[r["id"]] = undo_info(conn, r["id"], r["status"], True)
                    bulk[r["id"]] = bulk_names("top", r["status"], True, undo[r["id"]])
        return render(request, "top10.html", rep=rep, top_n=cfg["scoring"]["top_n"], outside=report.outside_line(rep, cfg) if rep else "",
                      actions=actions, undo=undo, bulk=bulk)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request, page: str = "1"):
        n = int(page) if page.isascii() and page.isdigit() and 0 < int(page) < 10**6 else 1
        with contextlib.closing(app.state.open_db()) as conn:
            rows, pages = job_list(conn, cfg, n)
        return render(request, "jobs.html", rows=rows, page_no=min(n, pages), pages=pages)

    @app.get("/applied", response_class=HTMLResponse)
    def applied_page(request: Request, page: str = "1"):
        n = int(page) if page.isascii() and page.isdigit() and 0 < int(page) < 10**6 else 1
        with contextlib.closing(app.state.open_db()) as conn:
            rows, pages = applied_list(conn, cfg, n)
            total = stage_count(conn, "applied")
        return render(request, "applied.html", rows=rows, page_no=min(n, pages), pages=pages, total=total)

    def stage_page(page_key: str):
        status = STAGE_STATUSES[page_key]

        def view(request: Request, page: str = "1"):
            n = int(page) if page.isascii() and page.isdigit() and 0 < int(page) < 10**6 else 1
            with contextlib.closing(app.state.open_db()) as conn:
                rows, pages = stage_list(conn, cfg, status, n)
                total = stage_count(conn, status)
            return render(request, "stage.html", rows=rows, page_no=min(n, pages), pages=pages, total=total, page_key=page_key, next_column=page_key in statusflow.NEXT_INTERVIEW_PAGES)
        return view

    for _page in statusflow.APPLICATION_PAGES[1:]:          # /interviews, /offers, /rejected, /withdrawn (v0.96.0); /applied has its own route above
        app.add_api_route(f"/{_page}", stage_page(_page), methods=["GET"], response_class=HTMLResponse, name=f"stage_{_page}")

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job(request: Request, job_id: str):
        if not (job_id.isascii() and job_id.isdigit()) or len(job_id) > 12:
            raise StarletteHTTPException(404)
        with contextlib.closing(app.state.open_db()) as conn:
            detail = job_detail(conn, cfg, int(job_id))
        if detail is None:
            raise StarletteHTTPException(404)
        return render(request, "job.html", j=detail)

    def pdf_response(job_id: str, kind: str, column: str, root: Path) -> FileResponse:
        """The stored PDF of one document of an open job. The file name comes only from the database row and is served only
        when it resolves to a file inside `root`; anything else is a 404."""
        if not (job_id.isascii() and job_id.isdigit()) or len(job_id) > 12 or kind not in KINDS:
            raise StarletteHTTPException(404)
        with contextlib.closing(app.state.open_db()) as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (int(job_id),)).fetchone() is None:
                raise StarletteHTTPException(404)
            row = conn.execute(f"SELECT {column} AS name FROM documents WHERE job_id = ? AND kind = ?", (int(job_id), kind)).fetchone()
        if row is None or not row["name"]:
            raise StarletteHTTPException(404)
        try:
            base = root.resolve()
            target = (base / row["name"]).resolve()
        except (OSError, ValueError):
            raise StarletteHTTPException(404) from None
        if not target.is_relative_to(base) or not target.is_file():
            raise StarletteHTTPException(404)
        return FileResponse(target, media_type="application/pdf", filename=target.name, content_disposition_type="inline")

    @app.get("/jobs/{job_id}/documents/{kind}/pdf")
    def document_pdf(job_id: str, kind: str):
        return pdf_response(job_id, kind, "pdf_path", documents_dir(cfg))

    @app.get("/jobs/{job_id}/documents/{kind}/approved-pdf")
    def document_approved_pdf(job_id: str, kind: str):
        return pdf_response(job_id, kind, "approved_pdf_path", approved_dir(cfg))

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_page(request: Request):
        """Read-only statistics (goal.md 5.8): tables with HTML/CSS bars, all from `app/analytics.py`."""
        with contextlib.closing(app.state.open_db()) as conn:
            data = analytics.analytics(conn, cfg)
        return render(request, "analytics.html", a=data, charts=analytics.chart_data(data))

    @app.get("/health", response_class=HTMLResponse)
    def health_page(request: Request):
        """Runs and health (goal.md 5.9): ids, times, counts and statuses only; never mail text or addresses."""
        with contextlib.closing(app.state.open_db()) as conn:
            data = analytics.health(conn, cfg)
        return render(request, "health.html", h=data, charts=analytics.health_chart_data(data))

    # --- the routes that write: POST only, each needs the CSRF token of its action and job --------------------------

    def _status_line(job_id: int, row, text: str) -> str:
        return f"Job {job_id} ({row['title']}, {row['company']}) {text}"

    def _status_one(conn, jid: int, action: str, to_status: str, note: str, extra=None) -> tuple[str, str, object, bool]:
        """One status change under its own write lock (the body of the single buttons and of the bulk route):
        (level, line, row, gone). `gone`: the job is not open any more. Raises sqlite3.Error when the database is busy.
        `extra(conn)` writes in the same transaction (the first interview round, v0.99.0); it runs after the checks and before the status, and a refused status writes nothing."""
        conn.execute("BEGIN IMMEDIATE")                       # the status is checked and changed under one write lock
        try:
            row = conn.execute("SELECT title, company, status FROM jobs WHERE id = ?", (jid,)).fetchone()
            if row is None:
                return "refused", f"Job {jid} is not open any more. Nothing was changed.", None, True
            scored = conn.execute("SELECT 1 FROM scores WHERE job_id = ? LIMIT 1", (jid,)).fetchone() is not None
            why = refusal(action, jid, row["status"], scored)
            if why:
                return "refused", why, row, False
            if extra is not None:
                extra(conn)
            db.set_status(conn, jid, to_status, note=note)
        finally:
            if conn.in_transaction:
                conn.rollback()
        return "ok", _status_line(jid, row, f"is now {to_status} (was {row['status']})."), row, False

    def _undo_one(conn, jid: int, confirmed: bool) -> tuple[str, str, object, bool, str | None]:
        """One undo under its own write lock: (level, line, row, gone, the status restored). `level` is `unconfirmed` when the
        job needs the user's confirmation and `confirmed` is False (nothing was changed)."""
        conn.execute("BEGIN IMMEDIATE")                       # the history is read and the status changed under one write lock
        try:
            row = conn.execute("SELECT title, company, status FROM jobs WHERE id = ?", (jid,)).fetchone()
            if row is None:
                return "refused", f"Job {jid} is not open any more. Nothing was changed.", None, True, None
            scored = conn.execute("SELECT 1 FROM scores WHERE job_id = ? LIMIT 1", (jid,)).fetchone() is not None
            target, why = undo_target(conn, jid, row["status"], scored)
            if why:
                return "refused", why, row, False, None
            if row["status"] != "shortlisted" and not confirmed:
                return "unconfirmed", "Not confirmed. Nothing was changed.", row, False, target
            db.set_status(conn, jid, target, note=UNDO_NOTE)
        finally:
            if conn.in_transaction:
                conn.rollback()
        return "ok", _status_line(jid, row, f"is back to {target} (it was {row['status']})."), row, False, target

    async def change_status(request: Request, job_id: str, action: str, to_status: str):
        jid, form = await verified(request, action, job_id)
        try:
            with contextlib.closing(app.state.open_rw()) as conn:
                level, line, row, gone = _status_one(conn, jid, action, to_status, "web")
        except sqlite3.Error as exc:
            log.warning("web: %s on job %s failed: %s", action, jid, exc.__class__.__name__)
            return back(form, jid, DB_BUSY)
        if level == "ok":
            log.info("web: job %s %s -> %s", jid, row["status"], to_status)
        return back(form, jid, [(level, line)], gone=gone)

    @app.post("/jobs/{job_id}/shortlist")
    async def shortlist(request: Request, job_id: str):
        return await change_status(request, job_id, "shortlist", "shortlisted")

    @app.post("/jobs/{job_id}/applied")
    async def applied(request: Request, job_id: str):
        return await change_status(request, job_id, "applied", "applied")

    @app.post("/jobs/{job_id}/approve")
    async def approve_documents(request: Request, job_id: str):
        """The web form of `python -m app approve-document`: the same function, the same rules, the same messages."""
        jid, form = await verified(request, "approve", job_id)
        kind = form.get("kind", "both")
        if kind != "both" and kind not in KINDS:
            return back(form, jid, [("refused", "Choose the cover letter, the CV or both. Nothing was changed.")])
        try:
            with contextlib.closing(app.state.open_rw()) as conn:
                if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is None:
                    return back(form, jid, [("refused", f"Job {jid} is not open any more. Nothing was changed.")], gone=True)
                res = approval.approve(conn, cfg, jid, None if kind == "both" else [kind])
        except (sqlite3.Error, OSError) as exc:
            log.warning("web: approve on job %s failed: %s", jid, exc.__class__.__name__)
            return back(form, jid, DB_BUSY)
        return back(form, jid, approval.outcome_lines(res))

    def _row_id(text: str) -> int | None:
        return int(text) if text.isascii() and text.isdigit() and len(text) <= 12 else None

    async def interview_change(request: Request, job_id: str, action: str, what: str, work, refused_to=None, checked=None):
        """One add, edit or delete of a round or a note (v0.98.0): CSRF per action and job, one write under BEGIN IMMEDIATE, a message back on the job page.
        `work(conn, jid, form)` returns (done, message); the texts of notes and places are never logged. `refused_to(jid)`: the path a refused input goes
        back to (default: the job page). `checked`: the (jid, form) of a POST that `verified` already read."""
        jid, form = checked or await verified(request, action, job_id, max_body=MAX_INTERVIEW_BODY)
        try:
            with contextlib.closing(app.state.open_rw()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is None:
                        return back(form, jid, [("refused", f"Job {jid} is not open any more. Nothing was changed.")], gone=True)
                    done, message = work(conn, jid, form)
                    conn.commit()
                finally:
                    if conn.in_transaction:
                        conn.rollback()
        except interviews.Refused as exc:
            if refused_to is not None:
                return go(refused_to(jid), [("refused", str(exc))])
            return back(form, jid, [("refused", str(exc))])
        except sqlite3.Error as exc:
            log.warning("web: %s on job %s failed: %s", action, jid, exc.__class__.__name__)
            return back(form, jid, DB_BUSY)
        if done:
            log.info("web: job %s %s", jid, what)
        return back(form, jid, [("ok" if done else "refused", message)])

    @app.post("/jobs/{job_id}/interviews")
    async def add_interview(request: Request, job_id: str):
        def work(conn, jid, form):
            interviews.add_round(conn, jid, form.get("day", ""), form.get("clock", ""), form.get("kind", ""), form.get("place", ""),
                                 form.get("notes", ""), cfg["timezone"])
            return True, "Interview round added."
        return await interview_change(request, job_id, "interviews", "interview round added", work)

    def _confirm_page(kind: str, job_id: str, item_id: str):
        """The server confirmation page of a delete (read-only): what confirm.js asks in the browser, and the fallback without script."""
        jid, iid = _row_id(job_id), _row_id(item_id)
        if jid is None or iid is None:
            raise StarletteHTTPException(404)
        with contextlib.closing(app.state.open_db()) as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is None:
                raise StarletteHTTPException(404)
            item = (interviews.get_round if kind == "round" else interviews.get_note)(conn, jid, iid, cfg["timezone"])
        if item is None:
            raise StarletteHTTPException(404)
        return jid, iid, item

    @app.get("/jobs/{job_id}/interviews/{round_id}/delete", response_class=HTMLResponse)
    def delete_interview_page(request: Request, job_id: str, round_id: str):
        jid, rid, item = _confirm_page("round", job_id, round_id)
        return render(request, "delete_confirm.html", j={"id": jid}, kind="round", item=item, action=f"/jobs/{jid}/interviews/{rid}/delete", purpose=f"interview-delete:{jid}")

    @app.post("/jobs/{job_id}/interviews/{round_id}/delete")
    async def delete_interview(request: Request, job_id: str, round_id: str):
        """Deletes only with `confirm=yes` (confirm.js or the confirmation page); anything else goes to the confirmation page, nothing changed."""
        def work(conn, jid, form):
            rid = _row_id(round_id)
            if rid is not None and interviews.delete_round(conn, jid, rid):
                return True, "Interview round deleted."
            return False, "That interview round does not exist any more. Nothing was changed."
        return await _confirmed_change(request, job_id, "interview-delete", "interview round deleted", work,
                                       lambda jid: f"/jobs/{jid}/interviews/{rid}/delete" if (rid := _row_id(round_id)) is not None else f"/jobs/{jid}")

    @app.get("/jobs/{job_id}/interviews/{round_id}/edit", response_class=HTMLResponse)
    def edit_interview_page(request: Request, job_id: str, round_id: str):
        jid, rid, item = _confirm_page("round", job_id, round_id)
        return render(request, "interview_edit.html", j={"id": jid}, r=item, round_kinds=interviews.KINDS)

    @app.post("/jobs/{job_id}/interviews/{round_id}")
    async def edit_interview(request: Request, job_id: str, round_id: str):
        def work(conn, jid, form):
            rid = _row_id(round_id)
            if rid is not None and interviews.update_round(conn, jid, rid, form.get("day", ""), form.get("clock", ""), form.get("kind", ""),
                                                           form.get("place", ""), form.get("notes", ""), cfg["timezone"]):
                return True, "Interview round changed."
            return False, "That interview round does not exist any more. Nothing was changed."
        return await interview_change(request, job_id, "interview-edit", "interview round changed", work, lambda jid: f"/jobs/{jid}/interviews/{round_id}/edit")

    async def _confirmed_change(request: Request, job_id: str, action: str, what: str, work, page):
        """A delete: the token is checked first, then `confirm=yes`; without it nothing is opened or changed and the user goes to the confirmation page."""
        jid, form = await verified(request, action, job_id, max_body=MAX_INTERVIEW_BODY)
        if form.get("confirm") != "yes":
            return go(page(jid), [("refused", "Not confirmed. Nothing was changed.")])
        return await interview_change(request, job_id, action, what, work, checked=(jid, form))

    @app.post("/jobs/{job_id}/notes")
    async def add_job_note(request: Request, job_id: str):
        def work(conn, jid, form):
            interviews.add_note(conn, jid, form.get("text", ""))
            return True, "Note added."
        return await interview_change(request, job_id, "notes", "note added", work)

    @app.get("/jobs/{job_id}/notes/{note_id}/delete", response_class=HTMLResponse)
    def delete_job_note_page(request: Request, job_id: str, note_id: str):
        jid, nid, item = _confirm_page("note", job_id, note_id)
        return render(request, "delete_confirm.html", j={"id": jid}, kind="note", item=item, action=f"/jobs/{jid}/notes/{nid}/delete", purpose=f"note-delete:{jid}")

    @app.post("/jobs/{job_id}/notes/{note_id}/delete")
    async def delete_job_note(request: Request, job_id: str, note_id: str):
        def work(conn, jid, form):
            nid = _row_id(note_id)
            if nid is not None and interviews.delete_note(conn, jid, nid):
                return True, "Note deleted."
            return False, "That note does not exist any more. Nothing was changed."
        return await _confirmed_change(request, job_id, "note-delete", "note deleted", work,
                                       lambda jid: f"/jobs/{jid}/notes/{nid}/delete" if (nid := _row_id(note_id)) is not None else f"/jobs/{jid}")

    def _accept_text(form: dict) -> str | None:
        comment = "".join(c for c in form.get("comment", "") if c.isprintable() or c.isspace())
        comment = rejection.clean_comment(comment)
        return comment if 1 <= len(comment) <= COMMENT_MAX else None

    def _recheck_lines(conn, jid: int) -> list[tuple[str, str]]:
        out = documents.recheck_blocked(conn, cfg, apply=True, job_id=jid)
        if not out:
            return [("ok", f"Job {jid} has no blocked document to recheck.")]
        lines = []
        for o in out:
            name = o["kind"].replace("_", " ")
            if o["status"] == "changed":
                lines.append(("refused", f"{name}: changed meanwhile (a new results file?), not rechecked. Reload the page."))
            elif o["status"] == "draft":
                lines.append(("ok", f"{name}: no blocking finding is left, it is a draft now (PDF rendered)."))
            else:
                lines.append(("refused", f"{name}: still blocked by the checks."))
        return lines

    def _locked(form: dict, jid: int):
        if pipeline.lock_is_held(cfg):
            return back(form, jid, [("refused", "A pipeline run is in progress, so nothing was changed. Try again when it has finished.")])
        return None

    @app.post("/jobs/{job_id}/recheck")
    async def recheck(request: Request, job_id: str):
        """Run today's checks again on the blocked documents of this job; clean ones become drafts and get their PDF."""
        jid, form = await verified(request, "recheck", job_id)
        if (busy := _locked(form, jid)) is not None:
            return busy

        def work():
            with contextlib.closing(app.state.open_rw()) as conn:
                if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is None:
                    return back(form, jid, [("refused", f"Job {jid} is not open any more. Nothing was changed.")], gone=True)
                return back(form, jid, _recheck_lines(conn, jid))
        try:
            return await run_in_threadpool(work)
        except (sqlite3.Error, OSError) as exc:
            log.warning("web: recheck on job %s failed: %s", jid, exc.__class__.__name__)
            return back(form, jid, DB_BUSY)

    @app.post("/jobs/{job_id}/revise")
    async def revise(request: Request, job_id: str):
        """"Ask the writer to fix" (v0.85.0, goal.md 5.10a): store a request row for a blocked document; the scheduler sends it to the llm
        service. The instruction is optional (0 to 1000 characters; empty means: fix the blocking findings). A held run lock does not
        refuse the press: the request is queued. The press is the user's go for this one writer call and one review."""
        jid, form = await verified(request, "revise", job_id, max_body=MAX_REVISE_BODY)
        if not cfg["headless"]["enabled"]:
            return back(form, jid, [("refused", "The headless writer is switched off (headless.enabled). Nothing was changed.")])
        kind = form.get("doc", "")
        if kind not in KINDS:
            return back(form, jid, [("refused", "Unknown document. Nothing was changed.")])
        instruction = form.get("instruction", "").replace("\r\n", "\n").replace("\r", "\n")
        instruction = "".join(c for c in instruction if c.isprintable() or c == "\n").strip()
        if len(instruction) > REVISE_INSTRUCTION_MAX:
            return back(form, jid, [("refused", f"The instruction may have at most {REVISE_INSTRUCTION_MAX} characters "
                                                f"(it has {len(instruction)}). Nothing was changed.")])

        def work():
            with contextlib.closing(app.state.open_rw()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute("SELECT d.id, d.status, d.content FROM documents d JOIN jobs j ON j.id = d.job_id "
                                       "WHERE d.job_id = ? AND d.kind = ?", (jid, kind)).fetchone()
                    if row is None:
                        return back(form, jid, [("refused", f"Job {jid} has no such document. Nothing was changed.")])
                    if not documents.revisable(row):
                        return back(form, jid, [("refused", f"The {kind.replace('_', ' ')} is not blocked. Nothing was changed.")])
                    if conn.execute("SELECT 1 FROM revision_requests WHERE document_id = ? AND status IN ('pending', 'relayed')", (row["id"],)).fetchone():
                        return back(form, jid, [("refused", f"The {kind.replace('_', ' ')} already has a request in progress. Nothing was changed.")])
                    conn.execute("INSERT INTO revision_requests (document_id, kind, instruction, content_sha, status, created_at) "
                                 "VALUES (?, ?, ?, ?, 'pending', ?)", (row["id"], kind, instruction, documents.content_sha(row["content"]), db.utcnow()))
                    conn.commit()
                except sqlite3.IntegrityError:                             # the partial unique index: a second press at the same moment
                    return back(form, jid, [("refused", f"The {kind.replace('_', ' ')} already has a request in progress. Nothing was changed.")])
                finally:
                    if conn.in_transaction:
                        conn.rollback()
            log.info("web: revision requested on job %s %s", jid, kind)          # no instruction text in the log
            queued = " It is queued and sent when the running pipeline step has finished." if pipeline.lock_is_held(cfg) else ""
            return back(form, jid, [("ok", f"The request for the {kind.replace('_', ' ')} is stored; the writer and the reviewer will work on it "
                                           f"and you get a mail when they are done.{queued}")])
        try:
            return await run_in_threadpool(work)
        except (sqlite3.Error, OSError) as exc:
            log.warning("web: revise on job %s failed: %s", jid, exc.__class__.__name__)
            return back(form, jid, DB_BUSY)

    @app.post("/jobs/{job_id}/findings/accept")
    async def accept_finding(request: Request, job_id: str):
        """Accept one finding as a false positive (a comment is required), recheck, and with `then=approve` approve that
        document when nothing blocks it. Language and address findings are refused."""
        jid, form = await verified(request, "accept", job_id)
        comment = _accept_text(form)
        if comment is None:
            return back(form, jid, [("refused", f"A comment of 1 to {COMMENT_MAX} characters is required. Nothing was changed.")])
        kind = form.get("doc", "")
        if kind not in KINDS:
            return back(form, jid, [("refused", "Unknown document. Nothing was changed.")])
        if (busy := _locked(form, jid)) is not None:
            return busy
        stored = [False]

        def work():
            with contextlib.closing(app.state.open_rw()) as conn:
                if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is None:
                    return back(form, jid, [("refused", f"Job {jid} is not open any more. Nothing was changed.")], gone=True)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    why = documents.add_override(conn, jid, kind, form.get("fkind", ""), form.get("fwhere", ""), form.get("ftext", ""), comment)
                    if why:
                        return back(form, jid, [("refused", f"{why}. Nothing was changed.")])
                    conn.commit()
                finally:
                    if conn.in_transaction:
                        conn.rollback()
                stored[0] = True
                log.info("web: finding accepted on job %s %s (%s)", jid, kind, form.get("fkind", ""))
                lines = [("ok", f"Finding accepted as a false positive on the {kind.replace('_', ' ')}.")] + _recheck_lines(conn, jid)
                if form.get("then") == "approve":
                    row = conn.execute("SELECT status FROM documents WHERE job_id = ? AND kind = ?", (jid, kind)).fetchone()
                    if row is None or row["status"] == "blocked":
                        lines.append(("refused", f"Not approved: the {kind.replace('_', ' ')} is still blocked."))
                    else:
                        lines += approval.outcome_lines(approval.approve(conn, cfg, jid, [kind]))
                return back(form, jid, lines)
        try:
            return await run_in_threadpool(work)
        except (sqlite3.Error, OSError) as exc:
            log.warning("web: accept on job %s failed: %s", jid, exc.__class__.__name__)
            if stored[0]:
                return back(form, jid, [("ok", "The finding was accepted and stored."),
                                        ("refused", "The recheck did not finish. Press Recheck to run it again.")])
            return back(form, jid, DB_BUSY)

    @app.get("/jobs/{job_id}/reject", response_class=HTMLResponse)
    def reject_page(request: Request, job_id: str):
        """The confirmation page of Reject (read-only): nothing happens until its form is sent."""
        if not (job_id.isascii() and job_id.isdigit()) or len(job_id) > 12:
            raise StarletteHTTPException(404)
        with contextlib.closing(app.state.open_db()) as conn:
            row = conn.execute("SELECT id, title, company, status FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
            scored = row is not None and conn.execute("SELECT 1 FROM scores WHERE job_id = ? LIMIT 1", (row["id"],)).fetchone() is not None
        if row is None:
            raise StarletteHTTPException(404)
        nxt = request.query_params.get("next")
        return render(request, "reject.html", j=dict(row), nxt=nxt if nxt in NEXT_PAGES else "job", comment_max=COMMENT_MAX,
                      why_not=refusal("reject", row["id"], row["status"], scored))

    @app.post("/jobs/{job_id}/reject")
    async def reject(request: Request, job_id: str):
        """Archive the job as rejected by the user (goal.md 5.3.3). Only a form sent from the confirmation page counts."""
        jid, form = await verified(request, "reject", job_id)
        nxt = form.get("next") if form.get("next") in NEXT_PAGES else "job"
        comment = rejection.clean_comment(form.get("comment", ""))
        if form.get("confirm") != "yes":
            return go(f"/jobs/{jid}/reject?next={nxt}", [("refused", "Not confirmed. Nothing was changed.")])
        if len(comment) > COMMENT_MAX:
            return go(f"/jobs/{jid}/reject?next={nxt}", [("refused", f"The comment is longer than {COMMENT_MAX} characters. Nothing was changed.")])
        try:
            with contextlib.closing(app.state.open_rw()) as conn:
                done = rejection.reject_job(conn, jid, comment, cfg["paths"]["approved"])
        except rejection.RejectRefused as exc:
            return back(form, jid, [("refused", str(exc))], gone=exc.gone)
        except sqlite3.Error as exc:
            log.warning("web: reject on job %s failed: %s", jid, exc.__class__.__name__)
            return back(form, jid, DB_BUSY)
        log.info("web: job %s archived as rejected by the user (archive entry %s)", jid, done.archive_id)
        return back(form, jid, [("ok", _status_line(jid, {"title": done.title, "company": done.company}, f"was rejected and moved to the archive (archive entry {done.archive_id}, reason user_rejected). "
                                       f"It is gone from the working lists. To bring it back: python -m app restore-job {done.archive_id}"))], gone=True)

    @app.get("/missing", response_class=HTMLResponse)
    def missing_page(request: Request):
        """The queue of jobs that would be in the top 10 but have no description (read-only), with the bookmarklet."""
        with contextlib.closing(app.state.open_db()) as conn:
            rows = missing_rows(conn)
        return render(request, "missing.html", rows=rows, bookmarklet=BOOKMARKLET)

    def paste_page_response(request: Request, job_id: int, status: int = 200, text: str = "", lines: list | None = None):
        with contextlib.closing(app.state.open_db()) as conn:
            row = conn.execute("SELECT id, title, company, status, description_missing FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise StarletteHTTPException(404)
            links = [l for l in dict.fromkeys(report.safe_url(s["url"]) for s in conn.execute(
                "SELECT url FROM job_sources WHERE job_id = ? ORDER BY id", (job_id,))) if l]
            lines = list(lines or [])
            if not text and not lines and row["description_missing"] and (partial := descriptions.partial_text(conn, job_id)):
                text = partial[0]                                                    # GET only: the partial text of the resolver, to check and complete
                lines.append(("note", f"Prefilled with {len(text)} characters found on {partial[1]} on {partial[3]}; check and complete it (at least 500)."))
        return render(request, "describe.html", status, j=dict(row), links=links, text=text, lines=lines)

    @app.get("/jobs/{job_id}/description", response_class=HTMLResponse)
    def description_page(request: Request, job_id: str):
        """The paste page (read-only): a box for the text of the posting; nothing happens until its form is sent."""
        if not (job_id.isascii() and job_id.isdigit()) or len(job_id) > 12:
            raise StarletteHTTPException(404)
        return paste_page_response(request, int(job_id))

    @app.post("/jobs/{job_id}/description")
    async def describe(request: Request, job_id: str):
        """Store a pasted description (goal.md 5.1, 5.7). Text under 500 characters is refused, a missing tasks or requirements part only warns (v0.85.3); the job is scored again in the next batch."""
        jid, form = await verified(request, "describe", job_id, max_body=MAX_DESCRIPTION_BODY)
        text = descriptions.strip_page_clutter(descriptions.clean_text(form.get("text", "")))
        found = completeness_problems(text)
        if found:
            return paste_page_response(request, jid, 422, text, [("refused", "The text is not complete, so nothing was stored: "
                                                                   + "; ".join(found) + ". Paste the whole posting.")])
        if pipeline.lock_is_held(cfg):
            return paste_page_response(request, jid, 409, text, [("refused", "A pipeline run is in progress, so nothing was stored. "
                                                                   "Try again when it has finished.")])
        try:
            with contextlib.closing(app.state.open_rw()) as conn:
                row = conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (jid,)).fetchone()
                if row is None:
                    return go("/missing", [("refused", f"Job {jid} is not open any more. Nothing was changed.")])
                if not row["description_missing"]:
                    return go("/missing", [("refused", f"Job {jid} already has a description. Nothing was changed.")])
                res = descriptions.apply_description(conn, cfg, jid, text, source="web", require_complete=True)
        except descriptions.DescriptionError as exc:
            return paste_page_response(request, jid, 422, text, [("refused", f"{exc}. Nothing was stored.")])
        except sqlite3.Error as exc:
            log.warning("web: description on job %s failed: %s", jid, exc.__class__.__name__)
            return paste_page_response(request, jid, 503, text, DB_BUSY)
        log.info("web: description stored for job %s (%s characters, %s)", jid, res.chars, res.outcome)
        if res.outcome == "archived":
            return go("/missing", [("ok", f"Job {jid}: {res.message}.")])
        note = "it will be scored again in the next batch" if res.rescore else "it is scored once it is in a batch"
        flash = [("ok", f"Job {jid}: description stored ({res.chars} characters); {note}.")]
        if res.warning:
            flash.append(("warning", f"Job {jid}: check the text, {res.warning}. It was stored anyway."))
        return go("/missing", flash)

    # --- undo (v0.49.0): the approval of documents, and the status Shortlisted / Applied ----------------------------------

    def _doc_states(conn: sqlite3.Connection, job_id: int) -> dict[str, str]:
        return {r["kind"]: r["status"] for r in conn.execute("SELECT kind, status FROM documents WHERE job_id = ?", (job_id,))}

    @app.get("/jobs/{job_id}/undo-approval", response_class=HTMLResponse)
    def undo_approval_page(request: Request, job_id: str):
        """The confirmation page of taking an approval back (read-only): it unfreezes documents, so nothing happens until its form is sent."""
        if not (job_id.isascii() and job_id.isdigit()) or len(job_id) > 12:
            raise StarletteHTTPException(404)
        with contextlib.closing(app.state.open_db()) as conn:
            row = conn.execute("SELECT id, title, company, status FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
            states = _doc_states(conn, row["id"]) if row is not None else {}
        if row is None:
            raise StarletteHTTPException(404)
        kind = request.query_params.get("kind")
        kind = kind if kind in KINDS else "both"
        nxt = request.query_params.get("next")
        chosen = [k for k in KINDS if kind in ("both", k) and states.get(k) == "approved"]
        return render(request, "undo_approval.html", j=dict(row), kind=kind, nxt=nxt if nxt in NEXT_PAGES else "job",
                      chosen=[k.replace("_", " ") for k in chosen], further=row["status"] not in ("approved",) + approval.BEFORE_APPROVED)

    @app.post("/jobs/{job_id}/undo-approval")
    async def undo_approval(request: Request, job_id: str):
        """The web form of `python -m app approve-document --undo`: the same function, the same rules, the same messages."""
        jid, form = await verified(request, "undo-approval", job_id)
        nxt = form.get("next") if form.get("next") in NEXT_PAGES else "job"
        kind = form.get("kind", "both")
        if kind != "both" and kind not in KINDS:
            return back(form, jid, [("refused", "Choose the cover letter, the CV or both. Nothing was changed.")])
        if form.get("confirm") != "yes":
            return go(f"/jobs/{jid}/undo-approval?kind={kind}&next={nxt}", [("refused", "Not confirmed. Nothing was changed.")])
        try:
            with contextlib.closing(app.state.open_rw()) as conn:
                if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is None:
                    return back(form, jid, [("refused", f"Job {jid} is not open any more. Nothing was changed.")], gone=True)
                res = approval.undo(conn, cfg, jid, None if kind == "both" else [kind])
        except (sqlite3.Error, OSError) as exc:
            log.warning("web: undo of the approval on job %s failed: %s", jid, exc.__class__.__name__)
            return back(form, jid, DB_BUSY)
        return back(form, jid, approval.outcome_lines(res, undo=True))

    @app.get("/jobs/{job_id}/undo-status", response_class=HTMLResponse)
    def undo_status_page(request: Request, job_id: str):
        """The confirmation page of Undo (read-only); Applied always comes here first, nothing happens until its form is sent."""
        if not (job_id.isascii() and job_id.isdigit()) or len(job_id) > 12:
            raise StarletteHTTPException(404)
        with contextlib.closing(app.state.open_db()) as conn:
            row = conn.execute("SELECT id, title, company, status FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
            scored = row is not None and conn.execute("SELECT 1 FROM scores WHERE job_id = ? LIMIT 1", (row["id"],)).fetchone() is not None
            target, why_not = undo_target(conn, row["id"], row["status"], scored) if row is not None else (None, None)
        if row is None:
            raise StarletteHTTPException(404)
        nxt = request.query_params.get("next")
        return render(request, "undo_status.html", j=dict(row), nxt=nxt if nxt in NEXT_PAGES else "job", target=target, why_not=why_not)

    @app.post("/jobs/{job_id}/undo-status")
    async def undo_status(request: Request, job_id: str):
        """Set a shortlisted or applied job one step back (goal.md 5.7). Applied needs the confirmation page's `confirm=yes`."""
        jid, form = await verified(request, "undo-status", job_id)
        nxt = form.get("next") if form.get("next") in NEXT_PAGES else "job"
        try:
            with contextlib.closing(app.state.open_rw()) as conn:
                level, line, row, gone, target = _undo_one(conn, jid, form.get("confirm") == "yes")
        except sqlite3.Error as exc:
            log.warning("web: undo on job %s failed: %s", jid, exc.__class__.__name__)
            return back(form, jid, DB_BUSY)
        if level == "unconfirmed":
            return go(f"/jobs/{jid}/undo-status?next={nxt}", [("refused", line)])
        if level == "ok":
            log.info("web: job %s %s -> %s (undo)", jid, row["status"], target)
        return back(form, jid, [(level, line)], gone=gone)

    # --- the bulk status change (v0.96.0): a confirmation page (GET) and the change (POST) for a selection of jobs --------

    def bulk_entries(conn, action: str, ids: list[int]) -> list[dict]:
        """For each job of the selection: what the action would do, or why it would be skipped (the server decides again on POST)."""
        out = []
        for jid in ids:
            row = conn.execute("SELECT id, title, company, status FROM jobs WHERE id = ?", (jid,)).fetchone()
            if row is None:
                out.append({"id": jid, "title": None, "company": "", "status": "", "will": None,
                            "skipped": f"Job {jid} is not open any more. Nothing was changed."})
                continue
            scored = conn.execute("SELECT 1 FROM scores WHERE job_id = ? LIMIT 1", (jid,)).fetchone() is not None
            entry = {"id": jid, "title": row["title"], "company": row["company"], "status": row["status"], "will": None, "skipped": None}
            if action == "undo":
                target, why = undo_target(conn, jid, row["status"], scored)
                entry["will"], entry["skipped"] = (f"will be set back to {target}" if target else None), why
            else:
                why = refusal(action, jid, row["status"], scored)
                entry["skipped"] = why
                if not why:
                    entry["will"] = "will be rejected and archived" if action == "reject" else f"will become {statusflow.FORWARD[action][0]}"
            out.append(entry)
        return out

    def bulk_confirm_page(request: Request, action: str, ids: list[int], nxt: str, page: int, problem: str | None = None, status: int = 200):
        entries, token = [], ""
        if problem is None:
            with contextlib.closing(app.state.open_db()) as conn:
                entries = bulk_entries(conn, action, ids)
            token = csrf(f"bulk:{action}")
        n_ok = sum(1 for e in entries if e["will"])
        return render(request, "bulk_confirm.html", status, problem=problem, action=action, label=bulk_label(action, nxt) if action in BULK_ACTIONS else "",
                      entries=entries, n_ok=n_ok, n_skipped=len(entries) - n_ok, ids_csv=",".join(map(str, ids)), nxt=nxt, page=page,
                      token=token, comment_max=COMMENT_MAX, cancel=bulk_path(nxt, page, ids) if nxt in BULK_PAGES else "/",
                      applied_warning=action == "reject" and any(e["will"] and e["status"] == "applied" for e in entries))

    def bulk_problem(action: str, nxt: str, ids: list[int]) -> str | None:
        """Why this action on this page for these ids is not accepted at all, or None."""
        if action not in BULK_ACTIONS:
            return "This action is not known. Nothing was changed."
        if nxt not in BULK_PAGES:
            return "This page is not known. Nothing was changed."
        if nxt == "job":
            if len(ids) != 1:
                return "From a job page exactly one job can be changed. Nothing was changed."
        elif action not in statusflow.PAGE_ACTIONS[nxt]:
            return f"{bulk_label(action, nxt)} is not offered on this page. Nothing was changed."
        return None

    @app.get("/bulk-status", response_class=HTMLResponse)
    def bulk_status_page(request: Request):
        """The confirmation page of a selection (read-only, no JavaScript): nothing happens until its form is sent."""
        q = request.query_params
        action, nxt, page = q.get("action", ""), q.get("next", ""), bulk_page_no(q.get("page"))
        ids, why = parse_bulk_ids(q.getlist("id"))
        problem = why or bulk_problem(action, nxt, ids)
        if problem:
            return bulk_confirm_page(request, action, [], nxt if nxt in BULK_PAGES else "top", page, problem=problem, status=400)
        return bulk_confirm_page(request, action, ids, nxt, page)

    @app.post("/bulk-status")
    async def bulk_status(request: Request):
        """Change the status of a selection of jobs after the confirmation page (goal.md 5.7). The token is per action."""
        form = await read_form(request)
        action = form.get("action", "")
        if action not in BULK_ACTIONS:
            raise StarletteHTTPException(400, "This action is not known. Nothing was changed.")
        if not hmac.compare_digest(form.get("csrf", "").encode(), csrf(f"bulk:{action}").encode()):
            raise StarletteHTTPException(403, TOKEN_REFUSED)                       # before the database is opened
        nxt, page = form.get("next", ""), bulk_page_no(form.get("page"))
        ids, why = parse_bulk_ids([form.get("ids", "")])
        home = bulk_path(nxt, page, ids) if nxt in BULK_PAGES and (nxt != "job" or len(ids) == 1) else "/"
        if why := why or bulk_problem(action, nxt, ids):
            return go(home if nxt != "job" else "/jobs", [("refused", why)])
        query = "&".join(f"id={i}" for i in ids)
        again = f"/bulk-status?action={action}&{query}&next={nxt}&page={page}"
        if form.get("confirm") != "yes":
            return go(again, [("refused", "Not confirmed. Nothing was changed.")])
        comment = rejection.clean_comment(form.get("comment", "")) if action == "reject" else ""
        if len(comment) > COMMENT_MAX:
            return go(again, [("refused", f"The comment is longer than {COMMENT_MAX} characters. Nothing was changed.")])
        first_round = None                                                          # the optional first interview round of the sheet (v0.99.0): one job, date and time together
        if action == "interview" and (form.get("day", "").strip() or form.get("clock", "").strip()):
            try:
                if len(ids) != 1:
                    raise interviews.Refused("A date can be given for one job only. Nothing was changed.")
                if not (form.get("day", "").strip() and form.get("clock", "").strip()):
                    raise interviews.Refused("Give the date and the time of the interview round together, or neither. Nothing was changed.")
                first_round = interviews.parse_round(form["day"], form["clock"], form.get("kind") or interviews.KINDS[0], "", "", cfg["timezone"])
            except interviews.Refused as exc:
                return go(again, [("refused", str(exc))])
        done: list[str] = []                                                        # the names of the jobs that were changed
        skipped: list[str] = []                                                     # why the others were not
        archive_ids: list[str] = []
        left: list[int] = []                                                        # the jobs not reached when the database got busy
        gone_any = False
        try:
            with contextlib.closing(app.state.open_rw()) as conn:
                for n, jid in enumerate(ids):
                    try:
                        if action == "undo":
                            level, line, row, gone, target = _undo_one(conn, jid, True)
                            to_status = target
                        elif action == "reject":
                            try:
                                out = rejection.reject_job(conn, jid, comment, cfg["paths"]["approved"])
                            except rejection.RejectRefused as exc:
                                level, line, row, gone = "refused", str(exc), None, exc.gone
                            else:
                                level, line, row, gone, to_status = "ok", "", {"title": out.title, "company": out.company, "status": out.status}, True, None
                                archive_ids.append(f"{jid} (archive entry {out.archive_id})")
                        else:
                            to_status = statusflow.FORWARD[action][0]
                            level, line, row, gone = _status_one(conn, jid, action, to_status, BULK_NOTE,
                                                                 extra=(lambda c: interviews.insert_round(c, jid, first_round)) if first_round else None)
                    except sqlite3.Error as exc:
                        log.warning("web: bulk %s on job %s failed: %s", action, jid, exc.__class__.__name__)
                        left = ids[n:]
                        break
                    if level == "ok":
                        done.append(f"{jid} {row['title']} ({row['company']})")
                        log.info("web: job %s %s -> %s (bulk)", jid, row["status"], "archive" if action == "reject" else to_status)
                    else:
                        skipped.append(line)
                    gone_any = gone_any or gone
        except sqlite3.Error as exc:                                                # the database could not be opened or locked at all
            log.warning("web: bulk %s failed: %s", action, exc.__class__.__name__)
            left = ids
        lines: list[tuple[str, str]] = []
        jobs = f"{len(done)} job{'s' if len(done) != 1 else ''}"
        if done and action == "reject":
            lines.append(("ok", f"{jobs} rejected and moved to the archive (reason user_rejected): {_names(done)}. They are gone from the working lists. "
                                f"To bring one back: python -m app restore-job <archive entry> ({_names(archive_ids)})."))
        elif done and action == "undo":
            lines.append(("ok", f"{jobs} set back one step: {_names(done)}."))
        elif done:
            added = f" Interview round on {' '.join(interviews.to_local(first_round['starts_at'], cfg['timezone']))} added." if first_round else ""
            lines.append(("ok", f"{jobs} {'is' if len(done) == 1 else 'are'} now {statusflow.FORWARD[action][0]}: {_names(done)}.{added}"))
        lines += [("refused", t) for t in skipped[:BULK_LISTED]]
        if len(skipped) > BULK_LISTED:
            lines.append(("refused", f"and {len(skipped) - BULK_LISTED} more were skipped."))
        if left:
            lines.append(("refused", f"{'Job' if len(left) == 1 else 'Jobs'} {_names([str(i) for i in left])} {'was' if len(left) == 1 else 'were'} not changed: "
                                     + rejection.DB_BUSY_TEXT))
        log.info("web: bulk %s: %s done, %s skipped, %s not reached", action, len(done), len(skipped), len(left))
        return go(bulk_path(nxt, page, ids, gone=gone_any), lines)

    return app


def serve(cfg: dict, host: str | None = None, port: int | None = None, run=None) -> int:
    """Start the server (blocking). `run` replaces `uvicorn.run` in tests. Refuses a non-local host before anything starts."""
    host = check_host(host or cfg["web"]["host"])
    port = check_port(port if port is not None else cfg["web"]["port"])
    bind, login = check_bind(cfg, host)
    served = ALLOWED_HOSTS + tuple(cfg["web"]["allowed_hosts"])
    app = create_app(cfg, allowed_hosts=served, login=login)
    if run is None:
        import uvicorn
        run = uvicorn.run
    if bind == CONTAINER_BIND_HOST:
        print(f"Recruitment web app (container): listening on {bind}:{port} inside the container, login required, "
              f"Host names served: {', '.join(served)}")
    else:
        print(f"Recruitment web app: http://{'[::1]' if host == '::1' else host}:{port}/  "
              f"({'login required, ' if login else ''}this computer only; Ctrl+C stops it)")
    run(app, host=bind, port=port, log_level="warning")
    return 0
