"""The web app, chunk 2 (v0.46.0, goal.md 5.7): approving documents, Shortlist / Applied / Reject, and the write protection
(POST only, CSRF token, Host / Origin / Referer checks). T-WEB-20 and following in docs/TEST_CASES.md.

Every test builds its own temporary database and approved folder; none uses the real config, the real database or the network.
"""
import html
import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import approval, db, documents, web
from tests.test_approval import doc_row, files, imported, job_status           # noqa: F401
from tests.test_web import HOST, client, conn, cfg, site                         # noqa: F401  (fixtures and helpers)
from tests.test_report import job, pipeline

WRITE_ROUTES = ("shortlist", "applied", "approve", "reject")


# --- helpers ----------------------------------------------------------------------------------------

def token(c, page, action_path):
    """The CSRF token the page `page` puts in the form that posts to `action_path`."""
    body = c.get(page).text
    m = re.search(r'action="' + re.escape(action_path) + r'"[^>]*>\s*<input type="hidden" name="csrf" value="([0-9a-f]{64})"', body)
    assert m, f"no form for {action_path} on {page}"
    return m.group(1)


def post(c, path, tok, follow=False, headers=None, **fields):
    data = dict(fields)
    if tok is not None:
        data["csrf"] = tok
    return c.post(path, data=data, headers=headers, follow_redirects=follow)


def act(c, job_id, action, page=None, **fields):
    """Post `action` on a job with the token of the job's own page (like a click on its button); returns the response, not followed."""
    path = f"/jobs/{job_id}/{action}"
    tok = c.app.state.csrf(f"{action}:{job_id}") if page is None else token(c, page, path)
    return post(c, path, tok, **fields)


def seen(r):
    return html.unescape(r.text)


def status_of(path, job_id):
    raw = sqlite3.connect(path)
    try:
        row = raw.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row[0] if row else None
    finally:
        raw.close()


def snapshot(path):
    raw = sqlite3.connect(path)
    try:
        return {t: raw.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                for t in ("jobs", "status_history", "job_archive", "documents", "scores", "job_sources")}
    finally:
        raw.close()


def set_status(conn, job_id, status):
    with conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))


def history(conn, job_id):
    return [tuple(r) for r in conn.execute("SELECT from_status, to_status, note FROM status_history WHERE job_id = ? ORDER BY id", (job_id,))]


@pytest.fixture
def dbfile(tmp_path):
    return tmp_path / "w.db"


# --- the buttons ------------------------------------------------------------------------------------

def test_top10_and_detail_show_the_buttons_that_fit_the_status(site, conn):
    c, ids = site
    top = c.get("/").text
    assert top.count('action="/jobs/') == 3 and f'action="/jobs/{ids[1]}/shortlist"' in top and not re.search(r'action="/jobs/\d+/applied"', top)       # the menu link /applied is not a form
    assert f'href="/jobs/{ids[1]}/reject?next=top"' in top and top.count("Reject&hellip;") == 3
    d = c.get(f"/jobs/{ids[0]}").text
    assert f'action="/jobs/{ids[0]}/shortlist"' in d and f'href="/jobs/{ids[0]}/reject?next=job"' in d and 'action="/jobs/' + f'{ids[0]}/applied"' not in d
    assert "<script" not in top.replace('<script src="/static/select.js" defer></script>', "").lower() and "style=" not in top.lower() and "onclick" not in top.lower()
    set_status(conn, ids[0], "shortlisted")
    d = c.get(f"/jobs/{ids[0]}").text
    assert "/shortlist" not in d and f'action="/jobs/{ids[0]}/applied"' in d and "Reject&hellip;" in d
    set_status(conn, ids[0], "applied")
    d = c.get(f"/jobs/{ids[0]}").text
    assert "/shortlist" not in d and f'action="/jobs/{ids[0]}/applied"' not in d and "Reject&hellip;" in d        # decision of 10 Nov: Reject is allowed from applied


def test_every_form_has_its_own_token(site):
    c, ids = site
    top = c.get("/").text
    tokens = re.findall(r'name="csrf" value="([0-9a-f]{64})"', top)
    assert len(tokens) == 3 and len(set(tokens)) == 3                               # one per job
    assert token(c, "/", f"/jobs/{ids[1]}/shortlist") == c.app.state.csrf(f"shortlist:{ids[1]}")
    assert c.app.state.csrf(f"shortlist:{ids[1]}") != c.app.state.csrf(f"applied:{ids[1]}")


# --- Shortlist and Applied --------------------------------------------------------------------------

