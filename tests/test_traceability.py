"""Traceability check (goal.md 5.6, app/traceability.py). Offline; the real fact file is read-only input.

Every injected fake fact must be caught by its own check: removing the check makes its test fail
(how this was verified is recorded in docs/TEST_CASES.md, T-TRC-*)."""
import json
from decimal import Decimal

import pytest
import yaml

from app import traceability as tr
from app.config import ROOT
from tests.test_cli import clean_logger, config_file, run      # noqa: F401  (fixtures and the runner)

MASTER = yaml.safe_load((ROOT / "profile" / "master_cv.yaml").read_text(encoding="utf-8"))
VARIANTS = yaml.safe_load((ROOT / "profile" / "variants.yaml").read_text(encoding="utf-8"))
FACTS = tr.load_facts()


def bullets():
    for exp in MASTER["experience"]:
        for b in exp["bullets"]:
            yield b


def text_of(fact_id: str, key: str | None = None) -> str:
    for b in bullets():
        if b["id"] == fact_id:
            return b["texts"][key] if key else next(iter(b["texts"].values()))
    raise KeyError(fact_id)


def check(*blocks, title=None, company=None, facts=FACTS):
    doc = tr.Document([tr.Block(b) if isinstance(b, str) else tr.Block(*b) for b in blocks], title)
    return tr.check_document(doc, facts, allowed_employers=[company] if company else [])


def kinds(result):
    return sorted({f.kind for f in result.findings})


# ----------------------------------------------------------------------------- clean documents pass

def test_every_real_bullet_text_passes_on_its_own():
    failures = []
    for b in bullets():
        for key, text in b["texts"].items():
            r = check((text, (b["id"],)))
            if not r.ok:
                failures.append((b["id"], key, [(f.kind, f.text) for f in r.findings]))
    assert failures == []


def test_a_document_built_from_real_bullets_passes():
    letter = [text_of("exp-northwind-zurich-automation-letter"), text_of("exp-northwind-zurich-swift-savings-letter"),
              text_of("exp-contoso-testing-letter"), text_of("exp-litware-reporting"),
              text_of("exp-northwind-wroclaw-realestate", "resume_cs"), text_of("exp-career-transition-sql-cert", "resume_cs")]
    r = check(*[(t, ()) for t in letter], title="Senior Business Analyst at Acme AG", company="Acme AG")
    assert r.ok, r.render_text()


def test_the_three_cover_letter_facts_pass_with_their_numbers():
    for fid in ("exp-northwind-zurich-automation-letter", "exp-northwind-zurich-swift-savings-letter", "exp-contoso-testing-letter"):
        assert check((text_of(fid), (fid,))).ok
    assert "200,000" in text_of("exp-northwind-zurich-swift-savings-letter")          # the figure the fake tests change


def test_variant_summaries_and_headline_pass():
    """Overseer note 1: 'over 10 years', the employer list and the headline of variants.yaml are fact sources."""
    for v in VARIANTS["variants"].values():
        for key in ("summary", "headline"):
            if v.get(key):
                r = check(" ".join(v[key].split()))
                assert r.ok, r.render_text()


def test_ten_years_as_digits_or_words():
    assert check("With over 10 years in financial services.").ok
    assert check("With ten years in financial services.").ok
    assert not check("With 12 years in financial services.").ok
    assert kinds(check("With twelve years in financial services.")) == ["number"]


# ----------------------------------------------------------------------------- one test per kind of fake fact

def test_invented_employer_is_caught():
    r = check("Developed scripts at Acme Robotics AG, saving over 500 manual hours annually.")
    assert kinds(r) == ["employer"] and r.findings[0].text == "Acme Robotics AG"
    assert kinds(check("Later I joined Globex Holding.")) == ["employer"]
    assert "employer" in kinds(check("My time with Goldman Sachs taught me a lot."))          # recognised by name, no cue word


def test_invented_title_is_caught():
    r = check("Worked as a Lead Data Scientist at Northwind.")
    assert kinds(r) == ["title"] and r.findings[0].text == "Lead Data Scientist"
    r = check("Spent two years as Chief Technology Officer.")
    assert "title" in kinds(r)
    assert "title" in kinds(check("Senior Business Analyst at Northwind since the start."))          # real title, wrong employer
    assert kinds(check("Worked as a Senior Business Analyst at Litware Limited for Adventure Works Poland.")) == []


