from pathlib import Path

import pytest

from app.parsers.ubs import ParseError, parse_alert, parse_date

FIX = Path(__file__).parent / "fixtures" / "ubs"
HEADERS = ["Title", "Job Reference #", "Location", "City", "Function Category", "Business Divisions",
           "Job Type", "Business Unit", "Business Sector", "Business Segment", "Corporate title", "Date updated"]


def load(name):
    return parse_alert((FIX / name).read_text(encoding="utf-8"))


def mail(rows, headers=HEADERS, sentence=True):
    """Build a minimal alert: rows are lists of cell texts; the first cell gets a job link if it has 'id|'."""
    out = ["<html><body>"]
    if sentence:
        out.append("These jobs matched the criteria specified for:     Zurich <br> ")
    out.append('<table><tr><td></td></tr><tr><th></th>'
               + "".join(f'<th class="BRANDINGdom2dark"><span>{h}</span></th>' for h in headers) + "</tr>")
    for i, row in enumerate(rows):
        cls = "COLUMNns1" if i % 2 == 0 else "COLUMNsel1"
        cells = []
        for j, text in enumerate(row):
            if "|" in text:
                jobid, title = text.split("|", 1)
                text = f'<a href="https://jobs.ubs.com/1033/asp/tg/cim_jobdetail.asp?partnerid=25008&amp;siteid=5012&amp;jobid={jobid}">{title}</a>'
            cells.append(f'<td class="{cls}" colspan="1">{text} </td>')
        out.append('<tr><td align="center" width="1%"></td>' + "".join(cells) + "<td></td></tr>")
    out.append('</table><a href="http://x/DeleteSavedSearchFromMail?qs=SECRETTOKEN">Click here to delete this Agent.</a></body></html>')
    return "".join(out)


ROW = ["111|Analyst", "1BR", "Switzerland - Zürich", "Zürich", "Fn", "Div", "Full Time", "BU", "BS", "BSeg",
       "AD - Associate Director", "29-Oct-2026"]


# --- the four real alerts -------------------------------------------------------------------

@pytest.mark.parametrize("name, ids", [
    ("alert_2026-10-24.html", ["347642", "352530"]),
    ("alert_2026-10-28.html", ["351249"]),
    ("alert_2026-10-29.html", ["351249", "350859", "351936"]),
    ("alert_2026-10-30.html", ["350099", "350625", "350979", "348531"]),
])
def test_real_alerts_job_ids(name, ids):
    r = load(name)
    assert [j.external_id for j in r.jobs] == ids
    assert r.errors == []
    assert r.criteria == "Zurich"


def test_nine_distinct_jobs_across_the_four_alerts():
    ids = {j.external_id for n in sorted(p.name for p in FIX.glob("*.html")) for j in load(n).jobs}
    assert len(ids) == 9


def test_all_twelve_fields_extracted():
    j = load("alert_2026-10-28.html").jobs[0]
    assert j.external_id == "351249" and j.reference == "343734BR"
    assert j.title.startswith("Business Manager, IB Unified Global Markets Distribution")
    assert j.locations == ["Switzerland - Zürich"] and j.cities == ["Opfikon"]
    assert j.corporate_title == "AD - Associate Director"
    assert j.date_updated == "2026-10-27"
    assert j.function_category and j.business_division and j.job_type == "Full Time"
    assert j.business_unit and j.business_sector and j.business_segment
    assert len(j.raw) == 12


def test_alternating_row_classes_both_parsed():
    # the 30 Oct alert alternates COLUMNns1 / COLUMNsel1; all four rows must come through
    assert len(load("alert_2026-10-30.html").jobs) == 4


def test_repeated_job_has_newer_date_in_later_mail():
    a = load("alert_2026-10-28.html").jobs[0]
    b = load("alert_2026-10-29.html").jobs[0]
    assert a.external_id == b.external_id == "351249"
    assert (a.date_updated, b.date_updated) == ("2026-10-27", "2026-10-28")


def test_multi_city_rows():
    two = load("alert_2026-10-24.html").jobs[1]
    assert two.locations == ["Switzerland - Western Switzerland", "Switzerland - Zürich"]
    assert two.cities == ["Genève", "Zürich"]
    many = load("alert_2026-10-29.html").jobs[1]
    assert "Switzerland - Zürich" in many.locations and len(many.locations) == 8
    assert "Zürich" in many.cities and len(many.cities) == 8


def test_director_titles_are_read_as_written():
    # goal.md 5.3 / config listed "D - Director"; the real alert says "DI - Director"
    titles = {j.corporate_title for j in load("alert_2026-10-24.html").jobs}
    assert titles == {"DI - Director", "ED - Executive Director"}


