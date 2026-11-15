"""Interview rounds and notes (v0.98.0, migration 25): the module, the job page forms, archive and restore, merge-duplicates and the CLI list.
Every test builds its own temporary database; the texts are made up."""
import json
import logging
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import cli, db, interviews, merge, web
from app.interviews import Refused
from tests.test_web import HOST, client, conn, cfg, site                                  # noqa: F401  (fixtures and helpers)
from tests.test_web_writes import _quick, act, dbfile, seen, snapshot, status_of          # noqa: F401
from tests.test_merge_duplicates import env, run as merge_run, pair                       # noqa: F401  (fixtures and helpers)

ZURICH = "Europe/Zurich"


def rounds_form(day="2026-12-01", clock="09:30", kind="video", place="Teams", notes="Ask about the team"):
    return {"day": day, "clock": clock, "kind": kind, "place": place, "notes": notes}


def count(path, table, job_id=None):
    raw = sqlite3.connect(path)
    try:
        sql = f"SELECT COUNT(*) FROM {table}" + (" WHERE job_id = ?" if job_id else "")
        return raw.execute(sql, (job_id,) if job_id else ()).fetchone()[0]
    finally:
        raw.close()


# --- the module -----------------------------------------------------------------------------------------------------

def test_the_time_is_read_in_zurich_and_stored_as_utc():
    assert interviews.to_utc("2026-12-01", "09:30", ZURICH) == "2026-12-01T08:30:00Z"          # winter: UTC+1
    assert interviews.to_utc("2026-07-01", "09:30", ZURICH) == "2026-07-01T07:30:00Z"          # summer: UTC+2
    assert interviews.to_local("2026-07-01T07:30:00Z", ZURICH) == ("2026-07-01", "09:30")


@pytest.mark.parametrize("day,clock", [("", "09:30"), ("2026-12-01", ""), ("2026-13-01", "09:30"), ("2026-12-01", "25:00"), ("1.12.2026", "09:30")])
def test_a_bad_date_or_time_is_refused(day, clock):
    with pytest.raises(Refused):
        interviews.to_utc(day, clock, ZURICH)


def test_a_time_the_clock_change_skips_is_refused():
    with pytest.raises(Refused, match="does not exist"):
        interviews.to_utc("2027-03-28", "02:30", ZURICH)


def test_rounds_are_numbered_by_start_and_notes_keep_the_status(conn):
    jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('a|x', 'T', 'C', 'interview', 't', 't')").lastrowid
    second = interviews.add_round(conn, jid, "2026-12-08", "10:00", "onsite", "Zurich", "", ZURICH)
    first = interviews.add_round(conn, jid, "2026-12-01", "09:30", "phone", "", "", ZURICH)
    assert [(r["number"], r["id"], r["place"]) for r in interviews.list_rounds(conn, jid, ZURICH)] == [(1, first, ""), (2, second, "Zurich")]
    assert interviews.delete_round(conn, jid, first) and not interviews.delete_round(conn, jid, first)
    assert [r["number"] for r in interviews.list_rounds(conn, jid, ZURICH)] == [1]                # no gap: the number is the position
    nid = interviews.add_note(conn, jid, "  first call went well ")
    assert interviews.list_notes(conn, jid, ZURICH)[0]["status_at"] == "interview" and interviews.list_notes(conn, jid, ZURICH)[0]["text"] == "first call went well"
    assert not interviews.delete_note(conn, jid + 1, nid) and interviews.delete_note(conn, jid, nid)


