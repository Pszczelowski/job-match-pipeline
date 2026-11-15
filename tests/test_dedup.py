from pathlib import Path

import pytest

from app import db
from app.dedup import canonicalize_url, dedup_key, ingest_job, legacy_dedup_key, normalize_company, normalize_company_key, normalize_text
from app.models import NormalizedJob
from app.parsers.ubs import parse_alert, to_job

FIX = Path(__file__).parent / "fixtures" / "ubs"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def job(**kw):
    base = dict(source="ubs_alert", external_id="1", title="Business Analyst", company="UBS",
                url="https://jobs.ubs.com/x?jobid=1", locations=["Zürich"], source_date="2026-10-28")
    base.update(kw)
    return NormalizedJob(**base)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- normalisation --------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("https://Example.com/Jobs/1/?utm_source=x&utm_medium=y", "https://example.com/Jobs/1"),
    ("https://www.linkedin.com/jobs/view/123/?trk=abc&refId=zzz&trackingId=q", "https://www.linkedin.com/jobs/view/123"),
    ("https://a.com/p?b=2&a=1#frag", "https://a.com/p?a=1&b=2"),
    ("https://jobs.ubs.com/1033/asp/tg/cim_jobdetail.asp?partnerid=25008&siteid=5012&jobid=351249&utm_campaign=z",
     "https://jobs.ubs.com/1033/asp/tg/cim_jobdetail.asp?jobid=351249&partnerid=25008&siteid=5012"),
    ("  ", ""), ("not a url", ""), ("", ""),
])
def test_canonicalize_url(raw, expected):
    assert canonicalize_url(raw) == expected


def test_same_url_different_tracking_is_same_canonical():
    assert canonicalize_url("https://a.com/j/1?utm_source=a") == canonicalize_url("https://a.com/j/1/?fbclid=b")


@pytest.mark.parametrize("a, b", [
    ("Zürich", "Zurich"), ("Senior  Business-Analyst (m/f/d)", "senior business analyst m f d"),
    ("Łódź", "Lodz"), ("Straße", "Strasse"),
])
def test_normalize_text(a, b):
    assert normalize_text(a) == normalize_text(b)


def test_company_suffixes_ignored():
    assert normalize_company("Julius Bär AG") == normalize_company("Julius Bar") == "julius bar"
    assert normalize_company("UBS Group AG") != normalize_company("UBS")  # "Group" is not stripped: kept distinct


def test_dedup_key_location_order_and_case_do_not_matter():
    assert dedup_key("UBS AG", "Analyst", ["Zürich", "Genève"]) == dedup_key("ubs", "ANALYST", ["geneve", "Zurich"])


def test_dedup_key_differs_for_different_title_or_place():
    base = dedup_key("UBS", "Business Analyst", ["Zürich"])
    assert base != dedup_key("UBS", "Senior Business Analyst", ["Zürich"])
    assert base != dedup_key("UBS", "Business Analyst", ["Basel"])
    assert base != dedup_key("Julius Baer", "Business Analyst", ["Zürich"])


# --- ingest ---------------------------------------------------------------------------------

def test_new_job_inserted_with_source_and_locations(conn):
    r = ingest_job(conn, job(locations=["Zürich", "Genève"], description="text"), run_id=None)
    assert r.outcome == "new" and r.job_id
    assert count(conn, "jobs") == 1 and count(conn, "job_sources") == 1 and count(conn, "job_locations") == 2
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["status"] == "new" and row["repost"] == 0 and row["description_missing"] == 0


def test_missing_description_flagged(conn):
    ingest_job(conn, job())
    assert conn.execute("SELECT description_missing FROM jobs").fetchone()[0] == 1


def test_reimport_is_idempotent(conn):
    ingest_job(conn, job())
    r = ingest_job(conn, job())
    assert r.outcome == "unchanged"
    assert count(conn, "jobs") == 1 and count(conn, "job_sources") == 1
    assert conn.execute("SELECT repost FROM jobs").fetchone()[0] == 0


