"""iCIMS / Jibe career sites, for example careers.axa.com (verified 3 Nov 2026, goal.md 5.1a).

The site's own search answers with JSON: `GET {base}/api/jobs?keywords=<words>&page=1` returns `jobs[].data` with
req_id, slug, title, city, country and the description as text, so the list answer already holds the posting. The job
page `{base}/careers-home/jobs/<id>?lang=en-us` carries the same text as schema.org JobPosting and is the fallback.
"""
from __future__ import annotations

import json
import urllib.parse

from ..ubs_page import html_to_text
from .common import PlatformError
from .jsonld import extract_job_posting
from .match import Candidate, normalise_title


def cache_key(title: str) -> str:
    return normalise_title(title)


def _queries(title: str) -> list[str]:
    words = list(dict.fromkeys(normalise_title(title).split()))        # no word twice
    if not words:
        return []
    queries = [" ".join(words)]
    if len(words) > 3:                                    # a shorter search for a site that wants every word in one posting
        keep = set(sorted(range(len(words)), key=lambda i: -len(words[i]))[:3])
        queries.append(" ".join(w for i, w in enumerate(words) if i in keep))
    return queries


def job_page_url(source: dict, slug: str) -> str:
    return f"{source['base_url'].rstrip('/')}/careers-home/jobs/{urllib.parse.quote(str(slug))}?lang=en-us"


def candidates(get, source: dict, title: str) -> list[Candidate]:
    api = source.get("list_url") or f"{source['base_url'].rstrip('/')}/api/jobs"
    for words in _queries(title):
        answer = get(f"{api}?{urllib.parse.urlencode({'keywords': words, 'page': 1})}")
        try:
            jobs = json.loads(answer)["jobs"]
            found = []
            for job in jobs:
                data = job["data"]
                text = data.get("description") or ""
                found.append(Candidate(str(data.get("req_id") or data["slug"]), str(data["title"]), data.get("city"),
                                       job_page_url(source, data.get("slug") or data["req_id"]),
                                       html_to_text(text) if "<" in text else text.strip()))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PlatformError("the job list is not the JSON this platform module expects") from None
        if found:
            return found
    return []


def detail_text(get, cand: Candidate) -> str | None:
    page = extract_job_posting(get(cand.url))
    return page.description if page else None
