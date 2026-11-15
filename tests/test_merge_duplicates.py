"""merge-duplicates (v0.77.1, goal.md 5.2): two open jobs that only differ in the company spelling become one. Temporary
databases and folders only; the command is driven through cli.main like the user does."""
import json
import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from app import backup, cli, db, pipeline
from app.config import load_config
from app.dedup import ingest_job, legacy_dedup_key
from app.models import NormalizedJob

OLD = "2026-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def clean_logger():
    log = logging.getLogger("recruitment")
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    yield
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


@pytest.fixture
def env(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"paths:\n  database: {(tmp_path / 'data' / 'jobs.db').as_posix()}\n  logs: {(tmp_path / 'logs').as_posix()}\n"
        f"  lock: {(tmp_path / 'data' / 'run.lock').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
        f"backup:\n  dir: {(tmp_path / 'backups').as_posix()}\n  keep: 2\n", encoding="utf-8")

    class Env:
        pass

    e = Env()
    e.cfg_path, e.cfg, e.tmp = cfg_path, load_config(cfg_path), tmp_path
    e.live, e.dir = tmp_path / "data" / "jobs.db", tmp_path / "backups"
    return e


def run(env, *args):
    return cli.main(["--config", str(env.cfg_path), *args])


def sighting(company="AXA", ext="1", source="s1", title="Analyst", places=("Winterthur",)):
    return NormalizedJob(source=source, external_id=ext, title=title, company=company, url=f"https://x.example/{source}/{ext}",
                         locations=list(places), source_date="2026-11-01")


def add_legacy(conn, company="AXA Switzerland", ext="1", source="s1", title="Analyst", places=("Winterthur",), status="new"):
    """A job stored the way v0.75 stored it: under the legacy key (company without legal form only)."""
    r = ingest_job(conn, sighting(company, ext, source, title, places))
    assert r.outcome == "new"
    conn.execute("UPDATE jobs SET dedup_key = ?, status = ? WHERE id = ?",
                 (legacy_dedup_key(company, title, list(places)), status, r.job_id))
    conn.commit()
    return r.job_id


def add_score(conn, job_id, overall=70):
    return conn.execute("INSERT INTO scores (job_id, prompt_version, model, overall, subscores, created_at) VALUES (?,?,?,?,?,?)",
                        (job_id, "p", "m", overall, "{}", db.utcnow())).lastrowid


def add_doc(conn, job_id, kind="cover_letter", status="draft"):
    conn.execute("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at, pdf_path, approved_at) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (job_id, kind, "{}", status, "2026-11-01", db.utcnow(), f"{job_id}_x/{kind}.pdf",
                  db.utcnow() if status == "approved" else None))
    conn.commit()


def pair(env, loser_status="new", survivor_status="shortlisted"):
    """Job 1 'AXA Switzerland' (legacy key) and job 2 'AXA' from another source: the case of 4 Nov 2026 (189 and 192)."""
    conn = db.connect(env.live)
    a = add_legacy(conn, status=survivor_status)
    b = ingest_job(conn, sighting("AXA", ext="9", source="s2")).job_id
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (loser_status, b))
    conn.commit()
    return conn, a, b


def ids(env):
    c = sqlite3.connect(env.live)
    try:
        return [r[0] for r in c.execute("SELECT id FROM jobs ORDER BY id")]
    finally:
        c.close()


def backups(env):
    return sorted(p.name for p in env.dir.iterdir()) if env.dir.exists() else []


# --- the failing-first case ---------------------------------------------------------------------------------------

def test_axa_switzerland_and_axa_become_one_job_and_the_next_sighting_links(env, capsys):
    conn, a, b = pair(env, loser_status="new", survivor_status="shortlisted")
    assert ids(env) == [1, 2]                                         # before: two jobs for one posting
    conn.close()
    assert run(env, "merge-duplicates") == 0
    assert ids(env) == [1]
    conn = db.connect(env.live)
    assert conn.execute("SELECT dedup_key FROM jobs").fetchone()[0] == "axa|analyst|winterthur"
    assert conn.execute("SELECT COUNT(*) FROM job_sources WHERE job_id = 1").fetchone()[0] == 2
    again = ingest_job(conn, sighting("AXA", ext="77", source="s3"))
    assert again.outcome == "linked" and again.job_id == 1
    row = conn.execute("SELECT survivor_job_id, merged_job_id, survivor_old_key, new_key, reason, backup_file FROM job_merges").fetchone()
    assert tuple(row)[:5] == (1, 2, "axa switzerland|analyst|winterthur", "axa|analyst|winterthur", "company_key")
    assert row["backup_file"].startswith("jobs_pre-merge_")
    out = capsys.readouterr().out
    assert "keep 1" in out and "merged" in out and row["backup_file"] in out


