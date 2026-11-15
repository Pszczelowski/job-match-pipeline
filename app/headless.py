"""Headless Claude Code on the NAS: the `llm` service (goal.md 5.10, v0.78.0).

The worker (`python -m app llm-worker`) runs in its own container: no database, no `.env` (Gmail password), no `config.yaml`
(home address). It takes a request file from `exchange/llm/requests/`, runs the three role agents one after the other
(scorer, letters, review) with the Claude Code CLI under the user's plan login (the OAuth token from `llm.env`), checks
what they wrote and moves it into `exchange/results/`. It then writes a done file; the scheduler in the `recruitment`
container reads that file and runs `finish`. No paid API (rule 10): the worker refuses to start when an API key or another
way to pay per token is configured, and the child environment is built from an allowlist, never copied.

This module imports neither `app.db` nor the config file. It has no network access of its own (the CLI has).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, quote_plus
from zoneinfo import ZoneInfo

import yaml
from jsonschema import Draft202012Validator

from .config import EFFORT_LEVELS, HEADLESS_ROLES, MODEL_ID_RE
from .postpone import limit_seen
from .schemas import COVER_LETTER, CV, RESULTS_SCHEMA, message as schema_message, problems as schema_violations

CLI_VERSION = "2.1.311"                                   # the binary in the image (Dockerfile); a bump changes both together
TOKEN_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
FORBIDDEN_PREFIXES = ("ANTHROPIC_",)                      # API key, bearer token, base URL, profile, federation ...
FORBIDDEN_NAMES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SIMPLE")
PASSTHROUGH_ENV = ("PATH", "HOME", "CLAUDE_CONFIG_DIR", "TZ", "LANG")      # copied from the worker's environment when set
FORBIDDEN_ARGV = ("--bare", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "bypassPermissions")
AGENT_FILES = {"scorer": "scorer-cv", "letters": "letter-writer", "review": "doc-reviewer"}
ROLE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
DISALLOWED_TOOLS = "Bash,WebFetch,WebSearch,Agent,mcp__*"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
RUN_ID_ENV = "RECRUITMENT_RUN_ID"                         # the scheduler makes the run id; the steps it starts inherit it (v0.78.2)
CHUNK_RE = re.compile(r"^results_(?P<run>.+)\.p(?P<n>\d{2,})\.json$")
STATUSES = ("ok", "partial", "usage_limit", "auth", "timeout", "error")
LIMIT_MESSAGE_MAX = 300
REASON_MAX = 300
STDERR_TAIL_MAX = 2000                                    # per role in the done file
STDERR_LOG_MAX = 500                                      # per role in the log
NOTE_MAX = 200
REVIEW_SUMMARY_MAX = 1500
RESUME_STAGES = ("letters", "review", "publish")
REVISION_RE = re.compile(r"^rev-[0-9]{1,12}$")           # "Ask the writer to fix" (v0.85.0): rev-<id>.json, one document, no state.json
REVISION_INSTRUCTION_MAX = 1000
REVISION_FINDINGS_MAX = 50
REVISION_NO_INSTRUCTION = "No instruction from the user: fix the blocking findings."
DEADLINE_MARGIN_MINUTES = 15                              # a role must end this long before the scheduler's deadline
LINE_ENDINGS_TIMEOUT_S = 180                              # the line-ending check runs in a child process (v0.78.6)
HEARTBEAT_SECONDS = 30                                    # the heartbeat is touched this often while the worker runs a request
APP_ROOT = Path(__file__).resolve().parent.parent
LINE_ENDINGS_CODE = ("import sys; from app.pdf import line_endings_report; "
                     "sys.stdout.buffer.write(line_endings_report(sys.argv[1]).encode('utf-8'))")
DROPPED_MAX = 50                                          # entries listed in the done file; the rest are counted in `dropped_more`


class GuardError(RuntimeError):
    """The worker must not run: a way to pay per token is configured, or the token file is wrong. Never carries a value."""


# ---------------------------------------------------------------------------------------------------- redaction and log files

REDACTED = "[redacted]"
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-\S+"),                                                    # an API key of any kind
    re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?\S+"),                # the value of an authorization header
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),                         # a bearer value
    re.compile(r"\b[A-Za-z0-9_]*(?i:token)=\S+"),                                 # CLAUDE_CODE_OAUTH_TOKEN=... and the like
    re.compile(r"(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{40,}"),                           # a run of 40+ token characters that holds a digit
)


def redact(text, token: str = ""):
    """One function for every text the worker writes (its log, the done file) and the run report reads: the exact token and its
    URL-quoted forms, `sk-ant-` strings, bearer and authorization values, `*TOKEN=` assignments and runs of 40+ token characters
    are replaced by `[redacted]`. Runs before any truncation, so a cut can never leave half a secret behind."""
    if not isinstance(text, str) or not text:
        return text
    if token and len(token) >= 6:
        for form in sorted({token, quote(token, safe=""), quote_plus(token)}, key=len, reverse=True):
            text = text.replace(form, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_obj(obj, token: str = ""):
    """`redact` over every string of a JSON-like structure (keys are kept)."""
    if isinstance(obj, str):
        return redact(obj, token)
    if isinstance(obj, dict):
        return {k: redact_obj(v, token) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, token) for v in obj]
    return obj


LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class UtcFormatter(logging.Formatter):
    """`2026-11-07T06:04:03Z INFO ...`: the stamp is UTC and every line passes `redact`."""
    converter = time.gmtime

    def __init__(self, fmt: str = "%(asctime)s %(levelname)s %(message)s"):
        super().__init__(fmt, LOG_DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def _log_value(value) -> str:
    text = " ".join(str(value).split())
    return text if text and re.fullmatch(r"[A-Za-z0-9_.:/\+@%-]+", text) else json.dumps(text, ensure_ascii=False)


def event_line(run_id, event: str, **fields) -> str:
    """`run=<id> event=<name> key=value ...` (empty values are left out; values with spaces are quoted)."""
    parts = [f"run={run_id or '-'}", f"event={event}"]
    parts += [f"{k}={_log_value(v)}" for k, v in fields.items() if v is not None and v != "" and v != []]
    return " ".join(parts)


class WorkerLog:
    """The log of the llm worker: standard output and `exchange/llm/llm-worker.log` (1 MB x 5), UTC stamps, redacted. A file that
    cannot be opened (read-only folder) leaves the standard output only."""

    def __init__(self, path, stream=None, max_bytes: int = 1_000_000, backups: int = 5):
        self.logger = logging.Logger("llm-worker")                       # not registered: no handler is shared between instances
        self.logger.setLevel(logging.INFO)
        handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stdout)]
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True))
        except OSError:
            pass
        for handler in handlers:
            handler.setFormatter(UtcFormatter())
            self.logger.addHandler(handler)

    def __call__(self, message: str) -> None:
        try:
            self.logger.info(message)
        except Exception:                                                # a full disk must not stop the worker
            pass

    def close(self) -> None:
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)



# ---------------------------------------------------------------------------------------------------- the guard (G1-G6)

def forbidden_names(names) -> list[str]:
    """The names (never values) among `names` that would let the CLI bill the API instead of the plan."""
    return sorted(n for n in names if n.startswith(FORBIDDEN_PREFIXES) or n in FORBIDDEN_NAMES)


def read_secret_file(path) -> dict[str, str]:
    """KEY=VALUE lines of llm.env. Refuses (GuardError, names only) when it holds a forbidden key. Read again for each request."""
    entries: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardError(f"the token file cannot be read ({type(exc).__name__})") from exc
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        entries[key] = value
    bad = forbidden_names(entries)
    if bad:
        raise GuardError(f"the token file holds forbidden setting(s): {', '.join(bad)}")
    return entries


def guard_environment(env) -> None:
    """Refuse to start when the environment of the worker holds a forbidden name (exit 3 in the command)."""
    bad = forbidden_names(env)
    if bad:
        raise GuardError(f"the environment holds forbidden setting(s): {', '.join(bad)}; a paid API key must never reach this service")


def build_child_env(parent, token: str, effort: str) -> dict[str, str]:
    """The environment of the CLI: an allowlist, never a copy of the parent (no ANTHROPIC_*, no mail password)."""
    env = {k: parent[k] for k in PASSTHROUGH_ENV if parent.get(k)}
    env[TOKEN_NAME] = token
    env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
    env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_UPDATES"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def split_command(command: str) -> list[str]:
    """`headless.command` as an argument list (POSIX rules; use forward slashes in a Windows path)."""
    parts = shlex.split(command)
    if not parts:
        raise GuardError("headless.command is empty")
    return parts


def preflight(command: str, env: dict[str, str], *, timeout: float = 60) -> tuple[bool, str]:
    """`claude auth status --json` with the child environment: only the plan login (oauth_token) and no API key source is accepted."""
    try:
        proc = subprocess.run(split_command(command) + ["auth", "status", "--json"], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
        data = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, "the login state could not be read"
    if not isinstance(data, dict):
        return False, "the login state could not be read"
    if data.get("apiKeySource") not in (None, "", "none"):
        return False, "the CLI sees an API key source; refused"
    if data.get("authMethod") != "oauth_token":
        return False, "the CLI is not logged in with the plan token (authMethod is not oauth_token)"
    return True, "oauth_token"


def leak_scan(paths, token: str) -> list[str]:
    """Names of the files that hold the token string or an `sk-ant-` string."""
    hits = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "sk-ant-" in text or (token and token in text):
            hits.append(Path(path).name)
    return hits


# ---------------------------------------------------------------------------------------------------- the CLI call

def parse_agent_file(path) -> tuple[dict[str, str], str]:
    """(front matter as flat key: value text, body) of a `.claude/agents/<name>.md` file."""
    text = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
    if text.startswith("---\n"):
        head, _, body = text[4:].partition("\n---\n")
        meta = {}
        for line in head.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip().strip('"')
        return meta, body.lstrip("\n")
    return {}, text


def build_agents_json(role: str, settings: dict, agents_dir, addendum_dir) -> dict:
    """The `--agents` file: the repo agent file (body) plus the tracked addendum; tools, model and effort from the request."""
    name = AGENT_FILES[role]
    meta, body = parse_agent_file(Path(agents_dir) / f"{name}.md")
    addendum = (Path(addendum_dir) / f"{name}.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    return {name: {"description": meta.get("description", name), "prompt": body.rstrip() + "\n\n" + addendum,
                   "model": settings["model"], "effort": settings["effort"], "tools": list(ROLE_TOOLS)}}


def build_settings(run_dir) -> dict:
    """The `--settings` file: write access to the work dir of this run only; no `env`, no `apiKeyHelper`."""
    work = Path(run_dir).as_posix().lstrip("/")
    return {"permissions": {"allow": [f"Edit(//{work}/**)"],
                            "deny": ["Read(//proc/**)", "Read(//sys/**)", "Read(//home/llm/**)", "Read(//app/llm.env)",
                                     "Edit(//app/profile/**)", "Edit(//app/prompts/**)"]},
            "cleanupPeriodDays": 7}


def build_argv(command: str, role: str, settings: dict, brief: str, run_dir, agents_file, settings_file, *,
               profile_dir="/app/profile", prompts_dir="/app/prompts") -> list[str]:
    argv = split_command(command) + [
        "-p", brief, "--restricted", "--agents", str(agents_file), "--agent", AGENT_FILES[role],
        "--model", settings["model"], "--effort", settings["effort"], "--max-turns", str(settings["max_turns"]),
        "--output-format", "json", "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--tools", ",".join(ROLE_TOOLS), "--disallowedTools", DISALLOWED_TOOLS,
        "--strict-mcp-config", "--disable-slash-commands", "--settings", str(settings_file),
        "--add-dir", str(profile_dir), "--add-dir", str(prompts_dir)]
    for part in argv:
        if part in FORBIDDEN_ARGV:
            raise GuardError("a forbidden option would reach the CLI")
    return argv


@dataclass
class RoleOutcome:
    role: str
    status: str = "ok"                    # ok, usage_limit, auth, timeout, partial, error
    rc: int | None = None
    message: str = ""                     # the `result` text of the CLI (kept out of the logs)
    turns: int | None = None
    duration_s: float = 0.0
    tokens: dict = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    reason: str = ""                      # why the role did not end `ok`, in a few words (v0.78.2; redacted, at most REASON_MAX)
    stderr_tail: str = ""                 # the end of the CLI's standard error (redacted, at most STDERR_TAIL_MAX)
    started: str = ""                     # UTC, `2026-11-07T06:04:03Z`
    ended: str = ""

    def summary(self) -> dict:
        out = {"status": self.status, "rc": self.rc, "duration_s": round(self.duration_s, 1), "turns": self.turns,
               "tokens": self.tokens, "models": self.models}
        out.update({k: v for k, v in (("started", self.started), ("ended", self.ended), ("reason", self.reason),
                                      ("stderr_tail", self.stderr_tail)) if v})
        return out


_AUTH_RE = re.compile(r"not logged in|failed to authenticate|invalid bearer|invalid authentication|oauth token (has expired|is invalid)|"
                      r"oauth access token is invalid", re.I)


def _result_object(stdout: str) -> dict:
    try:
        data = json.loads(stdout)
    except ValueError:
        return {}
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
    return data if isinstance(data, dict) else {}


def _reason(out: RoleOutcome, data: dict, text: str, token: str) -> str:
    """A few words on why the role did not end `ok`: the subtype of the CLI's answer, the answer's text (it is the error text when
    the CLI failed), else the exit code. Redacted before it is cut."""
    if out.status == "ok":
        return ""
    if out.status == "timeout":
        return "timed out; the process was killed"
    bits = []
    subtype = str(data.get("subtype") or "")
    if subtype and subtype != "success":
        bits.append(subtype)
    shown = " ".join(redact(text, token).split())[:REASON_MAX]
    if shown:
        bits.append(shown)
    if not bits:
        bits.append(f"exit code {out.rc}" if out.rc else "the CLI reported an error")
    return ": ".join(bits)


def classify(role: str, rc: int | None, stdout: str, *, timed_out: bool, duration: float, stderr: str = "", token: str = "") -> RoleOutcome:
    out = RoleOutcome(role, rc=rc, duration_s=duration)
    data = _result_object(stdout)
    text = str(data.get("result") or "") if data else stdout.strip()
    out.message = text
    out.turns = data.get("num_turns") if isinstance(data.get("num_turns"), int) else None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    out.tokens = {k: usage[k] for k in ("input_tokens", "output_tokens") if isinstance(usage.get(k), int)}
    model_usage = data.get("modelUsage")
    out.models = sorted(model_usage) if isinstance(model_usage, dict) else []
    marker = f"{data.get('subtype', '')} {data.get('terminal_reason', '')}".lower()
    # a login or limit sentence counts only when the CLI itself reported a failure: a good result may quote such words
    failed = rc != 0 or bool(data.get("is_error")) or data.get("api_error_status") is not None or not data
    if timed_out:
        out.status = "timeout"
    elif data.get("api_error_status") == 401 or (failed and _AUTH_RE.search(text)):
        out.status = "auth"
    elif failed and (limit_seen(text) or limit_seen(stdout)):
        out.status = "usage_limit"
    elif "max_turns" in marker:
        out.status = "partial"
    elif rc != 0 or data.get("is_error"):
        out.status = "error"
    out.reason = _reason(out, data, text, token)
    out.stderr_tail = redact((stderr or "").strip(), token)[-STDERR_TAIL_MAX:]
    return out


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime(LOG_DATE_FORMAT)


def run_role(argv: list[str], env: dict[str, str], cwd, timeout_s: float, role: str, token: str = "") -> RoleOutcome:
    """Run the CLI once. The process group is killed at the timeout. stdin is closed (the CLI waits for it otherwise). The end of
    standard error is kept (v0.78.2); a CLI that cannot be started is an `error` outcome, not an exception."""
    started, started_at = time.monotonic(), _utc_stamp()
    popen_kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    try:
        proc = subprocess.Popen(argv, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **popen_kw)
    except OSError as exc:
        out = RoleOutcome(role, status="error", reason=f"could not start the CLI ({type(exc).__name__})", started=started_at, ended=_utc_stamp())
        return out
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    out = classify(role, proc.returncode, stdout or "", timed_out=timed_out, duration=time.monotonic() - started,
                   stderr=stderr or "", token=token)
    out.started, out.ended = started_at, _utc_stamp()
    return out


# ---------------------------------------------------------------------------------------------------- reset time

_RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?", re.I)


def parse_reset(message: str, now: datetime, tz_name: str) -> datetime | None:
    """The moment the usage limit resets, read from the limit message ("... resets 3pm (Europe/Zurich)"); the next such
    wall-clock time after `now`. None when the text has no readable time."""
    m = _RESET_RE.search(message or "")
    if not m:
        return None
    hour, minute, ampm, zone = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    try:
        tz = ZoneInfo(zone.strip()) if zone else ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= local:
        at += timedelta(days=1)
    return at


# ---------------------------------------------------------------------------------------------------- paths and request

@dataclass
class Paths:
    exchange: Path
    secrets: Path
    agents_dir: Path
    addendum_dir: Path
    profile_dir: Path
    prompts_dir: Path
    heartbeat: Path

    @property
    def llm(self) -> Path:
        return self.exchange / "llm"

    def sub(self, name: str) -> Path:
        path = self.llm / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def defaults(cls, exchange="/app/exchange", secrets="/app/llm.env", app="/app", home="/home/llm") -> "Paths":
        return cls(Path(exchange), Path(secrets), Path(app) / "headless" / "agents", Path(app) / "prompts" / "headless",
                   Path(app) / "profile", Path(app) / "prompts", Path(home) / "llm-worker.heartbeat")


def parse_deadline(value) -> datetime | None:
    """The request's deadline (an ISO time with a zone, UTC from the scheduler) as an aware UTC datetime; None when it is missing or malformed."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None


