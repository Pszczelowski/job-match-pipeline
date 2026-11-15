"""The LinkedIn job-alert parser (goal.md 5.1, v0.38.0), on the sanitised copy of the one real alert (1 Nov 2026)."""
import re
from pathlib import Path

import pytest

from app.dedup import canonicalize_url
from app.parsers.linkedin import JOB_URL, SOURCE, ParseError, job_id_from_url, parse_alert, to_job

FIX = Path(__file__).parent / "fixtures" / "linkedin"
REAL = (FIX / "alert_2026-11-01.html").read_text(encoding="utf-8")

# (job id, title, company, location, work mode, badges) of the five cards of the real alert, in mail order
REAL_JOBS = [
    ("4441286658", "Business Transformation Specialist - Data & Analytics", "Deutsche Bank", "Zurich", "Hybrid", ["Actively recruiting"]),
    ("4477598320", "Product Owner Vorsorgelösungen (a) – befristet für 2 Jahre – 80% - 100% – Aarau", "Aargauische Kantonalbank", "Aarau", "", []),
    ("4475607540", "Senior IT-Business Analyst", "yellowshark® AG", "Zurich", "", ["Actively recruiting"]),
    ("4485175120", "ERP Application Manager (m/w/d)", "Rocken®", "Effretikon", "", ["Actively recruiting"]),
    ("4483959119", "Oracle Business Consultant / Berater für Digitalisierte Geschäftsprozesse (m/w/d)", "PROMATIS",
     "Wallisellen / Hof", "On-site", ["Easy Apply"]),
]
DB_LINE = "Deutsche Bank &middot; Zurich (Hybrid)"
AKB_LINE = "Aargauische Kantonalbank &middot; Aarau"


def parse(html=REAL):
    return parse_alert(html)


def card_span(html, n):
    """(start, end) of the n-th job card (a table row), n < 4; the end is the start of the next card."""
    marks = [m.start() for m in re.finditer('data-test-id="job-card"', html)]
    return html.rfind("<tr", 0, marks[n]), html.rfind("<tr", 0, marks[n + 1])


# --- the real alert -----------------------------------------------------------------------------------

def test_real_alert_gives_the_five_jobs_in_order():
    r = parse()
    assert r.errors == [] and r.criteria == "example search"
    got = [(j.external_id, j.title, j.company, j.location, j.work_mode, j.badges) for j in r.jobs]
    assert got == REAL_JOBS


def test_job_urls_are_rebuilt_from_the_job_id_without_tracking():
    for j in parse().jobs:
        assert j.url == JOB_URL.format(jobid=j.external_id) == f"https://www.linkedin.com/jobs/view/{j.external_id}/"
        assert "?" not in j.url and "REDACTED" not in j.url and "comm" not in j.url


def test_the_tracking_links_in_the_mail_are_not_used():
    # the mail's own job links carry trackingId, refId, lipi, midToken, otpToken ...; none of them reaches the job
    assert "trackingId=" in REAL and "otpToken=" in REAL
    blob = repr(parse())
    assert "trackingId" not in blob and "otpToken" not in blob and "lipi" not in blob


def test_work_mode_stays_out_of_the_location():
    jobs = {j.external_id: j for j in parse().jobs}
    assert jobs["4441286658"].location == "Zurich" and jobs["4441286658"].work_mode == "Hybrid"
    assert jobs["4483959119"].location == "Wallisellen / Hof" and jobs["4483959119"].work_mode == "On-site"


def test_to_job_is_a_normalized_job_without_a_date_or_description():
    job = parse().jobs[0]
    n = to_job(job, message_id="<m1@x>")
    assert (n.source, n.external_id, n.title, n.company) == (SOURCE, "4441286658", job.title, "Deutsche Bank")
    assert n.url == "https://www.linkedin.com/jobs/view/4441286658/" and n.locations == ["Zurich"]
    assert n.source_date == "" and n.description == "" and n.reference == "" and n.message_id == "<m1@x>"
    assert n.raw["work_mode"] == "Hybrid" and n.raw["badges"] == ["Actively recruiting"]


