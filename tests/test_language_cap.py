"""Cap for a posting that is not written in English (goal.md 5.4, decided by the user on 1 Nov 2026).

No network, no real config, no real database."""
import html
import json
import re
from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator

from app import db
from app.batch import build_batch, exchange_dir
from app.config import ConfigError, ROOT, load_config
from app.dedup import ingest_job
from app.filters.language import posting_is_german
from app.filters.runner import filter_new_jobs
from app.models import NormalizedJob
from app.prompt import load_prompt
from app.report import build_message, collect
from app.results import import_results
from app.schemas import BATCH_SCHEMA

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
SUBS = {"skills": 80, "experience": 75, "domain": 70, "seniority": 80, "location": 100}      # formula: 79

GERMAN = ("Wir suchen eine erfahrene Business Analystin oder einen erfahrenen Business Analysten für unser Team. "
          "Sie sind verantwortlich für die Analyse der Anforderungen und die Zusammenarbeit mit den Fachbereichen. "
          "Ihre Aufgaben sind die Erhebung und Dokumentation von Anforderungen sowie die Abstimmung mit der IT. "
          "Wir bieten Ihnen eine abwechslungsreiche Tätigkeit und ein starkes Team.")
ENGLISH_WITH_FOOTER = ("We are looking for a Business Analyst to join our team. You will work with the business and the IT "
                       "teams to define the requirements and you will be responsible for the testing of the platform. "
                       "Our team is based in Zurich and you will have the support of the whole department, with the "
                       "tools that you need to deliver the work on time. We offer a flexible environment for the "
                       "people that join us and a clear path for your growth in the company. "
                       "Wir freuen uns auf Ihre Bewerbung. Mit freundlichen Grüssen, das Team.")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"]["exchange"] = str(tmp_path / "exchange")
    return c


def _import(cfg, tmp_path, overall, description, subs=SUBS, version="1.0"):
    conn = db.connect(tmp_path / "t.db")
    jid = ingest_job(conn, NormalizedJob(source="s", external_id="1", title="Senior IT-Business Analyst", company="A",
                                         locations=["Zürich"], description=description)).job_id
    filter_new_jobs(conn, cfg)
    build_batch(conn, cfg, "p", "v1", run_id="r1", now=NOW)
    entry = {"job_id": jid, "overall": overall, "subscores": subs, "matches": [], "gaps": [], "red_flags": [],
             "confidence": "high", "summary": "s", "cv_variant": "resume"}
    path = exchange_dir(cfg) / "results" / "r.json"
    path.write_text(json.dumps({"schema_version": version, "run_id": "r1", "model": "m", "prompt_version": "v1",
                                "results": [entry], "top": [jid]}), encoding="utf-8")
    result = import_results(conn, cfg, path, NOW)
    return conn, jid, result


def _stored(conn, jid):
    return conn.execute("SELECT overall FROM scores WHERE job_id = ?", (jid,)).fetchone()["overall"]


# --- prompt and config stay in sync ---------------------------------------------------------------

def test_prompt_version_is_1_9_0_and_states_the_cap_of_the_config(cfg):
    text, version = load_prompt(cfg)
    assert version == "1.18.0"
    cap = cfg["scoring"]["non_english_cap"]
    assert f"at most {cap}, the top of the low band" in text and f"min(formula, {cap})" in text
    assert cap == 49 < cfg["scoring"]["band_low_below"]                     # rule changed 69 -> 49 by user, 14 Nov 2026: low band
    assert "decided by the user on 1 Nov 2026" in text and "German" in text


def test_prompt_example_uses_the_current_version(cfg):
    text, version = load_prompt(cfg)
    example = json.loads(re.search(r"```json\n(.+?)\n```", text, re.DOTALL).group(1))
    assert example["prompt_version"] == version


def test_prompt_tells_the_session_about_the_flag_and_the_footer_case(cfg):
    text, _ = load_prompt(cfg)
    assert "posting_language_english" in text and "German company footer" in text and "the lower one counts" in text