def validate_request(req) -> str | None:
    """None when the request is usable, else the reason (a model alias, a missing role, a bad run id ...)."""
    if not isinstance(req, dict):
        return "the request is not a JSON object"
    if not isinstance(req.get("run_id"), str) or not RUN_ID_RE.fullmatch(req["run_id"]):
        return "bad run_id"
    if req.get("batch_file") is not None and not (isinstance(req["batch_file"], str) and req["batch_file"]):
        return "bad batch_file"
    if req.get("deadline") is not None and parse_deadline(req["deadline"]) is None:
        return "bad deadline"
    roles = req.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(HEADLESS_ROLES):
        return "roles must be scorer, letters and review"
    for name, role in roles.items():
        problem = _role_problem(name, role)
        if problem:
            return problem
    return None


def _role_problem(name: str, role) -> str | None:
    if not isinstance(role, dict):
        return f"role {name} is not an object"
    if not isinstance(role.get("model"), str) or not MODEL_ID_RE.fullmatch(role["model"]):
        return f"role {name}: the model must be a full model id (no alias)"
    if role.get("effort") not in EFFORT_LEVELS:
        return f"role {name}: bad effort"
    for key in ("timeout_minutes", "max_turns"):
        if not isinstance(role.get(key), int) or isinstance(role.get(key), bool) or role[key] < 1:
            return f"role {name}: bad {key}"
    return None