def test_a_card_without_location_gives_no_locations():
    n = to_job(parse(REAL.replace(AKB_LINE, "Aargauische Kantonalbank")).jobs[1])
    assert n.locations == [] and n.company == "Aargauische Kantonalbank"


def test_the_canonical_url_of_dedup_matches_for_a_stable_key():
    assert canonicalize_url(to_job(parse().jobs[0]).url) == "https://www.linkedin.com/jobs/view/4441286658"


# --- edge cases built from the real alert ------------------------------------------------------------

def test_missing_location_keeps_the_job():
    r = parse(REAL.replace(AKB_LINE, "Aargauische Kantonalbank"))
    assert r.errors == [] and len(r.jobs) == 5
    akb = r.jobs[1]
    assert (akb.company, akb.location, akb.work_mode) == ("Aargauische Kantonalbank", "", "")


def test_missing_company_skips_the_card_and_reports_it():
    r = parse(REAL.replace(AKB_LINE, "&middot; Aarau"))
    assert [j.external_id for j in r.jobs] == ["4441286658", "4475607540", "4485175120", "4483959119"]
    assert len(r.errors) == 1 and "card 2" in r.errors[0] and "4477598320" in r.errors[0] and "no company" in r.errors[0]


def test_a_card_whose_first_line_is_only_a_badge_has_no_company():
    r = parse(REAL.replace(AKB_LINE, "Easy Apply"))
    assert len(r.jobs) == 4 and "no company line" in r.errors[0]


def test_a_card_without_any_line_is_skipped():
    r = parse(re.sub(r"<p [^>]*>\s*Rocken® &middot; Effretikon\s*</p>", "", REAL))
    assert "4485175120" not in [j.external_id for j in r.jobs] and len(r.errors) == 1


def test_a_card_without_a_title_is_skipped():
    title = "ERP Application Manager (m/w/d)"
    r = parse(REAL.replace(title, " "))
    assert len(r.jobs) == 4 and "no title" in r.errors[0] and "4485175120" in r.errors[0]


def test_a_card_without_a_job_link_is_skipped():
    start, end = card_span(REAL, 3)
    r = parse(REAL[:start] + REAL[start:end].replace("/jobs/view/", "/company/") + REAL[end:])
    assert [j.external_id for j in r.jobs] == ["4441286658", "4477598320", "4475607540", "4483959119"]
    assert len(r.errors) == 1 and "card 4: no link to a job posting" in r.errors[0]


def test_extra_rows_are_all_read():
    start, end = card_span(REAL, 0)
    extra = REAL[start:end].replace("4441286658", "4400000001").replace("Business Transformation Specialist - Data &amp; Analytics", "Extra Analyst")
    r = parse(REAL[:end] + extra + REAL[end:])
    assert len(r.jobs) == 6 and r.errors == []
    assert (r.jobs[1].external_id, r.jobs[1].title, r.jobs[1].company) == ("4400000001", "Extra Analyst", "Deutsche Bank")


def test_the_same_job_twice_in_one_mail_is_read_once():
    start, end = card_span(REAL, 0)
    r = parse(REAL[:end] + REAL[start:end] + REAL[end:])
    assert [j.external_id for j in r.jobs] == [x[0] for x in REAL_JOBS] and r.errors == []


def test_a_slug_link_and_other_hosts():
    assert job_id_from_url("https://www.linkedin.com/jobs/view/senior-analyst-at-ubs-4441286658?trk=x") == "4441286658"
    assert job_id_from_url("https://ch.linkedin.com/comm/jobs/view/4441286658/?trackingId=a%3D%3D") == "4441286658"
    assert job_id_from_url("https://www.linkedin.com/jobs/search/?currentJobId=4441286658") == "4441286658"
    assert job_id_from_url("https://example.com/jobs/view/4441286658/") == ""
    assert job_id_from_url("https://notlinkedin.com/jobs/view/4441286658/") == ""
    assert job_id_from_url("https://www.linkedin.com/jobs/view/123/") == ""            # not a job id (too short)
    assert job_id_from_url("https://www.linkedin.com/comm/feed/") == "" and job_id_from_url("") == ""


