"""Phase 3 slice 3e: the approval step (app/approval.py, migration 13, `approve-document`).

No network, no real config, no real database. PDFs and approved copies go to temporary folders (conftest)."""
import copy
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app import approval, db, documents
from app.config import ConfigError, load_config
from app.report import collect
from app.results import import_results
from tests.test_batch_results import NOW, results_file, score
from tests.test_cli import clean_logger, config_file, run  # noqa: F401
from tests.test_documents import CV, docs, letter, prepare

KINDS = ("cover_letter", "cv")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"]["exchange"] = str(tmp_path / "exchange")
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def imported(conn, cfg, scores=(90,), mutate=None, run_id="run1"):
    """Jobs with documents (rendered PDFs) for the first job; returns the ids."""
    ids = prepare(conn, cfg, list(scores), run_id=run_id)
    es = [score(i, s, **({"documents": (mutate(docs()) if mutate else docs())} if n == 0 else {}))
          for n, (i, s) in enumerate(zip(ids, scores))]
    import_results(conn, cfg, results_file(cfg, run_id, es, schema_version="1.1"), NOW)
    return ids


def doc_row(conn, job_id, kind):
    return dict(conn.execute("SELECT * FROM documents WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone())


def job_status(conn, job_id):
    return conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def by_kind(out):
    return {i.kind: i for i in out.items}


def files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# --- migration 13 ---------------------------------------------------------------------------------------------

def raw_at(path, number):
    raw = sqlite3.connect(path)
    for n, script in enumerate(db.MIGRATIONS[:number], start=1):
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {n};\nCOMMIT;")
    return raw


def test_migration_13_keeps_the_rows_the_constraints_the_index_and_the_cascade(tmp_path):
    path = tmp_path / "old.db"
    raw = raw_at(path, 12)
    raw.execute("INSERT INTO runs (id, started_at) VALUES ('r', 'x')")
    for k in ("a", "b"):
        raw.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES (?, 'T', 'C', 'scored', 'x', 'y')", (k,))
    raw.execute("INSERT INTO imports (batch_id, file_name, sha256, status, imported_at) VALUES ('r', 'f', 's', 'imported', 'x')")
    ins = ("INSERT INTO documents (job_id, kind, run_id, import_id, content, status, findings, generated_on, created_at, pdf_path, pages, rendered_at) "
           "VALUES (?, ?, 'r', 1, ?, ?, ?, '2026-11-01', 'c', ?, ?, ?)")
    raw.execute(ins, (1, "cv", '{"v":1}', "draft", "[]", "1_c/cv.pdf", 2, "t1"))
    raw.execute(ins, (1, "cover_letter", '{"v":2}', "blocked", '[{"kind":"number"}]', None, None, None))
    raw.execute(ins, (2, "cv", '{"v":3}', "draft", "[]", "2_c/cv.pdf", 1, "t3"))
    raw.commit()
    before = [tuple(r) for r in raw.execute("SELECT id, job_id, kind, run_id, import_id, content, status, findings, generated_on, "
                                            "created_at, pdf_path, pages, rendered_at FROM documents ORDER BY id")]
    raw.close()
    c = db.connect(path)
    assert db.schema_version(c) == len(db.MIGRATIONS) >= 13
    cols = "id, job_id, kind, run_id, import_id, content, status, findings, generated_on, created_at, pdf_path, pages, rendered_at"
    assert [tuple(r) for r in c.execute(f"SELECT {cols} FROM documents ORDER BY id")] == before          # rows and ids unchanged
    assert [tuple(r) for r in c.execute("SELECT approved_at, approved_pdf_path FROM documents")] == [(None, None)] * 3
    assert c.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_documents_job'").fetchone()
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'documents_new'").fetchone()
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    # unique (job, kind)
    ins2 = "INSERT INTO documents (job_id, kind, content, status, generated_on, created_at, approved_at) VALUES (?, ?, '{}', ?, 'd', 'c', ?)"
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins2, (1, "cv", "draft", None))
    # the status values: approved is allowed now (with its time), anything else is not
    c.execute(ins2, (2, "cover_letter", "approved", "2026-11-01T10:00:00Z"))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE documents SET status = 'approved', approved_at = NULL WHERE job_id = 2 AND kind = 'cv'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE documents SET status = 'final' WHERE job_id = 2 AND kind = 'cv'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins2, (2, "poster", "draft", None))
    # the foreign keys: a document cannot point at a missing job, and goes with its job
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(ins2, (99, "cv", "draft", None))
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.execute("DELETE FROM jobs WHERE id = 1")
    assert [r["job_id"] for r in c.execute("SELECT job_id FROM documents ORDER BY id")] == [2, 2]
    c.close()


