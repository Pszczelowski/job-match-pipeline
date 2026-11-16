"""Phase 3 slice 3b: tailored documents in the results file (schema 1.1), checked and stored."""
import copy
import json
import re
from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator

from app import cli, db, documents
from app import traceability as tr
from app.batch import build_batch, exchange_dir
from app.config import ROOT, ConfigError, load_config
from app.report import collect
from app.results import import_results
from app.schemas import BATCH_SCHEMA, DOCUMENTS, RESULTS_SCHEMA
from tests.test_batch_results import NOW, PROMPT, add_jobs, batch, count, results_file, score

DESCRIPTION = "Business analyst for payments. Your contact is Ms Muster, Head of Operations. You write requirements."


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


# Neutral closing prose that makes the fixture letter land inside the 290-350 word range (so no `length` warning appears by accident);
# it makes no claim about the user, a skill, a number or a place.
FILLER = ("Thank you for taking the time to read my application. I like to understand a problem properly before I propose a "
          "solution, and I like to write down what was agreed so that everybody works from the same page. " * 3).strip()


def letter(company="Acme", contact=None):
    return {"contact_name": contact, "blocks": {
        "opening": f"I am writing to apply for the Analyst position at {company}. With over 10 years in financial institutions in "
                   "Switzerland and abroad, I combine requirements work, end-to-end testing and process automation.",
        "lead_in": f"Three results from my work show how I would contribute at {company}:",
        "achievements": [
            {"label": "Process Automation", "facts": ["exp-northwind-zurich-automation-letter"],
             "text": "Developed scripts to automate processes, in result, increasing team productivity by 20% and eliminating "
                     "over 500 manual hours annually at Northwind."},
            {"label": "Requirements Delivery", "facts": ["exp-contoso-requirements"],
             "text": "Worked closely with IT and Operations at Contoso Fund Services to define Fund Platform requirements "
                     "and translate business needs into clear specifications."},
            {"label": "Testing Excellence", "facts": ["exp-contoso-testing-letter"],
             "text": "Achieved over 90% test accuracy in a fast-paced environment at Contoso Fund Services while designing and "
                     "executing detailed test plan. In result, my actions saved more than 50 hours annually per FTE by optimizing the process."},
            {"label": "Data Validation", "facts": ["exp-contoso-sql"],
             "text": "Used SQL, including CTEs, to validate reports and check outputs."}],
        "skills": "I combine Python, SQL and VBA with a solid understanding of business processes. I write requirements that "
                  "developers can build from and that testers can verify.",
        "closing": FILLER + f" I would welcome the opportunity to discuss in an interview how my skills in business analysis, testing and "
                   f"automation can contribute to the team at {company}."}}


CV = {"variant": "resume",
      "summary": "I'm a BA who also tests and understands developers. I write the requirement, verify it, and script away daily manual work.",
      "experience": [
          {"experience_id": "exp-contoso", "bullets": [
              {"fact_id": "exp-contoso-requirements",
               "text": "Requirements Gathering: Worked with IT and Operations to define and document requirements for the Fund Platform."},
              {"fact_id": "exp-contoso-testing",
               "text": "Testing Management: Wrote detailed test cases and executed End-to-End UAT in Xray to ensure platform stability."}]},
          {"experience_id": "exp-northwind-zurich", "bullets": [
              {"fact_id": "exp-northwind-zurich-automation",
               "text": "Automation Development: Developed Python & VBA scripts to automate manual tasks, saving over 500 hours annually."}]},
          {"experience_id": "exp-career-transition", "bullets": [
              {"fact_id": "exp-career-transition-digital",
               "text": "Digital Transformation: Researching AI integration strategies to enhance automations and workflow output quality."}]},
          {"experience_id": "exp-litware", "bullets": [
              {"fact_id": "exp-litware-reporting-ownership",
               "text": "Reporting Ownership: Owned the service management reporting, including its automation."}]},
          {"experience_id": "exp-northwind-wroclaw", "bullets": [
              {"fact_id": "exp-northwind-wroclaw-vba-tableau",
               "text": "VBA and Tableau reports that enabled Sr. management to shape Real Estate utilization, saving 200 hours annually."}]},
          {"experience_id": "exp-cs-coo", "bullets": [
              {"fact_id": "exp-cs-coo-budget-reporting",
               "text": "Budgeting and reporting: Owned the review of the hiring process, the budgeting and the reporting."}]},
          {"experience_id": "exp-cs-workforce", "bullets": [
              {"fact_id": "exp-cs-workforce-planning",
               "text": "Coordinated global hiring reviews, headcount forecasting, and strategic business planning for Group IT"}]},
          {"experience_id": "exp-cs-servicedesk", "bullets": [
              {"fact_id": "exp-cs-servicedesk-support",
               "text": "Provided technical support and ticket management for global partners in a shift-based environment"}]}],
      "skills": ["skills-testing-collab", "skills-automation-dev"]}


def docs(contact=None):
    return {"cover_letter": letter(contact=contact), "cv": copy.deepcopy(CV)}


def prepare(conn, cfg, scores, description=DESCRIPTION, run_id="run1", start=1):
    """Jobs with the given overall scores in one batch; returns the job ids."""
    ids = add_jobs(conn, cfg, len(scores), start=start, description=description)
    batch(conn, cfg, run_id)
    return ids


def entries(ids, scores, with_docs=(), contact=None):
    return [score(i, s, **({"documents": docs(contact)} if i in with_docs else {})) for i, s in zip(ids, scores)]


def send(cfg, ids, scores, with_docs=(), version="1.1", contact=None, name=None, run_id="run1"):
    return results_file(cfg, run_id, entries(ids, scores, with_docs, contact), schema_version=version, name=name)


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT job_id, kind, status, findings FROM documents ORDER BY job_id, kind")]


def kinds(conn, job_id, kind):
    r = conn.execute("SELECT findings FROM documents WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone()
    return [f["kind"] for f in json.loads(r["findings"]) if f["kind"] != "line_ending"]      # the line-ending warning has its own tests


# --- schema 1.1 ---------------------------------------------------------------------------------

def test_a_1_0_file_still_imports_exactly_as_before(conn, cfg):
    ids = prepare(conn, cfg, [80, 70])
    r = import_results(conn, cfg, send(cfg, ids, [80, 70], version="1.0"), NOW)
    assert r.status == "imported" and r.scored == ids and r.documents == [] and r.documents_missing == []
    assert not any("document" in w for w in r.warnings) and count(conn, "documents") == 0


def test_a_1_0_file_may_not_carry_documents(conn, cfg):
    ids = prepare(conn, cfg, [80])
    r = import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids, version="1.0"), NOW)
    assert r.status == "rejected" and "documents need schema_version 1.1" in r.reason and count(conn, "scores") == 0


def test_batch_file_declares_1_1_and_the_document_rule(conn, cfg):
    add_jobs(conn, cfg, 1)
    doc = json.loads(batch(conn, cfg).path.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(BATCH_SCHEMA).iter_errors(doc)) == []
    assert doc["schema_version"] == "1.1" and doc["tailoring"] == {"documents_top_n": 10, "min_score": 60, "require_description": True}
    assert doc["schemas"]["documents"] == DOCUMENTS and doc["schemas"]["results"] == RESULTS_SCHEMA


