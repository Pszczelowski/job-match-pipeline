"""v0.85.0: documents.store_revision, the DB side of "Ask the writer to fix" (goal.md 5.10a). No network, temporary databases."""
import copy
import json
from datetime import date

import pytest

from app import db, documents
from tests.test_approval import doc_row, imported
from tests.test_documents import CV, docs
from tests.test_documents import cfg, conn                                           # noqa: F401
from tests.test_findings_override import blocked, spoil

TODAY = date(2026, 11, 12)
HOME = "Rosenweg 12, 8180 Exampletown"


def request(conn, row, kind="cv", status="relayed"):
    with conn:
        cur = conn.execute(
            "INSERT INTO revision_requests (document_id, kind, instruction, content_sha, status, created_at, relayed_at) "
            "VALUES (?, ?, 'fix it', ?, ?, '2026-11-12T08:00:00Z', '2026-11-12T08:01:00Z')",
            (row["id"], kind, documents.content_sha(row["content"]), status))
    return cur.lastrowid


def req(conn, rid):
    return dict(conn.execute("SELECT * FROM revision_requests WHERE id = ?", (rid,)).fetchone())


def good_cv():
    return copy.deepcopy(CV)


def test_a_clean_revision_is_stored_as_a_draft_with_its_pdf(conn, cfg):
    jid, row, _ = blocked(conn, cfg)
    rid = request(conn, row)
    out = documents.store_revision(conn, cfg, rid, good_cv(), ["check the tone"], today=TODAY)
    new = doc_row(conn, jid, "cv")
    assert (out.status, out.outcome) == ("stored", "draft") and new["status"] == "draft"
    assert json.loads(new["content"]) == good_cv() and new["generated_on"] == "2026-11-12"
    assert new["pdf_path"] and out.rendered.rendered
    assert any(f["kind"] == "review_attention" and f["reason"] == "check the tone" for f in json.loads(new["findings"]))
    r = req(conn, rid)
    assert (r["status"], r["outcome"]) == ("stored", "draft") and r["finished_at"]
    assert json.loads(r["previous_content"]) == json.loads(row["content"])


def test_a_revision_that_is_still_blocked_is_stored_blocked_with_the_new_findings(conn, cfg):
    jid, row, fs = blocked(conn, cfg)
    rid = request(conn, row)
    still = good_cv()
    still["experience"][0]["bullets"][1]["text"] = "Cut 7 million CHF of costs at Northwind."
    out = documents.store_revision(conn, cfg, rid, still, [], today=TODAY)
    new = doc_row(conn, jid, "cv")
    assert (out.status, out.outcome) == ("stored", "blocked") and new["status"] == "blocked" and not new["pdf_path"]
    texts = {f["text"] for f in json.loads(new["findings"])}
    assert any("7 million" in t for t in texts) and not any("5 million" in t for t in texts)
    assert req(conn, rid)["outcome"] == "blocked"


@pytest.mark.parametrize("how", ["content", "draft", "approved"])
def test_a_stale_request_stores_nothing(conn, cfg, how):
    jid, row, _ = blocked(conn, cfg)
    rid = request(conn, row)
    with conn:
        if how == "content":
            conn.execute("UPDATE documents SET content = ? WHERE id = ?", (row["content"] + " ", row["id"]))
        else:
            conn.execute("UPDATE documents SET status = ?, approved_at = '2026-11-12T08:02:00Z' WHERE id = ?", (how, row["id"]))
    before = doc_row(conn, jid, "cv")
    out = documents.store_revision(conn, cfg, rid, good_cv(), [], today=TODAY)
    assert out.status == "stale" and req(conn, rid)["status"] == "stale" and req(conn, rid)["finished_at"]
    assert doc_row(conn, jid, "cv") == before


def test_a_schema_invalid_document_fails_the_request_and_leaves_the_document(conn, cfg):
    jid, row, _ = blocked(conn, cfg)
    rid = request(conn, row)
    bad = good_cv()
    del bad["variant"]
    out = documents.store_revision(conn, cfg, rid, bad, [], today=TODAY)
    assert (out.status, out.outcome) == ("failed", "invalid") and "variant" in out.message
    assert doc_row(conn, jid, "cv") == row and req(conn, rid)["status"] == "failed"


