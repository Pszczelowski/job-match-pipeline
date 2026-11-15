"""Automatic discovery of the same job (goal.md 5.1a (a), v0.73.0): a job that came without a description (a LinkedIn alert) is
looked up by company and title where the company publishes it, without a source in the config.

Two stages per job, after the filters (so no request is made for a job the filters reject):

A. local twin (no request): an open job of the same company that already has a complete description (an employer-site
   adapter job) and a close title and the same place; its text is copied and the job is marked as a duplicate of it.
B. guessed public job boards: the board names a company is likely to use (`slugs_for`) are asked on the public interface of each
   enabled platform (Greenhouse, Ashby and Lever, tier T1; Ashby and Lever only with the names built from the whole company name, because their
   postings do not name the company). A guess is one list request; the answer is cached in `board_guesses`
   (hit, miss, other company, error), so a miss is not asked again every day. A match is confident only when the posting
   names the same company (`company_ok`), the title scores at least 0.90 with a margin of 0.10 over the runner-up, both sides
   state a place and the places agree, and the text is complete (`complete.is_complete`).

Rules as for the resolver (`resolver/__init__.py`): the hosts are those of the enabled platforms, robots.txt is read first and
respected, an honest User-Agent, a delay between requests, the run stops at the first 403, 429, 5xx or network error, no login.
Limits: at most `new_companies_per_day` companies get board-name guesses per day, job attempts per host and day
`daily_cap_per_host`, one discovery attempt per job and day. A dry run asks the sites and writes nothing but the `board_guesses`
(since v0.75.0: the company budget of the day is shared by the dry run and the real run).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..dedup import normalize_company
from ..descriptions import MAX_CHARS, DescriptionError, apply_description, queued_jobs
from ..descriptions_fetch import FetchError
from . import Outcome, ResolveSummary, _job_places, _record, _Session
from . import ashby, greenhouse, lever
from .common import DISCOVERY_PLATFORMS, PlatformError, Refused, StopRun
from .complete import is_complete
from .match import Candidate, choose, normalise_place

PLATFORMS = {"greenhouse": greenhouse, "ashby": ashby, "lever": lever}
ACCEPT = 0.90                           # title score on a guessed board (the named sources use 0.85)
MAX_CHECKS = 4                          # probes of one guessed name inside the window
MAX_SLUGS = 3                           # names per platform and company
COUNTRIES = {"switzerland", "schweiz", "suisse", "svizzera", "germany", "deutschland", "austria", "osterreich", "france", "italy",
             "italia", "liechtenstein", "united kingdom", "uk", "united states", "usa", "finland", "poland", "spain", "netherlands"}
GENERIC = {"group", "holding", "management", "services", "switzerland", "schweiz", "suisse", "international", "europe"}


# --- guessing -------------------------------------------------------------------------------------------

def slugs_for(company: str) -> list[str]:
    """The board names a company is likely to use: compact, hyphenated, compact without generic words (each at least 3 characters, at most 3 names)."""
    words = re.findall(r"[a-z0-9]+", normalize_company(company))
    plain = [w for w in words if w not in GENERIC]
    guesses = ["".join(words), "-".join(words) if len(words) > 1 else "", "".join(plain)]
    out: list[str] = []
    for g in guesses:
        if len(g) >= 3 and g not in out:
            out.append(g)
    return out[:MAX_SLUGS]


def company_matches(company_norm: str, name: str | None) -> bool:
    """The board's company is the job's company, or one starts with the other as a whole word ("AXA Versicherungen" for "AXA")."""
    if not name or not company_norm:
        return False
    other = normalize_company(name)
    return bool(other) and (other == company_norm or other.startswith(company_norm + " ") or company_norm.startswith(other + " "))


def full_slugs(company_norm: str) -> list[str]:
    """The board names built from all words of the company: compact and hyphenated (each at least 3 characters)."""
    words = re.findall(r"[a-z0-9]+", company_norm)
    out: list[str] = []
    for g in ("".join(words), "-".join(words) if len(words) > 1 else ""):
        if len(g) >= 3 and g not in out:
            out.append(g)
    return out


PLACE_WORDS = {"schweiz", "suisse", "svizzera", "switzerland", "zurich", "zürich", "geneva", "genf", "geneve", "basel", "bern",
               "lausanne", "zug", "group", "holding", "international"}      # "swiss" stays: it is part of brands ("Swiss Re")


def strip_place_words(company_norm: str) -> tuple[str, bool]:
    """The company name without place and group words (v0.75.0), and whether "group" or "holding" was among the dropped words.
    Nothing is dropped when no word would remain."""
    words = company_norm.split()
    kept = [w for w in words if w not in PLACE_WORDS]
    if not kept or len(kept) == len(words):
        return company_norm, False
    return " ".join(kept), any(w in ("group", "holding") for w in words if w in PLACE_WORDS)


def company_ok(company_norm: str, slug: str, name: str | None, names_company: bool = True) -> bool:
    """Whether a board guessed with `slug` belongs to the job's company (v0.75.0, stricter than `company_matches`).
    A board whose postings carry no company name (Ashby) is accepted only for a name built from the whole company name
    or from the company name without place and group words ("Roche Schweiz AG" -> "roche").
    For a shortened guess the place and group words (`PLACE_WORDS`) are dropped from the job's company first, and the board is
    accepted when its name equals the rest or starts with it plus a space ("Roche Schweiz AG" accepts "Roche", "Swiss Re Group"
    accepts "Swiss Re"). Exception: when "group" or "holding" was dropped and one word remains ("Partners Group" -> "partners"),
    that word alone is too loose ("Partners Capital"), so the board must name the whole company, as before."""
    if not company_norm:
        return False
    stripped, group_dropped = strip_place_words(company_norm)
    if not names_company:
        loose = group_dropped and " " not in stripped          # "Partners Group" -> "partners" is too short a name
        return slug in full_slugs(company_norm) or (not loose and slug in full_slugs(stripped))
    if slug in full_slugs(company_norm):
        return company_matches(company_norm, name)
    if not name:
        return False
    other = normalize_company(name)
    if other == company_norm or other.startswith(company_norm + " "):
        return True
    if stripped == company_norm or (group_dropped and " " not in stripped):
        return False
    return other == stripped or other.startswith(stripped + " ")


def _slugs(mod, company: str) -> list[str]:
    return slugs_for(company) if mod.COMPANY_KEY else full_slugs(company)


def _place_agrees(job_places: list[str], city: str) -> bool:
    """One word group contains the other ("Zurich" and "Zurich, Zurich, Switzerland"). A place that is only a country says nothing about the city."""
    c = normalise_place(city)
    if not c or c in COUNTRIES:
        return False
    for p in (normalise_place(p) for p in job_places):
        if p and p not in COUNTRIES and (re.search(rf"\b{re.escape(c)}\b", p) or re.search(rf"\b{re.escape(p)}\b", c)):
            return True
    return False


# --- the run -------------------------------------------------------------------------------------------

@dataclass
class _State:
    conn: object
    dc: dict
    day: str
    stamp: str
    dry_run: bool
    guesses: dict = field(default_factory=dict)           # (platform, slug) -> row as dict
    lists: dict = field(default_factory=dict)             # (platform, slug) -> postings of this run
    details: dict = field(default_factory=dict)
    companies: set = field(default_factory=set)           # companies that got a probe today
    used: dict = field(default_factory=dict)              # platform -> job attempts today
    resolved_here: dict = field(default_factory=dict)
    cap_said: set = field(default_factory=set)
    budget_said: set = field(default_factory=set)
    refused_said: set = field(default_factory=set)         # hosts robots.txt refused (one line each)
    why_empty: dict = field(default_factory=dict)          # (platform, slug) -> why its list is empty in this run (v0.75.1)

    def guess(self, key):
        if key not in self.guesses:
            row = self.conn.execute("SELECT * FROM board_guesses WHERE platform = ? AND slug = ?", key).fetchone()
            self.guesses[key] = dict(row) if row else None
        return self.guesses[key]

    def save(self, key, status: str, company_norm: str, board_company, listed, http_status) -> None:
        old = self.guess(key)
        row = {"platform": key[0], "slug": key[1], "status": status, "board_company": board_company, "jobs_listed": listed,
               "http_status": http_status, "first_checked_at": old["first_checked_at"] if old else self.stamp,
               "checked_at": self.stamp, "day": self.day, "company_norm": old["company_norm"] if old else company_norm,
               "checks": (old["checks"] + 1) if old else 1}
        self.guesses[key] = row
        with self.conn:                                     # also in a dry run (v0.75.0): the company budget of the day is shared
            self.conn.execute(
                "INSERT INTO board_guesses (platform, slug, status, board_company, jobs_listed, http_status, first_checked_at, "
                "checked_at, day, company_norm, checks) VALUES (:platform, :slug, :status, :board_company, :jobs_listed, "
                ":http_status, :first_checked_at, :checked_at, :day, :company_norm, :checks) "
                "ON CONFLICT (platform, slug) DO UPDATE SET status = :status, board_company = :board_company, jobs_listed = :jobs_listed, "
                "http_status = :http_status, checked_at = :checked_at, day = :day, checks = :checks", row)

    def refusal(self, summary, mod, exc, job_id: int) -> None:
        if mod.HOST not in self.refused_said:
            self.refused_said.add(mod.HOST)
            summary.items.append((job_id, f"not asked: {mod.HOST}: {exc}"))
            summary.refusals.append(f"not asked: {mod.HOST}: {exc}")

    def due(self, g: dict, now: datetime) -> bool:
        """A guess that found no board is asked again after miss_retry_days, at most MAX_CHECKS times."""
        age = now - datetime.strptime(g["checked_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return g["checks"] < MAX_CHECKS and age >= timedelta(days=self.dc["miss_retry_days"])


def _asked_again(g: dict, retry_days: int) -> str:
    """What a cached guess that is not due says: what was found, and when it is asked again (v0.75.1)."""
    day = g["checked_at"][:10]
    if g["status"] == "other_company":
        what = f"board of another company ({g['board_company']}) on {day}"
    elif g["status"] == "error":
        what = f"error (HTTP {g['http_status']}) on {day}" if g["http_status"] else f"error on {day}"
    elif g["http_status"] == 200:
        what = f"empty board on {day}"
    else:
        what = f"no board on {day}"
    if g["checks"] >= MAX_CHECKS:
        return f"{what}, given up after {MAX_CHECKS} checks"
    again = (datetime.strptime(g["checked_at"], "%Y-%m-%dT%H:%M:%SZ") + timedelta(days=retry_days)).strftime("%Y-%m-%d")
    return f"{what}, asked again from {again}"


def _selection(conn, dc: dict, now: datetime, only_job: int | None) -> list:
    since = (now - timedelta(days=dc["window_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT j.id, j.title, j.company, j.location_text, j.first_seen_at FROM jobs j
           WHERE j.description_missing = 1 AND j.closed_at IS NULL AND j.first_seen_at >= ?
             AND EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id = j.id AND f.filter = 'seniority')""", (since,)).fetchall()
    rank = {q["id"]: i for i, q in enumerate(queued_jobs(conn))}
    rows = sorted(rows, key=lambda r: (r["first_seen_at"], r["id"]), reverse=True)         # newest first
    rows.sort(key=lambda r: rank.get(r["id"], len(rank)))                                   # queued jobs first, in their queue order
    return [r for r in rows if only_job is None or r["id"] == only_job]