def test_a_schema_10_database_goes_through_11_12_and_13(tmp_path):
    path = tmp_path / "ten.db"
    raw = raw_at(path, 10)                                        # the user's real database is at schema 10
    raw.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('k', 'Analyst', 'SIX', 'scored', 'a', 'b')")
    raw.commit()
    raw.close()
    c = db.connect(path)
    assert db.schema_version(c) == len(db.MIGRATIONS) >= 13
    cols = [r["name"] for r in c.execute("PRAGMA table_info(documents)")]
    assert cols[-5:] == ["pdf_path", "pages", "rendered_at", "approved_at", "approved_pdf_path"]
    assert c.execute("SELECT title, status FROM jobs").fetchone()[:] == ("Analyst", "scored")
    with c:
        c.execute("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at, approved_at) "
                  "VALUES (1, 'cv', '{}', 'approved', 'd', 'c', 'now')")
    assert c.execute("SELECT status FROM documents").fetchone()[0] == "approved"
    c.close()


def test_the_migration_rolls_back_as_a_whole(tmp_path):
    """A failure in the rebuild leaves the old table (the runner wraps a script in one transaction)."""
    path = tmp_path / "twelve.db"
    raw = raw_at(path, 12)
    raw.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('k', 'T', 'C', 'scored', 'a', 'b')")
    raw.execute("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at) VALUES (1, 'cv', '{}', 'draft', 'd', 'c')")
    raw.commit()
    raw.close()
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    broken = db.MIGRATIONS[12].replace("DROP TABLE documents;", "DROP TABLE documents; SELECT * FROM no_such_table;")
    with pytest.raises(sqlite3.OperationalError):
        c.executescript(f"BEGIN;\n{broken}\nPRAGMA user_version = 13;\nCOMMIT;")
    if c.in_transaction:
        c.execute("ROLLBACK")
    assert c.execute("PRAGMA user_version").fetchone()[0] == 12
    assert c.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'documents_new'").fetchone()
    c.close()


