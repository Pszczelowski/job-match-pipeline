"""v0.85.0: the web side of "Ask the writer to fix" (POST /jobs/<id>/revise, goal.md 5.10a). The route only stores a request row; the
scheduler sends it. Temporary databases, no network, no real config."""
import json
import re

import pytest

from app import documents, pipeline
from tests.test_approval import doc_row
from tests.test_findings_override import accept, blocked, overrides
from tests.test_web import client, conn, cfg as base_cfg                              # noqa: F401
from tests.test_web_writes import post, seen


@pytest.fixture
def cfg(base_cfg):
    base_cfg["headless"]["enabled"] = True
    return base_cfg


def revise(c, jid, doc="cv", instruction="", token=None, **extra):
    return post(c, f"/jobs/{jid}/revise", token or c.app.state.csrf(f"revise:{jid}"), doc=doc, instruction=instruction, next="job", **extra)


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM revision_requests ORDER BY id")]


def flash(c, r):
    return seen(c.get(r.headers["location"]))


def test_the_form_shows_only_on_a_blocked_document_with_headless_on(conn, cfg, tmp_path):
    jid, row, _ = blocked(conn, cfg)
    page = client(cfg, tmp_path).get(f"/jobs/{jid}").text
    assert page.count(f'action="/jobs/{jid}/revise"') == 1                       # the CV is blocked, the cover letter is a draft
    assert 'name="doc" value="cv"' in page and 'maxlength="1000"' in page and "Ask the writer to fix" in page
    cfg["headless"]["enabled"] = False
    assert "/revise" not in client(cfg, tmp_path).get(f"/jobs/{jid}").text


def test_the_form_also_shows_on_a_document_with_an_address_finding(conn, cfg, tmp_path):
    jid, row, fs = blocked(conn, cfg)
    bad = {"kind": "address", "text": "(home address)", "block": None, "where": "document", "reason": "the text contains the home address", "block_text": ""}
    with conn:
        conn.execute("UPDATE documents SET findings = ? WHERE id = ?", (json.dumps(fs + [bad]), row["id"]))
    c = client(cfg, tmp_path)
    assert f'action="/jobs/{jid}/revise"' in c.get(f"/jobs/{jid}").text
    assert revise(c, jid, instruction="please").status_code == 303 and len(rows(conn)) == 1


def test_a_press_stores_a_pending_row_with_the_sha_of_the_text(conn, cfg, tmp_path):
    jid, row, _ = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    r = revise(c, jid, instruction="  Make it\r\nshorter.\x00 ")
    assert r.status_code == 303 and "stored" in flash(c, r)
    (req,) = rows(conn)
    assert (req["status"], req["kind"], req["document_id"]) == ("pending", "cv", row["id"])
    assert req["instruction"] == "Make it\nshorter." and req["content_sha"] == documents.content_sha(row["content"])
    assert req["created_at"] and req["relayed_at"] is None and req["scrubbed"] == 0
    assert doc_row(conn, jid, "cv") == row                                          # the document itself is untouched


def test_an_empty_instruction_is_stored_as_an_empty_string(conn, cfg, tmp_path):
    jid, _, _ = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    assert revise(c, jid, instruction="").status_code == 303
    assert rows(conn)[0]["instruction"] == ""
    c2 = client(cfg, tmp_path)
    page = c2.get(f"/jobs/{jid}").text
    assert f'action="/jobs/{jid}/revise"' not in page and "waiting since" in page      # the active request hides the form


def test_a_press_while_the_run_lock_is_held_is_queued_not_refused(conn, cfg, tmp_path, monkeypatch):
    jid, _, _ = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    monkeypatch.setattr(pipeline, "lock_is_held", lambda _cfg, now=None: True)
    r = revise(c, jid, instruction="x")
    t = flash(c, r)
    assert len(rows(conn)) == 1 and rows(conn)[0]["status"] == "pending" and "queued" in t and "refused" not in t.lower()