@pytest.mark.parametrize("cap", [70, 71, 100, -1, "69", True, 69.5])
def test_config_rejects_a_cap_that_could_be_high(tmp_path, cap):
    f = tmp_path / "c.yaml"
    f.write_text(f"scoring:\n  non_english_cap: {json.dumps(cap)}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="non_english_cap"):
        load_config(f)


def test_config_cap_must_stay_below_a_moved_high_band(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("scoring:\n  band_low_below: 20\n  band_high_from: 45\n", encoding="utf-8")          # default cap 49 is no longer below the band
    with pytest.raises(ConfigError, match="non_english_cap"):
        load_config(f)
    f.write_text("scoring:\n  band_low_below: 20\n  band_high_from: 45\n  non_english_cap: 44\n", encoding="utf-8")
    assert load_config(f)["scoring"]["non_english_cap"] == 44


def test_default_cap_is_49_and_below_the_high_band(cfg):
    assert cfg["scoring"]["non_english_cap"] == 49 < cfg["scoring"]["band_high_from"]


# --- detector -------------------------------------------------------------------------------------

def test_detector_reads_german_and_not_english_with_a_footer(cfg):
    assert posting_is_german(GERMAN, cfg) is True
    assert posting_is_german(ENGLISH_WITH_FOOTER, cfg) is False
    assert posting_is_german("", cfg) is False and posting_is_german(None, cfg) is False


# --- batch ----------------------------------------------------------------------------------------

def test_batch_carries_the_caps_and_flags_a_german_job(cfg, tmp_path):
    conn = db.connect(tmp_path / "t.db")
    ids = [ingest_job(conn, NormalizedJob(source="s", external_id=str(i), title=f"Analyst {i}", company="A", locations=["Zürich"],
                                          description=d)).job_id
           for i, d in ((1, GERMAN), (2, ENGLISH_WITH_FOOTER))]
    filter_new_jobs(conn, cfg)
    info = build_batch(conn, cfg, "p", "v1", run_id="r1", now=NOW)
    doc = json.loads(info.path.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(BATCH_SCHEMA).iter_errors(doc)) == []
    assert doc["schema_version"] == "1.1"
    assert doc["scoring"] == {"no_description_cap": 60, "non_english_cap": 49}
    flags = {j["job_id"]: j.get("posting_language_english") for j in doc["jobs"]}
    assert flags == {ids[0]: False, ids[1]: None}                         # absent for the English posting
    conn.close()


# --- importer -------------------------------------------------------------------------------------

def test_a_german_posting_is_clamped_to_49_with_a_warning(cfg, tmp_path):
    conn, jid, r = _import(cfg, tmp_path, 79, GERMAN)
    assert r.status == "imported" and r.scored == [jid]
    assert _stored(conn, jid) == 49
    assert any("not in English" in w and f"{jid}: 79 -> 49" in w for w in r.warnings)
    assert not any("overall differs" in w for w in r.warnings)             # one warning, not two
    conn.close()


def test_a_clamped_job_is_ranked_and_selected_with_the_clamped_value(cfg, tmp_path):
    conn, jid, r = _import(cfg, tmp_path, 79, GERMAN)
    assert r.top == [jid]
    row = conn.execute("SELECT s.overall FROM batch_jobs bj JOIN scores s ON s.job_id = bj.job_id WHERE bj.selected = 1").fetchone()
    assert row["overall"] == 49 < cfg["scoring"]["band_high_from"]
    conn.close()


@pytest.mark.parametrize("overall", [40, 49])
def test_a_german_posting_at_or_below_the_cap_is_untouched(cfg, tmp_path, overall):
    conn, jid, r = _import(cfg, tmp_path, overall, GERMAN, subs={"skills": 70, "experience": 70, "domain": 70, "seniority": 70, "location": 70})
    assert _stored(conn, jid) == overall
    assert not any("not in English" in w for w in r.warnings)
    conn.close()


def test_an_english_posting_is_not_touched(cfg, tmp_path):
    conn, jid, r = _import(cfg, tmp_path, 79, "We are looking for a Business Analyst. You will work with the team and the business.")
    assert _stored(conn, jid) == 79
    assert not any("not in English" in w for w in r.warnings)
    conn.close()


def test_english_with_a_german_footer_is_not_capped(cfg, tmp_path):
    conn, jid, r = _import(cfg, tmp_path, 79, ENGLISH_WITH_FOOTER)
    assert _stored(conn, jid) == 79
    assert not any("not in English" in w for w in r.warnings)
    conn.close()


def test_both_caps_the_lower_one_counts(cfg, tmp_path):
    # raising the no-description cap above the language cap is not what is tested: use a language cap below it
    cfg["scoring"]["non_english_cap"] = 55
    conn, jid, r = _import(cfg, tmp_path, 79, GERMAN)
    assert _stored(conn, jid) == 55
    conn.close()


def test_expected_overall_uses_the_lower_of_both_caps(cfg):
    from app.prompt import weighted_overall
    w = cfg["scoring"]["weights"]
    assert weighted_overall(SUBS, w, min(60, 69)) == 60 and weighted_overall(SUBS, w, min(60, 55)) == 55


def test_a_german_posting_gets_no_documents_from_the_clamped_score(cfg, tmp_path):
    # 49 is below tailoring.min_score (60): no documents are asked for (cap changed 69 -> 49 on 14 Nov 2026)
    conn, jid, r = _import(cfg, tmp_path, 79, GERMAN, version="1.1")
    assert _stored(conn, jid) == 49 < cfg["tailoring"]["min_score"]
    assert not r.documents_missing
    conn.close()


# --- recheck-language --cap-scores (14 Nov 2026) --------------------------------------------------

def _scored(conn, jid, overall):
    conn.execute("UPDATE scores SET overall = ? WHERE job_id = ?", (overall, jid))
    conn.commit()


def _two_extra(conn, cfg):
    """A German row already at 49 and an English row at 79 next to the German one at 79."""
    ids = {}
    for ext, desc in (("2", GERMAN), ("3", "We are looking for a Business Analyst. You will work with the team and the business.")):
        ids[ext] = ingest_job(conn, NormalizedJob(source="s", external_id=ext, title=f"T{ext}", company="B", locations=["Zürich"],
                                                  description=desc)).job_id
        conn.execute("INSERT INTO scores (job_id, prompt_version, model, overall, subscores, created_at) "
                     "VALUES (?, 'v', 'm', ?, '{}', 'x')", (ids[ext], 49 if ext == "2" else 79))
    conn.commit()
    return ids


def test_cap_scores_dry_run_writes_nothing_and_lists_the_german_job(cfg, tmp_path):
    from app import descriptions
    conn, jid, _ = _import(cfg, tmp_path, 79, GERMAN)
    _scored(conn, jid, 79)
    _two_extra(conn, cfg)
    rows = descriptions.cap_german_scores(conn, cfg)
    assert [(r["job_id"], r["old"], r["new"]) for r in rows] == [(jid, 79, 49)]
    assert _stored(conn, jid) == 79
    conn.close()


def test_cap_scores_apply_lowers_only_german_rows_above_the_cap(cfg, tmp_path):
    from app import descriptions
    conn, jid, _ = _import(cfg, tmp_path, 79, GERMAN)
    _scored(conn, jid, 79)
    ids = _two_extra(conn, cfg)
    assert len(descriptions.cap_german_scores(conn, cfg, apply=True)) == 1
    assert _stored(conn, jid) == 49
    assert _stored(conn, ids["2"]) == 49 and _stored(conn, ids["3"]) == 79
    assert descriptions.cap_german_scores(conn, cfg, apply=True) == []        # second run changes nothing
    conn.close()


# --- report ---------------------------------------------------------------------------------------

def test_the_report_notes_the_language_cap(cfg, tmp_path):
    conn, jid, r = _import(cfg, tmp_path, 79, GERMAN)
    rep = collect(conn, cfg, "r1")
    job = rep.jobs[0]
    assert job.score == 49 and job.band == "low"
    assert job.language_note == "posting in German: score capped at 49 (low)"
    msg = build_message(cfg, rep)
    body = msg.get_body(("plain",)).get_content()
    page = msg.get_body(("html",)).get_content()
    assert "Note: posting in German: score capped at 49 (low)" in body
    assert "Posting in German: score capped at 49 (low)" in page
    conn.close()


def test_the_report_note_is_html_escaped(cfg, tmp_path, monkeypatch):
    import app.report as report
    monkeypatch.setattr(report, "NON_ENGLISH", "posting in German <b>x</b> & more {cap}")
    conn, jid, r = _import(cfg, tmp_path, 79, GERMAN)
    rep = collect(conn, cfg, "r1")
    msg = build_message(cfg, rep)
    page = msg.get_body(("html",)).get_content()
    assert "<b>x</b>" not in page and html.escape("<b>x</b>") in page
    conn.close()


def test_no_note_for_an_english_posting(cfg, tmp_path):
    conn, jid, r = _import(cfg, tmp_path, 79, "We are looking for a Business Analyst. You will work with the team and the business.")
    assert collect(conn, cfg, "r1").jobs[0].language_note == ""
    conn.close()
