"""Snapshot of the real repository paths a test must never write to (used by the hooks in `tests/conftest.py`)."""
import os
from pathlib import Path

WATCHED_FILES = ("data/jobs.db", "data/run.lock")
WATCHED_FOLDERS = ("logs", "backups", "exchange", "output")
SKIP_VARIABLE = "RECRUITMENT_SKIP_REAL_PATH_GUARD"


def skipped():
    return bool(os.environ.get(SKIP_VARIABLE))


def snapshot(root):
    """Relative path -> (size, mtime_ns) for the watched files and every file or folder below the watched folders."""
    root = Path(root)
    found = {}

    def record(path):
        try:
            stat = path.stat()
        except OSError:
            return
        found[path.relative_to(root).as_posix()] = (0 if path.is_dir() else stat.st_size, 0 if path.is_dir() else stat.st_mtime_ns)

    for name in WATCHED_FILES:
        if (root / name).exists():
            record(root / name)
    for name in WATCHED_FOLDERS:
        base = root / name
        if base.is_dir():
            for path in base.rglob("*"):
                record(path)
    return found


def differences(before, after):
    out = [f"added: {p}" for p in sorted(after.keys() - before.keys())]
    out += [f"removed: {p}" for p in sorted(before.keys() - after.keys())]
    out += [f"changed: {p}" for p in sorted(p for p in before.keys() & after.keys() if before[p] != after[p])]
    return out