def test_the_config_key_has_a_default_and_is_checked(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    assert load_config()["paths"]["approved"] == "output/approved"
    bad = tmp_path / "c.yaml"
    bad.write_text("paths:\n  approved: ''\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="paths.approved"):
        load_config(bad)


# --- approving ------------------------------------------------------------------------------------------------

def test_approving_both_copies_the_pdfs_freezes_the_rows_and_approves_the_job(conn, cfg):
    ids = imported(conn, cfg)
    assert job_status(conn, ids[0]) == "scored"
    out = approval.approve(conn, cfg, ids[0], stamp="2026-11-01T10:00:00Z")
    assert [(i.kind, i.outcome) for i in out.items] == [("cover_letter", "approved"), ("cv", "approved")] and not out.refused
    assert out.job_status == "approved" and job_status(conn, ids[0]) == "approved"
    src, dst = documents.documents_dir(cfg), documents.approved_dir(cfg)
    for kind in KINDS:
        row = doc_row(conn, ids[0], kind)
        assert row["status"] == "approved" and row["approved_at"] == "2026-11-01T10:00:00Z"
        assert row["approved_pdf_path"] == row["pdf_path"]                   # same folder and file name, relative to paths.approved
        assert (dst / row["approved_pdf_path"]).read_bytes() == (src / row["pdf_path"]).read_bytes()
        assert (src / row["pdf_path"]).is_file()                             # the original stays
    assert files(dst) == sorted(r["pdf_path"] for r in conn.execute("SELECT pdf_path FROM documents"))
    assert f"{ids[0]}_acme/" in files(dst)[0] and files(dst)[0].endswith(".pdf")
    hist = conn.execute("SELECT from_status, to_status, note FROM status_history WHERE job_id = ? ORDER BY id DESC LIMIT 1", (ids[0],)).fetchone()
    assert tuple(hist) == ("scored", "approved", "both documents approved")


def test_one_document_alone_changes_nothing_on_the_job(conn, cfg):
    ids = imported(conn, cfg)
    out = approval.approve(conn, cfg, ids[0], ["cv"])
    assert [(i.kind, i.outcome) for i in out.items] == [("cv", "approved")] and out.job_status is None
    assert job_status(conn, ids[0]) == "scored"
    assert doc_row(conn, ids[0], "cover_letter")["status"] == "draft"
    assert len(files(documents.approved_dir(cfg))) == 1
    assert documents.state(conn, ids[0], cfg) == ("ready", 0)                  # one approved, one draft: still "ready"
    out = approval.approve(conn, cfg, ids[0], ["cover_letter"])                # the second one completes it
    assert out.job_status == "approved" and job_status(conn, ids[0]) == "approved"


def test_a_blocked_document_is_refused_with_its_findings(conn, cfg):
    def spoil(d):
        d["cv"]["experience"][0]["bullets"][0]["text"] = "Saved 5 million CHF at Northwind."
        return d
    ids = imported(conn, cfg, mutate=spoil)
    out = approval.approve(conn, cfg, ids[0])
    items = by_kind(out)
    assert items["cover_letter"].outcome == "approved"                          # each kind is decided on its own
    assert items["cv"].outcome == "refused" and out.refused
    assert "blocked" in items["cv"].message and "[number]" in items["cv"].message and "5 million" not in items["cv"].message
    assert doc_row(conn, ids[0], "cv")["status"] == "blocked" and job_status(conn, ids[0]) == "scored"
    only = approval.approve(conn, cfg, ids[0], ["cv"])
    assert only.refused and not documents.approved_dir(cfg).joinpath("x").exists()


def test_a_draft_without_a_pdf_is_refused_and_told_to_render(conn, cfg):
    ids = imported(conn, cfg)
    with conn:
        conn.execute("UPDATE documents SET pdf_path = NULL, pages = NULL, rendered_at = NULL WHERE kind = 'cv'")
    out = approval.approve(conn, cfg, ids[0], ["cv"])
    assert out.refused and "render-documents --job" in out.items[0].message and str(ids[0]) in out.items[0].message
    assert doc_row(conn, ids[0], "cv")["status"] == "draft"


def test_a_pdf_that_is_gone_from_the_disk_is_refused(conn, cfg):
    ids = imported(conn, cfg)
    (documents.documents_dir(cfg) / doc_row(conn, ids[0], "cv")["pdf_path"]).unlink()
    out = approval.approve(conn, cfg, ids[0], ["cv"])
    assert out.refused and "no PDF on disk" in out.items[0].message and doc_row(conn, ids[0], "cv")["status"] == "draft"
    assert files(documents.approved_dir(cfg)) == []


def test_a_job_without_documents_and_a_missing_kind_are_refused(conn, cfg):
    ids = imported(conn, cfg, scores=(90, 80))
    out = approval.approve(conn, cfg, ids[1])
    assert out.refused and [i.outcome for i in out.items] == ["refused", "refused"] and "no stored" in out.items[0].message
    assert approval.approve(conn, cfg, 999).refused
    with conn:
        conn.execute("DELETE FROM documents WHERE kind = 'cv'")
    out = approval.approve(conn, cfg, ids[0])
    assert by_kind(out)["cover_letter"].outcome == "approved" and by_kind(out)["cv"].outcome == "refused"
    assert job_status(conn, ids[0]) == "scored"                                # both would be needed


def test_a_warning_finding_is_shown_but_does_not_stop_the_approval(conn, cfg):
    cfg["tailoring"]["letter_max_pages"] = 1
    ids = prepare(conn, cfg, [90])
    long_letter = letter()
    long_letter["blocks"]["skills"] = " ".join(["I work carefully with stakeholders and teams every single day."] * 40)
    with conn:
        documents.store_document(conn, ids[0], "cover_letter", None, long_letter, [], date(2026, 11, 1), "x")
        documents.store_document(conn, ids[0], "cv", None, copy.deepcopy(CV), [], date(2026, 11, 1), "x")
    documents.render_stored(conn, cfg)
    out = approval.approve(conn, cfg, ids[0])
    assert by_kind(out)["cover_letter"].outcome == "approved" and by_kind(out)["cover_letter"].warnings == ["cover letter is 2 pages (limit 1)"]
    assert by_kind(out)["cv"].warnings == []


def test_approving_twice_is_idempotent(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], stamp="first")
    dst = documents.approved_dir(cfg)
    stat = {p: p.stat().st_mtime_ns for p in dst.rglob("*.pdf")}
    rows = [doc_row(conn, ids[0], k) for k in KINDS]
    history = conn.execute("SELECT COUNT(*) FROM status_history WHERE job_id = ?", (ids[0],)).fetchone()[0]
    out = approval.approve(conn, cfg, ids[0], stamp="second")
    assert [i.outcome for i in out.items] == ["already", "already"] and not out.refused and out.job_status is None
    assert "already approved on first" in out.items[0].message
    assert [doc_row(conn, ids[0], k) for k in KINDS] == rows                    # approved_at is the first time
    assert {p: p.stat().st_mtime_ns for p in dst.rglob("*.pdf")} == stat and len(stat) == 2
    assert not list(dst.rglob("*.part"))
    assert conn.execute("SELECT COUNT(*) FROM status_history WHERE job_id = ?", (ids[0],)).fetchone()[0] == history


def test_an_approved_row_whose_copy_vanished_is_reported(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0])
    (documents.approved_dir(cfg) / doc_row(conn, ids[0], "cv")["approved_pdf_path"]).unlink()
    out = approval.approve(conn, cfg, ids[0], ["cv"])
    assert out.items[0].outcome == "already" and "copy is missing" in out.items[0].warnings[0]