def test_shortlist_from_the_top10_changes_the_status_records_history_and_comes_back_to_the_top10(site, conn, dbfile):
    c, ids = site
    r = post(c, f"/jobs/{ids[1]}/shortlist", token(c, "/", f"/jobs/{ids[1]}/shortlist"), next="top")
    assert r.status_code == 303 and r.headers["location"].startswith("/?flash=") and r.text == ""
    assert status_of(dbfile, ids[1]) == "shortlisted" and status_of(dbfile, ids[0]) == "scored"
    assert history(conn, ids[1])[-1] == ("scored", "shortlisted", "web")
    page = c.get(r.headers["location"])
    assert page.status_code == 200 and f"Job {ids[1]} (Analyst 2, Acme) is now shortlisted (was scored)." in seen(page)
    assert 'class="notice ok"' in page.text
    assert "is now shortlisted" not in c.get(r.headers["location"]).text             # shown once
    assert f'action="/jobs/{ids[1]}/applied"' in c.get("/").text                      # the buttons follow the new status


def test_shortlist_from_the_detail_page_returns_to_the_detail_page(site, dbfile):
    c, ids = site
    r = post(c, f"/jobs/{ids[0]}/shortlist", token(c, f"/jobs/{ids[0]}", f"/jobs/{ids[0]}/shortlist"), next="job")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}?flash=")
    page = c.get(r.headers["location"])
    assert "status shortlisted" in page.text and "is now shortlisted" in page.text
    assert status_of(dbfile, ids[0]) == "shortlisted"


def test_shortlist_of_a_not_selected_job(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(i) for i in range(1, 14)], list(range(40, 53)))
    low = conn.execute("SELECT id FROM jobs WHERE status = 'not_selected' ORDER BY id LIMIT 1").fetchone()["id"]
    c = client(cfg, tmp_path)
    assert f"/jobs/{low}/shortlist" in c.get(f"/jobs/{low}").text
    assert act(c, low, "shortlist", page=f"/jobs/{low}").status_code == 303
    assert status_of(tmp_path / "w.db", low) == "shortlisted" and history(conn, low)[-1] == ("not_selected", "shortlisted", "web")


def test_shortlist_of_a_job_waiting_for_a_score_is_allowed_only_when_it_has_one(site, conn, dbfile):
    c, ids = site
    set_status(conn, ids[0], "awaiting_llm")                                        # scored before (a rescore): allowed
    assert "/shortlist" in c.get(f"/jobs/{ids[0]}").text
    assert act(c, ids[0], "shortlist").status_code == 303 and status_of(dbfile, ids[0]) == "shortlisted"
    with conn:                                                                      # no score at all: refused
        conn.execute("DELETE FROM scores WHERE job_id = ?", (ids[2],))
    set_status(conn, ids[2], "awaiting_llm")
    assert "/shortlist" not in c.get(f"/jobs/{ids[2]}").text
    r = act(c, ids[2], "shortlist")
    assert r.status_code == 303 and status_of(dbfile, ids[2]) == "awaiting_llm"
    assert "waiting for its first score" in seen(c.get(r.headers["location"]))


@pytest.mark.parametrize("start", ["shortlisted", "approved"])
def test_applied_from_shortlisted_or_approved(site, conn, dbfile, start):
    c, ids = site
    set_status(conn, ids[0], start)
    r = post(c, f"/jobs/{ids[0]}/applied", token(c, f"/jobs/{ids[0]}", f"/jobs/{ids[0]}/applied"), next="job")
    assert r.status_code == 303 and status_of(dbfile, ids[0]) == "applied"
    assert history(conn, ids[0])[-1] == (start, "applied", "web")
    assert f"is now applied (was {start})" in seen(c.get(r.headers["location"]))


