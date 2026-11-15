"""The missing-description queue (goal.md 5.5, v0.72.0): a job without a description that would be in the top 10 is
queued (batch_jobs.queue_rank), not ranked and not shown with its title-only score."""
import json
import sqlite3

import pytest

from app import db, descriptions, documents
from app.descriptions import apply_description
from app.report import collect
from app.results import import_results
from tests.test_batch_results import add_jobs, batch, count, results_file, score, status
from tests.test_batch_results import NOW, cfg, conn   # noqa: F401  (fixtures)
from tests.test_descriptions import COMPLETE_POSTING


def mixed(conn, cfg, described, title_only, top_n=3, run_id="run1"):
    """Jobs with and without a description in one batch; returns (ids with, ids without)."""
    cfg["scoring"]["top_n"] = top_n
    with_text = add_jobs(conn, cfg, len(described), start=1) if described else []
    without = add_jobs(conn, cfg, len(title_only), start=len(described) + 1, description="") if title_only else []
    batch(conn, cfg, run_id)
    return with_text, without


def queue_ranks(conn, batch_id="run1"):
    return {r["job_id"]: r["queue_rank"] for r in conn.execute("SELECT job_id, queue_rank FROM batch_jobs WHERE batch_id = ?", (batch_id,))}


def rank_of(conn, job_id, batch_id="run1"):
    return conn.execute("SELECT selected, rank FROM batch_jobs WHERE batch_id = ? AND job_id = ?", (batch_id, job_id)).fetchone()


# --- migration 18 ----------------------------------------------------------------------------------------

def test_migration_18_adds_queue_rank_and_keeps_old_rows_null(tmp_path, monkeypatch):
    assert len(db.MIGRATIONS) >= 18
    full = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", full[:17])
    old = db.connect(tmp_path / "old.db")
    assert "queue_rank" not in [r[1] for r in old.execute("PRAGMA table_info(batch_jobs)")]
    old.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES ('b', 't', 'f', 's', 1, 1)")
    old.execute("INSERT INTO batch_jobs (batch_id, job_id, fresh) VALUES ('b', 7, 1)")
    old.commit()
    monkeypatch.setattr(db, "MIGRATIONS", full)
    assert db.migrate(old) == len(full)
    assert tuple(old.execute("SELECT queue_rank, title_only FROM batch_jobs WHERE job_id = 7").fetchone()) == (None, None)
    old.close()
    fresh = db.connect(tmp_path / "new.db")
    assert {"queue_rank", "title_only"} <= {r[1] for r in fresh.execute("PRAGMA table_info(batch_jobs)")}
    fresh.close()


# --- select_top ------------------------------------------------------------------------------------------

def test_a_title_only_job_in_the_would_be_top_ten_is_queued_not_selected(conn, cfg):
    described, title_only = mixed(conn, cfg, [1, 2, 3], [1])                     # top_n = 3
    entries = [score(described[0], 90), score(described[1], 80), score(described[2], 50), score(title_only[0], 60)]
    r = import_results(conn, cfg, results_file(cfg, "run1", entries), NOW)
    assert title_only[0] not in r.top
    assert tuple(rank_of(conn, title_only[0])) == (0, None)
    assert queue_ranks(conn)[title_only[0]] == 3                                 # the 3rd place of the would-be list (90, 80, 60)
    assert status(conn, title_only[0]) == "not_selected"
    assert [queue_ranks(conn)[i] for i in described] == [None, None, None]


def test_the_next_described_job_moves_up_into_the_freed_place(conn, cfg):
    described, title_only = mixed(conn, cfg, [1, 2, 3, 4], [1])
    entries = [score(described[0], 90), score(described[1], 80), score(described[2], 50), score(described[3], 40), score(title_only[0], 60)]
    r = import_results(conn, cfg, results_file(cfg, "run1", entries), NOW)
    assert r.top == described[:3]                                                # 90, 80, 50: the 50 moved up past the queued 60
    assert tuple(rank_of(conn, described[2])) == (1, 3) and status(conn, described[2]) == "scored"
    assert status(conn, described[3]) == "not_selected"


def test_a_title_only_job_outside_the_would_be_top_ten_keeps_its_score_and_is_not_queued(conn, cfg):
    described, title_only = mixed(conn, cfg, [1, 2], [1], top_n=2)
    entries = [score(described[0], 90), score(described[1], 80), score(title_only[0], 60)]
    r = import_results(conn, cfg, results_file(cfg, "run1", entries), NOW)
    assert r.top == described
    assert queue_ranks(conn)[title_only[0]] is None and status(conn, title_only[0]) == "not_selected"
    assert conn.execute("SELECT overall FROM scores WHERE job_id = ?", (title_only[0],)).fetchone()[0] == 60


