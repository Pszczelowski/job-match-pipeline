"""The Julius Baer job-alert parser (goal.md 5.1, v0.48.0), on the sanitised copy of the one real alert (1 Nov 2026)."""
import re
from pathlib import Path

import pytest

from app.dedup import canonicalize_url
from app.parsers.julius import SOURCE, ParseError, job_from_url, parse_alert, to_job

FIX = Path(__file__).parent / "fixtures" / "julius"
REAL = (FIX / "alert_2026-11-02.html").read_text(encoding="utf-8")
URL = "https://juliusbaer.wd3.myworkdayjobs.com/External/job/Geneva/Account-Manager--Turkey--100---f-m-d-_r-20213-1"
TITLE = "Account Manager, Turkey, 100% (f/m/d)"


def parse(html=REAL):
    return parse_alert(html)


def row_span(html=REAL):
    """(start, end) of the one job row: the section that holds the title paragraph up to the next section."""
    start = html.rfind('<div style="margin:0px auto;max-width:600px;">', 0, html.index(f"<p>{TITLE}</p>"))
    end = html.index('<div style="margin:0px auto;max-width:600px;">', html.index("Apply</span>"))
    return start, end


def two_jobs(second_id="r-20999-2", second_title="Relationship Manager, Zurich (f/m/d)", second_place="Zurich"):
    start, end = row_span()
    row = (REAL[start:end].replace("r-20213-1", second_id).replace(TITLE, second_title)
           .replace("<p>Geneva</p>", f"<p>{second_place}</p>").replace("/Geneva/", f"/{second_place}/"))
    return REAL[:end] + row + REAL[end:]


# --- the real alert -----------------------------------------------------------------------------------

def test_real_alert_gives_the_one_job():
    r = parse()
    assert r.errors == [] and len(r.jobs) == 1
    j = r.jobs[0]
    assert (j.external_id, j.title, j.company, j.location, j.reference) == ("r-20213-1", TITLE, "Julius Baer", "Geneva", "R-20213")
    assert j.url == URL


def test_the_url_is_clean_and_the_shared_id_is_not_kept():
    assert "shared_id=REDACTED" in REAL
    j = parse().jobs[0]
    assert "?" not in j.url and "shared_id" not in repr(parse()) and "REDACTED" not in repr(parse())
    assert j.url.startswith("https://juliusbaer.wd3.myworkdayjobs.com/External/job/Geneva/")


def test_to_job_is_a_normalized_job_without_a_date_or_description():
    n = to_job(parse().jobs[0], message_id="<m1@x>")
    assert (n.source, n.external_id, n.title, n.company) == (SOURCE, "r-20213-1", TITLE, "Julius Baer") and SOURCE == "julius_alert"
    assert n.url == URL and n.locations == ["Geneva"] and n.reference == "R-20213"
    assert n.source_date == "" and n.description == "" and n.message_id == "<m1@x>"


def test_the_canonical_url_of_dedup_is_stable():
    url = to_job(parse().jobs[0]).url
    assert canonicalize_url(url) == url == URL


def test_other_links_of_the_mail_are_not_jobs():
    # "See all jobs", the job-alert settings, the company site and the social links are not job postings
    for href in ("https://juliusbaer.wd3.myworkdayjobs.com/en-US/External", "https://juliusbaer.wd3.myworkdayjobs.com/en-US/External/jobAlerts",
                 "https://www.juliusbaer.com/ch/en/", "https://www.linkedin.com/company/julius-baer/", ""):
        assert job_from_url(href) == ("", ""), href


def test_job_links_with_a_language_prefix_and_other_ids():
    assert job_from_url("https://juliusbaer.wd3.myworkdayjobs.com/en-US/External/job/Zurich/Analyst_R-123?x=1") == (
        "r-123", "https://juliusbaer.wd3.myworkdayjobs.com/en-US/External/job/Zurich/Analyst_R-123")
    assert job_from_url("https://example.com/External/job/Zurich/Analyst_R-123") == ("", "")
    assert job_from_url("https://notmyworkdayjobs.com/External/job/Zurich/Analyst_R-123") == ("", "")


# --- several jobs, edge cases built from the real alert ---------------------------------------------------

def test_several_jobs_are_read_in_order():
    r = parse(two_jobs())
    assert r.errors == []
    assert [(j.external_id, j.title, j.location, j.reference) for j in r.jobs] == [
        ("r-20213-1", TITLE, "Geneva", "R-20213"), ("r-20999-2", "Relationship Manager, Zurich (f/m/d)", "Zurich", "R-20999")]
    assert r.jobs[1].url == "https://juliusbaer.wd3.myworkdayjobs.com/External/job/Zurich/Account-Manager--Turkey--100---f-m-d-_r-20999-2"


def test_the_same_job_twice_in_one_mail_is_read_once():
    r = parse(two_jobs(second_id="r-20213-1"))
    assert [j.external_id for j in r.jobs] == ["r-20213-1"] and r.errors == []