def test_the_tailoring_rule_in_the_batch_follows_the_config(conn, cfg):
    cfg["tailoring"].update(documents_top_n=3, min_score=75)
    add_jobs(conn, cfg, 1)
    assert json.loads(batch(conn, cfg).path.read_text(encoding="utf-8"))["tailoring"] == {"documents_top_n": 3, "min_score": 75, "require_description": True}


def test_the_example_documents_of_the_schema_are_valid(conn, cfg):
    assert list(Draft202012Validator(RESULTS_SCHEMA).iter_errors(
        {"schema_version": "1.1", "run_id": "r", "model": "m", "prompt_version": "v", "top": [],
         "results": [score(1, 70, documents=docs(contact="Ms Muster"))]})) == []


# --- eligible jobs: stored as draft ---------------------------------------------------------------

def test_valid_documents_are_stored_as_drafts(conn, cfg):
    ids = prepare(conn, cfg, [80, 70])
    r = import_results(conn, cfg, send(cfg, ids, [80, 70], with_docs=ids), NOW)
    assert r.status == "imported" and r.documents_ignored == [] and r.documents_missing == []
    assert sorted((j, k, s, n) for j, k, s, n in r.documents) == sorted((j, k, "draft", 0) for j in ids for k in ("cv", "cover_letter"))
    got = [dict(x) for x in conn.execute("SELECT * FROM documents ORDER BY job_id, kind")]
    assert len(got) == 4 and {g["status"] for g in got} == {"draft"} and all(g["findings"] == "[]" for g in got)
    assert {g["generated_on"] for g in got} == {"2026-11-02"} and {g["run_id"] for g in got} == {"run1"}
    imp = conn.execute("SELECT id FROM imports WHERE status = 'imported'").fetchone()["id"]
    assert {g["import_id"] for g in got} == {imp}
    assert "blocks" in json.loads(got[0]["content"]) and json.loads(got[1]["content"])["variant"] == "resume"


def test_the_import_summary_records_the_documents(conn, cfg):
    ids = prepare(conn, cfg, [80])
    import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids), NOW)
    summary = json.loads(conn.execute("SELECT summary FROM imports").fetchone()["summary"])
    assert len(summary["documents"]) == 2 and summary["documents_ignored"] == [] and summary["documents_missing"] == []


def test_a_known_contact_in_the_description_is_accepted(conn, cfg):
    ids = prepare(conn, cfg, [80])
    r = import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids, contact="  ms   MUSTER "), NOW)
    assert [s for _, _, s, _ in r.documents] == ["draft", "draft"]


def test_numbers_in_the_job_title_are_not_findings(conn, cfg):
    ids = add_jobs(conn, cfg, 1, title="Business Analyst (80-100%) Graduate Programme 2027", description=DESCRIPTION)
    batch(conn, cfg)
    r = import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids), NOW)
    assert [s for _, _, s, _ in r.documents] == ["draft", "draft"]


@pytest.mark.parametrize("title", ["(Senior) IAM Business Analyst (80-100%)",
                                   "Business Manager, IB Unified Global Markets Distribution COO"])
def test_an_unknown_acronym_in_the_job_title_is_not_a_finding(conn, cfg, title):
    ids = add_jobs(conn, cfg, 1, title=title, description=DESCRIPTION)
    batch(conn, cfg)
    r = import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids), NOW)
    assert [s for _, _, s, _ in r.documents] == ["draft", "draft"]


def test_the_acronym_of_the_job_title_is_still_a_finding_in_the_body(conn, cfg):
    ids = add_jobs(conn, cfg, 1, title="(Senior) IAM Business Analyst (80-100%)", description=DESCRIPTION)
    batch(conn, cfg)
    d = mutate(lambda x: (x["cover_letter"]["blocks"].update(skills="I combine Python and SQL with IAM and a solid understanding of processes."),
                          x["cv"].update(summary="I'm a BA who knows IAM and tests and understands developers.")))
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1"), NOW)
    for kind in ("cover_letter", "cv"):
        found = json.loads(conn.execute("SELECT findings FROM documents WHERE kind = ?", (kind,)).fetchone()["findings"])
        assert [(f["kind"], f["text"]) for f in found] == [("skill", "IAM")]
        assert found[0]["where"] == ("skills" if kind == "cover_letter" else "summary")
    assert {r["kind"]: r["status"] for r in rows(conn)} == {"cover_letter": "blocked", "cv": "blocked"}


def test_no_contact_is_fine(conn, cfg):
    cfg["tailoring"]["require_description"] = False        # these jobs have no description on purpose
    ids = prepare(conn, cfg, [80], description="")
    r = import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids), NOW)
    assert [s for _, _, s, _ in r.documents] == ["draft", "draft"]


# --- blocked documents ---------------------------------------------------------------------------

def mutate(path_fn):
    """A documents object after path_fn changed it."""
    d = docs()
    path_fn(d)
    return d


FAKES = [
    ("number", lambda d: d["cover_letter"]["blocks"]["achievements"][0].update(text="Saved 5 million CHF by automating reports at Northwind."), "cover_letter"),
    ("employer", lambda d: d["cover_letter"]["blocks"].update(skills="I worked at Goldman Sachs on pricing and I know Python."), "cover_letter"),
    ("skill", lambda d: d["cover_letter"]["blocks"].update(skills="I run Kubernetes clusters and write Python."), "cover_letter"),
    ("number", lambda d: d["cv"]["experience"][0]["bullets"][0].update(text="Requirements Gathering: defined requirements for 12 platforms."), "cv"),
    ("skill", lambda d: d["cv"]["experience"][0]["bullets"][1].update(text="Testing Management: automated tests with Kubernetes."), "cv"),
    ("employer", lambda d: d["cv"].update(summary="I spent ten years at Goldman Sachs and Northwind."), "cv"),
    ("certificate", lambda d: d["cv"].update(summary="I am an Oracle Certified Associate who writes SQL."), "cv"),
]


@pytest.mark.parametrize("kind, change, which", FAKES)
def test_an_invented_fact_blocks_the_document(conn, cfg, kind, change, which):
    ids = prepare(conn, cfg, [80])
    d = mutate(change)
    path = results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1")
    r = import_results(conn, cfg, path, NOW)
    assert r.status == "imported"
    assert kind in kinds(conn, ids[0], which)
    status = {k: s for _, k, s, _ in r.documents}
    assert status[which] == "blocked" and status["cv" if which == "cover_letter" else "cover_letter"] == "draft"
    assert any("documents blocked" in w for w in r.warnings)
    assert count(conn, "scores") == 1 and conn.execute("SELECT status FROM jobs").fetchone()[0] in ("scored", "not_selected")


# Prompt 1.11.0 (3 Nov 2026): the CV lists every experience of the fact file, each with at least one bullet.
def test_a_cv_that_omits_an_experience_of_the_fact_file_is_blocked():
    facts, index = tr.load_facts(), documents.load_fact_index()
    job = {"title": "Business Analyst", "company": "Acme", "description": ""}
    doc = copy.deepcopy(CV)
    doc["experience"] = [e for e in doc["experience"] if e["experience_id"] != "exp-cs-coo"]
    findings = documents.check_cv(doc, job, facts, index, datetime(2026, 11, 1).date())
    assert [(f["kind"], f["text"]) for f in findings] == [("missing_experience", "exp-cs-coo")]