def test_title_only_jobs_never_fill_a_freed_place(conn, cfg):
    described, title_only = mixed(conn, cfg, [1], [1, 2], top_n=2)
    entries = [score(described[0], 90), score(title_only[0], 60), score(title_only[1], 55)]
    r = import_results(conn, cfg, results_file(cfg, "run1", entries), NOW)
    assert r.top == described                                                    # place 2 stays empty
    ranks = queue_ranks(conn)
    assert ranks[title_only[0]] == 2 and ranks[title_only[1]] is None


def test_the_queue_holds_at_most_top_n_jobs_per_batch(conn, cfg):
    _, title_only = mixed(conn, cfg, [], list(range(15)), top_n=10)
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(i, 60) for i in title_only]), NOW)
    assert r.top == []
    ranks = queue_ranks(conn)
    assert sorted(v for v in ranks.values() if v) == list(range(1, 11))
    assert sum(1 for v in ranks.values() if v is None) == 5


def test_require_description_false_keeps_title_only_jobs_in_the_top_ten(conn, cfg):
    cfg["tailoring"]["require_description"] = False
    described, title_only = mixed(conn, cfg, [1, 2], [1])
    entries = [score(described[0], 90), score(described[1], 50), score(title_only[0], 60)]
    r = import_results(conn, cfg, results_file(cfg, "run1", entries), NOW)
    assert r.top == [described[0], title_only[0], described[1]]
    assert set(queue_ranks(conn).values()) == {None}


def test_a_later_chunk_takes_a_job_out_of_the_queue(conn, cfg):
    described, title_only = mixed(conn, cfg, [1, 2, 3], [1], top_n=2)
    first = results_file(cfg, "run1", [score(title_only[0], 60), score(described[0], 50)], name="results_run1.p01.json")
    import_results(conn, cfg, first, NOW)
    assert queue_ranks(conn)[title_only[0]] == 1                                 # would-be: 60, 50
    second = results_file(cfg, "run1", [score(described[1], 90), score(described[2], 80)], name="results_run1.p02.json")
    r = import_results(conn, cfg, second, NOW)
    assert r.top == [described[1], described[2]]
    assert queue_ranks(conn)[title_only[0]] is None                              # pushed out of the would-be list
    assert status(conn, title_only[0]) == "not_selected"


def test_a_queued_job_with_a_description_is_scored_again_fresh_and_leaves_the_queue(conn, cfg):
    described, title_only = mixed(conn, cfg, [1], [1], top_n=2)
    import_results(conn, cfg, results_file(cfg, "run1", [score(described[0], 70), score(title_only[0], 60)]), NOW)
    assert [q["id"] for q in descriptions.queued_jobs(conn)] == title_only
    apply_description(conn, cfg, title_only[0], COMPLETE_POSTING, require_complete=True)
    assert descriptions.queued_jobs(conn) == []                                  # it has a description now
    assert conn.execute("SELECT needs_rescore FROM jobs WHERE id = ?", (title_only[0],)).fetchone()[0] == 1
    batch(conn, cfg, "run2", NOW)
    r = import_results(conn, cfg, results_file(cfg, "run2", [score(title_only[0], 88)], name="r2.json"), NOW)
    assert r.top == title_only
    assert queue_ranks(conn, "run2")[title_only[0]] is None and status(conn, title_only[0]) == "scored"


def test_the_session_top_list_with_title_only_jobs_gives_no_differs_warning(conn, cfg):
    described, title_only = mixed(conn, cfg, [1, 2, 3], [1])
    entries = [score(described[0], 90), score(described[1], 80), score(described[2], 50), score(title_only[0], 60)]
    would_be = [described[0], described[1], title_only[0]]                       # what the session's prompt rule gives
    r = import_results(conn, cfg, results_file(cfg, "run1", entries, top=would_be), NOW)
    assert not any("differs" in w for w in r.warnings)
    assert r.top == described                                                    # the NAS selection skips the queued job


def test_documents_for_a_queued_job_are_ignored_with_the_no_description_reason(conn, cfg):
    described, title_only = mixed(conn, cfg, [1, 2, 3], [1])
    entries = [score(described[0], 90), score(described[1], 80), score(described[2], 50), score(title_only[0], 60)]
    import_results(conn, cfg, results_file(cfg, "run1", entries), NOW)
    assert documents.not_eligible_reason(conn, "run1", title_only[0], cfg) == documents.NO_DESCRIPTION_REASON


