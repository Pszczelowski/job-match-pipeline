"""`recheck-archive-location` (v0.92.0): archived location rejections run through the location rule again, offline.

Jobs are filtered with fake providers (which fills the caches) and then archived by hand with the old reason code.
No network, no real database."""
import json
import urllib.request
from datetime import datetime, timezone

import pytest

from app import archive_recheck, db
from app.config import load_config
from app.dedup import ingest_job
from app.filters.base import context_from_record, load_context
from app.location import make_location_filter
from app.models import NormalizedJob
from tests.test_location import fake, place

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
FILLER = "You will work with the business and the IT teams to define requirements. "


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"]["exchange"] = str(tmp_path / "exchange")
    c["paths"]["approved"] = str(tmp_path / "approved")
    return c


@pytest.fixture
def conn(tmp_path, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def archived(conn, cfg, title, location, raw, reason, description="", train=100, warm=True):
    """Ingest a job, run the location filter with fake providers (fills the caches) and archive it with `reason`."""
    jid = ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id=title, title=title, company="Acme", url="",
                                         locations=[location], description=description, raw=raw)).job_id
    if warm:
        providers, *_ = fake({location: place(location, 47.4, 8.0)}, train={location: train}, car={location: 400})
        make_location_filter(conn, providers, NOW)(load_context(conn, jid), cfg)
    return db.archive_job(conn, jid, "filter", reason, "old")


def scenario(conn, cfg):
    return {
        "hybrid": archived(conn, cfg, "Hybrid far", "Aarau", {"work_mode": "Hybrid"}, "location_too_far"),
        "onsite": archived(conn, cfg, "Onsite far", "Aarau", {"work_mode": "On-site"}, "location_too_far"),
        "uncached": archived(conn, cfg, "Uncached", "Xtown", {"work_mode": "Hybrid"}, "location_too_far", warm=False),
        "poland": archived(conn, cfg, "Polish office", "Warsaw, Poland", {"work_mode": "On-site"}, "poland_on_site",
                           description=FILLER + "The office is attended once a month."),
        "duplicate": archived(conn, cfg, "Open twin", "Aarau", {"work_mode": "Hybrid"}, "location_too_far"),
    }


def reopen_twin(conn):
    """An open job with the same dedup key as the archived 'Open twin' (written by SQL: ingest keeps an archived key archived)."""
    row = conn.execute("SELECT dedup_key, record FROM job_archive WHERE title = 'Open twin'").fetchone()
    job = json.loads(row["record"])["job"]
    with conn:
        conn.execute("INSERT INTO jobs (dedup_key, canonical_url, title, company, location_text, description, description_missing, "
                     "status, repost, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', 0, ?, ?)",
                     (row["dedup_key"], job["canonical_url"], job["title"], job["company"], job["location_text"],
                      job["description"], job["description_missing"], job["first_seen_at"], job["last_seen_at"]))


def snapshot(conn):
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in ("jobs", "job_archive", "job_filter_results", "place_cache", "travel_cache", "job_locations", "status_history")}


def by_title(result):
    return {i.title: i for i in result.items}


def test_context_from_record_rebuilds_what_the_filters_read(conn, cfg):
    aid = archived(conn, cfg, "Hybrid far", "Aarau", {"work_mode": "Hybrid"}, "location_too_far", description="Text.")
    job = context_from_record(json.loads(conn.execute("SELECT record FROM job_archive WHERE id = ?", (aid,)).fetchone()[0]))
    assert (job.title, job.description, job.locations, job.raw.get("work_mode"), job.sources) == \
        ("Hybrid far", "Text.", ["Aarau"], "Hybrid", ["linkedin_alert"])


def test_dry_run_lists_the_rows_and_writes_nothing(conn, cfg):
    scenario(conn, cfg)
    reopen_twin(conn)
    before = snapshot(conn)
    result = archive_recheck.recheck_archive_location(conn, cfg, dry_run=True)
    assert snapshot(conn) == before
    items = by_title(result)
    assert (items["Hybrid far"].new_outcome, items["Hybrid far"].new_reason, items["Hybrid far"].action) == \
        ("pass", "location_hybrid", "restore")
    assert (items["Onsite far"].new_outcome, items["Onsite far"].new_reason, items["Onsite far"].action) == ("fail", "location_too_far", "")
    assert items["Uncached"].skipped and "cache" in items["Uncached"].skipped
    assert (items["Polish office"].new_outcome, items["Polish office"].new_reason) == ("pass", "poland_hybrid")
    assert "already open" in items["Open twin"].skipped
    assert items["Hybrid far"].locations == ["Aarau"] and items["Hybrid far"].company == "Acme"
    assert (result.restored, result.still_failing, result.skipped_uncached, result.skipped_open) == (2, 1, 1, 1)


def test_apply_restores_only_the_listed_rows(conn, cfg):
    scenario(conn, cfg)
    reopen_twin(conn)
    result = archive_recheck.recheck_archive_location(conn, cfg)
    assert result.restored == 2
    restored = {r["title"] for r in conn.execute("SELECT title FROM job_archive WHERE restored_at IS NOT NULL")}
    assert restored == {"Hybrid far", "Polish office"}
    open_jobs = sorted(r["title"] for r in conn.execute("SELECT title FROM jobs"))
    assert open_jobs == ["Hybrid far", "Open twin", "Polish office"]
    assert {r["status"] for r in conn.execute("SELECT status FROM jobs WHERE title IN ('Hybrid far', 'Polish office')")} == {"new"}
    assert all(i.restored_job_id for i in result.items if i.action == "restore")


def test_a_second_run_changes_nothing(conn, cfg):
    scenario(conn, cfg)
    reopen_twin(conn)
    archive_recheck.recheck_archive_location(conn, cfg)
    before = snapshot(conn)
    again = archive_recheck.recheck_archive_location(conn, cfg)
    assert snapshot(conn) == before
    assert again.restored == 0 and all(i.action == "" for i in again.items)


def test_other_archive_rows_are_not_selected(conn, cfg):
    archived(conn, cfg, "Other reason", "Aarau", {"work_mode": "Hybrid"}, "language_c1_required")
    stage = archived(conn, cfg, "Other stage", "Aarau", {"work_mode": "Hybrid"}, "location_too_far")
    conn.execute("UPDATE job_archive SET rejection_stage = 'user' WHERE id = ?", (stage,))
    result = archive_recheck.recheck_archive_location(conn, cfg)
    assert result.items == [] and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0



def test_online_resolves_a_row_that_is_skipped_offline(conn, cfg):
    aid = archived(conn, cfg, "Cold hybrid", "Brugg", {"work_mode": "Hybrid"}, "location_too_far", warm=False)
    assert archive_recheck.recheck_archive_location(conn, cfg, dry_run=True).items[0].skipped
    providers, *_ = fake({"Brugg": place("Brugg", 47.48, 8.2)}, train={"Brugg": 100}, car={"Brugg": 400})
    result = archive_recheck.recheck_archive_location(conn, cfg, providers=providers)
    assert (result.items[0].new_reason, result.restored) == ("location_hybrid", 1)
    assert conn.execute("SELECT restored_at FROM job_archive WHERE id = ?", (aid,)).fetchone()[0]


def test_online_never_restores_on_a_service_failure(conn, cfg):
    archived(conn, cfg, "Cold hybrid", "Brugg", {"work_mode": "Hybrid"}, "location_too_far", warm=False)
    providers, *_ = fake({}, search_error=True)
    result = archive_recheck.recheck_archive_location(conn, cfg, providers=providers)
    assert result.restored == 0 and result.skipped_uncached == 1 and conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
