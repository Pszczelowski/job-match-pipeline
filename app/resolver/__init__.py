"""The description resolver (goal.md 5.1a, decided 3 Nov 2026): find the complete description of a job that came without one.

A LinkedIn alert carries no description, so the job gets no CV. For every open job without a description whose company is
a configured source (`resolver.sources`, matched by alias) the resolver asks that employer's own career site: it searches
or lists the postings, matches the job's title and city to one posting (`match.py`), reads the text and stores it with
`descriptions.apply_description` only if it is complete (`complete.py`).

Tiers: a source with a `permission_note` is T1 (permitted); a source without one is T2 (the site's terms may forbid
automated access) and is used only while `resolver.accept_t2_risk` is true. Rules for both: only the hosts of the
configured sources; robots.txt is read first and respected (including its crawl-delay); an honest User-Agent; a delay
between requests to a host and a daily cap of job attempts per host; the whole run stops at the first HTTP 403, 429 or 5xx
or a network error; no login; the fetcher keeps the cookies a site sets in memory for the host that set them and sends them back to it
within the run (`PageFetcher.cookies` in `descriptions_fetch.py`, one fetcher per run), never stored; nothing done to avoid bot protection. Every attempt is one row in
`description_resolutions`; a job is given up on after `max_attempt_days` days with an attempt.

Order and limits (v0.86.0): when the daily cap of a host cannot serve every job, the jobs go in this order: a job with the
posting its jobs.ch alert mail links (path B) first, then the job with the fewest days of attempts, then the newest job; a job
that got no attempt because of a cap is listed in `ResolveSummary.deferred`, a job given up on in `given_up`, and
`run_warning` turns both into the warning of the run. A job located only abroad (Poland or another country, never Switzerland
or Liechtenstein) is not looked up on a job board (`skipped_abroad`); the named employer sources and the alert link still are.
A no match, an ambiguous match and a partial posting store their reason in `description_resolutions.error` (a closed posting has the status `expired`,
since v0.87.1; a partial posting also keeps its text in `description_resolutions.text` for the paste page, never as the description); the board's name for a
company counts if both normalised names (legal form, trailing "Group", "Switzerland" and similar dropped) are equal or one starts
with the other as a whole word.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .. import db
from ..dedup import normalize_company, normalize_company_key
from ..descriptions import MAX_CHARS, DescriptionError, apply_description
from ..descriptions_fetch import STOP_STATUSES, FetchError
from ..filters.poland import fold, is_polish_location
from . import jibe, jobcloud, liebherr, refline
from .common import DISCOVERY_PLATFORMS, Expired, PlatformError, Refused, StopRun
from .complete import is_complete
from .match import ACCEPT, Candidate, MatchResult, choose, city_agrees, title_score
from .robots import Robots

PLATFORMS = {"jibe": jibe, "refline": refline, "liebherr": liebherr, "jobcloud": jobcloud}


@dataclass
class Outcome:
    job_id: int
    status: str                        # success | no_match | ambiguous | partial | refused | error | expired
    message: str
    source_key: str
    tier: str
    platform: str
    url: str | None = None
    posting_id: str | None = None
    confidence: float | None = None
    duplicate_of_job_id: int | None = None
    error: str | None = None
    text: str | None = None            # a partial posting's text (kept for the paste page; never stored as the description)


@dataclass
class ResolveSummary:
    resolved: int = 0
    attempted: int = 0
    looked_up: int = 0                                                  # discovery: jobs that reached the lookup (`attempted`: those with a board to try)
    stopped: str | None = None
    items: list[tuple[int, str]] = field(default_factory=list)          # (job id, message)
    outcomes: list[Outcome] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)                   # hosts robots.txt refused (discovery): kept for the run record
    deferred: list[int] = field(default_factory=list)                   # jobs that got no attempt because a daily cap was reached
    capped_hosts: list[str] = field(default_factory=list)               # the hosts whose daily cap was reached
    given_up: list[int] = field(default_factory=list)                   # jobs given up on after `max_attempt_days` days
    skipped_abroad: int = 0                                             # jobs located only abroad that no job board was asked about


def run_warning(summary: ResolveSummary, max_days: int) -> str | None:
    """The warning of the run for jobs the resolver could not serve: waiting for a daily cap, or given up on; None if neither."""
    parts = []
    if summary.deferred:
        parts.append(f"{len(summary.deferred)} job(s) wait for the daily cap of {', '.join(summary.capped_hosts)}")
    if summary.given_up:
        parts.append(f"{len(summary.given_up)} given up after {max_days} days")
    return "; ".join(parts) or None


def user_agent(cfg: dict) -> str:
    return cfg["resolver"]["user_agent"].strip() or cfg["descriptions"]["user_agent"]


def sources_for(srcs: dict[str, dict], company: str) -> list[dict]:
    """The sources to try for a company: those whose alias matches first, then the `any_company` sources (job boards)."""
    named = [s for s in srcs.values() if company in s["aliases_norm"]]
    return named + [s for s in srcs.values() if s.get("any_company") and s not in named]


def sources(cfg: dict) -> dict[str, dict]:
    """The configured sources with their tier, host and normalised aliases."""
    out = {}
    for key, s in cfg["resolver"]["sources"].items():
        out[key] = {**s, "key": key, "host": urllib.parse.urlsplit(s["base_url"]).netloc,
                    "tier": "T1" if s.get("permission_note", "").strip() else "T2",
                    "aliases_norm": {normalize_company(a) for a in s["aliases"]}}
    return out


def _job_places(conn, job) -> list[str]:
    places = [job["location_text"] or ""]
    for r in conn.execute("SELECT location_text, city FROM job_locations WHERE job_id = ?", (job["id"],)):
        places += [r["location_text"] or "", r["city"] or ""]
    return [p for p in places if p.strip()]


_SWISS_NAMES = {"switzerland", "schweiz", "suisse", "svizzera", "liechtenstein"}


def _outside_switzerland(places: list[str], cfg: dict) -> bool:
    """True when the job names places and every one is Polish or names a country other than Switzerland and Liechtenstein.
    A place that names no country (and no Polish city) is unclear: the job is then not abroad."""
    if not places:
        return False

    def named(place: str, names) -> bool:
        return any(re.search(r"(?<!\w)" + re.escape(fold(n).strip()) + r"(?!\w)", fold(place)) for n in names if n.strip())

    others = [c for c in cfg["location"]["countries"] if fold(c).strip() not in _SWISS_NAMES]
    return all(is_polish_location(p, cfg) or (named(p, others) and not named(p, _SWISS_NAMES)) for p in places)


def _own_site_jobs(conn, cfg: dict) -> set[int]:
    """Jobs whose own career site is read by an enabled adapter (a sighting from its page sources) and whose page was not
    fetched with an error: `fetch-descriptions` serves them after the filters, so the resolver leaves them alone (v0.90.0)."""
    from .. import adapters                                             # adapters import the resolver's robots module: imported late
    sources = sorted({s for spec in adapters.enabled_adapters(cfg) for s in spec.page_sources()})
    if not sources:
        return set()
    return {r[0] for r in conn.execute(
        f"""SELECT DISTINCT s.job_id FROM job_sources s
            WHERE s.source IN ({', '.join('?' * len(sources))})
              AND NOT EXISTS (SELECT 1 FROM description_fetches d WHERE d.job_id = s.job_id AND d.status = 'error')""", sources)}


def _candidate_jobs(conn, only_job: int | None) -> list:
    rows = conn.execute(
        """SELECT j.id, j.title, j.company, j.location_text,
                  EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id = j.id AND f.filter = 'seniority') AS filtered
           FROM jobs j WHERE j.description_missing = 1 AND j.closed_at IS NULL ORDER BY j.id""").fetchall()
    return [r for r in rows if only_job is None or r["id"] == only_job]


def _ordered(conn, jobs: list, srcs: dict[str, dict]) -> list:
    """The order in which the daily cap is spent: path B jobs, then the fewest days of attempts, then the newest job."""
    boards = [s for s in srcs.values() if s["platform"] == "jobcloud"]
    return sorted(jobs, key=lambda j: (not any(_alert_candidate(conn, j, s) is not None for s in boards),
                                       len(_attempt_days(conn, j["id"])), -j["id"]))


class _Session:
    """The requests of one run: allowlist, robots.txt, delay per host, stop on refusal."""

    def __init__(self, cfg: dict, fetch, sleep, srcs: dict[str, dict], extra_hosts: tuple[str, ...] = ()):
        self.cfg, self.fetch, self.sleep = cfg, fetch, sleep
        self.hosts = {s["host"] for s in srcs.values()} | set(extra_hosts)
        self.requests: dict[str, int] = {}
        self.robots = Robots(self._raw_get, user_agent(cfg))

    def _wait(self, host: str) -> None:
        if self.requests.get(host):
            self.sleep(max(self.cfg["resolver"]["delay_seconds"], self.robots.crawl_delay(host)))
        self.requests[host] = self.requests.get(host, 0) + 1

    def _raw_get(self, url: str) -> str:          # used for robots.txt: a host on the allowlist, spaced like the pages
        host = urllib.parse.urlsplit(url).netloc
        if host not in self.hosts:
            raise Refused(f"{host} is not a configured source")
        self._wait(host)
        return self.fetch(url)

    def get(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.netloc not in self.hosts:
            raise Refused(f"{url!r} is not a page of a configured source")
        if not self.robots.allowed(url):
            why = self.robots.refusal(parts.netloc)
            if why and (why[0] is None or why[0] in STOP_STATUSES):
                raise StopRun(why[1], why[0])
            raise Refused(why[1] if why else f"robots.txt of {parts.netloc} disallows {parts.path or '/'}")
        self._wait(parts.netloc)
        if hasattr(self.fetch, "path_allowed"):         # a redirect to a path robots.txt disallows is refused by the fetcher
            self.fetch.path_allowed = self.robots.allowed
        try:
            return self.fetch(url)
        except FetchError as exc:
            if exc.http_status in STOP_STATUSES or (exc.http_status is None and str(exc).startswith("network error")):
                raise StopRun(f"{parts.netloc}: {exc}", exc.http_status) from None
            raise
        finally:
            if hasattr(self.fetch, "path_allowed"):
                self.fetch.path_allowed = None


def _used_today(conn, day: str, key: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM description_resolutions WHERE day = ? AND source_key = ?", (day, key)).fetchone()[0]


def _attempt_days(conn, job_id: int) -> set[str]:
    """The days with an attempt on a named source or job board; the discovery step (v0.73.0) has its own days."""
    marks = ", ".join("?" for _ in DISCOVERY_PLATFORMS)
    return {r[0] for r in conn.execute(
        f"SELECT DISTINCT day FROM description_resolutions WHERE job_id = ? AND platform NOT IN ({marks})", (job_id, *DISCOVERY_PLATFORMS))}


def _record(conn, out: Outcome, stamp: str, day: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, url, posting_id, "
            "confidence, status, duplicate_of_job_id, error, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (out.job_id, stamp, day, out.tier, out.source_key, out.platform, out.url, out.posting_id, out.confidence,
             out.status, out.duplicate_of_job_id, out.error, out.text))


class _Caches:
    """What one run remembers: list answers, detail texts, and (dry run) which job got a posting."""

    def __init__(self):
        self.lists: dict[tuple, list] = {}
        self.details: dict[tuple, str | None] = {}
        self.resolved_here: dict[tuple, int] = {}                       # (source, posting id) -> job that got it in a dry run


def _same_company(company: str, cand) -> bool:
    """The board's company is the job's company, or one starts with the other as a whole word ("AXA Versicherungen AG" for "AXA",
    "Merck" for "Merck Group"); both names are compared without legal form, trailing "Group" and country names."""
    if not cand.company or not company:
        return False
    name, own = normalize_company_key(cand.company), normalize_company_key(company)
    return bool(name and own) and (name == own or name.startswith(own + " ") or own.startswith(name + " "))


_ALERT_DETAIL = re.compile(r"^/(?:[a-z]{2}/)?vacancies/detail/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$", re.I)


def _alert_candidate(conn, job, src: dict) -> Candidate | None:
    """Path B (v0.84.0): a job that came from a jobs.ch alert mail already has the short detail link of its posting on this
    job board's host; the posting is read directly, without a search. None when the job has no such link."""
    for row in conn.execute("SELECT url FROM job_sources WHERE job_id = ? AND url IS NOT NULL ORDER BY id", (job["id"],)):
        parts = urllib.parse.urlsplit(row["url"])
        m = _ALERT_DETAIL.match(parts.path)
        if parts.scheme == "https" and parts.netloc == src["host"] and m and not parts.query:
            uid = m.group(1).lower()
            return Candidate(uid, job["title"], None, f"https://{parts.netloc}/en/vacancies/detail/{uid}/")
    return None


