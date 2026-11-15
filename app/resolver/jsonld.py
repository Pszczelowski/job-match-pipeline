"""The schema.org JobPosting of a career-site page (a <script type="application/ld+json"> block)."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass

from ..ubs_page import html_to_text

_BLOCK = re.compile(r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)


@dataclass
class JobPostingData:
    title: str
    location: str
    description: str                  # plain text


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _is_job_posting(node: dict) -> bool:
    kind = node.get("@type")
    return kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind)


def _location(node: dict) -> str:
    places = node.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    out = []
    for place in places:
        address = place.get("address") if isinstance(place, dict) else None
        if isinstance(address, dict) and address.get("addressLocality"):
            out.append(str(address["addressLocality"]))
    return ", ".join(out)


def extract_job_posting(page_html: str) -> JobPostingData | None:
    """The first JobPosting with a description (also inside @graph or a list), or None."""
    for m in _BLOCK.finditer(page_html or ""):
        try:
            data = json.loads(m.group(1).strip(), strict=False)
        except ValueError:
            continue
        for node in _walk(data):
            if _is_job_posting(node) and isinstance(node.get("description"), str) and node["description"].strip():
                raw = node["description"]
                if "<" not in raw and "&lt;" in raw:                  # HTML that was escaped once more
                    raw = html.unescape(raw)
                return JobPostingData(html.unescape(str(node.get("title") or "")).strip(), _location(node), html_to_text(raw))
    return None
