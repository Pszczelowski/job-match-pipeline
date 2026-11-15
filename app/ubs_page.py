"""Reading the text of one UBS job posting from its public page (goal.md 5.1).

The page (https://jobs.ubs.com/1033/asp/tg/cim_jobdetail.asp?...&jobid=N) holds the posting in a JSON block in the
hidden input `preLoadJSON`: a list of fields, each with a `QuestionName` (the heading, for example "Key
responsibilities"), a `VerityZone` (its internal name) and an `AnswerValue` (HTML text). `parse_job_page` turns the
job-specific fields into plain text with their headings. Company boilerplate ("About us", "Join us", disclaimers,
contact details) is left out, so that it cannot trigger the language rule or the scorer.

Fetching these pages is allowed only because the user holds UBS's written permission for personal use (see
`app/descriptions_fetch.py`); this module only parses.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Headings that are the same in every posting.
BOILERPLATE = {"about us", "join us", "disclaimer / policy statements", "your career comeback", "contact details",
               "equal opportunity", "how we hire"}
MIN_FIELD_CHARS = 40


class PageError(ValueError):
    """The page does not contain a readable posting."""


@dataclass
class JobPage:
    job_id: str
    title: str
    sections: list[tuple[str, str]] = field(default_factory=list)      # (heading, text)

    @property
    def text(self) -> str:
        return "\n\n".join(f"{heading}\n{body}" for heading, body in self.sections)


class _InputFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.value: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("id") == "preLoadJSON" and a.get("value"):
            self.value = a["value"]                      # the parser has already unescaped &quot; and friends


def html_to_text(fragment: str) -> str:
    """Posting HTML to plain text: <br> and paragraphs become line breaks, list items get a bullet."""
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", fragment)
    text = re.sub(r"(?i)</\s*(p|div|ul|ol|h\d)\s*>", "\n\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace(" ", " ")
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fields(node):
    if isinstance(node, dict):
        if "VerityZone" in node and "AnswerValue" in node:
            yield node
        for value in node.values():
            yield from _fields(value)
    elif isinstance(node, list):
        for value in node:
            yield from _fields(value)


def parse_job_page(page_html: str) -> JobPage:
    finder = _InputFinder()
    finder.feed(page_html)
    if not finder.value:
        raise PageError("the page has no job data block (preLoadJSON); the page layout may have changed")
    try:
        data = json.loads(finder.value)
    except json.JSONDecodeError as exc:
        raise PageError(f"the job data block is not valid JSON: {exc.msg}") from None
    fields = list(_fields(data))
    by_zone = {f["VerityZone"]: f["AnswerValue"] for f in fields if isinstance(f.get("AnswerValue"), str)}
    job_id = str(by_zone.get("reqid", "")).strip()
    title = by_zone.get("jobtitle", "").strip()
    sections: list[tuple[str, str]] = []
    for f in fields:
        value, heading = f.get("AnswerValue"), (f.get("QuestionName") or "").strip()
        if not isinstance(value, str) or not heading or heading.lower() in BOILERPLATE:
            continue
        body = html_to_text(value)
        if len(body) >= MIN_FIELD_CHARS and (heading, body) not in sections:
            sections.append((heading, body))
    if not sections:
        raise PageError("the page holds no posting text")
    return JobPage(job_id=job_id, title=title, sections=sections)