def test_changed_number_is_caught():
    amount = text_of("exp-northwind-zurich-swift-savings-letter").replace("200,000", "250,000")
    r = check(amount)
    assert kinds(r) == ["number"] and r.findings[0].text == "250,000 CHF"
    pct = text_of("exp-northwind-zurich-automation-letter").replace("20%", "30%")
    r = check(pct)
    assert kinds(r) == ["number"] and r.findings[0].text == "30%"
    hours = text_of("exp-northwind-zurich-automation").replace("500", "700")
    assert kinds(check(hours)) == ["number"]


def test_a_changed_currency_or_unit_class_is_caught():
    assert kinds(check("Saved over 200,000 USD annually at Northwind.")) == ["number"]          # fact says CHF
    assert kinds(check("Test accuracy of 50% at Contoso Fund Services.")) == ["number"]


def test_invented_year_and_date_are_caught():
    r = check("Worked at Northwind in Zurich from 2019 to 2023.")
    assert kinds(r) == ["date"] and r.findings[0].text == "2019"
    assert kinds(check("Business Analyst at Contoso Fund Services from Sept. 2023.")) == ["date"]        # start is 2023-08
    assert kinds(check("Graduated in 1999.")) == ["date"]
    assert kinds(check("Worked at Northwind 2021-23.")) == []                                                     # 2021 and 2023 both exist


def test_invented_skill_is_caught():
    r = check("Built backend services in Kotlin and Python at Northwind.")
    assert kinds(r) == ["skill"] and [f.text for f in r.findings] == ["Kotlin"]
    assert kinds(check("Experienced with TensorFlow pipelines.")) == ["skill"]               # unknown camel-case product
    assert kinds(check("Strong KYC and AML background.")) == ["skill"]                       # unknown acronyms
    assert check("Python, VBA, Tableau, Power BI, Jira, Xray and SQL.").ok


def test_invented_certificate_is_caught():
    assert kinds(check("I hold a PMP certification.")) == ["certificate"]
    r = check("Holds the AWS Certified Solutions Architect credential.")
    assert "certificate" in kinds(r)
    assert kinds(check("Passed exam 1Z0-999 last year.")) == ["certificate"]
    assert "certificate" in kinds(check("I am certified."))                                  # a claim with no name


def test_oracle_certificate_in_preparation_is_not_an_obtained_certificate():
    """Overseer note 6: Oracle 1Z0-071 is in preparation only."""
    ok = check(text_of("exp-career-transition-sql-cert"))
    assert ok.ok, ok.render_text()
    assert check("Currently preparing for the Oracle Database SQL Certified Associate exam (1Z0-071).").ok
    for claim in ("Oracle Certified Associate in SQL.", "I am certified in Oracle SQL.",
                  "Oracle Database SQL Certified Associate (1Z0-071).", "Holder of the Oracle 1Z0-071 certificate."):
        assert "certificate" in kinds(check(claim)), claim


def test_unknown_fact_id_is_caught():
    r = check(("A real sentence from the file, citing a made-up id.", ("exp-northwind-zurich-automation", "exp-made-up")))
    assert kinds(r) == ["unknown_fact_id"] and r.findings[0].text == "exp-made-up"
    r = check("Saved hours [exp-nowhere-else] at Northwind.")                                      # inline citation
    assert kinds(r) == ["unknown_fact_id"]
    assert check("Built Python scripts at Northwind [exp-northwind-zurich-automation].").ok


# ----------------------------------------------------------------------------- the employer being applied to

def test_target_employer_is_allowed_and_another_is_not():
    letter = "I would bring this to Helvetia Versicherungen AG, where Acme Bank Holding is a partner."
    assert kinds(check("I would be proud to join Acme Bank AG as your analyst.", company="Acme Bank AG")) == []
    assert kinds(check("I would be proud to join Acme Bank AG as your analyst.")) == ["employer"]
    assert kinds(check("I would be proud to join Acme Bank AG.", company="Other Bank AG")) == ["employer"]
    r = check(letter, company="Helvetia Versicherungen AG")
    assert [f.text for f in r.findings] == ["Acme Bank Holding"]
    r = check("Developer at Acme", title="Senior Business Analyst at Acme AG", company="Acme AG")
    assert "employer" not in kinds(r)


def test_document_company_field_and_legal_suffixes():
    doc = tr.Document([tr.Block("I look forward to working at SIX.")], company="SIX Group AG")
    assert tr.check_document(doc, FACTS).ok
    assert not tr.check_document(tr.Document(doc.blocks), FACTS).ok


