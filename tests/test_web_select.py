"""The selection on the 8 list pages, v0.97.0 (goal.md 5.7): `script-src 'self'` and the select.js tag on exactly those pages, the swipe data, the
confirmation sheet and the labels in the markup. Every test builds its own temporary database; none uses the real config, the real database or the network."""
import re
import shutil
import subprocess

import pytest

from app import statusflow, web
from tests.test_web import client, conn, cfg                                                      # noqa: F401  (fixtures and helpers)
from tests.test_report import job, pipeline
from tests.test_web_bulk import bulk, dbfile, flash_of                                            # noqa: F401
from tests.test_web_stages import put
from tests.test_web_writes import status_of

LISTED = ["/", "/jobs", "/missing", "/applied", "/interviews", "/offers", "/accepted", "/rejected", "/withdrawn"]        # written out: a widened SCRIPT_PATHS must fail
PAGE_OF = {"/": "top", "/jobs": "list", "/missing": "missing", "/applied": "applied", "/interviews": "interviews", "/offers": "offers",
           "/accepted": "accepted", "/rejected": "rejected", "/withdrawn": "withdrawn"}
TAG = '<script src="/static/select.js" defer></script>'


@pytest.fixture
def world(cfg, conn, tmp_path):
    """Nine jobs: 1 on Top 10 and the list, 2 and 3 waiting for a description, then one each in applied, interview, offer, accepted, rejected, withdrawn."""
    cfg["paths"]["lock"] = str(tmp_path / "run.lock")
    cfg["scoring"]["top_n"] = 3
    jobs = [job(1), job(2, description=""), job(3, description=""), job(4), job(5), job(6), job(7), job(8), job(9)]
    ids, _ = pipeline(conn, cfg, jobs, [60, 90, 85, 70, 65, 55, 45, 40, 35])
    for jid, status in zip(ids[3:], ("applied", "interview", "offer", "accepted", "rejected", "withdrawn")):
        put(conn, [jid], status)
    return client(cfg, tmp_path), ids


def csp(c, path):
    return c.get(path).headers["content-security-policy"]


def test_the_script_paths_are_exactly_the_eight_list_pages():
    assert sorted(web.SCRIPT_PATHS) == sorted(LISTED)


@pytest.mark.parametrize("path", LISTED)
def test_the_eight_list_pages_allow_scripts_from_this_app_only(world, path):
    c, _ = world
    policy = csp(c, path)
    assert "script-src 'self'" in policy and "script-src 'none'" not in policy and "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert "default-src 'self'" in policy and "base-uri 'none'" in policy and "form-action 'self'" in policy and "frame-ancestors 'none'" in policy


JOB_PAGE_CSP = "default-src 'self'; script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"       # v0.99.0: confirm.js on the job page only


def test_the_job_page_allows_scripts_from_this_app_only_and_changes_nothing_else(world):
    c, ids = world
    assert csp(c, f"/jobs/{ids[0]}") == JOB_PAGE_CSP
    assert csp(c, f"/jobs/{ids[0]}") == web.HEADERS["Content-Security-Policy"].replace("script-src 'none'", "script-src 'self'")


def test_every_other_response_keeps_script_src_none(world):
    c, ids = world
    for path in (f"/jobs/{ids[0]}/reject", f"/jobs/{ids[5]}/undo-status", f"/bulk-status?action=shortlist&id={ids[0]}&next=list",
                 f"/jobs/{ids[0]}/interviews/1/delete", f"/jobs/{ids[0]}/interviews/1/edit", f"/jobs/{ids[0]}/notes/1/delete",
                 "/static/style.css", "/static/select.js", "/static/confirm.js", "/nope", "/jobs/99999"):
        r = c.get(path)
        assert "script-src 'none'" in r.headers["content-security-policy"] and "script-src 'self'" not in r.headers["content-security-policy"], path
        assert "unsafe-inline" not in r.headers["content-security-policy"] and "unsafe-eval" not in r.headers["content-security-policy"], path


@pytest.mark.parametrize("path", LISTED)
def test_the_select_js_tag_is_the_only_script_of_a_list_page(world, path):
    c, _ = world
    text = c.get(path).text
    assert re.findall(r"<script[^>]*>", text.lower()) == [TAG[:-9]] and text.count(TAG) == 1


