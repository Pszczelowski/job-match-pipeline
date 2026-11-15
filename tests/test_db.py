import json
import sqlite3

import pytest

from app import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def add_job(conn, key="acme|analyst|zurich", title="Analyst", company="Acme", status="new"):
    cur = conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)",
        (key, title, company, status, db.utcnow(), db.utcnow()),
    )
    conn.commit()
    return cur.lastrowid


def test_schema_created_and_versioned(conn):
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "jobs", "job_sources", "job_locations", "job_travel_checks", "travel_cache",
            "job_filter_results", "scores", "status_history", "job_archive"} <= tables


def test_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path):
    c = db.connect(tmp_path / "t.db")
    add_job(c)
    c.close()
    c = db.connect(tmp_path / "t.db")  # reopening must not recreate or drop anything
    assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    c.close()


def test_newer_schema_is_refused(tmp_path):
    p = tmp_path / "t.db"
    raw = sqlite3.connect(p)
    raw.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    raw.close()
    with pytest.raises(RuntimeError, match="newer"):
        db.connect(p)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", ["CREATE TABLE a (x); CREATE TABLE a (x);"])
    p = tmp_path / "t.db"
    with pytest.raises(sqlite3.OperationalError):
        db.connect(p)
    raw = sqlite3.connect(p)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    assert raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='a'").fetchone()[0] == 0
    raw.close()


