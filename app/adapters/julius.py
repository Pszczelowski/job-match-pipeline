"""Julius Baer (Workday career site `juliusbaer.wd3.myworkdayjobs.com`), goal.md 5.1 (v0.51.0).

The user decided on 2 Nov 2026 to read this site, knowing the open point on the Julius Baer terms of use (goal.md 5.1),
so no permission note is required; `adapters.julius.decision_note` only records the decision. The reading is the one
the career site does itself: the page `https://juliusbaer.wd3.myworkdayjobs.com/en-US/External?Location_Country=...` loads
its jobs from the site's JSON interface `/wday/cxs/juliusbaer/External/...`, and this adapter asks that interface the
same questions with an honest User-Agent, slowly. robots.txt of the host allows `/External/` (it disallows only
`/refreshFacet/`, which is never requested).

List: `POST .../wday/cxs/juliusbaer/External/jobs` with the JSON `{"appliedFacets": {"Location_Country": [<facet>]},
"limit": 20, "offset": N, "searchText": ""}`. The answer has `total` (ONLY on the first page: later pages say 0, observed
2 Nov 2026) and `jobPostings` (`title`, `externalPath` = `/job/<place>/<slug>_<id>`, `locationsText`, `bulletFields` with the
requisition number). A posting that has only `bulletFields` (seen once, no title and no path: nothing to open) is skipped.
The job id is the part after the last underscore of the path, lower-cased (`r-20213-1`), and the URL is rebuilt in the
form of the alert-mail parser (`app/parsers/julius.py`), so a job from the mail and from the site is the same job.

Job page: `GET .../wday/cxs/juliusbaer/External/job/<place>/<slug>_<id>` returns `jobPostingInfo` with `jobDescription`
(HTML: a standard Julius Baer introduction, the role text, "YOUR CHALLENGE", "YOUR PROFILE", a standard closing
paragraph), `startDate` (ISO, the posting date) and `jobReqId`. The introduction and the closing paragraphs are dropped.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse

from ..descriptions import normalise_posted
from ..descriptions_fetch import FetchError, JsonPost
from ..parsers.julius import SOURCE as ALERT_SOURCE, job_from_url
from . import AdapterSpec, JobDetail, ListedJob, PageError, html_to_text

log = logging.getLogger("recruitment")

HOST = "juliusbaer.wd3.myworkdayjobs.com"
TENANT, SITE = "juliusbaer", "External"
API = f"https://{HOST}/wday/cxs/{TENANT}/{SITE}"
PAGE_SIZE = 20
SOURCE = "julius_site"
EXTERNAL_PATH = re.compile(r"^/job/[^/]+(?:/[^/]+)?$")
SEVERAL_PLACES = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)      # "2 Locations": the place is not named in the list
REQUISITION = re.compile(r"^r-\d+", re.IGNORECASE)
DETAIL_PATH = re.compile(rf"^/(?:[A-Za-z]{{2}}-[A-Za-z]{{2}}/)?{SITE}(/job/.+)$")
BOILERPLATE_STARTS = ("at julius baer, we celebrate", "we are looking forward to receiving", "is this not quite what you are looking for")


def _load(page: str, what: str) -> dict:
    try:
        data = json.loads(page)
    except ValueError:
        raise PageError(f"the Julius Baer {what} is not JSON: the site changed, or it answered with an error or check page") from None
    if not isinstance(data, dict):
        raise PageError(f"the Julius Baer {what} is not a JSON object: the site changed")
    return data


def _postings(page: str) -> list:
    data = _load(page, "job list")
    postings = data.get("jobPostings")
    if not isinstance(postings, list) or not isinstance(data.get("total"), int) or isinstance(data.get("total"), bool):
        raise PageError("the Julius Baer job list has no 'total' and 'jobPostings': the site changed")
    return postings


def list_requests(url: str, s: dict, pages: list[str]) -> tuple[str, dict] | None:
    """The next list request, from the pages read so far: `limit` 20, `offset` = postings read, up to the `total` of page 1."""
    facet = s["country_facet"].strip()
    body = {"appliedFacets": {"Location_Country": [facet]} if facet else {}, "limit": PAGE_SIZE, "offset": 0, "searchText": ""}
    if not pages:
        return url, JsonPost(body)
    offset = sum(len(_postings(p)) for p in pages)
    total = _load(pages[0], "job list")["total"]             # later pages say 0: only the first page states the total
    if offset >= total:
        return None
    if not _postings(pages[-1]):
        raise PageError(f"the Julius Baer job list ended at {offset} of {total} jobs (an empty page): the site changed")
    return url, JsonPost({**body, "offset": offset})


def parse_list(page: str, s: dict) -> list[ListedJob]:
    postings = _postings(page)
    jobs = []
    for p in postings:
        if not isinstance(p, dict):
            raise PageError("a posting of the Julius Baer job list is not an object: the site changed")
        if not p.get("title") and not p.get("externalPath"):
            log.info("adapter julius: a posting without title and path (%s) is skipped", p.get("bulletFields"))
            continue
        path, title = p.get("externalPath"), p.get("title")
        if not isinstance(path, str) or not isinstance(title, str) or not EXTERNAL_PATH.match(path) or not title.strip():
            raise PageError("a posting of the Julius Baer job list has no usable title and path: the site changed")
        job_id, url = job_from_url(f"https://{HOST}/{SITE}{path}")
        if not job_id:
            raise PageError(f"the path {path!r} of the Julius Baer job list is not a job path: the site changed")
        place = p.get("locationsText")
        place = place.strip() if isinstance(place, str) else ""
        if not place or SEVERAL_PLACES.match(place):             # the first place of the path is the main place of the job
            parts = path.split("/")
            place = urllib.parse.unquote(parts[2]).replace("-", " ") if len(parts) == 4 else ""
        req = REQUISITION.match(job_id)
        jobs.append(ListedJob(external_id=job_id, url=url, title=title.strip(), locations=[place] if place else [],
                              reference=req.group(0).upper() if req else ""))
    if postings and not jobs:
        raise PageError("no posting of the Julius Baer job list has a title and a path: the site changed")
    return jobs


def detail_url(url: str) -> str:
    """The JSON address of the job page whose public address is `url` (`/External/job/...`, a language prefix is dropped)."""
    parts = urllib.parse.urlsplit(url)
    m = DETAIL_PATH.match(parts.path)
    if parts.netloc != HOST or not m:
        raise FetchError(f"{url!r} is not a Julius Baer job page")
    return f"{API}{m.group(1)}"


def parse_detail(page: str) -> JobDetail:
    info = _load(page, "job page").get("jobPostingInfo")
    if not isinstance(info, dict) or not isinstance(info.get("jobDescription"), str):
        raise PageError("the Julius Baer job page has no 'jobPostingInfo.jobDescription': the site changed")
    lines = [ln for ln in html_to_text(info["jobDescription"]).split("\n") if not ln.strip().lower().startswith(BOILERPLATE_STARTS)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not text:
        raise PageError("the Julius Baer job page has an empty description")
    start = info.get("startDate")
    return JobDetail(text=text, posted=(normalise_posted(start) if isinstance(start, str) else None) or "")


SPEC = AdapterSpec(key="julius", label="Julius Baer career site", source=SOURCE, company="Julius Baer", host=HOST,
                   parse_list=parse_list, parse_detail=parse_detail, list_requests=list_requests, detail_url=detail_url,
                   detail_sources=(SOURCE, ALERT_SOURCE))