def test_the_report_lists_queued_jobs_and_the_outside_line_ignores_them(conn, cfg):
    described, title_only = mixed(conn, cfg, [1, 2, 3], [1])
    entries = [score(described[0], 90), score(described[1], 80), score(described[2], 55), score(title_only[0], 60)]
    import_results(conn, cfg, results_file(cfg, "run1", entries), NOW)
    rep = collect(conn, cfg, "run1")
    assert [m.job_id for m in rep.missing] == title_only and rep.missing[0].place == 3
    assert rep.outside_count == 0                                                # the queued 60 is not "outside the top 10"
    assert "WAITING FOR A DESCRIPTION (1)" in rep.text and "Analyst 4" in rep.text
    assert "/missing" not in rep.text and "/missing" not in rep.html             # no web_base_url: no link
    assert "waiting for a description" in rep.html.lower()
    cfg["report"]["web_base_url"] = "http://127.0.0.1:8000"
    rep = collect(conn, cfg, "run1")
    assert "http://127.0.0.1:8000/missing" in rep.text and 'href="http://127.0.0.1:8000/missing"' in rep.html
    assert "score 60" not in rep.text.lower()


# --- a pasted description does not make the title-only estimate rankable (M1) ---------------------------------

def _setup_paste(conn, cfg):
    cfg["scoring"]["top_n"] = 2
    d = add_jobs(conn, cfg, 3, start=1)
    t = add_jobs(conn, cfg, 1, start=4, description="")
    batch(conn, cfg, "run1")
    return d, t[0]


def test_a_refresh_after_a_paste_does_not_rank_the_title_only_estimate(conn, cfg):
    from app.results import refresh_selection
    d, j = _setup_paste(conn, cfg)
    import_results(conn, cfg, results_file(cfg, "run1", [score(d[0], 90), score(j, 80), score(d[1], 70), score(d[2], 50)]), NOW)
    assert queue_ranks(conn)[j] == 2
    apply_description(conn, cfg, j, COMPLETE_POSTING, require_complete=True)
    db.archive_job(conn, d[0], stage="user", reason_code="user_rejected")
    refresh_selection(conn, cfg)
    assert rank_of(conn, j)["selected"] == 0
    assert j not in [m.job_id for m in collect(conn, cfg, "run1").jobs]


def test_a_late_chunk_after_a_paste_does_not_rank_the_title_only_estimate(conn, cfg):
    d, j = _setup_paste(conn, cfg)
    import_results(conn, cfg, results_file(cfg, "run1", [score(j, 80), score(d[2], 50)], name="results_run1.p01.json"), NOW)
    apply_description(conn, cfg, j, COMPLETE_POSTING, require_complete=True)
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(d[0], 90), score(d[1], 70)], name="results_run1.p02.json"), NOW)
    assert j not in r.top


def test_a_paste_between_the_export_and_the_import_does_not_rank_the_title_only_estimate(conn, cfg):
    d, j = _setup_paste(conn, cfg)
    apply_description(conn, cfg, j, COMPLETE_POSTING, require_complete=True)          # the session scored the title only
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(d[0], 70), score(j, 80), score(d[1], 60), score(d[2], 50)]), NOW)
    assert j not in r.top and r.top == [d[0], d[1]]
    assert queue_ranks(conn)[j] == 1
    assert conn.execute("SELECT needs_rescore FROM jobs WHERE id = ?", (j,)).fetchone()[0] == 1       # it is scored again next time
    assert descriptions.queued_jobs(conn) == []                                   # the text is there: not listed as missing


def test_the_batch_row_stores_title_only_at_export(conn, cfg):
    d, j = _setup_paste(conn, cfg)
    rows = {r["job_id"]: r["title_only"] for r in conn.execute("SELECT job_id, title_only FROM batch_jobs WHERE batch_id = 'run1'")}
    assert rows[j] == 1 and [rows[i] for i in d] == [0, 0, 0]


def test_after_a_paste_the_job_page_list_and_report_show_added_not_the_estimate(conn, cfg):
    d, j = _setup_paste(conn, cfg)
    import_results(conn, cfg, results_file(cfg, "run1", [score(d[0], 90), score(j, 80), score(d[1], 70), score(d[2], 50)]), NOW)
    apply_description(conn, cfg, j, COMPLETE_POSTING, require_complete=True)
    from app import web
    assert web.is_queued(conn, j) is True
    detail = web.job_detail(conn, cfg, j)
    assert detail["queued"] and detail["added"] and detail["score"] is None
    with conn:
        conn.execute("UPDATE jobs SET status = 'shortlisted' WHERE id = ?", (j,))         # decision of 10 Nov: not_selected jobs are not on the list
    row = next(r for r in web.job_list(conn, cfg, 1)[0] if r["id"] == j)
    assert row["added"] and row["score"] is None
    assert [r for r in descriptions.jobs_needing_description(conn)] == []         # it has a text now