def revision_writer(document_kind: str) -> str:
    """The role of the writer of a revision: the letter-writer for a cover letter, the CV writer (`scorer-cv`) for a CV."""
    return "letters" if document_kind == "cover_letter" else "scorer"


def validate_revision_request(req, stem: str = "") -> str | None:
    """None when a revision request is usable, else the reason. The run id must be `rev-<id>` and equal the file name, the roles exactly the
    writer of the document kind and `review`, the instruction at most 1000 characters (it may be empty), the document an object."""
    if not isinstance(req, dict):
        return "the request is not a JSON object"
    if not isinstance(req.get("run_id"), str) or not REVISION_RE.fullmatch(req["run_id"]) or (stem and req["run_id"] != stem):
        return "bad run_id"
    if req.get("kind") != "revision":
        return "bad kind"
    rev = req.get("revision_id")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev != int(req["run_id"][4:]):
        return "bad revision_id"
    if parse_deadline(req.get("deadline")) is None:
        return "bad deadline"
    if req.get("document_kind") not in ("cv", "cover_letter"):
        return "bad document_kind"
    if not isinstance(req.get("content_sha"), str) or not re.fullmatch(r"[0-9a-f]{64}", req["content_sha"]):
        return "bad content_sha"
    if not isinstance(req.get("instruction"), str) or len(req["instruction"]) > REVISION_INSTRUCTION_MAX:
        return f"the instruction must be text of at most {REVISION_INSTRUCTION_MAX} characters"
    findings = req.get("findings")
    if not isinstance(findings, list) or len(findings) > REVISION_FINDINGS_MAX or not all(isinstance(f, dict) for f in findings):
        return "bad findings"
    job = req.get("job")
    if (not isinstance(job, dict) or not isinstance(job.get("job_id"), int) or isinstance(job.get("job_id"), bool) or job["job_id"] < 1):
        return "bad job"
    if not isinstance(req.get("document"), dict):
        return "bad document"
    roles = req.get("roles")
    if not isinstance(roles, dict) or set(roles) != {revision_writer(req["document_kind"]), "review"}:
        return "roles must be the writer of the document and review"
    for name, role in roles.items():
        problem = _role_problem(name, role)
        if problem:
            return problem
    return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _schema_problems(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["not readable JSON"]
    return [e.message[:120] for e in Draft202012Validator(RESULTS_SCHEMA).iter_errors(data)][:3]


@dataclass
class Salvage:
    ok: bool = True                                           # False: the file itself is wrong (not an entry); the caller quarantines it
    problem: str = ""
    kept: int = 0
    dropped: list[dict] = field(default_factory=list)         # {"job_id", "stage", "path"}: what the done file lists
    entries: list[dict] = field(default_factory=list)         # the dropped entries with the reason, for the quarantine file only


def salvage_file(path: Path, *, stage: str, batch_ids=None, variants=None, strip_letters: bool = False) -> Salvage:
    """Keep what is good in a results file (v0.78.2). `finish` rejects a whole file for one bad entry, so the worker drops the entries
    that would do it: a schema violation (the field path is recorded), a repeated job id (the first stays), a job id that is not in
    the batch and an unknown `cv_variant`; the last two are skipped when `batch_ids` or `variants` is empty. The survivors are written
    back atomically. With `strip_letters` every `cover_letter` is removed (the scorer writes the CV only). A problem of the file
    itself (not JSON, wrong version or run id, no `results` list) is returned as `ok=False` and the file is not touched."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Salvage(ok=False, problem="not readable JSON")
    found = schema_violations(data)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        where, why = found[0] if found else ("file", "no results list")
        return Salvage(ok=False, problem=f"{where}: {why}")
    bad: dict[int, tuple[str, str]] = {}
    for where, why in found:
        m = re.match(r"results/(\d+)(?:/|$)", where)
        if not m:
            return Salvage(ok=False, problem=f"{where}: {why}")
        bad.setdefault(int(m.group(1)), (where, why))
    seen: set = set()
    for i, entry in enumerate(data["results"]):
        if i in bad:
            continue
        job_id = entry["job_id"]
        if job_id in seen:
            bad[i] = (f"results/{i}/job_id", "job id repeated")
        elif batch_ids and job_id not in batch_ids:
            bad[i] = (f"results/{i}/job_id", "job id not in the batch")
        elif variants and entry.get("cv_variant") and entry["cv_variant"] not in variants:
            bad[i] = (f"results/{i}/cv_variant", "unknown cv_variant")
        else:
            seen.add(job_id)
    out = Salvage()
    survivors, changed = [], bool(bad)
    for i, entry in enumerate(data["results"]):
        if i in bad:
            job_id = entry.get("job_id") if isinstance(entry, dict) and isinstance(entry.get("job_id"), int) else None
            out.dropped.append({"job_id": job_id, "stage": stage, "path": bad[i][0]})
            out.entries.append({"job_id": job_id, "stage": stage, "path": bad[i][0], "message": bad[i][1], "entry": entry})
            continue
        docs = entry.get("documents")
        if strip_letters and isinstance(docs, dict) and "cover_letter" in docs:
            del docs["cover_letter"]
            changed = True
            if not docs:
                del entry["documents"]
        survivors.append(entry)
    out.kept = len(survivors)
    if changed:
        data["results"] = survivors
        _atomic_write(path, json.dumps(data, ensure_ascii=False))
    return out


def has_cv(path: Path) -> bool:
    """True when some entry of a documents file carries a `cv`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(isinstance(e, dict) and isinstance(e.get("documents"), dict) and e["documents"].get("cv") for e in data.get("results", []))


def worse(a: str, b: str) -> str:
    """The more serious of two run statuses (STATUSES runs from ok to error): a later step never lowers what an earlier one found."""
    rank = {s: i for i, s in enumerate(STATUSES)}
    return a if rank.get(a, len(STATUSES)) >= rank.get(b, len(STATUSES)) else b


def unfinished_jobs(path: Path) -> list[int]:
    """Jobs of a documents file that carry some documents but not both kinds."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in data.get("results", []):
        docs = entry.get("documents") if isinstance(entry, dict) else None
        if docs is not None and not (docs.get("cv") and docs.get("cover_letter")):
            out.append(entry.get("job_id"))
    return out


def clean_attention(raw, valid_ids: set[int]) -> list[dict]:
    """The `attention` list of review.json: unknown job ids and unknown kinds dropped, notes shortened."""
    items = raw.get("attention") if isinstance(raw, dict) else None
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("job_id") not in valid_ids or item.get("kind") not in ("cv", "cover_letter"):
            continue
        note = " ".join(str(item.get("note") or "").split())[:NOTE_MAX]
        if note:
            out.append({"job_id": item["job_id"], "kind": item["kind"], "note": note})
    return out


def check_cli(command: str, paths: Paths, environ=None) -> tuple[bool, str]:
    """`llm-worker --check-cli`: run the role command line with an empty config folder and no token. The CLI must accept every
    flag and answer "not logged in" (an unknown flag or a version without --restricted would fail differently)."""
    import tempfile
    environ = os.environ if environ is None else environ
    settings = {"model": "claude-sonnet-5-5", "effort": "low", "max_turns": 1}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        agents_file, settings_file = tmp_path / "agents.json", tmp_path / "settings.json"
        agents_file.write_text(json.dumps(build_agents_json("scorer", settings, paths.agents_dir, paths.addendum_dir)), encoding="utf-8")
        settings_file.write_text(json.dumps(build_settings(tmp_path)), encoding="utf-8")
        argv = build_argv(command, "scorer", settings, "Reply OK.", tmp_path, agents_file, settings_file,
                          profile_dir=paths.profile_dir, prompts_dir=paths.prompts_dir)
        env = build_child_env({**{k: environ[k] for k in PASSTHROUGH_ENV if environ.get(k)}, "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")},
                              "", "low")
        env.pop(TOKEN_NAME, None)
        out = run_role(argv, env, tmp_path, 120, "scorer")
    if out.status == "auth":
        return True, "flags accepted, not logged in"
    return False, f"unexpected answer from the CLI: {out.status} (rc {out.rc})"


# ---------------------------------------------------------------------------------------------------- the worker

class Worker:
    """Takes requests one by one and runs the chain. All file names are made from the validated run id."""

    def __init__(self, paths: Paths, command: str, *, environ=None, resume_attempts: int = 2, log=lambda msg: None, now=None):
        self.p = paths
        self.now = now or (lambda: datetime.now(timezone.utc))      # a test replaces the clock
        self.deadline: datetime | None = None                   # of the request in hand (audit A5); None: no limit
        self.command = command
        self.environ = os.environ if environ is None else environ
        self.resume_attempts = resume_attempts
        self._late_merged: list[Path] = []                               # late done files read by this request, renamed after its done file
        self._late_entries: list[dict] = []
        self.log = log
        self.minute = 60.0                      # seconds in a configured minute (a test makes it short)
        self.run_id = ""                        # the run the log lines are about (set by handle_claimed and _resume)
        self.token = ""                         # the token of the request in hand; only used to redact

    def _fits(self, minutes: int) -> bool:
        """A role of `minutes` ends, with a margin of 15 minutes, before the request's deadline (the scheduler stops waiting then)."""
        return self.deadline is None or self.now() + timedelta(minutes=minutes + DEADLINE_MARGIN_MINUTES) <= self.deadline

    def _ev(self, event: str, **fields) -> None:
        """One log line `run=<id> event=<name> key=value`; the token and token-like strings never reach it."""
        self.log(redact(event_line(self.run_id, event, **fields), self.token))

    # -- start and loop
    def check_start(self) -> None:
        guard_environment(self.environ)
        if self.p.secrets.exists():
            read_secret_file(self.p.secrets)

    def touch(self) -> None:
        self.p.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.p.heartbeat.write_text(datetime.now().isoformat(timespec="seconds"), encoding="ascii")

    def pending_requests(self) -> list[Path]:
        return sorted(self.p.sub("requests").glob("*.json"))

    def poll_once(self) -> int:
        """Handle every waiting request; returns how many."""
        n = 0
        for req in self.pending_requests():
            claimed = self.p.sub("claimed") / req.name
            try:
                os.replace(req, claimed)                                  # atomic claim: a second worker finds nothing
            except OSError:
                continue
            self.handle_claimed(claimed)
            n += 1
        return n

    def healthy(self, max_age: float = 180, now: float | None = None) -> bool:
        try:
            age = (now if now is not None else time.time()) - self.p.heartbeat.stat().st_mtime
        except OSError:
            return False
        return age <= max_age

    def _beat(self, done: threading.Event) -> None:
        while not done.wait(HEARTBEAT_SECONDS):
            try:
                self.touch()
            except OSError:                                              # a full disk must not stop the worker
                pass

    def serve(self, stop, poll_seconds: float = 5.0, once: bool = False) -> int:
        self._ev("worker_start")
        beat_done = threading.Event()
        beat = threading.Thread(target=self._beat, args=(beat_done,), daemon=True)
        beat.start()
        try:
            if not once:                                                 # --once can run beside the daemon: it must not take over a live run
                try:
                    self.recover_interrupted()
                except Exception as exc:                                 # recovery must never stop the worker
                    self._ev("recover_failed", error=type(exc).__name__)
            beat_failed = False
            while not stop.is_set():
                try:
                    self.touch()
                    beat_failed = False
                except OSError as exc:                                   # a full disk or a locked share must not stop the worker
                    if not beat_failed:
                        self._ev("heartbeat_failed", error=type(exc).__name__)
                    beat_failed = True
                handled = self.poll_once()
                if once:
                    return 0
                if not handled:
                    stop.wait(poll_seconds)
            return 0
        finally:
            beat_done.set()

    def recover_interrupted(self) -> None:
        """After a restart: a claimed request without a done file and without a cancel marker was cut off. A run in the
        letters, review or publish stage is resumed (the retry counter stops a crash loop); any other gets an error done file at
        once, so that the scheduler stops waiting."""
        for claimed in sorted(self.p.sub("claimed").glob("*.json")):
            run_id = claimed.stem
            if not RUN_ID_RE.fullmatch(run_id) or (self.p.sub("done") / f"{run_id}.json").exists() or self._cancelled(run_id):
                continue
            if run_id.startswith("rev-") and any(self.p.sub("done").glob(f"{run_id}.json.processed*")):
                continue                                                 # a revision already finished and stored: nothing was cut off
            work = self.p.sub("work") / run_id
            try:
                state = json.loads((work / "state.json").read_text(encoding="utf-8"))
                stage = state.get("stage")
            except (OSError, ValueError):
                state, stage = None, ""
            outer = self.run_id
            self.run_id = run_id
            self.deadline = None
            try:
                self._ev("worker_recovered", stage=stage or None)
                token = ""
                try:
                    token = read_secret_file(self.p.secrets).get(TOKEN_NAME, "")
                except (GuardError, OSError):
                    pass
                started = _utc_stamp()
                result = None
                if isinstance(state, dict) and stage in RESUME_STAGES and token and state.get("run_id") == run_id:
                    try:
                        req = json.loads(claimed.read_text(encoding="utf-8"))
                        self.deadline = parse_deadline(req.get("deadline"))
                        attempts = req.get("resume_attempts")
                        if isinstance(attempts, int) and not isinstance(attempts, bool) and 0 <= attempts <= 10:
                            self.resume_attempts = attempts
                    except (OSError, ValueError, AttributeError):
                        pass
                    try:
                        result = self._resume(work, token)
                    except Exception as exc:
                        result = {"status": "error", "message": f"the resume after the restart crashed: {type(exc).__name__}"}
                if result is None:
                    result = {"status": "error", "message": "the worker restarted during the run (stage "
                                                            f"{stage or 'unknown'}); the run was not resumed"}
                    kept = {"dropped": [], "status": result["status"]}
                    try:                                                 # chunks the scorer already wrote are not lost
                        self.token = token
                        self._move_chunks(work, token, kept)
                    except OSError:
                        pass
                    result = {**result, "dropped": kept["dropped"], "files": kept.get("files", []),
                              "message": kept.get("message") or result["message"]}
                self._write_done(run_id, {"status": result.get("status", "error"), "roles": {},
                                          "attention": result.get("attention", []), "review_summary": result.get("review_summary", ""),
                                          "message": result.get("message", ""), "dropped": result.get("dropped", []),
                                          "limit_message": result.get("limit_message", ""), "resumed": [], "started": started,
                                          **({"files": result["files"]} if result.get("files") else {}), "ended": _utc_stamp(),
                                          **({"review_file": result["review_file"]} if result.get("review_file") else {})})
            finally:
                self.run_id = outer

    # -- one request
    def handle_claimed(self, claimed: Path) -> dict:
        if claimed.stem.startswith("rev-"):                              # "Ask the writer to fix" (v0.85.0): its own path, no chain, no state.json
            return self.handle_revision(claimed)
        try:
            req = json.loads(claimed.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            req = None
        problem = validate_request(req)
        if problem:
            run_id = claimed.stem if RUN_ID_RE.fullmatch(claimed.stem) else "invalid"
            return self._write_done(run_id, {"status": "error", "message": problem})
        attempts = req.get("resume_attempts")
        if isinstance(attempts, int) and not isinstance(attempts, bool) and 0 <= attempts <= 10:
            self.resume_attempts = attempts
        self.run_id = req["run_id"]
        self.deadline = parse_deadline(req.get("deadline"))
        started_at = _utc_stamp()
        self._ev("request_claimed", file=claimed.name)
        self._late_merged, self._late_entries = [], []
        try:
            done = self.run_request(req)
        except Exception as exc:                                         # a crash must not leave the scheduler waiting for a done file
            done = {"status": "error", "message": f"the worker crashed: {type(exc).__name__}", "roles": {}, "attention": [],
                    "limit_message": "", "review_summary": "", "resumed": list(self._late_entries), "dropped": []}
        self._ev("run_end", status=done.get("status"), message=done.get("message"), dropped=len(done.get("dropped") or []) or None)
        written = self._write_done(req["run_id"], {**done, "started": started_at, "ended": _utc_stamp()})
        for late in self._late_merged:                                   # its data is in the done file now: do not read it twice
            try:
                os.replace(late, late.with_name(late.name + ".merged"))
            except OSError:
                pass
        self._late_merged = []
        return written

    def _write_done(self, run_id: str, done: dict) -> dict:
        done = redact_obj({"run_id": run_id, **done}, self.token)
        target = self.p.sub("done") / f"{run_id}.json"
        try:
            if "started" not in json.loads(target.read_text(encoding="utf-8")):      # the scheduler's synthetic timeout file: keep it
                target = target.with_name(f"{run_id}.late.json")
        except (OSError, ValueError, AttributeError):
            pass
        _atomic_write(target, json.dumps(done, ensure_ascii=False, indent=1))
        return done

    def _cancelled(self, run_id: str) -> bool:
        return (self.p.llm / "cancel" / run_id).exists()

    def _merge_late_done_files(self, done: dict) -> None:
        """A done file the worker wrote after the scheduler's timeout (`<run>.late.json`) never reached `finish`. Its reviewer flags and
        review lines go in through `resumed`, like a resumed run's; the file is renamed so it is not read twice (audit A5)."""
        for late in sorted(self.p.sub("done").glob("*.late.json")):
            if late.name.startswith("rev-"):                             # a revision's late result is not part of a daily run (v0.85.0)
                continue
            try:
                data = json.loads(late.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                nested = data.pop("resumed", None)                       # done_parts reads one level only: lift the late file's own entries
                done["resumed"].append(data)
                done["resumed"].extend(e for e in nested or [] if isinstance(e, dict))
                self._ev("late_done_merged", file=late.name)
            self._late_merged.append(late)                               # renamed only once the done file holds its data

    def run_request(self, req: dict) -> dict:
        run_id = req["run_id"]
        done: dict = {"status": "ok", "roles": {}, "attention": [], "limit_message": "", "review_summary": "", "resumed": [], "dropped": []}
        self._merge_late_done_files(done)
        self._late_entries = list(done["resumed"])
        try:
            entries = read_secret_file(self.p.secrets)
        except GuardError as exc:
            return {**done, "status": "error", "message": str(exc)}
        token = entries.get(TOKEN_NAME, "")
        self.token = token
        if not token:
            return {**done, "status": "auth", "message": "llm.env holds no CLAUDE_CODE_OAUTH_TOKEN line"}
        env = build_child_env(self.environ, token, req["roles"]["scorer"]["effort"])
        ok, why = preflight(self.command, env)
        if not ok:
            return {**done, "status": "auth", "message": why}

        def resume_parked() -> None:                                      # after today's scorer (decision of 11 Nov: today's scoring first)
            if done.get("status") in ("usage_limit", "auth"):             # the account is out of reach: a resume would only burn a retry
                return
            for pending in self._pending_runs(exclude=run_id):
                try:
                    state = json.loads((pending / "state.json").read_text(encoding="utf-8"))
                    role = state.get("stage") if state.get("stage") in ("letters", "review") else None
                    if role and not self._fits(state["roles"][role]["timeout_minutes"]):
                        self._ev("deferred", role=role, run=pending.name, reason="does not fit before the deadline")
                        done["message"] = done.get("message") or f"deferred: the {role} of run {pending.name} does not fit before the deadline"
                        continue                                          # stays parked, its retries unchanged
                    resumed = self._resume(pending, token)
                    done["resumed"].append(resumed)
                    if resumed.get("status") in ("usage_limit", "auth"):
                        return
                except Exception as exc:                                  # one broken old run must not stop today's
                    done["resumed"].append({"run_id": pending.name, "status": "error", "message": type(exc).__name__})

        if req.get("batch_file") is None:
            resume_parked()
            return {**done, "message": done.get("message") or "no batch to score"}
        src = self.p.exchange / req["batch_file"]
        work = self.p.sub("work") / run_id
        work.mkdir(parents=True, exist_ok=True)
        if not src.is_file() or Path(os.path.normpath(src)).parent != Path(os.path.normpath(self.p.exchange / "batches")):
            resume_parked()
            return {**done, "status": "error", "message": "the batch file is missing or outside exchange/batches"}
        if self._cancelled(run_id):                                       # a cancelled request starts no scorer
            resume_parked()
            return {**done, "status": "error", "message": "cancelled"}
        if not self._fits(req["roles"]["scorer"]["timeout_minutes"]):
            self._ev("deferred", role="scorer", reason="does not fit before the deadline")
            done["message"] = "deferred: today's scorer does not fit before the deadline"
            resume_parked()
            return done
        shutil.copyfile(src, work / f"batch_{run_id}.json")
        state = {"run_id": run_id, "stage": "scorer", "attempts": 0, "roles": req["roles"]}
        self._save_state(work, state)
        result = self._chain(work, state, token, done)
        resume_parked()
        return result

    # -- a revision (v0.85.0, goal.md 5.10a): one document, a writer call and a reviewer call, no state.json, nothing moved to exchange/results
    def handle_revision(self, claimed: Path) -> dict:
        try:
            req = json.loads(claimed.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            req = None
        problem = validate_revision_request(req, claimed.stem)
        stem_ok = bool(REVISION_RE.fullmatch(claimed.stem))
        run_id = claimed.stem if stem_ok else "invalid"
        if problem:
            written = self._write_done(run_id, {"status": "error", "outcome": "invalid", "message": problem,
                                                **({"revision_id": int(run_id[4:])} if stem_ok else {})})
            self._claim_handled(claimed)
            return written
        self.run_id = run_id
        self.token = ""
        self.deadline = parse_deadline(req.get("deadline"))
        started_at = _utc_stamp()
        self._ev("request_claimed", file=claimed.name, kind="revision")
        self._late_merged, self._late_entries = [], []
        try:
            done = self.run_revision(req)
        except Exception as exc:                                         # a crash must not leave the scheduler waiting for a done file
            done = {"status": "error", "outcome": "", "message": f"the worker crashed: {type(exc).__name__}"}
        done = {"revision_id": req["revision_id"], "document_kind": req["document_kind"], "job_id": req["job"]["job_id"],
                "content_sha": req["content_sha"], "attention": [], "review_summary": "", "limit_message": "", "roles": {}, **done}
        self._ev("run_end", status=done.get("status"), message=done.get("message"))
        written = self._write_done(run_id, {**done, "started": started_at, "ended": _utc_stamp()})
        self._claim_handled(claimed)
        return written

    def _claim_handled(self, claimed: Path) -> None:
        """The done file is written: the claim is moved aside, so a restart does not take it for a cut-off request."""
        try:
            os.replace(claimed, claimed.with_name(claimed.name + ".handled"))
        except OSError:
            pass

    def _read_revised(self, path: Path, req: dict, original: dict) -> tuple[dict | None, str, str]:
        """(document, outcome, message): the revised document when the file is one entry of the job with only that kind, valid against
        its schema and different from the input; else None, `invalid` or `unchanged` and why."""
        kind = req["document_kind"]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, "invalid", "the revised document is missing or not readable JSON"
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            return None, "invalid", "the revised file must hold exactly one entry"
        entry = results[0]
        docs = entry.get("documents")
        if entry.get("job_id") != req["job"]["job_id"] or not isinstance(docs, dict) or set(docs) != {kind} or not isinstance(docs[kind], dict):
            return None, "invalid", f"the revised file must hold the {kind} of job {req['job']['job_id']} only"
        bad = [f"{'/'.join(map(str, e.path)) or 'document'}: {schema_message(e)}"
               for e in Draft202012Validator(COVER_LETTER if kind == "cover_letter" else CV).iter_errors(docs[kind])]
        if bad:
            return None, "invalid", "the revised document does not fit the schema: " + "; ".join(bad[:3])
        if docs[kind] == original:
            return None, "unchanged", "the writer changed nothing"
        return docs[kind], "", ""

    def run_revision(self, req: dict) -> dict:
        rid, kind = req["run_id"], req["document_kind"]
        writer = revision_writer(kind)

        def fail(status: str, message: str, outcome: str = "", **more) -> dict:
            return {"status": status, "outcome": outcome, "message": message, "roles": roles, **more}

        roles: dict = {}
        try:
            token = read_secret_file(self.p.secrets).get(TOKEN_NAME, "")
        except GuardError as exc:
            return fail("error", str(exc))
        self.token = token
        if not token:
            return fail("auth", "llm.env holds no CLAUDE_CODE_OAUTH_TOKEN line")
        env = build_child_env(self.environ, token, req["roles"][writer]["effort"])
        ok, why = preflight(self.command, env)
        if not ok:
            return fail("auth", why)
        if self._cancelled(rid):
            return fail("error", "cancelled")
        minutes = req["roles"][writer]["timeout_minutes"] + req["roles"]["review"]["timeout_minutes"]
        if not self._fits(minutes):
            return fail("error", "the revision did not fit before the deadline")
        work = self.p.sub("revisions") / rid
        shutil.rmtree(work, ignore_errors=True)                          # a clean slate: an old try of this id must not leak into this one
        work.mkdir(parents=True)
        doc_file = work / f"document_{rid}.json"
        original = req["document"]
        _atomic_write(work / "job.json", json.dumps({"job": req["job"], "instruction": req["instruction"], "findings": req["findings"],
                                                      "document_kind": kind}, ensure_ascii=False, indent=1))
        _atomic_write(doc_file, json.dumps({"run_id": rid, "results": [{"job_id": req["job"]["job_id"], "documents": {kind: original}}]},
                                           ensure_ascii=False))
        state = {"roles": req["roles"]}
        addendum = self.p.addendum_dir / "revision"
        instruction = req["instruction"].strip() or REVISION_NO_INSTRUCTION
        common = (f"Revision {rid}. The one document to edit is {doc_file.name} in the current directory (the {kind.replace('_', ' ')} of job "
                  f"{req['job']['job_id']}); job.json holds the job, the findings that block it and the user's instruction. ")

        out = self._role(writer, state, work, token, common + "Fix it as your instructions say.\nUser instruction: " + instruction, addendum)
        roles[out.role] = out.summary()
        limit = " ".join(out.message.split())[:LIMIT_MESSAGE_MAX] if out.status == "usage_limit" else ""
        if out.status in ("usage_limit", "auth", "timeout"):
            return fail(out.status, out.reason, limit_message=limit)
        if out.status != "ok":
            return fail("error", "the writer did not finish" + (f" ({out.reason})" if out.reason else ""))
        revised, outcome, message = self._read_revised(doc_file, req, original)
        if revised is None:
            return fail("error", message, outcome)
        if self._cancelled(rid):
            return fail("error", "cancelled")
        flagged = self._line_endings(doc_file)
        out = self._role("review", state, work, token,
                         common + "Review and correct it as your instructions say. Write your report to review.md and the list of documents that "
                         "still need the user's attention to review.json. The user's instruction was: " + instruction +
                         f"\nLine-ending problems found in the document:\n{flagged}", addendum)
        roles[out.role] = out.summary()
        if out.status in ("usage_limit", "auth", "timeout"):
            limit = " ".join(out.message.split())[:LIMIT_MESSAGE_MAX] if out.status == "usage_limit" else ""
            return fail(out.status, out.reason, limit_message=limit)
        if out.status != "ok" or not (work / "review.md").is_file() or not (work / "review.json").is_file():
            return fail("error", "the review did not finish")                   # an unreviewed text is never stored
        revised, outcome, message = self._read_revised(doc_file, req, original)
        if revised is None:
            return fail("error", message, outcome)
        hits = leak_scan([doc_file, work / "review.md", work / "review.json"], token)
        if hits:
            return fail("error", "the token string was found in " + ", ".join(hits))
        try:
            raw = json.loads((work / "review.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        attention = [a for a in clean_attention(raw, {req["job"]["job_id"]}) if a["kind"] == kind]
        summary = " ".join((work / "review.md").read_text(encoding="utf-8", errors="replace").split())[:REVIEW_SUMMARY_MAX]
        return {"status": "ok", "outcome": "", "message": "", "document": revised, "attention": attention, "review_summary": summary,
                "roles": roles}

    # -- the chain
    def _save_state(self, work: Path, state: dict) -> None:
        _atomic_write(work / "state.json", json.dumps(state))

    def _role(self, role: str, state: dict, work: Path, token: str, brief: str, addendum_dir=None) -> RoleOutcome:
        settings = state["roles"][role]
        agents_file, settings_file = work / "agents.json", work / "settings.json"
        agents_file.write_text(json.dumps(build_agents_json(role, settings, self.p.agents_dir, addendum_dir or self.p.addendum_dir)), encoding="utf-8")
        settings_file.write_text(json.dumps(build_settings(work)), encoding="utf-8")
        argv = build_argv(self.command, role, settings, brief, work, agents_file, settings_file,
                          profile_dir=self.p.profile_dir, prompts_dir=self.p.prompts_dir)
        env = build_child_env(self.environ, token, settings["effort"])
        self._ev("role_start", role=role, model=settings["model"], effort=settings["effort"], timeout_minutes=settings["timeout_minutes"])
        out = run_role(argv, env, work, settings["timeout_minutes"] * self.minute, role, token)
        self._ev("role_end", role=role, status=out.status, rc=out.rc, duration_s=round(out.duration_s, 1), turns=out.turns,
                 reason=out.reason, stderr_tail=" ".join(out.stderr_tail.split())[-STDERR_LOG_MAX:])
        return out

    def _chain(self, work: Path, state: dict, token: str, done: dict) -> dict:
        run_id = state["run_id"]
        docs_file = work / f"results_{run_id}.json"
        fresh = state["stage"] == "scorer"                               # a resumed run must not strip letters the letter-writer wrote

        def finish(status: str, message: str = "") -> dict:
            done["status"] = worse(done["status"], status)               # never lower what an earlier step found
            if message:
                done["message"] = message
            self._move_chunks(work, token, done)
            return done

        def record(out: RoleOutcome) -> RoleOutcome:
            done["roles"][out.role] = out.summary()
            if out.status == "usage_limit":
                done["limit_message"] = " ".join(out.message.split())[:LIMIT_MESSAGE_MAX]
            return out

        if state["stage"] == "scorer":
            out = record(self._role("scorer", state, work, token,
                                    f"Run id {run_id}. The batch file is batch_{run_id}.json in the current directory. Score it as your instructions say. "
                                    f"Write the chunk files results_{run_id}.pNN.json and the documents file results_{run_id}.json in the current directory."))
            if out.status in ("usage_limit", "auth", "timeout"):
                return finish(out.status, out.reason)
            if not list(self._chunks(work)) and (out.status != "ok" or not docs_file.is_file()):
                return finish("error", "the scorer wrote no result" + (f" ({out.reason})" if out.reason else ""))
            state["stage"] = "letters"
            self._save_state(work, state)
            if out.status in ("partial", "error"):                       # it ended early but wrote chunks: the run is partial
                done["status"] = worse(done["status"], "partial")
        if self._cancelled(run_id):
            return finish("error", "cancelled")
        if not docs_file.is_file():
            state["stage"] = "done"
            self._save_state(work, state)
            return finish("partial" if done["status"] == "partial" else "ok", "no documents to write")
        salvage = self._salvage(work, docs_file, "scorer" if fresh else state["stage"], done, strip_letters=fresh)
        if not salvage.ok:
            return self._quarantine(work, docs_file, token, done, "the documents file does not fit the schema: " + salvage.problem)
        if not has_cv(docs_file):                                        # no entry carries a CV: nothing for the letters or the review
            state["stage"] = "done"
            self._save_state(work, state)
            return finish("partial" if done["status"] == "partial" else "ok", "no documents to write")
        failed = self._stage_letters(work, state, token, done, record)
        if failed is not None:
            return finish(*failed)
        failed = self._stage_review(work, state, token, done, record)
        if failed is not None:
            return finish(*failed)
        return self._publish(work, state, token, done, finish)

    def _stage_letters(self, work, state, token, done, record):
        run_id, docs_file = state["run_id"], work / f"results_{state['run_id']}.json"
        if state["stage"] != "letters":
            return None
        if self._cancelled(run_id):
            return "error", "cancelled"
        if not self._fits(state["roles"]["letters"]["timeout_minutes"]):
            return self._defer(work, state, "letters")
        out = record(self._role("letters", state, work, token,
                                f"Run id {run_id}. Add the cover letters to the documents file results_{run_id}.json in the current directory, "
                                "as your instructions say."))
        if out.status in ("usage_limit", "auth", "timeout"):
            return self._park(work, state, "letters", out.status, out.reason)
        salvage = self._salvage(work, docs_file, "letters", done)
        missing = unfinished_jobs(docs_file) if salvage.ok else []
        if out.status != "ok" or not salvage.ok or missing:
            note = ("cover letters unfinished for job(s) " + ", ".join(map(str, missing)) if missing
                    else "" if salvage.ok else "the documents file does not fit the schema: " + salvage.problem)
            return self._park(work, state, "letters", "error" if out.status == "ok" else out.status, note)
        if not salvage.kept:                                             # every entry was dropped: nothing to review
            state["stage"] = "done"
            self._save_state(work, state)
            return "partial", "no documents left after the checks"
        state["stage"] = "review"
        self._save_state(work, state)
        return None

    def _stage_review(self, work, state, token, done, record):
        run_id, docs_file = state["run_id"], work / f"results_{state['run_id']}.json"
        if state["stage"] != "review":
            return None
        if self._cancelled(run_id):
            return "error", "cancelled"
        if not self._fits(state["roles"]["review"]["timeout_minutes"]):
            return self._defer(work, state, "review")
        flagged = self._line_endings(docs_file)                          # after the letters: the cover letters are in the file now (v0.78.2)
        out = record(self._role("review", state, work, token,
                                f"Run id {run_id}. Review and correct the documents file results_{run_id}.json in the current directory as your instructions say. "
                                "Write your report to review.md and the list of jobs that still need the user's attention to review.json. "
                                f"Line-ending problems found in the documents (letters included):\n{flagged}"))
        if out.status in ("usage_limit", "auth", "timeout"):
            return self._park(work, state, "review", out.status, out.reason)
        salvage = self._salvage(work, docs_file, "review", done)
        if out.status != "ok" or not salvage.ok or not (work / "review.md").is_file() or not (work / "review.json").is_file():
            return self._park(work, state, "review", "error" if out.status == "ok" else out.status, "the review did not finish")
        state["stage"] = "publish"
        self._save_state(work, state)
        return None

    def _line_endings(self, docs_file: Path) -> str:
        """The line-ending report from a child process: fpdf2 keeps its fonts in memory, so a failure or a time-out
        there must not take the worker down (v0.78.6). The child gets no token."""
        env = {"PATH": self.environ.get("PATH", os.defpath), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
               "HOME": self.environ.get("HOME", "/tmp")}                 # fpdf2 asks for the home folder on a missing glyph
        if os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]               # Windows needs it to start Python at all
        try:
            proc = subprocess.run([sys.executable, "-B", "-c", LINE_ENDINGS_CODE, str(docs_file)], cwd=str(APP_ROOT), env=env,
                                  capture_output=True, timeout=LINE_ENDINGS_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._ev("line_endings_unavailable", reason="timeout")
            return "not available"
        except OSError as exc:
            self._ev("line_endings_unavailable", reason=type(exc).__name__)
            return "not available"
        if proc.returncode != 0:
            self._ev("line_endings_unavailable", reason=f"exit {proc.returncode}")
            return "not available"
        return proc.stdout.decode("utf-8", errors="replace").strip() or "not available"

    def _park(self, work: Path, state: dict, stage: str, status: str, message: str = "", deferred: bool = False):
        """Keep the documents file in the work dir for the next request (B4); the attempts are counted on resume, except for a
        deferral (the role did not fit before the deadline, nothing failed)."""
        state["stage"] = stage
        if deferred:
            state["deferred"] = True
        else:
            state.pop("deferred", None)
        self._save_state(work, state)
        return status, message

    def _defer(self, work: Path, state: dict, stage: str):
        message = f"deferred: the {stage} does not fit before the deadline; the run waits for the next request"
        self._ev("deferred", role=stage, reason="does not fit before the deadline")
        return self._park(work, state, stage, "ok", message, deferred=True)

    def _publish(self, work: Path, state: dict, token: str, done: dict, finish) -> dict:
        run_id = state["run_id"]
        docs_file = work / f"results_{run_id}.json"
        hits = leak_scan([docs_file, work / "review.md", work / "review.json", *self._chunks(work)], token)
        if hits:
            return self._quarantine(work, docs_file, token, done, "the token string was found in " + ", ".join(hits))
        data = json.loads(docs_file.read_text(encoding="utf-8"))
        valid_ids = {e.get("job_id") for e in data.get("results", []) if isinstance(e, dict)}
        try:
            raw = json.loads((work / "review.json").read_text(encoding="utf-8"))
        except ValueError:
            raw = {}
        done["attention"] = clean_attention(raw, valid_ids)
        done["review_summary"] = " ".join((work / "review.md").read_text(encoding="utf-8", errors="replace").split())[:REVIEW_SUMMARY_MAX]
        done["letter_findings"] = self._letter_findings(work, data)
        reviews = self.p.sub("reviews")
        shutil.copyfile(work / "review.md", reviews / f"{run_id}.md")
        done["review_file"] = f"llm/reviews/{run_id}.md"
        self._move_chunks(work, token, done)
        results = self.p.exchange / "results"
        results.mkdir(parents=True, exist_ok=True)
        os.replace(docs_file, results / docs_file.name)                  # last: the documents file arrives after the scores and the review
        state["stage"] = "done"                                          # only now: a crash before the replace stays resumable (stage "publish")
        self._save_state(work, state)
        done.setdefault("files", []).append(docs_file.name)
        return done                                                      # done["status"] keeps what the steps before found (partial, error)

    def _letter_findings(self, work: Path, data: dict) -> int | None:
        """The traceability check of every cover letter (finish repeats it with the home address). Never fails the chain."""
        try:
            from . import documents, traceability
            batch = json.loads((work / f"batch_{data['run_id']}.json").read_text(encoding="utf-8"))
            jobs = {j["job_id"]: j for j in batch.get("jobs", [])}
            facts = traceability.load_facts(self.p.profile_dir / "master_cv.yaml")
            total = 0
            for entry in data.get("results", []):
                letter = (entry.get("documents") or {}).get("cover_letter") if isinstance(entry, dict) else None
                if letter and entry["job_id"] in jobs:
                    total += len(documents.check_cover_letter(letter, jobs[entry["job_id"]], facts, date.today()))
            return total
        except Exception:
            return None

    # -- files
    def _chunks(self, work: Path):
        return sorted(p for p in work.glob("results_*.json") if CHUNK_RE.match(p.name))

    def _move_chunks(self, work: Path, token: str, done: dict) -> None:
        """Move the chunk files that pass the checks into exchange/results; the others go to quarantine and make the run an error."""
        results = self.p.exchange / "results"
        results.mkdir(parents=True, exist_ok=True)
        for chunk in self._chunks(work):
            leaked = leak_scan([chunk], token)
            salvage = Salvage(ok=False, problem="") if leaked else self._salvage(work, chunk, "chunk", done)
            if leaked or not salvage.ok:
                self._to_quarantine(work, chunk)
                done["status"] = "error"
                done["message"] = ("the token string was found in " + chunk.name) if leaked else f"{chunk.name}: {salvage.problem}"
                self._ev("chunk_quarantined", file=chunk.name, reason="token found" if leaked else salvage.problem)
                continue
            os.replace(chunk, results / chunk.name)
            done.setdefault("files", []).append(chunk.name)
            self._ev("chunk_moved", file=chunk.name, entries=salvage.kept, dropped=len(salvage.dropped) or None)

    def _salvage_context(self, work: Path):
        """(job ids of the batch, names of the CV variants), each None when it is not known or empty: the checks that need them are skipped."""
        ids = variants = None
        for batch_file in sorted(work.glob("batch_*.json")):
            try:
                jobs = json.loads(batch_file.read_text(encoding="utf-8")).get("jobs", [])
                ids = {j["job_id"] for j in jobs if isinstance(j, dict) and isinstance(j.get("job_id"), int)} or None
            except (OSError, ValueError, AttributeError):
                pass
            break
        try:
            loaded = yaml.safe_load((self.p.profile_dir / "variants.yaml").read_text(encoding="utf-8")) or {}
            variants = set(loaded.get("variants", {})) or None
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            pass
        return ids, variants

    def _salvage(self, work: Path, path: Path, stage: str, done: dict, *, strip_letters: bool = False) -> Salvage:
        """`salvage_file` plus the bookkeeping: the dropped entries go to work/quarantine/<name>.dropped.json, are listed in the done file
        (at most DROPPED_MAX) and logged, and the run is at least partial."""
        ids, variants = self._salvage_context(work)
        out = salvage_file(path, stage=stage, batch_ids=ids, variants=variants, strip_letters=strip_letters)
        if out.ok and out.dropped:
            q = work / "quarantine"
            q.mkdir(exist_ok=True)
            side = q / f"{path.name}.dropped.json"
            try:
                before = json.loads(side.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                before = []
            _atomic_write(side, json.dumps(before + out.entries, ensure_ascii=False, indent=1))
            listed = done.setdefault("dropped", [])
            for item in out.dropped:
                if len(listed) < DROPPED_MAX:
                    listed.append(item)
                else:
                    done["dropped_more"] = done.get("dropped_more", 0) + 1
                self._ev("entry_dropped", file=path.name, job=item["job_id"], stage=item["stage"], path=item["path"])
            done["status"] = worse(done["status"], "partial")
        return out

    def _to_quarantine(self, work: Path, path: Path) -> None:
        q = work / "quarantine"
        q.mkdir(exist_ok=True)
        os.replace(path, q / path.name)

    def _quarantine(self, work: Path, docs_file: Path, token: str, done: dict, message: str) -> dict:
        self._to_quarantine(work, docs_file)
        self._move_chunks(work, token, done)
        done["status"] = "error"
        done["message"] = message
        return done

    # -- resume (B4)
    def _pending_runs(self, exclude: str) -> list[Path]:
        out = []
        for state_file in sorted(self.p.sub("work").glob("*/state.json")):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if state.get("stage") in RESUME_STAGES and state.get("run_id") != exclude:
                out.append(state_file.parent)
        return out

    def _resume(self, work: Path, token: str) -> dict:
        state = json.loads((work / "state.json").read_text(encoding="utf-8"))
        run_id = state["run_id"]
        outer, self.run_id, self.token = self.run_id, run_id, token
        try:
            return self._resume_run(work, state, token)
        finally:
            self.run_id = outer

    def _resume_run(self, work: Path, state: dict, token: str) -> dict:
        run_id = state["run_id"]
        marker = self.p.llm / "cancel" / run_id                          # a deliberate resume overrides an old cancel marker
        try:
            if marker.exists():
                marker.unlink(missing_ok=True)
                self._ev("cancel_cleared")
        except OSError:
            pass
        self._ev("resume", stage=state["stage"], attempt=state["attempts"] + 1)
        was_deferred = bool(state.get("deferred"))                       # parked only for time: not an attempt, never given up on
        if not was_deferred and state["attempts"] >= self.resume_attempts:
            state["stage"] = "failed"
            self._save_state(work, state)
            return {"run_id": run_id, "status": "error", "message": "gave up after the retries"}
        counted = state["attempts"]
        state["attempts"] += 1
        state.pop("deferred", None)
        self._save_state(work, state)
        sub: dict = {"status": "ok", "roles": {}, "attention": [], "limit_message": "", "review_summary": "", "resumed": [], "dropped": []}
        result = self._chain(work, state, token, sub)
        message = result.get("message", "")
        if state.get("deferred"):                                        # it did not fit again: this try did not count
            state["attempts"] = counted
            self._save_state(work, state)
        elif result["status"] in ("usage_limit", "auth"):                # the account had no room: not a failed try, the stage stays resumable
            state["attempts"] = counted
            self._save_state(work, state)
        if (result["status"] != "ok" and state["attempts"] >= self.resume_attempts and state["stage"] in RESUME_STAGES
                and not state.get("deferred")):
            state["stage"] = "failed"
            self._save_state(work, state)
            message = f"failed again, given up after {self.resume_attempts} retries" + (f": {message}" if message else "")
        return {"run_id": run_id, "status": result["status"], "attention": result["attention"],
                "review_summary": result["review_summary"], "message": message, "dropped": result.get("dropped", []),
                "limit_message": result.get("limit_message", ""), "review_file": result.get("review_file", "")}