@pytest.mark.parametrize("call", [
    lambda c, j: interviews.add_round(c, j, "2026-12-01", "09:30", "fax", "", "", ZURICH),
    lambda c, j: interviews.add_round(c, j, "2026-12-01", "09:30", "video", "p" * 301, "", ZURICH),
    lambda c, j: interviews.add_round(c, j, "2026-12-01", "09:30", "video", "", "n" * 2001, ZURICH),
    lambda c, j: interviews.add_round(c, j, "2026-12-01", "09:30", "video", "a\nb", "", ZURICH),
    lambda c, j: interviews.add_note(c, j, "   "),
    lambda c, j: interviews.add_note(c, j, "x" * 2001),
    lambda c, j: interviews.add_note(c, j, "a\x00b"),
])
def test_bad_input_is_refused_and_nothing_is_written(conn, call):
    jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at) VALUES ('a|x', 'T', 'C', 't', 't')").lastrowid
    with pytest.raises(Refused):
        call(conn, jid)
    assert conn.execute("SELECT (SELECT COUNT(*) FROM interviews) + (SELECT COUNT(*) FROM job_notes)").fetchone()[0] == 0


def test_a_job_that_is_not_open_gets_nothing(conn):
    assert interviews.add_round(conn, 99, "2026-12-01", "09:30", "video", "", "", ZURICH) is None and interviews.add_note(conn, 99, "x") is None


# --- the web forms --------------------------------------------------------------------------------------------------

def post_round(c, jid, **over):
    fields = rounds_form(**over)
    return c.post(f"/jobs/{jid}/interviews", data={**fields, "csrf": c.app.state.csrf(f"interviews:{jid}")}, follow_redirects=False)


def post_note(c, jid, text="Call back on Monday"):
    return c.post(f"/jobs/{jid}/notes", data={"text": text, "csrf": c.app.state.csrf(f"notes:{jid}")}, follow_redirects=False)


