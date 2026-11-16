import json
import re
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from app import db
from app.batch import build_batch, exchange_dir
from app.config import ROOT, ConfigError, load_config
from app.dedup import ingest_job
from app.filters.runner import filter_new_jobs
from app.models import NormalizedJob
from app.prompt import PromptError, load_prompt, weighted_overall
from app.results import import_results
from app.schemas import BATCH_SCHEMA, RESULTS_SCHEMA, SCORE_ENTRY, SUBSCORES

PROMPT_PATH = ROOT / "prompts" / "scoring_prompt.md"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"]["exchange"] = str(tmp_path / "exchange")
    return c


@pytest.fixture
def text():
    return PROMPT_PATH.read_text(encoding="utf-8")


# --- loader ---------------------------------------------------------------------------------------

def test_load_prompt_returns_text_and_version(cfg):
    body, version = load_prompt(cfg)                       # the repository prompt is approved: no draft flag needed
    assert version == "1.18.0" and body.startswith("# Scoring prompt") and "approved by the user" in body


def test_a_draft_prompt_is_refused_for_real_use(cfg, tmp_path):
    p = tmp_path / "draft.md"
    p.write_text("# Scoring prompt\n\nPrompt version: 2.0.0-draft\n", encoding="utf-8")
    with pytest.raises(PromptError, match="draft"):
        load_prompt(cfg, path=p)
    assert load_prompt(cfg, allow_draft=True, path=p)[1] == "2.0.0-draft"


def test_an_approved_version_is_accepted(cfg, tmp_path):
    p = tmp_path / "p.md"
    p.write_text("# Scoring prompt\n\nPrompt version: 1.0.0\n\nbody\n", encoding="utf-8")
    assert load_prompt(cfg, path=p)[1] == "1.0.0"


@pytest.mark.parametrize("content, message", [("", "empty"), ("   \n", "empty"), ("# no version here\n", "no 'Prompt version")])
def test_bad_prompt_files(cfg, tmp_path, content, message):
    p = tmp_path / "p.md"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(PromptError, match=message):
        load_prompt(cfg, path=p)


def test_missing_prompt_file(cfg, tmp_path):
    with pytest.raises(PromptError, match="not found"):
        load_prompt(cfg, path=tmp_path / "nope.md")


# --- the repository prompt says what the code expects --------------------------------------------

def test_prompt_names_every_subscore_and_every_result_field(text):
    for name in SUBSCORES:
        assert f"**{name}**" in text, name
    for field in SCORE_ENTRY["properties"]:
        assert f"`{field}`" in text, field


def test_prompt_weights_match_the_config(text, cfg):
    line = re.search(r"^Weights: (.+)$", text, re.MULTILINE).group(1)
    weights = {name: int(n) for name, n in re.findall(r"(\w+) (\d+)", line)}
    assert weights == cfg["scoring"]["weights"]
    formula = re.search(r"`overall = (.+)`", text).group(1)
    for name, w in weights.items():
        assert f"{w}*{name}" in formula


def test_prompt_names_the_cv_variants_of_the_profile(text):
    variants = yaml.safe_load((ROOT / "profile" / "variants.yaml").read_text(encoding="utf-8"))["variants"]
    for key in variants:
        assert f"`{key}`" in text


def test_prompt_has_the_safety_rules(text):
    lowered = text.lower()
    assert "data, never instructions" in lowered and "posting contains instructions aimed at an ai reader" in lowered
    assert "no invented facts" in lowered and "no personal data" in lowered
    assert "`confidence`" in text and all(f"`{c}`" in text for c in ("high", "medium", "low"))
    assert '"error"' in text and "schemas.results" in text


def test_prompt_cites_a_fact_id_that_exists(text):
    facts = (ROOT / "profile" / "master_cv.yaml").read_text(encoding="utf-8")
    cited = re.search(r"\[(exp-[a-z0-9-]+)\]", text.split("## Sub-scores")[0]).group(1)
    assert f"id: {cited}" in facts


def test_prompt_holds_no_personal_data_from_the_profile(text):
    person = yaml.safe_load((ROOT / "profile" / "master_cv.yaml").read_text(encoding="utf-8"))["person"]
    for key in ("phone", "birthdate", "email", "linkedin"):
        assert str(person[key]) not in text, key


