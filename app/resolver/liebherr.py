"""Liebherr career site, www.liebherr.com (verified by a scout on 3 Nov 2026, goal.md 5.1a).

The search page `{base}/de-ch/karriere/offene-stellen-3360980?term=<words>` is server-rendered Next.js: the postings are in
`<script id="__NEXT_DATA__">` at props.pageProps.hydrationData.jlpData (`jobs[]` with jobId, title, company.name,
city.label, jobDetailPageUrl; `total`, `pageSize`, `pageIndex`). Further pages: `&p=N` (up to MAX_PAGES). The same jobId can
be listed in several languages; the candidates are deduplicated by jobId. The posting page carries the text as schema.org
JobPosting (HTML in `description`, converted to text by jsonld.py).
"""
from __future__ import annotations

import json
import re
import urllib.parse

from .common import PlatformError
from .jibe import _queries
from .jsonld import extract_job_posting
from .match import Candidate, normalise_title, title_score

MAX_PAGES = 3
SEARCH_PATH = "/de-ch/karriere/offene-stellen-3360980"
_NEXT_DATA = re.compile(r'<script[^>]*\bid\s*=\s*["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)


def cache_key(title: str) -> str:
    return normalise_title(title)


def _jlp(page: str) -> dict:
    m = _NEXT_DATA.search(page or "")
    try:
        data = json.loads(m.group(1))["props"]["pageProps"]["hydrationData"]["jlpData"]
        if not isinstance(data["jobs"], list):
            raise TypeError
        return data
    except (AttributeError, ValueError, KeyError, TypeError):
        raise PlatformError("the search page has no job list in the form this platform module expects") from None


def _absolute(source: dict, url: str) -> str:
    return urllib.parse.urljoin(source["base_url"].rstrip("/") + "/", url)


def _parse(source: dict, jlp: dict) -> list[Candidate]:
    found = []
    try:
        for job in jlp["jobs"]:
            city = (job.get("city") or {}).get("label")
            found.append(Candidate(str(job["jobId"]), str(job["title"]), city, _absolute(source, str(job["jobDetailPageUrl"]))))
    except (KeyError, TypeError, AttributeError):
        raise PlatformError("a job of the search page lacks jobId, title or jobDetailPageUrl") from None
    return found


def _dedupe(found: list[Candidate], title: str) -> list[Candidate]:
    """One candidate per jobId; of several languages the entry whose title is closest to the job's (the first on a tie)."""
    best: dict[str, Candidate] = {}
    for c in found:
        if c.posting_id not in best or title_score(title, c.title) > title_score(title, best[c.posting_id].title):
            best[c.posting_id] = c
    return list(best.values())


def candidates(get, source: dict, title: str) -> list[Candidate]:
    base = source.get("list_url") or f"{source['base_url'].rstrip('/')}{SEARCH_PATH}"
    for words in _queries(title):
        found: list[Candidate] = []
        for page_no in range(1, MAX_PAGES + 1):
            query = {"term": words, **({"p": page_no} if page_no > 1 else {})}
            jlp = _jlp(get(f"{base}?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}"))
            found += _parse(source, jlp)
            if not jlp["jobs"] or len(found) >= int(jlp.get("total") or 0):
                break
        if found:
            return _dedupe(found, title)
    return []


def detail_text(get, cand: Candidate) -> str | None:
    page = extract_job_posting(get(cand.url))
    return page.description if page else None