def test_an_experience_without_a_bullet_is_blocked():
    facts, index = tr.load_facts(), documents.load_fact_index()
    job = {"title": "Business Analyst", "company": "Acme", "description": ""}
    doc = copy.deepcopy(CV)
    doc["experience"][-1]["bullets"] = []
    findings = documents.check_cv(doc, job, facts, index, datetime(2026, 11, 1).date())
    assert [(f["kind"], f["text"]) for f in findings] == [("empty_experience", "exp-cs-servicedesk")]


def test_a_cv_with_every_experience_and_one_bullet_each_passes_the_check():
    facts, index = tr.load_facts(), documents.load_fact_index()
    job = {"title": "Business Analyst", "company": "Acme", "description": ""}
    assert {e["experience_id"] for e in CV["experience"]} == set(index.experience_bullets)
    assert documents.check_cv(copy.deepcopy(CV), job, facts, index, datetime(2026, 11, 1).date()) == []


# Prompt 1.7.0 (1 Nov 2026): the CV summary is written for the job; it is checked like a bullet, never exempted.
TAILORED_SUMMARY = ("I'm a business analyst who writes the requirement, tests it end to end and scripts away the manual work around "
                    "it. At Contoso Fund Services I defined requirements for the Fund Platform with IT and Operations and "
                    "executed End-to-End UAT in Xray. At Northwind I automated manual tasks with Python and VBA, saving over 500 "
                    "manual hours per year. I work best where a process needs one person who can specify it, verify it and improve it.")


def test_a_summary_written_for_the_job_from_the_facts_passes(conn, cfg):
    ids = prepare(conn, cfg, [80])
    d = mutate(lambda x: x["cv"].update(summary=TAILORED_SUMMARY))
    assert d["cv"]["summary"] != CV["summary"] and 3 <= len(re.findall(r"[.!?](?:\s|$)", TAILORED_SUMMARY)) <= 4
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1"), NOW)
    assert {k: (s, n) for _, k, s, n in r.documents}["cv"] == ("draft", 0)


@pytest.mark.parametrize("sentence, kind", [
    ("It saved 900 manual hours a year.", "number"),
    ("I also run Kubernetes clusters.", "skill"),
    ("Earlier I worked at Goldman Sachs.", "employer"),
    ("I hold the Oracle Certified Associate certificate.", "certificate"),
    ("My German is fluent.", "language_claim")])
def test_a_tailored_summary_is_checked_like_a_bullet_and_blocks_the_cv(conn, cfg, sentence, kind):
    ids = prepare(conn, cfg, [80])
    d = mutate(lambda x: x["cv"].update(summary=f"{TAILORED_SUMMARY} {sentence}"))
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1"), NOW)
    found = json.loads(conn.execute("SELECT findings FROM documents WHERE kind = 'cv'").fetchone()["findings"])
    assert {(f["kind"], f["where"]) for f in found if f["kind"] == kind} == {(kind, "summary")}
    assert {k: s for _, k, s, _ in r.documents} == {"cover_letter": "draft", "cv": "blocked"}


def test_the_summary_is_a_checked_block_of_the_cv_and_not_exempt():
    facts, index = tr.load_facts(), documents.load_fact_index()
    job = {"title": "Business Analyst", "company": "Acme", "description": ""}
    doc = copy.deepcopy(CV)
    doc["summary"] = "I saved 900 manual hours a year."
    findings = documents.check_cv(doc, job, facts, index, datetime(2026, 11, 1).date())
    assert [(f["kind"], f["where"]) for f in findings] == [("number", "summary")]


def test_the_unfaithful_text_is_stored_but_blocked_and_visible(conn, cfg):
    ids = prepare(conn, cfg, [80])
    d = mutate(FAKES[0][1])
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1"), NOW)
    row = conn.execute("SELECT content, findings FROM documents WHERE kind = 'cover_letter'").fetchone()
    assert "5 million CHF" in row["content"]
    f = json.loads(row["findings"])[0]
    assert f["kind"] == "number" and f["where"] == "achievement 1" and "5 million" in f["text"]


def test_an_invented_contact_blocks_the_letter(conn, cfg):
    ids = prepare(conn, cfg, [80])
    r = import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids, contact="Ms Meier"), NOW)
    assert kinds(conn, ids[0], "cover_letter") == ["contact"] and kinds(conn, ids[0], "cv") == []
    assert {k: s for _, k, s, _ in r.documents} == {"cover_letter": "blocked", "cv": "draft"}


def test_a_contact_with_no_description_is_blocked(conn, cfg):
    cfg["tailoring"]["require_description"] = False        # these jobs have no description on purpose
    ids = prepare(conn, cfg, [80], description="")
    import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids, contact="Ms Muster"), NOW)
    assert kinds(conn, ids[0], "cover_letter") == ["contact"]


def test_a_contact_must_match_whole_words(conn, cfg):
    ids = prepare(conn, cfg, [80], description="Please write to Ms Mustermann.")
    import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids, contact="Ms Muster"), NOW)
    assert kinds(conn, ids[0], "cover_letter") == ["contact"]


@pytest.mark.parametrize("description, contact, expected", [
    ("For further information please contact us.\n\nMélanie MoosHuman Resources\n\nLGT Capital Partners AG, Pfaeffikon"
     "Contact:", "Mélanie Moos", []),
    ("Mélanie Moosbrugger Human Resources", "Mélanie Moos", ["contact"]),
    ("Please contact ContactMélanie Moos today", "Mélanie Moos", ["contact"]),
    ("Ms McDonaldHuman Resources", "Ms McDonald", []),
])
def test_a_contact_glued_to_the_following_word_is_found(conn, cfg, description, contact, expected):
    ids = prepare(conn, cfg, [80], description=description)
    import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids, contact=contact), NOW)
    assert kinds(conn, ids[0], "cover_letter") == expected


STRUCTURE = [
    ("unknown_fact_id", lambda d: d["cover_letter"]["blocks"]["achievements"][1].update(facts=["exp-nowhere-made-up"]), "cover_letter"),
    ("unknown_fact_id", lambda d: d["cv"]["experience"][0]["bullets"][0].update(fact_id="exp-made-up-bullet"), "cv"),
    ("unknown_fact_id", lambda d: d["cv"]["experience"][1].update(experience_id="exp-made-up"), "cv"),
    ("fact_experience", lambda d: d["cv"]["experience"][0]["bullets"][0].update(fact_id="exp-northwind-zurich-automation"), "cv"),
    ("variant", lambda d: d["cv"].update(variant="resume_fancy"), "cv"),
    ("skill_group", lambda d: d["cv"].update(skills=["skills-made-up"]), "cv"),
    ("duplicate", lambda d: d["cv"]["experience"].append(copy.deepcopy(d["cv"]["experience"][0])), "cv"),
]


@pytest.mark.parametrize("kind, change, which", STRUCTURE)
def test_wrong_ids_block_the_document_not_the_file(conn, cfg, kind, change, which):
    ids = prepare(conn, cfg, [80, 70])
    d = mutate(change)
    path = results_file(cfg, "run1", [score(ids[0], 80, documents=d), score(ids[1], 70)], schema_version="1.1")
    r = import_results(conn, cfg, path, NOW)
    assert r.status == "imported" and r.scored == ids
    assert kind in kinds(conn, ids[0], which)
    assert dict(((j, k), s) for j, k, s, _ in r.documents)[(ids[0], which)] == "blocked"