@pytest.mark.parametrize("mode, expected", [("Remote", "Remote"), ("On-site", "On-site"), ("Vor Ort", "On-site"), ("hybrid", "Hybrid")])
def test_work_modes(mode, expected):
    j = parse(REAL.replace(DB_LINE, f"Deutsche Bank &middot; Zurich ({mode})")).jobs[0]
    assert (j.location, j.work_mode) == ("Zurich", expected)


def test_a_bracket_that_is_not_a_work_mode_stays_in_the_location():
    j = parse(REAL.replace(DB_LINE, "Deutsche Bank &middot; Zurich (ZH)")).jobs[0]
    assert (j.location, j.work_mode) == ("Zurich (ZH)", "")


def test_a_comma_separated_location_is_one_entry():
    j = parse(REAL.replace(DB_LINE, "Deutsche Bank &middot; Zürich, Zürich, Switzerland (Hybrid)")).jobs[0]
    assert j.location == "Zürich, Zürich, Switzerland" and to_job(j).locations == ["Zürich, Zürich, Switzerland"]


# --- unknown formats and empty results -----------------------------------------------------------------

@pytest.mark.parametrize("html", [
    "<html><body>Your weekly newsletter</body></html>",
    "<html><body><h1>You have a new invitation</h1><p>Someone wants to connect</p></body></html>",
    "",
])
def test_an_unknown_format_is_a_parse_error(html):
    with pytest.raises(ParseError, match="not a recognised LinkedIn job alert"):
        parse(html)


def test_the_ubs_alert_is_not_a_linkedin_alert():
    ubs = (Path(__file__).parent / "fixtures" / "ubs" / "alert_2026-10-30.html").read_text(encoding="utf-8")
    with pytest.raises(ParseError):
        parse(ubs)


def test_the_heading_without_cards_is_an_empty_result_not_an_error():
    html = ('<html><body><table><tr><td><h1 data-test-id="email-header"><a href="https://www.linkedin.com/comm/jobs/search-results/">'
            'Your job alert for <strong>requirements analyst</strong></a></h1></td></tr></table></body></html>')
    r = parse(html)
    assert r.jobs == [] and r.errors == [] and r.criteria == "requirements analyst"


def test_cards_without_the_heading_are_still_read():
    r = parse(REAL.replace('data-test-id="email-header"', 'data-test-id="something-else"'))
    assert len(r.jobs) == 5 and r.criteria == ""


def test_unclosed_and_odd_html_does_not_stop_the_parser():
    r = parse(REAL.replace("</p>", "", 3).replace("<br>", ""))
    assert len(r.jobs) >= 3


# --- the fixture itself is clean -------------------------------------------------------------------------

def test_fixture_holds_no_personal_data_or_tracking_values():
    for word in ("Alex", "Zzqx", "zzqx", "gmail.com", "Exampletown", "Exampletown"):
        assert word not in REAL, word
    assert "requirements" not in REAL.lower()                     # the saved search is the user's own: its words are dropped
    tracking = ("trackingId", "refId", "lipi", "midToken", "midSig", "trk", "trkEmail", "eid", "otpToken", "f_TPR",
                "keywords", "geoId", "distance")
    for key, value in re.findall(r"[?&;]([A-Za-z_]+)=([^&\"<>\s]*)", REAL.replace("&amp;", "&")):
        if key in tracking:
            assert value == "REDACTED", (key, value)
    public = re.sub(r'(?:class|style)="[^"]*"|origin=[^&"]*', "", REAL)
    assert not re.search(r"=[A-Za-z0-9%_+/~-]{20,}", public), "a token-like query value is left in the fixture"
