"""SAP SuccessFactors career sites with the "career site builder" layout (Swiss Re, EY), goal.md 5.1 (v0.70.0).

The same layout as SIX (`six.py`: a table of `tr.data-row`, a JobPosting block on the job page) with a result line
"Results 1 - 25 of 141" and `&startrow=N` paging. Tier T2 (HTML sites whose terms may forbid automated reading; the user
accepts the risk in `decision_note`); robots.txt of the host is read first and respected at run time. `/services/` is never
requested (EY disallows it).

List: location "City, CC", "City, Region, CC" or "City, CC, ZIP"; a job is kept when one of the parts after the city is a code
of `country_codes` (or when "+N more" hides further places). The first page states the total; with `size` rows on it, the
other pages are `startrow=size, 2*size, ...` below the total.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass

from . import AdapterSpec, JobDetail, ListedJob, PageError, html_to_text
from .six import LINK, LOCATION, PHONE, ROW, _posted, _span_inner

TOTAL = re.compile(r"of\s*<b>\s*([\d,.'’]+)\s*</b>")


@dataclass(frozen=True)
class Site:
    key: str
    label: str
    company: str
    host: str
    prefix: str = ""            # path prefix of the job links ("/ey")
    boilerplate_starts: tuple[str, ...] = ()        # lower-case line starts of standard paragraphs that are dropped


def _total(page: str) -> int | None:
    m = TOTAL.search(page)
    return int(re.sub(r"\D", "", m.group(1))) if m and re.search(r"\d", m.group(1)) else None


def _spec_functions(site: Site):
    href_re = re.compile(rf'href="({re.escape(site.prefix)}/job/[^"]+/(\d+)/)"')

    def parse_list(page: str, s: dict) -> list[ListedJob]:
        rows = ROW.findall(page)
        if not rows:
            if _total(page) == 0 or "No jobs" in page:
                return []
            raise PageError(f"the {site.label} job list has no rows and no result line: the page layout changed")
        keep = {c.upper() for c in s["country_codes"]}
        jobs = []
        for row in rows:
            m = LINK.search(row)
            h = href_re.search(m.group(0)) if m else None
            title = re.search(r'class="jobTitle-link"[^>]*>([^<]*)</a>', row)
            if not h or not title:
                raise PageError(f"a row of the {site.label} job list has no job link: the page layout changed")
            loc = LOCATION.search(row)
            place = html.unescape(loc.group(1)).strip() if loc else ""
            codes = {part.strip().upper() for part in place.split(",")[1:]}
            more = bool(loc and loc.group(2))                 # "+1 more": other places are hidden, so do not drop it by country
            if keep and not (codes & keep) and not more:
                continue
            jobs.append(ListedJob(external_id=h.group(2), url=f"https://{site.host}{html.unescape(h.group(1))}",
                                  title=html.unescape(title.group(1)).strip(), locations=[place] if place else []))
        return jobs

    def more_pages(url: str, page: str) -> list[str]:
        rows = ROW.findall(page)
        total = _total(page)
        if rows and total is None:
            raise PageError(f"the {site.label} job list has rows but no total ('of <b>N</b>'): the page layout changed")
        if not rows or total is None or total <= len(rows):
            return []
        parts = urllib.parse.urlsplit(url)
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "startrow"]
        return [urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode([*query, ("startrow", str(n))])))
                for n in range(len(rows), total, len(rows))]

    def parse_detail(page: str) -> JobDetail:
        inner = _span_inner(page, 'itemprop="description"')
        if inner is None:
            raise PageError(f"the {site.label} job page has no description block: the page layout changed")
        lines = [ln for ln in html_to_text(inner).split("\n")
                 if not PHONE.search(ln) and not ln.strip().lower().startswith(site.boilerplate_starts)]   # no recruiter phone numbers
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        if not text:
            raise PageError(f"the {site.label} job page has an empty description")
        return JobDetail(text=text, posted=_posted(page))

    return parse_list, more_pages, parse_detail


def make_spec(site: Site) -> AdapterSpec:
    parse_list, more_pages, parse_detail = _spec_functions(site)
    return AdapterSpec(key=site.key, label=site.label, source=f"{site.key}_site", company=site.company, host=site.host,
                       parse_list=parse_list, parse_detail=parse_detail, more_pages=more_pages,
                       list_filters=("country_codes",), tier="T2", check_robots=True)


SITES = [
    Site("swissre", "Swiss Re career site", "Swiss Re", "careers.swissre.com"),
    Site("ey", "EY career site", "EY", "careers.ey.com", prefix="/ey", boilerplate_starts=(
        "at ey, we’re all in", "we’ll help you succeed in a globally", "join ey and help to build", "ey | building a better working world",
        "ey is building a better working world", "enabled by data, ai and advanced technology", "ey teams work across a full spectrum")),
]
SPECS = [make_spec(s) for s in SITES]