def test_known_aliases_of_real_employers():
    """Overseer note 2."""
    text = ("Worked at Contoso, at Litware and with Adventure Works; earlier at Fabrikam Group and Northwind AG, "
            "and at Litware Limited for Adventure Works Poland.")
    assert check(text).ok
    assert "employer" in kinds(check("At CS I coordinated hiring reviews."))      # no "CS" alias on purpose: spell it out


# ----------------------------------------------------------------------------- normalisation

@pytest.mark.parametrize("form", ["200,000 CHF", "CHF 200,000", "200'000 CHF", "200 000 CHF", "CHF 200k", "200k CHF",
                                  "CHF 0.2 million", "200.000 CHF", "chf 200,000", "200000 CHF"])
def test_amount_forms_equal_the_fact(form):
    assert check(f"Saved over {form} annually at Northwind.").ok


@pytest.mark.parametrize("form", ["250,000 CHF", "CHF 20,000", "200,000 EUR", "2,000,000 CHF"])
def test_amount_forms_that_differ_are_caught(form):
    assert kinds(check(f"Saved over {form} annually at Northwind.")) == ["number"]


@pytest.mark.parametrize("form", ["20%", "20 %", "20 percent", "20 per cent"])
def test_percent_forms_equal_the_fact(form):
    assert check(f"Raised team productivity by {form} at Northwind.").ok


@pytest.mark.parametrize("form", ["$100,000+", "100,000 USD", "USD 100k", "$100k", "100 000 dollars"])
def test_dollar_amount_forms(form):
    assert check(f"Established reporting standards, saving {form} annually.").ok


@pytest.mark.parametrize("form", ["5 billion USD", "USD 5 billion", "$5bn", "5,000,000,000 USD"])
def test_billion_forms(form):
    assert check(f"Potential savings of over {form} across more than 100 buildings.").ok


def test_ranges_inherit_the_unit_and_each_end_is_checked():
    assert check("Accuracy between 90-95% at Contoso.").findings[0].text.startswith("95")
    assert check("Accuracy of 90% at Contoso.").ok
    assert kinds(check("Between 40-50% faster.")) == ["number"]


@pytest.mark.parametrize("form", ["Aug. 2023", "August 2023", "Aug 2023", "08/2023", "8/2023", "2023-08", "1 August 2023", "2023"])
def test_date_forms_equal_the_fact(form):
    """Overseer note 5: the fact file says 2023-08."""
    assert check(f"Started at Contoso Fund Services in {form}.").ok


@pytest.mark.parametrize("form", ["Sep 2023", "09/2023", "2023-09", "2024", "2022"])
def test_date_forms_that_differ(form):
    r = check(f"Started at Contoso Fund Services in {form}.")
    # 2024 and 2022 are not start or end years of any role (2022 is not), 2023 is
    assert kinds(r) == ["date"], form


def test_present_is_valid_and_not_a_date():
    assert check("Since July 2025 I have been in a career transition, 2025 to present.").ok


def test_accents_and_polish_letters_fold():
    assert tr.fold("Zürich Wrocław Morgan Ångström") == "zurich wroclaw morgan angstrom"
    assert check("Based in Zürich; earlier at Fabrikam in Wrocław and Wroclaw.").ok
    assert tr.words("Zürich Straße") == ("zurich", "strasse")
    assert check("Worked at Contoso FUND SERVICES.").ok               # case of the name does not matter


def test_source_notes_are_stripped_from_the_fact_sets():
    """Overseer note 3."""
    assert tr.strip_source_notes("SQL Server Reporting Services (resume_cs only)") == "SQL Server Reporting Services"
    assert tr.strip_source_notes("90% test accuracy (resume only)") == "90% test accuracy"
    assert tr.strip_source_notes("AI prompting (resume)") == "AI prompting"
    assert Decimal(90) in FACTS.pct
    assert check("Used SQL Server Reporting Services and AI prompting and workflows.").ok


def test_numbers_are_read_from_texts_as_well_as_from_the_numbers_field():
    """Overseer note 4: a figure that only the bullet text carries is still a fact."""
    master = {"experience": [{"id": "e", "title": "Analyst", "employer": "Acme", "start": "2020-01", "end": "2021-01", "bullets": [
        {"id": "b", "skills": [], "texts": {"resume": "Cut the backlog by 37% across 12 teams."}, "numbers": []}]}]}
    facts = tr.Facts(master, None, tr.load_vocab())
    assert check("Cut the backlog by 37% across 12 teams at Acme.", facts=facts).ok
    assert kinds(check("Cut the backlog by 38% across 12 teams at Acme.", facts=facts)) == ["number"]