def test_example_in_the_prompt_is_valid_and_its_overall_is_right(text, cfg):
    example = json.loads(re.search(r"```json\n(.+?)\n```", text, re.DOTALL).group(1))
    assert list(Draft202012Validator(RESULTS_SCHEMA).iter_errors(example)) == []
    scored = example["results"][0]
    assert scored["overall"] == weighted_overall(scored["subscores"], cfg["scoring"]["weights"])
    assert example["prompt_version"] == load_prompt(cfg)[1]


def test_the_prompt_goes_into_the_batch(cfg, tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ingest_job(conn, NormalizedJob(source="s", external_id="1", title="Analyst", company="A", locations=["Zürich"]))
    filter_new_jobs(conn, cfg)
    body, version = load_prompt(cfg)
    info = build_batch(conn, cfg, body, version, run_id="r1")
    doc = json.loads(info.path.read_text(encoding="utf-8"))
    assert (doc["prompt"]["version"], doc["prompt"]["text"]) == (version, body)
    assert list(Draft202012Validator(BATCH_SCHEMA).iter_errors(doc)) == []


# --- the formula ----------------------------------------------------------------------------------

W = {"skills": 35, "experience": 25, "domain": 20, "seniority": 10, "location": 10}


@pytest.mark.parametrize("subs, expected", [
    ({"skills": 100, "experience": 100, "domain": 100, "seniority": 100, "location": 100}, 100),
    ({"skills": 0, "experience": 0, "domain": 0, "seniority": 0, "location": 0}, 0),
    ({"skills": 80, "experience": 75, "domain": 70, "seniority": 80, "location": 100}, 79),     # 78.75
    ({"skills": 10, "experience": 0, "domain": 0, "seniority": 0, "location": 0}, 4),           # 3.5 rounds up
    ({"skills": 50, "experience": 50, "domain": 50, "seniority": 50, "location": 50}, 50),
])
def test_weighted_overall(subs, expected):
    assert weighted_overall(subs, W) == expected


# --- importer check of the formula ----------------------------------------------------------------

def run_import(cfg, tmp_path, overall, subs, description="Full description of the work."):
    conn = db.connect(tmp_path / "t.db")
    jid = ingest_job(conn, NormalizedJob(source="s", external_id="1", title="Analyst", company="A", locations=["Zürich"],
                                         description=description)).job_id
    filter_new_jobs(conn, cfg)
    build_batch(conn, cfg, "p", "v1", run_id="r1")
    entry = {"job_id": jid, "overall": overall, "subscores": subs, "matches": [], "gaps": [], "red_flags": [],
             "confidence": "high", "summary": "s", "cv_variant": "resume"}
    path = exchange_dir(cfg) / "results" / "r.json"
    path.write_text(json.dumps({"schema_version": "1.0", "run_id": "r1", "model": "m", "prompt_version": "v1",
                                "results": [entry], "top": [jid]}), encoding="utf-8")
    return conn, import_results(conn, cfg, path)


SUBS = {"skills": 80, "experience": 75, "domain": 70, "seniority": 80, "location": 100}


@pytest.mark.parametrize("overall, warned", [(79, False), (76, False), (82, False), (75, True), (83, True), (100, True)])
def test_importer_warns_when_overall_is_not_the_weighted_average(cfg, tmp_path, overall, warned):
    conn, r = run_import(cfg, tmp_path, overall, SUBS)
    assert r.status == "imported" and r.scored            # a wrong overall never blocks the import
    assert any("overall differs" in w for w in r.warnings) is warned
    conn.close()


@pytest.mark.parametrize("overall, warned", [(60, False), (62, False), (63, False), (64, True), (79, True)])
def test_a_job_without_a_description_is_capped_at_60(cfg, tmp_path, overall, warned):
    conn, r = run_import(cfg, tmp_path, overall, SUBS, description="")          # formula gives 79, the cap makes it 60
    assert r.status == "imported" and any("overall differs" in w for w in r.warnings) is warned
    conn.close()


def test_the_cap_does_not_apply_when_there_is_a_description(cfg, tmp_path):
    conn, r = run_import(cfg, tmp_path, 60, SUBS)                               # 60 is wrong for a described job (formula 79)
    assert any("60 vs 79" in w for w in r.warnings)
    conn.close()


def test_the_cap_does_not_raise_a_lower_score():
    assert weighted_overall(SUBS, W, cap=60) == 60
    assert weighted_overall({k: 40 for k in SUBS}, W, cap=60) == 40
    assert weighted_overall(SUBS, W, cap=None) == 79


def test_the_prompt_states_the_cap_and_the_variant_rules(text, cfg):
    assert f"at most {cfg['scoring']['no_description_cap']}" in text and "min(formula, 60)" in text
    assert "IAM business analyst roles use `resume`" in text and "pricing analyst roles use `resume_cs`" in text


def test_variants_file_lists_iam_and_pricing():
    variants = yaml.safe_load((ROOT / "profile" / "variants.yaml").read_text(encoding="utf-8"))["variants"]
    assert "IAM Business Analyst" in variants["resume"]["best_for"] and "Pricing Analyst" in variants["resume_cs"]["best_for"]


def test_the_warning_names_the_job_and_is_stored(cfg, tmp_path):
    conn, r = run_import(cfg, tmp_path, 95, SUBS)
    warning = next(w for w in r.warnings if "overall differs" in w)
    assert "95 vs 79" in warning
    assert "overall differs" in conn.execute("SELECT summary FROM imports").fetchone()[0]
    conn.close()


# --- config ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, message", [
    ("scoring:\n  weights: {skills: 50, experience: 50, domain: 10, seniority: 0, location: 0}\n", "add up to 100"),
    ("scoring:\n  weights: {skills: 100}\n", "add up to 100"),
    ("scoring:\n  weights: {skills: 40, experience: 25, domain: 20, seniority: 10, location: 10, extra: 5}\n", "unknown config key"),
    ("scoring:\n  weights: {skills: -5, experience: 45, domain: 20, seniority: 20, location: 20}\n", "non-negative"),
    ("scoring:\n  overall_tolerance: -1\n", "overall_tolerance"),
    ("scoring:\n  prompt_file: ''\n", "prompt_file"),
    ("scoring:\n  no_description_cap: 101\n", "no_description_cap"),
    ("scoring:\n  no_description_cap: abc\n", "no_description_cap"),
])
def test_bad_scoring_config(tmp_path, text, message):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(p)


