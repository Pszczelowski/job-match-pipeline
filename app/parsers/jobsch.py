"""Parser for the jobs.ch job-alert email (goal.md 5.1, v0.84.0).

Input is the HTML body of one alert (sender `jobmail@jobs.ch`, subject "3 new job offers found"); reading the mailbox and
decoding the MIME part is the ingestion step, not done here.

Format, as observed in ONE real alert (12 Nov 2026; other variants are not seen): one section per saved search ("2 new
jobs for <search>"), each listing its jobs as a row with a logo link and a text link to
`https://www.jobs.ch/en/vacancies/detail/<uuid>/?...&utm_content=job-link&...` (the query is personal: hash, profile id, uid),
and under the title link a paragraph "company, place". The same job can be listed in two sections: it is one job. After the
sections come a block "Don't miss! These jobs are still online" (links with `utm_content=job-link-promo`, the company line
BEFORE the link) and account, unsubscribe and salary links; the promo block is skipped (the user's decision, 12 Nov 2026: it
is advertising, not a match for a saved search) and so are the other links.

A job is found by its link (host `jobs.ch`, path `.../vacancies/detail/<uuid>`, `utm_content=job-link`). The id is the uuid,
lower-cased; the URL is rebuilt as `https://www.jobs.ch/en/vacancies/detail/<uuid>/` without any query (the short form; the
long slug form is disallowed by robots.txt, see `app/resolver/jobcloud.py`). The title is the text of the link, the company
and the place are the paragraph after it, split at the last ", ". There is no description and no posting date in the mail.
A mail that has neither a job link nor an alert marker is an unknown format (ParseError), never "zero jobs"; the markers
without jobs are an empty result. Fetching job pages is the resolver's task (goal.md 5.1a), not done here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from ..models import NormalizedJob
from .linkedin import _Builder, _Node

SOURCE = "jobsch_alert"

_DETAIL = re.compile(r"^/(?:[a-z]{2}/)?vacancies/detail/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$", re.IGNORECASE)
_MARKER = re.compile(r"\bnew jobs? for\b|\bjob alerts?\b", re.IGNORECASE)


class ParseError(ValueError):
    """The mail is not a recognisable jobs.ch job alert."""


@dataclass
class JobschJob:
    external_id: str            # the uuid of the job link, lower-cased; the stable key
    title: str
    url: str                    # `https://www.jobs.ch/en/vacancies/detail/<uuid>/`, no query
    company: str = ""
    location: str = ""          # as written in the mail; "" if the line has no place
    raw: dict = field(default_factory=dict)              # the company line, for audit


@dataclass
class ParseResult:
    criteria: str = ""                                   # the mail names no single search; kept for the common shape
    jobs: list[JobschJob] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)      # rows that were skipped, with the reason


def job_from_url(href: str) -> tuple[str, str]:
    """(uuid lower-cased, clean URL without query) of a `job-link` (not promo) link to a jobs.ch posting, or ('', '')."""
    if not href:
        return "", ""
    parts = urlparse(href.strip())
    host = parts.netloc.lower()
    if host not in ("jobs.ch", "www.jobs.ch"):
        return "", ""
    m = _DETAIL.match(parts.path)
    if not m or parse_qs(parts.query).get("utm_content") != ["job-link"]:
        return "", ""
    uid = m.group("id").lower()
    return uid, f"https://www.jobs.ch/en/vacancies/detail/{uid}/"


def _next_paragraph(anchor: _Node) -> str:
    """The text of the paragraph that follows the one holding the title link ("" if there is none)."""
    p = anchor.parent
    while p is not None and p.tag != "p":
        p = p.parent
    if p is None or p.parent is None:
        return ""
    siblings = [c for c in p.parent.children if isinstance(c, _Node)]
    later = [c for c in siblings[siblings.index(p) + 1:] if c.tag == "p"]
    return later[0].text() if later else ""


def parse_alert(html: str) -> ParseResult:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    anchors = []                                        # (anchor, uuid, clean url)
    for a in builder.root.walk():
        if a.tag == "a":
            uid, url = job_from_url(a.attrs.get("href", ""))
            if uid:
                anchors.append((a, uid, url))
    if not anchors and not _MARKER.search(builder.root.text()):
        raise ParseError("no job link and no 'new jobs for' line; not a recognised jobs.ch job alert")

    result = ParseResult()
    seen: set[str] = set()
    for n, (anchor, uid, url) in enumerate(anchors, start=1):
        title = anchor.text()
        if not title:
            continue                                    # the logo link of the row: the text link has the title
        if uid in seen:
            continue                                    # the same job in a second section
        line = _next_paragraph(anchor)
        if not line:
            result.errors.append(f"job {n} ({uid}): no company line under the title; skipped")
            continue
        seen.add(uid)
        company, sep, place = line.rpartition(", ")
        if not sep:
            company, place = line, ""
        result.jobs.append(JobschJob(external_id=uid, title=title, url=url, company=company.strip(), location=place.strip(),
                                     raw={"line": line}))
    return result


def to_job(job: JobschJob, message_id: str = "") -> NormalizedJob:
    """Convert a parsed row into the common job record used by dedup.

    `source_date` and `description` stay empty: the mail states no listing date and has no description (the resolver
    reads the posting page later, goal.md 5.1a). Such a job is scored from the title only until then.
    """
    return NormalizedJob(
        source=SOURCE,
        external_id=job.external_id,
        title=job.title,
        company=job.company,
        url=job.url,
        locations=[job.location] if job.location else [],
        message_id=message_id,
        raw=dict(job.raw),
    )
