"""v0.83.0: blocked documents self-service (goal.md 5.6): every finding on the job page, Recheck, accepting a finding as a
false positive with a comment, Accept and approve. No network, no real database."""
import html
import json

import pytest

from app import db, documents, pipeline
from tests.test_approval import doc_row, imported, job_status
from tests.test_web import client, conn, cfg                                           # noqa: F401
from tests.test_web_writes import post, seen


def spoil(d):
    d["cv"]["experience"][0]["bullets"][0]["text"] = "Saved 5 million CHF at Northwind."
    return d


def spoil_twice(d):
    d = spoil(d)
    d["cv"]["experience"][0]["bullets"][1]["text"] = "Cut 7 million CHF of costs at Northwind."
    return d


def blocked(conn, cfg, mutate=spoil):
    ids = imported(conn, cfg, mutate=mutate)
    row = doc_row(conn, ids[0], "cv")
    assert row["status"] == "blocked"
    return ids[0], row, [f for f in json.loads(row["findings"]) if f["kind"] == "number"]


def accept(c, jid, f, comment="it is in the facts", **extra):
    return post(c, f"/jobs/{jid}/findings/accept", c.app.state.csrf(f"accept:{jid}"), doc="cv", fkind=f["kind"], fwhere=f["where"],
                ftext=f["text"], comment=comment, next="job", **extra)


def overrides(conn):
    return [tuple(r) for r in conn.execute("SELECT document_id, kind, finding_where, text, comment FROM finding_overrides")]


def test_override_matches_on_kind_where_and_text(conn, cfg):
    jid, row, fs = blocked(conn, cfg)
    f = fs[0]
    key = documents.finding_key(f)
    assert documents.has_blocking([f]) and documents.has_blocking([f], set())
    assert not documents.has_blocking([f], {key})
    assert documents.has_blocking([f], {(key[0], key[1], key[2] + "x")}) and documents.has_blocking([f], {("title",) + key[1:]})
    assert documents.has_blocking([f], {(key[0], "other", key[2])})
    assert documents.add_override(conn, jid, "cv", *key, "ok") is None
    assert documents.load_overrides(conn, row["id"]) == {key}


@pytest.mark.parametrize("kind", ["language_claim", "german_not_stated", "address"])
def test_language_and_address_findings_are_refused_and_have_no_button(conn, cfg, tmp_path, kind):
    jid, row, fs = blocked(conn, cfg)
    bad = {"kind": kind, "text": "x", "block": None, "where": "document", "reason": "r", "block_text": ""}
    with conn:
        conn.execute("UPDATE documents SET findings = ? WHERE id = ?", (json.dumps(fs + [bad]), row["id"]))
    assert documents.add_override(conn, jid, "cv", kind, "document", "x", "ok") and not overrides(conn)
    c = client(cfg, tmp_path)
    r = post(c, f"/jobs/{jid}/findings/accept", c.app.state.csrf(f"accept:{jid}"), doc="cv", fkind=kind, fwhere="document", ftext="x", comment="c")
    assert "cannot be accepted" in seen(c.get(r.headers["location"])) and not overrides(conn)
    assert c.get(f"/jobs/{jid}").text.count('name="fkind"') == len(fs)       # the number findings have a form, the added one has none


def test_an_accepted_finding_survives_a_recheck_and_the_document_becomes_a_draft(conn, cfg, tmp_path):
    jid, row, fs = blocked(conn, cfg)
    assert len(fs) == 1
    c = client(cfg, tmp_path)
    r = accept(c, jid, fs[0])
    assert r.status_code == 303 and len(overrides(conn)) == 1
    assert "accepted as a false positive" in seen(c.get(r.headers["location"]))
    after = doc_row(conn, jid, "cv")
    assert after["status"] == "draft" and after["pdf_path"]                 # the recheck turned it into a draft and rendered it
    assert "accepted: it is in the facts" in c.get(f"/jobs/{jid}").text
    # the override outlives a recheck of the document when it is blocked again by something else
    with conn:
        conn.execute("UPDATE documents SET status = 'blocked' WHERE id = ?", (row["id"],))
    out = documents.recheck_blocked(conn, cfg, apply=True, job_id=jid)
    assert out[0]["status"] == "draft" and len(overrides(conn)) == 1


