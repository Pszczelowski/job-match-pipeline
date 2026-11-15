"""The headless Claude Code worker (app/headless.py, `llm-worker`; goal.md 5.10, v0.78.0). No network, no model, no real `claude`:
tests/fixtures/fake_claude.py plays the CLI. Guard tests G1-G6 are the rule 10 requirement (no ANTHROPIC_* may reach the CLI)."""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cli, headless, pipeline
from app.config import ROOT, load_config
from app.headless import GuardError, Paths, Worker
from app.postpone import limit_seen
from tests.test_batch_results import score
from tests.test_documents import docs

TOKEN = "made-up-oauth-token-for-tests-123"
FAKE = ROOT / "tests" / "fixtures" / "fake_claude.py"
ALLOWED_CHILD_ENV = {"PATH", "HOME", "TZ", "LANG", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL",
                     "DISABLE_AUTOUPDATER", "DISABLE_UPDATES", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}


def posix(path) -> str:
    return str(path).replace("\\", "/")


def payload_file(run_id, entries, version="1.1", top=None):
    return json.dumps({"schema_version": version, "run_id": run_id, "model": "claude-sonnet-5-5", "prompt_version": "v-test",
                       "results": entries, "top": top if top is not None else []})


class World:
    """A folder tree like the llm container's, a fake CLI and a worker that uses it."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.exchange = tmp_path / "exchange"
        (self.exchange / "batches").mkdir(parents=True)
        (self.exchange / "batches" / "batch_run1.json").write_text(json.dumps({"run_id": "run1", "jobs": []}), encoding="utf-8")
        self.secrets = tmp_path / "llm.env"
        self.secrets.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n", encoding="utf-8")
        self.fake = tmp_path / "fake"
        (self.fake / "payload").mkdir(parents=True)
        shutil.copyfile(FAKE, self.fake / "claude.py")
        ids, cv_only = [1, 2], [score(i, 80 - i, documents={"cv": copy.deepcopy(docs()["cv"])}) for i in (1, 2)]
        full = [score(i, 80 - i, documents=docs()) for i in (1, 2)]
        (self.fake / "payload" / "chunk.json").write_text(payload_file("RUNID", [score(i, 80 - i) for i in ids], "1.0", ids), encoding="utf-8")
        (self.fake / "payload" / "docs_cv.json").write_text(payload_file("RUNID", cv_only), encoding="utf-8")
        (self.fake / "payload" / "docs_full.json").write_text(payload_file("RUNID", full), encoding="utf-8")
        (self.fake / "payload" / "review.json").write_text(json.dumps({"attention": [
            {"job_id": 1, "kind": "cv", "note": "x" * 300},
            {"job_id": 99, "kind": "cv", "note": "unknown job id"},
            {"job_id": 2, "kind": "diary", "note": "unknown kind"},
            {"job_id": 2, "kind": "cover_letter", "note": "claim about German could not be settled"}]}), encoding="utf-8")
        self.mode({})
        self.app = tmp_path / "app"                                    # what the image holds: headless/agents, prompts/, profile/
        (self.app / "headless" / "agents").mkdir(parents=True)
        for name in headless.AGENT_FILES.values():
            shutil.copyfile(ROOT / ".claude" / "agents" / f"{name}.md", self.app / "headless" / "agents" / f"{name}.md")
        shutil.copytree(ROOT / "prompts", self.app / "prompts")
        (self.app / "profile").mkdir()
        self.paths = Paths.defaults(self.exchange, self.secrets, self.app, tmp_path / "home")
        self.environ = {"PATH": os.environ["PATH"], "HOME": posix(tmp_path / "home"), "TZ": "Europe/Zurich", "LANG": "C.UTF-8",
                        "CLAUDE_CONFIG_DIR": posix(tmp_path / "home" / ".claude"), "GMAIL_APP_PASSWORD": "mail-made-up"}
        self.command = f'"{posix(sys.executable)}" "{posix(self.fake / "claude.py")}"'

    def mode(self, roles=None, **more):
        (self.fake / "mode.json").write_text(json.dumps({"roles": roles or {}, **more}), encoding="utf-8")

    def worker(self, **kw):
        w = Worker(self.paths, self.command, environ=kw.pop("environ", self.environ), **kw)
        return w

    def calls(self):
        path = self.fake / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def role_calls(self):
        return [c for c in self.calls() if c.get("role")]

    def request(self, run_id="run1", batch="batches/batch_run1.json", **over):
        roles = copy.deepcopy(load_config()["headless"]["roles"])
        req = {"run_id": run_id, "batch_file": batch, "roles": roles, "resume_attempts": 2, **over}
        folder = self.exchange / "llm" / "requests"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{run_id}.json").write_text(json.dumps(req), encoding="utf-8")
        return req

    def run(self, worker=None, run_id="run1", **over):
        self.request(run_id, **over)
        (worker or self.worker()).poll_once()
        return json.loads((self.exchange / "llm" / "done" / f"{run_id}.json").read_text(encoding="utf-8"))

    def results(self):
        folder = self.exchange / "results"
        return sorted(p.name for p in folder.glob("*.json")) if folder.is_dir() else []

    def work(self, run_id="run1"):
        return self.exchange / "llm" / "work" / run_id


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# ---- the chain ----------------------------------------------------------------------------------------------------

def test_success_moves_the_files_into_results_only_after_the_review_and_stores_the_review(world):
    done = world.run()
    assert done["status"] == "ok" and done["run_id"] == "run1"
    assert world.results() == ["results_run1.json", "results_run1.p01.json"]
    calls = {c["role"]: c for c in world.role_calls()}
    assert list(calls) == ["scorer", "letters", "review"]
    for role in ("scorer", "letters", "review"):
        assert "results_run1.json" not in calls[role]["results_seen"]            # the documents file arrives after the review
    assert (world.exchange / "llm" / "reviews" / "run1.md").read_text(encoding="utf-8").startswith("Job 1")
    assert done["review_file"] == "llm/reviews/run1.md" and done["review_summary"].startswith("Job 1: no change")
    assert set(done["roles"]) == {"scorer", "letters", "review"} and done["roles"]["scorer"]["models"] == ["claude-sonnet-5-5"]
    assert done["roles"]["review"]["models"] == ["claude-opus-5-5"]
    assert not list((world.exchange / "llm" / "requests").glob("*.json"))              # claimed
    assert (world.exchange / "llm" / "claimed" / "run1.json").exists()


def test_the_reviewers_attention_list_is_cleaned(world):
    done = world.run()
    assert done["attention"] == [{"job_id": 1, "kind": "cv", "note": "x" * headless.NOTE_MAX},
                                 {"job_id": 2, "kind": "cover_letter", "note": "claim about German could not be settled"}]


def test_usage_limit_in_the_scorer_moves_the_finished_chunk_and_stops(world):
    world.mode({"scorer": "limit"}, chunk_before_limit=True)
    done = world.run()
    assert done["status"] == "usage_limit" and limit_seen(done["limit_message"]) and len(done["limit_message"]) <= 300
    assert world.results() == ["results_run1.p01.json"]
    assert [c["role"] for c in world.role_calls()] == ["scorer"]                       # no letters, no review


def test_auth_error_stops_the_chain_with_status_auth(world):
    world.mode({"scorer": "auth"})
    done = world.run()
    assert done["status"] == "auth" and world.results() == [] and [c["role"] for c in world.role_calls()] == ["scorer"]


def test_a_hanging_cli_is_killed_at_the_timeout(world):
    world.mode({"scorer": "hang"})
    worker = world.worker()
    worker.minute = 1.0                                                                  # timeout_minutes 150 -> 150 s is too long
    req = world.request(roles={**copy.deepcopy(load_config()["headless"]["roles"])})
    req["roles"]["scorer"]["timeout_minutes"] = 1
    (world.exchange / "llm" / "requests" / "run1.json").write_text(json.dumps(req), encoding="utf-8")
    started = time.monotonic()
    worker.poll_once()
    done = json.loads((world.exchange / "llm" / "done" / "run1.json").read_text(encoding="utf-8"))
    assert done["status"] == "timeout" and time.monotonic() - started < 60 and world.results() == []


@pytest.mark.parametrize("role", ["letters", "review"])
def test_a_token_in_an_output_file_quarantines_the_documents_and_keeps_the_clean_chunk(world, role):
    world.mode({role: "leak"})
    done = world.run()
    assert done["status"] == "error" and "token" in done["message"]
    assert world.results() == ["results_run1.p01.json"]
    assert (world.work() / "quarantine" / "results_run1.json").exists()
    assert TOKEN not in json.dumps(done)


def test_a_chunk_with_the_token_is_never_moved(world):
    chunk = world.fake / "payload" / "chunk.json"
    chunk.write_text(chunk.read_text(encoding="utf-8").replace("Job 1 summary", f"Job 1 summary {TOKEN}"), encoding="utf-8")
    done = world.run()
    assert done["status"] == "error" and "results_run1.p01.json" not in world.results()
    assert (world.work() / "quarantine" / "results_run1.p01.json").exists()


def test_unfinished_letters_keep_the_documents_file_and_the_chunks_still_arrive(world):
    world.mode({"letters": "noop"})
    done = world.run()
    assert done["status"] == "error" and "unfinished" in done["message"]
    assert world.results() == ["results_run1.p01.json"]
    assert (world.work() / "results_run1.json").exists()
    assert json.loads((world.work() / "state.json").read_text(encoding="utf-8"))["stage"] == "letters"
    assert "review" not in [c["role"] for c in world.role_calls()]


def test_the_next_request_resumes_unfinished_documents_and_publishes_them(world):
    world.mode({"letters": "noop"})
    world.run()
    world.mode({})
    (world.exchange / "batches" / "batch_run2.json").write_text("{}", encoding="utf-8")
    done = world.run(run_id="run2", batch="batches/batch_run2.json")
    assert [r["status"] for r in done["resumed"]] == ["ok"] and done["resumed"][0]["run_id"] == "run1"
    assert "results_run1.json" in world.results()
    assert json.loads((world.work() / "state.json").read_text(encoding="utf-8"))["stage"] == "done"


def test_unfinished_documents_are_retried_twice_then_marked_failed(world):
    world.mode({"letters": "noop"})
    world.run()                                                                       # the first try
    resumed = []
    for n in (2, 3, 4):                                                               # requests without a batch only resume
        resumed.append(world.run(run_id=f"run{n}", batch=None)["resumed"])
    assert [[r["status"] for r in x] for x in resumed] == [["error"], ["error"], []]
    assert "given up after 2 retries" in resumed[1][0]["message"]
    letters = [c for c in world.role_calls() if c["role"] == "letters"]
    assert len(letters) == 3                                                          # first try plus resume_attempts retries
    assert json.loads((world.work("run1") / "state.json").read_text(encoding="utf-8"))["stage"] == "failed"
    assert "results_run1.json" not in world.results()


def test_a_missing_review_report_parks_the_run_in_the_review_stage(world):
    world.mode({"review": "no_report"})
    done = world.run()
    assert done["status"] == "error" and "results_run1.json" not in world.results()
    assert json.loads((world.work() / "state.json").read_text(encoding="utf-8"))["stage"] == "review"


def test_max_turns_in_the_scorer_gives_status_partial_when_chunks_exist(world):
    world.mode({"scorer": "max_turns"})
    done = world.run()
    assert done["status"] in ("partial", "error") and done["roles"]["scorer"]["status"] == "partial"


def test_a_cancel_marker_stops_the_chain_before_the_letters(world):
    cancel = world.exchange / "llm" / "cancel"
    cancel.mkdir(parents=True)
    worker = world.worker()
    original = worker._role

    def role(name, *args, **more):                                                     # the marker appears while the scorer runs
        out = original(name, *args, **more)
        (cancel / "run1").write_text("cancel", encoding="ascii")
        return out
    worker._role = role
    done = world.run(worker=worker)
    assert done["status"] == "error" and done["message"] == "cancelled"
    assert [c["role"] for c in world.role_calls()] == ["scorer"]


def test_no_batch_means_no_cli_call_for_the_roles(world):
    done = world.run(batch=None)
    assert done["status"] == "ok" and world.role_calls() == []


def test_a_batch_file_outside_the_batches_folder_is_refused(world):
    done = world.run(batch="../llm.env")
    assert done["status"] == "error" and world.role_calls() == []


@pytest.mark.parametrize("change", [lambda r: r["roles"]["scorer"].update(model="sonnet"), lambda r: r["roles"]["review"].update(model="opus"),
                                    lambda r: r["roles"]["letters"].update(effort="turbo"), lambda r: r["roles"].pop("review"),
                                    lambda r: r["roles"]["scorer"].update(max_turns=0), lambda r: r.update(run_id="../x")])
def test_a_request_with_an_alias_or_a_bad_value_is_refused(world, change):
    req = world.request()
    change(req)
    assert headless.validate_request(req) is not None
    folder = world.exchange / "llm" / "requests"
    (folder / "run1.json").write_text(json.dumps(req), encoding="utf-8")
    world.worker().poll_once()
    assert world.role_calls() == [] and world.results() == []


def test_a_valid_request_passes_validation(world):
    assert headless.validate_request(world.request()) is None


def test_the_token_is_read_again_for_each_request(world):
    world.run()
    world.secrets.write_text("CLAUDE_CODE_OAUTH_TOKEN=second-made-up-token-456\n", encoding="utf-8")
    (world.exchange / "batches" / "batch_run2.json").write_text("{}", encoding="utf-8")
    world.run(run_id="run2", batch="batches/batch_run2.json")
    assert {c["token_seen"] for c in world.role_calls() if "run2" in c["cwd"]} == {"second-made-up-token-456"}


def test_a_missing_token_gives_status_auth_without_a_cli_call(world):
    world.secrets.write_text("# nothing\n", encoding="utf-8")
    done = world.run()
    assert done["status"] == "auth" and world.calls() == []


def test_the_module_does_not_import_the_database():
    code = "import sys, app.headless; sys.exit(1 if 'app.db' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0


# ---- G1-G7: no API key may reach the CLI ------------------------------------------------------------------------------

def llm_args(world, *extra):
    return ["llm-worker", "--once", "--exchange", str(world.exchange), "--secrets", str(world.secrets), "--home", str(world.root / "home"),
            "--app", str(world.app), "--command", world.command, *extra]


def test_worker_refuses_to_start_with_anthropic_api_key(world, monkeypatch, capsys):          # G1
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-made-up")
    world.request()
    assert cli.main(llm_args(world)) == 3
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err and "sk-ant-api-made-up" not in err                       # the name, never the value
    assert world.calls() == [] and (world.exchange / "llm" / "requests" / "run1.json").exists()   # nothing claimed, the CLI never ran


@pytest.mark.parametrize("name", ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID",
                                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SIMPLE"])
def test_worker_refuses_every_other_way_to_pay_per_token(world, monkeypatch, name):            # G2
    monkeypatch.setenv(name, "1")
    world.request()
    assert cli.main(llm_args(world)) == 3
    assert world.calls() == [] and (world.exchange / "llm" / "requests" / "run1.json").exists()


@pytest.mark.parametrize("line", ["ANTHROPIC_API_KEY=sk-ant-api-made-up", "ANTHROPIC_AUTH_TOKEN=x", "CLAUDE_CODE_USE_BEDROCK=1"])
def test_worker_refuses_a_token_file_with_a_forbidden_line(world, line):                        # G2
    world.secrets.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n{line}\n", encoding="utf-8")
    world.request()
    assert cli.main(llm_args(world)) == 3
    assert world.calls() == [] and (world.exchange / "llm" / "requests" / "run1.json").exists()
    with pytest.raises(GuardError) as exc:
        headless.read_secret_file(world.secrets)
    assert "made-up" not in str(exc.value) and TOKEN not in str(exc.value)


def test_a_forbidden_line_added_to_the_token_file_later_stops_the_next_request(world):          # G2, re-read for each request
    world.secrets.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\nANTHROPIC_API_KEY=sk-ant-x\n", encoding="utf-8")
    done = world.run()
    assert done["status"] == "error" and "ANTHROPIC_API_KEY" in done["message"] and world.calls() == []


def test_api_key_never_reaches_the_cli(world):                                                  # G3
    environ = {**world.environ, "ANTHROPIC_API_KEY": "sk-ant-api-made-up", "ANTHROPIC_AUTH_TOKEN": "bearer-made-up",
               "CLAUDE_CODE_USE_BEDROCK": "1"}
    done = world.run(worker=world.worker(environ=environ))      # run_request does not run the start check: the child env alone must hold
    assert done["status"] == "ok"                               # the fake exits 97 when any ANTHROPIC_* or GMAIL* name is present
    names = [set(c["env"]) for c in world.calls()]
    assert names and all(n <= ALLOWED_CHILD_ENV for n in names)
    role_names = [set(c["env"]) for c in world.role_calls()]
    assert all({"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL", "DISABLE_AUTOUPDATER", "DISABLE_UPDATES",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "PATH"} <= n for n in role_names)


def test_the_child_environment_is_built_from_the_allowlist():                                    # G3
    env = headless.build_child_env({"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "k", "GMAIL_APP_PASSWORD": "p", "FOO": "1"}, "tok", "low")
    assert set(env) == {"PATH", "HOME", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL", "DISABLE_AUTOUPDATER", "DISABLE_UPDATES",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "low" and env["DISABLE_UPDATES"] == "1"


def test_the_effort_level_of_each_role_reaches_the_cli_environment(world):
    world.run()
    efforts = {c["role"]: c["effort_env"] for c in world.role_calls()}
    assert efforts == {"scorer": "medium", "letters": "medium", "review": "low"}


def test_preflight_refuses_an_api_key_source(world):                                             # G4
    world.mode({}, auth_status={"loggedIn": True, "authMethod": "oauth_token", "apiKeySource": "ANTHROPIC_API_KEY"})
    done = world.run()
    assert done["status"] == "auth" and "API key" in done["message"] and world.role_calls() == []


def test_preflight_refuses_another_login_method(world):                                          # G4
    world.mode({}, auth_status={"loggedIn": True, "authMethod": "claude.ai"})
    done = world.run()
    assert done["status"] == "auth" and world.role_calls() == []


def test_preflight_accepts_the_plan_token_only(world):                                           # G4
    ok, _ = headless.preflight(world.command, headless.build_child_env(world.environ, TOKEN, "low"))
    assert ok


def role_settings(role):
    return load_config()["headless"]["roles"][role]


@pytest.mark.parametrize("role, agent", [("scorer", "scorer-cv"), ("letters", "letter-writer"), ("review", "doc-reviewer")])
def test_argv_of_each_role(world, role, agent):                                                  # G5
    argv = headless.build_argv("claude", role, role_settings(role), "brief", world.root, "a.json", "s.json")
    flat = " ".join(argv)
    assert argv[0] == "claude" and argv[argv.index("-p") + 1] == "brief"
    for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in argv
    assert argv[argv.index("--agent") + 1] == agent and argv[argv.index("--agents") + 1] == "a.json"
    assert argv[argv.index("--model") + 1] == role_settings(role)["model"] and argv[argv.index("--effort") + 1] == role_settings(role)["effort"]
    assert argv[argv.index("--max-turns") + 1] == str(role_settings(role)["max_turns"])
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk" and argv[argv.index("--permission-prompts") + 1] == "none"
    assert "Bash" not in argv[argv.index("--tools") + 1].split(",")
    assert {"Bash", "WebFetch", "WebSearch", "Agent", "mcp__*"} <= set(argv[argv.index("--disallowedTools") + 1].split(","))
    for banned in headless.FORBIDDEN_ARGV:
        assert banned not in argv and banned not in flat


def test_every_real_call_uses_the_pinned_ids_and_never_a_forbidden_option(world):                # G5
    world.run()
    roles = load_config()["headless"]["roles"]
    for call in world.role_calls():
        argv = call["argv"]
        assert not set(headless.FORBIDDEN_ARGV) & set(argv)
        assert argv[argv.index("--model") + 1] == {"scorer": roles["scorer"]["model"], "letters": roles["letters"]["model"],
                                                   "review": roles["review"]["model"]}[call["role"]]
        assert "--restricted" in argv and "--agents" in argv


def test_build_argv_refuses_a_forbidden_option_in_the_command():                                 # G5
    with pytest.raises(GuardError):
        headless.build_argv("claude --bare", "scorer", role_settings("scorer"), "b", ".", "a", "s")


def test_the_settings_file_has_no_env_and_no_api_key_helper_and_allows_only_the_work_dir(tmp_path):   # G6
    settings = headless.build_settings(tmp_path / "work" / "run1")
    assert "env" not in settings and "apiKeyHelper" not in settings and "helper" not in json.dumps(settings).lower()
    assert len(settings["permissions"]["allow"]) == 1
    assert settings["permissions"]["allow"][0].startswith("Edit(//") and settings["permissions"]["allow"][0].endswith("run1/**)")
    assert "Read(//app/llm.env)" in settings["permissions"]["deny"] and "Edit(//app/profile/**)" in settings["permissions"]["deny"]


def test_the_agents_file_has_no_bash_and_takes_model_and_effort_from_the_request(world):
    agents = headless.build_agents_json("review", {"model": "claude-opus-5-5", "effort": "low"}, world.paths.agents_dir, world.paths.addendum_dir)
    (name, spec), = agents.items()
    assert name == "doc-reviewer" and spec["model"] == "claude-opus-5-5" and spec["effort"] == "low"
    assert "Bash" not in spec["tools"] and "Write" in spec["tools"]
    assert "Headless run" in spec["prompt"] and "You review and correct the CV drafts" in spec["prompt"] and "name: doc-reviewer" not in spec["prompt"]


def test_every_role_has_a_tracked_addendum():
    for name in headless.AGENT_FILES.values():
        assert (ROOT / "prompts" / "headless" / f"{name}.md").is_file() and (ROOT / ".claude" / "agents" / f"{name}.md").is_file()


def test_leak_scan_finds_the_token_and_the_key_prefix(tmp_path):
    clean, token, key = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "c.json"
    clean.write_text("nothing here", encoding="utf-8")
    token.write_text(f"x {TOKEN} y", encoding="utf-8")
    key.write_text("sk-ant-abc", encoding="utf-8")
    assert headless.leak_scan([clean, token, key], TOKEN) == ["b.json", "c.json"]


# ---- the command --------------------------------------------------------------------------------------------------

def test_llm_worker_is_dispatched_before_the_config_and_the_env_are_loaded(world, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("llm-worker must not load the env, the config or the logging")
    monkeypatch.setattr(cli, "load_env", boom)
    monkeypatch.setattr(cli, "load_config", boom)
    monkeypatch.setattr(cli, "setup_logging", boom)
    world.request()
    assert cli.main(llm_args(world)) == 0
    assert (world.exchange / "llm" / "done" / "run1.json").exists() and world.results() == ["results_run1.json", "results_run1.p01.json"]


def test_llm_worker_health_needs_a_fresh_heartbeat(world, capsys):
    assert cli.main(llm_args(world, "--health")) == 1
    cli.main(llm_args(world))
    assert cli.main(llm_args(world, "--health")) == 0


def test_check_cli_expects_the_not_logged_in_answer(world):
    world.mode({"scorer": "auth"})
    ok, text = headless.check_cli(world.command, world.paths, environ=world.environ)
    assert ok and "not logged in" in text
    world.mode({"scorer": "ok"})
    ok, _ = headless.check_cli(world.command, world.paths, environ=world.environ)
    assert not ok
    assert all("CLAUDE_CODE_OAUTH_TOKEN" not in c["env"] for c in world.role_calls())              # no token in the check


def test_worker_log_file_has_no_token(world, monkeypatch):
    """v0.78.2: exchange/llm/llm-worker.log holds the role events with a UTC stamp and the run id; the token the CLI printed on
    stderr is in neither the log nor the done file."""
    for name in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_SIMPLE"):                          # the start guard reads the real environment
        monkeypatch.delenv(name, raising=False)
    world.mode({"scorer": "stderr_crash"})
    world.request()
    assert cli.main(llm_args(world, "--once")) == 0
    log_file = world.exchange / "llm" / "llm-worker.log"
    text = log_file.read_text(encoding="utf-8")
    assert "run=run1" in text and "event=role_start" in text and "event=role_end" in text
    assert re.search(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO run=run1 event=", text, re.M)
    assert TOKEN not in text and "sk-ant-" not in text
    assert TOKEN not in (world.exchange / "llm" / "done" / "run1.json").read_text(encoding="utf-8")


def test_redact_covers_the_token_in_every_listed_form():
    from urllib.parse import quote
    odd = "tok/en+with=odd chars 99"
    text = " | ".join([f"plain {TOKEN}", "key sk-ant-oat01-abcdef", "Authorization: Bearer abc.def-123456",
                       "Bearer abcdefgh12345", "CLAUDE_CODE_OAUTH_TOKEN=whatever", "run " + "a1" * 30, "kept: basic German, run 2026-11-07T060403Z"])
    out = headless.redact(text, TOKEN)
    assert out.count("[redacted]") == 6 and TOKEN not in out and "sk-ant" not in out and "abcdef" not in out
    assert headless.redact(f"x {odd} y {quote(odd, safe='')}", odd) == "x [redacted] y [redacted]" and headless.redact("kept: basic German, run 2026-11-07T060403Z") == \
        "kept: basic German, run 2026-11-07T060403Z"
    assert headless.redact_obj({"a": [f"t {TOKEN}", 3], "b": {"c": TOKEN}}, TOKEN) == {"a": ["t [redacted]", 3], "b": {"c": "[redacted]"}}


# ---- v0.78.2: one bad entry must not lose the documents of the others -----------------------------------------------

def docs_payload(world, name, entries):
    (world.fake / "payload" / name).write_text(payload_file("RUNID", entries), encoding="utf-8")


def test_one_bad_documents_entry_is_dropped_and_the_rest_get_letters_and_review(world):
    bad = score(2, 78, documents={"cv": copy.deepcopy(docs()["cv"])})
    del bad["documents"]["cv"]["summary"]
    docs_payload(world, "docs_cv_one_bad.json", [score(1, 79, documents={"cv": copy.deepcopy(docs()["cv"])}), bad])
    docs_payload(world, "docs_full_job1.json", [score(1, 79, documents=docs())])
    world.mode({}, scorer_docs="docs_cv_one_bad.json", letters_docs="docs_full_job1.json")
    done = world.run()
    assert done["status"] == "partial"
    assert [c["role"] for c in world.role_calls()] == ["scorer", "letters", "review"]
    assert len(done["dropped"]) == 1 and done["dropped"][0]["job_id"] == 2 and done["dropped"][0]["stage"] == "scorer"
    assert done["dropped"][0]["path"].startswith("results/1/documents/cv")
    published = json.loads((world.exchange / "results" / "results_run1.json").read_text(encoding="utf-8"))
    assert [e["job_id"] for e in published["results"]] == [1] and set(published["results"][0]["documents"]) == {"cv", "cover_letter"}
    assert (world.work() / "quarantine" / "results_run1.json.dropped.json").is_file()


def test_a_file_without_documents_skips_letters_and_review(world):
    docs_payload(world, "docs_none.json", [score(1, 79), score(2, 78)])
    world.mode({}, scorer_docs="docs_none.json")
    done = world.run()
    assert done["status"] == "ok" and done["message"] == "no documents to write"
    assert [c["role"] for c in world.role_calls()] == ["scorer"]
    assert world.results() == ["results_run1.p01.json"]


def test_the_cover_letters_the_scorer_wrote_are_stripped(world):
    world.mode({"letters": "noop"}, scorer_docs="docs_full.json")                      # the scorer ignores "write the CV only"
    done = world.run()
    assert done["status"] == "error" and "unfinished" in done["message"]


def test_salvage_drops_duplicate_foreign_and_unknown_variant_entries_and_keeps_the_rest(tmp_path):
    path = tmp_path / "results_r.json"
    entries = [score(1, 80), score(1, 70), score(9, 60), score(2, 75, cv_variant="made_up"), score(3, 50)]
    path.write_text(payload_file("r", entries, "1.0"), encoding="utf-8")
    out = headless.salvage_file(path, stage="chunk", batch_ids={1, 2, 3}, variants={"resume"})
    assert out.ok and out.kept == 2
    assert [(d["job_id"], d["path"]) for d in out.dropped] == [
        (1, "results/1/job_id"), (9, "results/2/job_id"), (2, "results/3/cv_variant")]
    assert [e["job_id"] for e in json.loads(path.read_text(encoding="utf-8"))["results"]] == [1, 3]
    path.write_text(payload_file("r", entries, "1.0"), encoding="utf-8")
    assert headless.salvage_file(path, stage="chunk", batch_ids=set(), variants=set()).kept == 4          # empty batch or profile: skipped checks


def test_salvage_leaves_a_file_level_problem_to_the_caller(tmp_path):
    path = tmp_path / "results_r.json"
    path.write_text(payload_file("r", [score(1, 80)], "9.9"), encoding="utf-8")
    out = headless.salvage_file(path, stage="chunk")
    assert not out.ok and "schema_version" in out.problem
    path.write_text("not json", encoding="utf-8")
    assert not headless.salvage_file(path, stage="chunk").ok


# ---- v0.78.2: a failed role says why ---------------------------------------------------------------------------------

def test_failed_role_reports_reason_and_stderr_tail_without_the_token(world):
    world.mode({"scorer": "stderr_crash"})
    done = world.run()
    role = done["roles"]["scorer"]
    assert role["status"] == "error" and role["rc"] == 2 and "exit code 2" in role["reason"]
    assert "boom: upstream refused the call" in role["stderr_tail"] and role["started"].endswith("Z") and role["ended"].endswith("Z")
    assert role["reason"] in done["message"]                                           # the alert text carries the reason
    assert TOKEN not in json.dumps(done) and "[redacted]" in role["stderr_tail"]


def _cli_result(text, **more):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text, **more})


@pytest.mark.parametrize("text", ["The posting says: you are not logged in to see the salary.",
                                  "The tool said you've hit your session limit · resets 3pm (Europe/Zurich)"])
def test_login_or_limit_words_in_a_good_result_do_not_make_it_a_failure(text):
    out = headless.classify("scorer", 0, _cli_result(text), timed_out=False, duration=1.0)
    assert out.status == "ok"


def test_a_failed_cli_result_with_a_limit_or_login_sentence_keeps_its_status():
    limit = _cli_result("You've hit your session limit · resets 3pm (Europe/Zurich)", is_error=True)
    assert headless.classify("scorer", 1, limit, timed_out=False, duration=1.0).status == "usage_limit"
    login = _cli_result("Failed to authenticate. API Error: 401", is_error=True)
    assert headless.classify("scorer", 1, login, timed_out=False, duration=1.0).status == "auth"


def test_the_stderr_tail_is_cut_to_2000_characters_before_it_is_stored():
    out = headless.classify("scorer", 1, "", timed_out=False, duration=1.0, stderr="x1 " * 2000)
    assert len(out.stderr_tail) == headless.STDERR_TAIL_MAX == 2000 and out.reason == "exit code 1"


def test_a_cli_that_cannot_be_started_is_an_error_outcome_not_an_exception(tmp_path):
    out = headless.run_role([str(tmp_path / "no-such-binary")], {}, tmp_path, 5, "scorer")
    assert out.status == "error" and out.rc is None and "could not start" in out.reason


def test_a_crash_inside_a_request_still_writes_a_done_file(world, monkeypatch):
    worker = world.worker()
    monkeypatch.setattr(worker, "run_request", lambda req: (_ for _ in ()).throw(RuntimeError(f"boom {TOKEN}")))
    world.request()
    worker.poll_once()                                                                 # does not raise
    done = json.loads((world.exchange / "llm" / "done" / "run1.json").read_text(encoding="utf-8"))
    assert done["status"] == "error" and "RuntimeError" in done["message"] and TOKEN not in json.dumps(done)


# ---- v0.78.2: the statuses agree ---------------------------------------------------------------------------------------

def test_scorer_error_with_chunks_is_partial(world):
    world.mode({"scorer": "error_after_chunk"})
    done = world.run()
    assert done["status"] == "partial" and done["roles"]["scorer"]["status"] == "error"
    assert world.results() == ["results_run1.p01.json"]                                # the chunk arrived; nothing else was written


def test_a_scorer_error_without_chunks_is_an_error_with_its_reason(world):
    world.mode({"scorer": "max_turns"})
    done = world.run()
    assert done["status"] == "error" and "error_max_turns" in done["message"]


def test_a_bad_chunk_is_not_hidden_by_a_good_publish(world):
    (world.fake / "payload" / "chunk.json").write_text(payload_file("RUNID", [score(1, 79), score(2, 78)], "9.9", [1, 2]), encoding="utf-8")
    done = world.run()
    assert done["status"] == "error" and "results_run1.p01.json" in done["message"]
    assert (world.work() / "quarantine" / "results_run1.p01.json").exists() and world.results() == ["results_run1.json"]


# ---- v0.78.2: the line-endings check runs after the letters -----------------------------------------------------------

def test_the_review_brief_lists_line_endings_after_letters(world):
    from app.pdf import line_endings_report
    done = world.run()
    assert done["status"] == "ok"
    briefs = {c["role"]: c["argv"][c["argv"].index("-p") + 1] for c in world.role_calls()}
    assert "Line-ending" not in briefs["letters"] and "Line-ending" not in briefs["scorer"]      # the letters did not exist yet
    published = world.exchange / "results" / "results_run1.json"
    assert "Line-ending problems found in the documents (letters included):\n" + line_endings_report(published) in briefs["review"]


def test_the_done_file_names_its_times_and_the_files_it_moved(world):
    done = world.run()
    assert done["started"].endswith("Z") and done["ended"] >= done["started"]
    assert done["files"] == ["results_run1.p01.json", "results_run1.json"] and done["roles"]["scorer"]["started"].endswith("Z")


# ---- v0.78.6: memory, restart and cancel marker ------------------------------------------------------------------------

def line_endings_calls(monkeypatch):
    seen = []
    real = subprocess.run

    def spy(argv, *a, **kw):
        if headless.LINE_ENDINGS_CODE in list(argv):
            seen.append(kw)
        return real(argv, *a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    return seen


def review_brief(world):
    call = [c for c in world.role_calls() if c["role"] == "review"][0]
    return call["argv"][call["argv"].index("-p") + 1]


def test_the_line_endings_child_gets_no_token_and_a_minimal_environment(world, monkeypatch):
    seen = line_endings_calls(monkeypatch)
    assert world.run()["status"] == "ok" and len(seen) == 1
    env = seen[0]["env"]
    assert set(env) <= {"PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING", "SYSTEMROOT", "HOME"}
    assert "PATH" in env and TOKEN not in json.dumps(env) and seen[0]["timeout"] == headless.LINE_ENDINGS_TIMEOUT_S
    assert "(letters included):\nnot available" not in review_brief(world)


def test_a_failing_line_endings_child_gives_not_available_and_the_review_still_runs(world, monkeypatch):
    monkeypatch.setattr(headless, "LINE_ENDINGS_CODE", "import sys; sys.exit(3)")
    lines = []
    done = world.run(worker=world.worker(log=lines.append))
    assert done["status"] == "ok" and "(letters included):\nnot available" in review_brief(world)
    assert any("line_endings_unavailable" in x for x in lines)


def test_a_line_endings_child_that_hangs_is_stopped_at_the_time_limit(world, monkeypatch):
    monkeypatch.setattr(headless, "LINE_ENDINGS_CODE", "import time; time.sleep(60)")
    monkeypatch.setattr(headless, "LINE_ENDINGS_TIMEOUT_S", 0.5)
    lines = []
    started = time.monotonic()
    done = world.run(worker=world.worker(log=lines.append))
    assert done["status"] == "ok" and time.monotonic() - started < 30
    assert "(letters included):\nnot available" in review_brief(world) and any("reason=timeout" in x for x in lines)


def test_serve_logs_worker_start(world):
    import threading
    lines = []
    assert world.worker(log=lines.append).serve(threading.Event(), once=True) == 0
    assert any("event=worker_start" in x for x in lines)


def test_the_heartbeat_is_touched_during_a_long_request(world, monkeypatch):
    import threading
    monkeypatch.setattr(headless, "HEARTBEAT_SECONDS", 0.05)
    worker = world.worker()
    seen = []

    def slow():
        os.utime(worker.p.heartbeat, (1_000_000, 1_000_000))
        time.sleep(0.6)
        seen.append(worker.p.heartbeat.stat().st_mtime)
        return 0
    monkeypatch.setattr(worker, "poll_once", slow)
    worker.serve(threading.Event(), once=True)
    assert seen and seen[0] > 1_000_000


def park_review(world, run_id="run1"):
    world.mode({"review": "no_report"})
    assert world.run(run_id=run_id)["status"] == "error"
    world.mode({})
    (world.exchange / "llm" / "done" / f"{run_id}.json").unlink()


def test_recovery_resumes_a_run_cut_off_in_the_review_stage(world):
    park_review(world)
    world.worker().recover_interrupted()
    done = json.loads((world.exchange / "llm" / "done" / "run1.json").read_text(encoding="utf-8"))
    assert done["status"] == "ok" and done["run_id"] == "run1" and done["resumed"] == []
    assert "results_run1.json" in world.results()
    assert json.loads((world.work() / "state.json").read_text(encoding="utf-8"))["stage"] == "done"


def test_recovery_gives_a_run_cut_off_in_the_scorer_stage_an_error_done_file(world):
    world.request("run9")
    claimed = world.exchange / "llm" / "claimed"
    claimed.mkdir(parents=True, exist_ok=True)
    os.replace(world.exchange / "llm" / "requests" / "run9.json", claimed / "run9.json")
    work = world.work("run9")
    work.mkdir(parents=True)
    (work / "state.json").write_text(json.dumps({"run_id": "run9", "stage": "scorer", "attempts": 0, "roles": {}}), encoding="utf-8")
    world.worker().recover_interrupted()
    done = json.loads((world.exchange / "llm" / "done" / "run9.json").read_text(encoding="utf-8"))
    assert done["status"] == "error" and "restarted" in done["message"] and world.role_calls() == []


def cut_off_in_scorer_stage(world, chunk_text):
    world.request("run9")
    claimed = world.exchange / "llm" / "claimed"
    claimed.mkdir(parents=True, exist_ok=True)
    os.replace(world.exchange / "llm" / "requests" / "run9.json", claimed / "run9.json")
    work = world.work("run9")
    work.mkdir(parents=True)
    (work / "state.json").write_text(json.dumps({"run_id": "run9", "stage": "scorer", "attempts": 0, "roles": {}}), encoding="utf-8")
    (work / "results_run9.p01.json").write_text(chunk_text, encoding="utf-8")
    world.worker().recover_interrupted()
    return json.loads((world.exchange / "llm" / "done" / "run9.json").read_text(encoding="utf-8"))


def test_recovery_moves_scorer_chunks_of_a_run_cut_off_in_the_scorer_stage(world):
    chunk = (world.fake / "payload" / "chunk.json").read_text(encoding="utf-8").replace("RUNID", "run9")
    done = cut_off_in_scorer_stage(world, chunk)
    assert done["status"] == "error" and "restarted" in done["message"]
    assert world.results() == ["results_run9.p01.json"] and done["files"] == ["results_run9.p01.json"]
    assert not (world.work("run9") / "results_run9.p01.json").exists()


def test_recovery_quarantines_a_scorer_chunk_with_the_token(world):
    chunk = (world.fake / "payload" / "chunk.json").read_text(encoding="utf-8").replace("RUNID", "run9").replace("Job 1 summary", f"Job 1 summary {TOKEN}")
    done = cut_off_in_scorer_stage(world, chunk)
    assert done["status"] == "error" and world.results() == []
    assert (world.work("run9") / "quarantine" / "results_run9.p01.json").exists() and TOKEN not in json.dumps(done)


def test_recovery_resumes_a_run_cut_off_in_the_publish_stage(world, monkeypatch):
    real = headless.Worker._publish
    calls = []

    def boom(self, *a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk went away")
        return real(self, *a, **kw)
    monkeypatch.setattr(headless.Worker, "_publish", boom)
    assert world.run()["status"] == "error"
    assert json.loads((world.work() / "state.json").read_text(encoding="utf-8"))["stage"] == "publish"
    (world.exchange / "llm" / "done" / "run1.json").unlink()
    roles_before = len(world.role_calls())
    world.worker().recover_interrupted()
    done = json.loads((world.exchange / "llm" / "done" / "run1.json").read_text(encoding="utf-8"))
    assert done["status"] == "ok" and "results_run1.json" in world.results() and len(world.role_calls()) == roles_before
    assert json.loads((world.work() / "state.json").read_text(encoding="utf-8"))["stage"] == "done"


def test_pending_publish_run_is_published_by_the_next_request(world, monkeypatch):
    real = headless.Worker._publish
    calls = []

    def boom(self, *a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk went away")
        return real(self, *a, **kw)
    monkeypatch.setattr(headless.Worker, "_publish", boom)
    assert world.run()["status"] == "error"
    assert json.loads((world.work() / "state.json").read_text(encoding="utf-8"))["stage"] == "publish"
    (world.exchange / "batches" / "batch_run2.json").write_text("{}", encoding="utf-8")
    done = world.run(run_id="run2", batch="batches/batch_run2.json")
    assert [r["status"] for r in done["resumed"]] == ["ok"] and done["resumed"][0]["run_id"] == "run1"
    assert "results_run1.json" in world.results()


def test_a_crash_between_the_stage_mark_and_the_replace_stays_resumable(world, monkeypatch):
    real_replace = os.replace

    def crash(src, dst):
        if Path(dst).parent.name == "results":
            raise OSError("crash at the replace")
        return real_replace(src, dst)
    monkeypatch.setattr(headless.os, "replace", crash)
    world.run()
    monkeypatch.undo()
    assert json.loads((world.work() / "state.json").read_text(encoding="utf-8"))["stage"] != "done"


def test_a_scorer_that_exits_ok_and_writes_nothing_is_an_error(world, monkeypatch):
    monkeypatch.setattr(headless, "run_role", lambda argv, env, cwd, timeout_s, role, token="": headless.RoleOutcome(role))
    done = world.run()
    assert done["status"] == "error" and done["message"].startswith("the scorer wrote no result") and world.results() == []


def test_the_worker_does_not_overwrite_the_schedulers_timeout_done_file(world):
    done_dir = world.exchange / "llm" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "run1.json").write_text(json.dumps({"run_id": "run1", "status": "timeout", "message": "the llm service did not answer in time"}), encoding="utf-8")
    world.worker()._write_done("run1", {"status": "ok", "roles": {}, "started": "x", "ended": "y"})
    assert json.loads((done_dir / "run1.json").read_text(encoding="utf-8"))["status"] == "timeout"
    assert json.loads((done_dir / "run1.late.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_recovery_leaves_a_run_with_a_done_file_alone(world):
    world.run()
    path = world.exchange / "llm" / "done" / "run1.json"
    before, calls = path.read_text(encoding="utf-8"), len(world.calls())
    world.worker().recover_interrupted()
    assert path.read_text(encoding="utf-8") == before and len(world.calls()) == calls


def test_recovery_skips_a_cancelled_run(world):
    park_review(world)
    cancel = world.exchange / "llm" / "cancel"
    cancel.mkdir(parents=True)
    (cancel / "run1").write_text("cancel", encoding="ascii")
    world.worker().recover_interrupted()
    assert not (world.exchange / "llm" / "done" / "run1.json").exists() and (cancel / "run1").exists()


def test_a_failure_in_recovery_does_not_stop_serve(world, monkeypatch):
    import threading
    worker = world.worker()
    monkeypatch.setattr(worker, "recover_interrupted", lambda: 1 / 0)
    stop = threading.Event()
    stop.set()
    assert worker.serve(stop) == 0


def test_a_failing_heartbeat_does_not_stop_the_main_loop(world, monkeypatch):
    import threading
    worker = world.worker()
    calls = {"touch": 0, "poll": 0}

    def touch():
        calls["touch"] += 1
        raise OSError("share locked")
    monkeypatch.setattr(worker, "touch", touch)
    monkeypatch.setattr(worker, "poll_once", lambda: calls.__setitem__("poll", calls["poll"] + 1) or 0)
    assert worker.serve(threading.Event(), once=True) == 0
    assert calls["poll"] == 1


def test_once_mode_does_not_recover_a_run(world, monkeypatch):
    import threading
    worker = world.worker()
    calls = []
    monkeypatch.setattr(worker, "recover_interrupted", lambda: calls.append(1))
    assert worker.serve(threading.Event(), once=True) == 0
    assert calls == []


def test_recovery_keeps_the_limit_message_and_the_review_file(world, monkeypatch):
    park_review(world)
    worker = world.worker()
    monkeypatch.setattr(worker, "_chain", lambda work, state, token, done: {
        **done, "status": "usage_limit", "limit_message": "limit text", "review_file": "llm/reviews/run1.md"})
    worker.recover_interrupted()
    done = json.loads((world.exchange / "llm" / "done" / "run1.json").read_text(encoding="utf-8"))
    assert done["limit_message"] == "limit text" and done["review_file"] == "llm/reviews/run1.md"


def test_a_resume_removes_a_stale_cancel_marker_and_proceeds(world):
    world.mode({"letters": "noop"})
    world.run()
    cancel = world.exchange / "llm" / "cancel"
    cancel.mkdir(parents=True)
    (cancel / "run1").write_text("cancel", encoding="ascii")
    world.mode({})
    done = world.run(run_id="run2", batch=None)
    assert [r["status"] for r in done["resumed"]] == ["ok"] and not (cancel / "run1").exists()
    assert "results_run1.json" in world.results()


# ---- the deadline of the request (audit A5, decision of 11 Nov: today's scoring first) ------------------------------

T0 = datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc)


def deadline_in(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def clocked(world, advance_after_scorer=0, **kw):
    """A worker with a fixed clock; the clock moves on by `advance_after_scorer` minutes once the scorer has run."""
    clock = {"t": T0}
    worker = world.worker(now=lambda: clock["t"], **kw)
    original = worker._role

    def role(name, *args, **more):
        out = original(name, *args, **more)
        if name == "scorer":
            clock["t"] += timedelta(minutes=advance_after_scorer)
        return out
    worker._role = role
    return worker


def run_names(world):
    return [(c["role"], Path(c["cwd"]).name) for c in world.role_calls()]


def test_a_request_with_a_bad_deadline_is_refused(world):
    for bad in ("tomorrow", 12345, "2026-11-11T10:00:00", ["x"]):                     # not a time, not a string, no zone, not a string
        assert headless.validate_request({**world.request(), "deadline": bad}) == "bad deadline"
    assert headless.validate_request({**world.request(), "deadline": deadline_in(100)}) is None
    assert headless.validate_request(world.request()) is None                          # missing: as before
    done = world.run(deadline="soon")
    assert done["status"] == "error" and done["message"] == "bad deadline" and world.role_calls() == []


def test_a_cancel_marker_stops_todays_scorer_before_it_starts(world):
    cancel = world.exchange / "llm" / "cancel"
    cancel.mkdir(parents=True)
    (cancel / "run1").write_text("cancel", encoding="ascii")
    done = world.run()
    assert done["status"] == "error" and done["message"] == "cancelled" and world.role_calls() == []


def test_todays_scorer_does_not_start_when_it_does_not_fit_before_the_deadline(world):
    scorer = copy.deepcopy(load_config()["headless"]["roles"]["scorer"]["timeout_minutes"])        # 150
    logged = []
    fits = world.run(worker=clocked(world, log=logged.append), deadline=deadline_in(scorer + headless.DEADLINE_MARGIN_MINUTES))
    assert fits["status"] == "ok" and [r for r, _ in run_names(world)][0] == "scorer"          # exactly 15 minutes to spare: starts
    world.mode({})
    (world.exchange / "batches" / "batch_run2.json").write_text("{}", encoding="utf-8")
    done = world.run(worker=clocked(world, log=logged.append), run_id="run2", batch="batches/batch_run2.json",
                     deadline=deadline_in(scorer + headless.DEADLINE_MARGIN_MINUTES - 1))
    assert done["status"] == "ok" and done["message"].startswith("deferred") and "scorer" in done["message"]
    assert [n for _, n in run_names(world)].count("run2") == 0
    assert any("event=deferred" in line and "role=scorer" in line for line in logged)


def test_letters_that_do_not_fit_are_parked_without_using_a_retry(world):
    logged = []
    worker = clocked(world, advance_after_scorer=150, log=logged.append, resume_attempts=1)
    done = world.run(worker=worker, deadline=deadline_in(170))                         # after the scorer 20 minutes remain; letters need 45 + 15
    assert done["status"] == "ok" and done["message"].startswith("deferred") and "letters" in done["message"]
    assert [r for r, _ in run_names(world)] == ["scorer"]
    state = json.loads((world.work() / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "letters" and state["attempts"] == 0 and state["deferred"] is True
    assert any("event=deferred" in line and "role=letters" in line for line in logged)
    for _ in range(3):                                                                 # a deferred resume neither counts nor gives up
        result = worker._resume(world.work(), TOKEN)
        assert result["status"] == "ok" and result["message"].startswith("deferred")
    state = json.loads((world.work() / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "letters" and state["attempts"] == 0
    assert "results_run1.json" not in world.results()
    done = world.run(run_id="run2", batch=None)                                        # without a deadline it finishes
    assert [r["status"] for r in done["resumed"]] == ["ok"] and "results_run1.json" in world.results()


def test_a_parked_run_that_does_not_fit_is_left_parked_with_its_retries(world):
    world.mode({"letters": "noop"})
    world.run()                                                                        # parks run1 in the letters stage (a real failure)
    world.mode({})
    state_file = world.work() / "state.json"
    before = json.loads(state_file.read_text(encoding="utf-8"))
    done = world.run(worker=clocked(world), run_id="run2", batch=None, deadline=deadline_in(30))
    assert done["resumed"] == [] and done["status"] == "ok" and done["message"].startswith("deferred")
    assert json.loads(state_file.read_text(encoding="utf-8")) == before
    assert len([c for c in world.role_calls() if c["role"] == "letters"]) == 1
    done = world.run(worker=clocked(world), run_id="run3", batch=None, deadline=deadline_in(200))
    assert [r["status"] for r in done["resumed"]] == ["ok"]


def test_todays_scorer_runs_before_parked_runs_are_resumed(world):
    world.mode({"letters": "noop"})
    world.run()
    world.mode({})
    (world.exchange / "batches" / "batch_run2.json").write_text("{}", encoding="utf-8")
    done = world.run(run_id="run2", batch="batches/batch_run2.json")
    assert [r["status"] for r in done["resumed"]] == ["ok"]
    assert run_names(world)[2:] == [("scorer", "run2"), ("letters", "run2"), ("review", "run2"), ("letters", "run1"), ("review", "run1")]


def test_recovery_respects_the_deadline_of_the_claimed_request(world):
    park_review(world)
    claimed = world.exchange / "llm" / "claimed" / "run1.json"
    req = json.loads(claimed.read_text(encoding="utf-8"))
    claimed.write_text(json.dumps({**req, "deadline": deadline_in(30)}), encoding="utf-8")
    reviews = len([c for c in world.role_calls() if c["role"] == "review"])
    world.worker(now=lambda: T0).recover_interrupted()
    done = json.loads((world.exchange / "llm" / "done" / "run1.json").read_text(encoding="utf-8"))
    assert done["status"] == "ok" and done["message"].startswith("deferred")
    assert len([c for c in world.role_calls() if c["role"] == "review"]) == reviews
    state = json.loads((world.work() / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "review" and "results_run1.json" not in world.results()


def test_a_late_done_file_reaches_the_next_finish(world):
    done_dir = world.exchange / "llm" / "done"
    done_dir.mkdir(parents=True)
    late = {"run_id": "run0", "status": "ok", "started": "x", "attention": [{"job_id": 1, "kind": "cv", "note": "check the dates"}],
            "review_summary": "Job 1: dates look odd", "review_file": "llm/reviews/run0.md", "resumed": [], "dropped": []}
    (done_dir / "run0.late.json").write_text(json.dumps(late), encoding="utf-8")
    done = world.run(batch=None)
    assert [r["run_id"] for r in done["resumed"]] == ["run0"]
    assert pipeline.done_parts(done)[1]["attention"] == late["attention"]
    assert any("Job 1: dates look odd" in line for line in pipeline.review_lines(done))
    assert not (done_dir / "run0.late.json").exists() and (done_dir / "run0.late.json.merged").exists()
    assert world.run(run_id="run2", batch=None)["resumed"] == []                      # not read twice


def test_a_usage_limit_in_todays_scorer_does_not_cost_parked_runs_a_retry(world):
    world.mode({"letters": "noop"})
    world.run()                                                                        # run1 parked in the letters stage, attempts 0
    world.mode({"scorer": "limit", "letters": "limit"})
    (world.exchange / "batches" / "batch_run2.json").write_text("{}", encoding="utf-8")
    done = world.run(run_id="run2", batch="batches/batch_run2.json", resume_attempts=1)
    assert done["status"] == "usage_limit" and done["resumed"] == []
    state = json.loads((world.work() / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "letters" and state["attempts"] == 0
    assert run_names(world)[2:] == [("scorer", "run2")]


def test_the_resumed_runs_inside_a_late_done_file_reach_finish(world):
    done_dir = world.exchange / "llm" / "done"
    done_dir.mkdir(parents=True)
    late = {"run_id": "run0", "status": "ok", "started": "x", "attention": [], "resumed": [
        {"run_id": "runP", "status": "ok", "attention": [{"job_id": 1, "kind": "cv", "note": "nested flag"}],
         "review_summary": "Job 1: nested review"}]}
    (done_dir / "run0.late.json").write_text(json.dumps(late), encoding="utf-8")
    done = world.run(batch=None)
    flags = [i for part in pipeline.done_parts(done) for i in (part.get("attention") or [])]
    assert any(i["note"] == "nested flag" for i in flags)
    assert any("nested review" in line for line in pipeline.review_lines(done))


def test_a_crash_after_reading_a_late_done_file_keeps_its_data(world):
    done_dir = world.exchange / "llm" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "run0.late.json").write_text(json.dumps({"run_id": "run0", "status": "ok", "started": "x", "resumed": [],
                                                         "attention": [{"job_id": 1, "kind": "cv", "note": "late flag"}]}), encoding="utf-8")
    worker = world.worker()

    def boom(*args, **kwargs):
        raise RuntimeError("x")
    worker._chain = boom
    done = world.run(worker=worker)
    assert done["status"] == "error" and "crashed" in done["message"]
    assert [e["attention"] for e in done["resumed"]] == [[{"job_id": 1, "kind": "cv", "note": "late flag"}]]
    assert not (done_dir / "run0.late.json").exists() and (done_dir / "run0.late.json.merged").exists()


def test_a_late_done_file_stays_when_the_done_file_cannot_be_written(world):
    done_dir = world.exchange / "llm" / "done"
    done_dir.mkdir(parents=True)
    (done_dir / "run0.late.json").write_text(json.dumps({"run_id": "run0", "status": "ok", "started": "x", "resumed": []}), encoding="utf-8")
    worker = world.worker()

    def broken(run_id, done):
        raise OSError("disk full")
    worker._write_done = broken
    world.request()
    with pytest.raises(OSError):
        worker.poll_once()
    assert (done_dir / "run0.late.json").exists()


def test_a_deferred_resume_is_not_given_up_with_zero_retries(world):
    world.mode({"letters": "noop"})
    world.run()
    state_file = world.work() / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["deferred"] = True
    state_file.write_text(json.dumps(state), encoding="utf-8")
    worker = world.worker(resume_attempts=0)

    def chain(work, st, token, done):
        st["deferred"] = True
        return {"status": "partial", "attention": [], "review_summary": "", "message": "x"}
    worker._chain = chain
    result = worker._resume(world.work(), TOKEN)
    assert result["status"] == "partial" and "given up" not in result["message"]
    assert json.loads(state_file.read_text(encoding="utf-8"))["stage"] == "letters"


@pytest.mark.parametrize("status", ["usage_limit", "auth"])
def test_a_resume_cut_by_the_usage_limit_or_auth_is_not_a_retry(world, status):
    world.mode({"letters": "noop"})
    world.run()
    worker = world.worker(resume_attempts=1)
    worker._chain = lambda work, st, token, done: {"status": status, "attention": [], "review_summary": "", "message": "x"}
    first = worker._resume(world.work(), TOKEN)
    second = worker._resume(world.work(), TOKEN)
    state = json.loads((world.work() / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "letters" and state["attempts"] == 0
    assert "given up" not in first["message"] and "given up" not in second["message"]