# --- survivor order ---------------------------------------------------------------------------------------------------

def _flow(conn, a, b):
    conn.execute("UPDATE jobs SET status = 'new' WHERE id = ?", (a,))
    conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (b,))


def _description(conn, a, b):
    conn.execute("UPDATE jobs SET description = ?, description_missing = 0 WHERE id = ?", ("d" * 400, b))


def _score(conn, a, b):
    add_score(conn, b)


def _documents(conn, a, b):
    add_doc(conn, b)


def _oldest(conn, a, b):
    conn.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", (OLD, b))


@pytest.mark.parametrize("prepare", [_flow, _description, _score, _documents, _oldest], ids=lambda f: f.__name__)
def test_survivor_is_the_one_that_wins_each_tiebreak(env, prepare):
    conn, a, b = pair(env, "scored", "scored")
    prepare(conn, a, b)
    conn.commit(); conn.close()
    assert run(env, "merge-duplicates") == 0
    assert ids(env) == [b]


def test_equal_jobs_keep_the_lowest_id(env):
    conn, a, b = pair(env, "scored", "scored")
    conn.execute("UPDATE jobs SET first_seen_at = ?", (OLD,)); conn.commit(); conn.close()
    assert run(env, "merge-duplicates") == 0
    assert ids(env) == [a]


def test_a_group_of_three_becomes_one(env):
    conn = db.connect(env.live)
    add_legacy(conn, "AXA Switzerland", ext="1", source="s1")
    add_legacy(conn, "AXA Schweiz", ext="5", source="s4")
    ingest_job(conn, sighting("AXA", ext="9", source="s2"))
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    conn.close()
    assert run(env, "merge-duplicates") == 0
    assert len(ids(env)) == 1
    c = sqlite3.connect(env.live)
    assert c.execute("SELECT COUNT(*) FROM job_merges").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 3


# --- what is moved, what is kept ---------------------------------------------------------------------------------

def test_survivor_state_is_not_touched_and_no_status_history_row_is_added(env):
    conn, a, b = pair(env, loser_status="scored", survivor_status="shortlisted")
    conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, created_at) VALUES (?, 'location', 'pass', ?)", (a, db.utcnow()))
    conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, created_at) VALUES (?, 'location', 'fail', ?)", (b, db.utcnow()))
    s = add_score(conn, a)
    add_score(conn, b, 99)
    conn.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", (OLD, a))
    conn.execute("INSERT INTO status_history (job_id, from_status, to_status, changed_at) VALUES (?, 'new', 'shortlisted', ?)", (a, db.utcnow()))
    conn.commit()
    before_hist = conn.execute("SELECT COUNT(*) FROM status_history WHERE job_id = ?", (a,)).fetchone()[0]
    conn.close()
    assert run(env, "merge-duplicates") == 0
    conn = db.connect(env.live)
    assert ids(env) == [a]
    assert conn.execute("SELECT id FROM scores WHERE job_id = ? ORDER BY id DESC LIMIT 1", (a,)).fetchone()[0] == s
    assert conn.execute("SELECT outcome FROM job_filter_results WHERE job_id = ? ORDER BY id DESC LIMIT 1", (a,)).fetchone()[0] == "pass"
    assert conn.execute("SELECT COUNT(*) FROM status_history WHERE job_id = ?", (a,)).fetchone()[0] == before_hist
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (a,)).fetchone()[0] == "shortlisted"
    rec = json.loads(conn.execute("SELECT record FROM job_merges").fetchone()[0])
    assert len(rec["scores"]) == 1 and rec["scores"][0]["overall"] == 99 and rec["filter_results"][0]["outcome"] == "fail"
    assert conn.execute("SELECT first_seen_at FROM jobs WHERE id = ?", (a,)).fetchone()[0] == OLD


def test_survivor_dates_are_merged(env):
    conn, a, b = pair(env, loser_status="new", survivor_status="shortlisted")
    conn.execute("UPDATE jobs SET first_seen_at = ?, last_seen_at = '2026-05-01T00:00:00Z' WHERE id = ?", (OLD, b))
    conn.execute("UPDATE jobs SET last_seen_at = '2026-02-01T00:00:00Z', closed_at = '2026-03-01T00:00:00Z' WHERE id = ?", (a,))
    conn.commit(); conn.close()
    assert run(env, "merge-duplicates") == 0
    c = sqlite3.connect(env.live)
    assert c.execute("SELECT first_seen_at, last_seen_at, closed_at FROM jobs").fetchone() == (OLD, "2026-05-01T00:00:00Z", None)


