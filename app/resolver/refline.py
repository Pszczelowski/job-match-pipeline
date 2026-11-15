"""Refline career sites (apply.refline.ch), for example the Zuercher Kantonalbank (goal.md 5.1a).

The list page `{base}/search.html` is a table: one row per posting with a link `{base}/<id>/pub/<n>`, the title and
the place. The posting page carries the text as schema.org JobPosting, so there is no search by words: the whole list
is read once per run and matched.
"""
from __future__ import annotations

import html
import re

from .common import PlatformError
from .jsonld import extract_job_posting
from .match import Candidate

_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_LINK = re.compile(r'<a\s[^>]*href="([^"]*/(\d+)/pub/\d+)"[^>]*>(.*?)</a>', re.S | re.I)
_PLACE = re.compile(r'<td[^>]*class="[^"]*workplace[^"]*"[^>]*>(.*?)</td>', re.S | re.I)


def cache_key(title: str) -> str:
    return ""                                              # the same list for every job


def list_page_url(source: dict) -> str:
    return source.get("list_url") or f"{source['base_url'].rstrip('/')}/search.html"


def candidates(get, source: dict, title: str) -> list[Candidate]:
    page = get(list_page_url(source))
    found = []
    for row in _ROW.findall(page):
        link = _LINK.search(row)
        if not link:
            continue
        place = _PLACE.search(row)
        found.append(Candidate(link.group(2), html.unescape(re.sub(r"<[^>]+>", "", link.group(3))).strip(),
                               html.unescape(re.sub(r"<[^>]+>", "", place.group(1))).strip() if place else None,
                               html.unescape(link.group(1))))
    if not found and "<table" not in page.lower():
        raise PlatformError("the list page has no table of postings")
    return found


def detail_text(get, cand: Candidate) -> str | None:
    page = extract_job_posting(get(cand.url))
    return page.description if page else None
