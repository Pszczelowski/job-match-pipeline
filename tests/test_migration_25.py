"""Migration 25 (v0.98.0): status 'accepted', tables `interviews` and `job_notes`. `jobs` and `status_history` are rebuilt for the new CHECK."""
import hashlib
import json
import re
import sqlite3

import pytest

from app import db

T = "2026-01-02T00:00:00Z"
OLD_STATUSES = ("new", "awaiting_llm", "scored", "not_selected", "shortlisted", "approved", "applied", "interview", "offer",
                "rejected", "withdrawn", "location_failed")
NEW_TABLES = ("interviews", "job_notes")


def v24(path, monkeypatch):
    full = db.MIGRATIONS
    monkeypatch.setattr(db, "MIGRATIONS", full[:24])
    c = db.connect(path)
    monkeypatch.setattr(db, "MIGRATIONS", full)
    assert db.schema_version(c) == 24
    return c


def tables(c):
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def counts(c, skip=()):
    return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables(c) if t not in skip}


def seq(c):
    row = c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'jobs'").fetchone()
    return row[0] if row else None


def fill(c):
    """One job per old status with children, plus an archived job and a merge row on ids above every live id."""
    c.execute("INSERT INTO runs (id, started_at, status) VALUES ('r1', ?, 'ok')", (T,))
    ids = {}
    for i, status in enumerate(OLD_STATUSES, start=1):
        ids[status] = c.execute(
            "INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at, run_id) VALUES (?, 'T', 'C', ?, ?, ?, 'r1')",
            (f"k{i}|x", status, T, T)).lastrowid
        j = ids[status]
        c.execute("INSERT INTO status_history (job_id, to_status, changed_at) VALUES (?, 'new', ?)", (j, T))
        c.execute("INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) VALUES (?, 'new', ?, 'web undo', ?)", (j, status, T))
        c.execute("INSERT INTO job_sources (job_id, source, external_id, seen_at) VALUES (?, 's', ?, ?)", (j, f"e{j}", T))
        loc = c.execute("INSERT INTO job_locations (job_id, location_text) VALUES (?, 'Zurich')", (j,)).lastrowid
        c.execute("INSERT INTO job_travel_checks (location_id, mode, origin, destination, provider, computed_at) VALUES (?, 'train', 'o', 'd', 'p', ?)", (loc, T))
        c.execute("INSERT INTO job_filter_results (job_id, filter, outcome, created_at) VALUES (?, 'f', 'pass', ?)", (j, T))
        c.execute("INSERT INTO scores (job_id, prompt_version, model, overall, subscores, created_at) VALUES (?, 'p', 'm', 70, '{}', ?)", (j, T))
        c.execute("INSERT INTO description_fetches (job_id, attempted_at, status) VALUES (?, ?, 'ok')", (j, T))
    j = ids["applied"]
    doc = c.execute("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at) VALUES (?, 'cv', '{}', 'draft', 'd', ?)", (j, T)).lastrowid
    c.execute("INSERT INTO finding_overrides (document_id, kind, finding_where, text, comment, created_at) VALUES (?, 'k', 'w', 't', 'c', ?)", (doc, T))
    c.execute("INSERT INTO revision_requests (document_id, kind, instruction, content_sha, created_at) VALUES (?, 'cv', 'i', 's', ?)", (doc, T))
    c.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES ('b1', ?, 'f', 's', 1, 1)", (T,))
    c.execute("INSERT INTO batch_jobs (batch_id, job_id, fresh) VALUES ('b1', ?, 1)", (j,))
    high = c.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at) VALUES ('high|x', 'T', 'C', ?, ?)", (T, T)).lastrowid
    c.execute("DELETE FROM jobs WHERE id = ?", (high,))                              # the counter is now above every live id
    c.execute("INSERT INTO job_archive (original_job_id, dedup_key, title, company, locations, record, rejection_stage, reason_code, rejected_at) "
              "VALUES (?, 'arch|x', 'T', 'C', '[]', '{}', 'user', 'user_rejected', ?)", (high, T))
    c.execute("INSERT INTO job_merges (survivor_job_id, merged_job_id, merged_key, survivor_old_key, new_key, reason, record, moved, merged_at) "
              "VALUES (?, ?, 'a', 'b', 'c', 'company_key', '{}', '{}', ?)", (j, high + 1, T))
    c.commit()
    assert seq(c) == high > max(ids.values())
    return ids


def shape(c):
    return sorted((r[0], r[1], r[2]) for r in c.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))


def test_up_keeps_every_row_and_id_and_the_counter(tmp_path, monkeypatch):
    c = v24(tmp_path / "t.db", monkeypatch)
    ids = fill(c)
    before = counts(c)
    old_seq = seq(c)
    rows_before = {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in ("jobs", "status_history")}
    assert db.migrate(c) == 25
    assert counts(c, skip=NEW_TABLES) == before
    assert {t: 0 for t in NEW_TABLES} == {t: counts(c)[t] for t in NEW_TABLES}
    for t, rows in rows_before.items():
        assert [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1")] == rows
    assert {r[0]: r[1] for r in c.execute("SELECT status, id FROM jobs")} == ids
    assert seq(c) == old_seq                                                         # DROP TABLE jobs removed the counter row; it is restored
    c.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at) VALUES ('n|x', 'T', 'C', ?, ?)", (T, T))
    assert c.execute("SELECT id FROM jobs WHERE dedup_key = 'n|x'").fetchone()[0] == old_seq + 1
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_jobs_status", "idx_jobs_canonical_url", "idx_status_history_job", "idx_interviews_job", "idx_job_notes_job"} <= names
    c.close()


