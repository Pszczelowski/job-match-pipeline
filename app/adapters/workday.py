"""Workday career sites (Swisscom, Vontobel, Temenos; Hitachi Energy, Novartis, Deutsche Bank), goal.md 5.1 (v0.70.0).

The same reading as `julius.py` (a Workday site loads its jobs from the JSON interface `/wday/cxs/<tenant>/<site>/...`; this
adapter asks that interface the same questions with an honest User-Agent, slowly), generalised to several tenants. Tier T1
(a public interface the site itself uses); robots.txt of the host is read first and respected at run time.

List: `POST https://<host>/wday/cxs/<tenant>/<site>/jobs` with `{"appliedFacets": <facets>, "limit": 20, "offset": N,
"searchText": ""}`; `total` is stated on the first page only. `adapters.<key>.facets` may hold a country facet (Vontobel); a
tenant without one (Swisscom, Temenos) is read completely and its postings are kept by `countries` and `places` (the text of
the posting's location, or the main place of its path when the list says "N Locations"); both empty keeps all.
Job page: `GET .../wday/cxs/<tenant>/<site>/job/<place>/<slug>_<id>`: `jobPostingInfo.jobDescription` (HTML), `startDate`.
The job id is the part of the path after the last underscore, lower-cased; the job URL is `https://<host>/<site><externalPath>`.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass

from ..descriptions import normalise_posted
from ..descriptions_fetch import FetchError, JsonPost
from . import AdapterSpec, JobDetail, ListedJob, PageError, html_to_text

log = logging.getLogger("recruitment")

PAGE_SIZE = 20
EXTERNAL_PATH = re.compile(r"^/job/[^/]+(?:/[^/]+)?$")
SEVERAL_PLACES = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)      # "2 Locations": the place is not named in the list
REFERENCE = re.compile(r"^[A-Za-z]{0,3}-?\d+$")


@dataclass(frozen=True)
class Tenant:
    key: str
    label: str                  # "Swisscom career site"
    company: str
    host: str
    tenant: str
    site: str
    boilerplate_starts: tuple[str, ...] = ()        # lower-case line starts of standard paragraphs that are dropped
    empty_is_normal: bool = False       # the tenant may have no vacancy that passes the country filter: no "zero jobs" alert


def _load(t: Tenant, page: str, what: str) -> dict:
    try:
        data = json.loads(page)
    except ValueError:
        raise PageError(f"the {t.label} {what} is not JSON: the site changed, or it answered with an error or check page") from None
    if not isinstance(data, dict):
        raise PageError(f"the {t.label} {what} is not a JSON object: the site changed")
    return data


def _postings(t: Tenant, page: str) -> list:
    data = _load(t, page, "job list")
    postings = data.get("jobPostings")
    if not isinstance(postings, list) or not isinstance(data.get("total"), int) or isinstance(data.get("total"), bool):
        raise PageError(f"the {t.label} job list has no 'total' and 'jobPostings': the site changed")
    return postings


def _key(text: str) -> str:
    """Letters and digits only, ASCII only, lower case: "S. Antonino", "S-Antonino" and "Zürich", "Zrich" (Workday's path drops
    umlauts) compare equal."""
    return "".join(c for c in text.casefold() if c.isascii() and c.isalnum())


def _is_swiss(text: str, s: dict, from_path: bool = False) -> bool:
    """Does the location text name a kept country or place? Both lists empty keeps everything.

    The text is cut at "," and "/"; the place taken from a path (hyphens instead of spaces) is also tested part by part
    (`Zurich-Switzerland`) and as a whole (`St-Gallen`).
    """
    wanted = {_key(x) for x in [*s.get("countries", []), *s.get("places", [])]} - {""}
    if not wanted:
        return True
    candidates = [*re.split(r"[,/]", text), text]
    if from_path:
        candidates += [part for c in candidates for part in c.split("-")]
    return any(_key(c) in wanted for c in candidates if _key(c))


def _spec_functions(t: Tenant):
    api = f"https://{t.host}/wday/cxs/{t.tenant}/{t.site}"
    detail_path = re.compile(rf"^/(?:[A-Za-z]{{2}}-[A-Za-z]{{2}}/)?{re.escape(t.site)}(/job/.+)$")

    def list_requests(url: str, s: dict, pages: list[str]) -> tuple[str, dict] | None:
        body = {"appliedFacets": s["facets"], "limit": PAGE_SIZE, "offset": 0, "searchText": ""}
        if not pages:
            return url, JsonPost(body)
        offset = sum(len(_postings(t, p)) for p in pages)
        total = _load(t, pages[0], "job list")["total"]          # later pages say 0: only the first page states the total
        if offset >= total:
            return None
        if not _postings(t, pages[-1]):
            raise PageError(f"the {t.label} job list ended at {offset} of {total} jobs (an empty page): the site changed")
        return url, JsonPost({**body, "offset": offset})

    def parse_list(page: str, s: dict) -> list[ListedJob]:
        postings = _postings(t, page)
        jobs = []
        for p in postings:
            if not isinstance(p, dict):
                raise PageError(f"a posting of the {t.label} job list is not an object: the site changed")
            if not p.get("title") and not p.get("externalPath"):
                log.info("adapter %s: a posting without title and path (%s) is skipped", t.key, p.get("bulletFields"))
                continue
            path, title = p.get("externalPath"), p.get("title")
            if not isinstance(path, str) or not isinstance(title, str) or not EXTERNAL_PATH.match(path) or not title.strip():
                raise PageError(f"a posting of the {t.label} job list has no usable title and path: the site changed")
            slug = path.rsplit("/", 1)[-1]
            if "_" not in slug or not slug.rsplit("_", 1)[1]:
                raise PageError(f"the path {path!r} of the {t.label} job list is not a job path: the site changed")
            job_id = slug.rsplit("_", 1)[1].lower()
            place = p.get("locationsText")
            place = place.strip() if isinstance(place, str) else ""
            from_path = not place or bool(SEVERAL_PLACES.match(place))
            match_text = place
            if from_path:                                     # the first place of the path is the main place of the job
                parts = path.split("/")
                match_text = urllib.parse.unquote(parts[2]).split("--")[0] if len(parts) == 4 else ""
                place = match_text.replace("-", " ")
            if not _is_swiss(match_text, s, from_path):
                continue
            bullets = p.get("bulletFields")
            ref = bullets[0] if isinstance(bullets, list) and bullets and isinstance(bullets[0], str) else ""
            jobs.append(ListedJob(external_id=job_id, url=f"https://{t.host}/{t.site}{path}", title=title.strip(),
                                  locations=[place] if place else [], reference=ref if REFERENCE.match(ref) else ""))
        if postings and not any(isinstance(p, dict) and (p.get("title") or p.get("externalPath")) for p in postings):
            raise PageError(f"no posting of the {t.label} job list has a title and a path: the site changed")
        return jobs

    def detail_url(url: str) -> str:
        """The JSON address of the job page whose public address is `url` (`/<site>/job/...`, a language prefix is dropped)."""
        parts = urllib.parse.urlsplit(url)
        m = detail_path.match(parts.path)
        if parts.netloc != t.host or not m:
            raise FetchError(f"{url!r} is not a {t.label} job page")
        return f"{api}{m.group(1)}"

    def parse_detail(page: str) -> JobDetail:
        info = _load(t, page, "job page").get("jobPostingInfo")
        if not isinstance(info, dict) or not isinstance(info.get("jobDescription"), str):
            raise PageError(f"the {t.label} job page has no 'jobPostingInfo.jobDescription': the site changed")
        lines = [ln for ln in html_to_text(info["jobDescription"]).split("\n") if not ln.strip().lower().startswith(t.boilerplate_starts)]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        if not text:
            raise PageError(f"the {t.label} job page has an empty description")
        start = info.get("startDate")
        return JobDetail(text=text, posted=(normalise_posted(start) if isinstance(start, str) else None) or "")

    return list_requests, parse_list, detail_url, parse_detail


def make_spec(t: Tenant) -> AdapterSpec:
    list_requests, parse_list, detail_url, parse_detail = _spec_functions(t)
    return AdapterSpec(key=t.key, label=t.label, source=f"{t.key}_site", company=t.company, host=t.host,
                       parse_list=parse_list, parse_detail=parse_detail, list_requests=list_requests, detail_url=detail_url,
                       list_filters=("countries", "places"), tier="T1", check_robots=True,
                       empty_is_normal=t.empty_is_normal)


TENANTS = [
    Tenant("swisscom", "Swisscom career site", "Swisscom", "swisscom.wd103.myworkdayjobs.com", "swisscom", "SwisscomExternalCareers",
           boilerplate_starts=("to all recruitment agencies",)),
    Tenant("vontobel", "Vontobel career site", "Vontobel", "vontobel.wd3.myworkdayjobs.com", "vontobel", "Vontobel_External_Career"),
    Tenant("temenos", "Temenos career site", "Temenos", "temenos.wd103.myworkdayjobs.com", "temenos", "Temenoscareers",
           empty_is_normal=True),      # no Swiss location among its 22 on 4 Nov 2026
    Tenant("hitachienergy", "Hitachi Energy career site", "Hitachi Energy", "hitachi.wd1.myworkdayjobs.com", "hitachi", "hitachi"),
    Tenant("novartis", "Novartis career site", "Novartis", "novartis.wd3.myworkdayjobs.com", "novartis", "Novartis_Careers"),
    Tenant("deutschebank", "Deutsche Bank career site", "Deutsche Bank", "db.wd3.myworkdayjobs.com", "db", "DBWebsite"),
]
SPECS = [make_spec(t) for t in TENANTS]