def _why_not(conn, dc: dict, now: datetime, job_id: int) -> str:
    """Why --job ID is not looked up: the first rule of `_selection` the job fails."""
    r = conn.execute(
        "SELECT j.closed_at, j.description_missing, j.first_seen_at, "
        "EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id = j.id AND f.filter = 'seniority') AS filtered "
        "FROM jobs j WHERE j.id = ?", (job_id,)).fetchone()
    if r is None:
        return "no job with this id"
    if r["closed_at"] is not None:
        return "the job is closed"
    if not r["description_missing"]:
        return "the job has a description"
    if not r["filtered"]:
        return "the job has not passed the filters yet"
    return f"first seen {r['first_seen_at'][:10]}, outside the {dc['window_days']}-day window (resolver.discovery.window_days)"


def _twins(conn) -> dict[str, list]:
    out: dict[str, list] = {}
    for r in conn.execute("SELECT id, title, company, location_text, description FROM jobs "
                          "WHERE description_missing = 0 AND closed_at IS NULL AND description IS NOT NULL"):
        out.setdefault(normalize_company(r["company"]), []).append(r)
    return out


def _local_twin(conn, cfg, job, company: str, twins: dict, run_id, dry_run, resolved_here) -> Outcome | None:
    places = _job_places(conn, job)
    cands, by_id = [], {}
    for t in twins.get(company, []):
        tp = _job_places(conn, t)
        if places and tp and not any(_place_agrees(places, p) or _place_agrees(tp, q) for p in tp for q in places):
            continue
        if is_complete(t["description"]):
            cands.append(Candidate(str(t["id"]), t["title"], None, "", t["description"]))
            by_id[str(t["id"])] = t
    match = choose(job["title"], [], cands)
    if match.status != "match":
        return None
    twin = by_id[match.candidate.posting_id]
    out = Outcome(job["id"], "success", "", "discovery:local", "T1", "local", None, match.candidate.posting_id, match.score,
                  twin["id"])
    try:
        if dry_run:
            out.message = f"would copy {len(twin['description'])} characters from job {twin['id']} ({match.score:.2f})"
        else:
            out.message = apply_description(conn, cfg, job["id"], twin["description"], source="discovery:local", run_id=run_id,
                                            check_language=True).message
        out.message += f"; the same posting as job {twin['id']}"
    except DescriptionError as exc:
        out.status, out.error, out.message = "error", str(exc), f"error: {exc}"
    return out


