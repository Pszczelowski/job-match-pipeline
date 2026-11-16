"""Slices 3c and 3d: the cover-letter and CV PDFs (app/pdf.py, documents.render_stored, render-documents).

No network, no real config, no real database. PDFs of the import tests go to a temporary folder (conftest)."""
import copy
import json
import random
import re
from datetime import date

import pytest
from fpdf.enums import MethodReturnValue
from pypdf import PdfReader
from pypdf.generic import ContentStream

from app import cli, db, documents, pdf
from app.config import ROOT, ConfigError, load_config
from app.report import collect
from app.results import import_results
from tests.test_batch_results import NOW, results_file, score
from tests.test_cli import clean_logger, config_file, run  # noqa: F401
from tests.test_documents import CV, DESCRIPTION, docs, letter, prepare

DAY = date(2026, 11, 1)
FACTS, VARIANTS = documents.load_profile()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"]["exchange"] = str(tmp_path / "exchange")
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def prompt_example():
    text = (ROOT / "prompts" / "scoring_prompt.md").read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.+?)\n```", text, re.DOTALL).group(1))
    return example["results"][0]["documents"]


def text_of(path) -> str:
    return " ".join(" ".join(p.extract_text().split()) for p in PdfReader(str(path)).pages)


def job(company="Example Bank AG", locations=("Zürich, Switzerland",)):
    return {"company": company, "title": "Business Analyst, Payments", "locations": list(locations)}


MEASURED_ON = "2026-11-01"      # the page budgets of the prompt were measured on the fact file of this day


def measured(bullet: dict) -> bool:
    """A bullet that was in the fact file when the budgets were measured (later ones carry a dated `origin`)."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})$", str(bullet.get("origin") or ""))
    return m is None or m.group(1) <= MEASURED_ON


def full_cv(variant="resume", limit=24, all_bullets=False):
    """Every bullet of the fact file of `MEASURED_ON` that has a text for the variant, at most `limit` (the prompt's 24):
    the longest CV the prompt allows, the same one the budgets were measured with. Bullets added later (v0.52.1) are left
    out, so new facts do not change the measurement."""
    exp = []
    for e in FACTS["experience"]:
        bullets = [{"fact_id": b["id"], "text": b["texts"][variant]} for b in e["bullets"]
                   if variant in b.get("texts", {}) and (all_bullets or measured(b))]
        if bullets:
            exp.append({"experience_id": e["id"], "bullets": bullets})
    while sum(len(e["bullets"]) for e in exp) > limit:
        max(exp, key=lambda e: len(e["bullets"]))["bullets"].pop()
    return {"variant": variant, "summary": " ".join(VARIANTS["variants"][variant]["summary"].split()), "experience": exp}


def fonts_of(path) -> set[str]:
    names = set()
    for page in PdfReader(str(path)).pages:
        for font in page["/Resources"]["/Font"].values():
            names.add(str(font.get_object()["/BaseFont"]))
    return names


def size_of(path):
    box = PdfReader(str(path)).pages[0].mediabox
    return round(float(box.width)), round(float(box.height))


PHOTO = ROOT / "profile" / "photo.jpg"


# --- the cover letter --------------------------------------------------------------------------------------------

def test_the_prompt_example_letter_is_one_a4_page(tmp_path):
    r = pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "l.pdf")
    assert r.pages == 1 and size_of(r.path) == (595, 842)                       # A4, not US Letter (612 x 792)


def test_the_letter_text_is_selectable_and_complete(tmp_path):
    r = pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "l.pdf")
    t = text_of(r.path)
    assert "Alex Morgan" in t and "Example Bank AG" in t and "1 November 2026" in t
    assert "Zürich" in t and "alex.morgan@example.com" in t and "+41 76 000 00 00" in t and "linkedin.com/in/alex-morgan-example" in t
    assert "Dear Hiring Manager," in t and "Thank you for your time and consideration." in t and "Sincerely," in t
    assert t.index("Dear Hiring Manager,") < t.index("Process Automation") < t.index("Sincerely,")
    for label in ("Process Automation:", "Requirements Delivery:", "Testing Excellence:", "Data Validation:"):
        assert label in t
    assert t.rstrip().endswith("Alex Morgan")


def test_the_salutation_names_the_contact_when_there_is_one(tmp_path):
    d = copy.deepcopy(prompt_example()["cover_letter"])
    d["contact_name"] = "Ms Muster"
    assert "Dear Ms Muster," in text_of(pdf.render_cover_letter(d, job(), FACTS, DAY, tmp_path / "a.pdf").path)
    d["contact_name"] = None
    t = text_of(pdf.render_cover_letter(d, job(), FACTS, DAY, tmp_path / "b.pdf").path)
    assert "Dear Hiring Manager," in t and "Dear Ms" not in t


def test_the_letter_has_no_street_address(tmp_path):
    t = text_of(pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "l.pdf").path)
    assert not re.search(r"\b(strasse|straße|weg|gasse|platz)\b", t, re.IGNORECASE) and not re.search(r"\b80\d\d\b", t)


def test_fonts_are_embedded(tmp_path):
    l = pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "l.pdf")
    c = pdf.render_cv(prompt_example()["cv"], FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
    for path in (l.path, c.path):
        names = fonts_of(path)
        assert names and all("EBGaramond" in n for n in names), names
        assert any("Bold" in n for n in names) and all(re.match(r"^/[A-Z]{6}\+", n) for n in names)     # embedded subsets


def test_the_date_is_written_in_full_and_does_not_depend_on_the_locale():
    assert pdf.long_date(date(2026, 11, 1)) == "1 November 2026"
    assert pdf.long_date(date(2027, 3, 14)) == "14 March 2027"


def test_the_city_is_the_first_place_of_the_first_location():
    assert pdf.city_of(["Zürich, Switzerland", "Basel"]) == "Zürich"
    assert pdf.city_of([]) == "" and pdf.city_of(["", "Bern"]) == "Bern"


def test_a_letter_without_a_location_has_the_employer_only(tmp_path):
    t = text_of(pdf.render_cover_letter(prompt_example()["cover_letter"], job(locations=()), FACTS, DAY, tmp_path / "l.pdf").path)
    assert "Example Bank AG Dear" in t


def test_special_characters_render(tmp_path):
    d = copy.deepcopy(prompt_example()["cover_letter"])
    d["blocks"]["closing"] = "Grüße aus Łódź, Köln and Älvsjö: ł ü ä ö ß é."
    t = text_of(pdf.render_cover_letter(d, job("Müller & Söhne AG"), FACTS, DAY, tmp_path / "l.pdf").path)
    assert "Grüße aus Łódź, Köln and Älvsjö: ł ü ä ö ß é." in t and "Müller & Söhne AG" in t


def test_markdown_markers_in_the_text_do_not_change_the_layout(tmp_path):
    d = copy.deepcopy(prompt_example()["cover_letter"])
    d["blocks"]["achievements"][0]["text"] = "Saved **a lot** with __scripts__ -- really."
    t = text_of(pdf.render_cover_letter(d, job(), FACTS, DAY, tmp_path / "l.pdf").path)
    assert "**" not in t and "__" not in t and "Saved a lot with _scripts_ – really." in t


# --- the CV ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["resume", "resume_cs"])
def test_the_longest_cv_the_files_allow_fits_two_a4_pages(tmp_path, variant):
    r = pdf.render_cv(full_cv(variant), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
    assert r.pages == 2 and size_of(r.path) == (595, 842) and r.warnings == []


@pytest.mark.parametrize("variant", ["resume", "resume_cs"])
def test_a_cv_at_the_prompt_caps_of_30_bullets_and_650_words_fits_two_a4_pages(tmp_path, variant):
    """The prompt (1.15.0) allows 30 bullets, 650 bullet words, a 100-word summary and all four skill groups: the
    longest CV it can produce. The texts are the fact file's, padded to 650 words, so the layout is the worst case."""
    cv = full_cv(variant, 30, all_bullets=True)
    bullets = [b for e in cv["experience"] for b in e["bullets"]]
    extra = 650 - sum(len(b["text"].split()) for b in bullets)
    for i, b in enumerate(bullets):
        b["text"] += " detail" * (extra // len(bullets) + (1 if i < extra % len(bullets) else 0))
    cv["summary"] = " ".join(["word"] * 100)
    cv["skills"] = [g["id"] for g in FACTS["skills"]]
    assert len(bullets) == 30 and sum(len(b["text"].split()) for b in bullets) == 650 and len(cv["skills"]) == 4
    r = pdf.render_cv(cv, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
    assert r.pages == 2 and r.warnings == []


def test_the_prompt_example_cv_renders(tmp_path):
    r = pdf.render_cv(prompt_example()["cv"], FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
    assert 1 <= r.pages <= 2
    t = text_of(r.path)
    assert "Alex Morgan" in t and "SUMMARY" in t and "WORK EXPERIENCE" in t and "TECHNICAL SKILLS" in t


def test_the_header_comes_from_the_fact_file(tmp_path):
    t = text_of(pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    p = FACTS["person"]
    for value in (p["name"], p["email"], p["phone"], p["location"], f"Nationality: {p['nationality']}", f"Permit: {p['permit']}",
                  "Birthdate: 15.01.1990", pdf.display_url(p["linkedin"])):
        assert value in t


def test_employer_title_place_and_dates_equal_the_fact_file(tmp_path):
    doc = full_cv()
    t = text_of(pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    by_id = {e["id"]: e for e in FACTS["experience"]}
    assert doc["experience"]
    for entry in doc["experience"]:
        e = by_id[entry["experience_id"]]
        title = e["title"] + (f" – {e['program']}" if e.get("program") else "")
        assert title in t and e["location"] in t
        if e["employer"]:
            assert e["employer"] in t
        assert pdf.date_range(e["start"], e["end"]) in t
    assert "Aug. 2023 – Jul. 2025" in t and "Jul. 2025 – Present" in t and "Nov. 2021 – Jul. 2023" in t


def test_the_session_cannot_change_employer_or_dates_in_the_cv(tmp_path):
    doc = full_cv()
    doc["experience"][1]["employer"] = "Evil Corp"
    doc["experience"][1]["start"] = "1999-01"
    t = text_of(pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    assert "Evil Corp" not in t and "1999" not in t


def test_the_two_variants_differ_in_the_heading_over_the_summary(tmp_path):
    a = text_of(pdf.render_cv(full_cv("resume"), FACTS, VARIANTS, tmp_path / "a.pdf", PHOTO, DAY).path)
    b = text_of(pdf.render_cv(full_cv("resume_cs"), FACTS, VARIANTS, tmp_path / "b.pdf", PHOTO, DAY).path)
    assert " SUMMARY " in a and VARIANTS["variants"]["resume_cs"]["headline"] in b and " SUMMARY " not in b
    assert "AI prompting / AI prompting" not in a + b and "AI prompting and workflows" in b and "AI prompting |" in a


def test_education_and_languages_come_from_the_fact_file(tmp_path):
    t = text_of(pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    assert "Tailspin University of Economics" in t and "Bachelor Studies in Business Administration (not completed)" in t
    assert "High School Diploma" in t and "Maturity Exam" in t and "Sep. 2012 – Jun. 2014" in t
    assert "English: fluent | Polish: fluent | German: basic" in t


def test_the_oracle_certificate_is_only_shown_as_in_preparation(tmp_path):
    t = text_of(pdf.render_cv(full_cv("resume"), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    assert t.count("1Z0-071") == 1 and "Preparing for: Oracle Database SQL Certified Associate (1Z0-071)" in t
    # a certificate with a status that is not "completed" or "in_preparation" is not shown at all
    facts = copy.deepcopy(FACTS)
    facts["certifications"][0]["status"] = "unclear"
    assert "1Z0-071" not in text_of(pdf.render_cv(full_cv("resume"), facts, VARIANTS, tmp_path / "d.pdf", PHOTO, DAY).path)


def test_a_bullet_that_already_says_it_is_not_repeated_as_a_certificate_line(tmp_path):
    t = text_of(pdf.render_cv(full_cv("resume_cs"), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    assert t.count("1Z0-071") == 1 and "Preparing for Oracle Database SQL certification (1Z0-071" in t


def test_skill_groups_follow_the_stored_order(tmp_path):
    doc = full_cv()
    doc["skills"] = ["skills-testing-collab", "skills-reporting-data"]
    t = text_of(pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    assert t.index("Testing & Collaboration") < t.index("Reporting & Data") and "Automation & Development" not in t
    assert "Operations & Controls" not in t


def test_the_photo_is_embedded_when_present(tmp_path):
    r = pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
    assert len(PdfReader(str(r.path)).pages[0].images) == 1 and r.warnings == []


@pytest.mark.parametrize("photo", [None, "missing.jpg"])
def test_a_missing_photo_gives_a_cv_without_photo_and_a_warning(tmp_path, photo):
    r = pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / "c.pdf", tmp_path / photo if photo else None, DAY)
    assert r.path.is_file() and len(PdfReader(str(r.path)).pages[0].images) == 0
    assert len(r.warnings) == 1 and "photo" in r.warnings[0] and "no photo" in r.warnings[0]


def test_a_broken_photo_gives_a_cv_without_photo_and_a_warning(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"this is not an image")
    r = pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / "c.pdf", bad, DAY)
    assert r.path.is_file() and len(PdfReader(str(r.path)).pages[0].images) == 0 and "could not be used" in r.warnings[0]


def test_an_unknown_experience_is_an_error_and_writes_nothing(tmp_path):
    doc = full_cv()
    doc["experience"][0]["experience_id"] = "exp-nowhere"
    with pytest.raises(pdf.PdfError):
        pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
    assert not (tmp_path / "c.pdf").exists()


# --- determinism, address, names -----------------------------------------------------------------------------------

def test_the_same_input_gives_the_same_bytes(tmp_path):
    for render in (lambda p: pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, p),
                   lambda p: pdf.render_cv(full_cv(), FACTS, VARIANTS, p, PHOTO, DAY)):
        a, b = render(tmp_path / "a.pdf"), render(tmp_path / "b.pdf")
        assert a.path.read_bytes() == b.path.read_bytes()


def test_render_cv_public_drops_contact_lines(tmp_path):
    """public=True (the landing-page CV, v0.101.0): name, city, permit, nationality, photo, LinkedIn and GitHub stay; email,
    phone, birthdate and any mailto link are gone."""
    facts = copy.deepcopy(FACTS)
    facts["person"]["github"] = "https://github.com/example-user"
    r = pdf.render_cv(full_cv(), facts, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY, public=True)
    t = text_of(r.path)
    person = FACTS["person"]
    for kept in (person["name"], person["location"], "Permit: B", "Nationality: Polish", pdf.display_url(person["linkedin"]), "github.com/example-user", "morgan.pages.dev"):
        assert kept in t
    assert person["email"] not in t and person["phone"] not in t and "Birthdate" not in t and "15.01.1990" not in t
    assert "@" not in t
    uris = [a.get_object().get("/A", {}).get("/URI", "") for page in PdfReader(str(r.path)).pages for a in page.get("/Annots") or []]
    assert uris and not any("mailto" in u for u in uris) and any("github.com" in u for u in uris)
    full = text_of(pdf.render_cv(full_cv(), facts, VARIANTS, tmp_path / "f.pdf", PHOTO, DAY).path)
    assert person["email"] in full and "Birthdate" in full            # the default is unchanged


# --- ATS compliance of the CV (v0.102.0) ---------------------------------------------------------------------------

def plain_text(path) -> str:
    """The text of all pages in extraction order, white space collapsed."""
    return text_of(path)


def uris_of(path) -> list[str]:
    return [a.get_object().get("/A", {}).get("/URI", "") for page in PdfReader(str(path)).pages for a in page.get("/Annots") or []]


def test_display_url_strips_scheme_www_and_slash():
    assert pdf.display_url("https://www.linkedin.com/in/alex-morgan-example/") == "linkedin.com/in/alex-morgan-example"
    assert pdf.display_url("https://github.com/Morgan") == "github.com/Morgan"
    assert pdf.display_url("http://example.org/") == "example.org"
    assert pdf.display_url("example.org/x") == "example.org/x"


def test_the_cv_header_shows_github_and_the_landing_page_with_links(tmp_path):
    p = FACTS["person"]
    for public in (False, True):
        r = pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / f"c{public}.pdf", PHOTO, DAY, public=public)
        t = plain_text(r.path)
        for shown in ("github.com/Morgan", "morgan.pages.dev", "linkedin.com/in/alex-morgan-example"):
            assert shown in t
        uris = uris_of(r.path)
        for url in (p["github"], p["website"], p["linkedin"]):
            assert url in uris
        if not public:
            assert p["email"] in t and p["phone"] in t
        else:
            assert p["email"] not in t and p["phone"] not in t


def test_the_cv_text_layer_reads_in_ats_order(tmp_path):
    doc = full_cv()
    p = FACTS["person"]
    t = plain_text(pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    pos = 0

    def after(anchor: str) -> None:
        nonlocal pos
        i = t.find(anchor, pos)
        assert i >= 0, f"{anchor!r} missing or out of order after position {pos}"
        pos = i + len(anchor)

    for anchor in (p["name"], p["email"], p["phone"], p["location"], "linkedin.com/in/alex-morgan-example", "github.com/Morgan",
                   "morgan.pages.dev", " ".join(doc["summary"].split()[:6]), "WORK EXPERIENCE"):
        after(anchor)
    by_id = {e["id"]: e for e in FACTS["experience"]}
    for entry in doc["experience"]:
        e = by_id[entry["experience_id"]]
        title = e["title"] + (f" – {e['program']}" if e.get("program") else "")
        for anchor in (title, pdf.date_range(e["start"], e["end"]), e.get("employer") or "", e["location"],
                       " ".join(entry["bullets"][0]["text"].split()[:5])):
            if anchor:
                after(anchor)
    after("EDUCATION & LANGUAGES")
    after("TECHNICAL SKILLS")
    skills = t[pos:]
    groups = {g["id"]: g for g in FACTS["skills"]}
    for g in groups.values():
        for item in g["items"]:
            wording = pdf._item_for(item, "resume")
            assert " ".join(wording.split()) in skills, f"skill item {wording!r} is not whole on one line"


def test_the_pdf_declares_language_title_and_author(tmp_path):
    for path in (pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path,
                 pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "l.pdf").path):
        reader = PdfReader(str(path))
        assert str(reader.trailer["/Root"].get("/Lang")) == "en"
        assert reader.metadata.title and reader.metadata.author == FACTS["person"]["name"]


def test_the_cv_has_no_hidden_or_white_text(tmp_path):
    """Guard (passes before v0.102.0): no invisible text render mode, only black or link-blue fills, no tiny fonts."""
    path = pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path
    allowed = {(0.0, 0.0, 0.0), tuple(round(c / 255, 3) for c in pdf.BLUE)}
    for page in PdfReader(str(path)).pages:
        ops = ContentStream(page.get_contents(), PdfReader(str(path))).operations
        for operands, op in ops:
            if op == b"Tr":
                assert int(operands[0]) != 3
            if op == b"rg":
                assert tuple(round(float(x), 3) for x in operands) in allowed
            if op == b"Tf":
                assert float(operands[1]) >= 6


def test_bullets_and_dashes_extract_as_plain_characters(tmp_path):
    """Guard: the bullet and the en dash come out as real characters, no replacement or private-use code points."""
    t = plain_text(pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    assert "•" in t and "–" in t
    assert "�" not in t and not any(0xE000 <= ord(c) <= 0xF8FF for c in t)


def test_no_skill_item_has_unbalanced_parentheses():
    """A comma inside parentheses splits a YAML flow-list item in two ("T-SQL (MSSQL queries" / "stored procedures)")."""
    import yaml
    for rel in ("profile/master_cv.yaml", "examples/persona/master_cv.yaml"):
        facts = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
        for group in facts["skills"]:
            for item in group["items"]:
                assert str(item).count("(") == str(item).count(")"), f"{rel}: {item!r}"


def test_the_letter_header_has_github_and_the_landing_page(tmp_path):
    r = pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "l.pdf")
    t = plain_text(r.path)
    assert "github.com/Morgan" in t and "morgan.pages.dev" in t
    assert FACTS["person"]["github"] in uris_of(r.path)


def test_the_home_address_never_reaches_a_file(tmp_path):
    address = "Rosenweg 12, 8180 Exampletown"
    d = copy.deepcopy(prompt_example()["cover_letter"])
    d["blocks"]["closing"] += f" You find me at {address}."
    with pytest.raises(pdf.PdfError, match="home address"):
        pdf.render_cover_letter(d, job(), FACTS, DAY, tmp_path / "l.pdf", home_address=address)
    cv = full_cv()
    cv["summary"] += " rosenweg 12 8180 Exampletown"
    with pytest.raises(pdf.PdfError, match="home address"):
        pdf.render_cv(cv, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY, home_address=address)
    assert list(tmp_path.iterdir()) == []
    # without the address in the text the same call works, and the file does not hold it
    ok = pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "ok.pdf", home_address=address)
    assert "Rosenweg" not in text_of(ok.path) and "Exampletown" not in text_of(ok.path)


@pytest.mark.parametrize("company", ["Müller & Söhne AG/Zürich", "Łódź Bank: \"Test\"", "A" * 200, "", "???", "Crédit Agricole S.A."])
def test_file_names_are_ascii_and_safe(company):
    for kind in ("cover_letter", "cv"):
        name = pdf.file_name("Alex Morgan", kind, company)
        assert name.isascii() and name.endswith(".pdf") and not re.search(r'[<>:"/\\|?*\x00-\x1f]', name) and len(name) < 120
        assert name.startswith("Alex Morgan - " + ("Cover Letter" if kind == "cover_letter" else "CV") + " - ")
    assert pdf.file_name("Alex Morgan", "cv", "Example Bank AG") == "Alex Morgan - CV - Example Bank AG.pdf"
    folder = pdf.folder_name(17, company)
    assert folder.isascii() and folder.startswith("17_") and "/" not in folder and " " not in folder


# --- stored documents, import and the command ---------------------------------------------------------------------------

def pdf_rows(conn):
    return {(r["job_id"], r["kind"]): dict(r) for r in conn.execute(
        "SELECT job_id, kind, status, findings, pdf_path, pages, rendered_at FROM documents")}


def import_with_docs(conn, cfg, scores=(90,), mutate=None, run_id="run1"):
    ids = prepare(conn, cfg, list(scores))
    es = [score(i, s, **({"documents": (mutate(docs()) if mutate else docs())} if n == 0 else {}))
          for n, (i, s) in enumerate(zip(ids, scores))]
    result = import_results(conn, cfg, results_file(cfg, run_id, es, schema_version="1.1"), NOW)
    return ids, result


def test_migration_12_adds_the_pdf_columns(conn):
    assert len(db.MIGRATIONS) >= 12
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    assert {"pdf_path", "pages", "rendered_at"} <= cols


def test_the_import_builds_both_pdfs_and_stores_path_and_pages(conn, cfg):
    ids, result = import_with_docs(conn, cfg)
    rows = pdf_rows(conn)
    root = documents.documents_dir(cfg)
    assert set(rows) == {(ids[0], "cover_letter"), (ids[0], "cv")}
    for (job_id, kind), r in rows.items():
        assert r["status"] == "draft" and r["pdf_path"] and r["pages"] and r["rendered_at"]
        assert (root / r["pdf_path"]).is_file() and r["pdf_path"].isascii()
        assert r["pdf_path"].startswith(f"{ids[0]}_acme/Alex Morgan - ")
    assert rows[(ids[0], "cover_letter")]["pages"] == 1 and rows[(ids[0], "cv")]["pages"] <= 2
    assert rows[(ids[0], "cover_letter")]["pdf_path"].endswith("- Cover Letter - Acme.pdf")
    assert rows[(ids[0], "cv")]["pdf_path"].endswith("- CV - Acme.pdf")
    assert [x[:2] for x in result.documents_rendered] == [(ids[0], "cover_letter"), (ids[0], "cv")]
    t = text_of(root / rows[(ids[0], "cover_letter")]["pdf_path"])
    assert "Analyst 1" not in t and "Dear Hiring Manager," in t and "Acme" in t and "2 November 2026" in t


def test_the_import_summary_lists_the_rendered_pdfs(conn, cfg):
    import_with_docs(conn, cfg)
    summary = json.loads(conn.execute("SELECT summary FROM imports ORDER BY id DESC LIMIT 1").fetchone()["summary"])
    assert [(k, n) for _j, k, n in summary["documents_rendered"]] == [("cover_letter", 1), ("cv", 2)]


def test_the_letter_names_the_contact_from_the_posting(conn, cfg):
    prepared = prepare(conn, cfg, [90])
    d = docs("Ms Muster")
    import_results(conn, cfg, results_file(cfg, "run1", [score(prepared[0], 90, documents=d)], schema_version="1.1"), NOW)
    r = pdf_rows(conn)[(prepared[0], "cover_letter")]
    assert "Dear Ms Muster," in text_of(documents.documents_dir(cfg) / r["pdf_path"])


@pytest.mark.parametrize("contact, expected", [
    ("Ms Anna Muster", "Ms Muster"),
    ("Dr. Alexandra Müller-Schwarzenbach", "Dr. Müller-Schwarzenbach"),
    ("Prof. Dr. Hans Peter Meier", "Prof. Dr. Meier"),
    ("mrs. Jane Doe", "mrs. Doe"),
    ("MR Jan van der Berg", "MR van der Berg"),
    ("Mx Alex Rivera", "Mx Rivera"),
    ("Miss Sophie Keller", "Miss Keller"),
    ("Ms Muster", "Ms Muster"),
    ("Dr", "Dr"),
    ("Anna Muster", "Anna Muster"),
    ("Hans van der Berg", "Hans van der Berg"),
    ("Doris Meier", "Doris Meier"),
])
def test_salutation_name_keeps_titles_and_the_surname_only(contact, expected):
    assert pdf.salutation_name(contact) == expected


def test_the_letter_shortens_the_contact_to_title_and_surname(tmp_path):
    d = copy.deepcopy(prompt_example()["cover_letter"])
    d["contact_name"] = "Ms Anna Muster"
    t = text_of(pdf.render_cover_letter(d, job(), FACTS, DAY, tmp_path / "a.pdf").path)
    assert "Dear Ms Muster," in t and "Anna" not in t
    d["contact_name"] = "Anna Muster"
    t = text_of(pdf.render_cover_letter(d, job(), FACTS, DAY, tmp_path / "b.pdf").path)
    assert "Dear Anna Muster," in t


def test_the_traceability_check_accepts_a_contact_that_the_salutation_shortens(conn, cfg):
    prepared = prepare(conn, cfg, [90], description=DESCRIPTION + " Please send your application to Ms Anna Muster.")
    d = docs("Ms Anna Muster")
    import_results(conn, cfg, results_file(cfg, "run1", [score(prepared[0], 90, documents=d)], schema_version="1.1"), NOW)
    r = pdf_rows(conn)[(prepared[0], "cover_letter")]
    assert r["status"] == "draft", r["findings"]
    assert "Dear Ms Muster," in text_of(documents.documents_dir(cfg) / r["pdf_path"])


def test_blocked_documents_get_no_pdf(conn, cfg):
    def spoil(d):
        d["cover_letter"]["blocks"]["skills"] = "I know Kubernetes."
        return d
    ids, result = import_with_docs(conn, cfg, mutate=spoil)
    rows = pdf_rows(conn)
    assert rows[(ids[0], "cover_letter")]["status"] == "blocked" and rows[(ids[0], "cover_letter")]["pdf_path"] is None
    assert rows[(ids[0], "cv")]["status"] == "draft" and rows[(ids[0], "cv")]["pdf_path"]
    files = [p.name for p in documents.documents_dir(cfg).rglob("*.pdf")]
    assert files == ["Alex Morgan - CV - Acme.pdf"]
    assert [x[1] for x in result.documents_rendered] == ["cv"]
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents blocked (1 finding)"


def test_a_later_file_replaces_the_pdf_and_removes_the_stale_one(conn, cfg):
    ids, _ = import_with_docs(conn, cfg)
    root = documents.documents_dir(cfg)
    old = pdf_rows(conn)[(ids[0], "cover_letter")]["pdf_path"]
    assert (root / old).is_file()
    d = docs()
    d["cover_letter"]["blocks"]["skills"] = "I know Kubernetes."          # now blocked: no PDF, the old file must go
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 90, documents=d)], schema_version="1.1", name="again.json"), NOW)
    r = pdf_rows(conn)[(ids[0], "cover_letter")]
    assert r["status"] == "blocked" and r["pdf_path"] is None and r["pages"] is None and not (root / old).exists()


def test_a_too_long_letter_keeps_its_pdf_and_stays_a_draft_with_a_warning(conn, cfg):
    ids = prepare(conn, cfg, [90])
    long_letter = letter()
    long_letter["blocks"]["skills"] = " ".join(["I work carefully with stakeholders and teams every single day."] * 40)
    with conn:
        documents.store_document(conn, ids[0], "cover_letter", None, long_letter, [], DAY, "x")
        documents.store_document(conn, ids[0], "cv", None, copy.deepcopy(CV), [], DAY, "x")
    out = documents.render_stored(conn, cfg)
    r = pdf_rows(conn)[(ids[0], "cover_letter")]
    assert r["pages"] == 2 and r["status"] == "draft" and (documents.documents_dir(cfg) / r["pdf_path"]).is_file()
    findings = json.loads(r["findings"])
    assert [f["kind"] for f in findings] == ["pdf_over_page_limit"]
    assert findings[0]["reason"] == "cover letter is 2 pages (limit 1)"
    assert out.over_limit == [(ids[0], "cover_letter", 2, 1)]
    assert documents.state(conn, ids[0], cfg) == ("ready", 0)
    assert documents.notes(conn, ids[0]) == ["cover letter is 2 pages (limit 1)"]


def test_a_too_long_cv_is_flagged_by_the_configured_limit(conn, cfg):
    cfg["tailoring"]["cv_max_pages"] = 1
    ids = prepare(conn, cfg, [90])
    with conn:
        documents.store_document(conn, ids[0], "cover_letter", None, letter(), [], DAY, "x")
        documents.store_document(conn, ids[0], "cv", None, full_cv(), [], DAY, "x")
    documents.render_stored(conn, cfg)
    r = pdf_rows(conn)[(ids[0], "cv")]
    assert r["pages"] == 2 and r["status"] == "draft"
    assert json.loads(r["findings"])[0]["reason"] == "CV is 2 pages (limit 1)"
    cfg["tailoring"]["cv_max_pages"] = 2                                        # a later run with the right limit drops the warning
    documents.render_stored(conn, cfg)
    assert json.loads(pdf_rows(conn)[(ids[0], "cv")]["findings"]) == []


def test_the_report_says_ready_and_adds_the_page_note(conn, cfg):
    ids = prepare(conn, cfg, [90])
    long_letter = letter()
    long_letter["blocks"]["skills"] = " ".join(["I work carefully with stakeholders and teams every single day."] * 40)
    with conn:
        documents.store_document(conn, ids[0], "cover_letter", None, long_letter, [], DAY, "x")
        documents.store_document(conn, ids[0], "cv", None, copy.deepcopy(CV), [], DAY, "x")
    documents.render_stored(conn, cfg)
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 90)], schema_version="1.1"), NOW)
    r = collect(conn, cfg, "run1")
    assert r.jobs[0].documents == "documents ready" and r.jobs[0].document_notes == ["cover letter is 2 pages (limit 1)"]
    assert "DOCUMENTS READY" in r.text and "Documents: cover letter is 2 pages (limit 1)" in r.text
    assert "Documents: cover letter is 2 pages (limit 1)" in r.html
    assert "UAT" not in r.text and "Kubernetes" not in r.text and "stakeholders and teams" not in r.text


def test_the_report_marker_needs_both_pdfs(conn, cfg):
    ids, _ = import_with_docs(conn, cfg)
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents ready"
    assert collect(conn, cfg, "run1").jobs[0].document_notes == []
    root = documents.documents_dir(cfg)
    victim = root / pdf_rows(conn)[(ids[0], "cv")]["pdf_path"]
    victim.unlink()                                                             # the file is gone
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents not rendered"
    with conn:
        conn.execute("UPDATE documents SET pdf_path = NULL, pages = NULL WHERE kind = 'cover_letter'")
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents not rendered"


def test_a_render_failure_is_recorded_and_does_not_stop_the_others(conn, cfg, monkeypatch):
    real = pdf.render_cover_letter

    def boom(*a, **k):
        raise RuntimeError("font trouble")
    monkeypatch.setattr(pdf, "render_cover_letter", boom)
    ids, result = import_with_docs(conn, cfg)
    rows = pdf_rows(conn)
    assert rows[(ids[0], "cover_letter")]["pdf_path"] is None and rows[(ids[0], "cv")]["pdf_path"]
    assert json.loads(rows[(ids[0], "cover_letter")]["findings"])[0]["kind"] == "render_error"
    assert any("could not be built" in w and "font trouble" in w for w in result.warnings)
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents not rendered"
    monkeypatch.setattr(pdf, "render_cover_letter", real)               # (not undo(): that would also drop conftest's patches)
    documents.render_stored(conn, cfg)                                          # a later run repairs it
    assert pdf_rows(conn)[(ids[0], "cover_letter")]["pdf_path"] and json.loads(pdf_rows(conn)[(ids[0], "cover_letter")]["findings"]) == []


def test_a_missing_photo_does_not_stop_the_import_and_is_a_warning(conn, cfg):
    cfg["tailoring"]["photo"] = "profile/no-such-photo.jpg"
    ids, result = import_with_docs(conn, cfg)
    assert pdf_rows(conn)[(ids[0], "cv")]["pdf_path"]
    assert sum("photo" in w for w in result.warnings) == 1
    cfg["tailoring"]["photo"] = ""
    ids2 = prepare(conn, cfg, [88], run_id="run2", start=10)
    r2 = import_results(conn, cfg, results_file(cfg, "run2", [score(ids2[0], 88, documents=docs())], schema_version="1.1"), NOW)
    assert any("no photo configured" in w for w in r2.warnings)


def test_no_documents_no_rendering_and_nothing_written(conn, cfg):
    ids = prepare(conn, cfg, [90])
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 90)], schema_version="1.1"), NOW)
    assert r.documents_rendered == [] and not list(documents.documents_dir(cfg).rglob("*.pdf"))


# --- the command ---------------------------------------------------------------------------------------------------

def seeded(config_file, mutate=None):
    c = load_config(config_file)
    conn = db.connect(c["paths"]["database"])
    ids = prepare(conn, c, [90, 80])
    es = [score(ids[0], 90, documents=docs()), score(ids[1], 80, documents=(mutate or (lambda d: d))(docs()))]
    import_results(conn, c, results_file(c, "run1", es, schema_version="1.1"), NOW)
    conn.close()
    return c, ids


def test_render_documents_rebuilds_missing_pdfs_and_prints_paths(config_file, capsys):
    c, ids = seeded(config_file)
    root = documents.documents_dir(c)
    for p in root.rglob("*.pdf"):
        p.unlink()
    capsys.readouterr()
    assert run(config_file, "render-documents") == 0
    out = capsys.readouterr().out
    assert out.count("1 page(s)") >= 2 and "4 PDF(s) built, 0 skipped, 0 failed" in out
    assert str(root / f"{ids[0]}_acme") in out
    assert len(list(root.rglob("*.pdf"))) == 4


def test_render_documents_for_one_job_and_unknown_job(config_file, capsys):
    c, ids = seeded(config_file)
    capsys.readouterr()
    assert run(config_file, "render-documents", "--job", str(ids[1])) == 0
    out = capsys.readouterr().out
    assert f"job {ids[1]} " in out and f"job {ids[0]} " not in out and "2 PDF(s) built" in out
    assert run(config_file, "render-documents", "--job", "999") == 1
    assert "no stored documents" in capsys.readouterr().err


def test_render_documents_skips_blocked_documents(config_file, capsys):
    def spoil(d):
        d["cv"]["experience"][0]["bullets"][0]["text"] = "Saved 5 million CHF at Northwind."
        return d
    c, ids = seeded(config_file, spoil)
    capsys.readouterr()
    assert run(config_file, "render-documents") == 0
    out = capsys.readouterr().out
    assert f"skipped: job {ids[1]} cv" in out and "3 PDF(s) built, 1 skipped" in out


def test_render_documents_with_out_writes_only_there(config_file, tmp_path, capsys):
    c, ids = seeded(config_file)
    conn = db.connect(c["paths"]["database"])
    before = [tuple(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")]
    conn.close()
    target = tmp_path / "elsewhere"
    capsys.readouterr()
    assert run(config_file, "render-documents", "--out", str(target)) == 0
    out = capsys.readouterr().out
    assert len(list(target.rglob("*.pdf"))) == 4 and str(target) in out
    conn = db.connect(c["paths"]["database"])
    assert [tuple(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")] == before
    conn.close()


def test_render_documents_changes_no_row_other_than_the_pdf_columns(config_file):
    c, ids = seeded(config_file)
    conn = db.connect(c["paths"]["database"])
    cols = "id, job_id, kind, run_id, import_id, content, status, generated_on, created_at"
    before = [tuple(r) for r in conn.execute(f"SELECT {cols} FROM documents ORDER BY id")]
    jobs_before = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]
    conn.close()
    assert run(config_file, "render-documents") == 0
    conn = db.connect(c["paths"]["database"])
    assert [tuple(r) for r in conn.execute(f"SELECT {cols} FROM documents ORDER BY id")] == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")] == jobs_before
    conn.close()


def test_the_documents_command_shows_the_pdf_state(config_file, capsys):
    seeded(config_file)
    capsys.readouterr()
    assert run(config_file, "documents", "--json") == 0
    listing = json.loads(capsys.readouterr().out)
    assert all(d["pdf_path"] and d["pages"] for d in listing)


# --- config ----------------------------------------------------------------------------------------------------------

def test_the_new_config_keys_have_defaults(cfg):
    assert cfg["paths"]["documents"] == "output/documents"
    assert cfg["tailoring"]["letter_max_pages"] == 1 and cfg["tailoring"]["cv_max_pages"] == 2
    assert cfg["tailoring"]["photo"] == "profile/photo.jpg" and (ROOT / "profile" / "photo.jpg").is_file()


@pytest.mark.parametrize("text, key", [
    ("tailoring:\n  letter_max_pages: 0\n", "letter_max_pages"), ("tailoring:\n  cv_max_pages: 2.5\n", "cv_max_pages"),
    ("tailoring:\n  cv_max_pages: true\n", "cv_max_pages"), ("tailoring:\n  photo: 5\n", "photo"),
    ("paths:\n  documents: ''\n", "documents")])
def test_bad_new_config_values(tmp_path, text, key):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=key):
        load_config(p)


def test_the_output_folder_is_ignored_by_git_and_the_fonts_are_bundled():
    assert re.search(r"^output/$", (ROOT / ".gitignore").read_text(encoding="utf-8"), re.MULTILINE)
    fonts = ROOT / "app" / "fonts"
    for name in ("EBGaramond-Regular.ttf", "EBGaramond-Bold.ttf", "EBGaramond-Italic.ttf", "EBGaramond-BoldItalic.ttf", "OFL.txt", "README.md"):
        assert (fonts / name).is_file()
    assert "SIL OPEN FONT LICENSE" in (fonts / "OFL.txt").read_text(encoding="utf-8")


# --- the length rule of the prompt ---------------------------------------------------------------------------------

def test_the_prompt_states_the_length_budgets_it_measured():
    text = (ROOT / "prompts" / "scoring_prompt.md").read_text(encoding="utf-8")
    assert "Prompt version: 1.18.0" in text
    assert "at most 350 words in total" in text and "at most 650 words in total" in text and "at most 30 bullets" in text
    assert "one A4 page" in text and "two" in text


def words_in(letter_doc):
    b = letter_doc["blocks"]
    parts = [b["opening"], b["lead_in"], b["skills"], b["closing"]] + [f"{a['label']} {a['text']}" for a in b["achievements"]]
    return sum(len(p.split()) for p in parts)


@pytest.mark.parametrize("budget", [330, 350])        # the aim of prompt 1.7.0 (290 to 330) and its hard limit (350)
def test_a_letter_at_the_prompt_budget_fits_one_page_even_with_long_names(tmp_path, budget):
    d = copy.deepcopy(prompt_example()["cover_letter"])
    d["contact_name"] = "Dr. Alexandra Müller-Schwarzenbach"
    prose = d["blocks"]["skills"].split()                          # ordinary words, about 5.5 letters on average
    while words_in(d) < budget:
        d["blocks"]["skills"] += " " + prose[words_in(d) % len(prose)]
    assert words_in(d) == budget
    r = pdf.render_cover_letter(d, job("Schweizerische Nationalbank Beteiligungsgesellschaft AG", ("Zürich-Oerlikon",)),
                                FACTS, DAY, tmp_path / "l.pdf")
    assert r.pages == 1


def test_a_350_word_letter_with_a_very_long_address_block_still_fits_one_page(tmp_path):
    """The hard limit of 350 words with the longest header the layout can get: a long salutation, an employer name
    that wraps nowhere (it is one line) and a long city. The page keeps room for a few more lines."""
    d = copy.deepcopy(prompt_example()["cover_letter"])
    d["contact_name"] = "Prof. Dr. Alexandra Müller-Schwarzenbach-Hohenlohe"
    prose = d["blocks"]["closing"].split()
    while words_in(d) < 350:
        d["blocks"]["closing"] += " " + prose[words_in(d) % len(prose)]
    assert words_in(d) == 350
    company = "Schweizerische Nationalbank Beteiligungsgesellschaft Aktiengesellschaft"
    r = pdf.render_cover_letter(d, job(company, ("Zürich-Oerlikon, Switzerland",)), FACTS, DAY, tmp_path / "l.pdf")
    assert r.pages == 1
    reader = PdfReader(str(r.path))
    ys = []
    reader.pages[0].extract_text(visitor_text=lambda text, cm, tm, font, size: ys.append(tm[5]) if text.strip() else None)
    assert min(ys) > 15 * 72 / 25.4              # the last line (the name) is above the 15 mm bottom margin, with room to spare


def test_a_cv_at_the_prompt_budget_fits_two_pages(tmp_path):
    doc = full_cv("resume")
    bullets = [b for e in doc["experience"] for b in e["bullets"]]
    words = len(doc["summary"].split()) + sum(len(b["text"].split()) for b in bullets)
    assert len(bullets) <= 24 and words <= 720
    r = pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
    assert r.pages <= 2


# --- v0.45.0: margins, justification, sections that stay together, raised budgets ------------------------------------
# Decided by the user on 1 Nov 2026: "margins - I would like to use as much page as possible ... I am missing justification
# too ... especially in the CV case, when you have whole part, like education and languages, it should stay on one page".

def text_positions(path, page=0):
    """(x, y) in points of the start of every non-empty text fragment of a page."""
    found = []
    PdfReader(str(path)).pages[page].extract_text(
        visitor_text=lambda text, cm, tm, font, size: found.append((tm[4], tm[5])) if text.strip() else None)
    return found


def test_the_letter_margins_are_18_mm_left_and_right_and_15_mm_at_the_top(tmp_path):
    assert (pdf.LETTER_MARGIN_X, pdf.LETTER_MARGIN_TOP, pdf.LETTER_MARGIN_BOTTOM) == (18.0, 15.0, 15.0)
    r = pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "l.pdf")
    mm = 72 / 25.4
    found = text_positions(r.path)
    assert min(x for x, _y in found) == pytest.approx(18 * mm, abs=0.5)             # text starts at the margin, no inner padding
    first_baseline = 297 * mm - max(y for _x, y in found)                          # distance of the first baseline from the top edge
    assert 15 * mm < first_baseline < 22 * mm                                       # the name (16 pt) sits right under the 15 mm margin


def justified_lines(path, page=0) -> int:
    """Number of text-showing operations of a page that carry spacing adjustments between words (a TJ operator with
    numbers): fpdf2 writes a justified line this way, and a plain line, such as the last line of a paragraph, with Tj."""
    reader = PdfReader(str(path))
    operations = ContentStream(reader.pages[page].get_contents(), reader).operations
    return sum(1 for operands, op in operations
               if op == b"TJ" and any(isinstance(x, (int, float)) for x in operands[0]))


def line_count(text: str, width: float, size: float, c_margin: float) -> int:
    probe = pdf._Pdf("t", "t", DAY)
    probe.add_page()
    probe.set_font(pdf.FAMILY, "", size)
    probe.c_margin = c_margin
    return len(probe.multi_cell(width, 5, text, align="J", dry_run=True, output=MethodReturnValue.LINES))


def test_a_letter_paragraph_is_justified_and_its_last_line_is_not_stretched(tmp_path):
    d = copy.deepcopy(prompt_example()["cover_letter"])
    b = d["blocks"]
    b["opening"] = " ".join([b["opening"]] * 2)                      # a paragraph of several lines; every other block is short
    b["lead_in"], b["skills"], b["closing"] = "Four results.", "Short.", "Short."
    for a in b["achievements"]:
        a["label"], a["text"] = "Label", "Short text."
    lines = line_count(b["opening"], 210 - 2 * pdf.LETTER_MARGIN_X, pdf.LETTER_PT, 0)
    assert lines >= 5
    r = pdf.render_cover_letter(d, job(), FACTS, DAY, tmp_path / "l.pdf")
    assert justified_lines(r.path) == lines - 1                      # every line but the last; a stretched last line would make it `lines`


def test_a_cv_summary_is_justified_and_its_last_line_is_not_stretched(tmp_path):
    doc = full_cv("resume")
    doc["summary"] = " ".join([doc["summary"]] * 2)
    doc["experience"] = [{"experience_id": "exp-contoso", "bullets": [{"fact_id": "x", "text": "A short line."}]}]
    lines = line_count(doc["summary"], 210 - 2 * pdf.MARGIN_X, pdf.CV_PT, 1.0)
    assert lines >= 5
    r = pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
    assert justified_lines(r.path) == lines - 1


def test_every_paragraph_of_both_documents_is_drawn_with_align_j(tmp_path, monkeypatch):
    aligns = []
    original = pdf._Pdf.multi_cell

    def spy(self, *args, **kwargs):
        aligns.append(kwargs.get("align"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pdf._Pdf, "multi_cell", spy)
    pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "l.pdf")
    letter_calls = len(aligns)
    pdf.render_cv(full_cv(), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
    assert letter_calls >= 8 and len(aligns) > letter_calls + 20      # letter paragraphs and achievements; summary, bullets, languages
    assert set(aligns) == {"J"}                                       # none left to the default of a later fpdf2 version


def test_justifying_keeps_every_word_of_both_documents(tmp_path):
    b = prompt_example()["cover_letter"]["blocks"]
    t = text_of(pdf.render_cover_letter(prompt_example()["cover_letter"], job(), FACTS, DAY, tmp_path / "l.pdf").path)
    for part in [b["opening"], b["lead_in"], b["skills"], b["closing"]] + [f"{a['label']}: {a['text']}" for a in b["achievements"]]:
        assert " ".join(part.split()) in t
    doc = full_cv()
    t = text_of(pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).path)
    assert " ".join(doc["summary"].split()) in t
    for entry in doc["experience"]:
        for bullet in entry["bullets"]:
            assert " ".join(bullet["text"].split()) in t


def unit_pdf(y: float) -> pdf._Pdf:
    p = pdf._Pdf("t", "t", DAY)
    p.set_margins(pdf.MARGIN_X, 9, pdf.MARGIN_X)
    p.set_auto_page_break(True, 9)
    p.add_page()
    p.set_y(y)
    return p


def unit_lines(n: int, gap: float = 0.0):
    """A unit of `n` lines and `gap` mm of empty space below the last one (a line break, as the CV leaves between jobs)."""
    def draw(p):
        p.set_font(pdf.FAMILY, "", pdf.CV_PT)
        for i in range(n):
            p.cell(0, pdf.CV_LINE, f"unit line {i}", new_x="LMARGIN", new_y="NEXT")
        p.ln(gap)
    return draw


def pages_of(p, tmp_path) -> list[str]:
    p.output(str(tmp_path / "u.pdf"))
    return [" ".join(page.extract_text().split()) for page in PdfReader(str(tmp_path / "u.pdf")).pages]


def test_a_unit_that_fits_stays_where_it_is(tmp_path):
    p = unit_pdf(100)
    pdf._keep_together(p, pdf._probe(p), unit_lines(10))
    assert p.page_no() == 1 and p.get_y() == pytest.approx(100 + 10 * pdf.CV_LINE)


@pytest.mark.parametrize("start", [250, 270, 285])           # the page break comes after 288 mm; a unit of 43 mm cannot fit from here
def test_a_unit_that_does_not_fit_starts_on_the_next_page_as_a_whole(tmp_path, start):
    p = unit_pdf(start)
    pdf._keep_together(p, pdf._probe(p), unit_lines(10))
    assert p.page_no() == 2 and p.get_y() == pytest.approx(9 + 10 * pdf.CV_LINE)
    pages = pages_of(p, tmp_path)
    assert pages[0] == "" and all(f"unit line {i}" in pages[1] for i in range(10))     # nothing of it is left on page 1


def test_the_empty_space_below_a_unit_does_not_push_it_to_the_next_page(tmp_path):
    p = unit_pdf(250)                                          # 250 + 8 x 4.3 + 6 = 290.4 > 288, but 6 mm of it are empty space below
    pdf._keep_together(p, pdf._probe(p), unit_lines(8, gap=6), tail=6)
    assert p.page_no() == 1 and p.get_y() == pytest.approx(250 + 8 * pdf.CV_LINE + 6)
    q = unit_pdf(250)
    pdf._keep_together(q, pdf._probe(q), unit_lines(8, gap=6))         # without the allowance the same unit moves
    assert q.page_no() == 2


def test_a_unit_longer_than_a_page_starts_on_a_fresh_page_and_then_breaks(tmp_path):
    p = unit_pdf(100)
    pdf._keep_together(p, pdf._probe(p), unit_lines(80))               # 344 mm: taller than any page (the documented limit)
    pages = pages_of(p, tmp_path)
    assert len(pages) == 3 and pages[0] == "" and "unit line 0" in pages[1] and "unit line 79" in pages[2]
    top = unit_pdf(9)                                          # at the top of a page it is drawn as it is: no empty first page
    pdf._keep_together(top, pdf._probe(top), unit_lines(80))
    pages = pages_of(top, tmp_path)
    assert len(pages) == 2 and "unit line 0" in pages[0]


def cv_with_bullets(n: int) -> dict:
    """The full CV of the fact file, cut after its n-th bullet."""
    doc = full_cv("resume")
    flat = [(e["experience_id"], b) for e in doc["experience"] for b in e["bullets"]]
    grouped: dict[str, list] = {}
    for experience_id, bullet in flat[:n]:
        grouped.setdefault(experience_id, []).append(bullet)
    doc["experience"] = [{"experience_id": k, "bullets": v} for k, v in grouped.items()]
    return doc


def test_no_part_of_the_cv_is_split_by_the_page_break(tmp_path):
    """Sweep the CV from 8 to 24 bullets, so that the page break moves through the jobs, education and skills. Whole
    parts stay on one page: education with languages, the technical skills, and every job with all its bullets. (With the
    old layout the sweep split education at 17 and 18 bullets and the skills at 13 and 14. It started at 10 until the
    skills grew in v0.52.1; since then they reach page 2 from 10 bullets.)"""
    by_id = {e["id"]: e for e in FACTS["experience"]}
    education_page, skills_page = set(), set()
    for n in range(8, 25):
        doc = cv_with_bullets(n)
        r = pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
        pages = [" ".join(p.extract_text().split()) for p in PdfReader(str(r.path)).pages]
        assert r.pages == len(pages) <= 2, n

        def page_of(needle: str) -> int:
            found = [i for i, t in enumerate(pages) if needle in t]
            assert len(found) == 1, (n, needle, found)
            return found[0]

        education = {page_of(s) for s in ("EDUCATION & LANGUAGES", "Tailspin University of Economics", "High School Diploma",
                                          "English: fluent | Polish: fluent | German: basic")}
        skills = {page_of(s) for s in ("TECHNICAL SKILLS", "Reporting & Data", "Testing & Collaboration")}
        assert len(education) == 1 and len(skills) == 1, (n, education, skills)
        education_page |= education
        skills_page |= skills
        for entry in doc["experience"]:
            exp = by_id[entry["experience_id"]]
            last_words = " ".join(entry["bullets"][-1]["text"].split()[-5:])
            assert page_of(pdf.date_range(exp["start"], exp["end"])) == page_of(last_words), (n, entry["experience_id"])
    assert education_page == {1} and skills_page == {1}             # since 2 Nov 2026 both always start on page 2


def test_education_and_skills_start_on_page_2_and_page_1_holds_header_summary_and_experience(tmp_path):
    for n in (3, 8, 24):
        r = pdf.render_cv(cv_with_bullets(n), FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
        pages = [" ".join(p.extract_text().split()) for p in PdfReader(str(r.path)).pages]
        assert len(pages) == 2 and "WORK EXPERIENCE" in pages[0], n
        for needle in ("EDUCATION & LANGUAGES", "TECHNICAL SKILLS"):
            assert needle in pages[1] and needle not in pages[0], (n, needle)


def test_the_work_experience_heading_stays_with_its_first_job(tmp_path):
    doc = full_cv("resume")
    doc["summary"] = " ".join([doc["summary"]] * 3)                  # push the heading towards the end of page 1
    for extra in range(0, 12):
        doc["summary"] += " Extra words that make the summary a little longer."
        r = pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY)
        pages = [" ".join(p.extract_text().split()) for p in PdfReader(str(r.path)).pages]
        heading = [i for i, t in enumerate(pages) if "WORK EXPERIENCE" in t]
        first_job = [i for i, t in enumerate(pages) if pdf.date_range("2025-07", "present") in t]
        assert heading == first_job and len(heading) == 1, extra


def padded_cv(variant: str, words: int, extra_bullets: int = 0) -> tuple[dict, int]:
    """The full CV of the fact file with `extra_bullets` copies of earlier bullets, every bullet then padded with ordinary
    words (evenly, so that as many bullets as possible wrap onto another line) up to `words` words. Returns it and the
    number of bullets."""
    doc = full_cv(variant)
    doc["experience"][1]["bullets"] += copy.deepcopy(doc["experience"][1]["bullets"][:extra_bullets])
    bullets = [b for e in doc["experience"] for b in e["bullets"]]
    filler = "and coordinated the results with the teams involved".split()
    i = 0
    while len(doc["summary"].split()) + sum(len(b["text"].split()) for b in bullets) < words:
        bullets[i % len(bullets)]["text"] += " " + filler[(i // len(bullets)) % len(filler)]
        i += 1
    assert len(doc["summary"].split()) + sum(len(b["text"].split()) for b in bullets) == words
    return doc, len(bullets)


@pytest.mark.parametrize("variant", ["resume", "resume_cs"])
def test_a_cv_at_the_raised_budget_of_prompt_1_7_0_still_fits_two_pages(tmp_path, variant):
    """720 words in 24 bullets (the 650 words of 1.3.0 plus about 10%), with the sections kept together."""
    doc, bullets = padded_cv(variant, 720)
    assert bullets == 24
    assert pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).pages == 2


def test_why_24_bullets_were_the_limit_of_prompts_1_7_0_to_1_10_0(tmp_path):
    """Measured on 1 Nov 2026 and stated in the prompt: with the sections kept together a part that does not fit
    leaves blank space at the end of page 1, so 26 bullets with the same 720 words run onto a third page."""
    doc, bullets = padded_cv("resume", 720, extra_bullets=2)
    assert bullets == 26
    assert pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).pages == 3


# --- prompt 1.11.0 (3 Nov 2026): every experience is on the CV, up to 26 bullets and 650 words ---

ALL_EXPERIENCES = ["exp-career-transition", "exp-contoso", "exp-northwind-zurich", "exp-litware", "exp-northwind-wroclaw",
                   "exp-cs-coo", "exp-cs-workforce", "exp-cs-servicedesk"]


def cv_with_all_experiences(variant: str, per_experience: list[int], words: int) -> dict:
    """All 8 experiences of the fact file with the given number of bullets each (the first bullets that have a text for the
    variant), every bullet then padded evenly with ordinary words up to `words` words in total."""
    plan = dict(zip(ALL_EXPERIENCES, per_experience))
    exp = []
    for e in FACTS["experience"]:
        bullets = [{"fact_id": b["id"], "text": b["texts"][variant]} for b in e["bullets"] if variant in b.get("texts", {})]
        exp.append({"experience_id": e["id"], "bullets": bullets[:plan[e["id"]]]})
    doc = {"variant": variant, "summary": " ".join(VARIANTS["variants"][variant]["summary"].split()), "experience": exp}
    bullets = [b for e in exp for b in e["bullets"]]
    filler = "and coordinated the results with the teams involved".split()
    total = lambda: len(doc["summary"].split()) + sum(len(b["text"].split()) for b in bullets)
    i = 0
    while total() < words:
        bullets[i % len(bullets)]["text"] += " " + filler[(i // len(bullets)) % len(filler)]
        i += 1
    return doc


def realistic_cv(variant: str, per_experience: list[int], words: int, seed: int) -> dict:
    """All 8 experiences with the given number of bullets each, chosen at random (seeded) from the real bullets of the fact
    file, so the lengths are natural; swapped for others until the word total (summary plus bullet texts, split on
    whitespace) is within 2 of `words`, then a few words are trimmed or added to reach it exactly."""
    rng = random.Random(seed)
    plan = dict(zip(ALL_EXPERIENCES, per_experience))
    avail = {e["id"]: [(b["id"], b["texts"][variant]) for b in e["bullets"] if variant in b.get("texts", {})]
             for e in FACTS["experience"]}
    summary = " ".join(VARIANTS["variants"][variant]["summary"].split())
    sel = {i: rng.sample(avail[i], plan[i]) for i in ALL_EXPERIENCES}
    count = lambda t: len(t.split())
    total = lambda: count(summary) + sum(count(x[1]) for i in ALL_EXPERIENCES for x in sel[i])
    for _ in range(400):
        if abs(total() - words) <= 2:
            break
        i = rng.choice(ALL_EXPERIENCES)
        k, cand = rng.randrange(plan[i]), rng.choice(avail[i])
        if cand in sel[i]:
            continue
        before, old = abs(total() - words), sel[i][k]
        sel[i][k] = cand
        if abs(total() - words) > before:
            sel[i][k] = old
    exp = [{"experience_id": i, "bullets": [{"fact_id": a, "text": b} for a, b in sel[i]]} for i in ALL_EXPERIENCES]
    doc = {"variant": variant, "summary": summary, "experience": exp}
    bullets = [b for e in exp for b in e["bullets"]]
    tails = ["across the teams involved", "for the business stakeholders", "with the key stakeholders"]
    cv_total = lambda: count(summary) + sum(count(b["text"]) for b in bullets)
    for _ in range(200):
        d = words - cv_total()
        if d == 0:
            break
        b = rng.choice(bullets)
        if d > 0:
            b["text"] += " " + " ".join(rng.choice(tails).split()[:min(d, 4)])
        elif count(b["text"]) > 12:
            b["text"] = " ".join(b["text"].split()[:count(b["text"]) + max(d, -3)])
    assert cv_total() == words
    return doc


@pytest.mark.parametrize("variant", ["resume", "resume_cs"])
@pytest.mark.parametrize("per_experience", [[3, 9, 5, 3, 3, 1, 1, 1], [3, 10, 4, 3, 3, 1, 1, 1], [2, 10, 7, 3, 1, 1, 1, 1],
                                            [1, 10, 10, 1, 1, 1, 1, 1], [3, 8, 10, 1, 1, 1, 1, 1]])
def test_a_cv_with_all_8_experiences_and_26_bullets_fits_two_pages_at_650_words(tmp_path, variant, per_experience):
    """Measured on 3 Nov 2026 over 84 realistic spreads of 26 bullets (both variants, real bullets of natural length):
    none runs onto a third page at 650 words, 2 do at 680."""
    doc = realistic_cv(variant, per_experience, 650, seed=sum(per_experience) + per_experience[1])
    assert sum(per_experience) == 26 and len(doc["experience"]) == 8 and all(e["bullets"] for e in doc["experience"])
    assert sum(len(e["bullets"]) for e in doc["experience"]) == 26
    assert pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).pages == 2


def test_26_bullets_with_720_words_can_run_onto_a_third_page(tmp_path):
    """720 words are too many for 26 bullets: with even padding 59 of 84 spreads need three pages, this one (many bullets
    in the two longest jobs) among them."""
    doc = cv_with_all_experiences("resume", [1, 10, 10, 1, 1, 1, 1, 1], 720)
    assert pdf.render_cv(doc, FACTS, VARIANTS, tmp_path / "c.pdf", PHOTO, DAY).pages == 3
