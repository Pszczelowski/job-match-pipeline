"""Parser of the jobs.ch job-alert mail (goal.md 5.1, v0.84.0): the sanitised copy of the real alert of 12 Nov 2026."""
from pathlib import Path

import pytest

from app.parsers import jobsch
from app.parsers.jobsch import ParseError, parse_alert, to_job

FIXTURES = Path(__file__).parent / "fixtures" / "jobsch"
REAL = (FIXTURES / "alert_2026-11-12.html").read_text(encoding="utf-8")
UID_1, UID_2 = "60c67166-41c0-4d7a-88e2-c00a9335fa99", "13fcf051-59da-4eda-8899-99dad19c94ee"
PROMO = ["ee9933bb-0007-4550-9b18-4eca2f2baea4", "3dd7aa1f-832d-45d3-8c1f-2be069e2ec5d", "566efdaa-8075-4ae9-a837-8e975b0aebeb"]


def test_the_real_alert_gives_exactly_the_two_jobs():
    r = parse_alert(REAL)
    assert [j.external_id for j in r.jobs] == [UID_1, UID_2] and r.errors == []
    a, b = r.jobs
    assert (a.title, a.company, a.location) == ("Equity Analyst/in Schweizer Aktien (100%)", "schilling partners ag", "Zug")
    assert (b.title, b.company) == ("Technical Sales Support Manager (m/w/d/) 100%", "Gi Group Burgdorf")
    assert b.location == "OberaargauRegion Oberaargau – Berner Mittelland"


def test_the_url_is_the_short_clean_detail_link_without_the_personal_query():
    for job in parse_alert(REAL).jobs:
        assert job.url == f"https://www.jobs.ch/en/vacancies/detail/{job.external_id}/"
        assert "?" not in job.url and "hash" not in job.url and "uid" not in job.url


def test_the_same_job_in_two_sections_is_one_job():
    assert REAL.count(UID_1) > 2 and [j.external_id for j in parse_alert(REAL).jobs].count(UID_1) == 1


def test_the_promo_block_is_skipped():
    ids = {j.external_id for j in parse_alert(REAL).jobs}
    assert not ids & set(PROMO)
    assert all(p in REAL for p in PROMO)                      # they are in the mail, with utm_content=job-link-promo


def test_account_unsubscribe_and_salary_links_are_ignored():
    for href in ("https://www.jobs.ch/en/account/job-alert/x/?utm_content=landingpage-btn",
                 "https://www.jobs.ch/en/salary/start/?utm_content=compare-salary",
                 "https://www.jobs.ch/en/job-alert/unsubscribe/x/",
                 f"https://www.jobs.ch/en/vacancies/detail/{UID_1}/?utm_content=job-link-promo"):
        assert jobsch.job_from_url(href) == ("", "")
    assert jobsch.job_from_url(f"https://www.jobs.ch/en/vacancies/detail/{UID_1.upper()}/?utm_content=job-link") == (UID_1, f"https://www.jobs.ch/en/vacancies/detail/{UID_1}/")
    assert jobsch.job_from_url(f"https://evil.example/en/vacancies/detail/{UID_1}/?utm_content=job-link") == ("", "")


def test_a_company_with_a_comma_splits_at_the_last_one():
    html = REAL.replace("schilling partners ag, Zug", "Foo, Bar AG, Zug")
    job = parse_alert(html).jobs[0]
    assert (job.company, job.location) == ("Foo, Bar AG", "Zug")


def test_a_line_without_a_place_keeps_the_job_without_a_location():
    job = parse_alert(REAL.replace("schilling partners ag, Zug", "schilling partners ag")).jobs[0]
    assert (job.company, job.location) == ("schilling partners ag", "") and to_job(job).locations == []


def test_a_row_without_the_company_line_is_reported_and_skipped():
    html = REAL.replace("schilling partners ag, Zug", "")
    r = parse_alert(html)
    assert [j.external_id for j in r.jobs] == [UID_2] and any(UID_1 in e for e in r.errors)


def test_to_job_has_no_description_and_no_date():
    job = to_job(parse_alert(REAL).jobs[0], "<m@x>")
    assert (job.source, job.external_id, job.company, job.description, job.source_date, job.message_id) == (
        "jobsch_alert", UID_1, "schilling partners ag", "", "", "<m@x>")
    assert job.locations == ["Zug"]


def test_a_mail_that_is_not_an_alert_is_a_parse_error():
    with pytest.raises(ParseError):
        parse_alert("<html><body><p>Thank you for registering</p></body></html>")


def test_a_marker_without_jobs_is_an_empty_result():
    r = parse_alert("<html><body><p>0 new jobs for your search</p></body></html>")
    assert r.jobs == [] and r.errors == []
    only_promo = REAL.replace("utm_content=job-link&amp;", "utm_content=job-link-promo&amp;")
    assert parse_alert(only_promo).jobs == []


def test_the_fixture_holds_no_personal_data():
    for name in ("alert_2026-11-12.html", "alert_2026-11-12.txt"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert "@gmail" not in text and "@" not in text.replace("@media", "").replace("@font-face", "")