def test_a_round_and_a_note_are_added_shown_and_deleted(site, conn, dbfile):
    c, ids = site
    r = post_round(c, ids[0])
    assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}?flash=")
    raw = sqlite3.connect(dbfile)
    assert raw.execute("SELECT starts_at, kind, place FROM interviews").fetchall() == [("2026-12-01T08:30:00Z", "video", "Teams")]
    rid = raw.execute("SELECT id FROM interviews").fetchone()[0]
    raw.close()
    assert post_note(c, ids[0]).status_code == 303
    page = c.get(f"/jobs/{ids[0]}").text
    assert "Interview rounds" in page and "2026-12-01" in page and "09:30" in page and "Teams" in page and "Call back on Monday" in page
    nid = sqlite3.connect(dbfile).execute("SELECT id FROM job_notes").fetchone()[0]
    r = c.post(f"/jobs/{ids[0]}/interviews/{rid}/delete", data={"csrf": c.app.state.csrf(f"interview-delete:{ids[0]}"), "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and count(dbfile, "interviews") == 0
    r = c.post(f"/jobs/{ids[0]}/notes/{nid}/delete", data={"csrf": c.app.state.csrf(f"note-delete:{ids[0]}"), "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and count(dbfile, "job_notes") == 0


@pytest.mark.parametrize("path", ["interviews", "notes", "interviews/1/delete", "notes/1/delete"])
def test_a_missing_or_wrong_token_and_a_foreign_origin_change_nothing(site, dbfile, path):
    c, ids = site
    before = snapshot(dbfile)
    assert c.post(f"/jobs/{ids[0]}/{path}", data={**rounds_form(), "text": "x"}).status_code == 403
    assert c.post(f"/jobs/{ids[0]}/{path}", data={**rounds_form(), "text": "x", "csrf": "0" * 64}).status_code == 403
    other = c.app.state.csrf(f"shortlist:{ids[0]}")                                            # the token of another action
    assert c.post(f"/jobs/{ids[0]}/{path}", data={**rounds_form(), "text": "x", "csrf": other}).status_code == 403
    tok = c.app.state.csrf(("interviews:", "notes:", "interview-delete:", "note-delete:")[["interviews", "notes", "interviews/1/delete", "notes/1/delete"].index(path)] + str(ids[0]))
    assert c.post(f"/jobs/{ids[0]}/{path}", data={**rounds_form(), "text": "x", "csrf": tok}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert snapshot(dbfile) == before and count(dbfile, "interviews") == 0 and count(dbfile, "job_notes") == 0


def test_the_token_of_one_job_does_not_work_on_another(site, dbfile):
    c, ids = site
    r = c.post(f"/jobs/{ids[1]}/notes", data={"text": "x", "csrf": c.app.state.csrf(f"notes:{ids[0]}")})
    assert r.status_code == 403 and count(dbfile, "job_notes") == 0


@pytest.mark.parametrize("over", [{"kind": "fax"}, {"day": "2026-02-30"}, {"clock": "9h"}, {"day": ""}, {"place": "p" * 301}, {"notes": "n" * 2001}])
def test_a_bad_round_is_refused_with_a_message(site, dbfile, over):
    c, ids = site
    r = post_round(c, ids[0], **over)
    assert r.status_code == 303 and "Nothing was changed" in seen(c.get(r.headers["location"])) and count(dbfile, "interviews") == 0


def test_a_note_of_2001_characters_is_refused_and_one_of_2000_non_ascii_characters_is_stored(site, dbfile):
    c, ids = site
    assert "Nothing was changed" in seen(c.get(post_note(c, ids[0], "x" * 2001).headers["location"])) and count(dbfile, "job_notes") == 0
    post_note(c, ids[0], "é\U0001F600" * 1000)                                           # the url-encoded body is far above the 10 kB of other forms
    assert count(dbfile, "job_notes") == 1


def test_markup_in_a_place_and_a_note_is_escaped(site):
    c, ids = site
    post_round(c, ids[0], place="<script>alert(1)</script>", notes="<img src=x onerror=1>")
    post_note(c, ids[0], "<b>bold</b> https://evil.example/")
    page = c.get(f"/jobs/{ids[0]}").text
    assert "<script>alert" not in page and "<img src=x" not in page and "<b>bold" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;b&gt;bold&lt;/b&gt;" in page
    assert 'href="https://evil.example' not in page                                            # a place or note is text, never a link


def test_get_writes_nothing_and_the_job_page_of_another_job_is_unaffected(site, dbfile):
    c, ids = site
    post_round(c, ids[0])
    post_note(c, ids[0])
    before = snapshot(dbfile)
    other = c.get(f"/jobs/{ids[1]}").text
    c.get(f"/jobs/{ids[0]}")
    assert snapshot(dbfile) == before and "Teams" not in other and "Call back" not in other
    top = c.get("/").text + c.get("/jobs").text
    assert "Teams" not in top and "Call back" not in top
    assert c.get(f"/jobs/{ids[0]}/interviews").status_code in (404, 405) and c.get(f"/jobs/{ids[0]}/notes").status_code in (404, 405)


def test_a_job_that_is_gone_gives_a_message_and_a_bad_round_id_nothing(site, dbfile):
    c, ids = site
    r = c.post("/jobs/9999/notes", data={"text": "x", "csrf": c.app.state.csrf("notes:9999")}, follow_redirects=False)
    assert r.status_code == 303 and count(dbfile, "job_notes") == 0
    r = c.post(f"/jobs/{ids[0]}/interviews/777/delete", data={"csrf": c.app.state.csrf(f"interview-delete:{ids[0]}"), "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and "Nothing was changed" in seen(c.get(r.headers["location"]))
    assert c.post(f"/jobs/{ids[0]}/interviews/x/delete", data={"csrf": "0" * 64}).status_code in (403, 404)


def test_a_round_of_another_job_cannot_be_deleted_through_this_one(site, dbfile):
    c, ids = site
    post_round(c, ids[1])
    rid = sqlite3.connect(dbfile).execute("SELECT id FROM interviews").fetchone()[0]
    c.post(f"/jobs/{ids[0]}/interviews/{rid}/delete", data={"csrf": c.app.state.csrf(f"interview-delete:{ids[0]}"), "confirm": "yes"})
    assert count(dbfile, "interviews") == 1


def test_a_busy_database_gives_a_message(site, conn, dbfile):
    c, ids = site
    c.app.state.open_rw = lambda: _quick(dbfile)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE jobs SET title = 'changing' WHERE id = ?", (ids[0],))
    try:
        for r in (post_round(c, ids[0]), post_note(c, ids[0])):
            assert r.status_code == 303 and "could not be written to" in seen(c.get(r.headers["location"]))
    finally:
        conn.rollback()
    assert count(dbfile, "interviews") == 0 and count(dbfile, "job_notes") == 0


def test_the_text_of_a_note_and_a_place_is_never_logged(site, caplog):
    c, ids = site
    with caplog.at_level(logging.DEBUG):
        post_round(c, ids[0], place="Secret Tower 9", notes="private round notes")
        post_note(c, ids[0], "private note text")
    assert caplog.records and not any(w in caplog.text for w in ("Secret Tower", "private round", "private note"))


# --- archive, restore, merge ----------------------------------------------------------------------------------------

def test_archive_and_restore_keep_rounds_and_notes_and_not_the_old_history(conn):
    jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('a|x', 'T', 'C', 'applied', 't', 't')").lastrowid
    db.set_status(conn, jid, "interview")
    interviews.add_round(conn, jid, "2026-12-01", "09:30", "video", "Teams", "bring CV", ZURICH)
    interviews.add_note(conn, jid, "a note")
    aid = db.archive_job(conn, jid, "user", "user_rejected")
    assert count_rows(conn, "interviews") == 0 and count_rows(conn, "job_notes") == 0
    record = json.loads(conn.execute("SELECT record FROM job_archive WHERE id = ?", (aid,)).fetchone()[0])
    assert len(record["interviews"]) == 1 and len(record["notes"]) == 1 and len(record["status_history"]) == 1
    new = db.restore_job(conn, aid)
    got = conn.execute("SELECT starts_at, kind, place, notes FROM interviews WHERE job_id = ?", (new,)).fetchall()
    assert [tuple(r) for r in got] == [("2026-12-01T08:30:00Z", "video", "Teams", "bring CV")]
    assert [(n["text"], n["status_at"]) for n in interviews.list_notes(conn, new, ZURICH)] == [("a note", "interview")]
    rows = conn.execute("SELECT to_status, note FROM status_history WHERE job_id = ?", (new,)).fetchall()
    assert len(rows) == 1 and rows[0]["note"].startswith("restored from archive")             # the old history is not replayed (undo reads the last row)


def test_an_old_archive_record_without_the_new_keys_restores(conn):
    jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('a|x', 'T', 'C', 'new', 't', 't')").lastrowid
    aid = db.archive_job(conn, jid, "user", "user_rejected")
    record = json.loads(conn.execute("SELECT record FROM job_archive WHERE id = ?", (aid,)).fetchone()[0])
    for key in ("interviews", "notes", "status_history"):
        record.pop(key)
    conn.execute("UPDATE job_archive SET record = ? WHERE id = ?", (json.dumps(record), aid))
    assert db.restore_job(conn, aid) > 0


def count_rows(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_merge_duplicates_moves_rounds_and_notes_to_the_survivor(env):
    conn, a, b = pair(env, loser_status="new", survivor_status="applied")
    interviews.add_round(conn, b, "2026-12-01", "09:30", "video", "Teams", "", ZURICH)
    interviews.add_note(conn, b, "loser note")
    conn.commit()
    conn.close()
    assert merge_run(env, "merge-duplicates") == 0
    conn = db.connect(env.live)
    keep = conn.execute("SELECT id FROM jobs").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM interviews WHERE job_id = ?", (keep,)).fetchone()[0] == 1
    assert [n["text"] for n in interviews.list_notes(conn, keep, ZURICH)] == ["loser note"]
    row = conn.execute("SELECT record, moved FROM job_merges").fetchone()
    assert len(json.loads(row["record"])["interviews"]) == 1 and json.loads(row["moved"])["interviews"] == 1 and json.loads(row["moved"])["notes"] == 1
    conn.close()


# Every table that points at jobs is dealt with when a job is archived or merged away: moved, kept in the record, or listed here with the reason.
DEALT_WITH = {
    "job_sources", "job_locations", "job_filter_results", "scores", "status_history", "documents", "interviews", "job_notes",   # in the archive record and the merge
    "job_travel_checks",                                                                                                 # through job_locations
}
NOT_KEPT = {"description_fetches", "description_log", "description_resolutions", "postponements", "batch_jobs"}             # no foreign key; the id is never reused (migration 21)


def test_every_table_with_a_key_to_jobs_is_handled_by_archive_and_merge(conn):
    parents = {}
    for t in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]:
        for fk in conn.execute(f"PRAGMA foreign_key_list({t})"):
            parents.setdefault(fk["table"], set()).add(t)
    assert parents["jobs"] - {"job_travel_checks"} <= DEALT_WITH, sorted(parents["jobs"] - DEALT_WITH)


# --- the command ----------------------------------------------------------------------------------------------------

def test_the_interviews_command_lists_rounds_and_notes_and_writes_nothing(env, capsys):
    conn, a, b = pair(env)
    interviews.add_round(conn, a, "2026-12-01", "09:30", "video", "Teams", "", ZURICH)
    interviews.add_note(conn, a, "a made-up note")
    conn.commit()
    conn.close()
    before = sqlite3.connect(env.live).execute("SELECT COUNT(*) FROM interviews").fetchone()[0]
    assert merge_run(env, "interviews", "--job", str(a)) == 0
    out = capsys.readouterr().out
    assert "2026-12-01" in out and "09:30" in out and "video" in out and "Teams" in out and "a made-up note" in out
    assert merge_run(env, "interviews", "--job", "9999") == 1
    assert sqlite3.connect(env.live).execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == before


# --- review fixes (v0.98.0) -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("day", ["0001-01-01", "1999-12-31", "2101-01-01", "9999-12-31"])
def test_a_year_outside_2000_to_2100_is_refused_not_a_500(day):
    with pytest.raises(Refused, match="2000"):
        interviews.to_utc(day, "00:00", ZURICH)
    assert interviews.to_utc("2000-06-01", "09:00", ZURICH) and interviews.to_utc("2100-06-01", "09:00", ZURICH)


def test_the_web_form_refuses_year_one_with_a_message(site, dbfile):
    c, ids = site
    r = post_round(c, ids[0], day="0001-01-01", clock="00:00")
    assert r.status_code == 303 and "2000" in seen(c.get(r.headers["location"])) and count(dbfile, "interviews") == 0


def test_non_breaking_spaces_from_a_mail_become_plain_spaces(conn):
    jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at) VALUES ('a|x', 'T', 'C', 't', 't')").lastrowid
    interviews.add_note(conn, jid, "call\xa0Monday at 9")
    interviews.add_round(conn, jid, "2026-12-01", "09:30", "video", "Room\xa04", "a\xa0b", ZURICH)
    assert interviews.list_notes(conn, jid, ZURICH)[0]["text"] == "call Monday at 9"
    r = interviews.list_rounds(conn, jid, ZURICH)[0]
    assert (r["place"], r["notes"]) == ("Room 4", "a b")


def test_a_note_is_shown_in_local_time_like_a_round(site, conn):
    c, ids = site
    conn.execute("INSERT INTO job_notes (job_id, status_at, text, created_at) VALUES (?, 'scored', 'x', '2026-12-01T08:30:00Z')", (ids[0],))
    conn.commit()
    assert interviews.list_notes(conn, ids[0], ZURICH)[0]["created_at"] == "2026-12-01 09:30"
    assert "2026-12-01 09:30" in c.get(f"/jobs/{ids[0]}").text and "08:30:00Z" not in c.get(f"/jobs/{ids[0]}").text


# --- delete confirmation (v0.99.0): confirm.js asks in the browser, the server acts only on confirm=yes -----------------

def first_id(dbfile, table):
    raw = sqlite3.connect(dbfile)
    try:
        return raw.execute(f"SELECT id FROM {table}").fetchone()[0]
    finally:
        raw.close()


def test_delete_without_confirm_changes_nothing_and_shows_the_confirmation_page(site, dbfile):
    c, ids = site
    post_round(c, ids[0])
    post_note(c, ids[0])
    rid, nid = first_id(dbfile, "interviews"), first_id(dbfile, "job_notes")
    for path, action, table in ((f"interviews/{rid}/delete", "interview-delete", "interviews"), (f"notes/{nid}/delete", "note-delete", "job_notes")):
        r = c.post(f"/jobs/{ids[0]}/{path}", data={"csrf": c.app.state.csrf(f"{action}:{ids[0]}")}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}/{path}?flash=")
        page = c.get(r.headers["location"])
        assert page.status_code == 200 and "Not confirmed" in page.text and 'name="confirm" value="yes"' in page.text
        assert count(dbfile, table) == 1
        r = c.post(f"/jobs/{ids[0]}/{path}", data={"csrf": c.app.state.csrf(f"{action}:{ids[0]}"), "confirm": "no"}, follow_redirects=False)
        assert count(dbfile, table) == 1


def test_the_confirmation_page_names_the_item_escapes_it_and_writes_nothing(site, dbfile):
    c, ids = site
    post_round(c, ids[0], place="<i>Room 4</i>")
    post_note(c, ids[0], "<b>private</b> text")
    rid, nid = first_id(dbfile, "interviews"), first_id(dbfile, "job_notes")
    before = snapshot(dbfile)
    r = c.get(f"/jobs/{ids[0]}/interviews/{rid}/delete")
    assert r.status_code == 200 and "2026-12-01" in r.text and "09:30" in r.text and "video" in r.text and "&lt;i&gt;Room 4&lt;/i&gt;" in r.text and "<i>Room" not in r.text
    assert "script-src 'none'" in r.headers["content-security-policy"]
    assert f'action="/jobs/{ids[0]}/interviews/{rid}/delete"' in r.text and c.app.state.csrf(f"interview-delete:{ids[0]}") in r.text
    r = c.get(f"/jobs/{ids[0]}/notes/{nid}/delete")
    assert r.status_code == 200 and "&lt;b&gt;private&lt;/b&gt; text" in r.text and "<b>private" not in r.text
    assert f'action="/jobs/{ids[0]}/notes/{nid}/delete"' in r.text and c.app.state.csrf(f"note-delete:{ids[0]}") in r.text
    assert snapshot(dbfile) == before


def test_the_confirmation_page_of_a_missing_or_foreign_round_or_note_is_404(site, dbfile):
    c, ids = site
    post_round(c, ids[1])
    post_note(c, ids[1])
    rid, nid = first_id(dbfile, "interviews"), first_id(dbfile, "job_notes")
    for path in (f"/jobs/{ids[0]}/interviews/{rid}/delete", f"/jobs/{ids[0]}/notes/{nid}/delete", f"/jobs/{ids[1]}/interviews/777/delete",
                 f"/jobs/{ids[1]}/notes/777/delete", f"/jobs/9999/interviews/{rid}/delete", f"/jobs/{ids[1]}/interviews/x/delete", f"/jobs/{ids[1]}/notes/%C2%B2/delete"):
        assert c.get(path).status_code == 404, path


def test_the_job_page_marks_its_delete_forms_for_confirm_js_and_loads_it(site, dbfile):
    c, ids = site
    post_round(c, ids[0])
    post_note(c, ids[0])
    page = c.get(f"/jobs/{ids[0]}").text
    assert len(re.findall(r'<form method="post" action="/jobs/\d+/(?:interviews|notes)/\d+/delete"[^>]*data-confirm="[^"]+"', page)) == 2
    assert re.findall(r"<script[^>]*>", page.lower()) == ['<script src="/static/confirm.js" defer>']
    js = c.get("/static/confirm.js")
    assert js.status_code == 200 and "data-confirm" in js.text and "confirm(" in js.text and "eval(" not in js.text and "innerHTML" not in js.text


# --- edit a round (v0.99.0) -----------------------------------------------------------------------------------------

def post_edit(c, jid, rid, token_job=None, **over):
    fields = rounds_form(**over)
    return c.post(f"/jobs/{jid}/interviews/{rid}", data={**fields, "csrf": c.app.state.csrf(f"interview-edit:{token_job or jid}")}, follow_redirects=False)


def test_a_round_is_edited_renumbered_and_updated_at_is_set(site, conn, dbfile):
    c, ids = site
    post_round(c, ids[0], day="2026-12-01")
    post_round(c, ids[0], day="2026-12-08", place="Zurich")
    raw = sqlite3.connect(dbfile)
    first = raw.execute("SELECT id FROM interviews WHERE starts_at LIKE '2026-12-01%'").fetchone()[0]
    assert raw.execute("SELECT updated_at FROM interviews WHERE id = ?", (first,)).fetchone()[0] is None
    r = post_edit(c, ids[0], first, day="2026-12-15", clock="14:00", kind="onsite", place="Basel", notes="new notes")
    assert r.status_code == 303 and "Interview round changed" in seen(c.get(r.headers["location"]))
    row = raw.execute("SELECT starts_at, kind, place, notes, updated_at FROM interviews WHERE id = ?", (first,)).fetchone()
    assert row[:4] == ("2026-12-15T13:00:00Z", "onsite", "Basel", "new notes") and row[4]
    assert [(x["number"], x["id"]) for x in interviews.list_rounds(conn, ids[0], ZURICH)][1] == (2, first)         # now the later round
    raw.close()


@pytest.mark.parametrize("over", [{"kind": "fax"}, {"day": "2026-02-30"}, {"clock": "9h"}, {"day": ""}, {"place": "p" * 301}, {"notes": "n" * 2001}, {"day": "1999-01-01"}])
def test_a_bad_edit_is_refused_and_nothing_changes(site, dbfile, over):
    c, ids = site
    post_round(c, ids[0])
    rid = first_id(dbfile, "interviews")
    before = snapshot(dbfile)
    r = post_edit(c, ids[0], rid, **over)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/jobs/{ids[0]}/interviews/{rid}/edit?flash=")
    assert "Nothing was changed" in seen(c.get(r.headers["location"])) and snapshot(dbfile) == before


def test_editing_a_round_of_another_job_or_a_missing_round_changes_nothing(site, dbfile):
    c, ids = site
    post_round(c, ids[1])
    rid = first_id(dbfile, "interviews")
    before = snapshot(dbfile)
    for jid, r_id in ((ids[0], rid), (ids[1], 777), (ids[1], "x")):
        r = post_edit(c, jid, r_id, place="changed")
        assert r.status_code == 303 and "does not exist" in seen(c.get(r.headers["location"]))
    assert post_edit(c, 9999, rid).status_code == 303
    assert snapshot(dbfile) == before


def test_the_edit_page_is_prefilled_in_zurich_time_and_writes_nothing(site, dbfile):
    c, ids = site
    post_round(c, ids[0], day="2026-07-01", clock="09:30", kind="onsite", place='A "quoted" <b>', notes="n1")
    rid = first_id(dbfile, "interviews")
    before = snapshot(dbfile)
    page = c.get(f"/jobs/{ids[0]}/interviews/{rid}/edit")
    assert page.status_code == 200 and "script-src 'none'" in page.headers["content-security-policy"]
    t = page.text
    assert 'value="2026-07-01"' in t and 'value="09:30"' in t and '<option value="onsite" selected>' in t and 'value="A &#34;quoted&#34; &lt;b&gt;"' in t and 'value="n1"' in t
    assert snapshot(dbfile) == before
    assert c.get(f"/jobs/{ids[1]}/interviews/{rid}/edit").status_code == 404 and c.get(f"/jobs/{ids[0]}/interviews/777/edit").status_code == 404


def test_the_edit_token_of_one_job_does_not_work_on_another_and_a_foreign_origin_is_refused(site, dbfile):
    c, ids = site
    post_round(c, ids[0])
    rid = first_id(dbfile, "interviews")
    before = snapshot(dbfile)
    assert post_edit(c, ids[0], rid, token_job=ids[1], place="x").status_code == 403
    assert c.post(f"/jobs/{ids[0]}/interviews/{rid}", data={**rounds_form(place="x")}).status_code == 403
    tok = c.app.state.csrf(f"interview-edit:{ids[0]}")
    assert c.post(f"/jobs/{ids[0]}/interviews/{rid}", data={**rounds_form(place="x"), "csrf": tok}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert snapshot(dbfile) == before


def test_the_text_of_an_edited_place_is_never_logged(site, dbfile, caplog):
    c, ids = site
    post_round(c, ids[0])
    rid = first_id(dbfile, "interviews")
    with caplog.at_level(logging.DEBUG):
        post_edit(c, ids[0], rid, place="Secret Tower 9", notes="private round notes")
    assert any("interview round changed" in r.getMessage() for r in caplog.records) and not any(w in caplog.text for w in ("Secret Tower", "private round"))


def test_an_edited_round_survives_archive_and_restore_with_updated_at(conn):
    jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('a|x', 'T', 'C', 'interview', 't', 't')").lastrowid
    rid = interviews.add_round(conn, jid, "2026-12-01", "09:30", "video", "Teams", "", ZURICH)
    assert interviews.update_round(conn, jid, rid, "2026-12-02", "10:00", "phone", "", "edited", ZURICH)
    aid = db.archive_job(conn, jid, "user", "user_rejected")
    new = db.restore_job(conn, aid)
    got = conn.execute("SELECT starts_at, kind, place, notes, updated_at FROM interviews WHERE job_id = ?", (new,)).fetchone()
    assert tuple(got)[:4] == ("2026-12-02T09:00:00Z", "phone", None, "edited") and got["updated_at"]


def test_update_round_refuses_before_writing_and_says_false_for_a_foreign_round(conn):
    a = conn.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at) VALUES ('a|x', 'T', 'C', 't', 't')").lastrowid
    b = conn.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at) VALUES ('b|x', 'T', 'C', 't', 't')").lastrowid
    rid = interviews.add_round(conn, a, "2026-12-01", "09:30", "video", "Teams", "", ZURICH)
    with pytest.raises(Refused):
        interviews.update_round(conn, a, rid, "2026-12-01", "09:30", "fax", "", "", ZURICH)
    assert not interviews.update_round(conn, b, rid, "2026-12-02", "09:30", "video", "", "", ZURICH)
    r = interviews.get_round(conn, a, rid, ZURICH)
    assert (r["date"], r["place"]) == ("2026-12-01", "Teams") and interviews.get_round(conn, b, rid, ZURICH) is None


def test_next_rounds_ignores_past_rounds_and_other_jobs(conn):
    a = conn.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at) VALUES ('a|x', 'T', 'C', 't', 't')").lastrowid
    b = conn.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at) VALUES ('b|x', 'T', 'C', 't', 't')").lastrowid
    for day in ("2026-12-01", "2026-12-10", "2026-11-01"):
        interviews.add_round(conn, a, day, "09:30", "video", "", "", ZURICH)
    interviews.add_round(conn, b, "2026-10-01", "09:30", "video", "", "", ZURICH)
    now = "2026-11-15T12:00:00Z"
    assert interviews.next_rounds(conn, [a, b], now, ZURICH) == {a: "2026-12-01 09:30"}
    assert interviews.next_rounds(conn, [b], now, ZURICH) == {} and interviews.next_rounds(conn, [], now, ZURICH) == {}
    assert interviews.next_rounds(conn, [a], "2026-12-01T08:30:00Z", ZURICH) == {a: "2026-12-10 09:30"}          # a round starting exactly now is not "next"