def test_refusals_write_nothing(conn, cfg, tmp_path):
    jid, row, _ = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    assert revise(c, jid, token="0" * 64).status_code == 403
    assert revise(c, jid, instruction="x" * 1001).status_code == 303 and "at most 1000" in flash(c, revise(c, jid, instruction="x" * 1001))
    assert "not blocked" in flash(c, revise(c, jid, doc="cover_letter"))                  # the cover letter is a draft
    assert "Unknown document" in flash(c, revise(c, jid, doc="diary"))
    assert revise(c, 9999, instruction="x").status_code == 303 and rows(conn) == []
    cfg["headless"]["enabled"] = False
    off = client(cfg, tmp_path)
    assert "switched off" in flash(off, revise(off, jid))
    cfg["headless"]["enabled"] = True
    assert rows(conn) == []
    assert revise(c, jid, instruction="first").status_code == 303 and len(rows(conn)) == 1
    assert "already has a request" in flash(c, revise(c, jid, instruction="second"))
    assert len(rows(conn)) == 1 and rows(conn)[0]["instruction"] == "first"


def test_a_new_press_is_possible_after_a_terminal_request(conn, cfg, tmp_path):
    jid, _, _ = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    revise(c, jid, instruction="one")
    with conn:
        conn.execute("UPDATE revision_requests SET status = 'failed', outcome = 'error' WHERE id = 1")
    assert revise(c, jid, instruction="two").status_code == 303 and [r["status"] for r in rows(conn)] == ["failed", "pending"]


def test_accept_as_false_positive_on_the_address_finding_is_still_refused(conn, cfg, tmp_path):
    jid, row, fs = blocked(conn, cfg)
    bad = {"kind": "address", "text": "(home address)", "block": None, "where": "document", "reason": "r", "block_text": ""}
    with conn:
        conn.execute("UPDATE documents SET findings = ? WHERE id = ?", (json.dumps(fs + [bad]), row["id"]))
    c = client(cfg, tmp_path)
    assert "cannot be accepted" in flash(c, accept(c, jid, bad)) and not overrides(conn)


@pytest.mark.parametrize("char, count", [("ł", 1000), ("\U0001F600", 1000)])
def test_an_instruction_of_1000_characters_is_accepted_whatever_their_size(conn, cfg, tmp_path, char, count):
    jid, _, _ = blocked(conn, cfg)
    c = client(cfg, tmp_path)                                                           # 1000 four-byte characters url-encode to 12000 bytes
    assert revise(c, jid, instruction=char * count).status_code == 303
    assert rows(conn)[0]["instruction"] == char * count


def test_a_body_over_the_limit_is_refused(conn, cfg, tmp_path):
    jid, _, _ = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    assert revise(c, jid, instruction="x" * 20000).status_code == 413 and rows(conn) == []


@pytest.mark.parametrize("status, text", [
    ("pending", "waiting since"), ("relayed", "with the writer since"), ("stored", "was stored as draft"),
    ("stale", "document changed meanwhile"), ("failed", "request failed (usage_limit): resets 3pm")])
def test_the_job_page_shows_each_status(conn, cfg, tmp_path, status, text):
    jid, row, _ = blocked(conn, cfg)
    with conn:
        conn.execute("INSERT INTO revision_requests (document_id, kind, instruction, content_sha, status, outcome, message, created_at, relayed_at, finished_at) "
                     "VALUES (?, 'cv', '', 'x', ?, ?, ?, '2026-11-12T07:00:00Z', '2026-11-12T07:01:00Z', '2026-11-12T07:30:00Z')",
                     (row["id"], status, {"stored": "draft", "failed": "usage_limit"}.get(status), "resets 3pm" if status == "failed" else None))
    page = seen(client(cfg, tmp_path).get(f"/jobs/{jid}"))
    assert text in page
    assert (f'action="/jobs/{jid}/revise"' in page) == (status not in ("pending", "relayed"))


def test_get_writes_nothing(conn, cfg, tmp_path):
    jid, _, _ = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    before = conn.execute("SELECT COUNT(*) FROM revision_requests").fetchone()[0]
    for path in (f"/jobs/{jid}", "/", "/jobs"):
        assert c.get(path).status_code == 200
    assert c.get(f"/jobs/{jid}/revise").status_code in (404, 405)
    assert conn.execute("SELECT COUNT(*) FROM revision_requests").fetchone()[0] == before == 0