def test_dedup_key_is_unique(conn):
    add_job(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_job(conn)


def test_invalid_status_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_job(conn, status="bogus")


def test_every_documented_status_is_accepted(conn):
    for i, s in enumerate(db.STATUSES):
        add_job(conn, key=f"k{i}", status=s)
    stored = [r[0] for r in conn.execute("SELECT status FROM jobs ORDER BY id")]
    assert len(db.STATUSES) > 1 and stored == list(db.STATUSES)


def test_source_sighting_is_idempotent_but_newer_date_is_new_row(conn):
    jid = add_job(conn)
    ins = "INSERT INTO job_sources (job_id, source, external_id, source_date, seen_at) VALUES (?,?,?,?,?)"
    conn.execute(ins, (jid, "ubs_alert", "351249", "2026-10-28", db.utcnow()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, (jid, "ubs_alert", "351249", "2026-10-28", db.utcnow()))
    conn.execute(ins, (jid, "ubs_alert", "351249", "2026-10-29", db.utcnow()))
    assert conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 2


def test_score_range_enforced(conn):
    jid = add_job(conn)
    ins = "INSERT INTO scores (job_id, prompt_version, model, overall, subscores, created_at) VALUES (?,?,?,?,?,?)"
    conn.execute(ins, (jid, "v1", "m", 100, "{}", db.utcnow()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(ins, (jid, "v1", "m", 101, "{}", db.utcnow()))


def test_set_status_records_history(conn):
    jid = add_job(conn)
    db.set_status(conn, jid, "awaiting_llm", "batch built")
    db.set_status(conn, jid, "scored")
    rows = conn.execute("SELECT from_status, to_status, note FROM status_history ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("new", "awaiting_llm", "batch built"), ("awaiting_llm", "scored", None)]
    with pytest.raises(sqlite3.IntegrityError):
        db.set_status(conn, jid, "bogus")
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0] == "scored"
    with pytest.raises(KeyError):
        db.set_status(conn, 999, "scored")


def _rich_job(conn):
    jid = add_job(conn, key="ubs|analyst|zurich", title="Analyst", company="UBS")
    conn.execute("UPDATE jobs SET description=?, canonical_url=? WHERE id=?", ("desc text", "https://x/1", jid))
    conn.execute("INSERT INTO job_sources (job_id, source, external_id, seen_at) VALUES (?,?,?,?)",
                 (jid, "ubs_alert", "351249", db.utcnow()))
    conn.execute("INSERT INTO job_locations (job_id, location_text) VALUES (?,?)", (jid, "Zürich"))
    conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, reason_code, created_at) VALUES (?,?,?,?,?)",
                 (jid, "language", "fail", "language_c1_required", db.utcnow()))
    conn.execute("INSERT INTO scores (job_id, prompt_version, model, overall, subscores, created_at) VALUES (?,?,?,?,?,?)",
                 (jid, "v1", "m", 42, '{"skills": 40}', db.utcnow()))
    conn.commit()
    return jid


def test_archive_keeps_full_record_and_removes_working_row(conn):
    jid = _rich_job(conn)
    aid = db.archive_job(conn, jid, "filter", "language_c1_required", "German C1 required", run_id=None)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 0  # cascade
    row = conn.execute("SELECT * FROM job_archive WHERE id=?", (aid,)).fetchone()
    assert (row["dedup_key"], row["title"], row["company"]) == ("ubs|analyst|zurich", "Analyst", "UBS")
    assert (row["source"], row["external_id"]) == ("ubs_alert", "351249")
    assert (row["rejection_stage"], row["reason_code"], row["comment"]) == (
        "filter", "language_c1_required", "German C1 required")
    assert row["score"] == 42 and row["description"] == "desc text"
    assert json.loads(row["locations"]) == ["Zürich"]
    record = json.loads(row["record"])
    assert record["job"]["title"] == "Analyst"
    assert record["filter_results"][0]["reason_code"] == "language_c1_required"
    assert record["sources"][0]["external_id"] == "351249"


def test_archive_user_rejection_stage(conn):
    jid = add_job(conn)
    db.archive_job(conn, jid, "user", "user_rejected")
    row = conn.execute("SELECT rejection_stage, reason_code FROM job_archive").fetchone()
    assert tuple(row) == ("user", "user_rejected")


def test_archive_rejects_bad_input(conn):
    jid = add_job(conn)
    with pytest.raises(ValueError):
        db.archive_job(conn, jid, "nonsense", "x")
    with pytest.raises(KeyError):
        db.archive_job(conn, 999, "filter", "x")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1  # nothing lost on failure


def test_archived_job_is_found_by_dedup_key_and_url(conn):
    jid = _rich_job(conn)
    db.archive_job(conn, jid, "filter", "seniority")
    assert conn.execute("SELECT COUNT(*) FROM job_archive WHERE dedup_key=?", ("ubs|analyst|zurich",)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_archive WHERE canonical_url=?", ("https://x/1",)).fetchone()[0] == 1


def test_archive_survives_backup_and_restore(conn, tmp_path):
    jid = _rich_job(conn)
    db.archive_job(conn, jid, "filter", "language_c1_required", "c1")
    dest = tmp_path / "backups" / "copy.db"
    db.backup(conn, dest)
    restored = db.connect(dest)
    row = restored.execute("SELECT reason_code, comment, title FROM job_archive").fetchone()
    assert tuple(row) == ("language_c1_required", "c1", "Analyst")
    assert db.schema_version(restored) == len(db.MIGRATIONS)
    restored.close()


def test_connect_creates_missing_folder(tmp_path):
    c = db.connect(tmp_path / "a" / "b" / "jobs.db")
    c.close()
    assert (tmp_path / "a" / "b" / "jobs.db").exists()


# --- taking a job out of the archive again --------------------------------------------------------------------

def _archived_job(conn):
    jid = _rich_job(conn)
    return db.archive_job(conn, jid, "filter", "language_c1_required", "wrong rejection", run_id=None)


def test_restore_job_brings_back_the_job_with_sources_and_locations(conn):
    aid = _archived_job(conn)
    new_id = db.restore_job(conn, aid)
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (new_id,)).fetchone()
    assert (job["title"], job["company"], job["status"], job["description"], job["dedup_key"]) == (
        "Analyst", "UBS", "new", "desc text", "ubs|analyst|zurich")
    src = conn.execute("SELECT source, external_id FROM job_sources WHERE job_id = ?", (new_id,)).fetchone()
    assert tuple(src) == ("ubs_alert", "351249")
    assert [r[0] for r in conn.execute("SELECT location_text FROM job_locations WHERE job_id = ?", (new_id,))] == ["Zürich"]
    assert conn.execute("SELECT COUNT(*) FROM job_filter_results WHERE job_id = ?", (new_id,)).fetchone()[0] == 0     # filters run again
    note = conn.execute("SELECT note FROM status_history WHERE job_id = ?", (new_id,)).fetchone()[0]
    assert "restored from archive entry" in note and "language_c1_required" in note


def test_restore_returns_a_job_to_its_status_before_the_rejection(conn):
    jid = add_job(conn, key="app|key", status="applied")
    aid = db.archive_job(conn, jid, "user", "user_rejected")
    new_id = db.restore_job(conn, aid)
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (new_id,)).fetchone()[0] == "applied"
    h = conn.execute("SELECT from_status, to_status, note FROM status_history WHERE job_id = ?", (new_id,)).fetchone()
    assert (h["from_status"], h["to_status"]) == (None, "applied") and "restored from archive entry" in h["note"]


def test_restore_falls_back_to_new_when_the_old_status_cannot_stand_alone_or_is_unknown(conn):
    jid = add_job(conn, key="sc|key", status="scored")                   # scores and documents are not restored
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (db.restore_job(conn, db.archive_job(conn, jid, "user", "user_rejected")),)).fetchone()[0] == "new"
    jid = add_job(conn, key="old|key", status="applied")
    aid = db.archive_job(conn, jid, "user", "user_rejected")
    rec = json.loads(conn.execute("SELECT record FROM job_archive WHERE id = ?", (aid,)).fetchone()[0])
    del rec["job"]["status"]                                              # an archive row without the field
    conn.execute("UPDATE job_archive SET record = ? WHERE id = ?", (json.dumps(rec), aid)); conn.commit()
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (db.restore_job(conn, aid),)).fetchone()[0] == "new"