def test_applied_from_scored_is_refused_with_a_message_and_changes_nothing(site, conn, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    r = act(c, ids[0], "applied")
    assert r.status_code == 303
    page = c.get(r.headers["location"])
    assert page.status_code == 200 and "only a shortlisted or approved job can be marked as applied" in seen(page)
    assert 'class="notice refused"' in page.text and snapshot(dbfile) == before


def test_refused_transitions_never_give_a_500_and_never_change_anything(site, conn, dbfile):
    """All statuses x all actions: allowed ones act, refused ones answer 303 with a message and leave the database alone."""
    c, ids = site
    for status in db.STATUSES:
        for action in ("shortlist", "applied", "reject"):
            set_status(conn, ids[2], status)
            allowed = action in web.allowed_actions(status, True)
            before = snapshot(dbfile)
            if action == "reject":
                data = {"confirm": "yes"} if not allowed else None
                if allowed:
                    continue                                    # archiving is tested on its own below
                r = act(c, ids[2], action, **data)
            else:
                r = act(c, ids[2], action)
            assert r.status_code == 303, (status, action)
            if not allowed:
                assert snapshot(dbfile) == before, (status, action)
                assert 'class="notice refused"' in c.get(r.headers["location"]).text, (status, action)
            else:
                assert status_of(dbfile, ids[2]) != status
                set_status(conn, ids[2], status)


def test_a_job_that_is_no_longer_open_is_a_message_not_an_error(site, conn):
    c, ids = site
    db.archive_job(conn, ids[0], "filter", "seniority")
    for action in ("shortlist", "applied"):
        r = act(c, ids[0], action)
        assert r.status_code == 303 and r.headers["location"].startswith("/jobs")
        assert "not open any more" in seen(c.get(r.headers["location"]))
    r = act(c, ids[0], "approve")
    assert r.status_code == 303 and "not open any more" in seen(c.get(r.headers["location"]))
    assert c.get(f"/jobs/{ids[0]}/reject").status_code == 404


def test_next_is_never_a_free_url(site):
    c, ids = site
    for nxt in ("https://evil.example/", "//evil.example", "/jobs/1", "", "TOP"):
        r = act(c, ids[0], "shortlist", next=nxt)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}?flash=") and "evil" not in r.headers["location"]
    r = post(c, f"/jobs/{ids[1]}/shortlist", c.app.state.csrf(f"shortlist:{ids[1]}"), next="list")
    assert r.headers["location"].startswith("/jobs?flash=")


def test_a_message_with_markup_in_the_job_title_is_escaped(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(1, title='T <script>alert("x")</script>', company="C <b>x</b>")], [80])
    c = client(cfg, tmp_path)
    r = act(c, ids[0], "shortlist", next="top")
    body = c.get(r.headers["location"]).text
    assert "<script" not in body.replace('<script src="/static/select.js" defer></script>', "").lower() and "&lt;script&gt;" in body and "<b>x" not in body


def test_flash_messages_cannot_be_made_up_by_a_link(site):
    c, _ = site
    for url in ("/?flash=abc", "/?msg=Job+approved", "/jobs?flash=../../x", "/?flash=" + "a" * 5000):
        r = c.get(url)
        assert r.status_code == 200 and "notice" not in r.text and "Job approved" not in r.text


# --- Reject -----------------------------------------------------------------------------------------

def test_the_confirmation_page_changes_nothing_and_explains_the_undo(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    r = c.get(f"/jobs/{ids[0]}/reject?next=top")
    assert r.status_code == 200
    b = r.text
    assert "Reject this job?" in b and "Analyst 1" in b and "user_rejected" in b and "python -m app restore-job" in b and "python -m app archive" in b
    assert f'action="/jobs/{ids[0]}/reject"' in b and 'name="confirm" value="yes"' in b and 'name="next" value="top"' in b
    assert "<script" not in b.lower() and "confirm(" not in b
    assert snapshot(dbfile) == before


def test_reject_without_confirmation_changes_nothing(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    tok = token(c, f"/jobs/{ids[0]}/reject", f"/jobs/{ids[0]}/reject")
    for fields in ({}, {"confirm": "no"}, {"confirm": ""}, {"confirm": "YES"}, {"comment": "x"}):
        r = post(c, f"/jobs/{ids[0]}/reject", tok, next="top", **fields)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}/reject?next=top&flash=")
        assert "Not confirmed" in seen(c.get(r.headers["location"]))
        assert snapshot(dbfile) == before


def test_reject_with_confirmation_archives_the_job_for_the_user(site, conn, dbfile):
    c, ids = site
    tok = token(c, f"/jobs/{ids[1]}/reject", f"/jobs/{ids[1]}/reject")
    r = post(c, f"/jobs/{ids[1]}/reject", tok, confirm="yes", comment="  not  my\n field ", next="top")
    assert r.status_code == 303 and r.headers["location"].startswith("/?flash=")
    row = conn.execute("SELECT * FROM job_archive").fetchone()
    assert (row["rejection_stage"], row["reason_code"], row["comment"], row["original_job_id"], row["title"]) == ("user", "user_rejected", "not my field", ids[1], "Analyst 2")
    assert row["score"] == 90 and row["restored_at"] is None
    assert status_of(dbfile, ids[1]) is None and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    page = c.get(r.headers["location"])
    assert f"was rejected and moved to the archive (archive entry {row['id']}, reason user_rejected)" in seen(page)
    assert f"python -m app restore-job {row['id']}" in seen(page) and "Analyst 2" not in page.text.split("</div>", 1)[1]
    assert c.get(f"/jobs/{ids[1]}").status_code == 404
    new = db.restore_job(conn, row["id"])                                           # reversible, as the page says
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (new,)).fetchone()[0] == "new"