def test_figures_in_free_text_numbers_field_are_parsed():
    values = {n.value for text in MASTER_NUMBER_STRINGS for n in tr.extract_figures(text)[0]}
    assert {Decimal(90), Decimal(50), Decimal(200000), Decimal(100000), Decimal(5 * 10 ** 9), Decimal(20), Decimal(500)} <= values


MASTER_NUMBER_STRINGS = [n for b in bullets() for n in b["numbers"]]


def test_skill_alternatives_with_slash_are_split():
    # "AI prompting (resume) / AI prompting and workflows (resume_cs)" gives both alternatives
    assert "ai prompting and workflows" in FACTS.corpus


def test_code_tokens_and_list_numbers_are_not_numbers():
    r = check("1. Preparing for Oracle Database SQL certification (1Z0-071).")
    assert r.ok, r.render_text()


def test_contact_literals_are_ignored_but_other_numbers_are_not():
    phone = MASTER["person"]["phone"]
    assert check(f"Reach me on {phone} or at {MASTER['person']['email']}.").ok
    assert kinds(check("Reach me on +41 79 111 22 33.")) == ["number"]


# ----------------------------------------------------------------------------- documents and findings

def test_findings_name_the_block_and_the_text():
    r = check("A clean sentence about Python scripts at Northwind.", "Then Kotlin appeared.", title="Business Analyst")
    assert [(f.kind, f.text, f.block) for f in r.findings] == [("skill", "Kotlin", 1)]
    assert "Kotlin" in r.render_text() and "block 2" in r.render_text()
    t = check("Fine.", title="Business Analyst at Acme Robotics AG")
    assert t.findings[0].block == -1 and "title line" in t.render_text()


def test_document_parsers():
    doc = tr.document_from_json({"title": "T", "company": "X", "blocks": ["a", {"text": "b", "facts": ["f1"]}]})
    assert doc.title == "T" and doc.company == "X" and doc.blocks[1].facts == ("f1",)
    with pytest.raises(tr.TraceabilityError):
        tr.document_from_json({"blocks": [3]})
    with pytest.raises(tr.TraceabilityError):
        tr.document_from_json([])
    d = tr.document_from_text("Title: Analyst\n\nFirst line\nsecond line\n- bullet one\n- bullet two\n\nLast")
    assert d.title == "Analyst" and [b.text for b in d.blocks] == ["First line second line", "bullet one", "bullet two", "Last"]


def test_load_facts_rejects_a_wrong_file(tmp_path):
    bad = tmp_path / "x.yaml"
    bad.write_text("hello: 1\n", encoding="utf-8")
    with pytest.raises(tr.TraceabilityError):
        tr.load_facts(bad)
    with pytest.raises(tr.TraceabilityError):
        tr.load_facts(tmp_path / "missing.yaml")


def test_vocabulary_is_data_and_extendable(tmp_path):
    vocab = tr.load_vocab()
    vocab["skills_extra"] = vocab["skills_extra"] + ["Fortran"]
    facts = tr.Facts(MASTER, VARIANTS, vocab)
    assert kinds(check("Wrote Fortran at Northwind.", facts=facts)) == ["skill"]


# ----------------------------------------------------------------------------- command line

def doc_file(tmp_path, blocks, **extra):
    p = tmp_path / "doc.json"
    p.write_text(json.dumps({"blocks": blocks, **extra}), encoding="utf-8")
    return p


def test_cli_exit_codes(config_file, tmp_path, capsys):
    good = doc_file(tmp_path, [{"text": text_of("exp-northwind-zurich-automation-letter"), "facts": ["exp-northwind-zurich-automation-letter"]}])
    assert run(config_file, "check-document", str(good)) == 0
    assert "passed" in capsys.readouterr().out
    bad = doc_file(tmp_path, [text_of("exp-northwind-zurich-swift-savings-letter").replace("200,000", "250,000")])
    assert run(config_file, "check-document", str(bad)) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "250,000 CHF" in out
    assert run(config_file, "check-document", str(tmp_path / "missing.json")) == 1
    assert "ERROR" in capsys.readouterr().err
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert run(config_file, "check-document", str(broken)) == 1