def test_same_source_newer_date_is_seen_again_and_flags_repost(conn):
    first = ingest_job(conn, job(source_date="2026-10-27"))
    r = ingest_job(conn, job(source_date="2026-10-28"))
    assert (r.outcome, r.repost, r.job_id) == ("seen_again", True, first.job_id)
    assert count(conn, "jobs") == 1 and count(conn, "job_sources") == 2
    assert conn.execute("SELECT repost FROM jobs").fetchone()[0] == 1


def test_same_job_from_second_source_is_linked_not_reposted(conn):
    first = ingest_job(conn, job(source="linkedin_alert", external_id="li-9", url="https://linkedin.com/jobs/view/9?trk=a"))
    r = ingest_job(conn, job(source="ubs_alert", external_id="351249"))
    assert (r.outcome, r.job_id, r.matched_by, r.repost) == ("linked", first.job_id, "key", False)
    assert count(conn, "jobs") == 1 and count(conn, "job_sources") == 2
    assert conn.execute("SELECT repost FROM jobs").fetchone()[0] == 0


def test_match_by_canonical_url_when_title_differs(conn):
    first = ingest_job(conn, job(source="linkedin_alert", external_id="a", url="https://x.com/j/5?utm_source=1", title="BA"))
    r = ingest_job(conn, job(source="other", external_id="b", url="https://X.com/j/5/?trk=2", title="Business Analyst (m/f)"))
    assert (r.outcome, r.job_id, r.matched_by) == ("linked", first.job_id, "url")


def test_title_casing_and_punctuation_variants_match(conn):
    ingest_job(conn, job(source="a", external_id="1", url="", title="Senior Business Analyst (m/f/d)"))
    r = ingest_job(conn, job(source="b", external_id="2", url="", title="SENIOR BUSINESS ANALYST  m-f-d", locations=["Zurich"]))
    assert r.outcome == "linked" and count(conn, "jobs") == 1


@pytest.mark.parametrize("other", [
    dict(title="Senior Business Analyst"),
    dict(locations=["Basel"]),
    dict(company="Julius Baer"),
])
def test_genuinely_different_jobs_are_not_merged(conn, other):
    ingest_job(conn, job(url="", source="a", external_id="1"))
    r = ingest_job(conn, job(url="", source="b", external_id="2", **other))
    assert r.outcome == "new" and count(conn, "jobs") == 2


def test_description_filled_in_later_but_never_overwritten(conn):
    ingest_job(conn, job())
    ingest_job(conn, job(source="linkedin_alert", external_id="9", url="", description="full text"))
    row = conn.execute("SELECT description, description_missing FROM jobs").fetchone()
    assert (row[0], row[1]) == ("full text", 0)
    ingest_job(conn, job(source="third", external_id="3", url="", description="other text"))
    assert conn.execute("SELECT description FROM jobs").fetchone()[0] == "full text"


def test_invalid_input_rejected(conn):
    for bad in (job(title=" "), job(company=""), job(external_id=""), job(source="")):
        with pytest.raises(ValueError):
            ingest_job(conn, bad)
    assert count(conn, "jobs") == 0


# --- archive --------------------------------------------------------------------------------

def test_archived_job_arriving_again_is_not_new(conn):
    first = ingest_job(conn, job())
    aid = db.archive_job(conn, first.job_id, "filter", "seniority")
    r = ingest_job(conn, job(source_date="2026-10-30"))
    assert (r.outcome, r.archive_id, r.matched_by) == ("archived", aid, "source")
    assert count(conn, "jobs") == 0 and count(conn, "job_archive") == 1


def test_archived_job_matched_from_other_source_by_key_and_url(conn):
    first = ingest_job(conn, job(url="https://a.com/j/1"))
    aid = db.archive_job(conn, first.job_id, "user", "user_rejected")
    by_key = ingest_job(conn, job(source="linkedin_alert", external_id="x", url=""))
    by_url = ingest_job(conn, job(source="linkedin_alert", external_id="y", url="https://a.com/j/1/?utm_source=z",
                                  title="Other title", locations=["Bern"]))
    assert (by_key.outcome, by_key.archive_id, by_key.matched_by) == ("archived", aid, "key")
    assert (by_url.outcome, by_url.archive_id, by_url.matched_by) == ("archived", aid, "url")
    assert count(conn, "jobs") == 0


