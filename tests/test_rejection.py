"""app/rejection.py: the rejection shared by the web button and `python -m app reject` (goal.md 5.3.3)."""
import json

import pytest

from app import db, rejection


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "r.db")
    yield c
    c.close()


def add_job(conn, status="scored", key="k1"):
    cur = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) "
                       "VALUES (?, 'Data Engineer', 'ACME', ?, 't', 't')", (key, status))
    conn.commit()
    return cur.lastrowid


def add_approved_doc(conn, job_id, kind="cv"):
    conn.execute("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at, approved_at, approved_pdf_path) "
                 "VALUES (?, ?, '{}', 'approved', 'd', 'c', '2026-11-01T10:00:00Z', ?)", (job_id, kind, f"{job_id}_x/{kind}.pdf"))
    conn.commit()


def test_reject_archives_as_user_rejected_and_removes_the_job(conn):
    jid = add_job(conn)
    out = rejection.reject_job(conn, jid, "", "output/approved")
    assert (out.title, out.company, out.status, out.approved_kinds) == ("Data Engineer", "ACME", "scored", ())
    assert conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is None
    a = conn.execute("SELECT * FROM job_archive WHERE id = ?", (out.archive_id,)).fetchone()
    assert (a["rejection_stage"], a["reason_code"], a["original_job_id"], a["comment"]) == ("user", "user_rejected", jid, None)
    assert not conn.in_transaction


def test_comment_is_cleaned_and_stored(conn):
    assert rejection.clean_comment("  too far\n\tfrom  home ") == "too far from home"
    jid = add_job(conn)
    out = rejection.reject_job(conn, jid, rejection.clean_comment("too\nfar"), "output/approved")
    assert conn.execute("SELECT comment FROM job_archive WHERE id = ?", (out.archive_id,)).fetchone()[0] == "too far"


def test_comment_limit_constant():
    assert rejection.COMMENT_MAX == 300 and len(rejection.clean_comment("x" * 301)) > rejection.COMMENT_MAX


@pytest.mark.parametrize("status", [s for s in db.STATUSES if s not in rejection.REJECT_FROM])
def test_statuses_outside_reject_from_are_refused_and_nothing_changes(conn, status):
    jid = add_job(conn, status)
    with pytest.raises(rejection.RejectRefused) as exc:
        rejection.reject_job(conn, jid, "", "output/approved")
    assert f"'{status}'" in str(exc.value) and "Nothing was changed" in str(exc.value) and not exc.value.gone
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == status
    assert conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 0 and not conn.in_transaction


@pytest.mark.parametrize("status", rejection.REJECT_FROM)
def test_every_reject_from_status_can_be_rejected(conn, status):
    assert rejection.refusal(1, status) is None
    rejection.reject_job(conn, add_job(conn, status), "", "output/approved")


def test_unknown_and_already_archived_ids_are_refused_as_gone(conn):
    with pytest.raises(rejection.RejectRefused) as exc:
        rejection.reject_job(conn, 999, "", "output/approved")
    assert exc.value.gone and "not open any more" in str(exc.value)
    jid = add_job(conn)
    rejection.reject_job(conn, jid, "", "output/approved")
    with pytest.raises(rejection.RejectRefused) as exc:
        rejection.reject_job(conn, jid, "", "output/approved")
    assert exc.value.gone and conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 1


def test_approved_documents_are_noted_and_kept(conn):
    jid = add_job(conn, "approved")
    add_approved_doc(conn, jid, "cv")
    add_approved_doc(conn, jid, "cover_letter")
    assert rejection.approved_kinds(conn, jid) == ("cover_letter", "cv")
    out = rejection.reject_job(conn, jid, "no fit", "output/approved")
    assert out.approved_kinds == ("cover_letter", "cv")
    comment = conn.execute("SELECT comment FROM job_archive WHERE id = ?", (out.archive_id,)).fetchone()[0]
    assert comment.startswith("no fit; approved documents kept on disk: cover_letter, cv (output/approved/")
    assert len(json.loads(conn.execute("SELECT record FROM job_archive").fetchone()[0])["documents"]) == 2