def test_reject_from_the_detail_page_goes_to_the_list_and_an_empty_comment_is_none(site, conn):
    c, ids = site
    r = post(c, f"/jobs/{ids[2]}/reject", c.app.state.csrf(f"reject:{ids[2]}"), confirm="yes", comment="   ", next="job")
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs?flash=")
    assert conn.execute("SELECT comment FROM job_archive").fetchone()[0] is None
    assert "was rejected" in c.get(r.headers["location"]).text


def test_a_long_comment_is_refused_and_nothing_is_archived(site, conn, dbfile):
    c, ids = site
    r = post(c, f"/jobs/{ids[0]}/reject", c.app.state.csrf(f"reject:{ids[0]}"), confirm="yes", comment="x" * (web.COMMENT_MAX + 1))
    assert r.status_code == 303 and "longer than" in seen(c.get(r.headers["location"]))
    assert conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 0 and status_of(dbfile, ids[0]) == "scored"


def test_rejecting_an_applied_job_warns_and_restore_brings_it_back_as_applied(site, conn, dbfile):
    c, ids = site
    set_status(conn, ids[0], "applied")
    page = seen(c.get(f"/jobs/{ids[0]}/reject"))
    assert "marked this job as applied" in page and "brings it back as applied" in page and "not applied" not in page
    r = post(c, f"/jobs/{ids[0]}/reject", c.app.state.csrf(f"reject:{ids[0]}"), confirm="yes")
    assert r.status_code == 303
    new = db.restore_job(conn, conn.execute("SELECT id FROM job_archive").fetchone()[0])
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (new,)).fetchone()[0] == "applied"


def test_a_job_that_is_further_along_than_applied_cannot_be_rejected_by_a_button(site, conn, dbfile):
    c, ids = site
    set_status(conn, ids[0], "interview")           # decision of 10 Nov: applied is rejectable now, interview and later are not
    page = c.get(f"/jobs/{ids[0]}/reject")
    assert page.status_code == 200 and "not rejected here" in seen(page) and 'name="confirm"' not in page.text
    r = post(c, f"/jobs/{ids[0]}/reject", c.app.state.csrf(f"reject:{ids[0]}"), confirm="yes")
    assert r.status_code == 303 and "not rejected here" in seen(c.get(r.headers["location"]))
    assert status_of(dbfile, ids[0]) == "interview" and conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 0


def test_rejecting_a_job_with_approved_documents_keeps_the_pdfs(cfg, conn, tmp_path):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0])
    kept = files(documents.approved_dir(cfg))
    assert kept
    c = client(cfg, tmp_path)
    r = post(c, f"/jobs/{ids[0]}/reject", c.app.state.csrf(f"reject:{ids[0]}"), confirm="yes")
    assert r.status_code == 303
    assert files(documents.approved_dir(cfg)) == kept
    assert "approved documents kept on disk" in conn.execute("SELECT comment FROM job_archive").fetchone()[0]


# --- approving documents ----------------------------------------------------------------------------

def test_the_detail_page_offers_approval_only_while_something_can_be_approved(cfg, conn, tmp_path):
    ids = imported(conn, cfg)
    c = client(cfg, tmp_path)
    b = c.get(f"/jobs/{ids[0]}").text
    assert f'action="/jobs/{ids[0]}/approve"' in b and 'value="cover_letter"' in b and 'value="cv"' in b and 'value="both" selected' in b
    approval.approve(conn, cfg, ids[0])
    assert f'action="/jobs/{ids[0]}/approve"' not in c.get(f"/jobs/{ids[0]}").text


def test_approve_both_documents_from_the_web(cfg, conn, tmp_path):
    ids = imported(conn, cfg)
    c = client(cfg, tmp_path)
    r = post(c, f"/jobs/{ids[0]}/approve", token(c, f"/jobs/{ids[0]}", f"/jobs/{ids[0]}/approve"), kind="both", next="job")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}?flash=")
    for kind in ("cover_letter", "cv"):
        row = doc_row(conn, ids[0], kind)
        assert row["status"] == "approved" and row["approved_at"] and (documents.approved_dir(cfg) / row["approved_pdf_path"]).is_file()
    assert job_status(conn, ids[0]) == "approved"
    page = c.get(r.headers["location"])
    t = seen(page)
    assert f"job {ids[0]} cover_letter: approved" in t and f"job {ids[0]} cv: approved" in t and f"job {ids[0]} is now 'approved'" in t
    assert "2 document(s) approved, 0 already approved, 0 refused" in t
    assert "status approved" in page.text and f'action="/jobs/{ids[0]}/applied"' in page.text and f'action="/jobs/{ids[0]}/approve"' not in page.text