def test_the_home_address_is_never_allowed(conn, cfg):
    cfg["location"]["car_origin_address"] = "Rosenweg 12, 8180 Exampletown"
    ids = prepare(conn, cfg, [80])
    d = mutate(lambda d: d["cover_letter"]["blocks"].update(closing="I live at Rosenweg 12, 8180 Exampletown and would welcome an interview."))
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1"), NOW)
    assert "address" in kinds(conn, ids[0], "cover_letter")


def test_the_generation_date_is_not_a_finding(conn, cfg):
    ids = prepare(conn, cfg, [80])
    d = mutate(lambda d: d["cover_letter"]["blocks"].update(closing=FILLER + " I would welcome an interview in November 2026."))
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1"), NOW)
    assert kinds(conn, ids[0], "cover_letter") == []


# --- who gets documents ---------------------------------------------------------------------------

def test_documents_for_jobs_that_do_not_qualify_are_ignored_and_counted(conn, cfg):
    ids = prepare(conn, cfg, [80, 59, 70])
    r = import_results(conn, cfg, send(cfg, ids, [80, 59, 70], with_docs=ids), NOW)
    assert sorted(j for j, _, _, _ in r.documents) == [ids[0], ids[0], ids[2], ids[2]]
    assert [j for j, _ in r.documents_ignored] == [ids[1]] and "below the cut-off of 60" in r.documents_ignored[0][1]
    assert any("documents ignored for 1 job(s)" in w for w in r.warnings)
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE job_id = ?", (ids[1],)).fetchone()[0] == 0


def test_no_documents_for_a_job_without_a_description(conn, cfg):
    ids = prepare(conn, cfg, [60], description="")
    assert conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (ids[0],)).fetchone()[0] == 1
    r = import_results(conn, cfg, send(cfg, ids, [60], with_docs=ids), NOW)
    assert r.documents == [] and [j for j, _ in r.documents_ignored] == [ids[0]]
    assert "no description (scored from the title only)" in r.documents_ignored[0][1] and "set-description" in r.documents_ignored[0][1]
    assert any("documents ignored for 1 job(s)" in w for w in r.warnings)
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE job_id = ?", (ids[0],)).fetchone()[0] == 0


def test_require_description_false_keeps_the_old_behaviour(conn, cfg):
    cfg["tailoring"]["require_description"] = False
    ids = prepare(conn, cfg, [60], description="")
    r = import_results(conn, cfg, send(cfg, ids, [60], with_docs=ids), NOW)
    assert len(r.documents) == 2 and r.documents_ignored == []


def test_the_cut_off_is_inclusive(conn, cfg):
    ids = prepare(conn, cfg, [60])
    r = import_results(conn, cfg, send(cfg, ids, [60], with_docs=ids), NOW)
    assert len(r.documents) == 2 and r.documents_ignored == []


def test_documents_outside_the_first_n_are_ignored(conn, cfg):
    cfg["tailoring"]["documents_top_n"] = 2
    ids = prepare(conn, cfg, [90, 80, 70])
    r = import_results(conn, cfg, send(cfg, ids, [90, 80, 70], with_docs=ids), NOW)
    assert {j for j, _, _, _ in r.documents} == set(ids[:2])
    assert [j for j, _ in r.documents_ignored] == [ids[2]] and "not among the first 2" in r.documents_ignored[0][1]


def test_documents_switched_off(conn, cfg):
    cfg["tailoring"]["documents_top_n"] = 0
    ids = prepare(conn, cfg, [90])
    r = import_results(conn, cfg, send(cfg, ids, [90], with_docs=ids), NOW)
    assert r.documents == [] and len(r.documents_ignored) == 1 and r.documents_missing == []
    assert count(conn, "documents") == 0


def test_documents_for_a_carried_over_job_are_ignored(conn, cfg):
    ids = prepare(conn, cfg, [90, 80])
    conn.execute("UPDATE batch_jobs SET fresh = 0 WHERE job_id = ?", (ids[0],))
    conn.commit()
    r = import_results(conn, cfg, send(cfg, ids, [90, 80], with_docs=ids), NOW)
    assert {j for j, _, _, _ in r.documents} == {ids[1]}
    assert r.documents_ignored == [(ids[0], "carried over from an earlier batch")]


def test_a_job_that_qualifies_without_documents_is_reported_missing(conn, cfg):
    ids = prepare(conn, cfg, [80, 70, 40])
    r = import_results(conn, cfg, send(cfg, ids, [80, 70, 40], with_docs=[ids[0]]), NOW)
    assert r.documents_missing == [(ids[1], ["cover_letter", "cv"])]            # job 3 is below the cut-off: not expected
    assert any("documents missing for job(s) that qualify" in w and str(ids[1]) in w for w in r.warnings)


def test_a_job_with_only_one_document_is_missing_the_other(conn, cfg):
    ids = prepare(conn, cfg, [80])
    d = {"cv": copy.deepcopy(CV)}
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1"), NOW)
    assert r.documents_missing == [(ids[0], ["cover_letter"])]


# --- schema violations reject the whole file ---------------------------------------------------------

def broken(change):
    d = docs()
    change(d)
    return d


BAD = [
    lambda d: d["cover_letter"]["blocks"]["achievements"].pop(),
    lambda d: d["cover_letter"]["blocks"]["achievements"].append(copy.deepcopy(d["cover_letter"]["blocks"]["achievements"][0])),
    lambda d: d["cover_letter"]["blocks"]["achievements"][0].pop("facts"),
    lambda d: d["cover_letter"]["blocks"]["achievements"][0].update(facts=[]),
    lambda d: d["cover_letter"]["blocks"].pop("closing"),
    lambda d: d["cover_letter"]["blocks"].update(salutation="Dear Sir,"),
    lambda d: d["cover_letter"].pop("contact_name"),
    lambda d: d["cover_letter"].update(contact_name="x" * 81),
    lambda d: d["cover_letter"].update(date="2026-11-02"),
    lambda d: d["cv"]["experience"][0]["bullets"][0].update(text="x" * 401),
    lambda d: d["cv"]["experience"][0].update(employer="Goldman Sachs"),
    lambda d: d["cv"]["experience"][0].update(dates="2020-2021"),
    lambda d: d["cv"].pop("summary"),
    lambda d: d["cv"].update(experience=[]),
    lambda d: d["cv"].update(skills="Python"),
    lambda d: d.update(portfolio={}),
]


@pytest.mark.parametrize("change", BAD)
def test_a_schema_violation_in_documents_rejects_the_whole_file(conn, cfg, change):
    ids = prepare(conn, cfg, [80, 70])
    path = results_file(cfg, "run1", [score(ids[0], 80, documents=broken(change)), score(ids[1], 70)], schema_version="1.1")
    r = import_results(conn, cfg, path, NOW)
    assert r.status == "rejected" and "schema check failed" in r.reason
    assert count(conn, "scores") == 0 and count(conn, "documents") == 0
    assert (exchange_dir(cfg) / "rejected" / path.name).exists()


def test_the_rejection_names_the_field_that_is_wrong(conn, cfg):
    ids = prepare(conn, cfg, [80])
    d = docs()
    d["cover_letter"]["blocks"]["achievements"].pop()
    d["cv"]["experience"][0]["bullets"][0]["text"] = "x" * 401
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1"), NOW)
    assert "results/0/documents/cover_letter/blocks/achievements: needs at least 4 item(s), has 3" in r.reason
    assert "documents/cv/experience/0/bullets/0/text: text needs at most 400 characters, has 401" in r.reason