def _attempt(conn, cfg: dict, job, src: dict, session: _Session, caches: _Caches, company: str, run_id: str | None,
             dry_run: bool, direct: Candidate | None = None) -> tuple[Outcome, str | None]:
    """One job against one source: the outcome and, when the run must stop, the reason. `direct` is the posting the job
    already names (path B): it is read without a search and counts as matched."""
    key = src["key"]
    platform = PLATFORMS[src["platform"]]
    out = Outcome(job["id"], "error", "", key, src["tier"], src["platform"])
    try:
        if direct is not None:
            match = MatchResult("match", direct, 1.0)
        else:
            ask = {}
            places = _job_places(conn, job)
            if src["platform"] == "jobcloud":                           # the search names the company; a row of the company with a close title in the job's city is "good"
                ask = {"company": job["company"], "good": lambda c: (_same_company(company, c) and title_score(job["title"], c.title) >= ACCEPT
                                                                     and city_agrees(places, c.city))}
            ck = (key, platform.cache_key(job["title"]), (normalize_company_key(job["company"]), tuple(sorted(places))) if ask else None)
            if ck not in caches.lists:
                caches.lists[ck] = platform.candidates(session.get, src, job["title"], **ask)
            listed = caches.lists[ck]
            read = listed
            if src.get("any_company"):                                  # a job board lists every employer: only this job's company counts
                listed = [c for c in listed if _same_company(company, c)]
            match = choose(job["title"], places, listed)
            if src.get("any_company") and not listed:
                seen = list(dict.fromkeys(c.company for c in read if c.company))[:3]
                match.note = (f"{src['host']} lists no posting of {job['company']} for these words "
                              f"({len({c.posting_id for c in read})} rows read" + (f"; companies seen: {', '.join(seen)}" if seen else "") + ")")
        out.confidence = match.score or None
        if match.candidate is not None:
            out.url, out.posting_id = match.candidate.url, match.candidate.posting_id
        if match.status != "match":
            out.status = "no_match" if match.status == "none" else "ambiguous"
            out.message = f"{out.status.replace('_', ' ')}: {match.note}"
            out.error = out.message
        else:
            cand = match.candidate
            text = cand.text or ""
            if not is_complete(text):
                dk = (key, cand.posting_id)
                if dk not in caches.details:
                    caches.details[dk] = platform.detail_text(session.get, cand)
                if caches.details[dk] and (is_complete(caches.details[dk]) or len(caches.details[dk]) > len(text)):
                    text = caches.details[dk]
            if not is_complete(text):
                out.status, out.message = "partial", f"partial: posting {cand.posting_id} has {len(text)} characters or no tasks and requirements part"
                out.error = out.message
                out.text = text if 0 < len(text) <= MAX_CHARS else None
            else:
                earlier = conn.execute(
                    "SELECT job_id FROM description_resolutions WHERE source_key = ? AND posting_id = ? AND status = 'success' "
                    "AND job_id != ? ORDER BY id LIMIT 1", (key, cand.posting_id, job["id"])).fetchone()
                out.duplicate_of_job_id = earlier["job_id"] if earlier else caches.resolved_here.get((key, cand.posting_id))
                if dry_run:
                    out.message = f"would store {len(text)} characters from posting {cand.posting_id} ({match.score:.2f})"
                    caches.resolved_here.setdefault((key, cand.posting_id), job["id"])
                else:
                    result = apply_description(conn, cfg, job["id"], text, source=f"resolver:{key}", run_id=run_id,
                                               check_language=bool(job["filtered"]))
                    out.message = result.message
                out.status = "success"
                if out.duplicate_of_job_id:
                    out.message += f"; the same posting as job {out.duplicate_of_job_id}"
    except StopRun as exc:
        out.status = "refused" if exc.http_status is not None else "error"
        out.error = out.message = str(exc)
        return out, f"stopped after job {job['id']}: {exc}; nothing more is requested in this run"
    except Expired as exc:                                              # a closed posting: the reason is stored, not a partial text
        out.status, out.error = "expired", f"expired: {exc}"
        out.message = out.error
    except Refused as exc:
        out.status, out.error = "refused", str(exc)
        out.message = f"not asked: {exc}"
    except (FetchError, PlatformError, DescriptionError) as exc:
        out.status, out.error = "error", str(exc)
        out.message = f"error: {exc}"
    return out, None


