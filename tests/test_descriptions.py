import json
import subprocess
from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator

from app import db, descriptions
from app.batch import build_batch, candidate_jobs, exchange_dir, withdraw_batch
from app.config import load_config
from app.resolver.complete import problems
from app.descriptions import DescriptionError, apply_description, clean_text, import_description_files, jobs_needing_description
from app.report import collect
from app.results import import_results
from app.schemas import BATCH_SCHEMA
from tests.test_batch_results import add_jobs, batch, count, results_file, score, status

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
POSTING = ("Key responsibilities\nYou analyse reporting requirements for the trading desks and document them for the "
           "developers. You test the reports and improve the daily data checks.\n\nYour skills and experience\n"
           "SQL, reporting tools, good communication in English, several years in a bank.")
COMPLETE_POSTING = POSTING + "\n\nAbout the team\n" + "We work in small teams and release every two weeks. " * 30      # 1,500 characters or more, tasks and requirements parts


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


def scored_job(conn, cfg, overall=50, n=1):
    """A job without a description that was exported and scored (title only)."""
    ids = add_jobs(conn, cfg, n, description="")
    batch(conn, cfg, "run1")
    import_results(conn, cfg, results_file(cfg, "run1", [score(i, overall) for i in ids]), NOW)
    return ids


# --- cleaning and validation ---------------------------------------------------------------------------

def test_clean_text():
    raw = "Line one  \r\n\r\n\r\n\r\nLine two\x00\x07\rLast\t"
    assert clean_text(raw) == "Line one\n\nLine two\nLast"


@pytest.mark.parametrize("text, message", [("", "only 0 characters"), ("too short", "only 9 characters"),
                                            ("x" * 100_001, "limit is 100000")])