def test_cli_company_flag_json_output_and_plain_text(config_file, tmp_path, capsys):
    txt = tmp_path / "letter.txt"
    txt.write_text("Title: Business Analyst at Acme Bank AG\n\nI would be glad to join Acme Bank AG.\n", encoding="utf-8")
    assert run(config_file, "check-document", str(txt)) == 1
    capsys.readouterr()
    assert run(config_file, "check-document", str(txt), "--company", "Acme Bank AG") == 0
    capsys.readouterr()
    assert run(config_file, "check-document", str(txt), "--json") == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["findings"][0]["kind"] == "employer"
    j = doc_file(tmp_path, ["I would be glad to join Acme Bank AG."], company="Acme Bank AG")
    assert run(config_file, "check-document", str(j)) == 0


def test_cli_does_not_touch_the_database(config_file, tmp_path):
    good = doc_file(tmp_path, [text_of("exp-litware-workshops")])
    assert run(config_file, "check-document", str(good)) == 0
    assert not (tmp_path / "jobs.db").exists()
    assert not (tmp_path / "logs").exists()


# ----------------------------------------------------------------------------- review follow-up: name boundary, employer-specific titles

@pytest.mark.parametrize("sentence", [
    "At Northwind I built Python and VBA scripts.",
    "At Fabrikam I coordinated the weekly hiring review.",
    "At Contoso Fund Services, I wrote test cases in Xray.",
    "At Contoso We tested the Fund Platform.",
    "At Contoso Fund Services We tested the Fund Platform.",
    "I worked at Northwind. In my role I used Tableau.",
    "At Litware Limited for Adventure Works Poland I ran workshops.",
    "At Northwind My team used Confluence.",
])
def test_employer_name_stops_before_the_next_clause(sentence):
    r = check(sentence)
    assert "employer" not in kinds(r), r.render_text()


def test_the_name_boundary_does_not_hide_an_unknown_employer():
    assert [f.text for f in check("At Acme Robotics I built Python scripts.").findings if f.kind == "employer"] == ["Acme Robotics"]
    assert [f.text for f in check("At Fake Capital Partners We grew.").findings if f.kind == "employer"] == ["Fake Capital Partners"]


def test_the_reviewers_example_is_fully_caught():
    text = "As a Senior Business Analyst at Northwind I saved over 300,000 CHF annually using Kotlin."
    r = check(text, company="Example Bank")
    assert [(f.kind, f.text) for f in r.findings] == [("number", "300,000 CHF"), ("title", "Senior Business Analyst"), ("skill", "Kotlin")]


def test_a_title_is_checked_against_the_employer_it_is_claimed_at():
    # Senior Business Analyst is a title at Litware only; Business Analyst is a title at Northwind and Contoso
    assert "title" in kinds(check("As a Senior Business Analyst at Northwind I led workshops."))
    assert "title" in kinds(check("As a Senior Business Analyst at Contoso Fund Services I led workshops."))
    assert check("As a Senior Business Analyst at Litware Limited for Adventure Works Poland I led workshops.").ok
    assert check("As a Business Analyst at Northwind I used Tableau.").ok
    assert check("As a Business Analyst at Contoso Fund Services I wrote test cases.").ok
    assert check("As an Automation Developer at Northwind I wrote Python scripts.").ok
    assert "title" in kinds(check("As an Automation Developer at Contoso Fund Services I wrote Python scripts."))


# ----------------------------------------------------------------------------- review follow-up: letter header and salutation

LETTER = ["Dear Ms McDonald,", "I am applying for the position of Senior Business Analyst at Example Bank AG.",
          text_of("exp-northwind-zurich-swift-savings-letter")]
HEADER = ["1 November 2026", "Example Bank AG", "8001 Zürich"]


def letter(blocks=None, **kw):
    doc = tr.Document([tr.Block(b) for b in (HEADER + LETTER if blocks is None else blocks)], "Senior Business Analyst at Example Bank AG",
                      **kw)
    return tr.check_document(doc, FACTS, allowed_employers=["Example Bank AG"])


def test_the_generated_date_is_allowed_in_the_usual_written_forms():
    for form in ("1 November 2026", "1st November 2026", "November 1, 2026", "Nov. 1, 2026", "01.11.2026", "1.11.2026",
                 "2026-11-01", "11/2026", "November 2026", "2026"):
        r = letter([form, text_of("exp-litware-workshops")], generated_on="2026-11-01")
        assert r.ok, (form, r.render_text())