def test_state_counts_only_findings_that_are_not_accepted(conn, cfg):
    jid, row, fs = blocked(conn, cfg, spoil_twice)
    assert len(fs) == 2
    assert documents.state(conn, jid) == ("blocked", 2)
    documents.add_override(conn, jid, "cv", *documents.finding_key(fs[0]), "ok")
    assert documents.state(conn, jid) == ("blocked", 1)


def test_a_new_results_file_clears_the_overrides_of_that_document(conn, cfg):
    jid, row, fs = blocked(conn, cfg)
    documents.add_override(conn, jid, "cv", *documents.finding_key(fs[0]), "ok")
    assert len(overrides(conn)) == 1
    with conn:
        documents.store_document(conn, jid, "cv", row["run_id"], json.loads(row["content"]), fs, documents.date.today(), db.utcnow())
    assert overrides(conn) == [] and doc_row(conn, jid, "cv")["status"] == "blocked"


def test_accept_and_approve_approves_when_everything_is_clear(conn, cfg, tmp_path):
    jid, row, fs = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    t = seen(c.get(accept(c, jid, fs[0], then="approve").headers["location"]))
    assert doc_row(conn, jid, "cv")["status"] == "approved" and doc_row(conn, jid, "cover_letter")["status"] == "draft"      # only the document of the finding
    assert job_status(conn, jid) != "approved" and "cv: approved" in t


def test_accept_and_approve_does_not_approve_while_a_finding_still_blocks(conn, cfg, tmp_path):
    jid, row, fs = blocked(conn, cfg, spoil_twice)
    c = client(cfg, tmp_path)
    t = seen(c.get(accept(c, jid, fs[0], then="approve").headers["location"]))
    assert doc_row(conn, jid, "cv")["status"] == "blocked" and doc_row(conn, jid, "cover_letter")["status"] == "draft"
    assert "Not approved" in t and job_status(conn, jid) != "approved" and len(overrides(conn)) == 1
    t = seen(c.get(accept(c, jid, fs[1], then="approve").headers["location"]))              # the second one clears it
    assert doc_row(conn, jid, "cv")["status"] == "approved" and doc_row(conn, jid, "cover_letter")["status"] == "draft"


def test_a_comment_is_required_and_cleaned(conn, cfg, tmp_path):
    jid, row, fs = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    for bad in ("", "   ", "x" * 301):
        r = accept(c, jid, fs[0], comment=bad)
        assert "comment of 1 to 300" in seen(c.get(r.headers["location"])) and not overrides(conn)
    accept(c, jid, fs[0], comment="fine\x00\x07 text\n here")
    assert overrides(conn)[0][4] == "fine text here"


def test_refused_while_a_run_holds_the_lock(conn, cfg, tmp_path, monkeypatch):
    jid, row, fs = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    monkeypatch.setattr(pipeline, "lock_is_held", lambda _cfg, now=None: True)
    for r in (accept(c, jid, fs[0]), post(c, f"/jobs/{jid}/recheck", c.app.state.csrf(f"recheck:{jid}"), next="job")):
        assert "pipeline run is in progress" in seen(c.get(r.headers["location"]))
    assert not overrides(conn) and doc_row(conn, jid, "cv")["status"] == "blocked"


def test_csrf_and_host_checks(conn, cfg, tmp_path):
    jid, row, fs = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    assert post(c, f"/jobs/{jid}/findings/accept", "0" * 64, doc="cv", fkind="number", fwhere=fs[0]["where"], ftext=fs[0]["text"], comment="c").status_code == 403
    assert post(c, f"/jobs/{jid}/recheck", "").status_code == 403
    assert post(c, f"/jobs/{jid}/recheck", c.app.state.csrf(f"accept:{jid}")).status_code == 403      # the token of another action
    tok = c.app.state.csrf(f"recheck:{jid}")
    assert post(c, f"/jobs/{jid}/recheck", tok, headers={"host": "evil.example"}).status_code == 400
    assert post(c, f"/jobs/{jid}/recheck", tok, headers={"origin": "http://evil.example"}).status_code == 403
    assert not overrides(conn)