def test_canonical_url_has_no_extra_parameters_and_no_token():
    for n in FIX.glob("*.html"):
        for j in parse_alert(n.read_text(encoding="utf-8")).jobs:
            assert j.url == f"https://jobs.ubs.com/1033/asp/tg/cim_jobdetail.asp?partnerid=25008&siteid=5012&jobid={j.external_id}"
            assert "REDACTED" not in repr(j) and "qs=" not in repr(j)


# --- synthetic edge cases -------------------------------------------------------------------

def test_delete_agent_link_is_ignored():
    r = parse_alert(mail([ROW]))
    assert len(r.jobs) == 1 and "SECRETTOKEN" not in repr(r)


def test_html_entities_and_special_characters():
    row = ["222|R&amp;D Analyst &ndash; Zürich &amp; Łódź"] + ROW[1:]
    assert parse_alert(mail([row])).jobs[0].title == "R&D Analyst – Zürich & Łódź"


def test_columns_found_by_header_not_position():
    order = [10, 9, 0, 1, 2, 3, 4, 5, 6, 7, 8, 11]
    headers = [HEADERS[i] for i in order]
    row = [ROW[i] for i in order]
    row[2] = "333|Analyst"  # the Title column moved to position 2
    j = parse_alert(mail([row], headers=headers)).jobs[0]
    assert (j.external_id, j.title, j.reference, j.corporate_title) == ("333", "Analyst", "1BR", "AD - Associate Director")


def test_extra_unknown_column_is_tolerated():
    j = parse_alert(mail([ROW + ["extra"]], headers=HEADERS + ["Something New"])).jobs[0]
    assert j.external_id == "111" and j.raw["Something New"] == "extra"


def test_optional_column_missing_gives_empty_value():
    headers = [h for h in HEADERS if h != "Business Segment"]
    row = ROW[:9] + ROW[10:]
    j = parse_alert(mail([row], headers=headers)).jobs[0]
    assert j.business_segment == "" and j.corporate_title == "AD - Associate Director"


def test_short_row_is_skipped_and_reported_others_kept():
    r = parse_alert(mail([ROW, ["444|Broken", "2BR"], ["555|Fine"] + ROW[1:]]))
    assert [j.external_id for j in r.jobs] == ["111", "555"]
    assert len(r.errors) == 1 and "row 2" in r.errors[0]


def test_row_without_jobid_is_skipped_and_reported():
    r = parse_alert(mail([["Plain title, no link"] + ROW[1:], ROW]))
    assert [j.external_id for j in r.jobs] == ["111"]
    assert "no jobid" in r.errors[0]


def test_non_numeric_jobid_is_rejected():
    r = parse_alert(mail([["abc|Analyst"] + ROW[1:]]))
    assert r.jobs == [] and len(r.errors) == 1


def test_locations_and_cities_deduplicated_and_trimmed():
    row = list(ROW)
    row[2], row[3] = " Switzerland - Zürich , Switzerland - Zürich ,, Germany ", "Zürich , Zürich"
    j = parse_alert(mail([row])).jobs[0]
    assert j.locations == ["Switzerland - Zürich", "Germany"] and j.cities == ["Zürich"]


def test_zero_jobs_with_table_header_is_empty_result():
    r = parse_alert(mail([]))
    assert r.jobs == [] and r.errors == [] and r.criteria == "Zurich"


def test_sentence_without_table_is_empty_result():
    r = parse_alert("<html><body>These jobs matched the criteria specified for: Zurich <br></body></html>")
    assert r.jobs == [] and r.criteria == "Zurich"


@pytest.mark.parametrize("text", [
    "",
    "not html at all",
    "<html><body><p>Your weekly newsletter</p></body></html>",
    "<html><body><table><tr><td>a</td></tr></table></body></html>",
])
def test_unrecognised_mail_is_an_error_not_zero_jobs(text):
    with pytest.raises(ParseError):
        parse_alert(text)


def test_missing_title_column_is_an_error():
    with pytest.raises(ParseError, match="title"):
        parse_alert(mail([], headers=[h for h in HEADERS if h != "Title"]))


def test_row_class_change_means_rows_are_not_found():
    # if UBS renamed the row class away from COLUMN*, we find a header but no rows: not an error here,
    # so the ingestion step must treat "table with header, 0 jobs, several days in a row" as an alert
    html = mail([ROW]).replace("COLUMNns1", "ROWX")
    assert parse_alert(html).jobs == []


@pytest.mark.parametrize("value, iso", [
    ("29-Oct-2026", "2026-10-29"), ("1-Jan-2027", "2027-01-01"), ("01-DEC-2026", "2026-12-01"),
    ("", ""), ("29/10/2026", ""), ("32-Foo-2026", ""), ("Oct 29 2026", ""),
])
def test_parse_date(value, iso):
    assert parse_date(value) == iso