def test_an_empty_documents_object_is_a_violation(conn, cfg):
    ids = prepare(conn, cfg, [80])
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents={})], schema_version="1.1"), NOW)
    assert r.status == "rejected"


def test_documents_on_an_error_entry_are_a_violation(conn, cfg):
    ids = prepare(conn, cfg, [80])
    entry = {"job_id": ids[0], "error": "unreadable", "documents": docs()}
    assert import_results(conn, cfg, results_file(cfg, "run1", [entry], schema_version="1.1"), NOW).status == "rejected"


# --- idempotence and replacement ----------------------------------------------------------------------

def test_the_same_file_twice_changes_nothing(conn, cfg):
    ids = prepare(conn, cfg, [80])
    path = send(cfg, ids, [80], with_docs=ids)
    content = path.read_text(encoding="utf-8")
    assert import_results(conn, cfg, path, NOW).status == "imported"
    before = [dict(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")]
    again = exchange_dir(cfg) / "results" / "again.json"
    again.write_text(content, encoding="utf-8")
    assert import_results(conn, cfg, again, NOW).status == "duplicate"
    assert [dict(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")] == before
    assert count(conn, "scores") == 1


def test_a_later_file_for_the_same_job_replaces_the_documents(conn, cfg):
    ids = prepare(conn, cfg, [80])
    bad = mutate(FAKES[0][1])
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=bad)], schema_version="1.1", name="first.json"), NOW)
    assert {r["kind"]: r["status"] for r in rows(conn)} == {"cover_letter": "blocked", "cv": "draft"}
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=docs())], schema_version="1.1", name="second.json"),
                       datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc))
    assert r.status == "imported" and r.skipped and r.scored == []             # the score is not stored twice
    assert [(x["kind"], x["status"], x["findings"]) for x in rows(conn)] == [("cover_letter", "draft", "[]"), ("cv", "draft", "[]")]
    assert count(conn, "documents") == 2 and count(conn, "scores") == 1
    assert {g["generated_on"] for g in conn.execute("SELECT generated_on FROM documents")} == {"2026-11-03"}
    last = conn.execute("SELECT MAX(id) FROM imports").fetchone()[0]
    assert {g["import_id"] for g in conn.execute("SELECT import_id FROM documents")} == {last}


def test_a_job_that_drops_out_of_the_selection_keeps_its_documents_row(conn, cfg):
    cfg["scoring"]["top_n"] = 1
    ids = prepare(conn, cfg, [70, 90])
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 70, documents=docs())], schema_version="1.1", name="a.json"), NOW)
    assert count(conn, "documents") == 2
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[1], 90)], schema_version="1.1", name="b.json"), NOW)
    assert conn.execute("SELECT selected FROM batch_jobs WHERE job_id = ?", (ids[0],)).fetchone()[0] == 0
    assert count(conn, "documents") == 2                                          # left alone; it is simply not shown


def test_documents_go_with_an_archived_job(conn, cfg):
    ids = prepare(conn, cfg, [80])
    import_results(conn, cfg, send(cfg, ids, [80], with_docs=ids), NOW)
    db.archive_job(conn, ids[0], "filter", "title_exclude")
    assert count(conn, "documents") == 0


def test_migration_11_makes_one_row_per_job_and_kind(conn, cfg):
    assert len(db.MIGRATIONS) >= 11 and db.schema_version(conn) == len(db.MIGRATIONS)
    ids = prepare(conn, cfg, [80])
    ins = ("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at) VALUES (?, ?, '{}', 'draft', '2026-11-02', 'x')")
    conn.execute(ins, (ids[0], "cv"))
    with pytest.raises(Exception):
        conn.execute(ins, (ids[0], "cv"))
    with pytest.raises(Exception):
        conn.execute(ins.replace("'draft'", "'final'"), (ids[0], "cover_letter"))
    with pytest.raises(Exception):                                                # migration 13: approved needs its time
        conn.execute(ins.replace("'draft'", "'approved'"), (ids[0], "cover_letter"))
    with pytest.raises(Exception):
        conn.execute(ins, (ids[0], "poster"))


# --- the report ---------------------------------------------------------------------------------------

def report_for(conn, cfg, scores, with_docs=(), mutate_docs=None, run_id="run1"):
    ids = prepare(conn, cfg, scores)
    es = []
    for n, (i, s) in enumerate(zip(ids, scores)):
        extra = {"documents": mutate_docs(docs()) if mutate_docs else docs()} if n in with_docs else {}
        es.append(score(i, s, **extra))
    import_results(conn, cfg, results_file(cfg, run_id, es, schema_version="1.1"), NOW)
    return ids, collect(conn, cfg, run_id)


def test_the_report_marks_each_top_job(conn, cfg):
    def spoil(d):
        d["cover_letter"]["blocks"]["achievements"][0]["text"] = "Saved 5 million CHF at Northwind."
        d["cover_letter"]["blocks"]["skills"] = "I know Kubernetes."
        return d
    ids = prepare(conn, cfg, [90, 80, 70, 40])
    es = [score(ids[0], 90, documents=docs()), score(ids[1], 80, documents=spoil(docs())), score(ids[2], 70), score(ids[3], 40)]
    import_results(conn, cfg, results_file(cfg, "run1", es, schema_version="1.1"), NOW)
    r = collect(conn, cfg, "run1")
    assert [j.documents for j in r.jobs] == ["documents ready", "documents blocked (2 findings)", "documents missing", "below cut-off, no documents"]
    assert "DOCUMENTS READY" in r.text and "DOCUMENTS BLOCKED (2 FINDINGS)" in r.text and "BELOW CUT-OFF, NO DOCUMENTS" in r.text
    assert "documents missing" in r.html and "below cut-off, no documents" in r.html
    assert r.cutoff_line == ""


def test_one_finding_wording(conn, cfg):
    ids = prepare(conn, cfg, [80])
    d = docs()
    d["cv"]["variant"] = "resume_fancy"
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=d)], schema_version="1.1"), NOW)
    assert collect(conn, cfg, "run1").jobs[0].documents == "documents blocked (1 finding)"


def test_no_job_reached_the_cut_off_is_said_at_the_top(conn, cfg):
    ids, r = report_for(conn, cfg, [55, 40])
    line = "No job reached the cut-off of 60; no CV or cover letter was written."
    assert r.cutoff_line == line
    assert r.text.splitlines()[3] == line and r.text.index(line) < r.text.index("TOP")
    assert f"<b>{line}</b>" in r.html and r.html.index(line) < r.html.index("#1")
    assert r.subject == "Recruitment: 2 job matches for 2 Nov 2026"
    assert [j.documents for j in r.jobs] == ["below cut-off, no documents"] * 2


def test_the_cut_off_in_the_line_is_the_configured_number(conn, cfg):
    cfg["tailoring"]["min_score"] = 75
    ids, r = report_for(conn, cfg, [70])
    assert r.cutoff_line == "No job reached the cut-off of 75; no CV or cover letter was written."


def test_no_cut_off_line_when_a_job_qualifies_or_documents_are_off(conn, cfg):
    ids, r = report_for(conn, cfg, [90])
    assert r.cutoff_line == ""
    cfg["tailoring"]["documents_top_n"] = 0
    ids = prepare(conn, cfg, [30], run_id="run2", start=10)
    import_results(conn, cfg, results_file(cfg, "run2", [score(ids[0], 30)], schema_version="1.1"), NOW)
    r2 = collect(conn, cfg, "run2")
    assert r2.cutoff_line == "" and r2.jobs[0].documents == "no documents (switched off)"


