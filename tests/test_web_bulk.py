"""The bulk status route, v0.96.0 (goal.md 5.7): `GET /bulk-status` is the read-only confirmation page of a selection of jobs,
`POST /bulk-status` changes them after it (one token per action, each job in its own transaction, the note `web bulk`).
Every test builds its own temporary database; none uses the real config, the real database or the network.
"""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, statusflow, web
from tests.test_web import HOST, client, conn, cfg, site                                               # noqa: F401  (fixtures and helpers)
from tests.test_web_login import clock, login, pwhash                                                  # noqa: F401  (fixtures)
from tests.test_web_writes import act, history, post, seen, snapshot, status_of                         # noqa: F401
from tests.test_web_writes import set_status as bare_status


@pytest.fixture
def dbfile(tmp_path):
    return tmp_path / "w.db"


def token(c, action):
    return c.app.state.csrf(f"bulk:{action}")


def bulk(c, action, ids, nxt="list", page=None, tok=None, headers=None, **fields):
    data = {"action": action, "ids": ",".join(map(str, ids)), "next": nxt, "csrf": token(c, action) if tok is None else tok}
    if page is not None:
        data["page"] = str(page)
    data.update(fields)
    return c.post("/bulk-status", data=data, headers=headers, follow_redirects=False)


def flash_of(c, r):
    """The messages of a redirect, read once from any page (the key is all that matters, so pages that come later work too)."""
    m = re.search(r"flash=([\w-]+)", r.headers["location"])
    assert m, r.headers["location"]
    return seen(c.get("/?flash=" + m.group(1)))


def walk(conn, job_id, *steps):
    """Move a job through the statuses with history rows (like the app does)."""
    for step in steps:
        db.set_status(conn, job_id, step, note="web")


def set_up(conn, ids, status):
    """Put jobs into a status through a valid path, so that history and undo make sense."""
    paths = {"rejected": ("shortlisted", "applied", "rejected"), "withdrawn": ("shortlisted", "applied", "withdrawn"),
             "scored": (), "shortlisted": ("shortlisted",), "applied": ("shortlisted", "applied"),
             "interview": ("shortlisted", "applied", "interview"), "offer": ("shortlisted", "applied", "interview", "offer"),
             "accepted": ("shortlisted", "applied", "interview", "offer", "accepted")}
    for i in ids:
        walk(conn, i, *paths[status])


# --- the confirmation page -----------------------------------------------------------------------------

def test_the_confirm_page_lists_each_job_as_will_change_or_skipped_and_writes_nothing(site, conn, dbfile):
    c, ids = site
    walk(conn, ids[1], "shortlisted")
    before = snapshot(dbfile)
    r = c.get(f"/bulk-status?action=shortlist&id={ids[0]}&id={ids[1]}&id=99999&next=list&page=2")
    assert r.status_code == 200 and snapshot(dbfile) == before
    body = seen(r)
    assert "Shortlist: 1 of 3 jobs?" in body and "will become shortlisted" in body
    assert f"Job {ids[1]} is already shortlisted. Nothing was changed." in body and "Job 99999 is not open any more" in body
    assert 'action="/bulk-status"' in r.text and 'name="confirm" value="yes"' in r.text
    assert f'name="ids" value="{ids[0]},{ids[1]},99999"' in r.text and 'name="next" value="list"' in r.text and 'name="page" value="2"' in r.text
    assert f'name="csrf" value="{token(c, "shortlist")}"' in r.text and 'href="/jobs?page=2"' in r.text
    assert "script-src 'none'" in r.headers["content-security-policy"] and "<script" not in r.text.lower()


def test_the_confirm_page_of_reject_has_the_comment_field_and_the_warning_for_applied_jobs(site, conn):
    c, ids = site
    set_up(conn, [ids[0]], "applied")
    r = c.get(f"/bulk-status?action=reject&id={ids[0]}&id={ids[1]}&next=list")
    assert 'name="comment"' in r.text and f'maxlength="{web.COMMENT_MAX}"' in r.text and "marked as applied" in r.text and 'class="reject"' in r.text
    other = c.get(f"/bulk-status?action=shortlist&id={ids[1]}&next=list")
    assert 'name="comment"' not in other.text and "marked as applied" not in other.text


