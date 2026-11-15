"""`recheck-travel` (goal.md 5.3.1, v0.68.0): the location rule run again, offline, on cached places and times.

Old state is written by SQL (as the database was before the train fallback existed). No network, no real database."""
import urllib.request
from datetime import datetime, timezone

import pytest

from app import db, descriptions
from app.config import load_config
from app.dedup import ingest_job
from app.filters.runner import run_filters
from app.location import make_location_filter
from app.models import NormalizedJob
from app.travel import ProviderError, Providers
from tests.test_location import BUELACH, ZUERICH_HB, fake, place

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
EXPIRED = "2020-01-01T00:00:00Z"


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


def _place_row(conn, p, query):
    conn.execute("INSERT OR REPLACE INTO place_cache (query, resolved, station_id, station_name, latitude, longitude, in_switzerland, "
                 "provider, resolved_at, expires_at) VALUES (?, 1, ?, ?, ?, ?, 1, 'x', ?, ?)",
                 (query.casefold(), p.station_id, p.name, p.latitude, p.longitude, EXPIRED, EXPIRED))


def seed_old(conn, title, status, town, car_raw, old_outcome="pass", old_reason=None, old_train=40.0, old_exact=False):
    """A job whose stored location result came from a failed timetable: train a straight-line approximation, car exact."""
    p = place(town, 47.5, 8.7)
    for origin, key in ((ZUERICH_HB, "Zürich HB"), (BUELACH, "Exampletown"), (p, town)):
        _place_row(conn, origin, key)
    conn.execute("INSERT OR REPLACE INTO travel_cache (origin, destination, mode, minutes, provider, approximate, assumptions, "
                 "computed_at, expires_at) VALUES ('Exampletown', ?, 'car', ?, 'x', 0, '{}', ?, ?)", (town, car_raw, EXPIRED, EXPIRED))
    jid = ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id=title, title=title, company="Acme",
                                         locations=[town], raw={"work_mode": "On-site"})).job_id     # on-site: the 60-minute limit
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, jid))
    conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, reason_code, comment, created_at) "
                 "VALUES (?, 'location', ?, ?, 'old comment', ?)", (jid, old_outcome, old_reason, EXPIRED))
    loc_id = conn.execute("SELECT id FROM job_locations WHERE job_id = ?", (jid,)).fetchone()[0]
    for mode, minutes, approx in (("train", old_train, 0 if old_exact else 1), ("car", car_raw * 1.25, 0)):
        conn.execute("INSERT INTO job_travel_checks (location_id, mode, origin, destination, minutes, provider, approximate, "
                      "assumptions, computed_at) VALUES (?, ?, 'o', ?, ?, 'x', ?, '{}', ?)", (loc_id, mode, town, minutes, approx, EXPIRED))
    conn.commit()
    return jid


def seed_timetable_job(conn, cfg):
    """A job filtered live with a timetable answer (train 30 min), caches expired afterwards."""
    providers, *_ = fake({"Town2": place("Town2", 47.4, 8.6)}, train={"Town2": 30}, car={"Town2": 20})
    jid = ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id="tt", title="Timetable job", company="Acme",
                                         locations=["Town2"])).job_id
    run_filters(conn, jid, cfg, filters=[make_location_filter(conn, providers, NOW)])
    conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (jid,))
    conn.commit()
    return jid


def expire_caches(conn):
    conn.execute("UPDATE place_cache SET expires_at = ?", (EXPIRED,))
    conn.execute("UPDATE travel_cache SET expires_at = ?", (EXPIRED,))
    conn.commit()


def snapshot(conn):
    tables = ("jobs", "job_filter_results", "job_archive", "job_travel_checks", "job_locations", "place_cache", "travel_cache")
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in tables}


def scenario(conn, cfg):
    flip = seed_old(conn, "Flip", "scored", "Town", 72.8)                       # car 91; train was 40 (approximate)
    user = seed_old(conn, "Mine", "shortlisted", "Town", 72.8)
    tt = seed_timetable_job(conn, cfg)
    uncached = ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id="u", title="Uncached", company="Acme",
                                              locations=["Nowhere"])).job_id
    conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (uncached,))
    conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, reason_code, comment, created_at) "
                 "VALUES (?, 'location', 'conditional', 'travel_time_unavailable', 'old', ?)", (uncached, EXPIRED))
    conn.commit()
    expire_caches(conn)
    return flip, user, tt, uncached


def item(result, job_id):
    return next(i for i in result.items if i.job_id == job_id)


def test_dry_run_lists_flips_and_writes_nothing(conn, cfg):
    flip, user, tt, uncached = scenario(conn, cfg)
    before = snapshot(conn)
    result = descriptions.recheck_travel(conn, cfg, dry_run=True)
    assert snapshot(conn) == before
    i = item(result, flip)
    assert (i.title, i.company, i.old_minutes, i.new_minutes, i.old_outcome, i.new_outcome) == ("Flip", "Acme", 40, 91, "pass", "fail")
    assert (result.checked, result.changed, result.archived, result.skipped_user, result.skipped_uncached) == (4, 1, 1, 1, 1)
    assert tt not in {x.job_id for x in result.items}


