"""v0.85.0: the worker path of "Ask the writer to fix" (app/headless.py `rev-<id>` requests, goal.md 5.10a). The fake CLI of
tests/fixtures/fake_claude.py plays the writer and the reviewer. No network, no model."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import headless, scheduler
from tests.test_documents import docs
from tests.test_headless import World, world                                           # noqa: F401

JOB_ID = 812


def roles_for(kind):
    writer = "letters" if kind == "cover_letter" else "scorer"
    return {writer: {"model": "claude-sonnet-5-5", "effort": "medium", "timeout_minutes": 20, "max_turns": 40},
            "review": {"model": "claude-opus-5-5", "effort": "low", "timeout_minutes": 20, "max_turns": 40}}


def revised_of(kind):
    doc = copy.deepcopy(docs()[kind])
    if kind == "cover_letter":
        doc["blocks"]["lead_in"] = "Three results from my work show how I would contribute:"
    else:
        doc["summary"] += " I like clear requirements."
    return doc


def put_payloads(world, kind, revised=None):
    revised = revised if revised is not None else revised_of(kind)
    (world.fake / "payload" / "revision_doc.json").write_text(
        json.dumps({"run_id": "RUNID", "results": [{"job_id": JOB_ID, "documents": {kind: revised}}]}), encoding="utf-8")
    other = "cv" if kind == "cover_letter" else "cover_letter"
    (world.fake / "payload" / "revision_review.json").write_text(json.dumps({"attention": [
        {"job_id": JOB_ID, "kind": kind, "note": "the tone is a little stiff"},
        {"job_id": JOB_ID, "kind": other, "note": "not this document"},
        {"job_id": 5, "kind": kind, "note": "other job"}]}), encoding="utf-8")


def rev_request(world, rid=17, kind="cover_letter", instruction="Make it warmer.", minutes=75, **over):
    req = {"kind": "revision", "run_id": f"rev-{rid}", "revision_id": rid,
           "deadline": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
           "document_kind": kind, "content_sha": "a" * 64, "instruction": instruction,
           "findings": [{"kind": "language_claim", "where": "skills", "text": "x", "reason": "r", "warning": False}],
           "job": {"job_id": JOB_ID, "title": "Analyst", "company": "Acme", "locations": ["Zurich"], "description": "d", "german_required": False},
           "document": copy.deepcopy(docs()[kind]), "roles": roles_for(kind), **over}
    folder = world.exchange / "llm" / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"rev-{rid}.json").write_text(json.dumps(req), encoding="utf-8")
    return req


def run_rev(world, rid=17, kind="cover_letter", payload=True, **over):
    if payload:
        put_payloads(world, kind)
    req = rev_request(world, rid, kind, **over)
    world.worker().poll_once()
    return json.loads((world.exchange / "llm" / "done" / f"rev-{rid}.json").read_text(encoding="utf-8")), req


@pytest.mark.parametrize("kind, writer", [("cover_letter", "letters"), ("cv", "scorer")])
def test_one_writer_call_and_one_reviewer_call_and_the_done_file_carries_the_result(world, kind, writer):
    done, req = run_rev(world, kind=kind)
    calls = world.role_calls()
    assert [c["role"] for c in calls] == [writer, "review"]
    assert all(c["revision_addendum"] for c in calls)                                    # the revision addendum, not the daily one
    assert all(c["cwd"].replace("\\", "/").endswith("/llm/revisions/rev-17") for c in calls)
    assert "Make it warmer." in calls[0]["brief"] and "document_rev-17.json" in calls[0]["brief"]
    assert done["status"] == "ok" and done["run_id"] == "rev-17" and done["revision_id"] == 17
    assert done["document_kind"] == kind and done["job_id"] == JOB_ID and done["content_sha"] == "a" * 64
    assert done["document"] == revised_of(kind) and done["started"] and done["ended"]
    assert done["attention"] == [{"job_id": JOB_ID, "kind": kind, "note": "the tone is a little stiff"}]       # only this document
    assert done["review_summary"].startswith("The revision is fine") and set(done["roles"]) == {writer, "review"}
    assert world.results() == [] and not (world.work("rev-17")).exists()                    # nothing moves to results; no state.json work dir


def test_an_empty_instruction_gives_the_fix_the_blocking_findings_brief(world):
    run_rev(world, instruction="")
    assert "No instruction from the user: fix the blocking findings." in world.role_calls()[0]["brief"]


def test_usage_limit_in_the_writer_stops_without_review_and_document(world):
    world.mode({"letters": "limit"})
    done, _ = run_rev(world)
    assert done["status"] == "usage_limit" and "resets" in done["limit_message"] and "document" not in done
    assert [c["role"] for c in world.role_calls()] == ["letters"]


def test_auth_in_the_writer_stops_the_revision(world):
    world.mode({"scorer": "auth"})
    done, _ = run_rev(world, kind="cv")
    assert done["status"] == "auth" and "document" not in done and [c["role"] for c in world.role_calls()] == ["scorer"]


@pytest.mark.parametrize("behaviour, expected", [("crash", "error"), ("limit", "usage_limit"), ("no_report", "error")])
def test_a_review_that_does_not_finish_stores_nothing(world, behaviour, expected):
    world.mode({"review": behaviour})
    done, _ = run_rev(world)
    assert done["status"] == expected and "document" not in done                         # an unreviewed text is never stored
    assert [c["role"] for c in world.role_calls()] == ["letters", "review"]


def test_an_invalid_document_gets_no_review(world):
    put_payloads(world, "cover_letter", revised={"contact_name": None, "blocks": {"opening": "only this"}})
    done, _ = run_rev(world, payload=False)
    assert done["status"] == "error" and done["outcome"] == "invalid" and "document" not in done
    assert [c["role"] for c in world.role_calls()] == ["letters"]


def test_an_unchanged_document_gets_no_review(world):
    put_payloads(world, "cover_letter", revised=docs()["cover_letter"])
    done, _ = run_rev(world, payload=False)
    assert done["status"] == "error" and done["outcome"] == "unchanged" and [c["role"] for c in world.role_calls()] == ["letters"]


def test_a_writer_that_changed_nothing_is_unchanged_and_gets_no_review(world):
    world.mode({"letters": "noop"})
    done, _ = run_rev(world)
    assert done["status"] == "error" and done["outcome"] == "unchanged" and [c["role"] for c in world.role_calls()] == ["letters"]


@pytest.mark.parametrize("role", ["letters", "review"])
def test_a_token_in_an_output_is_an_error_and_never_in_the_done_file(world, role):
    from tests.test_headless import TOKEN
    world.mode({role: "leak"})
    done, _ = run_rev(world)
    assert done["status"] == "error" and "token" in done["message"] and "document" not in done and TOKEN not in json.dumps(done)


def test_a_cancel_marker_starts_no_call(world):
    (world.exchange / "llm" / "cancel").mkdir(parents=True)
    (world.exchange / "llm" / "cancel" / "rev-17").write_text("", encoding="utf-8")
    done, _ = run_rev(world)
    assert done["status"] == "error" and done["message"] == "cancelled" and world.role_calls() == []


def test_a_revision_that_does_not_fit_before_the_deadline_starts_no_call(world):
    done, _ = run_rev(world, minutes=40 + 14)                                            # 2 x 20 minutes and a margin of 15
    assert done["status"] == "error" and "did not fit" in done["message"] and world.role_calls() == []


@pytest.mark.parametrize("change", [
    lambda r: r["roles"]["review"].update(model="opus"),                                 # an alias
    lambda r: r.update(instruction="x" * 1001),
    lambda r: r.update(roles={"scorer": r["roles"]["letters"], "review": r["roles"]["review"]}),   # the writer of a cv for a letter
    lambda r: r.update(document_kind="diary"),
    lambda r: r.update(kind="daily"),
    lambda r: r.update(run_id="rev-18"),                                                  # not the file name
    lambda r: r.update(content_sha="zz"),
], ids=["alias", "long", "roles", "kind", "request-kind", "run-id", "sha"])
def test_a_bad_revision_request_gets_an_error_done_file_and_no_call(world, change):
    req = rev_request(world)
    change(req)
    (world.exchange / "llm" / "requests" / "rev-17.json").write_text(json.dumps(req), encoding="utf-8")
    world.worker().poll_once()
    done = json.loads((world.exchange / "llm" / "done" / "rev-17.json").read_text(encoding="utf-8"))
    assert done["status"] == "error" and done["message"] and world.role_calls() == []


def test_an_instruction_of_exactly_1000_characters_is_accepted(world):
    done, _ = run_rev(world, instruction="ł" * 1000)
    assert done["status"] == "ok"


def test_a_daily_request_is_taken_before_a_revision_request(world):
    shutil_batch = world.exchange / "batches"
    (shutil_batch / "batch_2026-11-12.json").write_text(json.dumps({"run_id": "2026-11-12", "jobs": []}), encoding="utf-8")
    put_payloads(world, "cover_letter")
    rev_request(world)
    world.request("2026-11-12", batch="batches/batch_2026-11-12.json")
    assert [p.name for p in world.worker().pending_requests()] == ["2026-11-12.json", "rev-17.json"]
    world.worker().poll_once()
    assert [c["role"] for c in world.role_calls()] == ["scorer", "letters", "review", "letters", "review"]


def test_a_late_revision_done_file_is_not_merged_into_a_daily_run(world):
    done_dir = world.exchange / "llm" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    late = done_dir / "rev-17.late.json"
    late.write_text(json.dumps({"run_id": "rev-17", "status": "ok", "attention": [{"job_id": 1, "kind": "cv", "note": "x"}]}), encoding="utf-8")
    done = world.run()
    assert done["status"] == "ok" and done["resumed"] == [] and late.exists() and not late.with_name(late.name + ".merged").exists()


def test_a_revision_work_dir_is_not_a_parked_run(world, tmp_path):
    run_rev(world)
    worker = world.worker()
    assert worker._pending_runs(exclude="x") == []
    cfg = {"paths": {"exchange": str(world.exchange)}}
    assert scheduler._has_parked_run(cfg) is False
    assert (world.exchange / "llm" / "revisions" / "rev-17" / "document_rev-17.json").is_file()


def test_a_revision_cut_off_by_a_restart_gets_an_error_done_file(world):
    rev_request(world)
    claimed = world.exchange / "llm" / "claimed"
    claimed.mkdir(parents=True, exist_ok=True)
    (world.exchange / "llm" / "requests" / "rev-17.json").replace(claimed / "rev-17.json")
    world.worker().recover_interrupted()
    done = json.loads((world.exchange / "llm" / "done" / "rev-17.json").read_text(encoding="utf-8"))
    assert done["status"] == "error" and world.role_calls() == []


def test_a_late_result_after_the_synthetic_timeout_is_written_beside_it(world):
    done_dir = world.exchange / "llm" / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "rev-17.json").write_text(json.dumps({"run_id": "rev-17", "status": "timeout"}), encoding="utf-8")
    run_rev(world)
    assert json.loads((done_dir / "rev-17.json").read_text(encoding="utf-8")) == {"run_id": "rev-17", "status": "timeout"}
    assert json.loads((done_dir / "rev-17.late.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_the_revision_addenda_exist_and_name_the_marker_rule():
    for name in headless.AGENT_FILES.values():
        text = (headless.APP_ROOT / "prompts" / "headless" / "revision" / f"{name}.md").read_text(encoding="utf-8")
        assert "[address removed]" in text and "Never restore it" in text
        assert "may be empty" in text


def test_a_restart_after_a_processed_revision_writes_no_new_done_file(world):
    done, _ = run_rev(world)
    done_dir = world.exchange / "llm" / "done"
    (done_dir / "rev-17.json").replace(done_dir / "rev-17.json.processed")             # what the recruitment side does
    world.worker().recover_interrupted()
    assert not (done_dir / "rev-17.json").exists()
    assert not list((world.exchange / "llm" / "claimed").glob("rev-17.json"))          # the claim was moved aside after the done file
    # even a claim that is still there is not taken for a cut-off request once the result was processed
    (world.exchange / "llm" / "claimed" / "rev-17.json").write_text("{}", encoding="utf-8")
    world.worker().recover_interrupted()
    assert not (done_dir / "rev-17.json").exists()


def test_the_revision_addenda_replace_the_daily_file_rules():
    for name in headless.AGENT_FILES.values():
        text = (headless.APP_ROOT / "prompts" / "headless" / "revision" / f"{name}.md").read_text(encoding="utf-8")
        assert "replace every part of the instructions above that names a batch file" in text and "job description is in job.json" in text
    reviewer = (headless.APP_ROOT / "prompts" / "headless" / "revision" / "doc-reviewer.md").read_text(encoding="utf-8")
    assert "the check is not run again" in reviewer