def test_the_address_written_into_the_revision_blocks_it(conn, cfg):
    cfg["location"]["car_origin_address"] = HOME
    jid, row, _ = blocked(conn, cfg)
    rid = request(conn, row)
    cv = good_cv()
    cv["summary"] = f"I live at {HOME}. " + cv["summary"]
    out = documents.store_revision(conn, cfg, rid, cv, [], today=TODAY)
    assert out.outcome == "blocked" and "address" in {f["kind"] for f in out.findings}
    assert doc_row(conn, jid, "cv")["status"] == "blocked"


def test_the_marker_left_in_the_revision_blocks_it_and_cannot_be_accepted(conn, cfg):
    jid, row, _ = blocked(conn, cfg)
    rid = request(conn, row)
    cv = good_cv()
    cv["summary"] = f"I live at {documents.ADDRESS_MARKER}. " + cv["summary"]
    out = documents.store_revision(conn, cfg, rid, cv, [], today=TODAY)
    new = doc_row(conn, jid, "cv")
    assert out.outcome == "blocked" and new["status"] == "blocked"
    f = next(f for f in json.loads(new["findings"]) if f["kind"] == "address")
    assert documents.add_override(conn, jid, "cv", *documents.finding_key(f), "ok") is not None


def _spoil_letter(d):
    d["cover_letter"]["blocks"]["skills"] += " I speak fluent German."
    return d


def test_a_cover_letter_revision_is_checked_as_a_letter(conn, cfg):
    ids = imported(conn, cfg, mutate=_spoil_letter)
    row = doc_row(conn, ids[0], "cover_letter")
    assert row["status"] == "blocked"
    rid = request(conn, row, kind="cover_letter")
    out = documents.store_revision(conn, cfg, rid, docs()["cover_letter"], [], today=TODAY)
    assert (out.status, out.outcome) == ("stored", "draft") and doc_row(conn, ids[0], "cover_letter")["status"] == "draft"


def test_accepted_overrides_stay_after_a_revision(conn, cfg):
    jid, row, fs = blocked(conn, cfg)
    documents.add_override(conn, jid, "cv", *documents.finding_key(fs[0]), "ok")
    conn.commit()
    rid = request(conn, row)
    out = documents.store_revision(conn, cfg, rid, spoil({"cv": good_cv()})["cv"], [], today=TODAY)      # the same accepted finding again
    assert out.outcome == "draft"
    assert conn.execute("SELECT COUNT(*) FROM finding_overrides").fetchone()[0] == 1


def test_a_request_that_is_not_relayed_is_ignored(conn, cfg):
    jid, row, _ = blocked(conn, cfg)
    rid = request(conn, row, status="failed")
    assert documents.store_revision(conn, cfg, rid, good_cv(), [], today=TODAY).status == "ignored"
    assert documents.store_revision(conn, cfg, 999, good_cv(), [], today=TODAY).status == "ignored"
    assert doc_row(conn, jid, "cv") == row


def test_the_same_revision_twice_stores_once(conn, cfg):
    jid, row, _ = blocked(conn, cfg)
    rid = request(conn, row)
    assert documents.store_revision(conn, cfg, rid, good_cv(), [], today=TODAY).status == "stored"
    assert documents.store_revision(conn, cfg, rid, good_cv(), [], today=TODAY).status == "ignored"


def test_migration_23_one_active_request_per_document_autoincrement_and_cascade(conn, cfg):
    jid, row, _ = blocked(conn, cfg)
    first = request(conn, row)
    with pytest.raises(db.sqlite3.IntegrityError):                          # the partial unique index
        request(conn, row, status="pending")
    with conn:
        conn.execute("UPDATE revision_requests SET status = 'failed' WHERE id = ?", (first,))
    second = request(conn, row, status="pending")                           # allowed after a terminal row
    assert second > first
    with conn:
        conn.execute("DELETE FROM revision_requests WHERE id = ?", (second,))
    third = request(conn, row, status="pending")
    assert third > second                                                   # AUTOINCREMENT: an id is never used twice
    with conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
    assert conn.execute("SELECT COUNT(*) FROM revision_requests").fetchone()[0] == 0
    with pytest.raises(db.sqlite3.IntegrityError):
        conn.execute("INSERT INTO revision_requests (document_id, kind, instruction, content_sha, status, created_at) "
                     "VALUES (1, 'cv', '', 'x', 'bogus', 't')")