def test_other_pages_have_no_script_tag(world):
    c, ids = world
    for path in (f"/jobs/{ids[0]}/reject", f"/jobs/{ids[5]}/undo-status", f"/jobs/{ids[0]}/undo-approval",
                 f"/bulk-status?action=shortlist&id={ids[0]}&next=list", "/login"):
        assert "<script" not in c.get(path).text.lower(), path
    assert re.findall(r"<script[^>]*>", c.get(f"/jobs/{ids[0]}").text.lower()) == ['<script src="/static/confirm.js" defer>']      # the job page: confirm.js only


@pytest.mark.parametrize("path", LISTED)
def test_no_inline_style_and_no_event_handler_on_a_list_page(world, path):
    c, _ = world
    text = c.get(path).text
    assert "style=" not in text.lower() and not re.search(r"\son[a-z]+=", text.lower()) and "|safe" not in text


# --- the sheet and the data select.js reads ---------------------------------------------------------------

@pytest.mark.parametrize("path", LISTED)
def test_the_sheet_is_once_on_a_page_with_rows_and_holds_one_token_per_action(world, path):
    c, _ = world
    text = c.get(path).text
    page = PAGE_OF[path]
    assert text.count('<dialog class="sheet"') == 1 and text.count('id="bulk"') == 1
    tokens = dict(re.findall(r' data-csrf-(\w+)="([0-9a-f]+)"', text))
    assert list(tokens) == list(statusflow.PAGE_ACTIONS[page])
    for action, value in tokens.items():
        assert value == c.app.state.csrf(f"bulk:{action}")
    form = re.search(r'<form method="post" action="/bulk-status" id="sheetform"[^>]*>', text)
    assert form and 'name="next" value="' + page + '"' in text
    for name in ("csrf", "action", "ids", "page"):
        assert f'<input type="hidden" name="{name}"' in text
    assert 'name="confirm" value="yes"' in text and 'class="sheet-cancel"' in text and 'maxlength="%d"' % web.COMMENT_MAX in text
    assert re.search(r'<div class="sheet-comment" hidden>', text) and "<textarea" in text


@pytest.mark.parametrize("path", [p for p in LISTED])
def test_a_page_without_rows_has_neither_sheet_nor_bar(cfg, conn, tmp_path, path):
    c = client(cfg, tmp_path)
    text = c.get(path).text
    assert "<dialog" not in text and 'id="bulk"' not in text and 'class="selbar"' not in text and "selecttoggle" not in text


def test_the_missing_sheet_offers_reject_only_and_offers_has_its_own_labels(world):
    c, _ = world
    missing = c.get("/missing").text
    assert re.findall(r' data-csrf-(\w+)="', missing) == ["reject"]
    offers = c.get("/offers").text
    form = re.search(r'<form id="bulk"[^>]*>', offers).group(0)
    assert 'data-label-withdrawn="Declined"' in form and 'data-label-employer_rejected="Offer withdrawn by employer"' in form and 'data-label-undo="Undo"' in form
    top = re.search(r'<form id="bulk"[^>]*>', c.get("/").text).group(0)
    assert 'data-label-reject="Reject"' in top and 'data-label-shortlist="Shortlist"' in top and 'data-label-applied="Applied"' in top


def test_the_swipe_table_matches_the_page_actions():
    assert set(statusflow.SWIPE) == set(statusflow.PAGE_ACTIONS)
    for page, (lead, trail) in statusflow.SWIPE.items():
        assert set(lead) | set(trail) <= set(statusflow.PAGE_ACTIONS[page]), page
        assert not set(lead) & set(trail), page
    assert statusflow.SWIPE["offers"][0] == ("accept",) and statusflow.SWIPE["accepted"] == ((), ("undo",)) and statusflow.SWIPE["rejected"] == ((), ("undo",)) and statusflow.SWIPE["withdrawn"] == ((), ("undo",))


@pytest.mark.parametrize("path", LISTED)
def test_the_swipe_data_comes_from_the_table(world, path):
    c, _ = world
    page = PAGE_OF[path]
    form = re.search(r'<form id="bulk"[^>]*>', c.get(path).text).group(0)
    lead, trail = statusflow.SWIPE[page]
    assert f'data-swipe-lead="{" ".join(lead)}"' in form and f'data-swipe-trail="{" ".join(trail)}"' in form
    assert re.findall(r' data-label-(\w+)="', form) == list(statusflow.PAGE_ACTIONS[page])