def test_accepted_is_allowed_and_junk_is_refused(tmp_path, monkeypatch):
    c = v24(tmp_path / "t.db", monkeypatch)
    jid = fill(c)["offer"]
    with pytest.raises(sqlite3.IntegrityError):                                      # before: 'accepted' is not a status
        c.execute("UPDATE jobs SET status = 'accepted' WHERE id = ?", (jid,))
    db.migrate(c)
    c.execute("UPDATE jobs SET status = 'accepted' WHERE id = ?", (jid,))
    c.execute("INSERT INTO status_history (job_id, from_status, to_status, changed_at) VALUES (?, 'offer', 'accepted', ?)", (jid, T))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE jobs SET status = 'junk' WHERE id = ?", (jid,))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO status_history (job_id, to_status, changed_at) VALUES (?, 'junk', ?)", (jid, T))
    c.close()


def test_the_migrated_schema_equals_a_fresh_one(tmp_path, monkeypatch):
    c = v24(tmp_path / "old.db", monkeypatch)
    db.migrate(c)
    fresh = db.connect(tmp_path / "fresh.db")
    assert shape(c) == shape(fresh)
    c.close()
    fresh.close()


def test_the_statuses_are_the_ones_of_both_live_checks(tmp_path):
    c = db.connect(tmp_path / "t.db")
    for table in ("jobs", "status_history"):
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        listed = re.search(r"(?:to_)?status\s+TEXT NOT NULL(?: DEFAULT '\w+')? CHECK \((?:to_)?status IN \(([^)]*)\)\)", sql).group(1)
        assert set(re.findall(r"'(\w+)'", listed)) == set(db.STATUSES), table
    assert "accepted" in db.STATUSES
    c.close()


def test_a_failure_rolls_everything_back(tmp_path, monkeypatch):
    c = v24(tmp_path / "t.db", monkeypatch)
    fill(c)
    before = counts(c)
    old_seq = seq(c)
    broken = list(db.MIGRATIONS)
    broken[24] = db.MIGRATIONS[24].replace("DROP TABLE jobs;", "DROP TABLE jobs; SELECT * FROM no_such_table;")
    monkeypatch.setattr(db, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(c)
    assert db.schema_version(c) == 24
    assert counts(c) == before
    assert seq(c) == old_seq
    for name in ("jobs_new", "status_history_new", *NEW_TABLES):
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.close()


def test_a_lost_child_row_rolls_the_migration_back(tmp_path, monkeypatch):
    c = v24(tmp_path / "t.db", monkeypatch)
    fill(c)
    broken = list(db.MIGRATIONS)
    broken[24] = db.MIGRATIONS[24] + "DELETE FROM job_sources;"
    monkeypatch.setattr(db, "MIGRATIONS", broken)
    with pytest.raises(RuntimeError, match="job_sources"):
        db.migrate(c)
    assert db.schema_version(c) == 24
    assert c.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == len(OLD_STATUSES)
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.close()


def test_a_second_run_keeps_the_new_rows(tmp_path, monkeypatch):
    c = v24(tmp_path / "t.db", monkeypatch)
    jid = fill(c)["interview"]
    db.migrate(c)
    c.execute("INSERT INTO interviews (job_id, starts_at, kind, created_at) VALUES (?, '2026-12-01T09:00:00Z', 'video', ?)", (jid, T))
    c.execute("INSERT INTO job_notes (job_id, status_at, text, created_at) VALUES (?, 'interview', 'a note', ?)", (jid, T))
    c.commit()
    before, old_seq = counts(c), seq(c)
    c.execute("PRAGMA user_version = 24")
    assert db.migrate(c) == 25
    assert counts(c) == before
    assert seq(c) == old_seq
    c.close()


def test_deleting_a_job_deletes_its_rounds_and_notes(tmp_path):
    c = db.connect(tmp_path / "t.db")
    jid = c.execute("INSERT INTO jobs (dedup_key, title, company, first_seen_at, last_seen_at) VALUES ('a|x', 'T', 'C', ?, ?)", (T, T)).lastrowid
    c.execute("INSERT INTO interviews (job_id, starts_at, kind, created_at) VALUES (?, ?, 'phone', ?)", (jid, T, T))
    c.execute("INSERT INTO job_notes (job_id, status_at, text, created_at) VALUES (?, 'new', 'x', ?)", (jid, T))
    for sql in ("INSERT INTO interviews (job_id, starts_at, kind, created_at) VALUES (%d, '%s', 'fax', '%s')" % (jid, T, T),
                "INSERT INTO job_notes (job_id, status_at, text, created_at) VALUES (%d, 'new', '', '%s')" % (jid, T),
                "INSERT INTO job_notes (job_id, status_at, text, created_at) VALUES (%d, 'new', '%s', '%s')" % (jid, "x" * 2001, T)):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql)
    c.execute("DELETE FROM jobs WHERE id = ?", (jid,))
    assert c.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM job_notes").fetchone()[0] == 0
    c.close()


def test_the_released_scripts_are_frozen():
    sha = lambda i: hashlib.sha256(db.MIGRATIONS[i].encode("utf-8")).hexdigest()
    assert sha(0) == "63a6d32bbbc7511538da1de1b3cf5225b23736b9b7cab77c4ce2085d2adc60b8"
    assert sha(20) == "4fc923b2f34ac63ec19e3858b9b1837100f997a0bc8419ede5c2ace253925ca2"
    assert sha(24) == "fb42855f255b6b362664f30872dd1d64645351cc922d500515c953a56e347c60"
    assert len(db.MIGRATIONS) == 25 and 25 in db.REBUILDS and 21 in db.REBUILDS
