"""The Applied page (decision of 10 Nov 2026, goal.md 5.7): applied jobs leave /jobs and have their own page; jobs waiting for a
description (not_selected) are hidden from /jobs too. Every test builds its own temporary database; no network.
"""
import re

import pytest

from app import db, web
from tests.test_report import job, pipeline
from tests.test_web import HOST, add_doc, client, conn, cfg, cv, letter, pdfs         # noqa: F401  (fixtures and helpers)
from tests.test_web_writes import act, history, post, seen, snapshot, status_of, token  # noqa: F401


def apply_at(conn, job_id, when):
    """Mark a job applied through the status history, with a chosen date (when=None: no history row, the status only)."""
    if when is None:
        with conn:
            conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (job_id,))
        return
    db.set_status(conn, job_id, "applied", note="web")
    with conn:
        conn.execute("UPDATE status_history SET changed_at = ? WHERE job_id = ? AND to_status = 'applied'", (when, job_id))


@pytest.fixture
def world(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(1), job(2), job(3)], [55, 90, 20])
    return client(cfg, tmp_path), ids


def test_an_applied_job_is_not_on_the_jobs_list(world, conn):
    c, ids = world
    assert f'href="/jobs/{ids[0]}"' in c.get("/jobs").text
    apply_at(conn, ids[0], "2026-11-05T10:00:00Z")
    assert f'href="/jobs/{ids[0]}"' not in c.get("/jobs").text
    assert c.get(f"/jobs/{ids[0]}").status_code == 200                              # its own page still opens


def test_a_not_selected_job_is_on_the_list_and_its_page_opens(cfg, conn, tmp_path):
    pipeline(conn, cfg, [job(i) for i in range(1, 14)], list(range(40, 53)))
    low = conn.execute("SELECT id FROM jobs WHERE status = 'not_selected' ORDER BY id LIMIT 1").fetchone()["id"]
    c = client(cfg, tmp_path)
    assert f'href="/jobs/{low}"' in c.get("/jobs").text and f'href="/jobs/{low}"' not in c.get("/applied").text
    assert c.get(f"/jobs/{low}").status_code == 200


def test_both_lists_count_and_page_only_their_own_jobs(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "PAGE_SIZE", 2)
    ids, _ = pipeline(conn, cfg, [job(i) for i in range(1, 8)], [60, 61, 62, 63, 64, 65, 66])
    for n, i in enumerate(ids[:3]):
        apply_at(conn, i, f"2026-11-0{n + 1}T10:00:00Z")
    c = client(cfg, tmp_path)
    assert "Page 1 of 2" in c.get("/applied").text and "Page 2 of 2" in c.get("/applied?page=2").text
    assert "Page 2 of 2" in c.get("/applied?page=99").text                           # capped at the last page
    assert "Page 2 of 2" in c.get("/jobs?page=99").text and c.get("/jobs?page=99").text.count("<tr>") == 3      # 4 open jobs, 2 pages; header + 2 rows
    assert c.get("/applied?page=2").text.count("<tr>") == 2                           # header + 1 job
    for odd in ("0", "-1", "abc", "1e9"):
        assert c.get(f"/applied?page={odd}").status_code == 200


def test_the_applied_page_shows_the_newest_application_first_and_says_not_recorded(world, conn):
    c, ids = world
    apply_at(conn, ids[0], "2026-11-03T10:00:00Z")
    apply_at(conn, ids[1], "2026-11-07T10:00:00Z")
    apply_at(conn, ids[2], None)
    body = c.get("/applied").text
    assert body.index("Analyst 2") < body.index("Analyst 1") < body.index("Analyst 3")
    assert "2026-11-07" in body and "2026-11-03" in body and "not recorded" in body
    assert body.count("not recorded") == 1