def test_the_hidden_controls_need_javascript(world):
    c, _ = world
    text = c.get("/jobs").text
    assert '<button type="button" class="selecttoggle" hidden aria-pressed="false">Select</button>' in text
    assert re.search(r'<input type="checkbox" class="selectall" hidden', text)


# --- the sheet posts like the confirmation page ---------------------------------------------------------------

def sheet_post(c, text, action, ids, confirm=True, **fields):
    """The POST that the sheet sends: its own token from `data-csrf-<action>`, the comma-joined ids and the hidden fields of the markup."""
    form = re.search(r'<form method="post" action="/bulk-status" id="sheetform"[^>]*>(.*?)</form>', text, re.S)
    hidden = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form.group(1)))
    token = re.search(rf' data-csrf-{action}="([0-9a-f]+)"', text).group(1)
    data = {**hidden, "csrf": token, "action": action, "ids": ",".join(map(str, ids)), **fields}
    if confirm:
        data["confirm"] = "yes"
    return c.post("/bulk-status", data=data, follow_redirects=False)


def test_a_post_built_from_the_sheet_changes_the_jobs_and_one_without_confirm_does_not(world, conn, dbfile):
    c, ids = world
    text = c.get("/jobs").text
    r = sheet_post(c, text, "shortlist", [ids[0]], confirm=False)
    assert r.status_code == 303 and status_of(dbfile, ids[0]) != "shortlisted" and "Not confirmed" in flash_of(c, r)
    r = sheet_post(c, text, "shortlist", [ids[0]])
    assert r.status_code == 303 and status_of(dbfile, ids[0]) == "shortlisted"
    interviews = c.get("/interviews").text
    r = sheet_post(c, interviews, "offer", [ids[4]])
    assert r.status_code == 303 and status_of(dbfile, ids[4]) == "offer"


def test_a_reject_through_the_sheet_takes_the_comment(world, conn):
    c, ids = world
    r = sheet_post(c, c.get("/missing").text, "reject", [ids[1]], comment="  not  for me ")
    assert r.status_code == 303 and conn.execute("SELECT COUNT(*) FROM jobs WHERE id = ?", (ids[1],)).fetchone()[0] == 0