def test_a_leftover_identical_file_is_overwritten_a_different_one_is_not(conn, cfg):
    ids = imported(conn, cfg)
    src, dst = documents.documents_dir(cfg), documents.approved_dir(cfg)
    letter_row, cv_row = doc_row(conn, ids[0], "cover_letter"), doc_row(conn, ids[0], "cv")
    same = dst / letter_row["pdf_path"]
    same.parent.mkdir(parents=True)
    same.write_bytes((src / letter_row["pdf_path"]).read_bytes())               # a leftover that is byte-identical
    other = dst / cv_row["pdf_path"]
    other.write_bytes(b"not the same")                                          # a leftover that is not
    out = approval.approve(conn, cfg, ids[0])
    items = by_kind(out)
    assert items["cover_letter"].outcome == "approved"
    assert items["cv"].outcome == "refused" and str(other) in items["cv"].message
    assert other.read_bytes() == b"not the same"                                # never overwritten
    assert doc_row(conn, ids[0], "cv")["status"] == "draft" and job_status(conn, ids[0]) == "scored"


def test_a_pdf_path_pointing_outside_the_folders_is_refused(conn, cfg):
    ids = imported(conn, cfg)
    outside = documents.documents_dir(cfg).parent / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4 outside")
    with conn:
        conn.execute("UPDATE documents SET pdf_path = ? WHERE kind = 'cv'", (f"../{outside.name}",))
    out = approval.approve(conn, cfg, ids[0], ["cv"])
    assert out.refused and doc_row(conn, ids[0], "cv")["status"] == "draft"
    assert files(documents.approved_dir(cfg)) == []


@pytest.mark.parametrize("status", ["applied", "interview", "offer", "rejected", "withdrawn"])
def test_a_job_further_along_keeps_its_status(conn, cfg, status):
    ids = imported(conn, cfg)
    db.set_status(conn, ids[0], status)
    out = approval.approve(conn, cfg, ids[0])
    assert out.job_status is None and job_status(conn, ids[0]) == status and out.notes and status in out.notes[0]


def test_a_shortlisted_job_becomes_approved_and_goes_back_on_undo(conn, cfg):
    ids = imported(conn, cfg)
    db.set_status(conn, ids[0], "shortlisted")
    approval.approve(conn, cfg, ids[0])
    assert job_status(conn, ids[0]) == "approved"
    out = approval.undo(conn, cfg, ids[0], ["cv"])
    assert out.job_status == "shortlisted" and job_status(conn, ids[0]) == "shortlisted"


# --- undo -----------------------------------------------------------------------------------------------------

def test_undo_makes_a_draft_again_removes_only_the_copy_and_restores_the_job(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0])
    src, dst = documents.documents_dir(cfg), documents.approved_dir(cfg)
    original = {k: (src / doc_row(conn, ids[0], k)["pdf_path"]).read_bytes() for k in KINDS}
    out = approval.undo(conn, cfg, ids[0])
    assert [i.outcome for i in out.items] == ["undone", "undone"] and out.job_status == "scored"
    for kind in KINDS:
        row = doc_row(conn, ids[0], kind)
        assert row["status"] == "draft" and row["approved_at"] is None and row["approved_pdf_path"] is None
        assert (src / row["pdf_path"]).read_bytes() == original[kind]           # the original is untouched
    assert files(dst) == [] and not list(dst.glob("*"))                          # the copies and the empty job folder are gone
    assert job_status(conn, ids[0]) == "scored"
    assert documents.state(conn, ids[0], cfg) == ("ready", 0)


def test_undo_of_one_kind_keeps_the_other_approved_but_the_job_goes_back(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0])
    out = approval.undo(conn, cfg, ids[0], ["cover_letter"])
    assert out.job_status == "scored" and job_status(conn, ids[0]) == "scored"
    assert doc_row(conn, ids[0], "cv")["status"] == "approved" and doc_row(conn, ids[0], "cover_letter")["status"] == "draft"
    assert len(files(documents.approved_dir(cfg))) == 1
    assert approval.undo(conn, cfg, ids[0], ["cover_letter"]).refused            # now it is not approved any more


def test_undo_falls_back_to_scored_when_the_history_has_no_earlier_status(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0])
    with conn:
        conn.execute("DELETE FROM status_history WHERE to_status = 'approved'")
    assert approval.undo(conn, cfg, ids[0]).job_status == "scored"
    ids = imported(conn, cfg, scores=(95, 90), run_id="run2")[:1]
    with conn:
        conn.execute("UPDATE jobs SET status = 'approved' WHERE id = ?", (ids[0],))   # set by hand, no history row
        conn.execute("UPDATE documents SET status = 'approved', approved_at = 'x', approved_pdf_path = pdf_path WHERE job_id = ?", (ids[0],))
    assert approval.undo(conn, cfg, ids[0], ["cv"]).job_status == "scored"


def test_undo_does_not_touch_a_job_that_moved_on(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0])
    db.set_status(conn, ids[0], "applied")
    out = approval.undo(conn, cfg, ids[0])
    assert out.job_status is None and job_status(conn, ids[0]) == "applied"