def _board(st: _State, session, mod, pname: str, slug: str, company: str, now: datetime, summary, job_id: int, notes: list):
    """The postings of a guessed board for this company, or None when there is no board of the company or nothing may be asked.
    Every way to no postings adds a short note to `notes` (v0.75.1), so a job that finds nothing can say why."""
    label = f"{pname}:{slug}"
    key = (pname, slug)
    g = st.guess(key)
    revived = bool(g and g["status"] == "other_company" and g["board_company"]
                   and company_ok(company, slug, g["board_company"], mod.COMPANY_KEY))   # the board names this company: not "another" one
    def budget_full() -> bool:
        if company in st.companies or len(st.companies) < st.dc["new_companies_per_day"]:
            return False
        if company not in st.budget_said:
            st.budget_said.add(company)
            summary.items.append((job_id, f"waits for tomorrow: {st.dc['new_companies_per_day']} companies were guessed today ({company} is next)"))
        notes.append(f"{label} waits for the company budget")
        return True

    if g and (g["status"] == "hit" or revived):
        if not company_ok(company, slug, g["board_company"], mod.COMPANY_KEY):             # the stored name decides: no request
            notes.append(f"{label} board of another company")
            return None
        asked_now = key not in st.lists
        if asked_now:
            try:
                found = mod.probe(session.get, slug)
                if found is None:                            # 404: the board is gone, a miss from now on
                    st.save(key, "miss", company, None, None, 404)
                    notes.append(f"{label} no board (404)")
                    st.why_empty[key] = notes[-1]
                elif revived:
                    st.save(key, "hit", company, g["board_company"], len(found), 200)
                st.lists[key] = found or []
            except Refused as exc:
                st.refusal(summary, mod, exc, job_id)
                st.lists[key] = []                           # not asked today; the job is tried again tomorrow
                notes.append(f"{label} not asked (robots.txt)")
                st.why_empty[key] = notes[-1]
            except (FetchError, PlatformError) as exc:
                st.lists[key] = []                           # not readable today; the job is tried again tomorrow
                notes.append(f"{label} error ({getattr(exc, 'http_status', None) and 'HTTP ' + str(exc.http_status) or exc})")
                st.why_empty[key] = notes[-1]
        if not st.lists[key] and not asked_now and key in st.why_empty:
            notes.append(st.why_empty[key])                  # a later job on the same failed board gets the same reason
        return st.lists[key]
    if g and not st.due(g, now):
        notes.append(f"{label} {_asked_again(g, st.dc['miss_retry_days'])}")
        return None
    if budget_full():
        return None
    try:
        postings = mod.probe(session.get, slug)
    except Refused as exc:
        st.refusal(summary, mod, exc, job_id)                # robots.txt disallows the interface: nothing is asked
        notes.append(f"{label} not asked (robots.txt)")
        return None
    except (FetchError, PlatformError) as exc:
        st.companies.add(company)
        st.save(key, "error", company, None, None, getattr(exc, "http_status", None))
        notes.append(f"{label} error ({getattr(exc, 'http_status', None) and 'HTTP ' + str(exc.http_status) or exc})")
        return None
    st.companies.add(company)
    if postings is None:
        st.save(key, "miss", company, None, None, 404)
        notes.append(f"{label} no board (404)")
        return None
    if not mod.COMPANY_KEY:                                  # the postings carry no company name: a non-empty board of the full name is the hit
        if postings:
            st.save(key, "hit", company, None, len(postings), 200)
            st.lists[key] = postings
            return postings
        st.save(key, "miss", company, None, 0, 200)
        notes.append(f"{label} empty board")
        return None
    names = [p.company for p in postings if p.company]
    mine = next((n for n in names if company_ok(company, slug, n)), None)
    if mine:
        st.save(key, "hit", company, mine, len(postings), 200)
        st.lists[key] = postings
        return postings
    st.save(key, "other_company" if names else "miss", company, names[0] if names else None, len(postings), 200)
    notes.append(f"{label} board of another company" if names else f"{label} empty board")
    return None