def test_the_token_of_another_action_is_refused(world, conn, dbfile):
    c, ids = world
    text = c.get("/jobs").text
    other = re.search(r' data-csrf-reject="([0-9a-f]+)"', text).group(1)
    r = c.post("/bulk-status", data={"csrf": other, "action": "shortlist", "ids": str(ids[0]), "next": "list", "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 403 and status_of(dbfile, ids[0]) != "shortlisted"


def test_markup_in_a_title_stays_escaped_in_the_data_the_sheet_uses(world, conn):
    c, ids = world
    evil = '"><img src=x onerror=alert(1)>'
    with conn:
        conn.execute("UPDATE jobs SET title = ?, company = ? WHERE id IN (?, ?)", (evil, evil, ids[0], ids[3]))
    for path in ("/", "/jobs", "/applied"):
        text = c.get(path).text
        assert "<img" not in text and "&lt;img" in text, path
        for attr in re.findall(r'data-(?:title|company|label-\w+|swipe-\w+)="([^"]*)"', text):
            assert "<" not in attr and '"' not in attr


# --- static/select.js (B17) ----------------------------------------------------------------------------------

JS = web.HERE / "static" / "select.js"
FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "fetch", "XMLHttpRequest", "sendBeacon",
             "localStorage", "sessionStorage", 'setAttribute("style"', "setAttribute('style'", "javascript:", "WebSocket", "import(")


def test_select_js_uses_no_unsafe_api_and_stays_small():
    text = JS.read_text(encoding="utf-8")
    assert text.count('"use strict";') == 1 and text.lstrip().startswith("/*") and "(function () {" in text
    for word in FORBIDDEN:
        assert word not in text, word
    assert len(text.encode("utf-8")) < 15_000
    assert "document.cookie" not in text and "location" not in text and "window.open" not in text
    assert "textContent" in text and "createElement" in text and "showModal" in text and "(pointer: coarse)" in text


def test_select_js_holds_no_status_action_or_page_name():
    text = JS.read_text(encoding="utf-8")
    names = set(statusflow.FORWARD) | {t for t, _ in statusflow.FORWARD.values()} | {s for _, f in statusflow.FORWARD.values() for s in f} \
        | set(statusflow.PAGE_ACTIONS) | set(statusflow.STAGE_PAGES) | set(statusflow.STAGE_PAGES.values()) | set(web.PAGE_PATHS) | {"awaiting_llm", "not_selected"}
    for name in names:
        assert not re.search(r"""["'`/]%s["'`]""" % re.escape(name), text), name
    for path in web.BULK_PATHS.values():
        assert f'"{path}"' not in text, path


def test_select_js_is_served_as_javascript_without_sniffing(world):
    c, _ = world
    r = c.get("/static/select.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and r.headers["x-content-type-options"] == "nosniff"
    assert r.content == JS.read_bytes()  # bytes: read_text() turns a CRLF checkout (Windows) into LF


def test_the_markup_names_the_careful_actions_and_the_reject_comment(world):
    c, _ = world
    text = c.get("/applied").text
    assert f'data-careful="{" ".join(statusflow.CAREFUL)}"' in text and set(statusflow.CAREFUL) <= set(web.BULK_ACTIONS)
    assert 'id="sheetform" data-comment-for="reject"' in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed (a local check of the syntax of select.js)")
def test_select_js_is_valid_javascript():
    done = subprocess.run([shutil.which("node"), "--check", str(JS)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_the_sheet_carries_the_applied_reject_warning_and_only_applied_rows_trigger_it(world):
    c, ids = world
    for path in ("/", "/jobs", "/missing", "/applied"):
        text = c.get(path).text
        m = re.search(r'<p class="sheet-warn" data-warn-for="reject" hidden>(.*?)</p>', text, re.S)
        assert m and "marked as applied" in m.group(1) and "restore-job" in m.group(1), path
    applied = c.get("/applied").text
    assert re.findall(r'data-warn="1"', applied) == ['data-warn="1"']
    assert f'<td class="sel" data-actions="' in applied and 'data-warn="1"' in re.search(r'<td class="sel"[^>]*>', applied).group(0)
    assert "data-warn" not in c.get("/jobs").text.replace('data-warn-for', "") and "data-warn" not in c.get("/missing").text.replace('data-warn-for', "")


def test_select_js_reads_the_warning_from_the_markup():
    text = JS.read_text(encoding="utf-8")
    assert "data-warn-for" in text and "data-warn" in text


def test_the_swipe_layers_are_not_shown_until_a_row_is_swiping():
    css = (web.HERE / "static" / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.swipe-lead, \.swipe-trail \{[^}]*display: none", css) and ".swipe-row.swiping .swipe-lead" in css
    js = JS.read_text(encoding="utf-8")
    assert '"swiping"' in js and js.index('classList.add("swiping")') < js.index("s.base = [s.lead")


def test_the_submit_handler_prevents_the_default_only_when_the_sheet_opens():
    js = JS.read_text(encoding="utf-8")
    body = js[js.index('bulk.addEventListener("submit"'):js.index('cancelBtn.addEventListener')]
    assert "if (open(" in body and body.index("preventDefault") > body.index("if (open(")


def test_the_sheet_of_applied_has_the_round_fields_disabled_and_hidden_with_phone_first(world):
    c, _ = world
    text = c.get("/applied").text
    box = re.search(r'<div class="sheet-round" data-round-for="interview" hidden>(.*?)</div>', text, re.S)
    assert box
    fields = re.findall(r"<(?:input|select)[^>]*>", box.group(1))
    assert len(fields) == 3 and all(" disabled" in f for f in fields) and [n for n in re.findall(r'name="(\w+)"', box.group(1))] == ["day", "clock", "kind"]
    assert box.group(1).index('value="phone"') < box.group(1).index('value="video"')


@pytest.mark.parametrize("path", [p for p in LISTED if p not in ("/applied",)])
def test_pages_without_the_interview_action_have_no_round_fields(world, path):
    c, _ = world
    assert "data-round-for" not in c.get(path).text
