"""The web app, v0.49.0 (goal.md 5.7): undo of an approval of documents and of the statuses Shortlisted and Applied, with the same
write protection as chunk 2 (POST only, CSRF token per action and job, Host / Origin / Referer). T-WEB-53 and following in
docs/TEST_CASES.md.

Every test builds its own temporary database and approved folder; none uses the real config, the real database or the network.
Assertions look at exact markup (the CI runs on Linux, temporary paths can contain words like `approved`).
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import approval, db, documents, web
from app.config import load_config
from tests.test_approval import config_file, doc_row, files, imported, job_status, run, seeded       # noqa: F401
from tests.test_web import HOST, client, conn, cfg, site                                              # noqa: F401  (fixtures and helpers)
from tests.test_web_writes import act, history, post, seen, snapshot, status_of, token             # noqa: F401
from tests.test_web_writes import set_status as bare_status

UNDO_ROUTES = ("undo-approval", "undo-status")


@pytest.fixture
def dbfile(tmp_path):
    return tmp_path / "w.db"


@pytest.fixture
def world(cfg, conn, tmp_path):
    """Job 0: both documents approved (status `approved`); job 1: shortlisted by the web app; job 2: scored."""
    ids = imported(conn, cfg, scores=(90, 80, 70))
    approval.approve(conn, cfg, ids[0])
    c = client(cfg, tmp_path)
    assert act(c, ids[1], "shortlist").status_code == 303
    return c, ids


def undo_status(c, job_id, **fields):
    return act(c, job_id, "undo-status", **fields)


def undo_approval(c, job_id, **fields):
    return act(c, job_id, "undo-approval", **fields)


def message_after(c, r):
    return seen(c.get(r.headers["location"]))


# --- Undo of Shortlisted and Applied: the buttons ------------------------------------------------------------

def test_undo_is_offered_only_when_the_last_status_change_can_be_taken_back(site, conn):
    c, ids = site
    assert "undo-status" not in c.get("/").text and "undo-status" not in c.get(f"/jobs/{ids[0]}").text
    assert act(c, ids[0], "shortlist").status_code == 303
    detail = c.get(f"/jobs/{ids[0]}").text
    assert f'<form method="post" action="/jobs/{ids[0]}/undo-status">' in detail
    assert '<button type="submit" title="Set this job back from shortlisted to scored">Undo</button>' in detail
    top = c.get("/").text
    assert top.count(f'action="/jobs/{ids[0]}/undo-status"') == 1 and top.count("undo-status") == 1          # only on that job's card
    assert 'name="next" value="top"' in top[top.index(f'action="/jobs/{ids[0]}/undo-status"'):]
    assert token(c, f"/jobs/{ids[0]}", f"/jobs/{ids[0]}/undo-status") == c.app.state.csrf(f"undo-status:{ids[0]}")
    assert act(c, ids[0], "applied").status_code == 303                                                       # Applied: a link, not a form
    detail = c.get(f"/jobs/{ids[0]}").text
    assert f'action="/jobs/{ids[0]}/undo-status"' not in detail
    assert f'<a class="button" href="/jobs/{ids[0]}/undo-status?next=job" title="Set this job back from applied to shortlisted (asks first)">Undo&hellip;</a>' in detail
    assert f'/jobs/{ids[0]}/undo-status' not in c.get("/").text                                                 # v0.82.0: an applied job leaves the Top 10
    bare_status(conn, ids[0], "shortlisted")                                                                  # the history no longer matches the status
    assert "undo-status" not in c.get(f"/jobs/{ids[0]}").text and "undo-status" not in c.get("/").text
    assert "<script" not in detail.replace('<script src="/static/confirm.js" defer></script>', "").lower() and "style=" not in detail.lower() and "onclick" not in detail.lower()


@pytest.mark.parametrize("previous", ["scored", "not_selected", "awaiting_llm"])
def test_undo_shortlisted_goes_back_and_comes_back_to_the_page_it_was_clicked_on(site, conn, dbfile, previous):
    c, ids = site
    bare_status(conn, ids[1], previous)
    assert act(c, ids[1], "shortlist").status_code == 303
    r = post(c, f"/jobs/{ids[1]}/undo-status", token(c, "/", f"/jobs/{ids[1]}/undo-status"), next="top")
    assert r.status_code == 303 and r.headers["location"].startswith("/?flash=") and r.text == ""
    assert status_of(dbfile, ids[1]) == previous
    assert history(conn, ids[1])[-2:] == [(previous, "shortlisted", "web"), ("shortlisted", previous, "web undo")]
    page = c.get(r.headers["location"])
    assert f"Job {ids[1]} (Analyst 2, Acme) is back to {previous} (it was shortlisted)." in seen(page) and 'class="notice ok"' in page.text
    assert "is back to" not in c.get(r.headers["location"]).text                                              # shown once
    assert f'action="/jobs/{ids[1]}/undo-status"' not in c.get("/").text                                      # one step only: nothing left to undo
    assert status_of(dbfile, ids[0]) == "scored" and status_of(dbfile, ids[2]) == "scored"


@pytest.mark.parametrize("previous", ["shortlisted", "approved"])
def test_undo_applied_asks_first_then_goes_back(world, conn, dbfile, previous):
    c, ids = world
    job = ids[1] if previous == "shortlisted" else ids[0]
    assert act(c, job, "applied").status_code == 303 and status_of(dbfile, job) == "applied"
    page = c.get(f"/jobs/{job}/undo-status?next=top")
    assert page.status_code == 200
    b = page.text
    assert "<h1>Set this job back?</h1>" in b and f"<code>applied</code> now. This sets it back to <code>{previous}</code>" in b
    assert f'<form method="post" action="/jobs/{job}/undo-status" class="confirm">' in b and 'name="confirm" value="yes"' in b
    assert 'name="next" value="top"' in b and f"Yes, set it back to {previous}</button>" in b and "<script" not in b.lower()
    assert status_of(dbfile, job) == "applied"                                                                # the page changed nothing
    before = snapshot(dbfile)
    tok = token(c, f"/jobs/{job}/undo-status", f"/jobs/{job}/undo-status")
    for fields in ({}, {"confirm": "no"}, {"confirm": ""}, {"confirm": "YES"}):
        r = post(c, f"/jobs/{job}/undo-status", tok, next="top", **fields)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{job}/undo-status?next=top&flash=")
        assert "Not confirmed. Nothing was changed." in seen(c.get(r.headers["location"]))
        assert snapshot(dbfile) == before
    r = post(c, f"/jobs/{job}/undo-status", tok, confirm="yes", next="job")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{job}?flash=")
    assert status_of(dbfile, job) == previous and history(conn, job)[-1] == ("applied", previous, "web undo")
    assert f"is back to {previous} (it was applied)." in seen(c.get(r.headers["location"]))


def test_undo_status_refuses_a_status_that_cannot_be_set_back(site, conn, dbfile):
    c, ids = site
    for status in db.STATUSES:
        if status in web.UNDO_STATUSES:
            continue
        bare_status(conn, ids[2], status)
        before = snapshot(dbfile)
        r = undo_status(c, ids[2], confirm="yes")
        assert r.status_code == 303, status
        page = c.get(r.headers["location"])
        assert f"Job {ids[2]} has the status '{status}'; only a job that is shortlisted, applied, interview, offer, accepted, rejected or withdrawn can be set back here. Nothing was changed." in seen(page)
        assert 'class="notice refused"' in page.text and snapshot(dbfile) == before, status
        assert "undo-status" not in c.get(f"/jobs/{ids[2]}").text, status
    page = c.get(f"/jobs/{ids[2]}/undo-status")                                                               # the page says it too and has no form
    assert page.status_code == 200 and 'class="notice refused"' in page.text and 'name="confirm"' not in page.text


def test_undo_status_refuses_when_the_last_history_row_is_not_that_change(site, conn, dbfile):
    c, ids = site
    bare_status(conn, ids[0], "shortlisted")                                                                  # a status without any history row
    before = snapshot(dbfile)
    r = undo_status(c, ids[0])
    assert (f"Job {ids[0]} is 'shortlisted', but its last recorded status change was not the change to 'shortlisted', "
            "so the app does not know what to go back to. Nothing was changed.") in message_after(c, r)
    assert snapshot(dbfile) == before
    db.set_status(conn, ids[1], "shortlisted")                                                                # the last row leads to another status
    db.set_status(conn, ids[1], "approved")
    bare_status(conn, ids[1], "applied")
    r = undo_status(c, ids[1], confirm="yes")
    assert "last recorded status change was not the change to 'applied'" in message_after(c, r) and status_of(dbfile, ids[1]) == "applied"


def test_undo_status_refuses_when_there_is_no_earlier_status(site, conn, dbfile):
    c, ids = site
    bare_status(conn, ids[0], "shortlisted")
    with conn:
        conn.execute("INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) VALUES (?, NULL, 'shortlisted', 'x', '2026-11-02T08:00:00Z')", (ids[0],))
    before = snapshot(dbfile)
    r = undo_status(c, ids[0])
    assert f"Job {ids[0]} has no earlier status recorded, so there is nothing to go back to. Nothing was changed." in message_after(c, r)
    assert snapshot(dbfile) == before and "undo-status" not in c.get(f"/jobs/{ids[0]}").text


@pytest.mark.parametrize("previous, status, with_score", [
    ("new", "shortlisted", True), ("approved", "shortlisted", True), ("applied", "shortlisted", True), ("interview", "shortlisted", True),
    ("location_failed", "shortlisted", True), ("awaiting_llm", "shortlisted", False),
    ("scored", "applied", True), ("not_selected", "applied", True), ("interview", "applied", True), ("new", "applied", True)])
def test_undo_status_refuses_a_previous_status_the_web_app_would_not_allow(site, conn, dbfile, previous, status, with_score):
    c, ids = site
    if not with_score:
        with conn:
            conn.execute("DELETE FROM scores WHERE job_id = ?", (ids[2],))
    db.set_status(conn, ids[2], previous)
    db.set_status(conn, ids[2], status)
    before = snapshot(dbfile)
    r = undo_status(c, ids[2], confirm="yes")
    assert r.status_code == 303
    assert (f"Job {ids[2]} was '{previous}' before, and the web app does not allow '{status}' from there, so it will not go back to it. "
            "Nothing was changed.") in message_after(c, r)
    assert snapshot(dbfile) == before and "undo-status" not in c.get(f"/jobs/{ids[2]}").text


def test_undo_is_one_step_only(world, conn, dbfile):
    c, ids = world
    job = ids[1]
    assert act(c, job, "applied").status_code == 303
    assert undo_status(c, job, confirm="yes").status_code == 303 and status_of(dbfile, job) == "shortlisted"
    assert history(conn, job)[-1] == ("applied", "shortlisted", "web undo")
    before = snapshot(dbfile)
    r = undo_status(c, job)                                                                                  # a second undo is refused, not followed back
    assert f"The last change on job {job} was already an undo; the app goes back one step only. Nothing was changed." in message_after(c, r)
    assert snapshot(dbfile) == before and "undo-status" not in c.get(f"/jobs/{job}").text
    assert undo_status(c, ids[2]).status_code == 303                                                         # a scored job: nothing to undo
    assert status_of(dbfile, ids[2]) == "scored"


def test_undo_to_approved_needs_both_documents_still_approved(world, conn, cfg, dbfile):
    c, ids = world
    assert act(c, ids[0], "applied").status_code == 303                                                      # approved -> applied
    approval.undo(conn, cfg, ids[0], ["cv"])                                                                  # the job stays applied, the CV is a draft
    assert status_of(dbfile, ids[0]) == "applied" and "undo-status" not in c.get(f"/jobs/{ids[0]}").text
    before = snapshot(dbfile)
    r = undo_status(c, ids[0], confirm="yes")
    assert (f"Job {ids[0]} was 'approved' before, but its documents are no longer both approved, so it cannot go back to that status. "
            "Approve them again first. Nothing was changed.") in message_after(c, r)
    assert snapshot(dbfile) == before
    approval.approve(conn, cfg, ids[0], ["cv"])
    assert f'href="/jobs/{ids[0]}/undo-status?next=job"' in c.get(f"/jobs/{ids[0]}").text
    assert undo_status(c, ids[0], confirm="yes").status_code == 303 and status_of(dbfile, ids[0]) == "approved"


def test_undo_status_of_a_job_that_is_not_open_any_more(world, conn):
    c, ids = world
    db.archive_job(conn, ids[1], "filter", "seniority")
    r = undo_status(c, ids[1])
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs")
    assert "not open any more" in message_after(c, r) and c.get(f"/jobs/{ids[1]}/undo-status").status_code == 404


def test_undo_status_while_the_database_is_busy_gives_a_message(world, conn, dbfile):
    from tests.test_web_writes import _quick
    c, ids = world
    c.app.state.open_rw = lambda: _quick(dbfile)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE jobs SET title = 'changing' WHERE id = ?", (ids[2],))
    try:
        r = undo_status(c, ids[1])
        assert r.status_code == 303 and "could not be written to" in message_after(c, r)
        r = undo_approval(c, ids[0], confirm="yes")
        assert r.status_code == 303 and "could not be written to" in message_after(c, r)
    finally:
        conn.rollback()
    assert status_of(dbfile, ids[1]) == "shortlisted" and doc_row(conn, ids[0], "cv")["status"] == "approved"


def test_the_undo_confirmation_page_of_a_status_is_read_only_and_the_job_can_be_shortlisted_again(site, conn, dbfile):
    c, ids = site
    act(c, ids[0], "shortlist")
    before = snapshot(dbfile)
    assert c.get(f"/jobs/{ids[0]}/undo-status?next=job").status_code == 200 and c.get(f"/jobs/{ids[0]}/undo-status?next=//evil.example").status_code == 200
    assert 'name="next" value="job"' in c.get(f"/jobs/{ids[0]}/undo-status?next=//evil.example").text
    assert snapshot(dbfile) == before and c.get("/jobs/abc/undo-status").status_code == 404 and c.get("/jobs/99999/undo-status").status_code == 404
    assert undo_status(c, ids[0]).status_code == 303 and act(c, ids[0], "shortlist").status_code == 303
    assert status_of(dbfile, ids[0]) == "shortlisted" and 'action="/jobs/%d/undo-status"' % ids[0] in c.get(f"/jobs/{ids[0]}").text


# --- Undo of an approval -----------------------------------------------------------------------------------------

def test_the_detail_page_offers_the_undo_of_an_approval_only_where_something_is_approved(cfg, conn, tmp_path):
    ids = imported(conn, cfg)
    c = client(cfg, tmp_path)
    assert "undo-approval" not in c.get(f"/jobs/{ids[0]}").text
    approval.approve(conn, cfg, ids[0], ["cv"])
    b = c.get(f"/jobs/{ids[0]}").text
    assert f'<form method="get" action="/jobs/{ids[0]}/undo-approval" class="approve">' in b and 'Take back approval&hellip;</button>' in b
    form = b.split(f'action="/jobs/{ids[0]}/undo-approval"', 1)[1].split("</form>")[0]
    assert '<option value="cv">the CV</option>' in form and "cover_letter" not in form and 'value="both"' not in form     # one approved: no "both"
    approval.approve(conn, cfg, ids[0])
    form = c.get(f"/jobs/{ids[0]}").text.split(f'action="/jobs/{ids[0]}/undo-approval"', 1)[1].split("</form>")[0]
    assert '<option value="both" selected>both documents</option>' in form and '<option value="cover_letter">the cover letter</option>' in form
    assert '<option value="cv">the CV</option>' in form and 'name="csrf"' not in form                          # a GET form: it changes nothing
    approval.undo(conn, cfg, ids[0])
    assert "undo-approval" not in c.get(f"/jobs/{ids[0]}").text


def test_the_confirmation_page_of_an_approval_changes_nothing_and_says_what_happens(world, dbfile, cfg):
    c, ids = world
    before, kept = snapshot(dbfile), files(documents.approved_dir(cfg))
    assert kept
    b = c.get(f"/jobs/{ids[0]}/undo-approval?kind=both&next=top").text
    assert "<h1>Take back the approval?</h1>" in b and "<strong>cover letter and cv</strong>" in b
    assert "The job is <code>approved</code> now, so it goes back to the status it had before the approval." in " ".join(b.split())
    assert f'<form method="post" action="/jobs/{ids[0]}/undo-approval" class="confirm">' in b and 'name="kind" value="both"' in b
    assert 'name="next" value="top"' in b and 'name="confirm" value="yes"' in b and "Yes, take back the approval</button>" in b
    assert f"python -m app approve-document --job {ids[0]} --undo" in b and "<script" not in b.lower()
    one = c.get(f"/jobs/{ids[0]}/undo-approval?kind=cv").text
    assert "<strong>cv</strong>" in one and 'name="kind" value="cv"' in one and 'name="next" value="job"' in one
    odd = c.get(f"/jobs/{ids[0]}/undo-approval?kind=evil%22%3E&next=//evil.example").text
    assert 'name="kind" value="both"' in odd and 'name="next" value="job"' in odd and "evil" not in odd
    assert snapshot(dbfile) == before and files(documents.approved_dir(cfg)) == kept
    nothing = c.get(f"/jobs/{ids[2]}/undo-approval").text                                                     # no approved document on this job
    assert 'class="notice refused"' in nothing and "There is no approved document to take back" in nothing and 'name="confirm"' not in nothing
    assert c.get("/jobs/abc/undo-approval").status_code == 404 and c.get("/jobs/99999/undo-approval").status_code == 404


def test_the_confirmation_page_says_when_the_job_stays_where_it_is(world, dbfile):
    c, ids = world
    assert act(c, ids[0], "applied").status_code == 303
    b = " ".join(c.get(f"/jobs/{ids[0]}/undo-approval").text.split())
    assert "The job is <code>applied</code> and stays so: this only unfreezes the documents." in b


def test_undo_without_confirmation_changes_nothing(world, dbfile, cfg):
    c, ids = world
    before, kept = snapshot(dbfile), files(documents.approved_dir(cfg))
    tok = token(c, f"/jobs/{ids[0]}/undo-approval?kind=both", f"/jobs/{ids[0]}/undo-approval")
    for fields in ({}, {"confirm": "no"}, {"confirm": ""}, {"confirm": "YES"}):
        r = post(c, f"/jobs/{ids[0]}/undo-approval", tok, kind="cv", next="top", **fields)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}/undo-approval?kind=cv&next=top&flash=")
        assert "Not confirmed. Nothing was changed." in seen(c.get(r.headers["location"]))
        assert snapshot(dbfile) == before and files(documents.approved_dir(cfg)) == kept


def test_undo_both_approvals_from_the_web(world, conn, cfg, dbfile):
    c, ids = world
    assert job_status(conn, ids[0]) == "approved"
    r = post(c, f"/jobs/{ids[0]}/undo-approval", token(c, f"/jobs/{ids[0]}/undo-approval", f"/jobs/{ids[0]}/undo-approval"), kind="both", confirm="yes", next="job")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}?flash=") and r.text == ""
    for kind in ("cover_letter", "cv"):
        row = doc_row(conn, ids[0], kind)
        assert row["status"] == "draft" and row["approved_at"] is None and row["approved_pdf_path"] is None
    assert files(documents.approved_dir(cfg)) == [] and files(documents.documents_dir(cfg))                  # the copies are gone, the originals are not
    assert job_status(conn, ids[0]) == "scored"
    page = c.get(r.headers["location"])
    t = seen(page)
    assert f"job {ids[0]} cover_letter: approval taken back, the document is a draft again" in t and f"job {ids[0]} cv: approval taken back" in t
    assert f"job {ids[0]} is now 'scored'" in t and "2 document(s) taken back to draft, 0 already approved, 0 refused" in t
    assert 'class="notice ok"' in page.text and "undo-approval" not in page.text and f'action="/jobs/{ids[0]}/approve"' in page.text


@pytest.mark.parametrize("kind", ["cover_letter", "cv"])
def test_undo_one_approval_from_the_web(world, conn, cfg, kind):
    c, ids = world
    other = "cv" if kind == "cover_letter" else "cover_letter"
    r = undo_approval(c, ids[0], kind=kind, confirm="yes", next="top")
    assert r.status_code == 303 and r.headers["location"].startswith("/?flash=")
    assert doc_row(conn, ids[0], kind)["status"] == "draft" and doc_row(conn, ids[0], other)["status"] == "approved"
    assert len(files(documents.approved_dir(cfg))) == 1
    assert job_status(conn, ids[0]) == "scored"                                                              # the job is `approved` only while both are
    t = seen(c.get(r.headers["location"]))
    assert f"job {ids[0]} {kind}: approval taken back" in t and "1 document(s) taken back to draft, 0 already approved, 0 refused" in t
    b = c.get(f"/jobs/{ids[0]}").text
    assert f'<option value="{other}">' in b.split("undo-approval", 1)[1] and f'<option value="{kind}">' not in b.split("undo-approval", 1)[1]
    r = undo_approval(c, ids[0], kind=kind, confirm="yes")                                                   # again: refused, like the command
    page = c.get(r.headers["location"])
    assert f"REFUSED: job {ids[0]} {kind}: it is not approved (status draft); nothing to undo" in seen(page) and 'class="notice refused"' in page.text
    assert doc_row(conn, ids[0], other)["status"] == "approved"


def test_undo_of_an_approval_after_applied_keeps_the_status_like_the_command(world, conn, cfg, dbfile):
    c, ids = world
    assert act(c, ids[0], "applied").status_code == 303
    r = undo_approval(c, ids[0], confirm="yes")
    assert r.status_code == 303 and status_of(dbfile, ids[0]) == "applied"
    assert {doc_row(conn, ids[0], k)["status"] for k in ("cv", "cover_letter")} == {"draft"}
    t = seen(c.get(r.headers["location"]))
    assert "2 document(s) taken back to draft" in t and "is now '" not in t


def test_undo_of_an_approval_refusals_show_the_reason(world, conn, cfg, dbfile):
    c, ids = world
    for job in (ids[1], ids[2]):                                                                              # these jobs have no documents at all
        r = undo_approval(c, job, kind="cv", confirm="yes")
        page = c.get(r.headers["location"])
        assert r.status_code == 303 and f"REFUSED: job {job} cv: job {job} has no stored cv" in seen(page) and 'class="notice refused"' in page.text
    with conn:
        conn.execute("UPDATE documents SET approved_pdf_path = '../outside.pdf' WHERE job_id = ? AND kind = 'cv'", (ids[0],))
    before = snapshot(dbfile)
    r = undo_approval(c, ids[0], kind="cv", confirm="yes")
    assert "lies outside the approved folder; nothing was removed" in seen(c.get(r.headers["location"]))
    assert doc_row(conn, ids[0], "cv")["status"] == "approved" and snapshot(dbfile)["documents"] == before["documents"]
    for kind in ("letter", "", "cv,cover_letter", "BOTH"):
        r = undo_approval(c, ids[0], kind=kind, confirm="yes")
        assert r.status_code == 303 and "Choose the cover letter, the CV or both. Nothing was changed." in message_after(c, r)


def test_undo_of_an_approval_for_a_job_that_is_not_open_any_more(world, conn):
    c, ids = world
    db.archive_job(conn, ids[0], "filter", "seniority")
    r = undo_approval(c, ids[0], confirm="yes")
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs") and "not open any more" in message_after(c, r)
    assert c.get(f"/jobs/{ids[0]}/undo-approval").status_code == 404


def test_undo_through_the_web_equals_undo_at_the_command_line(cfg, conn, tmp_path):
    """The same fixture twice: one copy undone by `approval.undo` (what `approve-document --undo` calls), one through the web form."""
    ids = imported(conn, cfg)
    db.set_status(conn, ids[0], "shortlisted")
    approval.approve(conn, cfg, ids[0])
    twin = tmp_path / "twin.db"
    conn.close()
    raw = sqlite3.connect(tmp_path / "w.db")
    target = sqlite3.connect(twin)
    raw.backup(target)
    raw.close()
    target.close()
    cli_conn = db.connect(twin)
    expected = approval.outcome_lines(approval.undo(cli_conn, cfg, ids[0], None), undo=True)
    cli_state = ([tuple(r) for r in cli_conn.execute("SELECT kind, status, approved_at, approved_pdf_path, pdf_path FROM documents ORDER BY kind")],
                 job_status(cli_conn, ids[0]), [tuple(r) for r in cli_conn.execute("SELECT from_status, to_status, note FROM status_history ORDER BY id")])
    cli_conn.close()
    c = client(cfg, tmp_path)
    r = undo_approval(c, ids[0], kind="both", confirm="yes")
    raw = sqlite3.connect(tmp_path / "w.db")
    web_state = ([tuple(x) for x in raw.execute("SELECT kind, status, approved_at, approved_pdf_path, pdf_path FROM documents ORDER BY kind")],
                 raw.execute("SELECT status FROM jobs WHERE id = ?", (ids[0],)).fetchone()[0],
                 [tuple(x) for x in raw.execute("SELECT from_status, to_status, note FROM status_history ORDER BY id")])
    raw.close()
    assert web_state == cli_state and web_state[1] == "shortlisted"
    shown = seen(c.get(r.headers["location"]))
    for _, text in expected:
        assert text in shown, text


def test_the_command_line_undo_still_works_on_what_the_web_approved(config_file, capsys):
    c, ids = seeded(config_file)
    web_app = TestClient(web.create_app(c), base_url=HOST)
    assert act(web_app, ids[0], "approve").status_code == 303
    capsys.readouterr()
    assert run(config_file, "approve-document", "--job", str(ids[0]), "--undo", "--kind", "cv") == 0
    out = capsys.readouterr().out
    assert "cv: approval taken back" in out and f"job {ids[0]} is now 'scored'" in out and "1 document(s) taken back to draft" in out
    assert run(config_file, "approve-document", "--job", str(ids[0]), "--undo", "--kind", "cv") == 1
    assert "not approved" in capsys.readouterr().err
    r = undo_approval(web_app, ids[0], kind="cover_letter", confirm="yes")                                    # and the web undoes what the command is left with
    assert r.status_code == 303 and "approval taken back" in seen(web_app.get(r.headers["location"]))
    conn = db.connect(c["paths"]["database"])
    assert {doc_row(conn, ids[0], k)["status"] for k in ("cv", "cover_letter")} == {"draft"}
    conn.close()
    assert run(config_file, "approve-document", "--job", str(ids[0])) == 0                                    # approve at the command line, undo on the web
    assert undo_approval(web_app, ids[0], confirm="yes").status_code == 303


# --- write protection of the two new routes ----------------------------------------------------------------------------

def paths(ids):
    return {"undo-approval": ids[0], "undo-status": ids[1]}


def test_get_never_changes_data_on_the_undo_routes(world, dbfile, cfg):
    c, ids = world
    before, kept = snapshot(dbfile), files(documents.approved_dir(cfg))
    for route, job in paths(ids).items():
        for url in (f"/jobs/{job}/{route}", f"/jobs/{job}/{route}?csrf={c.app.state.csrf(f'{route}:{job}')}&confirm=yes&kind=both&next=top"):
            assert c.get(url).status_code == 200, url
        c.head(f"/jobs/{job}/{route}")
        assert c.options(f"/jobs/{job}/{route}", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"}).status_code == 405
        for verb in ("put", "patch", "delete"):
            assert getattr(c, verb)(f"/jobs/{job}/{route}").status_code == 405, (verb, route)
    assert snapshot(dbfile) == before and files(documents.approved_dir(cfg)) == kept


def test_a_post_without_a_token_or_with_a_wrong_one_is_403_on_the_undo_routes(world, dbfile, cfg):
    c, ids = world
    before, kept = snapshot(dbfile), files(documents.approved_dir(cfg))
    for route, job in paths(ids).items():
        other_job = ids[2]
        tokens = [None, "", "x", "0" * 64, c.app.state.csrf(f"{route}:{other_job}"), c.app.state.csrf(f"other:{job}"), "é" * 64, "a" * 5000,
                  c.app.state.csrf(f"shortlist:{job}"), c.app.state.csrf(f"approve:{job}"), c.app.state.csrf(f"reject:{job}"),
                  c.app.state.csrf(f"{'undo-status' if route == 'undo-approval' else 'undo-approval'}:{job}")]
        for tok in tokens:
            r = post(c, f"/jobs/{job}/{route}", tok, confirm="yes", kind="both")
            assert r.status_code == 403, (route, tok)
            assert "Request refused" in r.text and "Nothing was changed" in r.text
    assert snapshot(dbfile) == before and files(documents.approved_dir(cfg)) == kept


def test_the_tokens_of_the_undo_routes_are_not_valid_for_the_other_routes(world, dbfile):
    c, ids = world
    for route in UNDO_ROUTES:
        for other in ("shortlist", "applied", "approve", "reject"):
            assert post(c, f"/jobs/{ids[2]}/{other}", c.app.state.csrf(f"{route}:{ids[2]}"), confirm="yes").status_code == 403
    assert c.app.state.csrf(f"undo-status:{ids[1]}") != c.app.state.csrf(f"undo-approval:{ids[1]}")


def test_a_token_of_an_earlier_server_run_is_refused_on_the_undo_routes(world, cfg, tmp_path, dbfile):
    c, ids = world
    old = token(c, f"/jobs/{ids[1]}", f"/jobs/{ids[1]}/undo-status")
    restarted = client(cfg, tmp_path)
    assert post(restarted, f"/jobs/{ids[1]}/undo-status", old).status_code == 403 and status_of(dbfile, ids[1]) == "shortlisted"
    assert post(c, f"/jobs/{ids[1]}/undo-status", old).status_code == 303 and status_of(dbfile, ids[1]) == "scored"


def test_the_token_check_comes_before_everything_else_on_the_undo_routes(world):
    c, _ = world
    for route in UNDO_ROUTES:
        assert post(c, f"/jobs/99999/{route}", None, confirm="yes").status_code == 403
        assert post(c, f"/jobs/abc/{route}", None, confirm="yes").status_code == 404
        assert post(c, f"/jobs/99999/{route}", c.app.state.csrf(f"{route}:99999"), confirm="yes").status_code == 303


def test_foreign_origin_referer_or_host_is_refused_on_the_undo_routes(world, dbfile, cfg):
    c, ids = world
    before, kept = snapshot(dbfile), files(documents.approved_dir(cfg))
    bad = [{"Origin": "http://evil.example"}, {"Origin": "https://127.0.0.1"}, {"Origin": "null"}, {"Origin": ""}, {"Origin": "http://127.0.0.1:9999"},
           {"Origin": "http://127.0.0.1.evil.example"}, {"Referer": "http://evil.example/jobs/1"}, {"Referer": "http://127.0.0.1@evil.example/"},
           {"Referer": "https://127.0.0.1/"}, {"Referer": "file:///x"}, {"Origin": "http://127.0.0.1", "Referer": "http://evil.example/"}]
    for route, job in paths(ids).items():
        tok = c.app.state.csrf(f"{route}:{job}")
        for headers in bad:
            assert post(c, f"/jobs/{job}/{route}", tok, headers=headers, confirm="yes", kind="both").status_code == 403, (route, headers)
        for host in ("evil.example", "127.0.0.1.evil.example", "10.0.0.5:8000", ""):
            r = post(c, f"/jobs/{job}/{route}", tok, headers={"Host": host}, confirm="yes", kind="both")
            assert r.status_code == 400 and "Bad Host" in r.text, (route, host)
    assert snapshot(dbfile) == before and files(documents.approved_dir(cfg)) == kept
    # the real origin of a browser form post is accepted
    r = post(c, f"/jobs/{ids[1]}/undo-status", c.app.state.csrf(f"undo-status:{ids[1]}"), headers={"Origin": "http://127.0.0.1", "Referer": "http://127.0.0.1/"})
    assert r.status_code == 303 and status_of(dbfile, ids[1]) == "scored"


def test_the_form_body_of_the_undo_routes_must_be_a_small_urlencoded_form(world, dbfile, cfg):
    c, ids = world
    before, kept = snapshot(dbfile), files(documents.approved_dir(cfg))
    for route, job in paths(ids).items():
        path, tok = f"/jobs/{job}/{route}", c.app.state.csrf(f"{route}:{job}")
        assert c.post(path, json={"csrf": tok}, follow_redirects=False).status_code == 415
        assert c.post(path, content=f"csrf={tok}", headers={"Content-Type": "text/plain"}, follow_redirects=False).status_code == 415
        assert c.post(path, data={"csrf": tok, "confirm": "yes", "pad": "x" * (web.MAX_BODY + 1)}, follow_redirects=False).status_code == 413
        assert c.post(path, content=b"csrf=\xff\xfe", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False).status_code == 400
    assert snapshot(dbfile) == before and files(documents.approved_dir(cfg)) == kept


def test_every_successful_undo_is_a_303_redirect_to_a_page_of_the_app(world):
    c, ids = world
    for r in (undo_status(c, ids[1], next="top"), undo_approval(c, ids[0], confirm="yes", next="list")):
        assert r.status_code == 303 and r.headers["location"].startswith("/") and not r.headers["location"].startswith("//") and r.text == ""
        assert r.headers["cache-control"] == "no-store" and "access-control-allow-origin" not in r.headers
        assert r.headers["content-security-policy"].startswith("default-src 'self'") and "script-src 'none'" in r.headers["content-security-policy"] and "unsafe-inline" not in r.headers["content-security-policy"] and "unsafe-eval" not in r.headers["content-security-policy"]


def test_next_is_never_a_free_url_on_the_undo_routes(world):
    c, ids = world
    for nxt in ("https://evil.example/", "//evil.example", "/jobs/1", "", "TOP"):
        r = undo_status(c, ids[1], next=nxt)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[1]}?flash=") and "evil" not in r.headers["location"]
        act(c, ids[1], "shortlist")
        r = undo_approval(c, ids[0], next=nxt)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}/undo-approval?kind=both&next=job&flash=")


def test_double_submit_of_an_undo_changes_things_once(world, conn, dbfile):
    c, ids = world
    tok = token(c, f"/jobs/{ids[1]}", f"/jobs/{ids[1]}/undo-status")
    first = post(c, f"/jobs/{ids[1]}/undo-status", tok, next="job")
    second = post(c, f"/jobs/{ids[1]}/undo-status", tok, next="job")
    assert first.status_code == second.status_code == 303 and status_of(dbfile, ids[1]) == "scored"
    assert history(conn, ids[1]).count(("shortlisted", "scored", "web undo")) == 1
    assert "only a job that is shortlisted, applied, interview, offer, accepted, rejected or withdrawn can be set back here" in message_after(c, second)
    tok = c.app.state.csrf(f"undo-approval:{ids[0]}")
    first = post(c, f"/jobs/{ids[0]}/undo-approval", tok, confirm="yes", kind="both")
    second = post(c, f"/jobs/{ids[0]}/undo-approval", tok, confirm="yes", kind="both")
    assert first.status_code == second.status_code == 303
    assert "2 document(s) taken back to draft" in message_after(c, first)
    t = message_after(c, second)
    assert "0 document(s) taken back to draft, 0 already approved, 2 refused" in t and "it is not approved (status draft); nothing to undo" in t
    assert history(conn, ids[0]).count(("approved", "scored", "approval of a document undone")) == 1


def test_the_new_pages_load_no_script_and_no_inline_style(world):
    c, ids = world
    act(c, ids[1], "applied")
    for url in (f"/jobs/{ids[0]}/undo-approval", f"/jobs/{ids[1]}/undo-status", f"/jobs/{ids[0]}", f"/jobs/{ids[1]}", "/"):
        r = c.get(url)
        low = (r.text.replace('<script src="/static/select.js" defer></script>', "") if url == "/" else r.text.replace('<script src="/static/confirm.js" defer></script>', "")).lower()      # the job page loads confirm.js (v0.99.0)
        assert r.status_code == 200 and "<script" not in low and "style=" not in low and "onclick" not in low and "javascript:" not in low, url
        assert ("script-src 'self'" if url == "/" or url.count("/") == 2 else "script-src 'none'") in r.headers["content-security-policy"] and "unsafe-inline" not in r.headers["content-security-policy"] and "unsafe-eval" not in r.headers["content-security-policy"] and "form-action 'self'" in r.headers["content-security-policy"]