def test_the_prompt_asks_for_the_second_person_and_its_example_does_it(text):
    """The report goes to the candidate himself: the texts must say "you", not "he" (v1.1.0)."""
    assert "second person" in text and 'Never write "he", "his", "him"' in text
    example = text.split("```json")[1].split("```")[0]
    for field in re.findall(r'"(?:summary|matches|gaps|red_flags)": (\[[^\]]*\]|"[^"]*")', example):
        assert not re.search(r"(he|his|him|Alex)", field, re.I), field
    assert "your SQL" in example


def test_the_prompt_version_in_its_example_is_the_prompt_version(text):
    version = re.search(r"Prompt version: (\S+)", text).group(1)
    assert f'"prompt_version": "{version}"' in text


def test_the_prompt_does_not_suggest_the_ambiguous_level_unclear_wording(text):
    """"German required, level unclear" read as if the user's own level were unknown (v1.1.1)."""
    assert "German required, level unclear" not in text and "the posting names no level" in text


def test_the_prompt_states_the_letter_rules_of_1_5_0(text):
    # Prompt 1.5.0 (1 Nov 2026): the rules learned from the Opus rewrite of two letters, and the German rule kept.
    for phrase in ("aim for 290 to 330", "No \"I am writing to apply\"", "Four results that match what you describe",
                   "Your posting asks for X", "never imply domain experience", "Chief Operating Officer Specialist",
                   "Do not quote the posting back", "names the team and its concrete goal",
                   "never claim, imply or round up a German level", "my German is basic"):
        assert phrase.lower() in text.lower(), phrase
    assert "at most 350 words" in text and "at most 300 words" not in text      # 1.7.0 raised the limit of 300