def test_the_generated_date_does_not_open_the_door_to_other_dates():
    assert letter(["1 November 2026"]).findings                                           # without generated_on it is a finding
    for other in ("1 November 2025", "2024", "December 2026", "12/2026", "2027", "02.11.2025"):
        r = letter([other], generated_on="2026-11-01")
        assert kinds(r) == ["date"], (other, r.render_text())
    assert kinds(letter(["Worked at Northwind from 2019."], generated_on="2026-11-01")) == ["date"]


def test_generated_on_as_a_date_object_and_a_bad_value():
    import datetime
    assert letter(["1 November 2026"], generated_on=datetime.date(2026, 11, 1)).ok
    with pytest.raises(tr.TraceabilityError):
        tr.parse_date("1 November 2026")


def test_the_employer_location_is_allowed_and_only_that_one():
    assert letter(["Example Bank AG", "8001 Zürich"], employer_location="8001 Zürich").ok
    assert kinds(letter(["Example Bank AG", "8001 Zürich"])) == ["number"]
    doc = tr.Document([tr.Block("Example Bank AG"), tr.Block("Bahnhofplatz 12"), tr.Block("9000 St. Gallen")])
    r = tr.check_document(doc, FACTS, allowed_employers=["Example Bank AG"], employer_location="Bahnhofplatz 12, 9000 St. Gallen")
    assert r.ok, r.render_text()
    r = tr.check_document(doc, FACTS, allowed_employers=["Example Bank AG"], employer_location="Bahnhofplatz 12")
    assert kinds(r) == ["number"] and r.findings[0].text == "9000"
    assert kinds(tr.check_document(tr.Document([tr.Block("I would work at Winterthur Capital Partners.")]), FACTS,
                                   employer_location="Winterthur")) == ["employer"]            # a place name does not allow a company


def test_the_contact_name_is_allowed_in_the_salutation():
    r = letter(["Dear Dr McDonald,", text_of("exp-litware-workshops")], contact="Dr McDonald")
    assert r.ok, r.render_text()
    assert "skill" in kinds(letter(["Dear Dr McDonald,"]))                               # camel-case name looks like a product
    assert "skill" in kinds(letter(["Dear Dr McDonald, and TensorFlow"], contact="Dr McDonald"))   # only the name is allowed
    assert letter(["Dear Hiring Manager,"]).ok


def test_the_whole_letter_header_passes_with_the_three_caller_values():
    r = letter(generated_on="2026-11-01", employer_location="8001 Zürich", contact="Ms McDonald")
    assert r.ok, r.render_text()
    r = letter(generated_on="2026-11-01", employer_location="8001 Zürich", contact="Ms McDonald",
               blocks=HEADER + LETTER + ["I have been at Northwind since 2019."])
    assert kinds(r) == ["date"]


def test_json_fields_and_cli_flags_for_the_letter_header(config_file, tmp_path, capsys):
    obj = {"title": "Senior Business Analyst at Example Bank AG", "company": "Example Bank AG", "generated_on": "2026-11-01",
           "employer_location": "8001 Zürich", "contact": "Ms McDonald", "blocks": HEADER + LETTER}
    doc = tr.document_from_json(obj)
    assert doc.generated_on.year == 2026 and doc.employer_location == "8001 Zürich" and doc.contact == "Ms McDonald"
    assert tr.check_document(doc, FACTS).ok
    with pytest.raises(tr.TraceabilityError):
        tr.document_from_json({**obj, "generated_on": "yesterday"})
    with pytest.raises(tr.TraceabilityError):
        tr.document_from_json({**obj, "contact": 5})
    path = tmp_path / "letter.json"
    path.write_text(json.dumps({k: v for k, v in obj.items() if k not in ("generated_on", "employer_location", "contact")}), encoding="utf-8")
    assert run(config_file, "check-document", str(path)) == 1
    capsys.readouterr()
    assert run(config_file, "check-document", str(path), "--generated-on", "2026-11-01", "--location", "8001 Zürich",
               "--contact", "Ms McDonald") == 0
    capsys.readouterr()
    assert run(config_file, "check-document", str(path), "--generated-on", "2026-13-01", "--location", "8001 Zürich") == 1
    assert "not a date" in capsys.readouterr().err


# ----------------------------------------------------------------------------- posting terms and gap statements (v0.56.0)

def _doc_check(text, title, company="Banank"):
    doc = tr.Document([tr.Block(text)], title, company)
    return tr.check_document(doc, FACTS, allowed_employers=[company])


