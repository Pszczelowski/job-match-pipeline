"""The missing-description queue in the web app (goal.md 5.7, v0.72.0): `/missing`, the paste page
`/jobs/<id>/description` (POST with its own 1 MB body limit), the clipboard bookmarklet and the hidden title-only score.

Every test builds its own temporary database; none uses the real config, the real database, the run lock of the
repository or the network.
"""
import html
import json
import re
import sqlite3
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from app import db, web
from tests.test_web import HOST, client, conn, cfg                              # noqa: F401  (fixtures and helpers)
from tests.test_web_writes import token, post, snapshot, dbfile                 # noqa: F401
from tests.test_report import job, pipeline
from tests.test_descriptions import COMPLETE_POSTING, POSTING


@pytest.fixture
def site(cfg, conn, tmp_path):
    """Job 1 (80) is described, job 2 is title-only (60, queued), job 3 is title-only and outside (top_n = 2)."""
    cfg["paths"]["lock"] = str(tmp_path / "run.lock")
    cfg["scoring"]["top_n"] = 2
    ids, _ = pipeline(conn, cfg, [job(1), job(2, description=""), job(3, description="")], [80, 60, 50])
    return client(cfg, tmp_path), ids


def seen(r):
    return html.unescape(r.text)


def paste(c, job_id, text, **kw):
    tok = c.app.state.csrf(f"describe:{job_id}")
    return post(c, f"/jobs/{job_id}/description", tok, text=text, **kw)


# --- /missing ----------------------------------------------------------------------------------------

def test_missing_lists_only_queued_open_jobs_with_safe_links(site, conn, cfg, tmp_path):
    c, ids = site
    with conn:
        conn.execute("UPDATE job_sources SET url = 'javascript:alert(1)' WHERE job_id = ?", (ids[1],))
    r = c.get("/missing")
    body = seen(r).replace(web.BOOKMARKLET, "")                                   # the bookmarklet itself is a javascript: link
    assert r.status_code == 200 and "Analyst 2" in body
    assert "Analyst 1" not in body and "Analyst 3" not in body                    # described / not queued
    assert "javascript:" not in body and "alert(1)" not in body                   # an unsafe URL is not a link
    assert f'href="/jobs/{ids[1]}/description"' in body
    with conn:
        conn.execute("UPDATE job_sources SET url = 'https://example.com/job/2' WHERE job_id = ?", (ids[1],))
    body = seen(c.get("/missing"))
    assert 'href="https://example.com/job/2"' in body and 'rel="noopener noreferrer"' in body and 'target="_blank"' in body
    with conn:
        conn.execute("UPDATE jobs SET closed_at = '2026-11-01T00:00:00Z' WHERE id = ?", (ids[1],))
    assert "Analyst 2" not in c.get("/missing").text                              # a closed job is hidden


def test_missing_shows_the_tiers_tried(site, conn):
    c, ids = site
    with conn:
        conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) "
                     "VALUES (?, 't', '2026-11-02', 'T2', 'workday-acme', 'workday', 'no_match')", (ids[1],))
    body = seen(c.get("/missing"))
    assert "T2" in body and "workday-acme" in body and "no_match" in body


def test_a_queued_job_page_and_the_job_list_show_waiting_not_the_score(site, conn):
    c, ids = site
    detail = seen(c.get(f"/jobs/{ids[1]}"))
    assert "Waits for a description: it would be in the top 10" in detail
    assert 'class="score"' not in detail and "confidence" not in detail
    assert f'href="/jobs/{ids[1]}/description"' in detail
    with conn:
        conn.execute("UPDATE jobs SET description_missing = 0 WHERE id = ?", (ids[1],))      # decision of 11 Nov: a job waiting on /missing is not on /jobs; one pasted but not scored again yet is
    listing = seen(c.get("/jobs"))
    row = next(line for line in listing.split("</tr>") if "Analyst 2" in line)
    assert "added" in row and "60" not in row.replace(f"{ids[1]}", "")
    other = next(line for line in listing.split("</tr>") if "Analyst 1" in line)
    assert "80" in other
    assert 'class="score"' in seen(c.get(f"/jobs/{ids[0]}"))                      # a described job keeps its score block