def test_recheck_button_runs_the_recheck_and_reports(conn, cfg, tmp_path):
    jid, row, fs = blocked(conn, cfg)
    c = client(cfg, tmp_path)
    r = post(c, f"/jobs/{jid}/recheck", c.app.state.csrf(f"recheck:{jid}"), next="job")
    assert "still blocked" in seen(c.get(r.headers["location"])) and doc_row(conn, jid, "cv")["status"] == "blocked"


def test_findings_are_listed_in_full_and_escaped(conn, cfg, tmp_path):
    jid, row, fs = blocked(conn, cfg)
    evil = {"kind": "number", "text": "<script>alert(1)</script>", "block": None, "where": "cv <b>", "reason": "r & <i>", "block_text": "<img src=x>"}
    warn = {"kind": documents.LENGTH_WARNING, "text": "w", "block": None, "where": "document", "reason": "too long", "block_text": ""}
    with conn:
        conn.execute("UPDATE documents SET findings = ? WHERE id = ?", (json.dumps(fs + [evil, warn]), row["id"]))
    page = client(cfg, tmp_path).get(f"/jobs/{jid}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;img src=x&gt;" in page
    assert "too long" in page and f"{documents.LENGTH_WARNING} (warning)" in page
    assert fs[0]["reason"] in html.unescape(page) and f'action="/jobs/{jid}/recheck"' in page


def test_migration_22_creates_the_table_and_cascades(conn, cfg):
    assert db.schema_version(conn) == len(db.MIGRATIONS) >= 22
    jid, row, fs = blocked(conn, cfg)
    documents.add_override(conn, jid, "cv", *documents.finding_key(fs[0]), "ok")
    with conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
    assert overrides(conn) == []


def test_a_stray_override_row_for_a_language_or_address_finding_does_not_unblock(conn, cfg):
    jid, row, fs = blocked(conn, cfg)
    for kind in ("language_claim", "german_not_stated", "address"):
        bad = {"kind": kind, "text": "x", "block": None, "where": "document", "reason": "r", "block_text": ""}
        with conn:
            conn.execute("INSERT OR REPLACE INTO finding_overrides (document_id, kind, finding_where, text, comment, created_at) VALUES (?, ?, 'document', 'x', 'c', 't')",
                         (row["id"], kind))
        assert documents.has_blocking([bad], documents.load_overrides(conn, row["id"]))
        with conn:
            conn.execute("UPDATE documents SET findings = ? WHERE id = ?", (json.dumps([bad]), row["id"]))
        assert documents.state(conn, jid) == ("blocked", 1)


def test_recheck_skips_a_document_that_was_replaced_meanwhile(conn, cfg, monkeypatch):
    jid, row, fs = blocked(conn, cfg)
    real = documents.check_cv

    def check_then_replace(*a, **k):
        out = real(*a, **k)
        with conn:
            conn.execute("UPDATE documents SET content = ? WHERE id = ?", (json.dumps({"replaced": True}), row["id"]))     # a results file lands now
        return out
    monkeypatch.setattr(documents, "check_cv", check_then_replace)
    out = documents.recheck_blocked(conn, cfg, apply=True, job_id=jid)
    assert out[0]["status"] == "changed"
    after = doc_row(conn, jid, "cv")
    assert after["status"] == "blocked" and json.loads(after["content"]) == {"replaced": True} and after["findings"] == row["findings"]


def test_migration_22_leaves_a_populated_database_untouched(tmp_path, monkeypatch):
    from tests.test_db import KEPT, counts, fill_children, put_job
    full = db.MIGRATIONS
    monkeypatch.setattr(db, "MIGRATIONS", full[:21])
    c = db.connect(tmp_path / "t.db")
    monkeypatch.setattr(db, "MIGRATIONS", full)
    fill_children(c, put_job(c, "a|x"))
    c.commit()
    before = counts(c, KEPT)
    docs = [tuple(r) for r in c.execute("SELECT * FROM documents ORDER BY id")]
    assert docs and db.migrate(c) == len(full)
    assert counts(c, KEPT) == before and [tuple(r) for r in c.execute("SELECT * FROM documents ORDER BY id")] == docs
    assert c.execute("SELECT COUNT(*) FROM finding_overrides").fetchone()[0] == 0
    c.close()