def test_the_job_title_of_the_posting_is_not_read_as_an_organisation():
    r = _doc_check("I am applying for the role of Principal Systems Analyst.", "Principal Systems Analyst")
    assert not [f for f in r.findings if f.kind == "employer"], r.render_text()
    r = _doc_check("Principal Systems Analyst, a role I would enjoy.", "Principal Systems Analyst")
    assert not [f for f in r.findings if f.kind == "employer"], r.render_text()


def test_an_invented_organisation_is_still_flagged_next_to_the_posting_title():
    r = _doc_check("I worked at Acme Systems Holding before.", "Principal Systems Analyst")
    assert "employer" in kinds(r)


def test_a_term_of_the_posting_title_is_not_an_invented_term():
    r = _doc_check("I would like to join the Genesys Cloud CX programme.", "Business Analyst - Genesys Cloud CX", "RM Group")
    assert not [f for f in r.findings if f.text == "CX"], r.render_text()


def test_a_posting_term_outside_the_title_is_still_flagged():
    r = _doc_check("I would like to join the CX programme.", "Business Analyst", "RM Group")
    assert any(f.text == "CX" for f in r.findings)


def test_a_gap_statement_about_a_tool_is_not_a_claim():
    for text in ("I have not worked with Kafka.", "Kafka is new to me.", "Kafka is not part of my background.",
                 "I have no hands-on experience with Kafka."):
        r = _doc_check(text, "Business Analyst")
        assert not [f for f in r.findings if "afka" in f.text], (text, r.render_text())


def test_a_positive_unsupported_tool_claim_is_still_flagged():
    for text in ("I have deep experience with Kafka.", "I have not used Jira, but I built Kafka pipelines."):
        r = _doc_check(text, "Business Analyst")
        assert any(f.kind == "skill" and "afka" in f.text for f in r.findings), (text, r.render_text())


@pytest.mark.parametrize("text", ["I have no experience with Kafka and built Terraform modules.",
                                  "I have not used Kafka, I built Terraform modules.",
                                  "I do not only analyse, I deploy Terraform modules.",
                                  "I never doubted my Terraform skills."])
def test_gap_exemption_does_not_leak_across_and_or_comma(text):
    r = _doc_check(text, "Business Analyst")
    assert any(f.kind == "skill" and "Terraform" in f.text for f in r.findings), (text, r.render_text())


def test_a_gap_statement_stays_exempt_next_to_a_claim_in_another_part():
    r = _doc_check("I have no experience with Kafka and built Terraform modules.", "Business Analyst")
    assert not [f for f in r.findings if "afka" in f.text], r.render_text()


def test_the_bare_title_term_as_a_skill_claim_is_still_flagged():
    r = _doc_check("I combine Python with CX and solid analysis.", "Business Analyst - Genesys Cloud CX", "RM Group")
    assert any(f.text == "CX" for f in r.findings)


# ----------------------------------------------------------------------------- terms repeated from the posting (v0.78.9)

POSTING = "SimCorp Business Analyst. You know SQL, APIs or Avaloq, Bloomberg and SIX feeds. AdTech. Client Services Enablement. KPI."


def _posting_check(text, title="SimCorp Business Analyst", company="Hays", posting=POSTING):
    return tr.check_document(tr.Document([tr.Block(text)], title, company, posting=posting), FACTS, allowed_employers=[company])


@pytest.mark.parametrize("text, term", [
    ("Your posting asks for SQL, APIs or Avaloq and for affinity with AI tools.", "Avaloq"),
    ("Your profile asks for knowledge of data feeds such as Bloomberg and SIX.", "Bloomberg"),
    ("Your profile asks for knowledge of data feeds such as Bloomberg and SIX.", "SIX"),
    ("Hays is recruiting a SimCorp Business Analyst near Zurich, and several of the tasks are ones I have done.", "SimCorp"),
    ("I would welcome an interview to discuss how I can support the insurer's SimCorp transformation.", "SimCorp"),
    ("I would welcome an interview to discuss how I can take ownership of reporting for Client Services.", "Client Services"),
    ("I have done that kind of work in banking rather than AdTech.", "AdTech"),
])
def test_a_term_only_repeated_from_the_posting_is_not_a_claim(text, term):
    r = _posting_check(text)
    assert not [f for f in r.findings if f.text == term], r.render_text()