def test_the_top10_page_has_the_waiting_block_and_no_title_only_score(site):
    c, ids = site
    body = seen(c.get("/"))
    assert "Waiting for a description (1)" in body and "Analyst 2" in body
    assert f'href="/missing"' in body


# --- the paste page ----------------------------------------------------------------------------------

def test_the_paste_page_has_the_form_and_token(site):
    c, ids = site
    r = c.get(f"/jobs/{ids[1]}/description")
    assert r.status_code == 200 and 'name="text"' in r.text and "<textarea" in r.text
    assert token(c, f"/jobs/{ids[1]}/description", f"/jobs/{ids[1]}/description") == c.app.state.csrf(f"describe:{ids[1]}")


def put_partial(conn, job_id, text, day="2026-11-12", source="axa"):
    with conn:
        conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, posting_id, status, text) "
                     "VALUES (?, 'x', ?, 'T1', ?, 'jibe', '77', 'partial', ?)", (job_id, day, source, text))


def test_the_paste_page_is_prefilled_with_the_newest_partial_text(site, conn):
    c, ids = site
    put_partial(conn, ids[1], "Old partial text.", day="2026-11-10")
    put_partial(conn, ids[1], "Newer <b>partial</b> text & more.", day="2026-11-12")
    body = c.get(f"/jobs/{ids[1]}/description").text
    assert "Newer &lt;b&gt;partial&lt;/b&gt; text &amp; more." in body and "<b>partial</b>" not in body and "Old partial" not in body
    assert "Prefilled with 33 characters found on axa on 2026-11-12; check and complete it (at least 500)" in body
    other = seen(c.get(f"/jobs/{ids[2]}/description"))                               # a job without a partial text: empty box, no notice
    assert "Prefilled" not in other and re.search(r'<textarea[^>]*></textarea>', other)
    r = paste(c, ids[1], "too short")                                                # POST: the minimum stays 500, the posted text is kept
    assert r.status_code == 422 and "too short" in seen(r) and "Prefilled" not in seen(r)


def test_the_job_page_says_a_partial_text_is_waiting_on_the_paste_page(site, conn):
    c, ids = site
    assert "is waiting on the" not in c.get(f"/jobs/{ids[1]}").text
    put_partial(conn, ids[1], "x" * 120)
    body = c.get(f"/jobs/{ids[1]}").text
    assert "A partial text (120 characters) is waiting on the" in body and f'href="/jobs/{ids[1]}/description"' in body
    assert "is waiting on the" not in c.get(f"/jobs/{ids[0]}").text                  # a described job shows nothing


def test_paste_page_404_for_unknown_job(site):
    c, _ = site
    assert c.get("/jobs/9999/description").status_code == 404
    assert c.get("/jobs/abc/description").status_code == 404


def test_complete_text_is_stored_marked_for_rescore_and_logged_as_web(site, conn, dbfile):
    c, ids = site
    r = paste(c, ids[1], COMPLETE_POSTING)
    assert r.status_code == 303 and r.headers["location"].startswith("/missing")
    row = conn.execute("SELECT description, description_missing, needs_rescore FROM jobs WHERE id = ?", (ids[1],)).fetchone()
    assert row["description"] == COMPLETE_POSTING.strip() and row["description_missing"] == 0 and row["needs_rescore"] == 1
    assert conn.execute("SELECT source FROM description_log WHERE job_id = ?", (ids[1],)).fetchone()[0] == "web"
    page = seen(c.get(r.headers["location"]))
    assert f"Job {ids[1]}: description stored" in page and "scored again in the next batch" in page


