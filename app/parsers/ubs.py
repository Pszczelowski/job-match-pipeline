"""Parser for the UBS job-alert email (goal.md 5.1).

Input is the HTML body of one alert (sender agentserver@ent.brassring.com, subject "Search agent
results"); reading the mailbox and decoding the MIME part is the ingestion step, not done here.

Format, as observed in four real alerts (24-30 Oct 2026): one HTML table, a header row of <th>
cells, then one row per job whose <td> cells carry a class matching `COLUMN\\w+` (the class
alternates between rows, so it is matched by pattern). Columns are found by header name, not by
position. The "delete this Agent" link holds a per-user token and is ignored.

A mail with zero jobs has not been observed. A mail that has neither the header sentence nor a job
table is treated as an unknown format (ParseError), never as "zero jobs".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

from ..models import NormalizedJob

SOURCE = "ubs_alert"
JOB_URL = "https://jobs.ubs.com/1033/asp/tg/cim_jobdetail.asp?partnerid=25008&siteid=5012&jobid={jobid}"
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_HEADER_SENTENCE = re.compile(r"These jobs matched the criteria specified for:\s*(.*?)\s*(?:<|$)", re.S)

# header text (lower case) -> field name
_COLUMNS = {
    "title": "title",
    "job reference #": "reference",
    "location": "location",
    "city": "city",
    "function category": "function_category",
    "business divisions": "business_division",
    "job type": "job_type",
    "business unit": "business_unit",
    "business sector": "business_sector",
    "business segment": "business_segment",
    "corporate title": "corporate_title",
    "date updated": "date_updated",
}


class ParseError(ValueError):
    """The mail is not a recognisable UBS alert."""


@dataclass
class UbsJob:
    external_id: str            # jobid from the link; the stable key
    reference: str              # "Job Reference #", e.g. 343734BR; second key
    title: str
    url: str                    # canonical link rebuilt from the jobid (no per-user parameters)
    locations: list[str] = field(default_factory=list)   # "Location" column, split on commas
    cities: list[str] = field(default_factory=list)      # "City" column, split on commas
    function_category: str = ""
    business_division: str = ""
    job_type: str = ""
    business_unit: str = ""
    business_sector: str = ""
    business_segment: str = ""
    corporate_title: str = ""
    date_updated: str = ""      # ISO date (YYYY-MM-DD); "" if missing or unreadable
    raw: dict = field(default_factory=dict)              # every cell as text, for audit


@dataclass
class ParseResult:
    criteria: str                                        # saved-search text, e.g. "Zurich"
    jobs: list[UbsJob] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)      # rows that were skipped, with the reason


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[str] | None = None
        self.rows: list[list[tuple[str, str | None]]] = []   # per row: (cell text, first link)
        self._cells: list[tuple[str, str | None]] | None = None
        self._ths: list[str] | None = None
        self._cell_kind: str | None = None
        self._cell_text: list[str] = []
        self._cell_href: str | None = None
        self._cell_is_data = False
        self.saw_table = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self.saw_table = True
        elif tag == "tr":
            self._cells, self._ths = [], []
        elif tag in ("td", "th"):
            self._cell_kind = tag
            self._cell_text, self._cell_href = [], None
            self._cell_is_data = tag == "td" and bool(re.fullmatch(r"COLUMN\w+", (a.get("class") or "").strip()))
        elif tag == "a" and self._cell_kind and self._cell_href is None and a.get("href"):
            self._cell_href = a["href"]
        elif tag == "br" and self._cell_kind:
            self._cell_text.append(" ")

    def handle_data(self, data):
        if self._cell_kind:
            self._cell_text.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell_kind == tag:
            text = re.sub(r"\s+", " ", "".join(self._cell_text)).strip()
            if tag == "th" and self._ths is not None:
                self._ths.append(text)
            elif self._cell_is_data and self._cells is not None:
                self._cells.append((text, self._cell_href))
            self._cell_kind = None
        elif tag == "tr":
            if self._ths and any(self._ths) and self.headers is None:
                self.headers = [t for t in self._ths if t]
            elif self._cells:
                self.rows.append(self._cells)
            self._cells = self._ths = None


def _split(value: str) -> list[str]:
    out: list[str] = []
    for part in value.split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def parse_date(value: str) -> str:
    """'29-Oct-2026' -> '2026-10-29'; '' if it cannot be read."""
    m = re.fullmatch(r"\s*(\d{1,2})-([A-Za-z]{3})[a-z]*-(\d{4})\s*", value or "")
    if not m or m.group(2).lower() not in _MONTHS:
        return ""
    return f"{int(m.group(3)):04d}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


def _jobid(href: str | None) -> str:
    if not href:
        return ""
    query = {k.lower(): v for k, v in parse_qs(urlparse(href).query).items()}
    value = (query.get("jobid") or [""])[0]
    return value if value.isdigit() else ""


def parse_alert(html: str) -> ParseResult:
    sentence = _HEADER_SENTENCE.search(html)
    parser = _TableParser()
    parser.feed(html)
    parser.close()

    if parser.headers is None:
        if sentence and not parser.saw_table:
            return ParseResult(criteria=sentence.group(1).strip())  # sentence but no table at all
        raise ParseError("no job table header found; not a recognised UBS alert")
    index = {_COLUMNS[h.lower()]: i for i, h in enumerate(parser.headers) if h.lower() in _COLUMNS}
    for needed in ("title", "reference"):
        if needed not in index:
            raise ParseError(f"job table has no '{needed}' column; header was {parser.headers}")

    result = ParseResult(criteria=sentence.group(1).strip() if sentence else "")
    for n, cells in enumerate(parser.rows, start=1):
        if len(cells) < len(parser.headers):
            result.errors.append(f"row {n}: {len(cells)} cells, expected {len(parser.headers)}; skipped")
            continue
        jobid = _jobid(cells[index["title"]][1])
        if not jobid:
            result.errors.append(f"row {n}: no jobid in the title link; skipped")
            continue

        def cell(name: str) -> str:
            return cells[index[name]][0] if name in index else ""

        raw = {h: cells[i][0] for i, h in enumerate(parser.headers)}
        result.jobs.append(UbsJob(
            external_id=jobid,
            reference=cell("reference"),
            title=cell("title"),
            url=JOB_URL.format(jobid=jobid),
            locations=_split(cell("location")),
            cities=_split(cell("city")),
            function_category=cell("function_category"),
            business_division=cell("business_division"),
            job_type=cell("job_type"),
            business_unit=cell("business_unit"),
            business_sector=cell("business_sector"),
            business_segment=cell("business_segment"),
            corporate_title=cell("corporate_title"),
            date_updated=parse_date(cell("date_updated")),
            raw=raw,
        ))
    return result


def to_job(job: UbsJob, message_id: str = ""):
    """Convert a parsed UBS row into the common job record used by dedup."""
    return NormalizedJob(
        source=SOURCE,
        external_id=job.external_id,
        title=job.title,
        company="UBS",
        url=job.url,
        locations=job.cities or job.locations,
        reference=job.reference,
        source_date=job.date_updated,
        message_id=message_id,
        raw=job.raw,
    )
