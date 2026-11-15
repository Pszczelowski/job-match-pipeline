"""The posting date of a job (goal.md 5.1, 5.2, 5.7; migration 14, v0.35.0): parsing, storing, report, batch, archive."""
import json

import pytest

from app import adapters, db
from app.adapters import six
from app.descriptions import apply_description, normalise_posted
from app.report import posted_line
from tests.test_adapters import NOW, Pages, cfg, conn, listed_and_filtered   # noqa: F401  (fixtures)
from tests import test_report as tr
from tests.test_report import cfg as report_cfg, conn as report_conn          # noqa: F401  (fixtures)

LONG = "We are looking for a reporting analyst. " * 10


def add_job(conn, **kw):
    cur = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) "
                       "VALUES (?, 'Analyst', 'Acme', 'scored', 'a', 'b')", (kw.get("key", "k"),))
    conn.commit()
    return cur.lastrowid


def posted_on(conn, job_id):
    return conn.execute("SELECT posted_on FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


# --- parsing ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, iso", [
    ("Tue Oct 13 00:00:00 UTC 2026", "2026-10-13"),
    ("2025-11-19T01:00:00+01:00", "2025-11-19"),
    ("2025-11-19", "2025-11-19"),
    ("  2025-11-19 10:00  ", "2025-11-19"),
])
def test_known_formats_become_iso(raw, iso):
    assert normalise_posted(raw) == iso


@pytest.mark.parametrize("raw", ["", None, "   ", "garbage", "2026-13-40", "2026-02-30T00:00:00Z", "Tue Foo 13 00:00:00 UTC 2026", "13.10.2026"])
def test_garbage_and_empty_give_none_and_never_raise(raw):
    assert normalise_posted(raw) is None


def test_adapters_return_iso_or_empty():
    page = '<meta itemprop="datePosted" content="Tue Oct 13 00:00:00 UTC 2026"><span itemprop="description">' + LONG + "</span>"
    assert six.parse_detail(page).posted == "2026-10-13"
    assert six.parse_detail(page.replace("Tue Oct 13 00:00:00 UTC 2026", "soon")).posted == ""


# --- storing ---------------------------------------------------------------------------------------

def test_a_fetched_job_page_stores_the_date(conn, cfg):                       # noqa: F811
    listed_and_filtered(conn, cfg)
    adapters.fetch_details(conn, cfg, six.SPEC, Pages(), sleep=lambda s: None, now=NOW, run_id="run1")
    dates = [r[0] for r in conn.execute("SELECT posted_on FROM jobs")]
    assert dates == ["2026-10-13"] * 3


def test_a_stored_date_is_kept_and_none_never_overwrites(conn, cfg):          # noqa: F811
    jid = add_job(conn)
    apply_description(conn, cfg, jid, LONG, check_language=False, posted_on="Tue Oct 13 00:00:00 UTC 2026")
    assert posted_on(conn, jid) == "2026-10-13"
    apply_description(conn, cfg, jid, LONG + " Changed text.", check_language=False, posted_on=None)
    apply_description(conn, cfg, jid, LONG + " Changed again.", check_language=False, posted_on="garbage")
    assert posted_on(conn, jid) == "2026-10-13"
    apply_description(conn, cfg, jid, LONG + " Third.", check_language=False, posted_on="2026-11-01")     # different date: first one stays
    assert posted_on(conn, jid) == "2026-10-13"


def test_a_job_without_a_date_gets_one_later_even_if_the_text_is_unchanged(conn, cfg):    # noqa: F811
    jid = add_job(conn)
    apply_description(conn, cfg, jid, LONG, check_language=False)
    assert posted_on(conn, jid) is None
    apply_description(conn, cfg, jid, LONG, check_language=False, posted_on="2026-10-20")
    assert posted_on(conn, jid) == "2026-10-20"


# --- migration 14 ----------------------------------------------------------------------------------

def test_migration_14_adds_the_column_and_keeps_the_rows(tmp_path):
    path = tmp_path / "old.db"
    raw = db.sqlite3.connect(str(path))
    for number, script in enumerate(db.MIGRATIONS[:13], start=1):
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    raw.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('k', 'Analyst', 'SIX', 'scored', 'a', 'b')")
    raw.commit()
    raw.close()
    c = db.connect(path)
    assert db.schema_version(c) == len(db.MIGRATIONS) >= 14
    assert c.execute("SELECT title, status, posted_on FROM jobs").fetchone()[:] == ("Analyst", "scored", None)
    c.close()


# --- report ----------------------------------------------------------------------------------------

def test_posted_line():
    assert posted_line("2026-10-13") == "posted 13 Oct 2026"
    assert posted_line("2026-11-01") == "posted 1 Nov 2026"
    assert posted_line(None) == "" and posted_line("") == "" and posted_line("junk") == ""


def test_report_shows_the_date_only_when_known(report_conn, report_cfg):       # noqa: F811
    ids, _ = tr.pipeline(report_conn, report_cfg, [tr.job(1), tr.job(2)], [80, 70])
    report_conn.execute("UPDATE jobs SET posted_on = '2026-10-13' WHERE id = ?", (ids[0],))
    report_conn.commit()
    r = tr.report_for(report_conn, report_cfg)
    assert r.text.count("posted ") == 1 and "posted 13 Oct 2026" in r.text
    assert r.html.count("posted ") == 1 and "posted 13 Oct 2026" in r.html
    assert [j.posted for j in r.jobs] == ["posted 13 Oct 2026", ""]


# --- batch and archive -----------------------------------------------------------------------------

def test_batch_entry_carries_posted_on(report_conn, report_cfg):                # noqa: F811
    ids, _ = tr.pipeline(report_conn, report_cfg, [tr.job(1)], [80])
    report_conn.execute("UPDATE jobs SET posted_on = '2026-10-13' WHERE id = ?", (ids[0],))
    report_conn.commit()
    from app.batch import _job_entry
    assert _job_entry(report_conn, ids[0], True)["posted_on"] == "2026-10-13"


def test_the_archive_record_keeps_the_date(conn):                               # noqa: F811
    jid = add_job(conn)
    conn.execute("UPDATE jobs SET posted_on = '2026-10-13' WHERE id = ?", (jid,))
    conn.commit()
    db.archive_job(conn, jid, "user", "user_rejected")
    record = json.loads(conn.execute("SELECT * FROM job_archive").fetchone()["record"])
    assert record["job"]["posted_on"] == "2026-10-13"
    new_id = db.restore_job(conn, conn.execute("SELECT id FROM job_archive").fetchone()["id"])      # restoring keeps it
    assert posted_on(conn, new_id) == "2026-10-13"