def test_undo_of_a_document_that_is_not_approved_is_a_refusal(conn, cfg):
    ids = imported(conn, cfg)
    out = approval.undo(conn, cfg, ids[0])
    assert out.refused and all("not approved" in i.message for i in out.items)
    assert approval.undo(conn, cfg, 999).refused
    assert job_status(conn, ids[0]) == "scored"


def test_undo_with_the_copy_already_gone_still_works(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0])
    for p in documents.approved_dir(cfg).rglob("*.pdf"):
        p.unlink()
    out = approval.undo(conn, cfg, ids[0])
    assert not out.refused and doc_row(conn, ids[0], "cv")["status"] == "draft"


def test_a_failed_database_update_leaves_the_approved_copy_in_place(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], ["cv"])
    copy = documents.approved_dir(cfg) / doc_row(conn, ids[0], "cv")["approved_pdf_path"]
    conn.execute("CREATE TEMP TRIGGER no_update BEFORE UPDATE ON documents BEGIN SELECT RAISE(ABORT, 'locked'); END")
    with pytest.raises(sqlite3.Error):
        approval.undo(conn, cfg, ids[0], ["cv"])
    conn.execute("DROP TRIGGER no_update")
    assert copy.is_file() and doc_row(conn, ids[0], "cv")["status"] == "approved"


def test_an_approved_copy_that_cannot_be_removed_is_a_warning_not_a_refusal(conn, cfg, monkeypatch):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], ["cv"])

    def refuse(self, missing_ok=False):
        raise PermissionError("in use")
    monkeypatch.setattr(Path, "unlink", refuse)
    out = approval.undo(conn, cfg, ids[0], ["cv"])
    monkeypatch.undo()
    assert [i.outcome for i in out.items] == ["undone"] and "delete it by hand" in out.items[0].warnings[0]
    assert doc_row(conn, ids[0], "cv")["status"] == "draft"


def test_undo_never_removes_a_file_outside_the_approved_folder(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0])
    victim = documents.documents_dir(cfg) / doc_row(conn, ids[0], "cv")["pdf_path"]
    with conn:
        conn.execute("UPDATE documents SET approved_pdf_path = ? WHERE kind = 'cv'", (f"../documents/{victim.relative_to(documents.documents_dir(cfg)).as_posix()}",))
    root = documents.approved_dir(cfg)
    out = approval.undo(conn, cfg, ids[0], ["cv"])
    assert out.refused and victim.is_file() and doc_row(conn, ids[0], "cv")["status"] == "approved"
    assert root.exists()


# --- rendering and later results files leave approved documents alone -----------------------------------------------

def test_render_documents_skips_approved_documents(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], ["cv"])
    dst = documents.approved_dir(cfg)
    copy_path = dst / doc_row(conn, ids[0], "cv")["approved_pdf_path"]
    before = copy_path.read_bytes()
    original = documents.documents_dir(cfg) / doc_row(conn, ids[0], "cv")["pdf_path"]
    original.unlink()
    out = documents.render_stored(conn, cfg)
    assert [(j, k) for j, k, _w in out.skipped] == [(ids[0], "cv")] and "approved" in out.skipped[0][2]
    assert not original.exists()                                                # not rebuilt
    assert [(j, k) for j, k, _p, _n in out.rendered] == [(ids[0], "cover_letter")]
    assert copy_path.read_bytes() == before
    row = doc_row(conn, ids[0], "cv")
    assert row["status"] == "approved" and row["approved_pdf_path"]


def new_texts():
    d = docs(contact="Ms Muster")                                             # a different, valid cover letter
    d["cv"]["experience"].reverse()                                           # a different, valid CV
    return d


def test_a_later_results_file_does_not_replace_an_approved_document(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], ["cover_letter"])
    frozen = doc_row(conn, ids[0], "cover_letter")
    old_cv = doc_row(conn, ids[0], "cv")
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 90, documents=new_texts())], schema_version="1.1", name="second.json"), NOW)
    assert r.status == "imported" and r.documents_frozen == [(ids[0], "cover_letter")]
    assert [(j, k) for j, k, _s, _n in r.documents] == [(ids[0], "cv")]       # only the CV was stored
    assert doc_row(conn, ids[0], "cover_letter") == frozen                      # byte for byte the same row
    new_cv = doc_row(conn, ids[0], "cv")
    assert new_cv["content"] != old_cv["content"] and new_cv["status"] == "draft"
    warning = [w for w in r.warnings if "approved already" in w]
    assert warning and f"{ids[0]} cover_letter" in warning[0]
    summary = json.loads(conn.execute("SELECT summary FROM imports ORDER BY id DESC LIMIT 1").fetchone()["summary"])
    assert summary["documents_frozen"] == [[ids[0], "cover_letter"]]
    assert (documents.approved_dir(cfg) / frozen["approved_pdf_path"]).is_file()