def test_the_archive_row_stays_as_history_and_is_marked(conn):
    aid = _archived_job(conn)
    new_id = db.restore_job(conn, aid)
    row = conn.execute("SELECT restored_at, restored_job_id, reason_code FROM job_archive WHERE id = ?", (aid,)).fetchone()
    assert row["restored_at"] and row["restored_job_id"] == new_id and row["reason_code"] == "language_c1_required"


def test_restore_refuses_twice_unknown_and_duplicates(conn):
    aid = _archived_job(conn)
    db.restore_job(conn, aid)
    with pytest.raises(ValueError, match="already restored"):
        db.restore_job(conn, aid)
    with pytest.raises(KeyError):
        db.restore_job(conn, 999)
    other = _rich_job_again(conn)
    with pytest.raises(ValueError, match="already open"):
        db.restore_job(conn, other)


def _rich_job_again(conn):
    """Archive a second job with the same key as one that is open again."""
    jid = add_job(conn, key="dup|key", title="Dup", company="X")
    aid = db.archive_job(conn, jid, "user", "user_rejected")
    add_job(conn, key="dup|key", title="Dup", company="X")
    return aid


def test_a_restored_job_is_filtered_again(conn):
    from app.config import load_config
    from app.filters.runner import filter_new_jobs
    aid = _archived_job(conn)
    db.restore_job(conn, aid)
    counts = filter_new_jobs(conn, load_config())
    assert counts["pass"] + counts["conditional"] + counts["fail"] == 1


