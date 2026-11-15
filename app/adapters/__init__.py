"""Employer career-site adapters (goal.md 5.1, Phase 2).

An adapter reads the public job list of one employer, stores each listed job through the same dedup as the alert mails,
and later fetches the text of the jobs that survived the filters. Per employer there is a `spec` (host, parsers, switch).

Rules kept by the code for every adapter, whatever a site's terms say: it is OFF until the user switches it on in
`config.yaml` (`adapters.<name>.enabled`); only https pages on the employer's own host are requested; an honest
User-Agent; a pause between requests; a daily cap on job pages; each job page at most once; no retries; a stop at the
first refusal (HTTP 403, 429, 5xx) or network error. Nothing is done to avoid bot protection or a login wall.

Closed jobs: a complete read of a list (every page read, no error, not cut short by `max_list_pages`) tells which stored jobs
of the site are no longer listed. A job missing in `adapters.closed_after_missing` complete reads in a row gets
`jobs.closed_at`; it is neither deleted nor archived. A job listed again is reopened. See `track_listing`.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .. import db, postpone
from ..dedup import ingest_job
from ..descriptions import DescriptionError, apply_description
from ..descriptions_fetch import STOP_STATUSES, FetchError, FetchSummary
from ..models import NormalizedJob
from ..parsers.linkedin import SOURCE as LINKEDIN_SOURCE
from ..resolver.robots import Robots
from ..ubs_page import html_to_text            # noqa: F401  (shared by the adapters)

log = logging.getLogger("recruitment")


class AdapterDisabled(Exception):
    """The adapter is switched off or its basis is not recorded."""


class PageError(ValueError):
    """A page does not have the expected layout (the site probably changed)."""


@dataclass
class ListedJob:
    external_id: str
    url: str
    title: str
    locations: list[str] = field(default_factory=list)
    source_date: str = ""
    reference: str = ""         # the employer's requisition number, if the list states one
    more_places: bool = False   # the list row says "+N more": other places are hidden and only the job page names them


@dataclass
class JobDetail:
    text: str
    posted: str = ""            # ISO date YYYY-MM-DD, if the page states one and it can be read
    locations: list[str] = field(default_factory=list)      # every place the page names (SIX: a list row "+N more" hides some)


def _no_more_pages(url: str, html: str) -> list[str]:
    return []


@dataclass
class AdapterSpec:
    key: str                    # config key under `adapters`
    label: str                  # name in the report
    source: str                 # `job_sources.source`
    company: str
    host: str                   # only this host is requested
    parse_list: Callable[[str, dict], list[ListedJob]]       # (html, adapter config) -> jobs
    parse_detail: Callable[[str], JobDetail]
    more_pages: Callable[[str, str], list[str]] = _no_more_pages    # extra list pages, from the first one
    next_page: Callable[[str, str], tuple[str, dict] | None] | None = None    # (url, html) -> (url, form data) of the next page
    note_required: bool = False                                      # the basis must be written down
    list_filters: tuple[str, ...] = ()      # config keys of the site's list filter (country, places): emptied to see every listed job
    list_requests: Callable[[str, dict, list[str]], tuple[str, dict | None] | None] | None = None
    # (list url, adapter config, pages read so far) -> (url, body) of the next list request, None when the list is read;
    # for a list that a JSON interface pages by offset: it replaces `more_pages` and `next_page`
    detail_url: Callable[[str], str] | None = None          # the address that is read for a job page, if not the job's own address
    detail_sources: tuple[str, ...] = ()    # sources whose jobs this adapter reads job pages for (default: its own source only)
    tier: str = ""              # "T1" (a public API), "T2" (an HTML site whose terms may forbid automated reading), "" (older adapters)
    check_robots: bool = False  # read robots.txt of the host first, respect it and its crawl-delay (goal.md 5.1, v0.70.0)
    job_hosts: tuple[str, ...] = ()     # further hosts a stored job URL may have; they are never requested (needs `detail_url`)
    empty_is_normal: bool = False       # an empty list is a valid answer (a small employer may have no vacancy): no "zero jobs" alert

    def page_sources(self) -> tuple[str, ...]:
        return self.detail_sources or (self.source,)


@dataclass
class ListSummary:
    found: int = 0
    new: int = 0
    error: str | None = None
    outcomes: dict[str, int] = field(default_factory=dict)
    complete: bool = False      # the whole list was read: only then a missing job counts
    closed: int = 0             # jobs marked closed by this read
    reopened: int = 0           # closed jobs that were listed again


def settings(cfg: dict, spec: AdapterSpec) -> dict:
    return cfg["adapters"][spec.key]


def require_enabled(cfg: dict, spec: AdapterSpec) -> dict:
    s = settings(cfg, spec)
    if not s["enabled"]:
        raise AdapterDisabled(f"{spec.label} is switched off (adapters.{spec.key}.enabled is false)")
    if spec.note_required and not s["permission_note"].strip():
        raise AdapterDisabled(f"adapters.{spec.key}.permission_note is empty: write down the basis (permission, date, where it is kept)")
    if spec.tier == "T2" and not s["permission_note"].strip() and not str(s.get("decision_note", "")).strip():
        raise AdapterDisabled(f"adapters.{spec.key} is tier T2: write the accepted risk in adapters.{spec.key}.decision_note")
    return s


def enabled_adapters(cfg: dict) -> list[AdapterSpec]:
    out = []
    for spec in REGISTRY:
        try:
            require_enabled(cfg, spec)
            out.append(spec)
        except AdapterDisabled:
            continue
    return out


def _check_url(spec: AdapterSpec, url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.netloc != spec.host:
        raise FetchError(f"{url!r} is not a page on {spec.host}")


def _check_job_url(spec: AdapterSpec, url: str) -> None:
    """A stored job URL: https on the adapter's host or on one of its `job_hosts` (those are never requested)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in (spec.host, *spec.job_hosts):
        raise FetchError(f"{url!r} is not a page on {spec.host}")