def test_drafts_move_only_when_the_survivor_lacks_the_kind(env):
    conn, a, b = pair(env, loser_status="scored", survivor_status="shortlisted")
    add_doc(conn, a, "cv")
    add_doc(conn, b, "cv")
    add_doc(conn, b, "cover_letter")
    conn.close()
    assert run(env, "merge-duplicates") == 0
    conn = db.connect(env.live)
    rows = conn.execute("SELECT job_id, kind, pdf_path FROM documents ORDER BY kind").fetchall()
    assert [(r["job_id"], r["kind"]) for r in rows] == [(a, "cover_letter"), (a, "cv")]
    assert rows[0]["pdf_path"].startswith(f"{b}_") and rows[1]["pdf_path"].startswith(f"{a}_")      # kept as written, relative


def test_a_loser_with_an_approved_document_skips_the_group(env, capsys):
    conn, a, b = pair(env, loser_status="scored", survivor_status="scored")
    add_doc(conn, b, "cv", "approved")
    conn.execute("UPDATE jobs SET description = ?, description_missing = 0 WHERE id = ?", ("d" * 400, a))       # a wins, so b is the loser
    conn.commit(); conn.close()
    assert run(env, "merge-duplicates") == 0
    assert ids(env) == [a, b] and "SKIPPED" in capsys.readouterr().out


# --- skips ----------------------------------------------------------------------------------------------------------------

def test_two_jobs_in_work_are_skipped_and_listed(env, capsys):
    conn, a, b = pair(env, loser_status="applied", survivor_status="shortlisted")
    conn.close()
    assert run(env, "merge-duplicates") == 0
    out = capsys.readouterr().out
    assert ids(env) == [a, b] and "SKIPPED" in out and "in work" in out
    assert backups(env) == []                                          # nothing to write, so no backup


def test_an_awaiting_llm_job_skips_the_group_and_is_not_re_keyed(env, capsys):
    conn, a, b = pair(env, loser_status="awaiting_llm", survivor_status="scored")
    conn.close()
    assert run(env, "merge-duplicates") == 0
    out = capsys.readouterr().out
    assert ids(env) == [a, b] and "awaiting_llm" in out and "finish" in out
    c = sqlite3.connect(env.live)
    assert c.execute("SELECT dedup_key FROM jobs WHERE id = ?", (a,)).fetchone()[0] == "axa switzerland|analyst|winterthur"


# --- dry run ---------------------------------------------------------------------------------------------------------

