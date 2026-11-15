"""Ashby public job boards, for the discovery step (goal.md 5.1a (a), v0.75.0).

`GET https://api.ashbyhq.com/posting-api/job-board/<board>` lists the listed postings of a board with the full description in
the same answer (`descriptionHtml`, `descriptionPlain`), so a posting needs no second request. The answer does not name the
company: the board name is the only evidence, and discovery therefore asks Ashby only with names built from the whole company
name (`COMPANY_KEY` is False). A board that does not exist answers HTTP 404 (assumed from the documentation, not yet checked
against the live interface; the network test in tests/test_discovery_network.py checks it), which `probe` returns as None.
The posting URL (`jobUrl`) is never requested. robots.txt of the host answered 401 on 3 Nov 2026, which counts as "no rules"
(RFC 9309 2.3.1.3, `resolver/robots.py`).
"""
from __future__ import annotations

import json

from ..descriptions_fetch import FetchError
from ..ubs_page import html_to_text
from .common import PlatformError
from .greenhouse import Posting

HOST = "api.ashbyhq.com"
COMPANY_KEY = False                    # the postings carry no company name


def board_url(slug: str) -> str:
    return f"https://{HOST}/posting-api/job-board/{slug}"


def _places(j: dict) -> tuple[str, ...]:
    places = [j.get("location")]
    places += [s.get("location") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
    address = j.get("address")
    postal = address.get("postalAddress") if isinstance(address, dict) else None
    places.append(postal.get("addressLocality") if isinstance(postal, dict) else None)
    return tuple(dict.fromkeys(p.strip() for p in places if isinstance(p, str) and p.strip()))


def parse_board(text: str) -> list[Posting]:
    try:
        jobs = json.loads(text)["jobs"]
        if not isinstance(jobs, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise PlatformError("the board answer is not the JSON this platform module expects") from None
    found = []
    for j in jobs:
        if not isinstance(j, dict) or not isinstance(j.get("id"), str) or not isinstance(j.get("title"), str):
            raise PlatformError("a posting of the board has no id and title")
        if j.get("isListed") is False:
            continue
        body = j.get("descriptionHtml")
        body = html_to_text(body) if isinstance(body, str) and body.strip() else j.get("descriptionPlain")
        url = j.get("jobUrl")
        cities = _places(j)
        found.append(Posting(j["id"], j["title"].strip(), cities[0] if cities else None, url if isinstance(url, str) else "",
                             body.strip() if isinstance(body, str) else None, None, cities))
    return found


def probe(get, slug: str) -> list[Posting] | None:
    """The listed postings of the board, or None when there is no such board (HTTP 404). Other errors propagate."""
    try:
        return parse_board(get(board_url(slug)))
    except FetchError as exc:
        if exc.http_status == 404:
            return None
        raise


def detail_text(get, slug: str, posting: Posting) -> str | None:
    """The description is already in the list answer: no request."""
    return posting.text
