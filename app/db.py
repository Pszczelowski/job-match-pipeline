"""SQLite schema, migrations and archive helpers (goal.md 4, 5.3.3, 5.7, 5.9).

One file, WAL mode. Schema changes are numbered scripts in MIGRATIONS; the applied count is kept in
`PRAGMA user_version`. Never edit a released script; add a new one.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Status flow from goal.md 5.7, plus the outcomes of 5.3.1 / 5.5.
STATUSES = (
    "new", "awaiting_llm", "scored", "not_selected", "shortlisted", "approved",
    "applied", "interview", "offer", "rejected", "withdrawn", "location_failed",
)
RUN_STATUSES = ("running", "ok", "failed")
FILTER_OUTCOMES = ("pass", "fail", "conditional")
ARCHIVE_STAGES = ("filter", "user", "not_selected")
TRAVEL_MODES = ("train", "car")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


_M21_COLS = ("id, dedup_key, canonical_url, title, company, location_text, description, description_missing, status, repost, "
             "first_seen_at, last_seen_at, run_id, needs_rescore, closed_at, posted_on")
_M21_HIGH = """COALESCE((SELECT MAX(v) FROM (
        SELECT MAX(id) AS v FROM jobs UNION ALL SELECT MAX(original_job_id) FROM job_archive UNION ALL SELECT MAX(restored_job_id) FROM job_archive
        UNION ALL SELECT MAX(merged_job_id) FROM job_merges UNION ALL SELECT MAX(survivor_job_id) FROM job_merges
        UNION ALL SELECT MAX(job_id) FROM batch_jobs UNION ALL SELECT MAX(job_id) FROM description_fetches
        UNION ALL SELECT MAX(job_id) FROM description_log UNION ALL SELECT MAX(job_id) FROM description_resolutions
        UNION ALL SELECT MAX(duplicate_of_job_id) FROM description_resolutions UNION ALL SELECT MAX(job_id) FROM postponements)), 0)"""
_M21_GONE = """gone AS (SELECT jid, MAX(t) AS t FROM (SELECT original_job_id AS jid, rejected_at AS t FROM job_archive WHERE original_job_id IS NOT NULL
        UNION ALL SELECT merged_job_id, merged_at FROM job_merges) GROUP BY jid)"""

MIGRATIONS: list[str] = [
    # 1: initial schema
    f"""
    CREATE TABLE runs (
        id          TEXT PRIMARY KEY,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ({_in(RUN_STATUSES)})),
        summary     TEXT
    );

    CREATE TABLE jobs (
        id                  INTEGER PRIMARY KEY,
        dedup_key           TEXT NOT NULL UNIQUE,
        canonical_url       TEXT,
        title               TEXT NOT NULL,
        company             TEXT NOT NULL,
        location_text       TEXT,
        description         TEXT,
        description_missing INTEGER NOT NULL DEFAULT 1 CHECK (description_missing IN (0, 1)),
        status              TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({_in(STATUSES)})),
        repost              INTEGER NOT NULL DEFAULT 0 CHECK (repost IN (0, 1)),
        first_seen_at       TEXT NOT NULL,
        last_seen_at        TEXT NOT NULL,
        run_id              TEXT REFERENCES runs(id)
    );
    CREATE INDEX idx_jobs_status ON jobs(status);
    CREATE INDEX idx_jobs_canonical_url ON jobs(canonical_url);

    -- One row per sighting of a job in a source (alert mail or career site). Same job from several
    -- sources, or re-listed with a newer date, adds rows here, not jobs (goal.md 5.2).
    CREATE TABLE job_sources (
        id          INTEGER PRIMARY KEY,
        job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        source      TEXT NOT NULL,
        external_id TEXT NOT NULL,
        reference   TEXT,
        url         TEXT,
        source_date TEXT NOT NULL DEFAULT '',
        message_id  TEXT,
        seen_at     TEXT NOT NULL,
        raw         TEXT,
        UNIQUE (source, external_id, source_date)
    );
    CREATE INDEX idx_job_sources_job ON job_sources(job_id);
    CREATE INDEX idx_job_sources_ext ON job_sources(source, external_id);

    CREATE TABLE job_locations (
        id            INTEGER PRIMARY KEY,
        job_id        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        location_text TEXT NOT NULL,
        city          TEXT,
        country       TEXT,
        resolved      INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
        latitude      REAL,
        longitude     REAL,
        UNIQUE (job_id, location_text)
    );

    -- Calculation record per job location and mode (goal.md 5.3.1).
    CREATE TABLE job_travel_checks (
        id          INTEGER PRIMARY KEY,
        location_id INTEGER NOT NULL REFERENCES job_locations(id) ON DELETE CASCADE,
        mode        TEXT NOT NULL CHECK (mode IN ({_in(TRAVEL_MODES)})),
        origin      TEXT NOT NULL,
        destination TEXT NOT NULL,
        minutes     REAL,
        provider    TEXT NOT NULL,
        approximate INTEGER NOT NULL DEFAULT 0 CHECK (approximate IN (0, 1)),
        assumptions TEXT,
        computed_at TEXT NOT NULL
    );
    CREATE INDEX idx_job_travel_location ON job_travel_checks(location_id);

    -- Cache per origin, destination and mode; a city is computed once (goal.md 5.3.1).
    CREATE TABLE travel_cache (
        origin      TEXT NOT NULL,
        destination TEXT NOT NULL,
        mode        TEXT NOT NULL CHECK (mode IN ({_in(TRAVEL_MODES)})),
        minutes     REAL,
        provider    TEXT NOT NULL,
        approximate INTEGER NOT NULL DEFAULT 0 CHECK (approximate IN (0, 1)),
        assumptions TEXT,
        computed_at TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        PRIMARY KEY (origin, destination, mode)
    );

    -- Audit trail of every filter decision; history is kept when a conditional job is re-checked.
    CREATE TABLE job_filter_results (
        id          INTEGER PRIMARY KEY,
        job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        filter      TEXT NOT NULL,
        outcome     TEXT NOT NULL CHECK (outcome IN ({_in(FILTER_OUTCOMES)})),
        reason_code TEXT,
        comment     TEXT,
        run_id      TEXT REFERENCES runs(id),
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_filter_results_job ON job_filter_results(job_id);

    CREATE TABLE scores (
        id             INTEGER PRIMARY KEY,
        job_id         INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        run_id         TEXT REFERENCES runs(id),
        prompt_version TEXT NOT NULL,
        model          TEXT NOT NULL,
        overall        INTEGER NOT NULL CHECK (overall BETWEEN 0 AND 100),
        subscores      TEXT NOT NULL,
        details        TEXT,
        cv_variant     TEXT,
        confidence     TEXT,
        created_at     TEXT NOT NULL
    );
    CREATE INDEX idx_scores_job ON scores(job_id);

    CREATE TABLE status_history (
        id          INTEGER PRIMARY KEY,
        job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        from_status TEXT,
        to_status   TEXT NOT NULL CHECK (to_status IN ({_in(STATUSES)})),
        note        TEXT,
        changed_at  TEXT NOT NULL
    );
    CREATE INDEX idx_status_history_job ON status_history(job_id);

    -- Rejected jobs kept for statistics and dedup (goal.md 5.3.3). No foreign key to jobs: the
    -- working row is removed when a job is archived.
    CREATE TABLE job_archive (
        id                INTEGER PRIMARY KEY,
        original_job_id   INTEGER,
        dedup_key         TEXT NOT NULL,
        canonical_url     TEXT,
        source            TEXT,
        external_id       TEXT,
        title             TEXT NOT NULL,
        company           TEXT NOT NULL,
        locations         TEXT,
        description       TEXT,
        record            TEXT NOT NULL,
        rejection_stage   TEXT NOT NULL CHECK (rejection_stage IN ({_in(ARCHIVE_STAGES)})),
        reason_code       TEXT NOT NULL,
        comment           TEXT,
        score             INTEGER,
        subscores         TEXT,
        rejected_at       TEXT NOT NULL,
        run_id            TEXT
    );
    CREATE INDEX idx_archive_dedup_key ON job_archive(dedup_key);
    CREATE INDEX idx_archive_url ON job_archive(canonical_url);
    CREATE INDEX idx_archive_reason ON job_archive(reason_code);
    CREATE INDEX idx_archive_source_ext ON job_archive(source, external_id);
    """,
    # 2: place lookups (name -> station and coordinates), cached like travel times (goal.md 5.3.1)
    """
    CREATE TABLE place_cache (
        query          TEXT PRIMARY KEY,
        resolved       INTEGER NOT NULL CHECK (resolved IN (0, 1)),
        station_id     TEXT,
        station_name   TEXT,
        latitude       REAL,
        longitude      REAL,
        in_switzerland INTEGER CHECK (in_switzerland IN (0, 1)),
        provider       TEXT NOT NULL,
        resolved_at    TEXT NOT NULL,
        expires_at     TEXT NOT NULL
    );
    """,
    # 3: batch exchange with Claude, option C (goal.md 5.10)
    """
    -- One row per exported batch file; the batch id is the run id.
    CREATE TABLE batches (
        id         TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        file_name  TEXT NOT NULL,
        sha256     TEXT NOT NULL,
        job_count  INTEGER NOT NULL,
        fresh_count INTEGER NOT NULL
    );

    -- The jobs in a batch. `fresh` = first time the job is exported: only fresh jobs can enter the top 10
    -- (goal.md 5.5). `selected` and `rank` are the top-10 result, recomputed when results arrive.
    -- No foreign key to jobs: a job may be archived later and the batch record stays.
    CREATE TABLE batch_jobs (
        batch_id TEXT NOT NULL REFERENCES batches(id),
        job_id   INTEGER NOT NULL,
        fresh    INTEGER NOT NULL CHECK (fresh IN (0, 1)),
        selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
        rank     INTEGER,
        PRIMARY KEY (batch_id, job_id)
    );
    CREATE INDEX idx_batch_jobs_job ON batch_jobs(job_id);

    -- Every results file seen, accepted or not, with the reason.
    CREATE TABLE imports (
        id          INTEGER PRIMARY KEY,
        batch_id    TEXT,
        file_name   TEXT NOT NULL,
        sha256      TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('imported', 'rejected', 'duplicate')),
        reason      TEXT,
        summary     TEXT,
        imported_at TEXT NOT NULL
    );
    CREATE INDEX idx_imports_sha ON imports(sha256);
    """,
    # 4: the daily report email, one row per batch (goal.md 5.7)
    """
    CREATE TABLE reports (
        batch_id   TEXT PRIMARY KEY,
        status     TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
        attempts   INTEGER NOT NULL,
        error      TEXT,
        subject    TEXT,
        message_id TEXT,
        created_at TEXT NOT NULL,
        sent_at    TEXT
    );
    """,
    # 5: mailbox ingestion and per-run source statistics (goal.md 5.1, 5.9)
    """
    -- Every alert mail already handled, so a mail is never read twice. Keyed by the Message-ID header.
    CREATE TABLE mail_messages (
        message_id  TEXT PRIMARY KEY,
        source      TEXT NOT NULL,
        mail_date   TEXT,
        status      TEXT NOT NULL CHECK (status IN ('ok', 'error')),
        jobs_found  INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        run_id      TEXT,
        handled_at  TEXT NOT NULL
    );
    CREATE INDEX idx_mail_source_date ON mail_messages(source, mail_date);

    -- What each source delivered in each run (shown in the report and on the Health page).
    CREATE TABLE adapter_runs (
        id        INTEGER PRIMARY KEY,
        run_id    TEXT NOT NULL REFERENCES runs(id),
        adapter   TEXT NOT NULL,
        found     INTEGER NOT NULL DEFAULT 0,
        new_jobs  INTEGER NOT NULL DEFAULT 0,
        error     TEXT,
        warning   TEXT,
        UNIQUE (run_id, adapter)
    );
    """,
    # 6: service mails (notices) are told apart from job alerts; errors that were only an unknown mail type are retried
    """
    ALTER TABLE mail_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'alert';
    ALTER TABLE mail_messages ADD COLUMN notice_days INTEGER;
    DELETE FROM mail_messages WHERE status = 'error' AND error LIKE 'not a recognised UBS alert%';
    """,
    # 7: descriptions added by hand and scoring a job again (goal.md 5.1, 5.4)
    """
    ALTER TABLE jobs ADD COLUMN needs_rescore INTEGER NOT NULL DEFAULT 0 CHECK (needs_rescore IN (0, 1));
    ALTER TABLE batch_jobs ADD COLUMN reevaluation INTEGER NOT NULL DEFAULT 0 CHECK (reevaluation IN (0, 1));
    CREATE TABLE description_log (
        id              INTEGER PRIMARY KEY,
        job_id          INTEGER NOT NULL,
        source          TEXT NOT NULL,
        chars           INTEGER NOT NULL,
        previous_status TEXT,
        rescore         INTEGER NOT NULL CHECK (rescore IN (0, 1)),
        set_at          TEXT NOT NULL
    );
    CREATE INDEX idx_description_log_job ON description_log(job_id);
    """,
    # 8: one row per job page requested (each page at most once; the daily cap counts these rows)
    """
    CREATE TABLE description_fetches (
        job_id       INTEGER PRIMARY KEY,
        attempted_at TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('ok', 'error')),
        http_status  INTEGER,
        error        TEXT
    );
    """,
    # 9: a job archived by mistake can be taken out of the archive again; the archive row stays as history
    """
    ALTER TABLE job_archive ADD COLUMN restored_at TEXT;
    ALTER TABLE job_archive ADD COLUMN restored_job_id INTEGER;
    """,
    # 10: jobs that left an employer's list (goal.md 5.1). A sighting from a career site remembers when a complete list
    # read last showed it and in how many complete reads in a row it was missing; `jobs.closed_at` is the marker.
    # Nothing is closed by this script: counting starts with the first complete read after the upgrade.
    """
    ALTER TABLE job_sources ADD COLUMN last_listed_at TEXT;
    ALTER TABLE job_sources ADD COLUMN missed_reads INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN closed_at TEXT;
    """,
    # 11: tailored documents from a results file of schema 1.1 (goal.md 5.6, 5.10). One row per job and kind; a later
    # results file for the same job replaces the row. `content` is the text the session wrote (selection, order and
    # wording; fixed facts come from the fact file later); `status` is `draft`, or `blocked` when the traceability
    # check or the other import checks found something (`findings`). A document goes with its job when the job is archived.
    """
    CREATE TABLE documents (
        id           INTEGER PRIMARY KEY,
        job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        kind         TEXT NOT NULL CHECK (kind IN ('cv', 'cover_letter')),
        run_id       TEXT REFERENCES runs(id),
        import_id    INTEGER REFERENCES imports(id),
        content      TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('draft', 'blocked')),
        findings     TEXT NOT NULL DEFAULT '[]',
        generated_on TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        UNIQUE (job_id, kind)
    );
    CREATE INDEX idx_documents_job ON documents(job_id);
    """,
    # 12: the rendered PDF of a document (goal.md 5.6, slices 3c and 3d). `pdf_path` is relative to `paths.documents`
    # (so the folder can move, for example into a container), `pages` is the page count of that file, `rendered_at` a
    # UTC time stamp. All three are NULL until a PDF exists, and again when a later results file replaces the document.
    """
    ALTER TABLE documents ADD COLUMN pdf_path TEXT;
    ALTER TABLE documents ADD COLUMN pages INTEGER;
    ALTER TABLE documents ADD COLUMN rendered_at TEXT;
    """,
    # 13: the approval step (goal.md 5.6, slice 3e). A document can now be `approved`: its text and its PDF are frozen, the
    # PDF is copied to `paths.approved` and `approved_pdf_path` (relative to that folder) names the copy; `approved_at` is
    # a UTC time stamp. SQLite cannot change a CHECK constraint, so the table is rebuilt the way the SQLite manual
    # describes (new table, copy, drop, rename, indexes again). Nothing refers to `documents`, so the foreign keys of
    # other tables are not touched, and the rows keep their ids. The extra CHECK keeps an approved row from lacking its time.
    """
    CREATE TABLE documents_new (
        id                INTEGER PRIMARY KEY,
        job_id            INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        kind              TEXT NOT NULL CHECK (kind IN ('cv', 'cover_letter')),
        run_id            TEXT REFERENCES runs(id),
        import_id         INTEGER REFERENCES imports(id),
        content           TEXT NOT NULL,
        status            TEXT NOT NULL CHECK (status IN ('draft', 'blocked', 'approved')),
        findings          TEXT NOT NULL DEFAULT '[]',
        generated_on      TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        pdf_path          TEXT,
        pages             INTEGER,
        rendered_at       TEXT,
        approved_at       TEXT,
        approved_pdf_path TEXT,
        UNIQUE (job_id, kind),
        CHECK (status <> 'approved' OR approved_at IS NOT NULL)
    );
    INSERT INTO documents_new (id, job_id, kind, run_id, import_id, content, status, findings, generated_on, created_at,
                               pdf_path, pages, rendered_at)
        SELECT id, job_id, kind, run_id, import_id, content, status, findings, generated_on, created_at,
               pdf_path, pages, rendered_at FROM documents;
    DROP TABLE documents;
    ALTER TABLE documents_new RENAME TO documents;
    CREATE INDEX idx_documents_job ON documents(job_id);
    """,
    # 14: the posting date a job page states (goal.md 5.1), ISO YYYY-MM-DD. NULL = not known. Written once, when the
    # page is read; a stored date is never changed or cleared.
    """
    ALTER TABLE jobs ADD COLUMN posted_on TEXT;
    """,
    # 15: health alerts by e-mail (goal.md 5.9). One row per condition key (for example `backup_failed`); the row is
    # reused when the same condition comes back. `last_sent` = last "raised" mail that went out, `resolved_sent_at` =
    # the "resolved" mail; NULL means not sent (yet). `message` holds no secret and no personal data.
    """
    CREATE TABLE alerts (
        id               INTEGER PRIMARY KEY,
        key              TEXT NOT NULL UNIQUE,
        first_seen       TEXT NOT NULL,
        last_seen        TEXT NOT NULL,
        last_sent        TEXT,
        resolved_at      TEXT,
        resolved_sent_at TEXT,
        message          TEXT NOT NULL,
        count            INTEGER NOT NULL DEFAULT 1 CHECK (count >= 1)
    );
    """,
    # 16: the description resolver (goal.md 5.1a, v0.64.0). One row per job and attempt: where it looked (tier, source,
    # platform, URL, posting id), how sure the match was and how it ended. `day` is the attempt's date (the limits count
    # distinct days and attempts per host and day). `duplicate_of_job_id` marks a job that matched a posting another job
    # already resolved; nothing is archived because of it.
    """
    CREATE TABLE description_resolutions (
        id                  INTEGER PRIMARY KEY,
        job_id              INTEGER NOT NULL,
        attempted_at        TEXT NOT NULL,
        day                 TEXT NOT NULL,
        tier                TEXT NOT NULL CHECK (tier IN ('T1', 'T2')),
        source_key          TEXT NOT NULL,
        platform            TEXT NOT NULL,
        url                 TEXT,
        posting_id          TEXT,
        confidence          REAL,
        status              TEXT NOT NULL CHECK (status IN ('success', 'no_match', 'ambiguous', 'partial', 'refused', 'error')),
        duplicate_of_job_id INTEGER,
        error               TEXT
    );
    CREATE INDEX idx_description_resolutions_job ON description_resolutions(job_id, day);
    CREATE INDEX idx_description_resolutions_posting ON description_resolutions(source_key, posting_id);
    """,
    # 17: postponements (goal.md 5.10, v0.69.0). A job of an imported batch that got no score and no error entry (the
    # usage limit hit, or the session stopped) is recorded once per batch. `fresh` is copied from batch_jobs: while the
    # 72-hour window is open the job may still enter the top 10 (app/postpone.py). No FK to jobs, like batch_jobs.
    """
    CREATE TABLE postponements (
        id          INTEGER PRIMARY KEY,
        job_id      INTEGER NOT NULL,
        batch_id    TEXT NOT NULL REFERENCES batches(id),
        fresh       INTEGER NOT NULL CHECK (fresh IN (0, 1)),
        reason      TEXT NOT NULL CHECK (reason IN ('usage_limit', 'no_result')),
        evidence    TEXT,
        noted_at    TEXT NOT NULL,
        resolved_at TEXT,
        UNIQUE (job_id, batch_id)
    );
    CREATE INDEX idx_postponements_job ON postponements(job_id, resolved_at);
    """,
    # 18: the missing-description queue (goal.md 5.5, v0.72.0). A job in the would-be top 10 of a batch that has no
    # description is not ranked; `queue_rank` is its place in that would-be list. NULL means not queued. Set by
    # results.select_top on every import; no backfill. `title_only` is 1 when the job had no description at export, so its
    # score of that batch is a title-only estimate even after a description arrives (NULL: an older batch, the current
    # `jobs.description_missing` decides).
    """
    ALTER TABLE batch_jobs ADD COLUMN queue_rank INTEGER;
    ALTER TABLE batch_jobs ADD COLUMN title_only INTEGER;
    """,
    # 19: same-job discovery (goal.md 5.1a, v0.73.0). One row per guessed public job board (platform, slug): what the
    # guess found, so a miss is not asked again every day. status hit = a board of the company; other_company = the board
    # exists but names another company; miss = no such board (HTTP 404) or an empty one; error = another HTTP answer.
    # `checks` counts the probes (a miss is asked again after resolver.discovery.miss_retry_days, at most 4 times),
    # `company_norm` is the company that caused the first guess, `day` the date of the latest probe.
    """
    CREATE TABLE IF NOT EXISTS board_guesses (
        id               INTEGER PRIMARY KEY,
        platform         TEXT NOT NULL,
        slug             TEXT NOT NULL,
        status           TEXT NOT NULL CHECK (status IN ('hit', 'miss', 'other_company', 'error')),
        board_company    TEXT,
        jobs_listed      INTEGER,
        http_status      INTEGER,
        first_checked_at TEXT NOT NULL,
        checked_at       TEXT NOT NULL,
        day              TEXT NOT NULL,
        company_norm     TEXT NOT NULL,
        checks           INTEGER NOT NULL DEFAULT 1,
        UNIQUE (platform, slug)
    );
    CREATE INDEX IF NOT EXISTS idx_board_guesses_day ON board_guesses(day);
    """,
    # 20: merge-duplicates (goal.md 5.2, v0.77.1). One row per open job that was merged into another because the company
    # part of the key now reads the same (AXA Switzerland = AXA). `record` is the full loser record (job, sources, locations,
    # filter results, documents, scores, status history, description log), `moved` says what went to the survivor. No FK,
    # like job_archive: the loser row is gone. merged_job_id is not UNIQUE: ids were reused before migration 21.
    """
    CREATE TABLE job_merges (
        id              INTEGER PRIMARY KEY,
        survivor_job_id INTEGER NOT NULL,
        merged_job_id   INTEGER NOT NULL,
        merged_key      TEXT NOT NULL,
        survivor_old_key TEXT NOT NULL,
        new_key         TEXT NOT NULL,
        reason          TEXT NOT NULL CHECK (reason IN ('company_key')),
        record          TEXT NOT NULL,
        moved           TEXT NOT NULL,
        backup_file     TEXT,
        merged_at       TEXT NOT NULL
    );
    CREATE INDEX idx_job_merges_survivor ON job_merges(survivor_job_id);
    CREATE INDEX idx_job_merges_merged ON job_merges(merged_job_id);
    """,    # 21: job ids are never handed out again (v0.80.7). `jobs` is rebuilt with AUTOINCREMENT; the counter starts above every id
    # any table still holds. Runs through _rebuild (foreign keys off, row counts checked). The last five statements remove,
    # once, the rows an earlier job left on an id that was reused: a row is stale when its time is not after the last
    # archive or merge of that id and is before the live job's first sighting (a batch of the job's own run is kept).
    f"""
    CREATE TABLE jobs_new (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        dedup_key           TEXT NOT NULL UNIQUE,
        canonical_url       TEXT,
        title               TEXT NOT NULL,
        company             TEXT NOT NULL,
        location_text       TEXT,
        description         TEXT,
        description_missing INTEGER NOT NULL DEFAULT 1 CHECK (description_missing IN (0, 1)),
        status              TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({_in(STATUSES)})),
        repost              INTEGER NOT NULL DEFAULT 0 CHECK (repost IN (0, 1)),
        first_seen_at       TEXT NOT NULL,
        last_seen_at        TEXT NOT NULL,
        run_id              TEXT REFERENCES runs(id),
        needs_rescore       INTEGER NOT NULL DEFAULT 0 CHECK (needs_rescore IN (0, 1)),
        closed_at           TEXT,
        posted_on           TEXT
    );
    CREATE TEMP TABLE m21_seq AS SELECT COALESCE(MAX(seq), 0) AS seq FROM sqlite_sequence WHERE name = 'jobs';
    INSERT INTO jobs_new ({_M21_COLS}) SELECT {_M21_COLS} FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_new RENAME TO jobs;
    CREATE INDEX idx_jobs_status ON jobs(status);
    CREATE INDEX idx_jobs_canonical_url ON jobs(canonical_url);
    INSERT INTO sqlite_sequence (name, seq) SELECT 'jobs', 0 WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'jobs');
    UPDATE sqlite_sequence SET seq = MAX(seq, (SELECT seq FROM m21_seq), {_M21_HIGH}) WHERE name = 'jobs';
    DROP TABLE m21_seq;
    WITH {_M21_GONE} DELETE FROM description_fetches WHERE job_id IN (SELECT d.job_id FROM description_fetches d JOIN gone g ON g.jid = d.job_id JOIN jobs j ON j.id = d.job_id WHERE d.attempted_at <= g.t AND d.attempted_at < j.first_seen_at);
    WITH {_M21_GONE} DELETE FROM description_log WHERE id IN (SELECT d.id FROM description_log d JOIN gone g ON g.jid = d.job_id JOIN jobs j ON j.id = d.job_id WHERE d.set_at <= g.t AND d.set_at < j.first_seen_at);
    WITH {_M21_GONE} DELETE FROM description_resolutions WHERE id IN (SELECT d.id FROM description_resolutions d JOIN gone g ON g.jid = d.job_id JOIN jobs j ON j.id = d.job_id WHERE d.attempted_at <= g.t AND d.attempted_at < j.first_seen_at);
    WITH {_M21_GONE} DELETE FROM postponements WHERE id IN (SELECT p.id FROM postponements p JOIN gone g ON g.jid = p.job_id JOIN jobs j ON j.id = p.job_id WHERE p.noted_at <= g.t AND p.noted_at < j.first_seen_at);
    WITH {_M21_GONE} DELETE FROM batch_jobs WHERE rowid IN (SELECT bj.rowid FROM batch_jobs bj JOIN batches b ON b.id = bj.batch_id JOIN gone g ON g.jid = bj.job_id JOIN jobs j ON j.id = bj.job_id WHERE b.created_at <= g.t AND b.created_at < j.first_seen_at AND bj.batch_id IS NOT j.run_id);
    """,
    # 22: findings the user accepted as false positives on the job page (v0.83.0). Matched on document, kind, where and text;
    # deleted with the document and when a new results file replaces the document's content.
    """
    CREATE TABLE finding_overrides (
        id            INTEGER PRIMARY KEY,
        document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL,
        finding_where TEXT NOT NULL,
        text          TEXT NOT NULL,
        comment       TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        UNIQUE (document_id, kind, finding_where, text)
    );
    """,
    # 23: "Ask the writer to fix" (v0.85.0, goal.md 5.10a). One row per press of the button. AUTOINCREMENT: the id names
    # the files rev-<id>.json in exchange/llm, so an id is never used twice. Deleted with its document (job archived).
    """
    CREATE TABLE revision_requests (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        kind             TEXT NOT NULL CHECK (kind IN ('cv', 'cover_letter')),
        instruction      TEXT NOT NULL CHECK (length(instruction) <= 1000),
        content_sha      TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'relayed', 'stored', 'failed', 'stale')),
        outcome          TEXT,
        message          TEXT,
        previous_content TEXT,
        created_at       TEXT NOT NULL,
        relayed_at       TEXT,
        deadline         TEXT,
        finished_at      TEXT,
        mailed_at        TEXT,
        mail_error       TEXT,
        scrubbed         INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX idx_revision_active ON revision_requests(document_id) WHERE status IN ('pending', 'relayed');
    CREATE INDEX idx_revision_status ON revision_requests(status);
    """,
    # 24: a closed posting is its own resolution status 'expired' (v0.87.1; before: 'no_match' with an error starting
    # "expired:"), and a partial posting keeps the text it found in `text`, for the paste page. No table has a key to
    # description_resolutions, so this is no REBUILDS entry.
    """
    CREATE TABLE description_resolutions_new (
        id                  INTEGER PRIMARY KEY,
        job_id              INTEGER NOT NULL,
        attempted_at        TEXT NOT NULL,
        day                 TEXT NOT NULL,
        tier                TEXT NOT NULL CHECK (tier IN ('T1', 'T2')),
        source_key          TEXT NOT NULL,
        platform            TEXT NOT NULL,
        url                 TEXT,
        posting_id          TEXT,
        confidence          REAL,
        status              TEXT NOT NULL CHECK (status IN ('success', 'no_match', 'ambiguous', 'partial', 'refused', 'error', 'expired')),
        duplicate_of_job_id INTEGER,
        error               TEXT,
        text                TEXT
    );
    INSERT INTO description_resolutions_new (id, job_id, attempted_at, day, tier, source_key, platform, url, posting_id,
                                             confidence, status, duplicate_of_job_id, error)
        SELECT id, job_id, attempted_at, day, tier, source_key, platform, url, posting_id, confidence,
               CASE WHEN status = 'no_match' AND error LIKE 'expired:%' THEN 'expired' ELSE status END,
               duplicate_of_job_id, error
        FROM description_resolutions;
    DROP TABLE description_resolutions;
    ALTER TABLE description_resolutions_new RENAME TO description_resolutions;
    CREATE INDEX idx_description_resolutions_job ON description_resolutions(job_id, day);
    CREATE INDEX idx_description_resolutions_posting ON description_resolutions(source_key, posting_id);
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database (creating the folder), apply pragmas, migrate to the latest schema."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