def _attempt(conn, cfg, st: _State, session, mod, pname: str, slug: str, postings: list, job, company: str, run_id) -> tuple[Outcome, str | None]:
    key = f"{pname}:{slug}"
    out = Outcome(job["id"], "error", "", key, "T1", pname)
    try:
        places = _job_places(conn, job)
        kept = []
        for p in postings:
            if not company_ok(company, slug, p.company, mod.COMPANY_KEY):
                continue
            agree = [c for c in p.cities if _place_agrees(places, c)]
            if agree:                                        # both sides must state a place and agree
                p.city = agree[0]
                kept.append(p)
        match = choose(job["title"], [], kept, accept=ACCEPT)
        out.confidence = match.score or None
        if match.candidate is not None:
            out.url, out.posting_id = match.candidate.url or None, match.candidate.posting_id
        if match.status != "match":
            out.status = "no_match" if match.status == "none" else "ambiguous"
            note = match.note if kept else "no posting of this company in the same place"
            out.message = f"{out.status.replace('_', ' ')}: {note}"
            return out, None
        cand = match.candidate
        dk = (key, cand.posting_id)
        if dk not in st.details:
            st.details[dk] = mod.detail_text(session.get, slug, cand)
        text = st.details[dk] or ""
        if not is_complete(text):
            out.status, out.message = "partial", f"partial: posting {cand.posting_id} has {len(text)} characters or no tasks and requirements part"
            out.text = text if 0 < len(text) <= MAX_CHARS else None
            return out, None
        earlier = conn.execute(
            "SELECT job_id FROM description_resolutions WHERE source_key = ? AND posting_id = ? AND status = 'success' "
            "AND job_id != ? ORDER BY id LIMIT 1", (key, cand.posting_id, job["id"])).fetchone()
        out.duplicate_of_job_id = earlier["job_id"] if earlier else st.resolved_here.get(dk)
        if st.dry_run:
            out.message = f"would store {len(text)} characters from posting {cand.posting_id} ({match.score:.2f})"
            st.resolved_here.setdefault(dk, job["id"])
        else:
            out.message = apply_description(conn, cfg, job["id"], text, source=f"discovery:{key}", run_id=run_id,
                                            check_language=True).message
        out.status = "success"
        if out.duplicate_of_job_id:
            out.message += f"; the same posting as job {out.duplicate_of_job_id}"
    except StopRun as exc:
        out.status = "refused" if exc.http_status is not None else "error"
        out.error = out.message = str(exc)
        return out, f"stopped after job {job['id']}: {exc}; nothing more is requested in this run"
    except Refused as exc:
        out.status, out.error = "refused", str(exc)
        out.message = f"not asked: {exc}"
    except (FetchError, PlatformError, DescriptionError) as exc:
        out.status, out.error = "error", str(exc)
        out.message = f"error: {exc}"
    return out, None