@pytest.mark.parametrize("kind", ["cover_letter", "cv"])
def test_approve_one_document_from_the_web(cfg, conn, tmp_path, kind):
    ids = imported(conn, cfg)
    other = "cv" if kind == "cover_letter" else "cover_letter"
    c = client(cfg, tmp_path)
    r = act(c, ids[0], "approve", kind=kind)
    assert r.status_code == 303
    assert doc_row(conn, ids[0], kind)["status"] == "approved" and doc_row(conn, ids[0], other)["status"] == "draft"
    assert job_status(conn, ids[0]) == "scored"                                      # one alone changes nothing on the job
    assert f'action="/jobs/{ids[0]}/approve"' in c.get(f"/jobs/{ids[0]}").text                               # the other one can still be approved
    assert f"job {ids[0]} {kind}: approved" in seen(c.get(r.headers["location"]))
    r = act(c, ids[0], "approve", kind=kind)                                         # again: idempotent, said so
    assert "already approved" in seen(c.get(r.headers["location"]))


def test_approve_a_refused_document_shows_the_reason(cfg, conn, tmp_path):
    ids = imported(conn, cfg)
    with conn:
        conn.execute("UPDATE documents SET status = 'blocked', findings = ? WHERE job_id = ? AND kind = 'cover_letter'",
                     (json.dumps([{"kind": "unknown_fact", "where": "opening", "reason": "a number not in the CV"}]), ids[0]))
    c = client(cfg, tmp_path)
    r = act(c, ids[0], "approve")                                                    # both: the CV goes through, the letter is refused
    page = c.get(r.headers["location"])
    t = seen(page)
    assert page.status_code == 200 and 'class="notice refused"' in page.text
    assert "REFUSED: job" in t and "blocked by the checks, it cannot be approved. Findings: [unknown_fact] opening: a number not in the CV" in t
    assert doc_row(conn, ids[0], "cover_letter")["status"] == "blocked" and doc_row(conn, ids[0], "cv")["status"] == "approved"
    assert job_status(conn, ids[0]) == "scored"


def test_approve_without_a_pdf_and_for_a_job_without_documents_is_a_message(cfg, conn, tmp_path):
    ids = imported(conn, cfg, scores=(90, 80))
    c = client(cfg, tmp_path)
    r = act(c, ids[1], "approve")                                                    # the second job has no documents at all
    assert r.status_code == 303 and "has no stored" in seen(c.get(r.headers["location"]))
    with conn:
        conn.execute("UPDATE documents SET pdf_path = NULL WHERE job_id = ?", (ids[0],))
    r = act(c, ids[0], "approve")
    assert "has no PDF on disk; run `python -m app render-documents" in seen(c.get(r.headers["location"]))
    assert doc_row(conn, ids[0], "cv")["status"] == "draft"


def test_approve_with_an_odd_kind_is_refused(cfg, conn, tmp_path):
    ids = imported(conn, cfg)
    c = client(cfg, tmp_path)
    for kind in ("letter", "", "cv,cover_letter", "BOTH"):
        r = act(c, ids[0], "approve", kind=kind)
        assert r.status_code == 303 and "Choose the cover letter, the CV or both" in seen(c.get(r.headers["location"]))
    assert doc_row(conn, ids[0], "cv")["status"] == "draft" and files(documents.approved_dir(cfg)) == []


def test_approve_through_the_web_equals_approve_at_the_command_line(cfg, conn, tmp_path):
    """The same fixture twice: one copy approved by `approval.approve` (what `approve-document` calls), one through the web form."""
    ids = imported(conn, cfg)
    twin = tmp_path / "twin.db"
    conn.close()
    raw = sqlite3.connect(tmp_path / "w.db")
    target = sqlite3.connect(twin)
    raw.backup(target)
    raw.close()
    target.close()
    cli_conn = db.connect(twin)
    expected = approval.outcome_lines(approval.approve(cli_conn, cfg, ids[0], None))
    cli_state = ([tuple(r) for r in cli_conn.execute("SELECT kind, status, approved_pdf_path, pdf_path FROM documents ORDER BY kind")],
                 job_status(cli_conn, ids[0]), [tuple(r) for r in cli_conn.execute("SELECT from_status, to_status, note FROM status_history ORDER BY id")])
    cli_conn.close()
    c = client(cfg, tmp_path)
    r = act(c, ids[0], "approve", kind="both")
    raw = sqlite3.connect(tmp_path / "w.db")
    web_state = ([tuple(x) for x in raw.execute("SELECT kind, status, approved_pdf_path, pdf_path FROM documents ORDER BY kind")],
                 raw.execute("SELECT status FROM jobs WHERE id = ?", (ids[0],)).fetchone()[0],
                 [tuple(x) for x in raw.execute("SELECT from_status, to_status, note FROM status_history ORDER BY id")])
    raw.close()
    assert web_state == cli_state and web_state[1] == "approved"
    shown = seen(c.get(r.headers["location"]))
    for _, text in expected:
        assert text in shown, text