def test_when_both_are_approved_nothing_is_stored_or_rendered(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0])
    before = [doc_row(conn, ids[0], k) for k in KINDS]
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 90, documents=new_texts())], schema_version="1.1", name="second.json"), NOW)
    assert sorted(r.documents_frozen) == [(ids[0], "cover_letter"), (ids[0], "cv")]
    assert r.documents == [] and r.documents_rendered == [] and r.documents_missing == []
    assert [doc_row(conn, ids[0], k) for k in KINDS] == before and job_status(conn, ids[0]) == "approved"
    last = conn.execute("SELECT id FROM imports ORDER BY id DESC LIMIT 1").fetchone()["id"]
    assert all(d["import_id"] != last for d in before)


def test_an_approved_document_stays_frozen_when_the_job_dropped_out_of_the_selection(conn, cfg):
    cfg["scoring"]["top_n"] = 1
    ids = prepare(conn, cfg, [70, 90])
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 70, documents=docs())], schema_version="1.1", name="a.json"), NOW)
    approval.approve(conn, cfg, ids[0])
    before = [doc_row(conn, ids[0], k) for k in KINDS]
    es = [score(ids[1], 90), score(ids[0], 70, documents=new_texts())]
    r = import_results(conn, cfg, results_file(cfg, "run1", es, schema_version="1.1", name="b.json"), NOW)
    assert conn.execute("SELECT selected FROM batch_jobs WHERE job_id = ?", (ids[0],)).fetchone()[0] == 0
    assert sorted(r.documents_frozen) == [(ids[0], "cover_letter"), (ids[0], "cv")] and r.documents_ignored == []
    assert [doc_row(conn, ids[0], k) for k in KINDS] == before


# --- the report ---------------------------------------------------------------------------------------------------------

def test_the_report_marker_says_approved_only_when_both_are(conn, cfg):
    ids = imported(conn, cfg)
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents ready"
    approval.approve(conn, cfg, ids[0], ["cv"])
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents ready"
    approval.approve(conn, cfg, ids[0], ["cover_letter"])
    r = collect(conn, cfg, "run1")
    assert r.jobs[0].documents == "documents approved" and "DOCUMENTS APPROVED" in r.text and "documents approved" in r.html
    assert "DOCUMENTS READY" not in r.text
    for p in documents.documents_dir(cfg).rglob("*.pdf"):                        # the approved marker does not depend on paths.documents
        p.unlink()
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents approved"
    approval.undo(conn, cfg, ids[0], ["cv"])
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents not rendered"


# --- the commands -----------------------------------------------------------------------------------------------------

def seeded(config_file):
    c = load_config(config_file)
    conn = db.connect(c["paths"]["database"])
    ids = prepare(conn, c, [90, 80])
    es = [score(ids[0], 90, documents=docs()), score(ids[1], 80, documents=docs())]
    import_results(conn, c, results_file(c, "run1", es, schema_version="1.1"), NOW)
    conn.close()
    return c, ids


def test_the_command_approves_both_and_prints_the_paths(config_file, capsys):
    c, ids = seeded(config_file)
    capsys.readouterr()
    assert run(config_file, "approve-document", "--job", str(ids[0])) == 0
    out = capsys.readouterr().out
    dst = documents.approved_dir(c)
    assert f"job {ids[0]} cover_letter: approved" in out and f"job {ids[0]} cv: approved" in out and str(dst / f"{ids[0]}_acme") in out
    assert f"job {ids[0]} is now 'approved'" in out and "2 document(s) approved, 0 already approved, 0 refused" in out
    assert len(files(dst)) == 2
    conn = db.connect(c["paths"]["database"])
    assert job_status(conn, ids[0]) == "approved" and job_status(conn, ids[1]) == "scored"
    conn.close()


def test_the_command_with_a_kind_and_twice(config_file, capsys):
    c, ids = seeded(config_file)
    assert run(config_file, "approve-document", "--job", str(ids[0]), "--kind", "cv") == 0
    out = capsys.readouterr().out
    assert "cv: approved" in out and "cover_letter" not in out and "is now" not in out
    assert run(config_file, "approve-document", "--job", str(ids[0]), "--kind", "cv") == 0
    assert "already approved on" in capsys.readouterr().out
    assert len(files(documents.approved_dir(c))) == 1