def test_undo_from_the_applied_page_returns_the_job_to_the_jobs_list(world, conn):
    c, ids = world
    db.set_status(conn, ids[0], "shortlisted", note="web")
    db.set_status(conn, ids[0], "applied", note="web")
    page = c.get("/applied").text
    assert f'href="/jobs/{ids[0]}/undo-status?next=applied"' in page
    r = c.post(f"/jobs/{ids[0]}/undo-status", data={"csrf": c.app.state.csrf(f"undo-status:{ids[0]}"), "next": "applied", "confirm": "yes"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/applied")
    assert status_of(conn.execute("PRAGMA database_list").fetchone()[2], ids[0]) == "shortlisted"
    assert f'href="/jobs/{ids[0]}"' in c.get("/jobs").text and f'href="/jobs/{ids[0]}"' not in c.get("/applied").text


def test_approved_pdfs_link_to_the_existing_route_and_drafts_have_no_links(cfg, conn, tmp_path, pdfs):
    docs, approved = pdfs
    ids, _ = pipeline(conn, cfg, [job(1), job(2), job(3)], [80, 70, 60])
    (approved / "cv.pdf").write_bytes(b"%PDF-approved")
    add_doc(conn, ids[0], "cv", cv(), "approved", pdf="j/cv.pdf", approved="cv.pdf")
    add_doc(conn, ids[0], "cover_letter", letter(), "draft", pdf="j/l.pdf")
    add_doc(conn, ids[1], "cv", cv(), "approved", pdf="j/cv2.pdf", approved="")          # approved, no approved copy (job 176 case)
    add_doc(conn, ids[1], "cover_letter", letter(), "draft")
    for i in ids:
        apply_at(conn, i, "2026-11-05T10:00:00Z")
    c = client(cfg, tmp_path)
    body = c.get("/applied").text
    assert body.count("/approved-pdf") == 1 and f'href="/jobs/{ids[0]}/documents/cv/approved-pdf"' in body
    assert c.get(f"/jobs/{ids[0]}/documents/cv/approved-pdf").content == b"%PDF-approved"
    assert f"/jobs/{ids[1]}/documents/" not in body and f"/jobs/{ids[2]}/documents/" not in body


def test_the_applied_page_writes_nothing_and_has_no_script(world, conn):
    c, ids = world
    apply_at(conn, ids[0], "2026-11-05T10:00:00Z")
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    before = snapshot(path)
    r = c.get("/applied")
    assert r.status_code == 200 and "<script" not in r.text.replace('<script src="/static/select.js" defer></script>', "").lower() and "onclick" not in r.text.lower()
    assert snapshot(path) == before
    assert 'href="/applied">Applied' in c.get("/").text                               # the menu link


def test_reject_from_the_applied_page_archives_the_job_and_returns_there(world, conn):
    c, ids = world
    apply_at(conn, ids[0], "2026-11-05T10:00:00Z")
    assert f'href="/jobs/{ids[0]}/reject?next=applied"' in c.get("/applied").text
    assert c.get(f"/jobs/{ids[0]}/reject?next=applied").status_code == 200
    r = post(c, f"/jobs/{ids[0]}/reject", c.app.state.csrf(f"reject:{ids[0]}"), next="applied", confirm="yes", comment="")
    assert r.status_code == 303 and r.headers["location"].startswith("/applied")
    assert status_of(conn.execute("PRAGMA database_list").fetchone()[2], ids[0]) is None
    assert conn.execute("SELECT COUNT(*) FROM job_archive WHERE reason_code = 'user_rejected'").fetchone()[0] == 1
    assert "Analyst 1" not in c.get("/applied").text


def test_the_reject_page_of_an_applied_job_warns_and_that_of_a_scored_job_does_not(world, conn):
    c, ids = world
    apply_at(conn, ids[0], "2026-11-05T10:00:00Z")
    warning = "no longer counts"
    assert warning in c.get(f"/jobs/{ids[0]}/reject").text
    assert warning not in c.get(f"/jobs/{ids[1]}/reject").text


def test_a_scored_job_outside_the_top_10_is_on_the_jobs_list(cfg, conn, tmp_path):
    pipeline(conn, cfg, [job(i) for i in range(1, 14)], list(range(40, 53)))
    low = conn.execute("SELECT id FROM jobs WHERE status = 'not_selected' ORDER BY id LIMIT 1").fetchone()["id"]
    assert f'href="/jobs/{low}"' in client(cfg, tmp_path).get("/jobs").text


def test_a_job_waiting_for_a_description_is_not_on_the_jobs_list(cfg, conn, tmp_path):
    cfg["scoring"]["top_n"] = 2
    ids, _ = pipeline(conn, cfg, [job(1), job(2, description=""), job(3, description="")], [80, 60, 50])
    body = client(cfg, tmp_path).get("/jobs").text
    assert f'href="/jobs/{ids[0]}"' in body
    assert f'href="/jobs/{ids[1]}"' not in body                      # queued on /missing
    assert f'href="/jobs/{ids[2]}"' in body                          # title-only but not queued: an ordinary not_selected job