def test_the_confirm_page_of_undo_names_the_status_it_goes_back_to(site, conn):
    c, ids = site
    set_up(conn, [ids[0]], "interview")
    seen_page = seen(c.get(f"/bulk-status?action=undo&id={ids[0]}&id={ids[1]}&next=applied"))
    assert "will be set back to applied" in seen_page and "can be set back here" in seen_page            # job 2: scored, cannot be undone


def test_the_confirm_page_refuses_a_bad_request_with_400_and_no_form(site):
    c, ids = site
    for url in ("/bulk-status", f"/bulk-status?action=nope&id={ids[0]}&next=list", f"/bulk-status?action=shortlist&id={ids[0]}&next=nowhere",
                f"/bulk-status?action=offer&id={ids[0]}&next=applied", "/bulk-status?action=shortlist&id=abc&next=list",
                f"/bulk-status?action=shortlist&id={ids[0]}&id={ids[1]}&next=job"):
        r = c.get(url)
        assert r.status_code == 400 and 'class="notice refused"' in r.text and 'method="post"' not in r.text, url


def test_the_confirm_page_escapes_titles_and_companies(site, conn):
    c, ids = site
    with conn:
        conn.execute("UPDATE jobs SET title = ?, company = ? WHERE id = ?", ('<script>alert(1)</script>', '"><img src=x onerror=1>', ids[0]))
    r = c.get(f"/bulk-status?action=shortlist&id={ids[0]}&next=list")
    assert "<script>alert" not in r.text and "<img" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


# --- the change: every action and page -----------------------------------------------------------------

CASES = [  # action, status the jobs start in, status after, page, history note
    ("shortlist", "scored", "shortlisted", "top", "web bulk"),
    ("shortlist", "scored", "shortlisted", "list", "web bulk"),
    ("applied", "shortlisted", "applied", "list", "web bulk"),
    ("applied", "shortlisted", "applied", "top", "web bulk"),
    ("interview", "applied", "interview", "applied", "web bulk"),
    ("employer_rejected", "applied", "rejected", "applied", "web bulk"),
    ("withdrawn", "applied", "withdrawn", "applied", "web bulk"),
    ("offer", "interview", "offer", "interviews", "web bulk"),
    ("employer_rejected", "interview", "rejected", "interviews", "web bulk"),
    ("withdrawn", "interview", "withdrawn", "interviews", "web bulk"),
    ("withdrawn", "offer", "withdrawn", "offers", "web bulk"),
    ("employer_rejected", "offer", "rejected", "offers", "web bulk"),
    ("undo", "shortlisted", "scored", "list", "web undo"),
    ("undo", "applied", "shortlisted", "applied", "web undo"),
    ("undo", "interview", "applied", "interviews", "web undo"),
    ("undo", "offer", "interview", "offers", "web undo"),
    ("undo", "rejected", "applied", "rejected", "web undo"),
    ("undo", "withdrawn", "applied", "withdrawn", "web undo"),
]


@pytest.mark.parametrize("action,start,end,page,note", CASES)
def test_a_bulk_change_per_action_and_page(site, conn, dbfile, action, start, end, page, note):
    c, ids = site
    chosen = [ids[0], ids[2]]
    set_up(conn, chosen, start)
    before = {i: history(conn, i) for i in chosen}
    r = bulk(c, action, chosen, nxt=page, page=1, confirm="yes")
    assert r.status_code == 303 and r.headers["location"].startswith(web.BULK_PATHS[page] + "?flash=")
    for i in chosen:
        assert status_of(dbfile, i) == end
        assert history(conn, i) == before[i] + [(start, end, note)]
    assert status_of(dbfile, ids[1]) == "scored"                                                        # not selected, not touched
    message = flash_of(c, r)
    assert "2 jobs" in message and "Analyst 1" in message and "Analyst 3" in message