def test_an_archived_job_id_is_not_given_out_again(conn):
    old = add_job(conn)
    conn.execute("INSERT INTO description_fetches (job_id, attempted_at, status) VALUES (?, ?, 'ok')", (old, db.utcnow()))
    conn.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES ('b1', ?, 'f', 's', 1, 1)", (db.utcnow(),))
    conn.execute("INSERT INTO batch_jobs (batch_id, job_id, fresh) VALUES ('b1', ?, 1)", (old,))
    conn.commit()
    db.archive_job(conn, old, "user", "user_rejected")
    new = add_job(conn, key="beta|analyst|bern", company="Beta")
    assert new > old
    assert conn.execute("SELECT COUNT(*) FROM description_fetches WHERE job_id = ?", (new,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM batch_jobs WHERE job_id = ?", (new,)).fetchone()[0] == 0


# --- migration 21: jobs.id AUTOINCREMENT (v0.80.7) ------------------------------------------------------------------

KEPT = ("jobs", "job_sources", "job_locations", "job_travel_checks", "job_filter_results", "scores", "status_history", "documents")
ALSO = KEPT + ("batch_jobs", "description_fetches", "description_log")
T0 = "2026-01-02T00:00:00Z"
T_ARCHIVED = "2026-01-10T00:00:00Z"
T_NEW = "2026-02-01T00:00:00Z"


def v20(path, monkeypatch):
    """A database at schema 20 (before the rebuild); db.migrate(conn) then applies migration 21."""
    full = db.MIGRATIONS
    monkeypatch.setattr(db, "MIGRATIONS", full[:20])
    c = db.connect(path)
    monkeypatch.setattr(db, "MIGRATIONS", full)
    assert db.schema_version(c) == 20
    return c


def put_job(c, key, first_seen=T0, run_id=None):
    cur = c.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at, run_id) VALUES (?, 'T', 'C', ?, ?, ?)",
                    (key, first_seen, first_seen, run_id))
    return cur.lastrowid


def put_batch(c, batch_id, created_at):
    c.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES (?, ?, 'f', 's', 1, 1)",
              (batch_id, created_at))


def counts(c, tables=KEPT):
    return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def fill_children(c, job_id):
    c.execute("INSERT INTO job_sources (job_id, source, external_id, seen_at) VALUES (?, 's', ?, ?)", (job_id, f"e{job_id}", T0))
    loc = c.execute("INSERT INTO job_locations (job_id, location_text) VALUES (?, 'Zurich')", (job_id,)).lastrowid
    c.execute("INSERT INTO job_travel_checks (location_id, mode, origin, destination, provider, computed_at) VALUES (?, 'train', 'o', 'd', 'p', ?)",
              (loc, T0))
    c.execute("INSERT INTO job_filter_results (job_id, filter, outcome, created_at) VALUES (?, 'f', 'pass', ?)", (job_id, T0))
    c.execute("INSERT INTO scores (job_id, prompt_version, model, overall, subscores, created_at) VALUES (?, 'p', 'm', 70, '{}', ?)", (job_id, T0))
    c.execute("INSERT INTO status_history (job_id, to_status, changed_at) VALUES (?, 'new', ?)", (job_id, T0))
    c.execute("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at) VALUES (?, 'cv', '{}', 'draft', 'd', ?)",
              (job_id, T0))


def test_migration_21_keeps_every_child_row_and_turns_foreign_keys_back_on(tmp_path, monkeypatch):
    c = v20(tmp_path / "t.db", monkeypatch)
    for key in ("a|x", "b|x"):
        fill_children(c, put_job(c, key))
    c.commit()
    before = counts(c)
    cols = [tuple(r) for r in c.execute("PRAGMA table_xinfo(jobs)")]
    assert all(v >= 2 for v in before.values())
    assert db.migrate(c) == len(db.MIGRATIONS)
    assert counts(c) == before
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    for t in ("job_sources", "job_locations", "job_filter_results", "scores", "status_history", "documents"):
        assert {r[2] for r in c.execute(f"PRAGMA foreign_key_list({t})")} >= {"jobs"}
    assert "AUTOINCREMENT" in c.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
    assert [tuple(r) for r in c.execute("PRAGMA table_xinfo(jobs)")] == cols
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'jobs'")}
    assert {"idx_jobs_status", "idx_jobs_canonical_url"} <= names
    c.execute("DELETE FROM jobs WHERE id = 1")                         # the cascade still works
    assert c.execute("SELECT COUNT(*) FROM job_sources WHERE job_id = 1").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 1
    c.close()


def test_migration_21_starts_ids_above_every_id_still_held(tmp_path, monkeypatch):
    c = v20(tmp_path / "t.db", monkeypatch)
    put_job(c, "a|x")
    put_job(c, "b|x")
    c.execute("INSERT INTO job_archive (original_job_id, dedup_key, title, company, record, rejection_stage, reason_code, rejected_at) "
              "VALUES (5, 'z|x', 'T', 'C', '{}', 'user', 'user_rejected', ?)", (T0,))
    put_batch(c, "b1", T0)
    c.execute("INSERT INTO batch_jobs (batch_id, job_id, fresh) VALUES ('b1', 7, 1)")
    c.commit()
    db.migrate(c)
    assert c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'jobs'").fetchone()[0] == 7
    assert put_job(c, "c|x") == 8
    c.close()