def test_bad_text_is_refused(conn, cfg, text, message):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    with pytest.raises(DescriptionError, match=message):
        apply_description(conn, cfg, jid, text)
    assert conn.execute("SELECT description_missing FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 1


def test_unknown_or_archived_job_is_refused(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    db.archive_job(conn, jid, "user", "user_rejected")
    for bad in (jid, 9999):
        with pytest.raises(DescriptionError, match="not an open job"):
            apply_description(conn, cfg, bad, POSTING)


# --- storing -------------------------------------------------------------------------------------------

def test_description_is_stored_and_logged(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    r = apply_description(conn, cfg, jid, POSTING)
    row = conn.execute("SELECT description, description_missing, needs_rescore FROM jobs WHERE id=?", (jid,)).fetchone()
    assert (r.outcome, r.rescore, r.language) == ("stored", False, "pass")
    assert row["description"] == POSTING and row["description_missing"] == 0 and row["needs_rescore"] == 0
    log = conn.execute("SELECT source, chars, previous_status, rescore FROM description_log").fetchone()
    assert tuple(log) == ("manual", len(POSTING), "new", 0)


def test_the_same_text_again_changes_nothing(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    apply_description(conn, cfg, jid, POSTING)
    assert apply_description(conn, cfg, jid, POSTING + "\n\n").outcome == "unchanged"
    assert count(conn, "description_log") == 1


def test_language_rule_runs_on_the_new_text_and_can_archive(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    r = apply_description(conn, cfg, jid, POSTING + "\nFluent German (C1) is a must for this role, written and spoken.")
    assert (r.outcome, r.language) == ("archived", "fail")
    assert count(conn, "jobs") == 0
    row = conn.execute("SELECT reason_code, rejection_stage FROM job_archive").fetchone()
    assert tuple(row) == ("language_c1_required", "filter")


def test_a_conditional_language_result_keeps_the_job(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    r = apply_description(conn, cfg, jid, POSTING + "\nGood German skills are required.")
    assert (r.outcome, r.language) == ("stored", "conditional") and count(conn, "jobs") == 1
    reasons = [row[0] for row in conn.execute("SELECT reason_code FROM job_filter_results WHERE filter='language' ORDER BY id")]
    assert reasons == ["language_not_assessed", "language_level_unspecified"]        # the history is kept


# --- score again ---------------------------------------------------------------------------------------

def test_a_scored_job_is_marked_for_scoring_again(conn, cfg):
    jid = scored_job(conn, cfg, 50)[0]
    assert status(conn, jid) == "not_selected"                # queued, not ranked (v0.72.0)
    r = apply_description(conn, cfg, jid, POSTING)
    assert r.rescore and "scored again" in r.message
    assert conn.execute("SELECT needs_rescore FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 1


@pytest.mark.parametrize("state", ["shortlisted", "applied", "approved"])
def test_a_job_the_user_works_on_is_not_requeued(conn, cfg, state):
    jid = scored_job(conn, cfg)[0]
    db.set_status(conn, jid, state)
    assert apply_description(conn, cfg, jid, POSTING).rescore is False
    assert status(conn, jid) == state


def test_the_next_batch_scores_the_job_again_as_fresh(conn, cfg):
    jid = scored_job(conn, cfg, 50)[0]
    apply_description(conn, cfg, jid, POSTING)
    assert candidate_jobs(conn) == [(jid, True)]
    info = batch(conn, cfg, "run2", NOW)
    doc = json.loads(info.path.read_text(encoding="utf-8"))
    entry = doc["jobs"][0]
    assert (entry["job_id"], entry["fresh"], entry["reevaluation"], entry["previous_score"]) == (jid, True, True, 50)
    assert entry["description"] == POSTING and entry["description_missing"] is False
    assert list(Draft202012Validator(BATCH_SCHEMA).iter_errors(doc)) == []
    assert status(conn, jid) == "awaiting_llm"
    assert conn.execute("SELECT needs_rescore FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 0
    assert conn.execute("SELECT reevaluation, fresh FROM batch_jobs WHERE batch_id='run2'").fetchone()[:] == (1, 1)
    history = conn.execute("SELECT from_status, to_status, note FROM status_history WHERE job_id=? ORDER BY id DESC LIMIT 1", (jid,)).fetchone()
    assert tuple(history) == ("not_selected", "awaiting_llm", "batch run2 (scored again with a description)")


def test_the_new_score_replaces_the_capped_one_and_can_enter_the_top_ten(conn, cfg):
    ids = scored_job(conn, cfg, 55, n=3)                      # title-only scores, all capped territory
    apply_description(conn, cfg, ids[0], POSTING)
    batch(conn, cfg, "run2", NOW)
    r = import_results(conn, cfg, results_file(cfg, "run2", [score(ids[0], 88)], name="r2.json"), NOW)
    assert r.scored == [ids[0]] and r.top == [ids[0]]
    assert conn.execute("SELECT overall FROM scores WHERE job_id=? ORDER BY id DESC LIMIT 1", (ids[0],)).fetchone()[0] == 88
    assert count(conn, "scores") == 4                          # the old score stays as history
    assert status(conn, ids[0]) == "scored" and status(conn, ids[1]) == "not_selected"     # job 2 is queued (title-only, v0.72.0): not_selected


def test_the_report_says_the_job_was_scored_again(conn, cfg):
    jid = scored_job(conn, cfg, 55)[0]
    apply_description(conn, cfg, jid, POSTING)
    batch(conn, cfg, "run2", NOW)
    import_results(conn, cfg, results_file(cfg, "run2", [score(jid, 88)], name="r2.json"), NOW)
    report = collect(conn, cfg, "run2")
    assert report.jobs[0].notes == ["scored again with the full description"]
    assert "SCORED AGAIN WITH THE FULL DESCRIPTION" in report.text


def test_a_job_waiting_in_an_unscored_batch_is_also_requeued(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    batch(conn, cfg, "run1")                                   # exported, no results yet
    apply_description(conn, cfg, jid, POSTING)
    info = batch(conn, cfg, "run2", NOW)
    doc = json.loads(info.path.read_text(encoding="utf-8"))
    assert doc["jobs"][0]["reevaluation"] is True and doc["jobs"][0]["fresh"] is True


def test_withdrawing_a_batch_with_a_rescored_job_puts_it_back_as_scored(conn, cfg):
    jid = scored_job(conn, cfg, 50)[0]
    apply_description(conn, cfg, jid, POSTING)
    batch(conn, cfg, "run2", NOW)
    assert withdraw_batch(conn, cfg, "run2") == 1
    assert status(conn, jid) == "scored"
    assert conn.execute("SELECT needs_rescore FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 1
    assert candidate_jobs(conn) == [(jid, True)]               # it goes into the next batch again


# --- the list, the files, the clipboard ----------------------------------------------------------------

def test_missing_lists_the_best_scored_jobs_first(conn, cfg):
    ids = add_jobs(conn, cfg, 4, description="")
    build_batch(conn, cfg, "p", "v1", run_id="run1", now=NOW)
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 30), score(ids[1], 60), score(ids[2], 45)]), NOW)
    rows = jobs_needing_description(conn)
    assert [r["id"] for r in rows] == [ids[1], ids[2], ids[0], ids[3]]        # by score; the unscored job last
    assert rows[0]["score"] == 60 and rows[-1]["score"] is None and rows[0]["url"].startswith("https://x/")
    assert len(jobs_needing_description(conn, limit=2)) == 2
    apply_description(conn, cfg, ids[1], POSTING)
    assert ids[1] not in [r["id"] for r in jobs_needing_description(conn)]


def test_import_description_files(conn, cfg):
    ids = add_jobs(conn, cfg, 2, description="")
    folder = exchange_dir(cfg) / "descriptions"
    (folder / f"{ids[0]}.txt").write_text(COMPLETE_POSTING, encoding="utf-8")
    (folder / "notanumber.txt").write_text(POSTING, encoding="utf-8")
    (folder / f"{ids[1]}.txt").write_text("too short", encoding="utf-8")
    out = dict(import_description_files(conn, cfg))
    assert "description stored" in out[f"{ids[0]}.txt"] and "job id" in out["notanumber.txt"] and "only 9 characters" in out[f"{ids[1]}.txt"]
    assert (folder / "done" / f"{ids[0]}.txt").exists() and (folder / "failed" / "notanumber.txt").exists()
    assert "only 9 characters" in (folder / "failed" / f"{ids[1]}.txt.reason.txt").read_text(encoding="utf-8")
    assert list(folder.glob("*.txt")) == [] and import_description_files(conn, cfg) == []


def test_read_clipboard(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="Zürich text".encode("utf-8"), stderr=b""))
    assert descriptions.read_clipboard() == "Zürich text"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=b"", stderr=b"x"))
    with pytest.raises(DescriptionError, match="cannot read the clipboard"):
        descriptions.read_clipboard()
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(DescriptionError, match="OSError"):
        descriptions.read_clipboard()


# --- running the language rule again on stored descriptions ----------------------------------------------

def test_recheck_language_archives_a_job_the_new_rule_rejects(conn, cfg):
    import copy
    old_rule = copy.deepcopy(cfg)
    old_rule["languages"]["names"] = [e for e in old_rule["languages"]["names"] if e["language"] != "Turkish"]     # the old rule did not know Turkish
    jid = add_jobs(conn, old_rule, 1, description="Your role: analyse requirements for the trading desks. Languages: fluent in Turkish and "
                                                   "English, written and spoken. You work with the developers and test the reports every day.")[0]
    assert count(conn, "jobs") == 1 and status(conn, jid) == "new"                    # it passed under the old rule
    counts = descriptions.recheck_language(conn, cfg)                                 # the current rule knows Turkish
    assert counts == {"checked": 1, "changed": 1, "archived": 1} and count(conn, "jobs") == 0
    assert conn.execute("SELECT reason_code FROM job_archive").fetchone()[0] == "language_c1_required"


@pytest.mark.parametrize("user_status", descriptions.USER_STATUSES)
def test_recheck_language_skips_jobs_the_user_works_on(conn, cfg, user_status):
    import copy
    old_rule = copy.deepcopy(cfg)
    old_rule["languages"]["names"] = [e for e in old_rule["languages"]["names"] if e["language"] != "Turkish"]
    jid = add_jobs(conn, old_rule, 1, description="Your role: analyse requirements for the trading desks. Languages: fluent in Turkish and "
                                                   "English, written and spoken. You work with the developers and test the reports every day.")[0]
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (user_status, jid))
    conn.commit()
    before = count(conn, "job_filter_results")
    counts = descriptions.recheck_language(conn, cfg)
    assert counts["archived"] == 0 and count(conn, "jobs") == 1 and status(conn, jid) == user_status
    assert count(conn, "job_filter_results") == before                                  # no new language row either


def test_recheck_language_dry_run_writes_nothing(conn, cfg):
    import copy
    old_rule = copy.deepcopy(cfg)
    old_rule["languages"]["names"] = [e for e in old_rule["languages"]["names"] if e["language"] != "Turkish"]
    text = ("Your role: analyse requirements for the trading desks. Languages: fluent in Turkish and "
            "English, written and spoken. You work with the developers and test the reports every day.")
    open_id, user_id = add_jobs(conn, old_rule, 2, description=text)
    conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (user_id,))
    conn.commit()
    before = (count(conn, "jobs"), count(conn, "job_filter_results"), count(conn, "job_archive"))
    counts = descriptions.recheck_language(conn, cfg, dry_run=True)
    assert counts["skipped"] == 1 and counts["archived"] == 1 and counts["changed"] == 1
    assert (count(conn, "jobs"), count(conn, "job_filter_results"), count(conn, "job_archive")) == before
    assert status(conn, open_id) == "new"


def test_recheck_language_changes_nothing_the_second_time(conn, cfg):
    add_jobs(conn, cfg, 2, description="Business analyst for reporting. Good German skills are required for this role in Zürich, "
                                        "and you work with SQL, requirements and the developers of the reporting team every day.")
    first = descriptions.recheck_language(conn, cfg)
    again = descriptions.recheck_language(conn, cfg)
    assert first["checked"] == 2 and again == {"checked": 2, "changed": 0, "archived": 0} and count(conn, "jobs") == 2


def test_recheck_language_skips_jobs_without_a_description(conn, cfg):
    add_jobs(conn, cfg, 2, description="")
    assert descriptions.recheck_language(conn, cfg) == {"checked": 0, "changed": 0, "archived": 0}


# --- a job with only a language result is not "filtered" (found when a restored job skipped its other filters) ---

def test_a_restored_job_gets_every_filter_even_after_recheck_language(conn, cfg):
    from app.filters.runner import filter_new_jobs
    jid = add_jobs(conn, cfg, 1, description="Senior role. " * 30)[0]
    aid = db.archive_job(conn, jid, "filter", "language_c1_required", "wrong rejection")
    new_id = db.restore_job(conn, aid)
    descriptions.recheck_language(conn, cfg)                                  # stores a language result only
    filters = {r[0] for r in conn.execute("SELECT filter FROM job_filter_results WHERE job_id = ?", (new_id,))}
    assert filters == {"language"}
    assert candidate_jobs(conn) == []                                         # not exported while the other filters are missing
    assert filter_new_jobs(conn, cfg)["pass"] == 1                            # prepare runs all of them
    filters = {r[0] for r in conn.execute("SELECT filter FROM job_filter_results WHERE job_id = ?", (new_id,))}
    assert filters == {"seniority", "title", "company", "language"}
    assert candidate_jobs(conn) == [(new_id, True)]


def test_a_job_with_only_a_language_result_counts_as_unfiltered_for_the_fetcher(conn, cfg):
    from app.descriptions_fetch import eligible_jobs
    from app.models import NormalizedJob
    from app.dedup import ingest_job
    jid = ingest_job(conn, NormalizedJob(source="ubs_alert", external_id="5", title="Analyst", company="UBS",
                                         url="https://jobs.ubs.com/x?jobid=5", locations=["Zürich"])).job_id
    from app.filters.language import language
    from app.filters.runner import run_filters
    run_filters(conn, jid, cfg, filters=[language])
    assert eligible_jobs(conn)[0]["filtered"] == 0


# --- rescore: mark open jobs for scoring again (v0.43.0) --------------------------------------------------

def flag(conn, job_id):
    return conn.execute("SELECT needs_rescore FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def test_rescore_marks_scored_jobs_and_the_next_batch_has_them_as_reevaluation(conn, cfg):
    ids = scored_job(conn, cfg, 60, n=2)
    items = descriptions.mark_rescore(conn, ids)
    assert [(i.job_id, i.old, i.new, i.skipped) for i in items] == [(ids[0], 0, 1, ""), (ids[1], 0, 1, "")]
    assert [flag(conn, i) for i in ids] == [1, 1]
    assert [j for j, fresh in candidate_jobs(conn)] == ids and all(f for _, f in candidate_jobs(conn))
    info = batch(conn, cfg, "run2")
    entries = json.loads(info.path.read_text(encoding="utf-8"))["jobs"]
    assert {e["job_id"] for e in entries} == set(ids) and all(e["reevaluation"] for e in entries)


def test_rescore_dry_run_writes_nothing(conn, cfg):
    ids = scored_job(conn, cfg, 60)
    items = descriptions.mark_rescore(conn, ids, dry_run=True)
    assert items[0].old == 0 and items[0].new == 1 and flag(conn, ids[0]) == 0


def test_rescore_twice_changes_nothing_the_second_time(conn, cfg):
    ids = scored_job(conn, cfg, 60)
    descriptions.mark_rescore(conn, ids)
    again = descriptions.mark_rescore(conn, ids + ids)       # a repeated id is handled once
    assert [(i.old, i.new) for i in again] == [(1, 1)] and flag(conn, ids[0]) == 1


@pytest.mark.parametrize("status", ["shortlisted", "approved", "applied", "interview", "offer", "rejected", "withdrawn"])
def test_rescore_skips_a_job_the_user_works_on(conn, cfg, status):
    ids = scored_job(conn, cfg, 60)
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, ids[0]))
    items = descriptions.mark_rescore(conn, ids)
    assert items[0].skipped and status in items[0].skipped and (items[0].old, items[0].new) == (0, 0)
    assert flag(conn, ids[0]) == 0