def test_bulk_reject_archives_each_job_with_the_comment(site, conn, dbfile):
    c, ids = site
    r = bulk(c, "reject", [ids[0], ids[1]], nxt="top", confirm="yes", comment="  not  my\n field ")
    assert r.status_code == 303 and r.headers["location"].startswith("/?flash=")
    rows = conn.execute("SELECT rejection_stage, reason_code, comment, original_job_id FROM job_archive ORDER BY original_job_id").fetchall()
    assert [tuple(x) for x in rows] == [("user", "user_rejected", "not my field", ids[0]), ("user", "user_rejected", "not my field", ids[1])]
    assert status_of(dbfile, ids[0]) is None and status_of(dbfile, ids[1]) is None and status_of(dbfile, ids[2]) == "scored"
    message = flash_of(c, r)
    assert "2 jobs rejected and moved to the archive" in message and "restore-job" in message


def test_bulk_reject_from_missing_and_the_job_page(site, conn, dbfile):
    c, ids = site
    r = bulk(c, "reject", [ids[0]], nxt="missing", confirm="yes")
    assert r.headers["location"].startswith("/missing?flash=") and status_of(dbfile, ids[0]) is None
    r = bulk(c, "reject", [ids[1]], nxt="job", confirm="yes")
    assert r.headers["location"].startswith("/jobs?flash=")                                             # the job page would be a 404 now


def test_next_job_needs_exactly_one_id_and_goes_back_to_the_job(site, conn, dbfile):
    c, ids = site
    r = bulk(c, "shortlist", [ids[0], ids[1]], nxt="job", confirm="yes")
    assert r.headers["location"].startswith("/jobs?flash=") and status_of(dbfile, ids[0]) == "scored"
    assert "exactly one job" in flash_of(c, r)
    r = bulk(c, "shortlist", [ids[0]], nxt="job", confirm="yes")
    assert r.headers["location"].startswith(f"/jobs/{ids[0]}?flash=") and status_of(dbfile, ids[0]) == "shortlisted"
    set_up(conn, [ids[1]], "interview")                                                                  # a stage action from the job page
    r = bulk(c, "offer", [ids[1]], nxt="job", confirm="yes")
    assert r.headers["location"].startswith(f"/jobs/{ids[1]}?flash=") and status_of(dbfile, ids[1]) == "offer"


def test_the_page_number_is_kept(site, conn):
    c, ids = site
    r = bulk(c, "shortlist", [ids[0]], nxt="list", page=3, confirm="yes")
    assert r.headers["location"].startswith("/jobs?page=3&flash=")
    r = bulk(c, "undo", [ids[0]], nxt="list", page=1, confirm="yes")
    assert r.headers["location"].startswith("/jobs?flash=")
    r = bulk(c, "shortlist", [ids[0]], nxt="top", page=5, confirm="yes")
    assert r.headers["location"].startswith("/?flash=")                                                 # the Top 10 has no pages
    for bad in ("0", "-1", "x", "9" * 12, "²"):
        assert bulk(c, "shortlist", [ids[1]], nxt="list", page=bad, confirm="yes").headers["location"].startswith("/jobs?flash=")


def test_a_mixed_selection_changes_the_allowed_jobs_and_names_the_refused_ones(site, conn, dbfile):
    c, ids = site
    walk(conn, ids[1], "shortlisted")
    before = snapshot(dbfile)
    r = bulk(c, "shortlist", [ids[0], ids[1], 99999], confirm="yes")
    assert status_of(dbfile, ids[0]) == "shortlisted" and status_of(dbfile, ids[2]) == "scored"
    message = flash_of(c, r)
    assert "1 job is now shortlisted" in message and f"Job {ids[1]} is already shortlisted" in message and "Job 99999 is not open any more" in message
    assert snapshot(dbfile) != before
    r = bulk(c, "shortlist", [ids[1]], confirm="yes")                                                   # nothing to do: only a refusal, no history row
    assert history(conn, ids[1])[-1] == ("scored", "shortlisted", "web") and "already shortlisted" in flash_of(c, r)


def test_more_than_ten_refusals_are_cut_off(site, conn):
    c, ids = site
    with conn:
        for i in range(12):
            conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES (?, 'x', 'y', 'offer', 'a', 'a')", (f"k{i}",))
    rows = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE status = 'offer'")]
    message = flash_of(c, bulk(c, "interview", [ids[0], *rows], nxt="applied", confirm="yes"))
    assert message.count("Nothing was changed.") == web.BULK_LISTED and "and 3 more were skipped" in message


