"""Parser for the LinkedIn job-alert email (goal.md 5.1).

Input is the HTML body of one alert (sender `jobalerts-noreply@linkedin.com`, template
`email_job_alert_digest_01`); reading the mailbox and decoding the MIME part is the ingestion step, not done here.

Format, as observed in ONE real alert (1 Nov 2026, five jobs; other LinkedIn alert templates are not seen): a heading
`data-test-id="email-header"` ("Your job alert for <strong>keywords</strong>"), then one block
`data-test-id="job-card"` per job. A card holds a link to `/comm/jobs/view/<job id>/?<tracking parameters>` (three
times: logo, title, wrapper), the title as the text of the innermost link, a paragraph "Company · Location (Hybrid)"
and optional badge paragraphs ("Actively recruiting", "Easy Apply", ...). There is no description, no posting date and
no salary in the mail. The tracking parameters (trackingId, refId, lipi, midToken, otpToken, ...) are personal to the
recipient and are dropped: the job URL is rebuilt from the job id.

The cards are found by their `data-test-id`, not by position or by class names (the classes are utility classes of
LinkedIn's mail framework). A mail that has neither a card nor the heading is an unknown format (ParseError), never
"zero jobs". A heading without cards is an empty result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

from ..models import NormalizedJob

SOURCE = "linkedin_alert"
JOB_URL = "https://www.linkedin.com/jobs/view/{jobid}/"

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SKIP_TEXT = {"style", "script", "head", "title"}
_PATH_ID = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d{5,})(?:[/?#]|$)")
_WORK_MODE = re.compile(r"\s*\(\s*(on-?site|hybrid|remote|vor ort)\s*\)\s*$", re.IGNORECASE)
_BADGE = re.compile(r"actively (?:recruiting|hiring)|easy apply|apply with|early applicant|promoted|connection|alumni|applicant|reposted",
                    re.IGNORECASE)
_HEADING = re.compile(r"your job alert for\s*", re.IGNORECASE)


class ParseError(ValueError):
    """The mail is not a recognisable LinkedIn job alert."""


@dataclass
class LinkedInJob:
    external_id: str            # the LinkedIn job id from the link; the stable key
    title: str
    company: str
    url: str                    # canonical link rebuilt from the job id (no tracking parameters)
    location: str = ""          # as written, without the work mode; "" if the card names none
    work_mode: str = ""         # "On-site", "Hybrid" or "Remote" as LinkedIn writes it; "" if not stated
    badges: list[str] = field(default_factory=list)      # "Actively recruiting", "Easy Apply", ...
    raw: dict = field(default_factory=dict)              # the card's text lines, for audit


@dataclass
class ParseResult:
    criteria: str                                        # saved-search keywords, e.g. "business analyst"
    jobs: list[LinkedInJob] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)      # cards that were skipped, with the reason


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict, parent: "_Node | None") -> None:
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[_Node | str] = []

    def walk(self):
        """This node and all descendants, in document order (iterative: mail HTML nests tables deeply)."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(c for c in reversed(node.children) if isinstance(c, _Node))

    def text(self) -> str:
        parts: list[str] = []
        stack: list[_Node | str] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in _SKIP_TEXT:
                stack.extend(reversed(item.children))
        return re.sub(r"\s+", " ", "".join(parts).replace("\xa0", " ")).strip()


