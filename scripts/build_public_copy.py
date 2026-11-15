"""Builds the sanitised public portfolio copy of this repository (goal.md 7.4 "Public portfolio copy", v0.95.0).

    python scripts/build_public_copy.py --out DIR [--force] [--pytest] [--allow-dirty]

What it does:
1. Takes the tracked files (`git ls-tree` at HEAD; content from `git show HEAD:path`). Untracked files and .claude/worktrees never get in.
   A dirty tree is refused. `--allow-dirty` reads the working tree instead (tracked and new, not ignored, files; CRLF turned to LF),
   for a build before the work is committed.
2. Keeps what the include globs of scripts/public_copy.yaml allow and no exclude glob removes; puts the fictional persona of
   examples/persona/ at profile/; strips `<!-- private:start -->` ... `<!-- private:end -->` blocks; applies the literal replacements
   of the rules file and of the untracked scripts/public_copy.local.yaml (the real values); adds the MIT LICENSE.
3. Scans the output (e-mail, phone, IPv4, secret shapes, denylist literals, binaries, image EXIF). Any finding prints
   `path:line:rule` with the value masked and ends with exit code 1.
4. `--pytest` runs the test suite inside the copy and prints the counts.

Exit codes: 0 clean, 1 scan findings or failing suite, 2 refused (dirty tree, bad --out, missing rules).
Stdlib and PyYAML only. The script never reads the database, `.env` or `config.yaml`.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "scripts" / "public_copy.yaml"
LOCAL = ROOT / "scripts" / "public_copy.local.yaml"
MARKER = ".public-copy-build"            # written into the output folder; --force only clears a folder that has it (or is empty)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}(?!\d)")
PHONE_CH = re.compile(r"(?<![\w.+-])0\d{2}[ .]\d{3}[ .]\d{2}[ .]\d{2}(?![\w-])")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d]|\.\d)")
TOKENS = {
    "token-github": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "token-github-pat": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    "token-anthropic": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    "token-aws": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "app-password": re.compile(r"(?<![A-Za-z])[a-z]{4}(?: [a-z]{4}){3}(?![A-Za-z])"),
}
APP_PASSWORD_CONTEXT = re.compile(r"pass|secret|token|credential|gmail|smtp|imap", re.IGNORECASE)    # four short words in a row are prose otherwise
ALWAYS_IP = tuple(ipaddress.ip_network(n) for n in ("127.0.0.1/32", "0.0.0.0/32", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    masked: str
    digest: str                                 # sha256 prefix of the stripped line, for `scan_allow`

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.rule}  {self.masked}  hash={self.digest}"


class Refused(Exception):
    """A request the build will not carry out (exit code 2)."""


# --- rules ---------------------------------------------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def fold(text: str) -> str:
    """NFKD, no combining marks, l-stroke as l, lower case: 'Morgan' and 'morgan' compare equal."""
    decomposed = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L"))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def glob_match(pattern: str, path: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    regex = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(regex, path) is not None


def any_match(patterns, path: str) -> bool:
    return any(glob_match(p, path) for p in patterns)


def excluded_by(rules: dict, path: str) -> str | None:
    """The reason of the first exclude rule that removes `path`, or None."""
    for rule in rules.get("exclude", []):
        if glob_match(rule["glob"], path) and not any_match(rule.get("except", []), path):
            return rule["reason"]
    return None


def selected(rules: dict, path: str) -> bool:
    return any_match(rules.get("include", []), path) and excluded_by(rules, path) is None


# --- reading the repository ----------------------------------------------------------------------------------------------

def git(*args: str, cwd: Path = ROOT) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def is_dirty(cwd: Path = ROOT) -> bool:
    """Tracked files differing from HEAD (untracked files are not content of HEAD and never copied from there)."""
    return bool(git("status", "--porcelain", "--untracked-files=no", cwd=cwd).strip())


def read_sources(rules: dict, allow_dirty: bool, cwd: Path = ROOT) -> dict[str, bytes]:
    """Path -> content of every file the rules select, plus the persona files under their target path."""
    persona = rules.get("persona")
    wanted = lambda p: selected(rules, p) or (persona and p.startswith(persona["source"]))      # noqa: E731
    files: dict[str, bytes] = {}
    if allow_dirty:
        names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", cwd=cwd).split(b"\0")
    else:
        names = git("ls-tree", "-r", "-z", "--name-only", "HEAD", cwd=cwd).split(b"\0")
    for raw in names:
        if not raw:
            continue
        path = raw.decode("utf-8")
        if not wanted(path):
            continue
        if allow_dirty:
            file = cwd / path
            if not file.is_file():
                continue
            data = file.read_bytes()
            if not _is_binary(data):
                data = data.replace(b"\r\n", b"\n")
        else:
            data = git("show", f"HEAD:{path}", cwd=cwd)
        files[path] = data
    return files


def _is_binary(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


# --- transforming --------------------------------------------------------------------------------------------------------

def strip_private(text: str, rules: dict, path: str = "") -> str:
    if any_match(rules["private_block"].get("skip_paths", []), path):
        return text                                                    # files that quote the markers (this script, its rules, its tests)
    start, end = (re.escape(rules["private_block"][k]) for k in ("start", "end"))
    return re.sub(rf"[ \t]*{start}.*?{end}[ \t]*\n?", "", text, flags=re.DOTALL)


def apply_replacements(text: str, path: str, entries: list[dict]) -> str:
    for e in entries:
        if ("paths" in e and not any_match(e["paths"], path)) or any_match(e.get("skip_paths", []), path):
            continue
        flags = re.IGNORECASE if e.get("ignore_case") else 0
        if e.get("regex"):
            text = re.sub(e["find"], e["replace"], text, flags=flags)                  # the replacement may use a group reference
        else:
            text = re.sub(re.escape(e["find"]), lambda _m, r=e["replace"]: r, text, flags=flags)
    return text


def transform(path: str, data: bytes, rules: dict, local: dict) -> bytes:
    if _is_binary(data):
        return data
    text = strip_private(data.decode("utf-8"), rules, path)
    entries = rules.get("replacements", [])
    if not any_match(rules.get("replace_skip_paths", []), path):
        entries = entries + local.get("replacements", [])
    text = apply_replacements(text, path, entries)
    return text.encode("utf-8")


def plan(files: dict[str, bytes], rules: dict, local: dict) -> dict[str, bytes]:
    """The output tree: selected files transformed, the persona moved to profile/, the license added."""
    out: dict[str, bytes] = {}
    persona = rules.get("persona")
    for path, data in files.items():
        if persona and path.startswith(persona["source"]):
            target = persona["target"] + path[len(persona["source"]):]
            out[target] = transform(target, data, rules, local)
        elif selected(rules, path):
            out[path] = transform(path, data, rules, local)
    out["LICENSE"] = rules["license"].encode("utf-8")
    return out


# --- scanning ------------------------------------------------------------------------------------------------------------

def _mask(value: str) -> str:
    return f"[masked, {len(value)} chars]"


def _digest(line: str) -> str:
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:10]


def _ip_ok(value: str, rules: dict) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return True                                           # an octet above 255: a version string, not an address
    return any(ip in net for net in ALWAYS_IP) or value in rules.get("ip_allow", [])


def _phone_ok(value: str, rules: dict) -> bool:
    digits = re.sub(r"\D", "", value)
    return digits[-7:] in ("0000000", "1234567") or digits in rules.get("phone_allow", [])


def _email_ok(value: str, rules: dict) -> bool:
    low = value.lower()
    domain = low.rsplit("@", 1)[1]
    labels = domain.split(".")
    if low == "noreply@anthropic.com" or low in {e.lower() for e in rules.get("email_allow", [])}:
        return True
    return any(".".join(labels[i:]) in rules.get("email_domain_allow", []) for i in range(len(labels)))


def deny_entries(deny) -> list[tuple[int, str, list[str]]]:
    """(number, literal, exempt path globs) of the denylist; an entry is a text or {value: text, exempt_paths: [globs]}."""
    out = []
    for i, d in enumerate(deny or []):
        value, skip = (d["value"], d.get("exempt_paths", [])) if isinstance(d, dict) else (d, [])
        if value:
            out.append((i, value, skip))
    return out


def scan_text(path: str, text: str, rules: dict, deny: list[str] | None = None, patterns: bool = True) -> list[Finding]:
    """Findings of one text file, `scan_allow` not yet applied."""
    found: list[Finding] = []
    folded_deny = [(i, fold(lit)) for i, lit, skip in deny_entries(deny) if not any_match(skip, path)]
    exempt = any_match(rules.get("denylist_exempt_paths", []), path)
    for number, line in enumerate(text.split("\n"), 1):
        digest = _digest(line)
        if patterns:
            for m in EMAIL.finditer(line):
                if not _email_ok(m.group(0), rules):
                    found.append(Finding(path, number, "email", _mask(m.group(0)), digest))
            for rx in (PHONE_INTL, PHONE_CH):
                for m in rx.finditer(line):
                    if not _phone_ok(m.group(0), rules):
                        found.append(Finding(path, number, "phone", _mask(m.group(0)), digest))
            for m in IPV4.finditer(line):
                if not _ip_ok(m.group(0), rules):
                    found.append(Finding(path, number, "ipv4", _mask(m.group(0)), digest))
            for rule, rx in TOKENS.items():
                if rule == "app-password" and not APP_PASSWORD_CONTEXT.search(line):
                    continue
                for m in rx.finditer(line):
                    if any(w in m.group(0).lower() for w in rules.get("placeholder_words", [])):
                        continue
                    found.append(Finding(path, number, rule, _mask(m.group(0)), digest))
        if folded_deny and not exempt:
            low = fold(line)
            for i, literal in folded_deny:
                if literal in low:
                    found.append(Finding(path, number, "denylist", f"denylist entry #{i + 1}", digest))
    return found


def has_exif(path: str, data: bytes) -> bool:
    low = path.lower()
    if low.endswith((".jpg", ".jpeg")):
        return b"Exif\0\0" in data[:65536] or b"http://ns.adobe.com/xap" in data[:65536]
    if low.endswith(".png"):
        return any(c in data for c in (b"eXIf", b"tEXt", b"iTXt", b"zTXt"))
    return low.endswith(IMAGE_SUFFIXES) and b"Exif" in data[:65536]


def scan_tree(tree: dict[str, bytes], rules: dict, local: dict | None = None) -> list[Finding]:
    """All findings of the output tree, minus the ones `scan_allow` lists."""
    deny = (local or {}).get("denylist", [])
    found: list[Finding] = []
    for path in sorted(tree):
        data = tree[path]
        found += _path_findings(path, deny, rules)
        if _is_binary(data):
            if not any_match(rules.get("binary_allow", []), path):
                found.append(Finding(path, 0, "binary", "not on the binary allowlist", _digest("")))
            elif has_exif(path, data):
                found.append(Finding(path, 0, "exif", "image carries metadata", _digest("")))
            continue
        found += scan_text(path, data.decode("utf-8"), rules, deny, patterns=not any_match(rules.get("scan_skip_patterns", []), path))
    allowed = {(a["path"], a["line_hash"], a.get("rule")) for a in rules.get("scan_allow", [])}
    return [f for f in found if (f.path, f.digest, f.rule) not in allowed and (f.path, f.digest, None) not in allowed]


def _path_findings(path: str, deny: list[str], rules: dict) -> list[Finding]:
    if any_match(rules.get("denylist_exempt_paths", []), path):
        return []
    low = fold(path)
    return [Finding(path, 0, "denylist-path", f"denylist entry #{i + 1}", _digest(""))
            for i, lit, skip in deny_entries(deny) if fold(lit) in low and not any_match(skip, path)]


# --- writing and running -------------------------------------------------------------------------------------------------

def check_out(out: Path, force: bool, root: Path = ROOT) -> Path:
    out = out.resolve()
    if out == root or root in out.parents or out in root.parents:
        raise Refused(f"--out must be outside the repository ({root}); got {out}")
    if out.exists() and not out.is_dir():
        raise Refused(f"--out {out} is a file")
    if out.exists() and any(out.iterdir()):
        if not force:
            raise Refused(f"--out {out} is not empty; use --force to rebuild a previous copy")
        if not (out / MARKER).exists() and set(p.name for p in out.iterdir()) - {".git"}:
            raise Refused(f"--out {out} was not made by this script (no {MARKER} file); empty it by hand")
    return out


def write_tree(tree: dict[str, bytes], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for child in out.iterdir():
        if child.name == ".git":
            continue                                                  # a repository the owner created in the copy keeps its history
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for path, data in tree.items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (out / MARKER).write_text("Made by scripts/build_public_copy.py; --force may rebuild this folder.\n", encoding="utf-8")


def run_pytest(out: Path) -> tuple[int, str]:
    """Runs the suite inside the copy; returns (exit code, the summary line)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("RECRUITMENT_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    parallel = ["-n", "auto"] if importlib.util.find_spec("xdist") else []          # the suite takes 12 minutes in one process
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *parallel], cwd=out, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if re.search(r"\b(passed|failed|error|errors)\b", ln)), "no summary line")
    if proc.returncode:
        print("\n".join(lines[-40:]))
    for cache in list(out.rglob("__pycache__")) + [out / ".pytest_cache"]:
        shutil.rmtree(cache, ignore_errors=True)
    return proc.returncode, summary