class _Paced:
    """Requests of an adapter with `check_robots`: robots.txt of the host first, every request after a pause of
    max(adapters.delay_seconds, the crawl-delay of robots.txt), and nothing that robots.txt disallows."""

    def __init__(self, cfg: dict, spec: AdapterSpec, fetch, sleep):
        self.spec, self.fetch, self.sleep = spec, fetch, sleep
        self.delay = cfg["adapters"]["delay_seconds"]
        self.robots = Robots(self._raw, cfg["adapters"]["user_agent"])

    def _raw(self, url: str) -> str:                    # robots.txt itself is paced like a page
        self.sleep(self.delay)
        return self.fetch(url)

    def check(self, url: str) -> None:
        refusal = self.robots.refusal(self.spec.host)
        if refusal:
            raise FetchError(refusal[1], refusal[0])
        if not self.robots.allowed(url):
            raise FetchError(f"robots.txt of {self.spec.host} disallows {url}")

    def __call__(self, url: str, *data):
        self.check(url)
        self.sleep(max(self.delay, self.robots.crawl_delay(self.spec.host)))
        if hasattr(self.fetch, "path_allowed"):         # a redirect to a path robots.txt disallows is refused by the fetcher
            self.fetch.path_allowed = self.robots.allowed
        try:
            return self.fetch(url, *data)
        finally:
            if hasattr(self.fetch, "path_allowed"):
                self.fetch.path_allowed = None