def test_migration_21_removes_only_rows_an_earlier_job_left_on_the_same_id(tmp_path, monkeypatch):
    c = v20(tmp_path / "t.db", monkeypatch)
    assert put_job(c, "a|x") == 1
    put_batch(c, "b-old", T0)
    c.execute("INSERT INTO batch_jobs (batch_id, job_id, fresh) VALUES ('b-old', 1, 1)")
    c.execute("INSERT INTO description_fetches (job_id, attempted_at, status) VALUES (1, ?, 'ok')", (T0,))
    c.execute("INSERT INTO description_log (job_id, source, chars, rescore, set_at) VALUES (1, 's', 1, 0, ?)", (T0,))
    c.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) "
              "VALUES (1, ?, '2026-01-02', 'T1', 'k', 'p', 'success')", (T0,))
    c.execute("INSERT INTO postponements (job_id, batch_id, fresh, reason, noted_at) VALUES (1, 'b-old', 1, 'no_result', ?)", (T0,))
    c.commit()
    c.executescript("CREATE TABLE interviews (id INTEGER, job_id INTEGER); CREATE TABLE job_notes (id INTEGER, job_id INTEGER);")      # archive_job reads the tables of schema 25 (v0.98.0)
    aid = db.archive_job(c, 1, "user", "user_rejected")
    c.execute("UPDATE job_archive SET rejected_at = ? WHERE id = ?", (T_ARCHIVED, aid))
    db.start_run(c, "b-new")
    assert put_job(c, "b|x", first_seen=T_NEW, run_id="b-new") == 1    # reuse at schema 20: B takes id 1
    put_batch(c, "b-new", "2026-01-31T00:00:00Z")                      # created at the run start, before B was first seen
    c.execute("INSERT INTO batch_jobs (batch_id, job_id, fresh) VALUES ('b-new', 1, 1)")
    c.execute("INSERT INTO description_log (job_id, source, chars, rescore, set_at) VALUES (1, 's', 1, 0, '2026-02-02T00:00:00Z')")
    assert put_job(c, "c|x") == 2
    c.execute("INSERT INTO description_fetches (job_id, attempted_at, status) VALUES (2, ?, 'ok')", (T0,))
    c.commit()
    c_aid = db.archive_job(c, 2, "user", "user_rejected")
    c.executescript("DROP TABLE interviews; DROP TABLE job_notes;")                                               # migration 25 creates the real ones
    c.execute("UPDATE job_archive SET rejected_at = ? WHERE id = ?", (T_ARCHIVED, c_aid))
    c.commit()
    db.migrate(c)
    rows = lambda sql: c.execute(sql).fetchall()
    assert rows("SELECT 1 FROM description_fetches WHERE job_id = 1") == []
    assert rows("SELECT 1 FROM description_resolutions WHERE job_id = 1") == []
    assert rows("SELECT 1 FROM postponements WHERE job_id = 1") == []
    assert rows("SELECT 1 FROM batch_jobs WHERE batch_id = 'b-old'") == []
    assert [r[0] for r in rows("SELECT batch_id FROM batch_jobs WHERE job_id = 1")] == ["b-new"]
    assert [r[0] for r in rows("SELECT set_at FROM description_log WHERE job_id = 1")] == ["2026-02-02T00:00:00Z"]
    assert len(rows("SELECT 1 FROM description_fetches WHERE job_id = 2")) == 1       # the orphan of an archived job stays
    assert put_job(c, "d|x") == 3
    c.close()