def build(out: Path, *, force=False, allow_dirty=False, pytest=False, root: Path = ROOT, rules_path: Path = RULES,
          local_path: Path = LOCAL, say=print) -> int:
    if not rules_path.is_file():
        raise Refused(f"rules file {rules_path} is missing")
    rules = load_yaml(rules_path)
    local = load_yaml(local_path) if local_path.is_file() else {}
    if not local.get("denylist"):
        say(f"warning: {local_path.name} has no denylist; only the pattern rules protect this build")
    out = check_out(out, force, root)
    if not allow_dirty and is_dirty(root):
        raise Refused("the working tree has changes to tracked files; commit them or use --allow-dirty (builds from the working tree)")
    tree = plan(read_sources(rules, allow_dirty, root), rules, local)
    findings = scan_tree(tree, rules, local)
    for f in findings:
        say(str(f))
    if findings:
        say(f"{len(findings)} finding(s); nothing was written to {out}")
        return 1
    write_tree(tree, out)
    say(f"copy written to {out}: {len(tree)} files, 0 findings")
    if pytest:
        code, summary = run_pytest(out)
        say(f"pytest in the copy: {summary}")
        return 1 if code else 0
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the sanitised public copy of this repository.")
    ap.add_argument("--out", required=True, type=Path, help="output folder, outside the repository")
    ap.add_argument("--force", action="store_true", help="rebuild a non-empty folder made by an earlier build (.git is kept)")
    ap.add_argument("--allow-dirty", action="store_true", help="build from the working tree instead of HEAD")
    ap.add_argument("--pytest", action="store_true", help="run the test suite inside the copy and report the counts")
    args = ap.parse_args(argv)
    try:
        return build(args.out, force=args.force, allow_dirty=args.allow_dirty, pytest=args.pytest)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