def fetch_list(conn, cfg: dict, spec: AdapterSpec, fetch, sleep=time.sleep, run_id: str | None = None,
               store: bool = True) -> tuple[ListSummary, list[ListedJob]]:
    """Read the list page(s) and store the jobs. A site problem is returned in `summary.error`; it does not raise."""
    s = require_enabled(cfg, spec)
    a = cfg["adapters"]
    summary, jobs, listed = ListSummary(), [], set()
    if spec.check_robots:                       # the pacing moves into the wrapper: before every request, robots.txt included
        fetch, sleep = _Paced(cfg, spec, fetch, sleep), (lambda seconds: None)
    try:
        url = s["list_url"]
        _check_url(spec, url)
        if spec.list_requests:                                              # a JSON list paged by offset
            pages, truncated = [], False
            while True:
                step = spec.list_requests(url, s, pages)
                if step is None:
                    break
                if len(pages) >= a["max_list_pages"]:
                    truncated = True                                        # the cap stopped the paging: more pages exist
                    break
                _check_url(spec, step[0])
                if pages:
                    sleep(a["delay_seconds"])
                pages.append(fetch(*step))
        else:
            first = fetch(url)
            pages = [first]
            extras = spec.more_pages(url, first)
            truncated = len(extras) > a["max_list_pages"] - 1
            for extra in extras[: a["max_list_pages"] - 1]:
                _check_url(spec, extra)
                sleep(a["delay_seconds"])
                pages.append(fetch(extra))
            while spec.next_page and len(pages) < a["max_list_pages"]:         # a list paged by form posts, like a browser's "Next"
                step = spec.next_page(url, pages[-1])
                if step is None:
                    break
                post_url, form = step
                _check_url(spec, post_url)
                sleep(a["delay_seconds"])
                pages.append(fetch(post_url, form))
            if spec.next_page and len(pages) >= a["max_list_pages"] and spec.next_page(url, pages[-1]) is not None:
                truncated = True                                                # the cap stopped the paging: more pages exist
        seen: set[str] = set()
        everything = {**s, **{key: [] for key in spec.list_filters}}      # the list filter is for storing, not for "still listed"
        for page in pages:
            listed.update(job.external_id for job in spec.parse_list(page, everything))
            for job in spec.parse_list(page, s):
                _check_job_url(spec, job.url)
                if job.external_id not in seen:
                    seen.add(job.external_id)
                    jobs.append(job)
    except (FetchError, PageError) as exc:
        summary.error = f"{spec.label}: {exc}"
        log.error("adapter %s: %s", spec.key, exc)
        return summary, []
    summary.found = len(jobs)
    # An empty list is not trusted as "the employer closed everything": it is more likely a page problem.
    summary.complete = not truncated and bool(listed)
    if truncated:
        log.warning("adapter %s: the list was cut short by adapters.max_list_pages; no job is counted as missing", spec.key)
    if store:
        for job in jobs:
            drop = _read_hidden_places(conn, cfg, spec, fetch, sleep, job) if job.more_places else False
            outcome = ingest_job(conn, NormalizedJob(
                source=spec.source, external_id=job.external_id, title=job.title, company=spec.company,
                url=job.url, locations=job.locations, source_date=job.source_date, reference=job.reference), run_id)
            summary.outcomes[outcome.outcome] = summary.outcomes.get(outcome.outcome, 0) + 1
            summary.new += outcome.outcome == "new"
            if drop and outcome.outcome == "new":
                db.archive_job(conn, outcome.job_id, "filter", "no_zurich_place",
                               "the list row says '+N more' and the job page names no Zurich place", run_id,
                               approved_folder=cfg["paths"]["approved"])
                log.info("adapter %s: job %s archived: '+N more' row, no Zurich place on the job page", spec.key, job.external_id)
        if summary.complete:
            summary.closed, summary.reopened = track_listing(conn, spec, listed, a["closed_after_missing"])
    return summary, jobs


def _is_zurich(place: str) -> bool:
    return place.split(",")[0].strip().casefold().replace("ü", "u") == "zurich"


def _read_hidden_places(conn, cfg: dict, spec: AdapterSpec, fetch, sleep, job: ListedJob) -> bool:
    """A new list row with "+N more" (SIX): read its job page once and add the Zurich place it names (the only hidden place
    that matters). Returns True when the page names no Zurich place, so the caller drops the job (decided 4 Nov 2026). A job
    already stored or archived is not read again, and neither is a row that already lists Zurich; a failed page read, or a
    page that names no place at all, keeps the row as listed."""
    if conn.execute("SELECT 1 FROM job_sources WHERE source = ? AND external_id = ? UNION ALL "
                    "SELECT 1 FROM job_archive WHERE source = ? AND external_id = ?",
                    (spec.source, job.external_id, spec.source, job.external_id)).fetchone():
        return False
    if any(_is_zurich(p) for p in job.locations):                       # a Zurich place is already listed: nothing to look for
        return False
    try:
        address = detail_address(spec, job.url)
        if not spec.check_robots:
            sleep(cfg["adapters"]["delay_seconds"])
        detail = spec.parse_detail(fetch(address))
    except (FetchError, PageError, DescriptionError) as exc:
        log.warning("adapter %s: the places of %s could not be read (%s); the row is kept as listed", spec.key, job.external_id, exc)
        return False
    if not detail.locations:
        log.warning("adapter %s: the job page of %s names no place; the row is kept as listed", spec.key, job.external_id)
        return False
    zurich = [p for p in dict.fromkeys(detail.locations) if _is_zurich(p)]
    if not zurich:
        return True
    job.locations.append(zurich[0])
    return False


