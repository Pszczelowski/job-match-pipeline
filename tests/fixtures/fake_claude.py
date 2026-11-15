"""A fake `claude` for tests/test_headless.py: no network, no model. It sits next to `mode.json` (what each role does) and a
`payload` folder (the files a role writes); it records every call in `calls.jsonl`. The child environment of the worker is an
allowlist, so the mode cannot come from the environment: it is read from the folder of this script."""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]
mode = json.loads((HERE / "mode.json").read_text(encoding="utf-8"))


def value(flag):
    return args[args.index(flag) + 1] if flag in args else None


def record(**extra):
    with (HERE / "calls.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "env": sorted(os.environ), "cwd": os.getcwd(), **extra}) + "\n")


def emit(status, text, rc=0, **more):
    body = {"type": "result", "subtype": "success", "is_error": status != "ok", "result": text, "num_turns": 3,
            "usage": {"input_tokens": 10, "output_tokens": 5}, "modelUsage": {value("--model") or "none": {}}, **more}
    print(json.dumps(body))
    sys.exit(rc)


bad = [k for k in os.environ if k.startswith("ANTHROPIC_") or "GMAIL" in k]
if bad:
    record(note="forbidden environment")
    sys.exit(97)

if args[:2] == ["auth", "status"]:
    record(note="auth status")
    print(json.dumps(mode.get("auth_status", {"loggedIn": True, "authMethod": "oauth_token"})))
    sys.exit(0)

role = {"scorer-cv": "scorer", "letter-writer": "letters", "doc-reviewer": "review"}.get(value("--agent"))
results_dir = Path.cwd().parents[2] / "results" if len(Path.cwd().parents) > 2 else Path("/nonexistent")   # --check-cli runs in a short temp folder
record(role=role, results_seen=sorted(os.listdir(results_dir)) if results_dir.is_dir() else [], token_seen=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""), effort_env=os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"),
       revision_addendum="Headless revision" in Path(value("--agents")).read_text(encoding="utf-8") if value("--agents") else False,
       brief=value("-p"))
behaviour = mode.get("roles", {}).get(role, "ok")
run_id = Path.cwd().name


def put(name, target):
    text = (HERE / "payload" / name).read_text(encoding="utf-8").replace("RUNID", run_id)
    Path(target).write_text(text, encoding="utf-8")


if behaviour == "hang":
    time.sleep(120)
if behaviour == "limit":
    if role == "scorer" and mode.get("chunk_before_limit"):
        put("chunk.json", f"results_{run_id}.p01.json")
    emit("error", "You've hit your session limit · resets 3pm (Europe/Zurich)", 1)
if behaviour == "auth":
    emit("error", "Failed to authenticate. API Error: 401", 1, api_error_status=401)
if behaviour == "max_turns":
    emit("error", "stopped", 1, subtype="error_max_turns")
if behaviour == "crash":
    sys.exit(2)
if behaviour == "stderr_crash":                 # v0.78.2: the CLI dies and tells why on stderr, with the token in it
    sys.stderr.write("boom: upstream refused the call, token " + os.environ["CLAUDE_CODE_OAUTH_TOKEN"] + "\n")
    sys.exit(2)
if behaviour == "error_after_chunk":            # v0.78.2: one chunk is written, then the CLI reports an error
    put("chunk.json", f"results_{run_id}.p01.json")
    emit("error", "API Error: 500 upstream failed", 1)

def leak(path, field="summary"):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if field == "run_id":
        data["run_id"] += " " + os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    else:
        data["results"][0]["summary"] += " " + os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    Path(path).write_text(json.dumps(data), encoding="utf-8")


if run_id.startswith("rev-"):                   # v0.85.0: a revision; the writer edits document_<run>.json, the reviewer writes review.md and review.json
    if role in ("scorer", "letters"):
        if behaviour != "noop":
            put(mode.get("revision_doc", "revision_doc.json"), f"document_{run_id}.json")
            if behaviour == "leak":
                leak(f"document_{run_id}.json", "run_id")
    elif role == "review":
        put("revision_review.json", "review.json")
        Path("review.md").write_text("The revision is fine.\n", encoding="utf-8")
        if behaviour == "leak":
            Path("review.md").write_text("report " + os.environ["CLAUDE_CODE_OAUTH_TOKEN"], encoding="utf-8")
        if behaviour == "no_report":
            Path("review.md").unlink()
elif role == "scorer":
    put("chunk.json", f"results_{run_id}.p01.json")
    put(mode.get("scorer_docs", "docs_cv.json"), f"results_{run_id}.json")        # v0.78.2: a test names another documents payload
elif role == "letters" and behaviour != "noop":
    put(mode.get("letters_docs", "docs_full.json"), f"results_{run_id}.json")
    if behaviour == "leak":
        leak(f"results_{run_id}.json")
elif role == "review":
    put("review.json", "review.json")
    Path("review.md").write_text("Job 1: no change. Job 2: one claim softened.\n", encoding="utf-8")
    if behaviour == "leak":
        Path("review.md").write_text("report " + os.environ["CLAUDE_CODE_OAUTH_TOKEN"], encoding="utf-8")
    if behaviour == "no_report":
        Path("review.md").unlink()
emit("ok", "done")
