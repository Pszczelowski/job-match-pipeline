"""SIX Group (jobs.six-group.com, a SAP SuccessFactors career site), goal.md 5.1 and reports/phase-2-research.md.

The list page holds every open job in one table (`tr.data-row`): title link `/job/<slug>/<id>/`, location "City, CC".
A job page carries the posting in a schema.org JobPosting block: `itemprop="description"` (HTML), a `datePosted` meta.
"""
from __future__ import annotations

import html
import re

from ..descriptions import normalise_posted
from . import AdapterSpec, JobDetail, ListedJob, PageError, html_to_text

HOST = "jobs.six-group.com"
ROW = re.compile(r'<tr class="data-row">(.*?)</tr>', re.S)
LINK = re.compile(r'<a\s[^>]*class="jobTitle-link"[^>]*>', re.S)
HREF = re.compile(r'href="(/job/[^"]+/(\d+)/)"')
LOCATION = re.compile(r'class="colLocation[^"]*"[^>]*>\s*<span class="jobLocation">\s*([^<]*?)\s*(<small[^>]*>[^<]*</small>)?\s*</span>', re.S)
PLACE = re.compile(r'<meta itemprop="streetAddress" content="([^"]+)"')
PHONE = re.compile(r"\+\d[\d ()/.-]{7,}")
BOILERPLATE_STARTS = ("diversity is important", "for this vacancy we only accept")


def parse_list(page: str, s: dict) -> list[ListedJob]:
    rows = ROW.findall(page)
    if not rows:
        if re.search(r"Results\s+1\s+(?:–|-|to)\s+\d+", page) or "No jobs" in page:
            return []
        raise PageError("the SIX job list has no rows and no result line: the page layout changed")
    keep = {c.upper() for c in s["country_codes"]}
    jobs = []
    for row in rows:
        m = LINK.search(row)
        h = HREF.search(m.group(0)) if m else None
        title = re.search(r'class="jobTitle-link"[^>]*>([^<]*)</a>', row)
        if not h or not title:
            raise PageError("a row of the SIX job list has no job link: the page layout changed")
        loc = LOCATION.search(row)
        place = html.unescape(loc.group(1)).strip() if loc else ""
        country = place.rsplit(",", 1)[-1].strip().upper() if "," in place else ""
        more = bool(loc and loc.group(2))                 # "+1 more": other places are hidden, so do not drop it by country
        if keep and country not in keep and not more:
            continue
        jobs.append(ListedJob(external_id=h.group(2), url=f"https://{HOST}{h.group(1)}", title=html.unescape(title.group(1)).strip(),
                              locations=[place] if place else [], more_places=more))
    return jobs


def _span_inner(page: str, marker: str) -> str | None:
    i = page.find(marker)
    if i < 0:
        return None
    start = page.rfind("<span", 0, i)
    depth = 0
    for m in re.finditer(r"<(/?)span\b[^>]*>", page[start:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return page[start:start + m.end()]
    return None


def _posted(page: str) -> str:
    m = re.search(r'<meta itemprop="datePosted" content="([^"]+)"', page)
    if not m:
        return ""
    return normalise_posted(m.group(1)) or ""


def _places(page: str) -> list[str]:
    """Every place of the job page's jobLocation block, in order, without duplicates (empty if the block is missing)."""
    block = _span_inner(page, 'itemprop="jobLocation"')
    if block is None:
        return []
    return list(dict.fromkeys(p for p in (html.unescape(m).strip() for m in PLACE.findall(block)) if p))


def parse_detail(page: str) -> JobDetail:
    inner = _span_inner(page, 'itemprop="description"')
    if inner is None:
        raise PageError("the SIX job page has no description block: the page layout changed")
    lines = [ln for ln in html_to_text(inner).split("\n")
             if not PHONE.search(ln) and not ln.strip().lower().startswith(BOILERPLATE_STARTS)]   # no recruiter phone numbers
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return JobDetail(text=text, posted=_posted(page), locations=_places(page))


SPEC = AdapterSpec(key="six", label="SIX career site", source="six_site", company="SIX", host=HOST,
                   parse_list=parse_list, parse_detail=parse_detail,
                   list_filters=("country_codes",))