def test_the_report_carries_no_document_text_and_no_address(conn, cfg):
    cfg["location"]["car_origin_address"] = "Rosenweg 12, 8180 Exampletown"
    ids = prepare(conn, cfg, [90])
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 90, documents=docs())], schema_version="1.1"), NOW)
    r = collect(conn, cfg, "run1")
    for text in (r.text, r.html):
        assert "Process Automation" not in text and "Requirements Delivery" not in text and "UAT" not in text
        assert "Rosenweg" not in text and "Dear Hiring Manager" not in text and "Fund Platform" not in text
    assert r.jobs[0].documents == "documents ready"


def test_the_ranks_beyond_the_documents_limit_are_marked(conn, cfg):
    cfg["tailoring"]["documents_top_n"] = 1
    ids, r = report_for(conn, cfg, [90, 80], with_docs=[0])
    assert r.jobs[1].documents == "no documents (outside the first 1)"


def test_the_report_marks_a_job_scored_without_a_description(conn, cfg):
    ids = prepare(conn, cfg, [70], description="")
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 70)], schema_version="1.1"), NOW)
    ids += prepare(conn, cfg, [65], run_id="run1b", start=2)
    r = collect(conn, cfg, "run1")
    assert r.jobs == []                                         # v0.72.0: a title-only job in the would-be top 10 is queued, not ranked
    assert [(m.job_id, m.place) for m in r.missing] == [(ids[0], 1)]
    assert "WAITING FOR A DESCRIPTION (1)" in r.text and "Waiting for a description (1)" in r.html
    assert "capped at 60" not in r.text and "70" not in r.text.split("WAITING FOR A DESCRIPTION")[1].split("Run summary")[0]


def test_the_report_has_no_description_marker_for_a_described_job(conn, cfg):
    ids, r = report_for(conn, cfg, [90])
    assert r.jobs[0].description_note == "" and "no description" not in r.text.lower()


def test_the_report_marker_for_a_job_without_a_description_when_documents_are_allowed(conn, cfg):
    cfg["tailoring"]["require_description"] = False
    ids = prepare(conn, cfg, [70], description="")
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 70)], schema_version="1.1"), NOW)
    j = collect(conn, cfg, "run1").jobs[0]
    assert j.description_note and j.documents == "documents missing"


# --- config ------------------------------------------------------------------------------------------

def test_the_tailoring_defaults(cfg):
    assert cfg["tailoring"] == {"documents_top_n": 10, "min_score": 60, "require_description": True, "photo": "profile/photo.jpg", "letter_max_pages": 1, "cv_max_pages": 2}


def write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize("text", ["tailoring:\n  documents_top_n: 0\n", "tailoring:\n  documents_top_n: 3\n  min_score: 0\n",
                                  "tailoring:\n  min_score: 100\n", "tailoring:\n  min_score: 70\n"])
def test_good_tailoring_values(tmp_path, text):
    assert load_config(write(tmp_path, text))["tailoring"]


@pytest.mark.parametrize("text, key", [
    ("tailoring:\n  documents_top_n: -1\n", "documents_top_n"), ("tailoring:\n  documents_top_n: 2.5\n", "documents_top_n"),
    ("tailoring:\n  documents_top_n: ten\n", "documents_top_n"), ("tailoring:\n  documents_top_n: true\n", "documents_top_n"),
    ("tailoring:\n  min_score: -1\n", "min_score"), ("tailoring:\n  min_score: 101\n", "min_score"),
    ("tailoring:\n  min_score: 60.5\n", "min_score"), ("tailoring:\n  min_score: \"60\"\n", "min_score"),
    ("tailoring:\n  min_score: false\n", "min_score"), ("tailoring:\n  mine: 1\n", "tailoring.mine"), ("tailoring: 5\n", "tailoring")])
def test_bad_tailoring_values(tmp_path, text, key):
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, text))


def test_documents_top_n_above_top_n_is_allowed_and_changes_nothing(tmp_path, conn):
    c = load_config(write(tmp_path, "scoring:\n  top_n: 3\n"))                    # the default 10 is above 3
    assert c["tailoring"]["documents_top_n"] == 10 and c["scoring"]["top_n"] == 3


# --- the command ---------------------------------------------------------------------------------------

from tests.test_cli import clean_logger, config_file, run  # noqa: E402,F401


def seeded(config_file):
    c = load_config(config_file)
    conn = db.connect(c["paths"]["database"])
    ids = add_jobs(conn, c, 2, description=DESCRIPTION)
    build_batch(conn, c, PROMPT, "v-test", run_id="run1", now=NOW)
    bad = mutate(FAKES[0][1])
    import_results(conn, c, results_file(c, "run1", [score(ids[0], 80, documents=bad), score(ids[1], 70, documents=docs())],
                                         schema_version="1.1"), NOW)
    conn.close()
    return ids


def test_documents_command_lists(config_file, capsys):
    ids = seeded(config_file)
    assert run(config_file, "documents") == 0
    out = capsys.readouterr().out
    assert re.search(rf"{ids[0]} \| cover_letter \| blocked +\| +1 finding\(s\) \| no PDF +\| Analyst 1 \(Acme\)", out)
    assert re.search(rf"{ids[0]} \| cv +\| draft +\| +0 finding\(s\) \| 2 page\(s\)", out) and "4 document(s)" in out


def test_documents_command_for_one_job_prints_findings_and_text(config_file, capsys):
    ids = seeded(config_file)
    assert run(config_file, "documents", "--job", str(ids[0])) == 0
    out = capsys.readouterr().out
    assert f"=== cover_letter for job {ids[0]}" in out and "| blocked |" in out and "FINDING [number]" in out and "(achievement 1)" in out
    assert "Saved 5 million CHF" in out and "no findings" in out and "Variant: resume" in out
    assert "exp-contoso-requirements" in out and "Sincerely" not in out
    assert "Analyst 2" not in out


def test_documents_command_json(config_file, capsys):
    ids = seeded(config_file)
    assert run(config_file, "documents", "--json") == 0
    listing = json.loads(capsys.readouterr().out)
    assert len(listing) == 4 and all("content" not in d for d in listing) and listing[0]["findings"]
    assert run(config_file, "documents", "--json", "--job", str(ids[1])) == 0
    one = json.loads(capsys.readouterr().out)
    assert {d["kind"] for d in one} == {"cv", "cover_letter"} and all("content" in d for d in one)


def test_documents_command_unknown_job_and_empty_list(config_file, capsys):
    assert run(config_file, "documents") == 0 and "0 document(s)" in capsys.readouterr().out
    assert run(config_file, "documents", "--job", "999") == 1
    assert "no stored documents" in capsys.readouterr().err