def test_dry_run_changes_nothing_and_makes_no_backup(env, capsys):
    conn, a, b = pair(env, "new", "shortlisted")
    conn.close()
    before = env.live.read_bytes()
    assert run(env, "merge-duplicates", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dry run" in out and f"keep {a}" in out and "1 to merge" in out
    assert env.live.read_bytes() == before and backups(env) == []


def test_dry_run_on_a_schema_19_file_does_not_migrate(env, capsys):
    conn, a, b = pair(env)
    conn.execute("DROP TABLE IF EXISTS job_merges")
    conn.execute("PRAGMA user_version = 19")
    conn.commit()
    conn.close()
    assert run(env, "merge-duplicates", "--dry-run") == 0
    c = sqlite3.connect(env.live)
    try:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 19
    finally:
        c.close()
    assert backups(env) == [] and "1 to merge" in capsys.readouterr().out


def test_dry_run_warns_about_a_pending_description_file_of_a_loser(env, capsys):
    conn, a, b = pair(env, "new", "shortlisted")
    conn.close()
    folder = env.tmp / "exchange" / "descriptions"
    folder.mkdir(parents=True)
    (folder / f"{b}.txt").write_text("x", encoding="utf-8")
    assert run(env, "merge-duplicates", "--dry-run") == 0
    assert f"{b}.txt" in capsys.readouterr().out


# --- safety ------------------------------------------------------------------------------------------------------------

def test_backup_failure_writes_nothing(env, monkeypatch, capsys):
    conn, a, b = pair(env)
    conn.close()

    def boom(*args, **kwargs):
        raise backup.BackupError("disk full")
    monkeypatch.setattr(backup, "create_backup", boom)
    assert run(env, "merge-duplicates") == 1
    assert ids(env) == [a, b] and "disk full" in capsys.readouterr().err


def test_lock_held_exits_2_and_writes_nothing(env):
    conn, a, b = pair(env)
    conn.close()
    with pipeline.run_lock(env.cfg):
        assert run(env, "merge-duplicates") == 2
    assert ids(env) == [a, b] and backups(env) == []


def test_the_pre_merge_backup_is_a_checked_copy_that_rotation_never_removes(env):
    conn, a, b = pair(env)
    conn.close()
    assert run(env, "merge-duplicates") == 0
    names = [n for n in backups(env) if n.startswith("jobs_pre-merge_")]
    assert len(names) == 1
    info = backup.verify_backup(env.dir / names[0])
    assert info.counts["jobs"] == 2                                    # the state before the merge
    for i in range(3):
        backup.create_backup(env.live, env.dir, 1, now=datetime(2026, 11, 5 + i, tzinfo=timezone.utc))
    assert names[0] in backups(env)


def test_second_run_changes_nothing(env, capsys):
    conn, a, b = pair(env)
    conn.close()
    assert run(env, "merge-duplicates") == 0
    capsys.readouterr()
    files = backups(env)
    snapshot = sqlite3.connect(env.live).execute("SELECT id, dedup_key FROM jobs").fetchall()
    assert run(env, "merge-duplicates") == 0
    out = capsys.readouterr().out
    assert "0 group(s)" in out and backups(env) == files
    c = sqlite3.connect(env.live)
    assert c.execute("SELECT COUNT(*) FROM job_merges").fetchone()[0] == 1
    assert c.execute("SELECT id, dedup_key FROM jobs").fetchall() == snapshot


# --- archive ---------------------------------------------------------------------------------------------------------------

def test_archive_rows_are_re_keyed_and_a_new_sighting_of_an_archived_job_is_archived(env):
    conn = db.connect(env.live)
    old = add_legacy(conn, company="AXA Switzerland", ext="3", source="s5", title="Quant")
    aid = db.archive_job(conn, old, "user", "user_rejected")
    conn.close()
    assert run(env, "merge-duplicates") == 0
    conn = db.connect(env.live)
    row = conn.execute("SELECT dedup_key, record FROM job_archive WHERE id = ?", (aid,)).fetchone()
    assert row["dedup_key"] == "axa|quant|winterthur" and json.loads(row["record"])["job"]["dedup_key"] == "axa|quant|winterthur"
    assert ingest_job(conn, sighting("AXA", ext="42", source="s6", title="Quant")).outcome == "archived"
    conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) "
                 "VALUES ('axa|quant|winterthur','Quant','AXA','new','x','x')")
    conn.commit()
    with pytest.raises(ValueError):                                    # restore still refuses while the same key is open
        db.restore_job(conn, aid)


def test_open_job_equal_to_an_archive_entry_is_only_reported(env, capsys):
    conn = db.connect(env.live)
    old = add_legacy(conn, company="AXA Switzerland", ext="3", source="s5", title="Quant")
    db.archive_job(conn, old, "user", "user_rejected")
    conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) "
                 "VALUES ('axa schweiz|quant|winterthur','Quant','AXA Schweiz','new','x','x')")
    conn.commit()
    conn.close()
    assert run(env, "merge-duplicates") == 0
    assert len(ids(env)) == 1                                          # nothing archived or removed
    assert "REPORT open job" in capsys.readouterr().out


def test_different_companies_titles_or_places_are_not_grouped(env):
    conn = db.connect(env.live)
    ingest_job(conn, sighting("Bank Pekao", ext="1"))
    ingest_job(conn, sighting("Pekao Investment", ext="2"))
    ingest_job(conn, sighting("AXA", ext="3"))
    ingest_job(conn, sighting("AXA Switzerland", ext="4", title="Senior Analyst"))
    ingest_job(conn, sighting("AXA Switzerland", ext="5", places=("Zurich",)))
    conn.close()
    assert len(ids(env)) == 5
    assert run(env, "merge-duplicates") == 0
    assert len(ids(env)) == 5


# --- description copy and selection -----------------------------------------------------------------------------------

def test_description_is_copied_and_a_scored_survivor_is_marked_to_be_scored_again(env):
    text = "Responsibilities: " + "analyse insurance data. " * 30
    conn, a, b = pair(env, loser_status="new", survivor_status="scored")
    conn.execute("UPDATE jobs SET description = ?, description_missing = 0 WHERE id = ?", (text, b))
    conn.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", (OLD, a))
    conn.commit(); conn.close()
    assert run(env, "merge-duplicates") == 0
    conn = db.connect(env.live)
    row = conn.execute("SELECT id, description, needs_rescore FROM jobs").fetchone()
    assert row["id"] == a and "insurance" in row["description"] and row["needs_rescore"] == 1
    assert conn.execute("SELECT source FROM description_log WHERE job_id = ?", (a,)).fetchone()[0] == "merge"