@pytest.mark.parametrize("text, term", [
    ("I built reports in Avaloq.", "Avaloq"),
    ("Avaloq is a platform I know well.", "Avaloq"),
    ("Your posting asks for Avaloq, which I used at Northwind.", "Avaloq"),
    ("Delivered Avaloq migrations across three banks.", "Avaloq"),
    ("I built feeds with SQL, Bloomberg, and Avaloq.", "Avaloq"),
    ("I would welcome an interview to discuss how I can use my Avaloq experience.", "Avaloq"),
    ("At Northwind I worked rather with Avaloq.", "Avaloq"),
])
def test_a_claim_about_a_posting_term_is_still_blocked(text, term):
    assert any(f.text == term for f in _posting_check(text).findings), text


def test_a_term_that_is_not_in_the_posting_is_still_blocked():
    r = _posting_check("Your posting asks for Kotlin.")
    assert any(f.text == "Kotlin" for f in r.findings)


def test_a_claim_is_caught_after_a_repeated_mention_of_the_same_term():
    r = _posting_check("Your posting asks for Avaloq. I built reports in Avaloq.")
    assert any(f.text == "Avaloq" for f in r.findings)


def test_a_term_glued_to_the_word_before_it_in_the_posting_is_still_found():
    r = _posting_check("I would welcome an interview to discuss how I can support Client Services.",
                       posting="Contact: Mélanie MoosClient Services Enablement. SQL.")
    assert not [f for f in r.findings if f.text == "Client Services"], r.render_text()
    r = _posting_check("Your posting asks for Avaloq.", posting="Tools: SQLAvaloqBloomberg")
    assert any(f.text == "Avaloq" for f in r.findings)               # all-capital glue is not split: still a finding


def test_without_the_posting_nothing_is_exempt():
    r = _posting_check("Your posting asks for Avaloq.", posting=None)
    assert any(f.text == "Avaloq" for f in r.findings)


def test_a_job_title_is_never_exempt_by_the_posting():
    r = _posting_check("The role asks for a Principal Quantum Analyst, and as a Principal Quantum Analyst at Northwind I delivered.",
                       posting="Principal Quantum Analyst")
    assert "title" in kinds(r)


def test_a_company_name_with_an_ampersand_is_the_employer_applied_to():
    company = "Bosshard & Partner Unternehmensberatung AG"
    r = tr.check_document(tr.Document([tr.Block(f"The contract at {company} centres on SQL.")], "Analyst", company), FACTS, allowed_employers=[company])
    assert "employer" not in kinds(r), r.render_text()


# ----------------------------------------------------------------------------- facts confirmed by the user (v0.78.10)

def test_the_confirmed_titles_and_kpi_pass_on_the_real_fact_file():
    facts = tr.load_facts()
    for text in ("As Global Service Desk Analyst at Fabrikam I handled tickets.",
                 "I worked as a tester and business analyst at Contoso.",
                 "KPI Reporting: I built reports."):
        r = tr.check_document(tr.Document([tr.Block(text)], "Analyst", "Banank"), facts, allowed_employers=["Banank"])
        assert r.ok, r.render_text()


def test_an_alias_is_not_accepted_at_another_employer():
    r = tr.check_document(tr.Document([tr.Block("I worked as a tester and business analyst at Northwind.")], "Analyst", "Banank"),
                          tr.load_facts(), allowed_employers=["Banank"])
    assert "title" in kinds(r)


def test_terms_confirmed_by_the_user_and_sector_words_pass_on_the_real_fact_file():
    facts = tr.load_facts()
    for text in ("I used SAP and ServiceNow.", "I know the MiFID rules.", "I tested APIs with Postman and Cucumber.",
                 "I used Azure, Linux, PowerShell, GitLab and GitHub.", "I worked with JSON, XML, PostgreSQL and pandas.",
                 "I met SLAs and built Power Automate flows and RPA.", "I used GenAI and ChatGPT.", "A SaaS and FinTech firm for UHNW and HNW clients in AdTech."):
        r = tr.check_document(tr.Document([tr.Block(text)], "Analyst", "Banank"), facts, allowed_employers=["Banank"])
        assert r.ok, r.render_text()


def test_terms_the_user_did_not_confirm_are_still_blocked():
    facts = tr.load_facts()
    for term in ("Avaloq", "Salesforce", "Kubernetes", "AML", "ITIL"):
        r = tr.check_document(tr.Document([tr.Block(f"I used {term} every day.")], "Analyst", "Banank"), facts, allowed_employers=["Banank"])
        assert any(f.text == term for f in r.findings), term
