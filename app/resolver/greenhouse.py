"""Greenhouse public job boards, for the discovery step (goal.md 5.1a (a), v0.73.0).

`GET https://boards-api.greenhouse.io/v1/boards/<slug>/jobs` lists a board (no `content`: smaller; every posting carries
`company_name` and `location.name`, with `content=true` also `offices[].name`); `GET .../jobs/<id>` is one posting with the
description. A board that does not exist answers HTTP 404 (verified 4 Nov 2026), which `probe` returns as None. The posting
URL is never requested.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..adapters import PageError
from ..adapters.greenhouse import API_HOST, content_text
from ..descriptions_fetch import FetchError
from .common import PlatformError
from .match import Candidate

HOST = API_HOST
COMPANY_KEY = True                 # every posting of a board names its company (`company_name`)


@dataclass
class Posting(Candidate):
    cities: tuple[str, ...] = ()       # every place the posting names (location and offices)


def board_url(slug: str) -> str:
    return f"https://{HOST}/v1/boards/{slug}/jobs"


def detail_url(slug: str, posting_id: str) -> str:
    return f"https://{HOST}/v1/boards/{slug}/jobs/{posting_id}"


def parse_board(text: str) -> list[Posting]:
    try:
        jobs = json.loads(text)["jobs"]
        if not isinstance(jobs, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise PlatformError("the board answer is not the JSON this platform module expects") from None
    found = []
    for j in jobs:
        if not isinstance(j, dict) or not isinstance(j.get("id"), int) or not isinstance(j.get("title"), str):
            raise PlatformError("a posting of the board has no id and title")
        loc = j.get("location")
        places = [loc.get("name")] if isinstance(loc, dict) else []
        places += [o.get("name") for o in j.get("offices") or [] if isinstance(o, dict)]
        cities = tuple(dict.fromkeys(p.strip() for p in places if isinstance(p, str) and p.strip()))
        company = j.get("company_name")
        url = j.get("absolute_url")
        found.append(Posting(str(j["id"]), j["title"].strip(), cities[0] if cities else None, url if isinstance(url, str) else "",
                             None, company.strip() if isinstance(company, str) and company.strip() else None, cities))
    return found


def probe(get, slug: str) -> list[Posting] | None:
    """The postings of the board, or None when there is no such board (HTTP 404). Other errors propagate."""
    try:
        return parse_board(get(board_url(slug)))
    except FetchError as exc:
        if exc.http_status == 404:
            return None
        raise


def detail_text(get, slug: str, posting: Posting) -> str | None:
    """The description of one posting, or None when the answer carries none."""
    try:
        return content_text(json.loads(get(detail_url(slug, posting.posting_id))), "Greenhouse")
    except ValueError:
        raise PlatformError("the posting answer is not JSON") from None
    except PageError:
        return None