def resolve_descriptions(conn, cfg: dict, fetch, sleep=time.sleep, now: datetime | None = None, run_id: str | None = None,
                         only_job: int | None = None, dry_run: bool = False, limit: int | None = None) -> ResolveSummary:
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    rc = cfg["resolver"]
    srcs = sources(cfg)
    summary = ResolveSummary()
    session = _Session(cfg, fetch, sleep, srcs)
    used: dict[str, int] = {}                                           # attempts per source today (this run included)
    cap_said: set[str] = set()                                          # job boards whose daily cap was reported in this run
    caches = _Caches()

    own_site = _own_site_jobs(conn, cfg)
    last_day = conn.execute("SELECT MAX(day) FROM description_resolutions WHERE day < ?", (day,)).fetchone()[0] or ""
    for job in _ordered(conn, _candidate_jobs(conn, only_job), srcs):
        if job["id"] in own_site:
            summary.items.append((job["id"], "skipped: its own career site is read after the filters"))
            continue
        company = normalize_company(job["company"])
        abroad = _outside_switzerland(_job_places(conn, job), cfg)       # a job board knows no posting of a job located only abroad
        chain = []
        dropped_abroad = False
        for src in sources_for(srcs, company):
            if src["tier"] == "T2" and not rc["accept_t2_risk"]:
                if not src.get("any_company"):                          # a job board is skipped silently, an employer's site is reported
                    summary.items.append((job["id"], f"skipped: {src['key']} has no permission_note (tier T2) and resolver.accept_t2_risk is false"))
                continue
            if abroad and src.get("any_company") and not (src["platform"] == "jobcloud" and _alert_candidate(conn, job, src)):
                dropped_abroad = True                                   # path B (the alert link) stays; the search does not
                continue
            chain.append(src)
        if not chain:
            if dropped_abroad:
                summary.skipped_abroad += 1
                summary.items.append((job["id"], "skipped: the job is located outside Switzerland; no job board is asked"))
            continue
        days = _attempt_days(conn, job["id"])
        if len(days) >= rc["max_attempt_days"]:
            summary.items.append((job["id"], f"given up: no description found in {len(days)} days of trying"))
            if max(days) >= last_day:                                   # counted once, in the run after its last attempt day; not again on later days
                summary.given_up.append(job["id"])
            continue
        if day in days and only_job is None:
            continue                                                    # tried today already; the next try is tomorrow
        if limit and summary.attempted >= limit:
            break
        stop = None
        counted = False
        capped = False
        named_total = sum(1 for s in sources_for(srcs, company) if not s.get("any_company"))
        named_no_match = sum(1 for s in sources_for(srcs, company) if not s.get("any_company") and s not in chain)
        named_error = 0                                                 # named sources that ended in an error (v0.85.6: path B still runs)
        # job boards are tried only when no named source exists or each one found no match or was skipped (tier T2, daily cap); decided 3 Nov 2026
        for src in chain:
            key = src["key"]
            direct = _alert_candidate(conn, job, src) if src["platform"] == "jobcloud" else None
            only_direct = abroad and direct is not None                 # a job abroad: the posting the mail links is read, no search
            if src.get("any_company") and named_total and named_no_match < named_total:
                # path B gate (decided by the user on 13 Nov 2026, option B): when the named sources found no match or
                # ended in an error, the posting the alert mail links is still read; the job-board search is not run
                if not (direct and named_no_match + named_error == named_total):
                    continue
                only_direct = True
            used.setdefault(key, _used_today(conn, day, key))
            if used[key] >= rc["daily_cap_per_host"]:
                if not src.get("any_company") or key not in cap_said:   # a job board's cap is reported once, not for every job
                    summary.items.append((job["id"], f"waits for tomorrow: the daily cap of {rc['daily_cap_per_host']} jobs for {src['host']} is reached"))
                cap_said.add(key)
                capped = True
                if src["host"] not in summary.capped_hosts:
                    summary.capped_hosts.append(src["host"])
                if not src.get("any_company"):
                    named_no_match += 1                                 # a capped named source counts as no match
                continue
            if not counted:
                summary.attempted += 1
                counted = True
            used[key] += 1
            for candidate in ([direct] if only_direct else [direct, None] if direct else [None]):     # path B first (the posting the alert mail links), then the search
                out, stop = _attempt(conn, cfg, job, src, session, caches, company, run_id, dry_run, candidate)
                summary.resolved += out.status == "success"
                if not src.get("any_company") and out.status in ("no_match", "expired"):
                    named_no_match += 1
                if not src.get("any_company") and out.status == "error":
                    named_error += 1
                summary.items.append((job["id"], out.message))
                summary.outcomes.append(out)
                if not dry_run:
                    _record(conn, out, db.utcnow(), day)         # the time of the attempt itself, not of the run's start
                if out.status == "success" or stop:
                    break
            if out.status == "success" or stop:                          # otherwise the next source (a job board) gets its turn
                break
        if capped and not counted:
            summary.deferred.append(job["id"])
        if stop:
            summary.stopped = stop
            break
    return summary