def test_migration_21_failure_rolls_back_everything(tmp_path, monkeypatch):
    c = v20(tmp_path / "t.db", monkeypatch)
    fill_children(c, put_job(c, "a|x"))
    c.commit()
    before = counts(c)
    broken = list(db.MIGRATIONS)
    broken[20] = db.MIGRATIONS[20].replace("DROP TABLE jobs;", "DROP TABLE jobs; SELECT * FROM no_such_table;")
    monkeypatch.setattr(db, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(c)
    assert db.schema_version(c) == 20
    assert "AUTOINCREMENT" not in c.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'jobs_new'").fetchone()
    assert counts(c) == before
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.close()


def test_migration_21_is_rolled_back_when_a_child_row_goes_missing(tmp_path, monkeypatch):
    c = v20(tmp_path / "t.db", monkeypatch)
    fill_children(c, put_job(c, "a|x"))
    c.commit()
    broken = list(db.MIGRATIONS)
    broken[20] = db.MIGRATIONS[20] + "DELETE FROM job_sources;"
    monkeypatch.setattr(db, "MIGRATIONS", broken)
    with pytest.raises(RuntimeError, match="job_sources"):
        db.migrate(c)
    assert db.schema_version(c) == 20
    assert c.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 1
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.close()


def test_migration_21_run_twice_changes_nothing(tmp_path, monkeypatch):
    c = v20(tmp_path / "t.db", monkeypatch)
    for key in ("a|x", "b|x"):
        fill_children(c, put_job(c, key))
    c.commit()
    db.migrate(c)
    before = counts(c, ALSO)
    seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'jobs'").fetchone()[0]
    c.execute("DROP TABLE revision_requests")                # migrations 22 to 24 ran as well; take 22 and 23 back so that 21 to 24 run again
    c.execute("DROP TABLE finding_overrides")
    c.execute("PRAGMA user_version = 20")
    assert db.migrate(c) == len(db.MIGRATIONS)
    assert counts(c, ALSO) == before
    assert c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'jobs'").fetchone()[0] == seq
    c.close()


# --- migration 24: status 'expired' and the partial text (v0.87.1) ---------------------------------------------------

def v23(path, monkeypatch):
    full = db.MIGRATIONS
    monkeypatch.setattr(db, "MIGRATIONS", full[:23])
    c = db.connect(path)
    monkeypatch.setattr(db, "MIGRATIONS", full)
    assert db.schema_version(c) == 23
    return c


def test_migration_24_converts_expired_rows_keeps_the_rest_and_adds_text(tmp_path, monkeypatch):
    c = v23(tmp_path / "t.db", monkeypatch)
    for i, (status, error) in enumerate([("no_match", "expired: posting x shows the expired notice"), ("no_match", "no match: none"),
                                         ("partial", "partial: posting y has 10 characters"), ("no_match", None), ("success", None)], 1):
        c.execute("INSERT INTO description_resolutions (id, job_id, attempted_at, day, tier, source_key, platform, status, error) "
                  "VALUES (?, ?, 'x', '2026-11-01', 'T1', 's', 'jibe', ?, ?)", (i * 10, i, status, error))
    c.commit()
    assert db.migrate(c) == len(db.MIGRATIONS)
    got = [(r["id"], r["job_id"], r["status"], r["error"], r["text"]) for r in c.execute("SELECT * FROM description_resolutions ORDER BY id")]
    assert got == [(10, 1, "expired", "expired: posting x shows the expired notice", None), (20, 2, "no_match", "no match: none", None),
                   (30, 3, "partial", "partial: posting y has 10 characters", None), (40, 4, "no_match", None, None), (50, 5, "success", None, None)]
    assert db.schema_version(c) == len(db.MIGRATIONS)
    idx = {r["name"]: r["sql"] for r in c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'description_resolutions'")}
    assert "idx_description_resolutions_job" in idx and "(job_id, day)" in idx["idx_description_resolutions_job"]
    assert "idx_description_resolutions_posting" in idx and "(source_key, posting_id)" in idx["idx_description_resolutions_posting"]
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'description_resolutions_new'").fetchone()
    c.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status, text) "
              "VALUES (9, 'x', 'd', 'T2', 's', 'p', 'expired', 'abc')")
    with pytest.raises(db.sqlite3.IntegrityError):
        c.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) "
                  "VALUES (9, 'x', 'd', 'T2', 's', 'p', 'bogus')")
    c.close()
