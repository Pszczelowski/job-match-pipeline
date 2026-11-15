"""Fetching UBS job descriptions for the user's own job search (goal.md 5.1).

Why this is allowed: the Terms of use of ubs.com prohibit systematic retrieval of content into a database "without the
prior written permission from UBS". The user states that he holds UBS's written permission to do this for personal use
(31 Oct 2026). The tool therefore does it only when the user has switched it on and written the basis down in the config:

    descriptions:
      fetch_enabled: true
      permission_note: "UBS written permission for personal use, received <date>, <where it is kept>"

The permission itself is the user's document and is not stored here. Conditions kept by the code, whatever the permission
says: only jobs that arrived in the user's own UBS alert mails, only the page of that job on jobs.ubs.com, each page at most
once, a pause between requests, a daily cap, an honest User-Agent, no retries, and a stop at the first refusal (HTTP 403,
429 or 5xx, or a network error). Nothing is done to avoid bot protection or login walls. The data stays in the private
database; nothing from the pages is written to the repository.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import db
from .descriptions import DescriptionError, apply_description
from .ubs_page import PageError, parse_job_page

ALLOWED_HOST = "jobs.ubs.com"
MAX_PAGE_BYTES = 5_000_000
STOP_STATUSES = {403, 429, 500, 502, 503, 504}


class FetchDisabled(Exception):
    """Fetching is switched off or its basis is not recorded."""


class FetchError(Exception):
    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


@dataclass
class FetchSummary:
    fetched: int = 0
    failed: int = 0
    attempted: int = 0
    remaining_today: int = 0
    stopped: str | None = None
    items: list[tuple[int, str]] = field(default_factory=list)       # (job id, message)


class JsonPost(dict):
    """The body of a POST that is sent as JSON (a plain dict is sent as a form, as a browser's "Next" button does it)."""


class PageFetcher:
    """Plain GET with an honest User-Agent. No retries. A redirect is followed only to the same scheme and host as the
    request (or `allowed_host`); any other is refused before it is followed, so a cookie never reaches another host.
    `path_allowed` (set per call by the robots.txt-checking wrappers) refuses a same-host redirect to a path robots.txt disallows.
    Only a robots.txt request follows redirects to other hosts (RFC 9309 2.3.1.2), at most five hops and without Cookie."""

    def __init__(self, user_agent: str, timeout: float = 30.0, allowed_host: str | None = None):
        self.user_agent, self.timeout, self.allowed_host = user_agent, timeout, allowed_host
        self.cookies: dict[str, dict[str, str]] = {}          # host -> name -> value, as a browser keeps them for a visit
        self.path_allowed = None                              # url -> bool, the robots.txt rules of the wrapper in use

    def _remember(self, host: str, response) -> None:
        headers = getattr(response, "headers", None)
        for line in (headers.get_all("Set-Cookie") or []) if headers is not None and hasattr(headers, "get_all") else []:
            name, _, rest = line.partition("=")
            self.cookies.setdefault(host, {})[name.strip()] = rest.split(";", 1)[0]

    def _open(self, request, scheme: str, robots: bool = False):
        host = urllib.parse.urlsplit(request.full_url).netloc
        allowed, path_allowed = self.allowed_host or host, self.path_allowed

        class Guard(urllib.request.HTTPRedirectHandler):
            max_redirections = 5 if robots else urllib.request.HTTPRedirectHandler.max_redirections

            def redirect_request(self, req, fp, code, msg, headers, newurl):
                target = urllib.parse.urlsplit(newurl)
                if robots:
                    if target.scheme not in ("http", "https"):
                        raise FetchError(f"redirect to {newurl!r} refused", code)
                elif target.scheme != scheme or target.netloc != allowed:
                    raise FetchError(f"redirect to {target.netloc or newurl!r} refused", code)
                elif path_allowed is not None and not path_allowed(newurl):
                    raise FetchError(f"redirect to {target.path or '/'} refused: robots.txt of {target.netloc} disallows it", code)
                new = super().redirect_request(req, fp, code, msg, headers, newurl)
                if new is not None and target.netloc != host:
                    new.headers.pop("Cookie", None)
                    new.unredirected_hdrs.pop("Cookie", None)
                return new
        return urllib.request.build_opener(Guard).open(request, timeout=self.timeout)

    def __call__(self, url: str, data: dict | None = None) -> str:
        """GET, or POST of a form (`data`) the way a browser's "Next" button does it; a `JsonPost` is sent as JSON.

        A JSON body, and any page under `/wday/cxs/` (the JSON interface that a Workday career site's own pages use),
        ask for `application/json`; everything else for HTML.
        """
        split = urllib.parse.urlsplit(url)
        host = split.netloc
        wants_json = isinstance(data, JsonPost) or "/wday/cxs/" in split.path
        headers = {"User-Agent": self.user_agent, "Accept": "application/json" if wants_json else "text/html"}
        if self.cookies.get(host):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies[host].items())
        body = None
        if isinstance(data, JsonPost):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self._open(request, split.scheme, robots=data is None and split.path == "/robots.txt" and not split.query) as response:
                self._remember(urllib.parse.urlsplit(getattr(response, "url", None) or url).netloc, response)
                raw = response.read(MAX_PAGE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise FetchError(f"HTTP {exc.code}", exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise FetchError(f"network error: {type(exc).__name__}") from None
        if len(raw) > MAX_PAGE_BYTES:
            raise FetchError("page is larger than the limit")
        return raw.decode("utf-8", "replace")


def require_enabled(cfg: dict) -> dict:
    d = cfg["descriptions"]
    if not d["fetch_enabled"]:
        raise FetchDisabled("fetching descriptions is switched off (descriptions.fetch_enabled is false); "
                            "add descriptions by hand with `set-description`, or switch it on in config.yaml")
    if not d["permission_note"].strip():
        raise FetchDisabled("descriptions.permission_note is empty: write down the basis (UBS written permission, date, "
                            "where it is kept) in config.yaml before fetching")
    return d


def _used_today(conn, now: datetime) -> int:
    day = now.strftime("%Y-%m-%d")
    return conn.execute("SELECT COUNT(*) FROM description_fetches WHERE substr(attempted_at, 1, 10) = ?", (day,)).fetchone()[0]


def eligible_jobs(conn, retry: bool = False) -> list[dict]:
    """Open jobs without a description whose UBS alert gave a job page, not fetched before (or failed and `retry`)."""
    rows = conn.execute(
        """SELECT j.id, j.status, s.url, s.external_id,
                  EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id = j.id AND f.filter = 'seniority') AS filtered
           FROM jobs j JOIN job_sources s ON s.id = (SELECT MIN(id) FROM job_sources WHERE job_id = j.id AND source = 'ubs_alert')
           LEFT JOIN description_fetches d ON d.job_id = j.id
           WHERE j.description_missing = 1 AND s.url IS NOT NULL AND (d.job_id IS NULL OR (? AND d.status = 'error'))
           ORDER BY j.id""", (int(retry),)).fetchall()
    return [dict(r) for r in rows]


def fetch_descriptions(conn, cfg: dict, fetch, sleep=time.sleep, limit: int | None = None, retry: bool = False,
                       now: datetime | None = None, run_id: str | None = None) -> FetchSummary:
    d = require_enabled(cfg)
    now = now or datetime.now(timezone.utc)
    summary = FetchSummary(remaining_today=max(0, d["daily_cap"] - _used_today(conn, now)))
    todo = eligible_jobs(conn, retry)
    allowed = min(summary.remaining_today, limit if limit else summary.remaining_today)
    for job in todo[:allowed]:
        parts = urllib.parse.urlsplit(job["url"])
        if parts.scheme != "https" or parts.netloc != ALLOWED_HOST:
            summary.items.append((job["id"], f"skipped: {job['url']!r} is not a page on {ALLOWED_HOST}"))
            continue
        if summary.attempted:
            sleep(d["delay_seconds"])
        summary.attempted += 1
        status, http_status, error = "ok", None, None
        try:
            page = parse_job_page(fetch(job["url"]))
            if page.job_id and page.job_id != str(job["external_id"]):
                raise PageError(f"the page is for job {page.job_id}, not {job['external_id']}")
            # an unfiltered new job only gets its text: the filters (language included) run on it afterwards
            result = apply_description(conn, cfg, job["id"], page.text, source="ubs_page", run_id=run_id,
                                       check_language=bool(job["filtered"]))
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
                summary.stopped = f"stopped after job {job['id']}: {error}; nothing more is requested in this run"
                break
    summary.remaining_today = max(0, d["daily_cap"] - _used_today(conn, now))
    if not summary.stopped and len(todo) > allowed and summary.remaining_today == 0:
        summary.stopped = f"daily cap of {d['daily_cap']} pages reached; {len(todo) - allowed} job(s) wait for tomorrow"
    return summary