def test_rescore_skips_a_job_that_is_not_scored_yet(conn, cfg):
    jid = add_jobs(conn, cfg, 1)[0]
    conn.execute("UPDATE jobs SET status = 'new' WHERE id = ?", (jid,))
    items = descriptions.mark_rescore(conn, [jid])
    assert "not scored yet" in items[0].skipped and flag(conn, jid) == 0


def test_rescore_refuses_an_unknown_or_archived_job_and_changes_nothing(conn, cfg):
    ids = scored_job(conn, cfg, 60, n=2)
    with pytest.raises(DescriptionError, match="job 999 is not an open job"):
        descriptions.mark_rescore(conn, [ids[0], 999])
    assert flag(conn, ids[0]) == 0
    db.archive_job(conn, ids[1], "user", "user_rejected")
    with pytest.raises(DescriptionError, match=f"job {ids[1]} is archived"):
        descriptions.mark_rescore(conn, [ids[0], ids[1]])
    assert flag(conn, ids[0]) == 0


# --- only complete text on the manual paths (goal.md 5.1, v0.72.0) ---------------------------------------

def test_complete_problems_names_length_tasks_and_requirements():
    assert problems(COMPLETE_POSTING) == []
    short = problems(POSTING)
    assert len(short) == 1 and short[0].startswith("only ") and "characters (at least 500)" in short[0]
    padding = "Lorem ipsum dolor sit amet. " * 70
    no_tasks = problems("Welcome to the bank.\nYour profile\n" + padding)
    assert any("no tasks part found" in p for p in no_tasks) and not any("no requirements part" in p for p in no_tasks)
    no_req = problems("Welcome to the bank.\nKey responsibilities\n" + padding)
    assert any("no requirements part found" in p for p in no_req) and not any("no tasks part" in p for p in no_req)
    assert len(problems("")) == 3