def test_the_command_line_prints_what_outcome_lines_gives(cfg, conn, tmp_path, capsys):
    """The refactor that shares the messages keeps the command's output: refused and warning lines on stderr, the rest on stdout."""
    ids = imported(conn, cfg)
    with conn:
        conn.execute("UPDATE documents SET status = 'blocked', findings = ? WHERE job_id = ? AND kind = 'cv'",
                     (json.dumps([{"kind": "k", "reason": "r"}]), ids[0]))
    res = approval.approve(conn, cfg, ids[0], None)
    lines = approval.outcome_lines(res)
    assert [lvl for lvl, _ in lines] == ["ok", "refused", "summary"]
    assert lines[1][1].startswith(f"REFUSED: job {ids[0]} cv: blocked by the checks") and lines[2][1] == "1 document(s) approved, 0 already approved, 1 refused"
    undone = approval.outcome_lines(approval.undo(conn, cfg, ids[0], ["cover_letter"]), undo=True)
    assert undone[-1] == ("summary", "1 document(s) taken back to draft, 0 already approved, 0 refused")


# --- write protection -------------------------------------------------------------------------------

def test_get_never_changes_data(site, conn, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    urls = ["/", "/jobs", f"/jobs/{ids[0]}", f"/jobs/{ids[0]}/reject", f"/jobs/{ids[0]}/reject?next=top", "/static/style.css", "/nothing"]
    urls += [f"/jobs/{ids[0]}/{a}" for a in ("shortlist", "applied", "approve")]
    urls += [f"/jobs/{ids[0]}/{a}?csrf={c.app.state.csrf(f'{a}:{ids[0]}')}&confirm=yes&kind=both" for a in WRITE_ROUTES]
    for url in urls:
        r = c.get(url)
        assert r.status_code in (200, 404, 405), url
        if url.split("?")[0].rsplit("/", 1)[-1] in ("shortlist", "applied", "approve"):
            assert r.status_code == 405, url
    for url in (f"/jobs/{ids[0]}/{a}" for a in WRITE_ROUTES):
        c.head(url)
    assert snapshot(dbfile) == before


def test_a_post_without_a_token_or_with_a_wrong_one_is_403_and_changes_nothing(site, conn, dbfile):
    c, ids = site
    other = c.app.state.csrf(f"shortlist:{ids[1]}")
    before = snapshot(dbfile)
    for action in WRITE_ROUTES:
        path = f"/jobs/{ids[0]}/{action}"
        extra = {"confirm": "yes", "kind": "both"}
        tokens = [None, "", "x", "0" * 64, other, c.app.state.csrf(f"{action}:{ids[1]}"),                 # missing, empty, junk, another job's
                  c.app.state.csrf(f"other:{ids[0]}"), "é" * 64, "a" * 5000]
        if action != "shortlist":
            tokens.append(c.app.state.csrf(f"shortlist:{ids[0]}"))                                           # another action's token
        for tok in tokens:
            r = post(c, path, tok, **extra)
            assert r.status_code == 403, (action, tok)
            assert "Request refused" in r.text and "Nothing was changed" in r.text
    assert snapshot(dbfile) == before


def test_a_token_of_an_earlier_server_run_is_refused(cfg, conn, tmp_path, site):
    c, ids = site
    old = token(c, "/", f"/jobs/{ids[1]}/shortlist")
    restarted = client(cfg, tmp_path)                                             # a new app: a new secret
    assert post(restarted, f"/jobs/{ids[1]}/shortlist", old).status_code == 403
    assert status_of(tmp_path / "w.db", ids[1]) == "scored"
    assert post(c, f"/jobs/{ids[1]}/shortlist", old).status_code == 303          # the old one still works in its own run


def test_the_token_check_comes_before_everything_else(site, conn, dbfile):
    c, _ = site
    assert post(c, "/jobs/99999/shortlist", None, x="1").status_code == 403              # not even "no such job" is told without a token
    assert post(c, "/jobs/abc/shortlist", None, x="1").status_code == 404                # a path that is no job id at all
    assert post(c, "/jobs/99999/shortlist", c.app.state.csrf("shortlist:99999")).status_code == 303


def test_foreign_origin_or_referer_is_refused_even_with_a_valid_token(site, dbfile):
    c, ids = site
    path, tok = f"/jobs/{ids[0]}/shortlist", c.app.state.csrf(f"shortlist:{ids[0]}")
    before = snapshot(dbfile)
    bad = [{"Origin": "http://evil.example"}, {"Origin": "https://127.0.0.1"}, {"Origin": "null"}, {"Origin": ""},
           {"Origin": "http://127.0.0.1:9999"}, {"Origin": "http://127.0.0.1.evil.example"}, {"Origin": "http://localhost"},
           {"Referer": "http://evil.example/jobs/1"}, {"Referer": "http://127.0.0.1@evil.example/"}, {"Referer": "https://127.0.0.1/"},
           {"Referer": "http://127.0.0.1:9999/"}, {"Referer": "http://[::1"}, {"Referer": "file:///x"},
           {"Origin": "http://127.0.0.1", "Referer": "http://evil.example/"}, {"Origin": "http://evil.example", "Referer": "http://127.0.0.1/"}]
    for headers in bad:
        r = post(c, path, tok, headers=headers)
        assert r.status_code == 403, headers
    assert snapshot(dbfile) == before
    assert r.headers["content-security-policy"].startswith("default-src 'self'") and "access-control-allow-origin" not in r.headers


def test_the_own_origin_and_a_missing_origin_are_accepted(site, dbfile):
    c, ids = site
    ok = [{"Origin": "http://127.0.0.1"}, {"Referer": "http://127.0.0.1/jobs/1"}, {"Origin": "http://127.0.0.1", "Referer": "http://127.0.0.1/"}, None]
    for n, headers in enumerate(ok):
        job_id = ids[n % 3]
        r = post(c, f"/jobs/{job_id}/shortlist", c.app.state.csrf(f"shortlist:{job_id}"), headers=headers)
        assert r.status_code == 303, headers
    # with a port in the Host header the Origin must carry the same port
    r = post(c, f"/jobs/{ids[2]}/applied", c.app.state.csrf(f"applied:{ids[2]}"), headers={"Host": "localhost:8123", "Origin": "http://localhost:8123"})
    assert r.status_code == 303
    r = post(c, f"/jobs/{ids[2]}/applied", c.app.state.csrf(f"applied:{ids[2]}"), headers={"Host": "localhost:8123", "Origin": "http://localhost:8000"})
    assert r.status_code == 403


def test_a_browser_form_post_works_with_the_origin_the_browser_sends(site, dbfile):
    """With Referrer-Policy no-referrer a browser sends `Origin: null` on a form POST; with same-origin it sends the real origin."""
    c, ids = site
    assert c.get("/").headers["referrer-policy"] == "same-origin" and 'content="same-origin"' in c.get("/").text
    r = post(c, f"/jobs/{ids[0]}/shortlist", token(c, "/", f"/jobs/{ids[0]}/shortlist"), headers={"Origin": "http://127.0.0.1", "Referer": "http://127.0.0.1/"})
    assert r.status_code == 303 and status_of(dbfile, ids[0]) == "shortlisted"


def test_a_foreign_host_header_is_refused_on_post_too(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    tok = c.app.state.csrf(f"shortlist:{ids[0]}")
    for host in ("evil.example", "127.0.0.1.evil.example", "10.0.0.5:8000", ""):
        r = post(c, f"/jobs/{ids[0]}/shortlist", tok, headers={"Host": host})
        assert 400 <= r.status_code < 500, host
        assert r.status_code == 400 and "Bad Host" in r.text
    assert snapshot(dbfile) == before


def test_the_form_body_must_be_a_small_urlencoded_form(site, dbfile):
    c, ids = site
    path, tok = f"/jobs/{ids[0]}/shortlist", c.app.state.csrf(f"shortlist:{ids[0]}")
    before = snapshot(dbfile)
    assert c.post(path, json={"csrf": tok}, follow_redirects=False).status_code == 415
    assert c.post(path, content=f"csrf={tok}", headers={"Content-Type": "text/plain"}, follow_redirects=False).status_code == 415
    assert c.post(path, content=b"", follow_redirects=False).status_code == 415
    assert c.post(path, data={"csrf": tok, "pad": "x" * (web.MAX_BODY + 1)}, follow_redirects=False).status_code == 413
    assert c.post(path, content=b"csrf=\xff\xfe", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False).status_code == 400
    many = "&".join(f"f{i}=1" for i in range(50))
    assert c.post(path, content=many, headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False).status_code == 400
    assert snapshot(dbfile) == before
    assert c.post(path, content=f"csrf={tok}", headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}, follow_redirects=False).status_code == 303


def test_every_successful_post_is_a_303_redirect_to_a_page_of_the_app(cfg, conn, tmp_path):
    ids = imported(conn, cfg, scores=(90, 80, 70))
    c = client(cfg, tmp_path)
    responses = [act(c, ids[0], "shortlist"), act(c, ids[0], "applied"), act(c, ids[0], "approve"), act(c, ids[1], "shortlist"),
                 post(c, f"/jobs/{ids[2]}/reject", c.app.state.csrf(f"reject:{ids[2]}"), confirm="yes")]
    for r in responses:
        assert r.status_code == 303 and r.headers["location"].startswith("/") and not r.headers["location"].startswith("//") and r.text == ""
        assert r.headers["cache-control"] == "no-store"


def test_there_is_no_cors_and_no_preflight(site):
    c, ids = site
    r = c.options(f"/jobs/{ids[0]}/shortlist", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 405 and "access-control-allow-origin" not in r.headers          # no CORS: nothing answers a preflight
    r = c.options(f"/jobs/{ids[0]}/shortlist")
    assert r.status_code == 405 and "access-control-allow-origin" not in r.headers
    assert not any("cors" in m.cls.__name__.lower() for m in c.app.user_middleware)


# --- the database -----------------------------------------------------------------------------------

def test_a_busy_database_gives_a_message_not_a_500(site, conn, dbfile):
    c, ids = site
    c.app.state.open_rw = lambda: _quick(dbfile)
    conn.execute("BEGIN IMMEDIATE")                                              # the pipeline holds the write lock
    conn.execute("UPDATE jobs SET title = 'changing' WHERE id = ?", (ids[0],))
    try:
        for action, fields in (("shortlist", {}), ("reject", {"confirm": "yes"})):
            r = act(c, ids[0], action, **fields)
            assert r.status_code == 303
            assert "could not be written to" in seen(c.get(r.headers["location"]))
    finally:
        conn.rollback()
    assert status_of(dbfile, ids[0]) == "scored"


def test_approving_while_the_database_is_busy_gives_a_message_and_the_rows_stay_drafts(cfg, conn, tmp_path):
    ids = imported(conn, cfg)
    c = client(cfg, tmp_path)
    c.app.state.open_rw = lambda: _quick(tmp_path / "w.db")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE jobs SET title = 'changing' WHERE id = ?", (ids[0],))
    try:
        r = act(c, ids[0], "approve")
        assert r.status_code == 303 and "could not be written to" in seen(c.get(r.headers["location"]))
    finally:
        conn.rollback()
    assert doc_row(conn, ids[0], "cv")["status"] == "draft" and job_status(conn, ids[0]) == "scored"


def _quick(path):
    conn = web.open_writable(path)
    conn.execute("PRAGMA busy_timeout = 50")
    return conn


def test_the_write_connection_never_creates_a_database_and_never_migrates(cfg, tmp_path):
    missing = TestClient(web.create_app(cfg, tmp_path / "nothing.db"), base_url=HOST)
    r = post(missing, "/jobs/1/shortlist", missing.app.state.csrf("shortlist:1"))
    assert r.status_code == 503 and "does not exist yet" in r.text and not (tmp_path / "nothing.db").exists()
    old = tmp_path / "old.db"
    raw = sqlite3.connect(old)
    raw.execute("CREATE TABLE jobs (id INTEGER)")
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()
    c = TestClient(web.create_app(cfg, old), base_url=HOST)
    r = post(c, "/jobs/1/shortlist", c.app.state.csrf("shortlist:1"))
    assert r.status_code == 503 and "schema version 3" in r.text
    raw = sqlite3.connect(old)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
    raw.close()


def test_reads_still_use_the_read_only_connection(site):
    c, _ = site
    ro = c.app.state.open_db()
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("UPDATE jobs SET title = 'x'")
    finally:
        ro.close()


# --- binding ----------------------------------------------------------------------------------------

def test_the_server_is_started_on_a_local_address_only(cfg):
    calls = []
    web.serve(cfg, run=lambda app, **kw: calls.append(kw))
    web.serve(cfg, host="localhost", port=8123, run=lambda app, **kw: calls.append(kw))
    web.serve(cfg, host="::1", run=lambda app, **kw: calls.append(kw))
    assert {kw["host"] for kw in calls} <= set(web.ALLOWED_HOSTS) and len(calls) == 3
    for host in ("0.0.0.0", "::", "", "192.168.1.5", "127.0.0.1.evil.example", "LOCALHOST "):
        with pytest.raises(web.ConfigError):
            web.check_host(host)
    assert web.ALLOWED_HOSTS == ("127.0.0.1", "localhost", "::1")
