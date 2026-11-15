"""Greenhouse job boards (Scandit, GetYourGuide, 21Shares), goal.md 5.1 (v0.70.0).

Greenhouse publishes each board through a public JSON interface meant for exactly this reading:
`GET https://boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true` (the list, `{"jobs": [...], "meta": ...}`) and
`GET .../v1/boards/<board>/jobs/<id>` (one job: `content` is HTML-escaped HTML, `first_published` the first posting date).
Tier T1; robots.txt of the host is read first and respected at run time. An empty board is a valid answer (`empty_is_normal`).

A job is kept when one of its offices names a country in `countries` (the last comma part of `offices[].location`, e.g.
"Zürich, Zürich, Switzerland") or the name of its location is one of `places`; both empty keeps all. The job URL is the
`absolute_url` when it is on a host of the board (some companies embed the board in their own site: `?gh_jid=<id>`), else
`https://job-boards.greenhouse.io/<board>/jobs/<id>`. Those hosts are never requested: the job page is read from the API.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass

from ..descriptions_fetch import FetchError
from ..descriptions import normalise_posted
from . import AdapterSpec, JobDetail, ListedJob, PageError, html_to_text

API_HOST = "boards-api.greenhouse.io"
DEFAULT_JOB_HOST = "job-boards.greenhouse.io"


@dataclass(frozen=True)
class Board:
    key: str
    label: str
    company: str
    board: str                  # the board's token in the interface address
    job_hosts: tuple[str, ...] = (DEFAULT_JOB_HOST,)


def _load(label: str, page: str, what: str):
    try:
        return json.loads(page)
    except ValueError:
        raise PageError(f"the {label} {what} is not JSON: the site changed, or it answered with an error or check page") from None


def content_text(data, label: str) -> str:
    """The description of one job answer (`content` is HTML-escaped HTML) as text; shared with the discovery step (v0.73.0)."""
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        raise PageError(f"the {label} job page has no 'content': the site changed")
    text = html_to_text(html.unescape(data["content"])).strip()
    if not text:
        raise PageError(f"the {label} job page has an empty description")
    return text


def _spec_functions(b: Board):
    label = b.label

    def parse_list(page: str, s: dict) -> list[ListedJob]:
        data = _load(label, page, "job list")
        jobs_raw = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs_raw, list):
            raise PageError(f"the {label} job list has no 'jobs' list: the site changed")
        countries = {c.strip().casefold() for c in s.get("countries", [])}
        places = {p.strip().casefold() for p in s.get("places", [])}
        jobs = []
        for j in jobs_raw:
            if not isinstance(j, dict) or not isinstance(j.get("id"), int) or not isinstance(j.get("title"), str) or not j["title"].strip():
                raise PageError(f"a job of the {label} job list has no id and title: the site changed")
            loc = j.get("location")
            name = loc.get("name") if isinstance(loc, dict) else None
            name = name.strip() if isinstance(name, str) else ""
            offices = j.get("offices") if isinstance(j.get("offices"), list) else []
            if countries or places:
                office_countries = {o["location"].rsplit(",", 1)[-1].strip().casefold()
                                    for o in offices if isinstance(o, dict) and isinstance(o.get("location"), str)}
                if not (office_countries & countries) and name.casefold() not in places:
                    continue
            job_id = str(j["id"])
            url = j.get("absolute_url")
            parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
            if not parts or parts.scheme != "https" or parts.netloc not in b.job_hosts:
                url = f"https://{DEFAULT_JOB_HOST}/{b.board}/jobs/{job_id}"
            req = j.get("requisition_id")
            jobs.append(ListedJob(external_id=job_id, url=url, title=j["title"].strip(), locations=[name] if name else [],
                                  reference=req.strip() if isinstance(req, str) else ""))
        return jobs

    def detail_url(url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        m = re.search(r"(?:^|&)gh_jid=(\d+)(?:&|$)", parts.query) or re.search(r"/jobs/(\d+)(?:/|$)", parts.path)
        if parts.netloc not in b.job_hosts or not m:
            raise FetchError(f"{url!r} is not a {label} job page")
        return f"https://{API_HOST}/v1/boards/{b.board}/jobs/{m.group(1)}"

    def parse_detail(page: str) -> JobDetail:
        data = _load(label, page, "job page")
        text = content_text(data, label)
        first = data.get("first_published")
        return JobDetail(text=text, posted=(normalise_posted(first) if isinstance(first, str) else None) or "")

    return parse_list, detail_url, parse_detail


def make_spec(b: Board) -> AdapterSpec:
    parse_list, detail_url, parse_detail = _spec_functions(b)
    return AdapterSpec(key=b.key, label=b.label, source=f"{b.key}_site", company=b.company, host=API_HOST,
                       parse_list=parse_list, parse_detail=parse_detail, detail_url=detail_url,
                       list_filters=("countries", "places"), tier="T1", check_robots=True, job_hosts=b.job_hosts,
                       empty_is_normal=True)


BOARDS = [
    Board("scandit", "Scandit job board", "Scandit", "scandit", job_hosts=("www.scandit.com", DEFAULT_JOB_HOST)),
    Board("getyourguide", "GetYourGuide job board", "GetYourGuide", "getyourguide"),
    Board("shares21", "21Shares job board", "21Shares", "21shares"),
]
SPECS = [make_spec(b) for b in BOARDS]