def test_missing_place_keeps_the_job_without_a_location():
    r = parse(REAL.replace("<p>Geneva</p>", ""))
    assert r.errors == [] and len(r.jobs) == 1 and r.jobs[0].location == ""
    n = to_job(r.jobs[0])
    assert n.locations == [] and n.title == TITLE


def test_an_empty_place_paragraph_keeps_the_job_without_a_location():
    r = parse(REAL.replace("<p>Geneva</p>", "<p></p>"))
    assert len(r.jobs) == 1 and r.jobs[0].location == ""


def test_a_row_without_a_title_is_skipped_and_reported():
    r = parse(two_jobs().replace(f"<p>{TITLE}</p>", "<p> </p>"))
    assert [j.external_id for j in r.jobs] == ["r-20999-2"]
    assert len(r.errors) == 1 and "job 1" in r.errors[0] and "r-20213-1" in r.errors[0] and "no title" in r.errors[0]


def test_a_link_without_any_paragraph_around_it_is_skipped_and_reported():
    start, end = row_span()
    row = re.sub(r"<p>[^<]*</p>", "", REAL[start:end])
    r = parse(REAL[:start] + row + REAL[end:])
    assert r.jobs == [] and len(r.errors) == 1 and "no title and place" in r.errors[0]


def test_an_id_without_a_requisition_number_has_no_reference():
    j = parse(REAL.replace("_r-20213-1", "_abc-7")).jobs[0]
    assert (j.external_id, j.reference) == ("abc-7", "")


def test_the_id_is_lower_cased():
    j = parse(REAL.replace("_r-20213-1", "_R-20213-1")).jobs[0]
    assert (j.external_id, j.reference) == ("r-20213-1", "R-20213")


def test_the_mail_without_the_utility_classes_is_read_the_same():
    plain = re.sub(r'(?:class|style)="[^"]*"', "", REAL)
    assert [(j.external_id, j.title, j.location) for j in parse(plain).jobs] == [("r-20213-1", TITLE, "Geneva")]


# --- unknown formats and empty results -----------------------------------------------------------------

@pytest.mark.parametrize("html", [
    "<html><body>Your weekly newsletter</body></html>",
    "<html><body><h1>Thank you for your application</h1><p>We will come back to you</p></body></html>",
    "",
])
def test_an_unknown_format_is_a_parse_error(html):
    with pytest.raises(ParseError, match="not a recognised Julius Baer job alert"):
        parse(html)


def test_other_alert_mails_are_not_julius_alerts():
    for name in ("linkedin/alert_2026-11-01.html", "ubs/alert_2026-10-30.html"):
        with pytest.raises(ParseError):
            parse((FIX.parent / name).read_text(encoding="utf-8"))


def test_the_marker_without_jobs_is_an_empty_result_not_an_error():
    r = parse("<html><body><p>Matching Opportunities:</p><p>See all jobs</p></body></html>")
    assert r.jobs == [] and r.errors == []


def test_the_mail_without_the_job_row_but_with_the_intro_is_an_empty_result():
    start, end = row_span()
    r = parse(REAL[:start] + REAL[end:])
    assert r.jobs == [] and r.errors == []


def test_unclosed_and_odd_html_does_not_stop_the_parser():
    r = parse(REAL.replace("</p>", "", 2).replace("</div>", "", 3))
    assert [j.external_id for j in r.jobs] == ["r-20213-1"]


# --- the fixture itself is clean -------------------------------------------------------------------------

def test_fixture_holds_no_personal_data_or_tracking_values():
    for word in ("morgan", "Zzqx", "@gmail", "gmail.com", "Alex", "Exampletown", "Exampletown"):
        assert word.lower() not in REAL.lower(), word
    for value in re.findall(r"shared_id=([^&\"<>\s]*)", REAL):
        assert value == "REDACTED", value
    for secret in ("65676c189c3c10000bbce88642831906", "aa1dd4ba176890009129ce03d5640000", "ee8b01a920e1100105ac4b5165000000",
                   "ai5wc3pjemVsb3dza2lAZ21haWwuY29t", "ZTIzMDgyNDktMDdkMy00NzMyLTk4Y2MtMTc2NWE5OTAyMjgz", "hmac=", "__token__=exp"):
        assert secret not in REAL, secret
    assert "intended for REDACTED" in REAL
    unsubscribe = re.findall(r'href="\s*(https://[^"]*unsubscribe[^"]*)"', REAL)
    assert unsubscribe and all(u.endswith("/REDACTED") for u in unsubscribe), unsubscribe
    public = re.sub(r'(?:class|style)="[^"]*"', "", REAL)
    assert not re.search(r"=[A-Za-z0-9%_+/~-]{20,}", public), "a token-like query value is left in the fixture"


def test_the_fixture_keeps_the_job_title_place_and_path():
    assert f"<p>{TITLE}</p>" in REAL and "<p>Geneva</p>" in REAL and "/External/job/Geneva/Account-Manager--Turkey--100---f-m-d-_r-20213-1?" in REAL