def test_undo_twice_is_refused_the_second_time(site, conn, dbfile):
    c, ids = site
    set_up(conn, [ids[0]], "interview")
    assert bulk(c, "undo", [ids[0]], nxt="interviews", confirm="yes").status_code == 303 and status_of(dbfile, ids[0]) == "applied"
    r = bulk(c, "undo", [ids[0]], nxt="applied", confirm="yes")
    assert status_of(dbfile, ids[0]) == "applied" and "already an undo" in flash_of(c, r)


def test_a_bulk_of_one_job_leaves_the_same_end_state_as_the_single_route(cfg, conn, tmp_path):
    from tests.test_report import job, pipeline
    ids, _ = pipeline(conn, cfg, [job(1), job(2)], [55, 90])
    c = client(cfg, tmp_path)
    assert act(c, ids[0], "shortlist").status_code == 303
    assert bulk(c, "shortlist", [ids[1]], confirm="yes").status_code == 303
    assert history(conn, ids[0])[-1] == ("scored", "shortlisted", "web") and history(conn, ids[1])[-1] == ("scored", "shortlisted", "web bulk")
    assert len(history(conn, ids[0])) == len(history(conn, ids[1]))
    one = [tuple(r) for r in conn.execute("SELECT status, title FROM jobs ORDER BY id").fetchall()]
    assert [x[0] for x in one] == ["shortlisted", "shortlisted"]
    assert act(c, ids[0], "applied").status_code == 303 and bulk(c, "applied", [ids[1]], confirm="yes").status_code == 303
    assert history(conn, ids[0])[-1][:2] == history(conn, ids[1])[-1][:2] == ("shortlisted", "applied")
    assert act(c, ids[0], "undo-status", confirm="yes").status_code == 303 and bulk(c, "undo", [ids[1]], confirm="yes").status_code == 303
    assert history(conn, ids[0])[-1] == history(conn, ids[1])[-1] == ("applied", "shortlisted", "web undo")


def test_after_a_bulk_applied_the_top_10_hides_the_jobs(site, conn):
    c, ids = site
    assert "Analyst 2" in c.get("/").text
    set_up(conn, [ids[1]], "shortlisted")
    bulk(c, "applied", [ids[1]], nxt="top", confirm="yes")
    top = c.get("/").text
    assert "Analyst 2" not in top and "Analyst 1" in top


# --- not confirmed, bad input --------------------------------------------------------------------------

