"""LGT (www.lgt.com, its own CMS), goal.md 5.1 and reports/phase-2-research.md.

The list (`/ch-en/career/jobs/48662!jobSearch?pageNum=N`) is server-rendered HTML: 10 teasers per page, each with a title
link `/ch-en/career/jobs/<slug>-<id>`, location items and tags. It covers all LGT locations, so `places` keeps the ones
worth checking (the travel-time rule decides the rest). Never the site search: robots.txt excludes `/*/search*`.
A job page has sections `<h2 id="<job id>-jobdescription|jobrequirements|...">`; only the job-specific ones are text
(the rest is company introduction, benefits and contact). The page also has a posting date (`<time datetime=...>`).
"""
from __future__ import annotations

import html
import re

from ..descriptions import normalise_posted
from . import AdapterSpec, JobDetail, ListedJob, PageError, html_to_text

HOST = "www.lgt.com"
LINK = re.compile(r'<a class="lgt-link lgt-teaser__title-link" href="(/[^"]*?-(\d+))"[^>]*>\s*<h3 class="lgt-teaser__title">([^<]*)</h3>', re.S)
LOCATIONS = re.compile(r'<ul class="lgt-teaser__location">(.*?)</ul>', re.S)
ITEM = re.compile(r"<li[^>]*>\s*([^<]*?)\s*</li>", re.S)
SECTION = re.compile(r'<h2 class="lgt-intro__title"[^>]*id="\d+-([a-z-]+)">\s*<span class="lgt-intro__title-block">([^<]*)</span>', re.S)
JOB_SECTIONS = {"jobdescription", "jobrequirements"}              # the job-specific sections (not intro, benefits, contact)
TOTAL_PAGES = re.compile(r"pageNum=(\d+)")


def parse_list(page: str, s: dict) -> list[ListedJob]:
    teasers = [t for t in page.split('<div class="lgt-teaser lgt-teaser--job"')[1:]]
    if not teasers:
        if "lgt-search-result" in page:
            return []                                                   # a result page with no jobs (also an empty last page)
        raise PageError("the LGT job list has no teasers and no result block: the page layout changed")
    keep = {p.casefold() for p in s["places"]}
    jobs = []
    for t in teasers:
        link = LINK.search(t)
        if not link:
            raise PageError("a teaser of the LGT job list has no job link: the page layout changed")
        loc = LOCATIONS.search(t)
        places = [html.unescape(x).strip() for x in ITEM.findall(loc.group(1))] if loc else []
        if keep and not any(p.casefold() in keep for p in places):
            continue
        jobs.append(ListedJob(external_id=link.group(2), url=f"https://{HOST}{html.unescape(link.group(1))}",
                              title=html.unescape(link.group(3)).strip(), locations=places))
    return jobs


def more_pages(url: str, first: str) -> list[str]:
    """The other list pages, from the pagination links of the first one."""
    numbers = sorted({int(n) for n in TOTAL_PAGES.findall(first)})
    base = url.split("?")[0]
    return [f"{base}?pageNum={n}" for n in numbers if n >= 2]


def _section_body(page: str, start: int) -> str:
    """The `lgt-rte` block that follows a section heading, up to its balanced closing tag."""
    i = page.find('<div class="lgt-rte', start)
    if i < 0:
        return ""
    depth = 0
    for m in re.finditer(r"<(/?)div\b[^>]*>", page[i:]):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return page[i:i + m.end()]
    return ""


def parse_detail(page: str) -> JobDetail:
    parts = []
    headings = list(SECTION.finditer(page))
    for n, m in enumerate(headings):
        if m.group(1) not in JOB_SECTIONS:
            continue
        end = headings[n + 1].start() if n + 1 < len(headings) else len(page)
        body = html_to_text(_section_body(page[:end], m.end())).strip()
        if body:
            parts.append(f"{html.unescape(m.group(2)).strip()}\n{body}")
    if not parts:
        raise PageError("the LGT job page has no job description section: the page layout changed")
    posted = re.search(r'<time datetime="([^"]*)"', page)
    return JobDetail(text="\n\n".join(parts), posted=(normalise_posted(posted.group(1)) if posted else None) or "")


SPEC = AdapterSpec(key="lgt", label="LGT career site", source="lgt_site", company="LGT", host=HOST,
                   parse_list=parse_list, parse_detail=parse_detail, more_pages=more_pages,
                   list_filters=("places",))