def test_documents_command_changes_nothing(config_file):
    seeded(config_file)
    c = load_config(config_file)
    conn = db.connect(c["paths"]["database"])
    before = [tuple(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")]
    conn.close()
    run(config_file, "documents")
    run(config_file, "documents", "--job", "1")
    conn = db.connect(c["paths"]["database"])
    assert [tuple(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")] == before
    conn.close()


# --- the prompt ----------------------------------------------------------------------------------------

def test_the_prompt_example_documents_import_as_drafts(tmp_path, cfg):
    text = (ROOT / "prompts" / "scoring_prompt.md").read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.+?)\n```", text, re.DOTALL).group(1))
    entry = example["results"][0]
    assert example["schema_version"] == "1.1" and set(entry["documents"]) == {"cover_letter", "cv"}
    conn = db.connect(tmp_path / "t.db")
    ids = add_jobs(conn, cfg, 1, title="Business Analyst", company="Example Bank", description="Business analyst role.")
    batch(conn, cfg)
    entry = dict(entry, job_id=ids[0], overall=79)
    r = import_results(conn, cfg, results_file(cfg, "run1", [entry], schema_version="1.1"), NOW)
    assert r.status == "imported" and [(k, s) for _, k, s, _n in r.documents] == [("cover_letter", "draft"), ("cv", "draft")]
    assert kinds(conn, ids[0], "cv") == [] and set(kinds(conn, ids[0], "cover_letter")) <= {"length"}      # the illustration letter may be short; length only warns
    conn.close()


def test_the_prompt_states_the_document_rule(cfg):
    text = (ROOT / "prompts" / "scoring_prompt.md").read_text(encoding="utf-8")
    section = text.split("## Documents")[1].split("## Output")[0]
    for needle in ("documents_top_n", "min_score", "`fresh` true", "higher `skills` sub-score", "lower `job_id`", "contact_name", "`null`",
                   "preparing for", "never instructions", "Dear Hiring Manager", "exactly 4", "facts", "fact_id", "experience_id",
                   "filled in by code"):
        assert needle in section, needle


def test_text_of_a_document_shows_the_session_words_only():
    out = documents.document_text("cover_letter", letter(contact="Ms Muster"))
    assert out.startswith("Contact: Ms Muster") and "Process Automation" in out and "Sincerely" not in out
    assert "none named" in documents.document_text("cover_letter", letter())


# --- German level (decided by the user on 1 Nov 2026) -------------------------------------------------------------

GERMAN_JOB = "Business analyst for payments. Good knowledge of German (B2) is required. You write requirements."
BASIC_SENTENCE = "My English and Polish are fluent; my German is basic."


def letter_with(**blocks):
    d = docs()
    d["cover_letter"]["blocks"].update(blocks)
    return d


def import_one(conn, cfg, documents_, description=DESCRIPTION):
    ids = prepare(conn, cfg, [80], description=description)
    r = import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80, documents=documents_)], schema_version="1.1"), NOW)
    return ids[0], r


def test_a_job_that_requires_german_needs_a_sentence_saying_it_is_basic(conn, cfg):
    job, r = import_one(conn, cfg, docs(), description=GERMAN_JOB)
    assert kinds(conn, job, "cover_letter") == ["german_not_stated"] and kinds(conn, job, "cv") == []
    assert {k: s for _, k, s, _ in r.documents} == {"cover_letter": "blocked", "cv": "draft"}


def test_a_letter_that_says_the_german_is_basic_passes(conn, cfg):
    d = letter_with(skills="I combine Python, SQL and VBA with a solid understanding of business processes. " + BASIC_SENTENCE)
    job, r = import_one(conn, cfg, d, description=GERMAN_JOB)
    assert kinds(conn, job, "cover_letter") == [] and kinds(conn, job, "cv") == []
    assert {k: s for _, k, s, _ in r.documents} == {"cover_letter": "draft", "cv": "draft"}


def test_a_job_without_a_german_requirement_needs_no_sentence(conn, cfg):
    job, _ = import_one(conn, cfg, docs())
    assert kinds(conn, job, "cover_letter") == []


def test_a_german_requirement_that_is_only_a_plus_needs_no_sentence(conn, cfg):
    job, _ = import_one(conn, cfg, docs(), description="Business analyst for payments. German is a plus. You write requirements.")
    assert kinds(conn, job, "cover_letter") == []


def test_the_requirement_is_read_from_the_stored_filter_results(conn, cfg):
    ids = prepare(conn, cfg, [80])
    assert documents.german_required(conn, ids[0], "Analyst 1", DESCRIPTION, cfg) is False
    conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, reason_code, comment, created_at) VALUES (?,?,?,?,?,?)",
                 (ids[0], "language", "conditional", "language_level_unspecified", 'German: required, no level stated; "x"', "2026-11-01T10:00:00Z"))
    assert documents.german_required(conn, ids[0], "Analyst 1", DESCRIPTION, cfg) is True


def test_a_french_requirement_in_the_stored_results_is_not_a_german_requirement(conn, cfg):
    ids = prepare(conn, cfg, [80])
    conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, reason_code, comment, created_at) VALUES (?,?,?,?,?,?)",
                 (ids[0], "language", "conditional", "language_level_unspecified", 'French: required, no level stated; "x"', "2026-11-01T10:00:00Z"))
    assert documents.german_required(conn, ids[0], "Analyst 1", DESCRIPTION, cfg) is False


def test_a_posting_in_german_counts_as_a_german_requirement(conn, cfg):
    ids = prepare(conn, cfg, [80])
    conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, reason_code, comment, created_at) VALUES (?,?,?,?,?,?)",
                 (ids[0], "language", "conditional", "language_implied_by_posting", "the posting is written in German", "2026-11-01T10:00:00Z"))
    assert documents.german_required(conn, ids[0], "Analyst 1", DESCRIPTION, cfg) is True


OVERSTATED = [
    "My English and Polish are fluent and I speak fluent German.",
    "I speak fluent German with clients.",
    "Fluent in English, Polish and German.",
    "I bring good German and English.",
    "My German is good enough for daily work.",
    "Working knowledge of German.",
    "Conversational German helps me with clients.",
    "I have German at B2 level.",
    "Deutsch auf C1 Niveau.",
    "German at business level.",
    "English, Polish and German: all fluent.",
    "I am fluent in English, Polish and German.",
    "Fluent in English, Polish, and German.",
    "English, German and Polish (fluent).",
    "English, Polish and German - advanced.",
    "Languages: English, Polish and German, all at C1 level.",
    "Proficient in English, Polish and German.",
]


@pytest.mark.parametrize("sentence", OVERSTATED)
def test_a_german_level_above_basic_blocks_the_letter_and_the_cv(conn, cfg, sentence):
    d = letter_with(skills="I combine Python, SQL and VBA. " + sentence + " " + BASIC_SENTENCE)
    d["cv"]["summary"] = "I'm a BA who also tests. " + sentence
    job, r = import_one(conn, cfg, d)
    assert "language_claim" in kinds(conn, job, "cover_letter") and "language_claim" in kinds(conn, job, "cv")
    assert {k: s for _, k, s, _ in r.documents} == {"cover_letter": "blocked", "cv": "blocked"}
    f = [x for x in json.loads(conn.execute("SELECT findings FROM documents WHERE kind = 'cv'").fetchone()[0]) if x["kind"] == "language_claim"][0]
    assert f["where"] == "summary" and "basic" in f["reason"]


HONEST = [
    "My English and Polish are fluent; my German is basic.",
    "My English and Polish are fluent, my German is basic.",
    "My English and Polish are fluent and my German is basic.",
    "Basic German.",
    "I have basic knowledge of German, not fluent.",
    "German at A1 level.",
    "I worked with German banks and the strong German market.",
    "I work in a good team of German-speaking colleagues.",
    "Fluent in English and Polish; basic German.",
    "English and Polish (fluent), German (basic).",
    "English and Polish fluent, German and French basic.",
    "Fluent in English and Polish, basic German.",
    "English, Polish and German (basic).",
    "I worked with English, Polish and German banks.",
]


@pytest.mark.parametrize("sentence", HONEST)
def test_honest_or_unrelated_text_about_german_passes(conn, cfg, sentence):
    d = letter_with(skills="I combine Python, SQL and VBA. " + sentence)
    d["cv"]["summary"] = "I'm a BA who also tests. " + sentence
    job, r = import_one(conn, cfg, d)
    assert "language_claim" not in kinds(conn, job, "cover_letter") and "language_claim" not in kinds(conn, job, "cv")


def test_basic_german_in_a_cv_bullet_passes(conn, cfg):
    d = docs()
    d["cv"]["experience"][0]["bullets"][0]["text"] = ("Requirements Gathering: Collaborated with IT and Operations Team to define and "
                                                        "document requirements for the Fund Platform, with basic German.")
    job, _ = import_one(conn, cfg, d)
    assert kinds(conn, job, "cv") == []


def test_the_language_claim_is_found_in_a_cv_bullet_and_names_it(conn, cfg):
    d = docs()
    d["cv"]["experience"][0]["bullets"][0]["text"] = ("Requirements Gathering: Collaborated with IT and Operations Team to define and "
                                                        "document requirements for the Fund Platform in fluent German.")
    job, _ = import_one(conn, cfg, d)
    f = json.loads(conn.execute("SELECT findings FROM documents WHERE kind = 'cv'").fetchone()[0])
    assert [x["kind"] for x in f] == ["language_claim"] and "bullet 1" in f[0]["where"]


def test_german_not_stated_applies_to_the_letter_only(conn, cfg):
    job, _ = import_one(conn, cfg, docs(), description=GERMAN_JOB)
    assert "german_not_stated" not in kinds(conn, job, "cv")


def test_the_prompt_states_the_german_rule():
    text = (ROOT / "prompts" / "scoring_prompt.md").read_text(encoding="utf-8")
    assert "Prompt version: 1.18.0" in text and "approved by the user" in text and "-draft" not in text.split("\n")[2]
    assert "my German is basic" in text and "never claim, imply or round up a german level" in text.lower()


def test_a_stub_last_line_is_a_warning_in_the_import_that_never_blocks(conn, cfg):
    d = docs()
    d["cv"]["experience"][0]["bullets"][1]["text"] = ("Testing Management: Wrote detailed test cases and executed End-to-End UAT "
                                                        "processes within Xray to ensure platform stability.")
    job, r = import_one(conn, cfg, d)
    row = conn.execute("SELECT status, findings FROM documents WHERE job_id = ? AND kind = 'cv'", (job,)).fetchone()
    found = [f for f in json.loads(row["findings"]) if f["kind"] == "line_ending"]
    assert row["status"] == "draft" and len(found) == 1 and found[0]["text"] == "stability." and "bullet 2" in found[0]["where"]
    assert any(w.startswith("documents with a last line") and f"{job} cv 1" in w for w in r.warnings)
    assert {k: s for _, k, s, _ in r.documents}["cv"] == "draft"
    assert any("stability." in n for n in documents.notes(conn, job))


def test_a_schema_1_1_chunk_file_gives_no_documents_missing_warning(conn, cfg):
    ids = prepare(conn, cfg, [80, 70])
    r = import_results(conn, cfg, send(cfg, ids, [80, 70], name="results_run1.p01.json"), NOW)
    assert r.status == "imported" and r.scored == ids and r.documents_missing == [(ids[0], ["cover_letter", "cv"]), (ids[1], ["cover_letter", "cv"])]
    assert not any("documents missing" in w for w in r.warnings)


# --- round-2 lows (v0.80.6) -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("fid", ["exp-northwind-zurich", "skills-reporting-data"])
def test_a_cv_bullet_citing_a_non_bullet_id_is_blocked(fid):
    facts, index = tr.load_facts(), documents.load_fact_index()
    assert fid not in index.fact_owner
    job = {"title": "Business Analyst", "company": "Acme", "description": ""}
    doc = copy.deepcopy(CV)
    doc["experience"][0]["bullets"][0]["fact_id"] = fid
    findings = documents.check_cv(doc, job, facts, index, datetime(2026, 11, 1).date())
    assert "fact_experience" in [f["kind"] for f in findings]


@pytest.mark.parametrize("text, expected", [
    ("I bring basic SQL skills and German is a plus for me.", False),
    ("My German is not basic.", False),
    ("my German is basic", True),
    (BASIC_SENTENCE, True),
    ("Basic German.", True),
    ("I have basic knowledge of German, not fluent.", True),
    ("English and Polish fluent, German and French basic.", True),
    ("English, Polish and German (basic).", True)])
def test_basic_german_must_describe_german(text, expected):
    assert documents._states_basic_german(text) is expected


# --- letter length warning (290 to 350 words, goal.md 5.2) ----------------------------------------------------------

def _words(d):
    b = d["cover_letter"]["blocks"]
    return sum(len(t.split()) for t in [b["opening"], b["lead_in"], b["skills"], b["closing"]] + [f"{a['label']}: {a['text']}" for a in b["achievements"]])


def _letter_kinds(words):
    """Finding kinds of the fixture letter after its closing was padded or cut to `words` words in total."""
    job = {"title": "Analyst", "company": "Acme", "description": DESCRIPTION, "locations": []}
    c = copy.deepcopy(docs()["cover_letter"])
    diff = words - _words({"cover_letter": c})
    parts = c["blocks"]["closing"].split()
    c["blocks"]["closing"] = " ".join(parts + ["word"] * diff if diff >= 0 else parts[:diff])
    return [f["kind"] for f in documents.check_cover_letter(c, job, tr.load_facts(), datetime(2026, 11, 11).date())]


def test_a_letter_inside_the_length_range_has_no_length_finding(conn, cfg):
    assert 290 <= _words(docs()) <= 350
    job, _ = import_one(conn, cfg, docs())
    assert "length" not in kinds(conn, job, "cover_letter")


@pytest.mark.parametrize("closing", ["I would welcome an interview.", ("ab " * 260).strip()])
def test_a_letter_outside_the_length_range_gets_a_warning_that_never_blocks(conn, cfg, closing):
    job, r = import_one(conn, cfg, letter_with(closing=closing))
    assert kinds(conn, job, "cover_letter") == ["length"]
    assert {k: s for _, k, s, _ in r.documents} == {"cover_letter": "draft", "cv": "draft"}
    row = conn.execute("SELECT findings FROM documents WHERE job_id = ? AND kind = 'cover_letter'", (job,)).fetchone()
    f = json.loads(row["findings"])[0]
    assert "words" in f["text"] and f["where"] == "document" and f["kind"] in documents.WARNING_KINDS


def test_the_length_range_edges_are_inclusive():
    for words, expect in ((290, False), (350, False), (289, True), (351, True)):
        assert ("length" in _letter_kinds(words)) is expect, words


def test_the_length_warning_is_kept_by_the_pdf_step_and_shown_with_the_approval_warnings():
    assert documents.LENGTH_WARNING in documents.WARNING_KINDS and documents.LENGTH_WARNING not in documents.PDF_WARNING_KINDS