def test_without_confirm_yes_nothing_changes_and_the_confirm_page_comes_back(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    for fields in ({}, {"confirm": "no"}, {"confirm": ""}, {"confirm": "YES"}, {"comment": "x"}):
        r = bulk(c, "shortlist", [ids[0], ids[1]], nxt="list", page=2, **fields)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/bulk-status?action=shortlist&id={ids[0]}&id={ids[1]}&next=list&page=2&flash=")
        assert "Not confirmed. Nothing was changed." in flash_of(c, r)
    assert snapshot(dbfile) == before
    assert c.get(bulk(c, "shortlist", [ids[0]]).headers["location"]).status_code == 200


def test_a_long_reject_comment_is_refused_and_nothing_is_archived(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    r = bulk(c, "reject", [ids[0]], confirm="yes", comment="x" * (web.COMMENT_MAX + 1))
    assert r.headers["location"].startswith("/bulk-status?action=reject") and "longer than" in flash_of(c, r) and snapshot(dbfile) == before


def test_the_ids_are_checked(site, conn, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    for raw, text in (("", "No job is selected"), (" , ,", "No job is selected"), ("abc", "not a job number"), ("-1", "not a job number"),
                      ("0", "not a job number"), ("1" * 13, "not a job number"), ("1,2;3", "not a job number"), ("²", "not a job number"),
                      (",".join(str(i) for i in range(1, 202)), "Too many jobs")):
        r = c.post("/bulk-status", data={"action": "shortlist", "ids": raw, "next": "list", "csrf": token(c, "shortlist"), "confirm": "yes"}, follow_redirects=False)
        assert r.status_code == 303 and text in flash_of(c, r), raw
    assert snapshot(dbfile) == before
    r = c.post("/bulk-status", data={"action": "shortlist", "next": "list", "csrf": token(c, "shortlist"), "confirm": "yes"}, follow_redirects=False)
    assert "No job is selected" in flash_of(c, r)
    r = bulk(c, "shortlist", [ids[0], ids[0], ids[0]], confirm="yes")                                  # duplicates are collapsed
    assert "1 job is now" in flash_of(c, r) and [h for h in history(conn, ids[0]) if h[2] == "web bulk"] == [("scored", "shortlisted", "web bulk")]
    exactly = ",".join(str(i) for i in range(1000, 1200))                                               # 200 ids are accepted
    r = c.post("/bulk-status", data={"action": "shortlist", "ids": exactly, "next": "list", "csrf": token(c, "shortlist"), "confirm": "yes"}, follow_redirects=False)
    assert "Too many" not in flash_of(c, r)


def test_an_action_the_page_does_not_offer_is_refused(site, conn, dbfile):
    c, ids = site
    set_up(conn, [ids[0]], "applied")
    before = snapshot(dbfile)
    for action, page in (("shortlist", "applied"), ("offer", "applied"), ("interview", "list"), ("shortlist", "missing"), ("applied", "missing"),
                         ("undo", "missing"), ("reject", "interviews"), ("reject", "rejected"), ("employer_rejected", "top"), ("offer", "withdrawn")):
        r = bulk(c, action, [ids[0]], nxt=page, confirm="yes")
        assert r.status_code == 303 and "is not offered on this page" in flash_of(c, r), (action, page)
    assert snapshot(dbfile) == before


def test_an_unknown_action_or_page_and_a_free_url_change_nothing(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    r = bulk(c, "delete", [ids[0]], confirm="yes", tok=token(c, "delete"))
    assert r.status_code == 400
    for nxt in ("nowhere", "http://evil.example/", "//evil.example", "/jobs", ""):
        r = bulk(c, "shortlist", [ids[0]], nxt=nxt, confirm="yes")
        assert r.status_code == 303 and r.headers["location"].startswith("/") and not r.headers["location"].startswith("//") and status_of(dbfile, ids[0]) == "scored", nxt
    assert snapshot(dbfile) == before


# --- the token and the origin --------------------------------------------------------------------------

def test_the_token_is_per_action_and_must_be_right(site, dbfile, cfg, tmp_path):
    c, ids = site
    before = snapshot(dbfile)
    other_app = client(cfg, tmp_path)
    for tok in (None, "", "x" * 64, token(c, "applied"), c.app.state.csrf(f"shortlist:{ids[0]}"), token(c, "shortlist")[:-1], token(c, "shortlist").upper(),
                other_app.app.state.csrf("bulk:shortlist")):
        data = {"action": "shortlist", "ids": str(ids[0]), "next": "list", "confirm": "yes"}
        if tok is not None:
            data["csrf"] = tok
        r = c.post("/bulk-status", data=data, follow_redirects=False)
        assert r.status_code == 403, tok
    assert snapshot(dbfile) == before and status_of(dbfile, ids[0]) == "scored"


def test_the_token_is_checked_before_the_database_is_opened(cfg, tmp_path):
    c = TestClient(web.create_app(cfg, tmp_path / "nothing.db"), base_url=HOST)
    r = c.post("/bulk-status", data={"action": "shortlist", "ids": "1", "next": "list", "csrf": "bad", "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 403 and not (tmp_path / "nothing.db").exists()
    r = bulk(c, "shortlist", [1], confirm="yes")
    assert r.status_code == 503 and not (tmp_path / "nothing.db").exists()                               # a right token reaches the database check


def test_a_foreign_origin_or_host_is_refused_even_with_a_valid_token(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Referer": "http://evil.example/"}, {"Origin": ""}):
        assert bulk(c, "shortlist", [ids[0]], confirm="yes", headers=headers).status_code == 403, headers
    for host in ("evil.example", "10.0.0.5:8000", ""):
        assert bulk(c, "shortlist", [ids[0]], confirm="yes", headers={"Host": host}).status_code == 400, host
    assert snapshot(dbfile) == before
    assert bulk(c, "shortlist", [ids[0]], confirm="yes", headers={"Origin": "http://127.0.0.1"}).status_code == 303


def test_the_body_must_be_a_small_urlencoded_form_and_only_get_and_post_exist(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    tok = token(c, "shortlist")
    assert c.post("/bulk-status", json={"csrf": tok}, follow_redirects=False).status_code == 415
    assert c.post("/bulk-status", content=f"csrf={tok}", headers={"Content-Type": "text/plain"}, follow_redirects=False).status_code == 415
    assert c.post("/bulk-status", data={"csrf": tok, "pad": "x" * (web.MAX_BODY + 1)}, follow_redirects=False).status_code == 413
    many = "&".join(f"f{i}=1" for i in range(50))
    assert c.post("/bulk-status", content=many, headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False).status_code == 400
    for verb in ("put", "patch", "delete"):
        assert getattr(c, verb)("/bulk-status").status_code == 405
    assert snapshot(dbfile) == before


def test_get_never_writes_and_a_selection_is_not_a_change(site, dbfile):
    c, ids = site
    before = snapshot(dbfile)
    for action in ("shortlist", "applied", "undo", "reject", "interview"):
        c.get(f"/bulk-status?action={action}&id={ids[0]}&id={ids[1]}&next=list&confirm=yes&csrf={token(c, action)}")
    assert snapshot(dbfile) == before


def test_with_the_login_on_a_post_without_the_cookie_is_refused(cfg, conn, tmp_path, login):
    from tests.test_report import job, pipeline
    ids, _ = pipeline(conn, cfg, [job(1)], [55])
    c = TestClient(web.create_app(cfg, tmp_path / "w.db", login=login), base_url=HOST, follow_redirects=False)
    r = bulk(c, "shortlist", [ids[0]], confirm="yes")
    assert r.status_code == 403 and "Login required" in r.text and conn.execute("SELECT status FROM jobs").fetchone()[0] == "scored"
    assert c.get(f"/bulk-status?action=shortlist&id={ids[0]}&next=list").status_code == 303            # the GET goes to the login


# --- a busy database -----------------------------------------------------------------------------------

def _quick(path):
    conn = web.open_writable(path)
    conn.execute("PRAGMA busy_timeout = 50")
    return conn


def test_a_busy_database_part_way_reports_what_was_done_and_leaves_the_rest(site, conn, dbfile, monkeypatch):
    c, ids = site
    c.app.state.open_rw = lambda: _quick(dbfile)
    locker = sqlite3.connect(dbfile, check_same_thread=False, isolation_level=None)         # the request runs in another thread
    real = db.set_status

    def set_then_lock(conn_, job_id, to_status, note=None):
        real(conn_, job_id, to_status, note=note)
        if not locker.in_transaction:                                  # after the first job another writer takes the lock
            locker.execute("BEGIN IMMEDIATE")

    monkeypatch.setattr(db, "set_status", set_then_lock)
    try:
        r = bulk(c, "shortlist", ids, confirm="yes")
    finally:
        if locker.in_transaction:
            locker.execute("ROLLBACK")
        locker.close()
    assert r.status_code == 303
    done = [i for i in ids if status_of(dbfile, i) == "shortlisted"]
    assert done == [ids[0]] and all(status_of(dbfile, i) == "scored" for i in ids[1:])
    message = flash_of(c, r)
    assert "1 job is now shortlisted" in message and "Jobs" in message and "could not be written to" in message
    assert str(ids[1]) in message and str(ids[2]) in message


def _lock_after_first(monkeypatch, dbfile, target, name):
    """Make `target.name` take the write lock with another connection right after its first job is done."""
    locker = sqlite3.connect(dbfile, check_same_thread=False, isolation_level=None)
    real = getattr(target, name)

    def wrapper(*args, **kw):
        out = real(*args, **kw)
        if not locker.in_transaction:
            locker.execute("BEGIN IMMEDIATE")
        return out

    monkeypatch.setattr(target, name, wrapper)
    return locker


@pytest.mark.parametrize("action,start,end", [("reject", "scored", None), ("undo", "shortlisted", "scored")])
def test_a_busy_database_part_way_through_reject_and_undo(site, conn, dbfile, monkeypatch, action, start, end):
    c, ids = site
    set_up(conn, ids, start)
    c.app.state.open_rw = lambda: _quick(dbfile)
    if action == "reject":
        locker = _lock_after_first(monkeypatch, dbfile, web.rejection, "reject_job")
    else:
        locker = _lock_after_first(monkeypatch, dbfile, db, "set_status")
    try:
        r = bulk(c, action, ids, confirm="yes")
    finally:
        if locker.in_transaction:
            locker.execute("ROLLBACK")
        locker.close()
    assert r.status_code == 303
    message = flash_of(c, r)
    assert "could not be written to" in message and str(ids[1]) in message and str(ids[2]) in message
    if action == "reject":
        assert conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 1
        assert all(status_of(dbfile, i) == "scored" for i in ids[1:])
    else:
        assert status_of(dbfile, ids[0]) == end and all(status_of(dbfile, i) == start for i in ids[1:])


def test_a_busy_database_from_the_start_changes_nothing(site, conn, dbfile):
    c, ids = site
    c.app.state.open_rw = lambda: _quick(dbfile)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE jobs SET title = 'changing' WHERE id = ?", (ids[0],))
    try:
        r = bulk(c, "reject", ids, confirm="yes")
        assert r.status_code == 303 and "could not be written to" in flash_of(c, r)
    finally:
        conn.rollback()
    assert all(status_of(dbfile, i) == "scored" for i in ids) and conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 0


def test_the_bulk_page_has_no_script_and_the_post_has_the_same_headers(site):
    c, ids = site
    r = c.get(f"/bulk-status?action=shortlist&id={ids[0]}&next=list")
    assert r.headers["content-security-policy"].startswith("default-src 'self'; script-src 'none'")
    assert "unsafe-inline" not in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"
    p = bulk(c, "shortlist", [ids[0]], confirm="yes")
    assert p.headers["content-security-policy"] == r.headers["content-security-policy"]


def test_accept_through_the_bulk_route_and_its_undo(site, conn, dbfile):
    c, ids = site
    set_up(conn, ids[:2], "offer")
    conn.commit()
    r = bulk(c, "accept", ids[:2], nxt="offers", confirm="yes")
    assert r.status_code == 303 and [status_of(dbfile, i) for i in ids[:2]] == ["accepted", "accepted"]
    r = bulk(c, "accept", ids[:1], nxt="offers", confirm="yes")                       # already accepted: refused, nothing changed
    assert status_of(dbfile, ids[0]) == "accepted"
    r = bulk(c, "undo", ids[:1], nxt="accepted", confirm="yes")
    assert r.status_code == 303 and status_of(dbfile, ids[0]) == "offer" and status_of(dbfile, ids[1]) == "accepted"


# --- the first interview round with the status change (v0.99.0) -----------------------------------------------------

def _rounds(dbfile, jid=None):
    raw = sqlite3.connect(dbfile)
    try:
        sql = "SELECT job_id, starts_at, kind, place, notes FROM interviews" + (" WHERE job_id = ?" if jid else "")
        return raw.execute(sql, (jid,) if jid else ()).fetchall()
    finally:
        raw.close()


def test_interview_with_a_date_adds_the_round_in_the_same_change(site, conn, dbfile):
    c, ids = site
    set_up(conn, [ids[0]], "applied")
    r = bulk(c, "interview", [ids[0]], nxt="applied", confirm="yes", day="2026-12-01", clock="09:30", kind="video")
    assert r.status_code == 303 and status_of(dbfile, ids[0]) == "interview"
    assert _rounds(dbfile) == [(ids[0], "2026-12-01T08:30:00Z", "video", None, None)]
    assert "Interview round on 2026-12-01 09:30 added" in flash_of(c, r)


def test_interview_without_a_date_adds_no_round_and_the_kind_defaults_to_phone(site, conn, dbfile):
    c, ids = site
    set_up(conn, [ids[0], ids[1]], "applied")
    bulk(c, "interview", [ids[0]], nxt="applied", confirm="yes", day="", clock="", kind="video")
    assert status_of(dbfile, ids[0]) == "interview" and _rounds(dbfile) == []
    bulk(c, "interview", [ids[1]], nxt="applied", confirm="yes", day="2026-12-01", clock="09:30")
    assert [x[2] for x in _rounds(dbfile)] == ["phone"]


@pytest.mark.parametrize("fields", [{"day": "2026-12-01", "clock": ""}, {"day": "", "clock": "09:30"}, {"day": "2026-13-01", "clock": "09:30"},
                                    {"day": "2026-12-01", "clock": "9h"}, {"day": "1999-01-01", "clock": "09:30"}, {"day": "2026-12-01", "clock": "09:30", "kind": "fax"}])
def test_a_bad_or_half_date_refuses_the_status_change_too(site, conn, dbfile, fields):
    c, ids = site
    set_up(conn, [ids[0]], "applied")
    before = history(conn, ids[0])
    r = bulk(c, "interview", [ids[0]], nxt="applied", confirm="yes", **fields)
    assert r.status_code == 303 and r.headers["location"].startswith("/bulk-status?action=interview")
    assert "Nothing was changed" in flash_of(c, r)
    assert status_of(dbfile, ids[0]) == "applied" and history(conn, ids[0]) == before and _rounds(dbfile) == []


def test_a_date_for_two_jobs_is_refused_and_nothing_changes(site, conn, dbfile):
    c, ids = site
    set_up(conn, [ids[0], ids[1]], "applied")
    r = bulk(c, "interview", [ids[0], ids[1]], nxt="applied", confirm="yes", day="2026-12-01", clock="09:30")
    assert "one job only" in flash_of(c, r)
    assert status_of(dbfile, ids[0]) == "applied" and status_of(dbfile, ids[1]) == "applied" and _rounds(dbfile) == []


def test_a_refused_status_writes_no_round(site, conn, dbfile):
    c, ids = site                                                                     # the job is not applied: interview is refused
    r = bulk(c, "interview", [ids[0]], nxt="applied", confirm="yes", day="2026-12-01", clock="09:30")
    assert status_of(dbfile, ids[0]) == "scored" and _rounds(dbfile) == []
    assert "Nothing was changed" in flash_of(c, r) or "applied" in flash_of(c, r)


def test_a_date_is_ignored_for_other_actions(site, conn, dbfile):
    c, ids = site
    set_up(conn, [ids[0]], "interview")
    bulk(c, "offer", [ids[0]], nxt="interviews", confirm="yes", day="2026-12-01", clock="09:30")
    assert status_of(dbfile, ids[0]) == "offer" and _rounds(dbfile) == []


def test_a_busy_database_writes_neither_the_status_nor_the_round(site, conn, dbfile):
    c, ids = site
    set_up(conn, [ids[0]], "applied")
    c.app.state.open_rw = lambda: _quick(dbfile)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE jobs SET title = 'changing' WHERE id = ?", (ids[1],))
    try:
        bulk(c, "interview", [ids[0]], nxt="applied", confirm="yes", day="2026-12-01", clock="09:30")
    finally:
        conn.rollback()
    assert status_of(dbfile, ids[0]) == "applied" and _rounds(dbfile) == []


def test_undoing_the_interview_status_keeps_the_round(site, conn, dbfile):
    c, ids = site
    set_up(conn, [ids[0]], "applied")
    bulk(c, "interview", [ids[0]], nxt="applied", confirm="yes", day="2026-12-01", clock="09:30")
    bulk(c, "undo", [ids[0]], nxt="interviews", confirm="yes")
    assert status_of(dbfile, ids[0]) == "applied" and len(_rounds(dbfile)) == 1


def test_the_confirmation_page_has_the_round_fields_for_one_interview_job_only(site, conn):
    c, ids = site
    set_up(conn, ids[:2], "applied")
    one = c.get(f"/bulk-status?action=interview&id={ids[0]}&next=applied").text
    two = c.get(f"/bulk-status?action=interview&id={ids[0]}&id={ids[1]}&next=applied").text
    other = c.get(f"/bulk-status?action=offer&id={ids[0]}&next=interviews").text
    assert 'name="day"' in one and 'name="clock"' in one and '<option value="phone">' in one and one.index('value="phone"') < one.index('value="video"')
    assert 'name="day"' not in two and 'name="day"' not in other