# Migrations that rebuild `jobs`, the parent of ON DELETE CASCADE keys. They run with foreign keys off: with them on,
# DROP TABLE jobs deletes every child row, and foreign_key_check does not notice (checked on a copy, 11 Nov 2026).
REBUILDS = frozenset({21})
_KEPT = ("jobs", "job_sources", "job_locations", "job_travel_checks", "job_filter_results", "scores", "status_history", "documents")


def _rebuild(conn: sqlite3.Connection, number: int, script: str) -> None:
    """Run a table-rebuild migration with foreign keys off; roll back if any child row went missing or a reference broke."""
    if conn.in_transaction:
        conn.commit()                                        # executescript() commits a pending transaction too
    was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")                # ignored inside a transaction, so before BEGIN
    try:
        if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise RuntimeError(f"migration {number}: foreign keys could not be switched off; nothing was changed")
        counts = " UNION ALL ".join(f"SELECT '{t}' AS t, COUNT(*) AS n FROM {t}" for t in _KEPT)
        conn.executescript(f"BEGIN IMMEDIATE;\nCREATE TEMP TABLE rebuild_counts AS {counts};\n{script}")
        lost = [r[0] for r in conn.execute(f"SELECT r.t FROM rebuild_counts r JOIN ({counts}) c ON c.t = r.t WHERE c.n <> r.n")]
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if lost or broken:
            raise RuntimeError(f"migration {number}: row counts changed in {lost} or {len(broken)} broken references; rolled back")
        conn.execute("DROP TABLE temp.rebuild_counts")
        conn.execute(f"PRAGMA user_version = {number}")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON" if was_on else "PRAGMA foreign_keys = OFF")


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations, each in its own transaction. Returns the new version."""
    current = schema_version(conn)
    if current > len(MIGRATIONS):
        raise RuntimeError(
            f"database schema version {current} is newer than this code ({len(MIGRATIONS)}); refusing to open"
        )
    for number in range(current + 1, len(MIGRATIONS) + 1):
        script = MIGRATIONS[number - 1]
        if number in REBUILDS:
            _rebuild(conn, number, script)
            continue
        try:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


def start_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Create the `runs` row that jobs, filter results and batches refer to. Safe to call twice."""
    with conn:
        conn.execute("INSERT OR IGNORE INTO runs (id, started_at, status) VALUES (?, ?, 'running')", (run_id, utcnow()))