def test_manual_text_under_500_characters_is_refused_and_nothing_changes(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    with pytest.raises(DescriptionError, match="the text is not complete: only .* characters"):
        apply_description(conn, cfg, jid, POSTING, require_complete=True)
    row = conn.execute("SELECT description_missing, needs_rescore FROM jobs WHERE id=?", (jid,)).fetchone()
    assert tuple(row) == (1, 0) and count(conn, "description_log") == 0


def test_manual_text_of_500_characters_with_both_parts_is_accepted(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    text = POSTING + "\n\nAbout the team\n" + "We release every two weeks. " * 12
    assert 500 <= len(text) < 1500 and problems(text) == []
    assert apply_description(conn, cfg, jid, text, require_complete=True).outcome == "stored"


def test_manual_text_without_a_tasks_part_is_stored_with_a_warning(conn, cfg, caplog):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    text = "Welcome to the bank.\nYour profile\n" + "Lorem ipsum dolor sit amet. " * 70
    with caplog.at_level("WARNING"):
        res = apply_description(conn, cfg, jid, text, require_complete=True)
    assert res.outcome == "stored" and "no tasks part found" in res.warning and "no requirements" not in res.warning
    assert "warning: no tasks part found" in res.message
    assert any("event=description_parts_missing" in r.getMessage() for r in caplog.records)
    assert conn.execute("SELECT description_missing FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 0


def test_manual_text_without_a_requirements_part_is_stored_with_a_warning(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    text = "Welcome to the bank.\nKey responsibilities\n" + "Lorem ipsum dolor sit amet. " * 70
    res = apply_description(conn, cfg, jid, text, require_complete=True)
    assert res.outcome == "stored" and "no requirements part found" in res.warning
    assert conn.execute("SELECT description_missing FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 0


def test_a_complete_manual_text_has_no_warning_and_automatic_paths_never_warn(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    assert apply_description(conn, cfg, jid, COMPLETE_POSTING, require_complete=True).warning == ""
    other = add_jobs(conn, cfg, 1, start=2, description="")[0]
    assert apply_description(conn, cfg, other, "No headings here at all. " * 20).warning == ""


def test_a_complete_text_passes_with_require_complete(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    assert apply_description(conn, cfg, jid, COMPLETE_POSTING, require_complete=True).outcome == "stored"


def test_import_descriptions_moves_incomplete_text_to_failed_with_the_reason(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    folder = exchange_dir(cfg) / "descriptions"
    (folder / f"{jid}.txt").write_text(POSTING, encoding="utf-8")
    out = dict(import_description_files(conn, cfg))
    assert "the text is not complete" in out[f"{jid}.txt"]
    assert (folder / "failed" / f"{jid}.txt").exists()
    assert "only " in (folder / "failed" / f"{jid}.txt.reason.txt").read_text(encoding="utf-8")
    assert conn.execute("SELECT description_missing FROM jobs WHERE id=?", (jid,)).fetchone()[0] == 1


def test_automatic_paths_keep_the_200_character_minimum(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    text = "A short employer page without any headings. " * 6          # about 260 characters, not complete
    assert 200 <= len(text) < 1500
    assert apply_description(conn, cfg, jid, text).outcome == "stored"       # the default of the automatic callers
    other = add_jobs(conn, cfg, 1, start=2, description="")[0]
    with pytest.raises(DescriptionError, match="only 9 characters"):
        apply_description(conn, cfg, other, "too short")


# --- pasted LinkedIn pages, headings, the same text on two companies (v0.78.4) ---------------------------------------
BODY_YOU_WILL = ("You Will\nAnalyse reporting needs of the trading desks and write them down for the developers.\n\n"
                 "Your profile\nSQL, reporting tools, good English, several years in a bank.\n\nAbout the team\n"
                 + "We work in small teams and release every two weeks. " * 30)


def linkedin_page(body):
    """The shape of a whole LinkedIn page as the 16 stored copies have it (checked on a copy of the NAS database, 10 Nov 2026)."""
    return ("RTB House\n\nSenior Business Analyst\n\nRestart Premium for CHF 0\n\nAbout the job\n\n" + body
            + "\n… more\nSet alert for similar jobs\n\nApplicant education level\n\nAbout the company\n\nShow more\nMore jobs\n"
            + "Your responsibilities and experience are shown on Premium.\nPolski (Polish)\n")


def test_a_linkedin_page_is_cut_to_the_posting():
    assert descriptions.strip_page_clutter(linkedin_page("Line one\nLine two")) == "Line one\nLine two"


@pytest.mark.parametrize("text", [
    "About the job\nA posting without the footer of the page",
    "A posting\nSet alert for similar jobs\nmore text",
    "Set alert for similar jobs\nAbout the job\nThe markers in the wrong order",
    COMPLETE_POSTING,
])
def test_a_text_without_both_page_markers_in_order_is_left_alone(text):
    assert descriptions.strip_page_clutter(text) == text


def test_a_marker_inside_a_sentence_does_not_cut():
    text = "Read About the job below\nbody\nPlease Set alert for similar jobs today"
    assert descriptions.strip_page_clutter(text) == text


def test_words_in_the_page_around_the_posting_no_longer_make_it_complete(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    res = apply_description(conn, cfg, jid, linkedin_page("A summary without any headings. " * 80), require_complete=True)
    assert "no tasks part" in res.warning                                         # v0.85.3: a warning, no longer a refusal


def test_a_pasted_linkedin_page_is_stored_as_the_posting_only(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    apply_description(conn, cfg, jid, linkedin_page(COMPLETE_POSTING), require_complete=True)
    stored = conn.execute("SELECT description FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
    assert stored == COMPLETE_POSTING.strip() and "Premium" not in stored


def test_the_automatic_paths_do_not_cut_anything(conn, cfg):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    page = linkedin_page(COMPLETE_POSTING)
    apply_description(conn, cfg, jid, page)
    assert "Premium" in conn.execute("SELECT description FROM jobs WHERE id = ?", (jid,)).fetchone()[0]


@pytest.mark.parametrize("heading", ["You Will", "you will:", "You'll", "You’ll"])
def test_a_you_will_heading_counts_as_a_tasks_part(heading):
    assert problems(BODY_YOU_WILL.replace("You Will", heading, 1)) == []


def test_you_will_in_a_sentence_is_not_a_tasks_part():
    text = BODY_YOU_WILL.replace("You Will\n", "In this company you will\nfind friendly people. ", 1)
    assert any("tasks" in p for p in problems(text))


def test_polish_headings_count():
    text = "Zadania:\nAnaliza procesów.\n\nCzego oczekujemy:\nMinimum 5 lat w roli analityka.\n\n" + "Pracujemy w małych zespołach. " * 60
    assert problems(text) == []


def two_companies(conn, cfg):
    first, second = add_jobs(conn, cfg, 2, description="")
    conn.execute("UPDATE jobs SET company = 'Vontobel' WHERE id = ?", (first,))
    conn.execute("UPDATE jobs SET company = 'RTB House' WHERE id = ?", (second,))
    conn.commit()
    return first, second


def test_the_same_text_on_a_job_of_another_company_is_allowed_and_logged(conn, cfg, caplog):
    first, second = two_companies(conn, cfg)
    apply_description(conn, cfg, first, COMPLETE_POSTING, require_complete=True)
    with caplog.at_level("WARNING"):
        assert apply_description(conn, cfg, second, COMPLETE_POSTING, require_complete=True).outcome == "stored"
    assert f"job {first} (Vontobel)" in caplog.text


def test_the_same_text_is_found_when_the_stored_copy_still_has_the_page_around_it(conn, cfg):
    first, second = two_companies(conn, cfg)
    apply_description(conn, cfg, first, linkedin_page(COMPLETE_POSTING))                    # how the 16 old copies are stored
    assert apply_description(conn, cfg, second, linkedin_page(COMPLETE_POSTING), require_complete=True).outcome == "stored"


def test_the_same_text_on_two_jobs_of_one_company_is_allowed(conn, cfg):
    first, second = add_jobs(conn, cfg, 2, description="")
    conn.execute("UPDATE jobs SET company = 'Vontobel'")
    conn.commit()
    apply_description(conn, cfg, first, COMPLETE_POSTING, require_complete=True)
    assert apply_description(conn, cfg, second, COMPLETE_POSTING, require_complete=True).outcome == "stored"


def test_the_automatic_paths_are_not_refused_for_the_same_text(conn, cfg):
    first, second = two_companies(conn, cfg)
    apply_description(conn, cfg, first, COMPLETE_POSTING)
    assert apply_description(conn, cfg, second, COMPLETE_POSTING).outcome == "stored"


@pytest.mark.parametrize("user_status", ["shortlisted", "applied", "interview"])
def test_a_c1_text_never_archives_a_job_the_user_works_on(conn, cfg, user_status):
    jid = add_jobs(conn, cfg, 1, description="")[0]
    db.set_status(conn, jid, user_status)
    text = POSTING + "\nFluent German (C1) is a must for this role, written and spoken."
    r = apply_description(conn, cfg, jid, text)
    assert r.outcome == "stored"
    assert status(conn, jid) == user_status and count(conn, "jobs") == 1 and count(conn, "job_archive") == 0
    assert "C1" in conn.execute("SELECT description FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