# --- the real alerts ------------------------------------------------------------------------

def run_alerts(conn, names):
    results = []
    for name in names:
        for j in parse_alert((FIX / name).read_text(encoding="utf-8")).jobs:
            results.append(ingest_job(conn, to_job(j, message_id=name)))
    return results


ALL = ["alert_2026-10-24.html", "alert_2026-10-28.html", "alert_2026-10-29.html", "alert_2026-10-30.html"]


def test_real_alerts_give_nine_jobs_and_one_repost(conn):
    results = run_alerts(conn, ALL)
    assert [r.outcome for r in results].count("new") == 9
    assert [r.outcome for r in results].count("seen_again") == 1
    assert count(conn, "jobs") == 9 and count(conn, "job_sources") == 10
    reposts = conn.execute("SELECT j.id FROM jobs j JOIN job_sources s ON s.job_id=j.id "
                           "WHERE j.repost=1 AND s.external_id='351249' GROUP BY j.id").fetchall()
    assert len(reposts) == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE repost=1").fetchone()[0] == 1


def test_real_alerts_rerun_creates_nothing_new(conn):
    run_alerts(conn, ALL)
    again = run_alerts(conn, ALL)
    assert {r.outcome for r in again} == {"unchanged"}
    assert count(conn, "jobs") == 9 and count(conn, "job_sources") == 10


def test_real_alerts_stored_fields(conn):
    run_alerts(conn, ["alert_2026-10-24.html"])
    row = conn.execute("SELECT j.*, s.reference, s.source_date FROM jobs j JOIN job_sources s ON s.job_id=j.id "
                       "WHERE s.external_id='352530'").fetchone()
    assert row["company"] == "UBS" and row["reference"] == "344962BR" and row["source_date"] == "2026-10-23"
    assert row["canonical_url"].endswith("jobid=352530&partnerid=25008&siteid=5012")
    locs = [r[0] for r in conn.execute("SELECT location_text FROM job_locations WHERE job_id=?", (row["id"],))]
    assert sorted(locs) == ["Genève", "Zürich"]


def test_dedup_key_strips_bank_word():
    assert dedup_key("Sygnum Bank", "Automation Engineer", ["Zürich"]) == dedup_key("Sygnum", "Automation Engineer", ["Zürich"])


def test_dedup_key_strips_country_word():
    assert normalize_company_key("AXA Switzerland") == normalize_company_key("AXA") == "axa"
    assert normalize_company_key("Foo Group Holding Schweiz") == "foo"
    assert dedup_key("AXA Switzerland", "Analyst", ["Winterthur"]) == dedup_key("AXA", "Analyst", ["Winterthur"])


def test_company_trailing_word_alone_is_kept():
    assert normalize_company_key("Bank") == "bank"
    assert normalize_company_key("Switzerland AG") == "switzerland"


def test_different_companies_not_merged():
    assert normalize_company_key("Bank Pekao") != normalize_company_key("Pekao Investment")
    assert normalize_company_key("Bank Pekao") == "bank pekao"


def test_normalize_company_itself_keeps_trailing_words():
    assert normalize_company("UBS Group AG") == "ubs group"  # only the dedup key drops them


def test_ingest_links_company_variant(conn):
    first = ingest_job(conn, job(company="Sygnum", external_id="a", url="https://a.example/1"))
    second = ingest_job(conn, job(company="Sygnum Bank", source="linkedin_alert", external_id="b", url="https://b.example/2"))
    assert second.job_id == first.job_id
    assert second.outcome == "linked"
    assert second.matched_by == "key"
    assert count(conn, "jobs") == 1


def test_job_stored_under_legacy_key_still_matches(conn):
    first = ingest_job(conn, job(company="Liebherr Group", external_id="a", url="https://a.example/1"))
    conn.execute("UPDATE jobs SET dedup_key = ? WHERE id = ?",
                 (legacy_dedup_key("Liebherr Group", "Business Analyst", ["Zürich"]), first.job_id))
    conn.commit()
    second = ingest_job(conn, job(company="Liebherr Group", source="linkedin_alert", external_id="b", url="https://b.example/2"))
    assert second.outcome == "linked"
    assert second.job_id == first.job_id
    assert second.matched_by == "key"
    assert count(conn, "jobs") == 1


