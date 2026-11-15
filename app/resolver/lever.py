"""Lever public job boards, for the discovery step (goal.md 5.1a (a), v0.76.0).

`GET https://api.lever.co/v0/postings/<board>?mode=json` lists the published postings of a board as a JSON list with the full
description in the same answer (`description`, `lists`, `additional`), so a posting needs no second request. The answer does not
name the company: the board name is the only evidence, and discovery therefore asks Lever only with names built from the whole
company name (`COMPANY_KEY` is False). A board that does not exist answers HTTP 404 (checked 4 Nov 2026 with a made-up name).
The posting URL (`hostedUrl`) is never requested. robots.txt of the host (4 Nov 2026): "User-agent: *, Allow: /, Crawl-delay: 1";
the session sleeps at least the crawl-delay between requests. The EU host (api.eu.lever.co) is not asked.
"""
from __future__ import annotations

import json

from ..descriptions_fetch import FetchError
from ..ubs_page import html_to_text
from .common import PlatformError
from .greenhouse import Posting

HOST = "api.lever.co"
COMPANY_KEY = False                    # the postings carry no company name


def board_url(slug: str) -> str:
    return f"https://{HOST}/v0/postings/{slug}?mode=json"


def _places(j: dict) -> tuple[str, ...]:
    cats = j.get("categories")
    cats = cats if isinstance(cats, dict) else {}
    places = [cats.get("location")]
    all_loc = cats.get("allLocations")
    places += all_loc if isinstance(all_loc, list) else []
    return tuple(dict.fromkeys(p.strip() for p in places if isinstance(p, str) and p.strip()))


def _html(value) -> str:
    return html_to_text(value) if isinstance(value, str) and value.strip() else ""


def _body(j: dict) -> str | None:
    parts = [_html(j.get("description")) or (j.get("descriptionPlain") or "")]
    for item in j.get("lists") or []:
        if isinstance(item, dict):
            parts.append("\n".join(x for x in (str(item.get("text") or "").strip(), _html(item.get("content"))) if x))
    parts.append(_html(j.get("additional")) or (j.get("additionalPlain") or ""))
    body = "\n\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return body or None


def parse_board(text: str) -> list[Posting]:
    try:
        jobs = json.loads(text)
        if not isinstance(jobs, list):
            raise TypeError
    except (ValueError, TypeError):
        raise PlatformError("the board answer is not the JSON this platform module expects") from None
    found = []
    for j in jobs:
        if not isinstance(j, dict) or not isinstance(j.get("id"), str) or not isinstance(j.get("text"), str):
            raise PlatformError("a posting of the board has no id and title")
        url = j.get("hostedUrl")
        cities = _places(j)
        found.append(Posting(j["id"], j["text"].strip(), cities[0] if cities else None, url if isinstance(url, str) else "",
                             _body(j), None, cities))
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
    """The description is already in the list answer: no request."""
    return posting.text