def test_incomplete_text_is_refused_with_422_the_warning_and_the_text_kept_and_nothing_written(site, conn, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    r = paste(c, ids[1], POSTING[:300] + " MY-PASTED-MARKER")
    body = seen(r)
    assert r.status_code == 422 and "not complete" in body.lower() and "only " in body
    assert "MY-PASTED-MARKER" in body                                             # the text stays in the box
    assert snapshot(dbfile) == before and conn.execute("SELECT COUNT(*) FROM description_log").fetchone()[0] == 0


def test_text_without_headings_is_stored_with_a_warning_on_the_missing_page(site, conn):
    c, ids = site
    text = "We are a bank in Zurich and look for an analyst for our trading desk. " * 10
    r = paste(c, ids[1], text)
    assert r.status_code == 303
    assert conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (ids[1],)).fetchone()[0] == 0
    page = seen(c.get(r.headers["location"]))
    assert f"Job {ids[1]}: description stored" in page and 'class="notice warning"' in page
    assert "no tasks part found" in page and "stored anyway" in page


def test_paste_refused_without_token_or_with_the_token_of_another_job(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    assert post(c, f"/jobs/{ids[1]}/description", None, text=COMPLETE_POSTING).status_code == 403
    wrong = c.app.state.csrf(f"describe:{ids[2]}")
    assert post(c, f"/jobs/{ids[1]}/description", wrong, text=COMPLETE_POSTING).status_code == 403
    other_action = c.app.state.csrf(f"reject:{ids[1]}")
    assert post(c, f"/jobs/{ids[1]}/description", other_action, text=COMPLETE_POSTING).status_code == 403
    assert snapshot(dbfile) == before


def test_paste_refused_with_a_foreign_origin_a_foreign_host_and_another_content_type(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    tok = c.app.state.csrf(f"describe:{ids[1]}")
    path = f"/jobs/{ids[1]}/description"
    assert post(c, path, tok, headers={"Origin": "http://evil.example"}, text=COMPLETE_POSTING).status_code == 403
    assert post(c, path, tok, headers={"Origin": "null"}, text=COMPLETE_POSTING).status_code == 403
    assert c.post(path, data={"csrf": tok, "text": COMPLETE_POSTING}, headers={"Host": "evil.example"}).status_code == 400
    assert c.post(path, json={"csrf": tok}, follow_redirects=False).status_code == 415
    assert c.post(path, content=f"csrf={tok}", headers={"Content-Type": "text/plain"}, follow_redirects=False).status_code == 415
    assert snapshot(dbfile) == before


def test_paste_over_the_body_limit_is_refused_and_the_other_routes_keep_10000_bytes(site, dbfile):
    c, ids = site
    assert web.MAX_BODY == 10_000 and web.MAX_DESCRIPTION_BODY == 1_000_000
    tok = c.app.state.csrf(f"describe:{ids[1]}")
    big = urllib.parse.urlencode({"csrf": tok, "text": "x" * (web.MAX_DESCRIPTION_BODY + 1)})
    r = c.post(f"/jobs/{ids[1]}/description", content=big, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    # a posting of about 100,000 characters (under MAX_CHARS) with umlauts is well over 10 kB encoded but inside the 1 MB limit
    mid = urllib.parse.urlencode({"csrf": tok, "text": COMPLETE_POSTING + "Zürich " * 13000})
    assert len(mid) > web.MAX_BODY
    r = c.post(f"/jobs/{ids[1]}/description", content=mid, headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 303
    for action in ("shortlist", "applied", "reject", "approve", "undo-approval", "undo-status"):
        t = c.app.state.csrf(f"{action}:{ids[0]}")
        assert c.post(f"/jobs/{ids[0]}/{action}", data={"csrf": t, "pad": "x" * (web.MAX_BODY + 1)}).status_code == 413


def test_paste_refused_for_a_job_that_has_a_description(site, conn):
    c, ids = site
    r = paste(c, ids[0], COMPLETE_POSTING)
    assert r.status_code == 303
    assert "already has a description" in seen(c.get(r.headers["location"]))
    assert conn.execute("SELECT description FROM jobs WHERE id = ?", (ids[0],)).fetchone()[0] == "Description 1"


def test_paste_refused_while_the_run_lock_is_held(site, conn, cfg):
    c, ids = site
    from datetime import datetime, timezone
    lock = web.Path(cfg["paths"]["lock"])
    lock.write_text(json.dumps({"pid": 1, "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}), encoding="utf-8")
    r = paste(c, ids[1], COMPLETE_POSTING)
    assert r.status_code == 409 and "a pipeline run is in progress" in seen(r).lower() and "About the team" in seen(r)
    assert conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (ids[1],)).fetchone()[0] == 1
    lock.write_text(json.dumps({"pid": 1, "started": "2020-01-01T00:00:00Z"}), encoding="utf-8")       # a stale lock does not count
    assert paste(c, ids[1], COMPLETE_POSTING).status_code == 303
    assert conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (ids[1],)).fetchone()[0] == 0


# --- the bookmarklet and the unchanged guards ----------------------------------------------------------

def test_the_bookmarklet_sends_nothing(site):
    c, _ = site
    r = c.get("/missing")
    bm = web.BOOKMARKLET
    assert bm.startswith("javascript:")
    for word in ("http", "fetch", "XMLHttpRequest", "sendBeacon", "window.open", "cookie", "localStorage", "location"):
        assert word.lower() not in bm.lower(), word
    assert "clipboard" in bm and "innerText" in bm
    assert "Copy failed" in bm                                                    # "Copied" is said only after a successful copy
    assert seen(r).count(bm) >= 2                                                 # shown as a link and as text to copy
    assert "script-src 'self'" in r.headers["content-security-policy"] and "script-src 'none'" not in r.headers["content-security-policy"] and "unsafe-inline" not in r.headers["content-security-policy"] and "unsafe-eval" not in r.headers["content-security-policy"]
    assert "<script" not in r.text.replace('<script src="/static/select.js" defer></script>', "").lower()


def test_get_missing_and_the_paste_page_write_nothing(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    assert c.get("/missing").status_code == 200 and c.get(f"/jobs/{ids[1]}/description").status_code == 200
    assert snapshot(dbfile) == before
    for verb in ("put", "patch", "delete"):
        assert getattr(c, verb)("/missing").status_code == 405
    assert c.post("/missing").status_code == 405


def test_an_empty_or_unreadable_lock_file_counts_as_held(site, conn, cfg):
    c, ids = site
    web.Path(cfg["paths"]["lock"]).write_text("", encoding="utf-8")
    r = paste(c, ids[1], COMPLETE_POSTING)
    assert r.status_code == 409 and conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (ids[1],)).fetchone()[0] == 1


def test_after_a_paste_the_job_page_says_the_description_was_added(site, conn):
    c, ids = site
    assert paste(c, ids[1], COMPLETE_POSTING).status_code == 303
    body = seen(c.get(f"/jobs/{ids[1]}"))
    assert "Description added; scored again in the next batch" in body and 'class="score"' not in body
    with conn:
        conn.execute("UPDATE jobs SET status = 'shortlisted' WHERE id = ?", (ids[1],))       # decision of 10 Nov: not_selected jobs are not on /jobs
    row = next(line for line in seen(c.get("/jobs")).split("</tr>") if "Analyst 2" in line)
    assert "added" in row


def test_a_whole_linkedin_page_pasted_is_stored_as_the_posting_only(site, conn, dbfile):
    from tests.test_descriptions import linkedin_page
    c, ids = site
    assert paste(c, ids[1], linkedin_page(COMPLETE_POSTING)).status_code == 303
    assert conn.execute("SELECT description FROM jobs WHERE id = ?", (ids[1],)).fetchone()[0] == COMPLETE_POSTING.strip()
