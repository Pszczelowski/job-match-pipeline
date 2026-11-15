"""Sygnum (Salesforce Sites portal with the FinancialForce recruiting app), goal.md 5.1 and reports/phase-2-research.md.

Sygnum confirmed to the user (31 Oct 2026) that reading its job portal is OK; the host's robots.txt is the generic Salesforce
one and gives no permission to other robots, so this adapter needs `adapters.sygnum.permission_note` and is off until then.

The list (`/recruit/fRecruit__ApplyJobList`) shows 10 vacancies per page in a table (`tr.dataRow`: title link
`fRecruit__ApplyJob?vacancyNo=VNnnn`, country, city, ...) and is paged by a form post, as a browser's "Next" link does it: the
hidden form fields (view state included) plus the field named in the link. A job page is a table of label/value rows; the
company introduction, values and offer rows are dropped, the rest (about the role, ideal candidate) is the text.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from . import AdapterSpec, JobDetail, ListedJob, PageError, html_to_text

HOST = "sygnumpeopleportal.my.salesforce-sites.com"
ROW = re.compile(r'<tr class="dataRow[^"]*"[^>]*>(.*?)</tr>', re.S)
VACANCY = re.compile(r'href="(/recruit/fRecruit__ApplyJob\?vacancyNo=(\w+))"[^>]*>([^<]*)</a>')
CELL = re.compile(r'<td class="dataCell[^"]*"[^>]*>(.*?)</td>', re.S)
PAGE_OF = re.compile(r"Page (\d+) of (\d+)")
NEXT = re.compile(r"jsfcljs\(document\.getElementById\('[^']+'\),'([^']+)','[^']*'\)[;}]*return false\"[^>]*>Next<")
FORM_ACTION = re.compile(r'<form[^>]*method="post"[^>]*action="([^"]+)"')
LABEL_ROW = re.compile(r'<th class="labelCol[^"]*" scope="row"><label>\s*([^<]*?)\s*</label></th>\s*<td class="data2Col[^"]*">(.*?)</td></tr>', re.S)
SKIP_LABELS = {"posting title", "country", "location city", "about sygnum", "our values", "our offer", "employment type",
               "duration", "working arrangement"}


def _plain(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_list(page: str, s: dict) -> list[ListedJob]:
    rows = ROW.findall(page)
    if not rows:
        if "Current Vacancies" in page:
            return []
        raise PageError("the Sygnum vacancy list has no rows and no heading: the page layout changed")
    keep = {c.casefold() for c in s["countries"]}
    jobs = []
    for row in rows:
        m = VACANCY.search(row)
        if not m:
            raise PageError("a row of the Sygnum vacancy list has no vacancy link: the page layout changed")
        cells = [_plain(c) for c in CELL.findall(row)]
        country, city = (cells[1], cells[2]) if len(cells) >= 3 else ("", "")
        if keep and country.casefold() not in keep:
            continue
        jobs.append(ListedJob(external_id=m.group(2), url=f"https://{HOST}{html.unescape(m.group(1))}",
                              title=html.unescape(m.group(3)).strip(), locations=[city] if city else []))
    return jobs


class _Hidden(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        name = a.get("name") or ""
        if tag == "input" and a.get("type") == "hidden" and (name.startswith("com.salesforce.visualforce.") or name.startswith("j_id0")):
            self.fields[name] = a.get("value") or ""


def next_page(url: str, page: str) -> tuple[str, dict] | None:
    """The form post that the "Next" link of the list performs; None on the last page."""
    pos = PAGE_OF.search(page)
    link, action = NEXT.search(page), FORM_ACTION.search(page)
    if not pos or int(pos.group(1)) >= int(pos.group(2)):
        return None
    if not link or not action:
        raise PageError("the Sygnum vacancy list has a page counter but no Next link: the page layout changed")
    form = _Hidden()
    form.feed(page)
    name, _, value = link.group(1).partition(",")
    if not form.fields or "com.salesforce.visualforce.ViewState" not in form.fields:
        raise PageError("the Sygnum vacancy list has no view state: the page layout changed")
    return f"https://{HOST}{html.unescape(action.group(1))}", {**form.fields, name: value}


def parse_detail(page: str) -> JobDetail:
    parts = []
    for label, body in LABEL_ROW.findall(page):
        if label.strip().casefold() in SKIP_LABELS:
            continue
        text = html_to_text(body).strip()
        if text:
            parts.append(f"{html.unescape(label).strip()}\n{text}")
    if not parts:
        raise PageError("the Sygnum job page has no description rows: the page layout changed")
    return JobDetail(text="\n\n".join(parts))


SPEC = AdapterSpec(key="sygnum", label="Sygnum career site", source="sygnum_site", company="Sygnum", host=HOST,
                   parse_list=parse_list, parse_detail=parse_detail, next_page=next_page, note_required=True,
                   list_filters=("countries",))