class _Builder(HTMLParser):
    """A small tolerant tree builder: unclosed tags are closed by the next end tag of an ancestor."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self._cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v or "") for k, v in attrs}, self._cur)
        self._cur.children.append(node)
        if tag not in _VOID:
            self._cur = node

    def handle_startendtag(self, tag, attrs):
        self._cur.children.append(_Node(tag, {k: (v or "") for k, v in attrs}, self._cur))

    def handle_endtag(self, tag):
        node = self._cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self._cur = node.parent

    def handle_data(self, data):
        self._cur.children.append(data)


def job_id_from_url(href: str) -> str:
    """The LinkedIn job id in a link to a job posting, or '' (other links, other hosts)."""
    if not href:
        return ""
    parts = urlparse(href.strip())
    if not (parts.netloc.lower() == "linkedin.com" or parts.netloc.lower().endswith(".linkedin.com")):
        return ""
    m = _PATH_ID.search(parts.path + "/")
    if m:
        return m.group(1)
    value = (parse_qs(parts.query).get("currentJobId") or [""])[0]
    return value if value.isdigit() else ""


def _split_line(line: str) -> tuple[str, str, str]:
    """'Company · Zurich (Hybrid)' -> ('Company', 'Zurich', 'Hybrid'). No separator: the whole line is the company."""
    company, sep, place = line.rpartition("·")
    if not sep:
        return line.strip(), "", ""
    place = place.strip()
    mode = _WORK_MODE.search(place)
    if mode:
        place = place[:mode.start()].strip()
    return company.strip(), place, (mode.group(1) if mode else "")


def _mode_text(value: str) -> str:
    return {"on-site": "On-site", "onsite": "On-site", "hybrid": "Hybrid", "remote": "Remote", "vor ort": "On-site"}.get(
        value.lower(), value)


def parse_alert(html: str) -> ParseResult:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    nodes = list(builder.root.walk())
    cards = [n for n in nodes if n.attrs.get("data-test-id") == "job-card"]
    heading = next((n.text() for n in nodes if n.attrs.get("data-test-id") == "email-header"), None)
    if not cards and (heading is None or not _HEADING.match(heading)):
        raise ParseError("no job cards and no 'Your job alert for' heading; not a recognised LinkedIn job alert")

    result = ParseResult(criteria=_HEADING.sub("", heading, count=1).strip() if heading else "")
    seen: set[str] = set()
    for n, card in enumerate(cards, start=1):
        anchors = [(a, job_id_from_url(a.attrs.get("href", ""))) for a in card.walk() if a.tag == "a"]
        anchors = [(a, jid) for a, jid in anchors if jid]
        if not anchors:
            result.errors.append(f"card {n}: no link to a job posting; skipped")
            continue
        jobid = anchors[0][1]
        # the title is the text of the innermost job link that has text (the logo link has none, the wrapper has a link inside)
        title = next((a.text() for a, _ in anchors if a.text() and not any(c.tag == "a" for c in a.walk() if c is not a)), "")
        if not title:
            result.errors.append(f"card {n} (job {jobid}): no title; skipped")
            continue
        lines = [t for t in (p.text() for p in card.walk() if p.tag == "p") if t]
        first = lines[0] if lines else ""
        if not first or ("·" not in first and _BADGE.search(first)):
            result.errors.append(f"card {n} (job {jobid}): no company line; skipped")
            continue
        company, location, mode = _split_line(first)
        if not company:
            result.errors.append(f"card {n} (job {jobid}): no company name; skipped")
            continue
        if jobid in seen:
            continue                                    # the same job twice in one mail
        seen.add(jobid)
        result.jobs.append(LinkedInJob(
            external_id=jobid, title=title, company=company, url=JOB_URL.format(jobid=jobid), location=location,
            work_mode=_mode_text(mode) if mode else "", badges=lines[1:], raw={"lines": lines}))
    return result


def to_job(job: LinkedInJob, message_id: str = "") -> NormalizedJob:
    """Convert a parsed card into the common job record used by dedup.

    `source_date` stays empty: the mail states no listing date, and the date of the mail is not one (the same job
    comes back in later digests, which must not count as a repost). The work mode goes into `raw`, not into the
    location, so the location rule (5.3.1) sees the place only.
    """
    raw = dict(job.raw)
    if job.work_mode:
        raw["work_mode"] = job.work_mode
    if job.badges:
        raw["badges"] = job.badges
    return NormalizedJob(
        source=SOURCE,
        external_id=job.external_id,
        title=job.title,
        company=job.company,
        url=job.url,
        locations=[job.location] if job.location else [],
        message_id=message_id,
        raw=raw,
    )