def test_the_command_refuses_with_exit_code_1(config_file, capsys):
    c, ids = seeded(config_file)
    conn = db.connect(c["paths"]["database"])
    with conn:
        conn.execute("UPDATE documents SET pdf_path = NULL WHERE job_id = ? AND kind = 'cv'", (ids[1],))
        conn.execute("UPDATE documents SET status = 'blocked', findings = ? WHERE job_id = ? AND kind = 'cover_letter'",
                     (json.dumps([{"kind": "number", "text": "5", "block": None, "where": "closing", "reason": "5 is not in the fact file", "block_text": ""}]), ids[1]))
    conn.close()
    capsys.readouterr()
    assert run(config_file, "approve-document", "--job", str(ids[1])) == 1
    err = capsys.readouterr().err
    assert "REFUSED: job" in err and "[number] closing: 5 is not in the fact file" in err and "render-documents --job" in err
    assert run(config_file, "approve-document", "--job", "999") == 1
    assert "has no stored" in capsys.readouterr().err
    assert files(documents.approved_dir(c)) == []


def test_the_command_prints_warnings(config_file, capsys):
    c, ids = seeded(config_file)
    conn = db.connect(c["paths"]["database"])
    with conn:
        conn.execute("UPDATE documents SET findings = ? WHERE job_id = ? AND kind = 'cv'",
                     (json.dumps([{"kind": "pdf_over_page_limit", "text": "3 pages", "block": None, "where": "pdf",
                                   "reason": "CV is 3 pages (limit 2)", "block_text": ""}]), ids[0]))
    conn.close()
    capsys.readouterr()
    assert run(config_file, "approve-document", "--job", str(ids[0])) == 0
    assert f"WARNING: job {ids[0]} cv: CV is 3 pages (limit 2)" in capsys.readouterr().err


def test_the_command_undo(config_file, capsys):
    c, ids = seeded(config_file)
    run(config_file, "approve-document", "--job", str(ids[0]))
    capsys.readouterr()
    assert run(config_file, "approve-document", "--job", str(ids[0]), "--undo", "--kind", "cv") == 0
    out = capsys.readouterr().out
    assert "cv: approval taken back" in out and f"job {ids[0]} is now 'scored'" in out and "1 document(s) taken back to draft" in out
    assert run(config_file, "approve-document", "--job", str(ids[0]), "--undo", "--kind", "cv") == 1          # not approved any more
    assert "not approved" in capsys.readouterr().err
    assert len(files(documents.approved_dir(c))) == 1


def test_the_documents_command_shows_the_approval(config_file, capsys):
    c, ids = seeded(config_file)
    run(config_file, "approve-document", "--job", str(ids[0]), "--kind", "cv")
    capsys.readouterr()
    assert run(config_file, "documents") == 0
    out = capsys.readouterr().out
    dst = documents.approved_dir(c)
    assert f"{ids[0]:>5} | cv           | approved |" in out
    assert str(dst / f"{ids[0]}_acme") in out and "approved 20" in out
    assert run(config_file, "documents", "--json") == 0
    listing = json.loads(capsys.readouterr().out)
    cv = next(d for d in listing if d["job_id"] == ids[0] and d["kind"] == "cv")
    assert cv["status"] == "approved" and cv["approved_at"] and cv["approved_pdf_path"] == cv["pdf_path"]
    other = next(d for d in listing if d["job_id"] == ids[0] and d["kind"] == "cover_letter")
    assert other["approved_at"] is None and other["approved_pdf_path"] is None
    assert run(config_file, "documents", "--job", str(ids[0])) == 0
    assert f"approved " in capsys.readouterr().out


def test_render_documents_says_it_skips_approved_documents(config_file, capsys):
    c, ids = seeded(config_file)
    run(config_file, "approve-document", "--job", str(ids[0]))
    capsys.readouterr()
    assert run(config_file, "render-documents") == 0
    out = capsys.readouterr().out
    assert f"skipped: job {ids[0]} cv: approved, frozen" in out and f"skipped: job {ids[0]} cover_letter: approved, frozen" in out
    assert "2 PDF(s) built, 2 skipped, 0 failed" in out


def test_the_approval_writes_only_the_two_configured_folders_and_never_uses_the_network(config_file, tmp_path):
    c, ids = seeded(config_file)
    watched = [tmp_path / "exchange"]
    before = {p: files(p) for p in watched if p.exists()}
    assert run(config_file, "approve-document", "--job", str(ids[0])) == 0       # the network is blocked by conftest
    assert run(config_file, "approve-document", "--job", str(ids[0]), "--undo") == 0
    assert {p: files(p) for p in watched if p.exists()} == before


def test_the_approval_does_not_log_the_text_of_a_document(config_file, tmp_path, capsys):
    c, ids = seeded(config_file)
    run(config_file, "approve-document", "--job", str(ids[0]))
    run(config_file, "approve-document", "--job", str(ids[0]), "--undo")
    log = (tmp_path / "logs" / "recruitment.log").read_text(encoding="utf-8")
    assert "approved" in log and "Process Automation" not in log and "Contoso" not in log


# --- archiving a job that has documents (v0.34.1) ---------------------------------------------------------------
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def captured():
    h, lg = _Capture(), logging.getLogger("recruitment")
    old_level = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    yield h.messages
    lg.removeHandler(h)
    lg.setLevel(old_level)