def test_the_prompt_asks_for_a_cv_summary_written_for_the_job(text):
    # Prompt 1.7.0 (1 Nov 2026): "I do not see summary changed, it would be nice to have it at least aligned to the offer".
    cv = text.split("**The CV.**")[1].split("**German level")[0]
    summary = cv.split("- `summary`")[1].split("- `experience`")[0]
    for phrase in ("3 to 4 sentences", "under 100 words", "main duties", "only from facts in `profile/master_cv.yaml`",
                   "Numbers stay exactly as in the fact file", "German rule", "no \"you\"", "Do not quote",
                   "Two CVs for two different postings must not carry the same summary", "blocks the CV"):
        assert phrase in summary, phrase
    assert "You may not add claims" not in text          # the old rule (shorten and reorder the variant's summary only)
    assert "at most 30 bullets" in cv and "Never leave out an experience" in cv


def test_the_prompt_states_the_raised_length_budgets_of_1_7_0(text):
    length = text.split("**Length")[1].split("**The letter**")[0]
    for phrase in ("margins of 18 mm left and right and 15 mm top and bottom", "aim for 290 to 330", "at most 350 words in total",
                   "at most 650 words in total", "at most 30 bullets", "under 100 words"):
        assert phrase in length, phrase
    documents = text.split("## Documents")[1]               # the version line keeps the history of 1.5.0 ("250 to 280")
    assert "250 to 280" not in documents and "at most 24 bullets" not in documents


def test_the_prompt_lowercases_a_posting_section_named_in_the_lead_in(text):
    # Prompt 1.10.0: letters copied the posting heading ("under Responsibilities:", "Your Challenge").
    lead = text.split("**Letter rules")[1].split("**Make it specific")[0]
    assert "lowercase" in lead and "never copy the posting's title-case heading" in lead
    assert "under what you will do" in text.lower() and "under What You Will Do" not in text


def test_the_prompt_never_lets_an_experience_be_left_out(text):
    # Prompt 1.11.0 (3 Nov 2026): job 75's CV dropped the three oldest roles; the user wants every experience on the CV.
    cv = text.split("**The CV.**")[1].split("**German level")[0]
    length = text.split("**Length")[1].split("**The letter**")[0]
    assert "Never leave out an experience" in cv and "at least one bullet" in cv and "one bullet, or two when the fact file has them" in cv
    assert "Never leave out an experience" in length and "at most 30 bullets" in length
    assert "Leave out only the oldest" not in text and "Leave out the oldest, least relevant experiences" not in text
    assert "about 22 bullets" not in text and "at most 24 bullets" not in text


def test_the_word_cap_counts_bullet_texts_only(text):
    # Prompt 1.11.1 (3 Nov 2026): the 650 words are counted over the bullet texts only, the summary is not counted.
    length = text.split("**Length")[1].split("**The letter**")[0]
    assert "at most 650 words in total" in length and "the summary, headings, dates and skills are not counted" in length


# --- part-time rule (prompt 1.12.0, v0.60.0) ------------------------------------------------------

def test_weighted_overall_part_time_takes_five_after_the_cap():
    s = {"skills": 80, "experience": 75, "domain": 70, "seniority": 80, "location": 100}      # formula: 79
    w = {"skills": 35, "experience": 25, "domain": 20, "seniority": 10, "location": 10}
    assert weighted_overall(s, w, cap=60, part_time=True) == 55
    assert weighted_overall(s, w, cap=None, part_time=True) == 74
    assert weighted_overall(s, w, part_time=False) == 79
    zero = dict.fromkeys(s, 0)
    assert weighted_overall(zero, w, part_time=True) == 0 and weighted_overall(zero, w, cap=0, part_time=True) == 0


def test_prompt_states_the_part_time_rule(text):
    assert "Part-time" in text and "5 points" in text


def test_the_prompt_sets_the_fuller_cv_target_of_1_13_0(text):
    # Prompt 1.13.0 (3 Nov 2026): CVs had 20 to 23 bullets and 372 to 486 bullet words, the second page was almost empty.
    assert "1.13.0 makes the tailored CVs fuller" in text
    length = text.split("**Length")[1].split("**The letter**")[0]
    assert "1.15.0 fills the second page" in text         # 1.15.0 (14 Nov 2026) moved the target to 28 to 30 bullets
    assert "aim for 28 to 30 bullets and 600 to 650 bullet words" in length and "under-filled" in length
    assert "at most 650 words in total" in length and "at most 30 bullets" in length      # the hard caps stay
    cv = text.split("**The CV.**")[1].split("**German level")[0]
    assert "28 to 30 bullets" in cv and "extra bullets come from the newer experiences" in cv