def test_archive_row_stored_under_legacy_key_still_matches(conn):
    first = ingest_job(conn, job(company="Hoval Schweiz", external_id="a", url="https://a.example/1"))
    aid = db.archive_job(conn, first.job_id, "user", "user_rejected")
    conn.execute("UPDATE job_archive SET dedup_key = ? WHERE id = ?",
                 (legacy_dedup_key("Hoval Schweiz", "Business Analyst", ["Zürich"]), aid))
    conn.commit()
    second = ingest_job(conn, job(company="Hoval Schweiz", source="linkedin_alert", external_id="b", url="https://b.example/2"))
    assert (second.outcome, second.archive_id, second.matched_by) == ("archived", aid, "key")
    assert count(conn, "jobs") == 0


# --- core cities (batch-rest item 19) -------------------------------------------------------

def _loose(conn, first_places, second_places, **kw):
    ingest_job(conn, job(locations=first_places, url="", **kw))
    return ingest_job(conn, job(source="linkedin_alert", external_id="x", locations=second_places, url="", **kw))


@pytest.mark.parametrize("a, b", [
    (["Warszawa"], ["Warsaw, Masovian, Poland"]),
    (["Cracow"], ["Kraków"]),
    (["Zurich"], ["Zürich, Switzerland"]),
])
def test_core_city_spelling_variants_link_across_sources(conn, a, b):
    r = _loose(conn, a, b)
    assert (r.outcome, r.matched_by) == ("linked", "city")
    assert count(conn, "jobs") == 1 and count(conn, "job_sources") == 2


@pytest.mark.parametrize("a, b", [
    (["Warszawa"], ["Warsaw, Indiana, United States"]),
    (["Remote"], ["Remote, Switzerland"]),
    (["Remote"], [""]),
    (["Zurich"], ["Zurich", "Basel"]),
])
def test_core_city_no_loose_match(conn, a, b):
    r = _loose(conn, a, b)
    assert r.outcome == "new" and count(conn, "jobs") == 2


def test_core_city_needs_same_title(conn):
    ingest_job(conn, job(title="X", locations=["Warszawa"], url=""))
    r = ingest_job(conn, job(source="linkedin_alert", external_id="x", title="Senior X", locations=["Warsaw"], url=""))
    assert r.outcome == "new"


def test_core_city_same_source_is_not_loosely_matched(conn):
    ingest_job(conn, job(locations=["Warszawa"], url=""))
    r = ingest_job(conn, job(external_id="2", locations=["Warsaw"], url=""))
    assert r.outcome == "new" and count(conn, "jobs") == 2


def test_core_city_two_candidates_give_new(conn):
    ingest_job(conn, job(external_id="1", locations=["Warszawa"], url=""))
    ingest_job(conn, job(external_id="2", locations=["Warsaw, Poland"], url=""))     # same source: a second job
    assert count(conn, "jobs") == 2
    r = ingest_job(conn, job(source="linkedin_alert", external_id="x", locations=["Warsaw"], url=""))
    assert r.outcome == "new" and count(conn, "jobs") == 3


def test_core_city_archived_job_is_not_loosely_matched(conn):
    first = ingest_job(conn, job(locations=["Warszawa"], url=""))
    db.archive_job(conn, first.job_id, "user", "user_rejected")
    r = ingest_job(conn, job(source="linkedin_alert", external_id="x", locations=["Warsaw, Poland"], url=""))
    assert r.outcome == "new"


def test_core_cities_function():
    from app.dedup import core_cities
    assert core_cities(["Warszawa", "Warsaw, Poland"]) == frozenset({"warszawa"})
    assert core_cities(["Warsaw, Indiana, United States"]) == frozenset()
    assert core_cities(["Zurich", "Remote"]) == frozenset()