def archived_record(conn, aid):
    row = conn.execute("SELECT record, comment FROM job_archive WHERE id = ?", (aid,)).fetchone()
    return json.loads(row["record"]), row["comment"]


def test_archiving_a_job_with_approved_documents_keeps_the_rows_notes_it_and_leaves_the_files(conn, cfg, captured):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], stamp="2026-11-01T10:00:00Z")
    dst = documents.approved_dir(cfg)
    before = files(dst)
    rows = {k: doc_row(conn, ids[0], k) for k in KINDS}
    aid = db.archive_job(conn, ids[0], "user", "user_rejected", "not for me")
    record, comment = archived_record(conn, aid)
    assert {d["kind"]: d for d in record["documents"]} == rows           # all columns, content included
    folder = rows["cv"]["approved_pdf_path"].rsplit("/", 1)[0]
    assert comment == f"not for me; approved documents kept on disk: cover_letter, cv (output/approved/{folder})"
    assert files(dst) == before and len(before) == 2                      # the approved PDFs are untouched
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    msgs = [m for m in captured if "approved documents" in m]
    assert len(msgs) == 1 and str(ids[0]) in msgs[0] and "cover_letter, cv" in msgs[0]
    assert rows["cv"]["content"][:40] not in msgs[0]


def test_archiving_a_job_with_only_drafts_stores_them_without_a_note(conn, cfg, captured):
    ids = imported(conn, cfg)
    rows = {k: doc_row(conn, ids[0], k) for k in KINDS}
    aid = db.archive_job(conn, ids[0], "user", "user_rejected")
    record, comment = archived_record(conn, aid)
    assert {d["kind"]: d for d in record["documents"]} == rows and comment is None
    assert not [m for m in captured if "approved documents" in m]


def test_archiving_a_job_with_one_approved_document_names_only_that_kind(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], kinds=["cv"])
    _, comment = archived_record(conn, db.archive_job(conn, ids[0], "user", "user_rejected"))
    assert comment.startswith("approved documents kept on disk: cv (output/approved/")


def test_archiving_a_job_without_documents_has_an_empty_list_and_no_note(conn):
    jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) "
                       "VALUES ('k|1', 'T', 'C', 'new', '2026-11-01', '2026-11-01')").lastrowid
    record, comment = archived_record(conn, db.archive_job(conn, jid, "filter", "seniority", "too senior"))
    assert record["documents"] == [] and comment == "too senior"


def test_the_archive_note_names_the_configured_approved_folder(conn, cfg, tmp_path):
    cfg["paths"]["approved"] = str(tmp_path / "my approved")                # not the default output/approved
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], stamp="2026-11-01T10:00:00Z")
    folder = doc_row(conn, ids[0], "cv")["approved_pdf_path"].rsplit("/", 1)[0]
    aid = db.archive_job(conn, ids[0], "user", "user_rejected", approved_folder=cfg["paths"]["approved"])
    _, comment = archived_record(conn, aid)
    assert comment == f"approved documents kept on disk: cover_letter, cv ({cfg['paths']['approved'].replace(chr(92), '/')}/{folder})"
    assert "output/approved" not in comment


def test_a_filter_archive_names_the_configured_approved_folder(conn, cfg, tmp_path):
    from app.filters.base import FilterResult
    from app.filters.runner import run_filters
    cfg["paths"]["approved"] = str(tmp_path / "custom" / "approved_pdfs") + "/"
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], stamp="2026-11-01T10:00:00Z")
    folder = doc_row(conn, ids[0], "cv")["approved_pdf_path"].rsplit("/", 1)[0]
    outcome = run_filters(conn, ids[0], cfg, None, filters=[lambda job, c: [FilterResult("title", "fail", "title_exclude", "no")]])
    assert outcome == "fail"
    comment = conn.execute("SELECT comment FROM job_archive").fetchone()[0]
    expected = cfg["paths"]["approved"].replace(chr(92), "/")
    assert f"({expected}{folder})" in comment and "output/approved" not in comment


def test_the_archive_note_defaults_to_output_approved(conn, cfg):
    ids = imported(conn, cfg)
    approval.approve(conn, cfg, ids[0], kinds=["cv"])
    _, comment = archived_record(conn, db.archive_job(conn, ids[0], "user", "user_rejected"))
    assert "(output/approved/" in comment


def test_taking_back_the_approval_after_applied_and_its_undo_goes_back_before_the_approval(conn, cfg):
    ids = imported(conn, cfg)
    db.set_status(conn, ids[0], "shortlisted")
    approval.approve(conn, cfg, ids[0])
    db.set_status(conn, ids[0], "applied", "web")
    db.set_status(conn, ids[0], "approved", "web undo")
    approval.undo(conn, cfg, ids[0])
    assert job_status(conn, ids[0]) == "shortlisted"