def track_listing(conn, spec: AdapterSpec, listed: set[str], after: int) -> tuple[int, int]:
    """Record a COMPLETE list read: which stored jobs of the site were listed and which were missing.

    `listed` holds the ids of every job on the site's list, before the list filter (country, places): a job that the filter
    drops is still on the site, so it is not missing; only a job absent from the site's list is. A sighting missing in
    `after` complete reads in a row marks its job closed (`closed_at`); when it is listed again, the count and the marker are
    cleared. The job is never deleted or archived; its status is not touched, except that a job still `awaiting_llm` goes back to
    `new` or `scored` (`postpone.release_closed`). A job counts as closed only when all of
    its site sightings are missing; sightings from mail are ignored. Returns (marked closed, reopened).
    """
    now, closed, reopened = db.utcnow(), 0, 0
    sites = [s.source for s in REGISTRY]
    with conn:
        touched = set()
        for r in conn.execute("SELECT DISTINCT job_id, external_id FROM job_sources WHERE source = ?", (spec.source,)).fetchall():
            if r["external_id"] in listed:
                conn.execute("UPDATE job_sources SET last_listed_at = ?, missed_reads = 0 WHERE job_id = ? AND source = ? AND external_id = ?",
                             (now, r["job_id"], spec.source, r["external_id"]))
            else:
                conn.execute("UPDATE job_sources SET missed_reads = missed_reads + 1 WHERE job_id = ? AND source = ? AND external_id = ?",
                             (r["job_id"], spec.source, r["external_id"]))
            touched.add(r["job_id"])
        for job_id in sorted(touched):
            misses = [r[0] for r in conn.execute(
                f"SELECT missed_reads FROM job_sources WHERE job_id = ? AND source IN ({', '.join('?' * len(sites))})", (job_id, *sites))]
            gone = bool(misses) and min(misses) >= after
            was = conn.execute("SELECT closed_at FROM jobs WHERE id = ?", (job_id,)).fetchone()["closed_at"]
            if gone and was is None:
                conn.execute("UPDATE jobs SET closed_at = ? WHERE id = ?", (now, job_id))
                postpone.release_closed(conn, job_id, now)          # a job that waited for a score does not wait for ever (v0.90.0)
                closed += 1
                log.info("adapter %s: job %d is no longer listed (missing in %d complete reads); marked closed", spec.key, job_id, after)
            elif not gone and was is not None:
                conn.execute("UPDATE jobs SET closed_at = NULL WHERE id = ?", (job_id,))
                reopened += 1
                log.info("adapter %s: job %d is listed again; reopened", spec.key, job_id)
    return closed, reopened


def _used_today(conn, spec: AdapterSpec, now: datetime) -> int:
    day = now.strftime("%Y-%m-%d")
    sources = spec.page_sources()
    return conn.execute(
        f"""SELECT COUNT(*) FROM description_fetches d
           WHERE substr(d.attempted_at, 1, 10) = ?
             AND EXISTS (SELECT 1 FROM job_sources x WHERE x.job_id = d.job_id AND x.source IN ({', '.join('?' * len(sources))}))""",
        (day, *sources)).fetchone()[0]


def eligible_jobs(conn, spec: AdapterSpec, retry: bool = False) -> list[dict]:
    """Open (not closed) jobs of this source that passed the first filter run, still without a description, not fetched before.

    A job counts for the adapter when its first sighting is from the adapter's own source or from one of `detail_sources`
    (Julius Baer: a job that first arrived in the alert mail and was later also listed on the site, or not listed at all).
    A job first seen in the LinkedIn alert and also listed on the employer's site (a sighting of the adapter's own source) counts
    too, and its page is the one of the site's sighting (v0.70.0, decided 3 Nov 2026).
    """
    sources = spec.page_sources()
    rows = conn.execute(
        f"""SELECT j.id, s.url, s.external_id
           FROM jobs j JOIN job_sources fs ON fs.id = (SELECT MIN(id) FROM job_sources WHERE job_id = j.id)
           JOIN job_sources s ON s.id = CASE WHEN fs.source IN ({', '.join('?' * len(sources))}) THEN fs.id
               WHEN fs.source = ? THEN (SELECT MIN(id) FROM job_sources x WHERE x.job_id = j.id AND x.source = ? AND x.url IS NOT NULL) END
           LEFT JOIN description_fetches d ON d.job_id = j.id
           WHERE j.description_missing = 1 AND s.url IS NOT NULL AND j.closed_at IS NULL
             AND EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id = j.id AND f.filter = 'seniority')
             AND (d.job_id IS NULL OR (? AND d.status = 'error'))
           ORDER BY j.id""", (*sources, LINKEDIN_SOURCE, spec.source, int(retry))).fetchall()
    return [dict(r) for r in rows]