def set_status(conn: sqlite3.Connection, job_id: int, to_status: str, note: str | None = None) -> None:
    """Change a job's status and record it in status_history. Invalid status values are rejected by the DB."""
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(f"no job with id {job_id}")
    with conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (to_status, job_id))
        conn.execute(
            "INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, row["status"], to_status, note, utcnow()),
        )


def archive_job(
    conn: sqlite3.Connection,
    job_id: int,
    stage: str,
    reason_code: str,
    comment: str | None = None,
    run_id: str | None = None,
    approved_folder: str = "output/approved",
) -> int:
    """Move a working job into job_archive with its full record, then delete the working row.

    The archive keeps the job as it was at rejection (goal.md 5.3.3): fields, locations, sources,
    filter results, and the latest score if there is one. Returns the archive row id.
    """
    if stage not in ARCHIVE_STAGES:
        raise ValueError(f"unknown archive stage {stage!r}")
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise KeyError(f"no job with id {job_id}")
    sources = [dict(r) for r in conn.execute("SELECT * FROM job_sources WHERE job_id = ? ORDER BY id", (job_id,))]
    locations = [r["location_text"] for r in conn.execute(
        "SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (job_id,))]
    filters = [dict(r) for r in conn.execute("SELECT * FROM job_filter_results WHERE job_id = ? ORDER BY id", (job_id,))]
    score = conn.execute("SELECT overall, subscores FROM scores WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    places = []
    for loc in conn.execute("SELECT * FROM job_locations WHERE job_id = ? ORDER BY id", (job_id,)):
        entry = dict(loc)
        entry["travel_checks"] = [dict(t) for t in conn.execute(
            "SELECT * FROM job_travel_checks WHERE location_id = ? ORDER BY id", (loc["id"],))]
        places.append(entry)
    docs = [dict(r) for r in conn.execute("SELECT * FROM documents WHERE job_id = ? ORDER BY id", (job_id,))]
    record = {"job": dict(job), "sources": sources, "filter_results": filters, "locations": places, "documents": docs}
    approved = [d for d in docs if d["status"] == "approved"]
    if approved:
        # The approved PDFs are never deleted or moved (decided by the user, 1 Nov 2026: they may already have gone to
        # an employer). The archive only says where they are; the path is the stored relative one, so no config is needed.
        folders = sorted({d["approved_pdf_path"].rsplit("/", 1)[0] for d in approved if d.get("approved_pdf_path") and "/" in d["approved_pdf_path"]})
        kinds = ", ".join(sorted(d["kind"] for d in approved))
        approved_prefix = approved_folder.replace("\\", "/").rstrip("/") + "/"       # the configured `paths.approved`
        where = f" ({', '.join(approved_prefix + f for f in folders)})" if folders else ""
        note = f"approved documents kept on disk: {kinds}{where}"
        comment = f"{comment}; {note}" if comment else note
        logging.getLogger("recruitment").warning(
            "job %s archived with approved documents (%s); the approved PDFs stay on disk", job_id, kinds)
    first = sources[0] if sources else {}
    with conn:
        cur = conn.execute(
            """INSERT INTO job_archive
               (original_job_id, dedup_key, canonical_url, source, external_id, title, company, locations,
                description, record, rejection_stage, reason_code, comment, score, subscores, rejected_at, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, job["dedup_key"], job["canonical_url"], first.get("source"), first.get("external_id"),
             job["title"], job["company"], json.dumps(locations, ensure_ascii=False), job["description"],
             json.dumps(record, ensure_ascii=False), stage, reason_code, comment,
             score["overall"] if score else None, score["subscores"] if score else None, utcnow(), run_id),
        )
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return cur.lastrowid


# Statuses a restored job gets back (the status it had when it was archived, kept in the archive record). Later statuses
# depend on scores, documents or an application that are not restored, so those jobs come back as `new`.
RESTORED_STATUSES = frozenset({"applied", "interview", "offer"})


def restore_job(conn: sqlite3.Connection, archive_id: int) -> int:
    """Put an archived job back as a working job: with its status from before the rejection if that is one of
    RESTORED_STATUSES (an applied job stays applied), else `new` (no filter results, so the filters run again).

    Everything stored at rejection is used: the job row, its sources and its locations. The archive row is kept as
    history and marked with the restore time and the new job id. Returns the new job id.
    """
    row = conn.execute("SELECT * FROM job_archive WHERE id = ?", (archive_id,)).fetchone()
    if row is None:
        raise KeyError(f"no archive entry {archive_id}")
    if row["restored_at"]:
        raise ValueError(f"archive entry {archive_id} was already restored (as job {row['restored_job_id']})")
    if conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (row["dedup_key"],)).fetchone():
        raise ValueError("a job with the same key is already open; nothing to restore")
    record = json.loads(row["record"])
    job, now = record["job"], utcnow()
    status = job.get("status") if job.get("status") in RESTORED_STATUSES else "new"
    with conn:
        cur = conn.execute(
            """INSERT INTO jobs (dedup_key, canonical_url, title, company, location_text, description, description_missing,
                                 status, repost, first_seen_at, last_seen_at, run_id, posted_on)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (job["dedup_key"], job["canonical_url"], job["title"], job["company"], job["location_text"], job["description"],
             job["description_missing"], status, job["repost"], job["first_seen_at"], now, job.get("posted_on")))
        new_id = cur.lastrowid
        for s in record.get("sources", []):
            conn.execute(
                "INSERT OR IGNORE INTO job_sources (job_id, source, external_id, reference, url, source_date, message_id, seen_at, raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id, s["source"], s["external_id"], s.get("reference"), s.get("url"), s.get("source_date") or "",
                 s.get("message_id"), s["seen_at"], s.get("raw")))
        for loc in record.get("locations", []):
            conn.execute("INSERT OR IGNORE INTO job_locations (job_id, location_text, city, country, resolved, latitude, longitude) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (new_id, loc["location_text"], loc.get("city"), loc.get("country"), loc.get("resolved", 0),
                          loc.get("latitude"), loc.get("longitude")))
        conn.execute("INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) VALUES (?, NULL, ?, ?, ?)",
                     (new_id, status, f"restored from archive entry {archive_id} ({row['reason_code']})", now))
        conn.execute("UPDATE job_archive SET restored_at = ?, restored_job_id = ? WHERE id = ?", (now, new_id, archive_id))
    return new_id


def backup(conn: sqlite3.Connection, dest: str | Path) -> None:
    """Consistent copy of the live database using SQLite's online backup API (goal.md 5.9)."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
