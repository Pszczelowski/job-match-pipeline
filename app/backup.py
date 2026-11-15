"""Database backup, verification and restore (goal.md 4, 5.9).

* `create_backup` copies the live database with SQLite's online backup API (safe while a reader or writer is
  active, WAL included) into `<dir>/jobs_<UTC yyyymmddTHHMMSSZ>.db`. The copy is written under a temporary name, checked
  (integrity, schema version, row count of every table) and only then renamed. A bad copy is deleted.
* Rotation keeps the newest `keep` files of exactly that name pattern and never touches any other file.
* `verify_backup` runs the same checks on any file, read-only.
* `restore_backup` first makes a safety copy of the current database (`jobs_pre-restore_<ts>.db`, a name that rotation
  does not match, so it is never rotated away), then writes the backup into the live path with the backup API.

Nothing here reads or prints job content: only paths, sizes, schema versions and row counts.
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db

log = logging.getLogger("recruitment")

STAMP = "%Y%m%dT%H%M%SZ"
BACKUP_NAME = re.compile(r"^jobs_\d{8}T\d{6}Z\.db$")          # what rotation may delete; nothing else
REQUIRED_TABLES = ("jobs", "job_archive")                     # a file without these is not our database


class BackupError(Exception):
    """A backup, a check or a restore failed. The text is safe to print (no job content)."""


@dataclass
class BackupInfo:
    path: Path
    size: int
    version: int
    counts: dict[str, int] = field(default_factory=dict)


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(STAMP)


def _open_existing(path: Path, read_only: bool) -> sqlite3.Connection:
    if not path.is_file():
        raise BackupError(f"file not found: {path}")
    mode = "ro" if read_only else "rw"
    try:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open {path}: {exc}") from exc


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def _inspect(path: Path) -> BackupInfo:
    """Read-only checks of one database file. Raises BackupError on any problem."""
    conn = _open_existing(path, read_only=True)
    try:
        try:
            problems = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            if problems != ["ok"]:
                raise BackupError(f"integrity check failed for {path.name}: {problems[0]}"
                                  + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""))
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            counts = _counts(conn)
        except sqlite3.Error as exc:
            raise BackupError(f"{path.name} is not a readable database: {exc}") from exc
    finally:
        conn.close()
    missing = [t for t in REQUIRED_TABLES if t not in counts]
    if version < 1 or missing:
        raise BackupError(f"{path.name} is not a job-match database (missing: {', '.join(missing) or 'schema version'})")
    return BackupInfo(path, path.stat().st_size, version, counts)


def verify_backup(path: str | Path) -> BackupInfo:
    return _inspect(Path(path))


def _copy_checked(source: Path, dest: Path) -> BackupInfo:
    """Online copy of `source` to `dest` (a new file), then the checks. `dest` is removed if anything fails."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    src = _open_existing(source, read_only=False)       # read-write: a WAL database needs its -shm file
    try:
        try:
            src.execute("BEGIN")                        # one read snapshot for the counts and the copy
            expected = _counts(src)
            version = src.execute("PRAGMA user_version").fetchone()[0]
            target = sqlite3.connect(str(tmp))
            try:
                src.backup(target)
                target.execute("PRAGMA journal_mode = DELETE")     # a single self-contained file, no -wal/-shm
            finally:
                target.close()
            src.rollback()
        except sqlite3.Error as exc:
            raise BackupError(f"copy failed: {exc}") from exc
        finally:
            src.close()
        info = _inspect(tmp)
        if info.version != version:
            raise BackupError(f"schema version differs: source {version}, copy {info.version}")
        if info.counts != expected:
            diff = sorted(t for t in set(expected) | set(info.counts) if expected.get(t) != info.counts.get(t))
            raise BackupError(f"row counts differ in: {', '.join(diff)}")
        tmp.replace(dest)
        info.path = dest
        return info
    except BaseException:
        tmp.unlink(missing_ok=True)                     # `dest` only appears with the final rename, after the checks
        raise


def rotate(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` files named jobs_<timestamp>.db. Returns the deleted paths."""
    files = sorted((p for p in directory.iterdir() if p.is_file() and BACKUP_NAME.match(p.name)), key=lambda p: p.name)
    doomed = files[:-keep] if keep < len(files) else []
    for p in doomed:
        p.unlink()
    return doomed


def create_backup(source: str | Path, directory: str | Path, keep: int, *, now: datetime | None = None,
                  prefix: str = "jobs") -> tuple[BackupInfo, list[Path]]:
    """Back up `source` into `directory` and rotate. Returns the checked copy and the rotated-away files."""
    directory = Path(directory)
    dest = directory / f"{prefix}_{_stamp(now)}.db"
    if dest.exists():
        raise BackupError(f"{dest.name} already exists (two backups in the same second); try again")
    info = _copy_checked(Path(source), dest)
    return info, rotate(directory, keep)


def restore_backup(backup: str | Path, live: str | Path, directory: str | Path, *, now: datetime | None = None) -> tuple[BackupInfo, Path | None]:
    """Replace the database at `live` with `backup`. Returns the restored state and the safety copy (None if
    there was no database to protect). The caller holds the run lock.

    Refused: a backup that fails its checks, or whose schema version is newer than this code. An older schema is
    restored as it is; the normal migrations apply the next time the database is opened.
    """
    backup, live, directory = Path(backup), Path(live), Path(directory)
    info = _inspect(backup)
    if info.version > len(db.MIGRATIONS):
        raise BackupError(f"backup schema version {info.version} is newer than this code ({len(db.MIGRATIONS)}); "
                          "update the program first")
    safety: Path | None = None
    if live.exists():
        safety = directory / f"jobs_pre-restore_{_stamp(now)}.db"
        try:
            if safety.exists():
                raise BackupError(f"{safety.name} already exists; try again in a second")
            _copy_checked(live, safety)
        except BackupError as exc:
            # A damaged live database is the very reason to restore: keep its raw bytes instead and go on.
            if safety.exists():
                raise
            safety = directory / f"jobs_pre-restore_{_stamp(now)}.unverified.db"
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, safety)
            wal = live.with_name(live.name + "-wal")
            if wal.exists():
                shutil.copy2(wal, safety.with_name(safety.name + "-wal"))
            log.warning("safety copy could not be verified (%s); raw file kept as %s", exc, safety.name)
            for leftover in (live, wal, live.with_name(live.name + "-shm")):      # the raw copy is safe; clear the way
                leftover.unlink(missing_ok=True)
    src = _open_existing(backup, read_only=True)
    try:
        dest = sqlite3.connect(str(live), timeout=30)
        try:
            src.backup(dest)
        except sqlite3.Error as exc:
            raise BackupError(f"restore failed: {exc}" + (f"; the previous database is kept in {safety}" if safety else "")) from exc
        finally:
            dest.close()
    finally:
        src.close()
    restored = _inspect(live)
    if restored.version != info.version or restored.counts != info.counts:
        raise BackupError("the restored database does not match the backup"
                          + (f"; the previous database is kept in {safety}" if safety else ""))
    return restored, safety