def detail_address(spec: AdapterSpec, url: str) -> str:
    """The address that is read for the job page at `url` (the same, unless the site has a separate address for the data)."""
    address = spec.detail_url(url) if spec.detail_url else url
    _check_url(spec, address)
    return address


def read_page(cfg: dict, spec: AdapterSpec, fetch, url: str, sleep=None) -> str:
    """One job page of the site at `url` (a stored job address), read the way `fetch_details` reads it: at the address of
    `detail_address`, after a pause, and with robots.txt respected for an adapter with `check_robots`. Raises FetchError."""
    sleep = sleep or time.sleep
    address = detail_address(spec, url)
    if spec.check_robots:
        return _Paced(cfg, spec, fetch, sleep)(address)
    sleep(cfg["adapters"]["delay_seconds"])
    return fetch(address)


def fetch_details(conn, cfg: dict, spec: AdapterSpec, fetch, sleep=time.sleep, limit: int | None = None,
                  retry: bool = False, now: datetime | None = None, run_id: str | None = None) -> FetchSummary:
    require_enabled(cfg, spec)
    a = cfg["adapters"]
    now = now or datetime.now(timezone.utc)
    summary = FetchSummary(remaining_today=max(0, a["daily_cap"] - _used_today(conn, spec, now)))
    todo = eligible_jobs(conn, spec, retry)
    allowed = min(summary.remaining_today, limit if limit else summary.remaining_today)
    paced = _Paced(cfg, spec, fetch, sleep) if spec.check_robots and todo[:allowed] else None
    for job in todo[:allowed]:
        try:
            _check_job_url(spec, job["url"])
            page_url = detail_address(spec, job["url"])
        except FetchError as exc:
            summary.items.append((job["id"], f"skipped: {exc}"))
            continue
        if paced:
            try:
                paced.check(page_url)
            except FetchError as exc:
                summary.stopped = f"{spec.label}: {exc}; no further page is requested"
                break
        elif summary.attempted:
            sleep(a["delay_seconds"])
        summary.attempted += 1
        status, http_status, error = "ok", None, None
        try:
            detail = spec.parse_detail((paced or fetch)(page_url))
            result = apply_description(conn, cfg, job["id"], detail.text, source=spec.source, run_id=run_id, check_language=True,
                                       posted_on=detail.posted)
            summary.fetched += 1
            summary.items.append((job["id"], result.message))
        except FetchError as exc:
            status, http_status, error = "error", exc.http_status, str(exc)
        except (PageError, DescriptionError) as exc:
            status, error = "error", str(exc)
        with conn:
            conn.execute("INSERT OR REPLACE INTO description_fetches (job_id, attempted_at, status, http_status, error) VALUES (?, ?, ?, ?, ?)",
                         (job["id"], now.strftime("%Y-%m-%dT%H:%M:%SZ"), status, http_status, error))
        if status == "error":
            summary.failed += 1
            summary.items.append((job["id"], f"not fetched: {error}"))
            if http_status in STOP_STATUSES or (http_status is None and error and error.startswith("network error")):
                summary.stopped = f"{spec.label}: stopped after job {job['id']}: {error}; nothing more is requested in this run"
                break
    summary.remaining_today = max(0, a["daily_cap"] - _used_today(conn, spec, now))
    if not summary.stopped and len(todo) > allowed and summary.remaining_today == 0:
        summary.stopped = f"{spec.label}: daily cap of {a['daily_cap']} pages reached; {len(todo) - allowed} job(s) wait for tomorrow"
    return summary


from . import csb, greenhouse, julius, lgt, six, sygnum, workday, zurich  # noqa: E402  (the entries need the classes above)

REGISTRY: list[AdapterSpec] = [six.SPEC, lgt.SPEC, sygnum.SPEC, julius.SPEC, *workday.SPECS, *csb.SPECS, zurich.SPEC, *greenhouse.SPECS]