def discover_descriptions(conn, cfg: dict, fetch, sleep=time.sleep, now: datetime | None = None, run_id: str | None = None,
                          only_job: int | None = None, dry_run: bool = False, limit: int | None = None) -> ResolveSummary:
    now = now or datetime.now(timezone.utc)
    day, stamp = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rc = cfg["resolver"]
    dc = rc["discovery"]
    summary = ResolveSummary()
    names = [p for p in dc["platforms"] if p in PLATFORMS]
    session = _Session(cfg, fetch, sleep, {}, extra_hosts=tuple(PLATFORMS[p].HOST for p in names))
    st = _State(conn, dc, day, stamp, dry_run)
    st.companies = {r[0] for r in conn.execute("SELECT DISTINCT company_norm FROM board_guesses WHERE day = ?", (day,))}
    marks = ", ".join("?" for _ in DISCOVERY_PLATFORMS)
    done_today = {r[0] for r in conn.execute(
        f"SELECT DISTINCT job_id FROM description_resolutions WHERE day = ? AND platform IN ({marks})", (day, *DISCOVERY_PLATFORMS))}
    twins = _twins(conn) if dc["local_twins"] else {}

    def finish(out: Outcome) -> None:
        summary.resolved += out.status == "success"
        summary.items.append((out.job_id, out.message))
        summary.outcomes.append(out)
        if not dry_run:
            _record(conn, out, stamp, day)

    def capped(pname: str, mod, job_id: int) -> bool:
        st.used.setdefault(pname, conn.execute(
            "SELECT COUNT(*) FROM description_resolutions WHERE day = ? AND platform = ?", (day, pname)).fetchone()[0])
        if st.used[pname] < rc["daily_cap_per_host"]:
            return False
        if pname not in st.cap_said:
            st.cap_said.add(pname)
            summary.items.append((job_id, f"waits for tomorrow: the daily cap of {rc['daily_cap_per_host']} jobs for {mod.HOST} is reached"))
        return True

    selected = _selection(conn, dc, now, only_job)
    if only_job is not None and not selected:
        summary.items.append((only_job, _why_not(conn, dc, now, only_job)))
    for job in selected:
        if job["id"] in done_today and only_job is None:
            continue                                            # tried today already; the next try is tomorrow
        if limit and summary.attempted >= limit:
            break
        company = normalize_company(job["company"])
        if not company:
            if only_job is not None:
                summary.items.append((job["id"], "no company name"))
            continue
        summary.looked_up += 1
        first_item, notes = len(summary.items), []
        if twins:
            out = _local_twin(conn, cfg, job, company, twins, run_id, dry_run, st.resolved_here)
            if out is not None:
                summary.attempted += 1
                finish(out)
                continue
        counted, success, stop, cap_hit = False, False, None, False
        try:
            for pname in names:
                mod = PLATFORMS[pname]
                for slug in _slugs(mod, company):
                    if capped(pname, mod, job["id"]):        # a reached cap asks no board at all, not even a known hit
                        cap_hit = True
                        break
                    before = len(notes)
                    postings = _board(st, session, mod, pname, slug, company, now, summary, job["id"], notes)
                    if not postings:
                        if postings is not None and len(notes) == before:
                            notes.append(f"{pname}:{slug} empty board")
                        continue
                    if not counted:
                        summary.attempted += 1
                        counted = True
                    st.used[pname] += 1
                    out, stop = _attempt(conn, cfg, st, session, mod, pname, slug, postings, job, company, run_id)
                    finish(out)
                    if out.status == "success" or stop:
                        success = True
                        break
                if success:
                    break
        except StopRun as exc:                                  # a probe was refused: nothing more is asked in this run
            stop = f"stopped at job {job['id']}: {exc}; nothing more is requested in this run"
        if stop:
            summary.stopped = stop
            break
        if len(summary.items) == first_item:                    # this job added no line of its own: say why nothing was found (v0.75.1)
            if not names:
                why = "no job board found: no discovery platform is enabled"
            elif cap_hit and not notes:
                why = "not looked up: daily cap reached"
            elif not notes:
                why = f"no job board found: no board name can be built from '{job['company']}'"
            else:
                why = "no job board found: " + "; ".join(notes + (["daily cap reached"] if cap_hit else []))
            summary.items.append((job["id"], why))
    return summary
