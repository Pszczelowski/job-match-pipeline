"""JobCloud job boards, www.jobs.ch and www.jobup.ch (verified on saved pages of 3 Nov 2026, goal.md 5.1a).

Only job PAGES are read, never the API: the user allowed automated retrieval of pages for personal use, not API access, and
robots.txt disallows /api/ and any `feat=` parameter, so neither is ever requested.

The search page `{base}/en/vacancies/?term=<words>` (jobup.ch: `/fr/emplois/`) is server-rendered: the page assigns a JSON
state to `__INIT__`, and `vacancy.results.main.results[]` holds the first 20 rows with id (uuid), title, place,
company.name and isActive, and `meta.numPages` the number of result pages. The posting page `{base}/<same path>/detail/<uuid>/` carries the text
as schema.org JobPosting (HTML in `description`). robots.txt disallows `detail/*/*/*` (the long slug form), so the short form is
the only one used. JobCloud lists many employers: the source is `any_company` and the resolver keeps only rows of the job's company.

With the job's company (v0.86.0) the first search is "<title words> <company key>", so the postings of that employer come first;
page 2 of the same search (`&page=2`, verified live on 13 Nov 2026: page 2 lists other postings than page 1) is read only when
`meta.numPages` says there is one and page 1 held no good row; then the plain title search follows. At most `MAX_PAGES` pages are
read per job and source; the first page with a good row ends the reading. A posting page that shows the expired notice raises
`Expired`, which the resolver records as `expired` (before v0.87.1: `no_match`) with the reason, not as `partial`.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse

from ..dedup import normalize_company_key
from .common import Expired, PlatformError
from .jibe import _queries
from .jsonld import extract_job_posting
from .match import Candidate, normalise_title

DEFAULT_PATHS = {"jobup": "/fr/emplois/"}
DEFAULT_PATH = "/en/vacancies/"
_INIT = re.compile(r"\b__INIT__\s*=\s*")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
_EXPIRED_KEY = re.compile(r'"vacancy\.detail\.expired\.header"\s*:\s*("(?:[^"\\]|\\.)*")')
_EXPIRED_FALLBACK = ("Dieses Inserat ist abgelaufen",)
_SEVERAL_PLACES = re.compile(r"\s(?:oder|ou|or|und|et|and)\s|[/,;]", re.I)
MAX_PAGES = 3                                                          # pages read per job and source with a company


def cache_key(title: str) -> str:
    return normalise_title(title)


def search_base(source: dict) -> str:
    """The search page (the detail pages are `<it>detail/<uuid>/`), always with a trailing slash: the list_url, else the host's default."""
    if source.get("list_url"):
        return source["list_url"].split("?")[0].rstrip("/") + "/"
    host = urllib.parse.urlsplit(source["base_url"]).netloc
    return source["base_url"].rstrip("/") + next((p for name, p in DEFAULT_PATHS.items() if name in host), DEFAULT_PATH)


def _state(page: str) -> dict:
    m = _INIT.search(page or "")
    try:
        data, _ = json.JSONDecoder().raw_decode(page, m.end())
        rows = data["vacancy"]["results"]["main"]["results"]
        if not isinstance(rows, list):
            raise TypeError
        return data
    except (AttributeError, ValueError, KeyError, TypeError):
        raise PlatformError("the search page has no job list in the form this platform module expects") from None


def _parse(source: dict, page: str) -> list[Candidate]:
    base = search_base(source)
    found = []
    try:
        for row in _state(page)["vacancy"]["results"]["main"]["results"]:
            if row.get("isActive") is False:
                continue
            place = str(row.get("place") or "").strip()
            title = re.sub(r"\s+\|\s+.*$", "", str(row["title"])).strip() or str(row["title"])        # "Title | Place" -> "Title"
            company = (row.get("company") or {}).get("name")
            found.append(Candidate(str(row["id"]), title, None if _SEVERAL_PLACES.search(place) else (place or None),
                                   f"{base}detail/{urllib.parse.quote(str(row['id']), safe='')}/", company=company))
    except (KeyError, TypeError, AttributeError):
        raise PlatformError("a row of the search page lacks id or title") from None
    return found


def _num_pages(page: str) -> int:
    meta = _state(page)["vacancy"]["results"]["main"].get("meta")
    n = meta.get("numPages") if isinstance(meta, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) else 1


def candidates(get, source: dict, title: str, company: str | None = None, good=None) -> list[Candidate]:
    """The rows of the search for a title. With `company` the first search also names it (see the module text); `good(row)`
    says whether a row is the posting wanted, and the first page with a good row ends the reading."""
    base = search_base(source)
    if company is None:
        for words in _queries(title):
            found = _parse(source, get(f"{base}?{urllib.parse.urlencode({'term': words}, quote_via=urllib.parse.quote)}"))
            if found:
                return found
        return []
    name = normalize_company_key(company)
    words = " ".join(w for w in (normalise_title(title), name) if w)
    found: list[Candidate] = []
    read = 0
    if words:
        url = f"{base}?{urllib.parse.urlencode({'term': words}, quote_via=urllib.parse.quote)}"
        page = get(url)
        read += 1
        rows = _parse(source, page)
        found += rows
        if not (good and any(good(c) for c in rows)) and _num_pages(page) >= 2:
            rows = _parse(source, get(f"{url}&page=2"))
            read += 1
            found += rows
        if good and any(good(c) for c in found):
            return found
    for words in _queries(title):
        if read >= MAX_PAGES:
            break
        rows = _parse(source, get(f"{base}?{urllib.parse.urlencode({'term': words}, quote_via=urllib.parse.quote)}"))
        read += 1
        found += rows
        if rows:
            break
    return found


def is_expired(page: str) -> bool:
    """True when the page SHOWS JobCloud's expired notice. The notice text also sits in the translation bundle inside a
    <script> on every page, so only the text outside scripts counts; the page's own language comes from that bundle."""
    shown = html.unescape(re.sub(r"<[^>]+>", " ", _SCRIPT.sub(" ", page or "")))
    shown = re.sub(r"\s+", " ", shown)
    texts = list(_EXPIRED_FALLBACK)
    for m in _EXPIRED_KEY.finditer(page or ""):
        try:
            texts.append(str(json.loads(m.group(1))))
        except ValueError:
            pass
    return any(t.strip() and t.strip() in shown for t in texts)


def detail_text(get, cand: Candidate) -> str | None:
    page = get(cand.url)
    if is_expired(page):
        raise Expired(f"posting {cand.posting_id} shows the expired notice")
    posting = extract_job_posting(page)
    return posting.description if posting else None