def test_apply_archives_a_scored_job_and_leaves_the_rest(conn, cfg):
    flip, user, tt, uncached = scenario(conn, cfg)
    rows_before = conn.execute("SELECT COUNT(*) FROM job_filter_results WHERE job_id = ?", (tt,)).fetchone()[0]
    result = descriptions.recheck_travel(conn, cfg)
    assert result.archived == 1
    assert conn.execute("SELECT reason_code FROM job_archive WHERE original_job_id = ?", (flip,)).fetchone()[0] == "location_too_far"
    assert conn.execute("SELECT 1 FROM jobs WHERE id = ?", (flip,)).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM job_filter_results WHERE job_id = ?", (tt,)).fetchone()[0] == rows_before
    for jid in (user, uncached):                           # untouched: no new filter result
        assert conn.execute("SELECT COUNT(*) FROM job_filter_results WHERE job_id = ?", (jid,)).fetchone()[0] == 1


def test_a_job_you_work_on_is_listed_and_not_touched(conn, cfg):
    flip, user, tt, uncached = scenario(conn, cfg)
    result = descriptions.recheck_travel(conn, cfg)
    i = item(result, user)
    assert "shortlisted" in i.skipped and (i.old_minutes, i.new_minutes) == (40, 91)
    row = conn.execute("SELECT status, needs_rescore FROM jobs WHERE id = ?", (user,)).fetchone()
    assert (row["status"], row["needs_rescore"]) == ("shortlisted", 0)
    assert conn.execute("SELECT COUNT(*) FROM job_travel_checks WHERE location_id IN (SELECT id FROM job_locations WHERE job_id = ?)",
                        (user,)).fetchone()[0] == 2


def test_uncached_place_is_skipped(conn, cfg):
    flip, user, tt, uncached = scenario(conn, cfg)
    result = descriptions.recheck_travel(conn, cfg)
    assert "cache" in item(result, uncached).skipped
    assert conn.execute("SELECT COUNT(*) FROM place_cache WHERE query = 'nowhere'").fetchone()[0] == 0


def test_second_run_changes_nothing(conn, cfg):
    scenario(conn, cfg)
    descriptions.recheck_travel(conn, cfg)
    before = snapshot(conn)
    result = descriptions.recheck_travel(conn, cfg)
    assert result.changed == 0 and result.archived == 0 and snapshot(conn) == before


def test_a_flip_to_conditional_marks_a_scored_job_for_rescoring(conn, cfg):
    cfg["location"]["car_traffic_factor"] = 2.0
    cfg["location"]["train_car_factor"] = 1.9           # car 62 (out), train estimate 59 (within 10 min of the limit)
    jid = seed_old(conn, "Near", "scored", "Town", 31, old_train=20.0)
    result = descriptions.recheck_travel(conn, cfg)
    i = item(result, jid)
    assert (i.new_outcome, i.new_minutes, i.action) == ("conditional", 59, "rescore") and result.rescore == 1
    assert conn.execute("SELECT needs_rescore FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == 1
    assert conn.execute("SELECT 1 FROM job_archive WHERE original_job_id = ?", (jid,)).fetchone() is None
    new = conn.execute("SELECT provider, approximate FROM job_travel_checks WHERE mode = 'train' ORDER BY id DESC").fetchone()
    assert (new["provider"], new["approximate"]) == ("car estimate", 1)


def test_offline_providers_never_call_a_service():
    p = Providers.offline()
    assert p.cache_any_age
    with pytest.raises(ProviderError):
        p.transport.find_station("x")
    with pytest.raises(ProviderError):
        p.osrm.car_minutes(ZUERICH_HB, BUELACH)
    assert p.transport.calls and p.osrm.calls


def test_an_exact_timetable_time_that_is_no_longer_cached_is_skipped(conn, cfg):
    jid = seed_old(conn, "Exact", "scored", "Town", 72.8, old_train=30.0, old_exact=True)       # no train cache row
    before = snapshot(conn)
    result = descriptions.recheck_travel(conn, cfg)
    assert "timetable" in item(result, jid).skipped and result.skipped_uncached == 1 and result.changed == 0
    assert snapshot(conn) == before                                          # without the skip the job would be archived


def test_the_exact_check_is_per_location(conn, cfg):
    jid = seed_old(conn, "Two", "scored", "Town", 72.8)                       # Town: approximate train, car cached
    p = place("Town3", 47.4, 8.6)
    _place_row(conn, p, "Town3")
    conn.execute("INSERT INTO travel_cache (origin, destination, mode, minutes, provider, approximate, assumptions, computed_at, "
                 "expires_at) VALUES ('Zürich HB', 'Town3', 'train', 30, 'x', 0, '{}', ?, ?)", (EXPIRED, EXPIRED))
    conn.execute("INSERT INTO travel_cache (origin, destination, mode, minutes, provider, approximate, assumptions, computed_at, "
                 "expires_at) VALUES ('Exampletown', 'Town3', 'car', 20, 'x', 0, '{}', ?, ?)", (EXPIRED, EXPIRED))
    conn.execute("INSERT INTO job_locations (job_id, location_text) VALUES (?, 'Town3')", (jid,))
    loc_id = conn.execute("SELECT id FROM job_locations WHERE job_id = ? AND location_text = 'Town3'", (jid,)).fetchone()[0]
    conn.execute("INSERT INTO job_travel_checks (location_id, mode, origin, destination, minutes, provider, approximate, assumptions, "
                 "computed_at) VALUES (?, 'train', 'o', 'Town3', 30, 'x', 0, '{}', ?)", (loc_id, EXPIRED))
    conn.commit()
    result = descriptions.recheck_travel(conn, cfg, dry_run=True)
    i = item(result, jid)
    assert i.skipped == "" and result.skipped_uncached == 0 and result.changed == 1      # rechecked, not skipped