def test_refresh_selection_re_ranks_a_batch_whose_top_held_a_loser(env):
    conn, a, b = pair(env, loser_status="scored", survivor_status="scored")
    other = ingest_job(conn, sighting("Zed", ext="8", source="s8", title="Other")).job_id
    conn.execute("UPDATE jobs SET status = 'not_selected' WHERE id = ?", (other,))
    add_score(conn, a, 50)
    add_score(conn, b, 90)
    add_score(conn, other, 60)
    conn.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES ('B1','x','f','h',3,3)")
    for j, sel, rank in ((b, 1, 1), (other, 0, None), (a, 0, None)):
        conn.execute("INSERT INTO batch_jobs (batch_id, job_id, fresh, selected, rank) VALUES ('B1', ?, 1, ?, ?)", (j, sel, rank))
    conn.execute("UPDATE jobs SET first_seen_at = ?, description = ?, description_missing = 0 WHERE id = ?", (OLD, "d" * 400, a))
    conn.execute("UPDATE jobs SET description = ?, description_missing = 0 WHERE id = ?", ("d" * 400, other))
    conn.commit(); conn.close()
    assert run(env, "merge-duplicates") == 0
    c = sqlite3.connect(env.live)
    ranks = dict(c.execute("SELECT job_id, rank FROM batch_jobs WHERE selected = 1").fetchall())
    c.close()
    assert ranks.get(other) is not None and b not in ranks


def test_a_merged_job_id_is_not_given_out_again(env):
    conn, a, b = pair(env)
    assert b > a
    conn.close()
    assert run(env, "merge-duplicates") == 0
    conn = db.connect(env.live)
    new = ingest_job(conn, sighting("Other Co", ext="5", source="s9", title="Quant"))
    assert new.outcome == "new" and new.job_id > b
    conn.close()


def test_an_interrupted_merge_exits_143_and_frees_the_lock(env, monkeypatch):
    conn, a, b = pair(env, loser_status="new", survivor_status="shortlisted")
    conn.close()

    def stopped(*a, **k):
        raise pipeline.Interrupted()
    monkeypatch.setattr(backup, "create_backup", stopped)
    assert run(env, "merge-duplicates") == 143
    assert not pipeline.lock_is_held(env.cfg)


# --- core city (batch-rest item 19) -------------------------------------------------------------------------------

def test_core_city_pair_is_merged_with_core_city_in_moved(env, capsys):
    conn = db.connect(env.live)
    a = ingest_job(conn, sighting("AXA", ext="1", source="s1", places=("Warszawa",))).job_id
    conn.execute("INSERT INTO jobs (dedup_key, title, company, location_text, status, first_seen_at, last_seen_at) "
                 "SELECT 'axa|analyst|warsaw masovian poland', title, company, 'Warsaw, Masovian, Poland', 'new', first_seen_at, last_seen_at "
                 "FROM jobs WHERE id = ?", (a,))
    b = conn.execute("SELECT MAX(id) FROM jobs").fetchone()[0]
    conn.execute("INSERT INTO job_sources (job_id, source, external_id, seen_at) VALUES (?, 's2', '2', ?)", (b, db.utcnow()))
    conn.execute("INSERT INTO job_locations (job_id, location_text) VALUES (?, 'Warsaw, Masovian, Poland')", (b,))
    conn.commit(); conn.close()
    assert ids(env) == [a, b]
    assert run(env, "merge-duplicates", "--dry-run") == 0
    assert "core city" in capsys.readouterr().out and ids(env) == [a, b]
    assert run(env, "merge-duplicates") == 0
    assert ids(env) == [a]
    conn = db.connect(env.live)
    row = conn.execute("SELECT merged_job_id, reason, moved FROM job_merges").fetchone()
    assert row["merged_job_id"] == b and row["reason"] == "company_key"
    assert json.loads(row["moved"])["match"] == "core_city" and json.loads(row["moved"])["sources"] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_sources WHERE job_id = ?", (a,)).fetchone()[0] == 2


def test_core_city_pass_leaves_same_source_and_exact_groups_alone(env, capsys):
    conn, a, b = pair(env)                                            # the exact company_key group
    c = ingest_job(conn, sighting("Other", ext="5", source="s1", places=("Warszawa",))).job_id
    d = ingest_job(conn, sighting("Other", ext="6", source="s1", places=("Warsaw, Poland",))).job_id   # same source: not merged
    conn.close()
    assert run(env, "merge-duplicates", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "core city" not in out
    assert run(env, "merge-duplicates") == 0
    assert ids(env) == [a, c, d]
    conn = db.connect(env.live)
    assert json.loads(conn.execute("SELECT moved FROM job_merges").fetchone()[0]).get("match") is None
