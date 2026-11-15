"""Parser for the Julius Baer job-alert email (goal.md 5.1, v0.48.0).

Input is the HTML body of one alert (sender `noreply.hrconnect@juliusbaer.com`, subject "New Job Alert for Zurich",
a Workday "HR Connect" notification); reading the mailbox and decoding the MIME part is the ingestion step, not done here.

Format, as observed in ONE real alert (1 Nov 2026, one job; other Julius Baer alert variants are not seen): an intro
text, a line "Matching Opportunities:", then one row per job with three columns: the title as a paragraph
("Account Manager, Turkey, 100% (f/m/d)"), the place as a paragraph ("Geneva") and an "Apply" button, a link to
`https://juliusbaer.wd3.myworkdayjobs.com/External/job/<place>/<slug>_<id>?shared_id=<token>`. Then "See all jobs", links
to the job-alert settings, social links and a footer with the recipient's address and a personal unsubscribe link.
There is no description, no posting date and no company name in the job rows (the sender is Julius Baer). The
`shared_id` is personal to the recipient and is dropped: the job URL is rebuilt from the path.

A job is found by its link (host `*.myworkdayjobs.com`, path `.../job/<place>/<slug>_<id>`), which is more stable than
the layout classes (generic e-mail builder output). The title and the place are the first two paragraphs of the
smallest block around the link that has paragraphs (at most three: a bigger block is not a row). The id is the part after the last underscore of the path, as
written but lower-cased (`r-20213-1`); the requisition number (`R-20213`) is kept as the reference. A mail that has
neither a job link nor one of the alert markers is an unknown format (ParseError), never "zero jobs"; the markers
without jobs are an empty result.

The place is NOT taken from the path: a row without a place paragraph keeps the job without a location (the location
rule then gives "conditional"). Fetching the job page is not done anywhere (goal.md 5.1: the terms are not checked).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..models import NormalizedJob
from .linkedin import _Builder, _Node

SOURCE = "julius_alert"
COMPANY = "Julius Baer"

_JOB_PATH = re.compile(r"^(?P<base>/(?:[A-Za-z]{2}-[A-Za-z]{2}/)?[^/]+/job/(?:[^/]+/)?[^/]*_(?P<id>[A-Za-z0-9][A-Za-z0-9-]*))/?$")
_REQUISITION = re.compile(r"^r-\d+", re.IGNORECASE)
MAX_PARAGRAPHS = 3             # a row is title, place (and perhaps one more line); a bigger block is the whole mail, not a row
_MARKER = re.compile(r"matching opportunities|new job alert|matches your job alert", re.IGNORECASE)


class ParseError(ValueError):
    """The mail is not a recognisable Julius Baer job alert."""


@dataclass
class JuliusJob:
    external_id: str            # the id from the job link path, lower-cased (`r-20213-1`); the stable key
    title: str
    url: str                    # the job link without any query string (`shared_id` dropped)
    company: str = COMPANY
    location: str = ""          # as written in the mail; "" if the row has no place
    reference: str = ""         # the requisition number (`R-20213`); "" if the id does not start with one
    raw: dict = field(default_factory=dict)              # the row's paragraphs, for audit


@dataclass
class ParseResult:
    criteria: str = ""                                   # the mail names no search; kept for the common shape
    jobs: list[JuliusJob] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)      # rows that were skipped, with the reason


def job_from_url(href: str) -> tuple[str, str]:
    """(job id lower-cased, clean URL without query) of a link to a Julius Baer job posting, or ('', '')."""
    if not href:
        return "", ""
    parts = urlparse(href.strip())
    host = parts.netloc.lower()
    if not (host == "myworkdayjobs.com" or host.endswith(".myworkdayjobs.com")):
        return "", ""
    m = _JOB_PATH.match(parts.path)
    if not m:
        return "", ""
    return m.group("id").lower(), f"https://{host}{m.group('base')}"


def _count(node: _Node, tag: str) -> int:
    return sum(1 for n in node.walk() if n.tag == tag)


def _job_links_in(node: _Node) -> int:
    return sum(1 for n in node.walk() if n.tag == "a" and job_from_url(n.attrs.get("href", ""))[0])


def parse_alert(html: str) -> ParseResult:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    links = []                                          # (anchor, job id, clean url)
    for a in builder.root.walk():
        if a.tag == "a":
            jid, url = job_from_url(a.attrs.get("href", ""))
            if jid:
                links.append((a, jid, url))
    if not links and not _MARKER.search(builder.root.text()):
        raise ParseError("no job link and no 'Matching Opportunities' line; not a recognised Julius Baer job alert")

    result = ParseResult()
    seen: set[str] = set()
    for n, (anchor, jid, url) in enumerate(links, start=1):
        block = anchor.parent
        while block is not None and block.tag != "#root" and not _count(block, "p"):
            block = block.parent
        if block is None or block.tag == "#root" or _job_links_in(block) != 1 or _count(block, "p") > MAX_PARAGRAPHS:
            result.errors.append(f"job {n} ({jid}): no title and place around the link; skipped")
            continue
        paragraphs = [p.text() for p in block.walk() if p.tag == "p"]
        title = paragraphs[0]
        if not title:
            result.errors.append(f"job {n} ({jid}): no title; skipped")
            continue
        if jid in seen:
            continue                                    # the same job twice in one mail
        seen.add(jid)
        place = paragraphs[1] if len(paragraphs) > 1 else ""
        req = _REQUISITION.match(jid)
        result.jobs.append(JuliusJob(external_id=jid, title=title, url=url, location=place,
                                     reference=req.group(0).upper() if req else "", raw={"lines": [p for p in paragraphs if p]}))
    return result


def to_job(job: JuliusJob, message_id: str = "") -> NormalizedJob:
    """Convert a parsed row into the common job record used by dedup.

    `source_date` and `description` stay empty: the mail states no listing date (and the date of the mail is not one:
    the same job may come back in later digests), and it has no description. Such a job is scored from the title only.
    """
    return NormalizedJob(
        source=SOURCE,
        external_id=job.external_id,
        title=job.title,
        company=job.company,
        url=job.url,
        locations=[job.location] if job.location else [],
        reference=job.reference,
        message_id=message_id,
        raw=dict(job.raw),
    )
