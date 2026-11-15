"""The guard that notices a test writing into the real repository (database, lock, logs, backups, exchange, output).

The guard itself runs for the whole session (`tests/conftest.py`, `tests/real_path_guard.py`); these tests check its logic on a
temporary copy of the layout, so they never touch the real folders.
"""
import os

from tests import real_path_guard as guard


def _layout(root):
    (root / "data").mkdir()
    (root / "data" / "jobs.db").write_bytes(b"db")
    (root / "logs").mkdir()
    (root / "logs" / "a.log").write_text("one", encoding="utf-8")
    for name in ("backups", "exchange", "output"):
        (root / name).mkdir()
    (root / "exchange" / "results.json").write_text("{}", encoding="utf-8")
    return root


def test_an_untouched_layout_has_no_differences(tmp_path):
    root = _layout(tmp_path)
    assert guard.differences(guard.snapshot(root), guard.snapshot(root)) == []


def test_a_new_file_in_logs_is_reported(tmp_path):
    root = _layout(tmp_path)
    before = guard.snapshot(root)
    (root / "logs" / "new.log").write_text("x", encoding="utf-8")
    assert any("new.log" in d and "added" in d for d in guard.differences(before, guard.snapshot(root)))


def test_a_changed_database_a_new_lock_and_a_removed_file_are_reported(tmp_path):
    root = _layout(tmp_path)
    before = guard.snapshot(root)
    (root / "data" / "jobs.db").write_bytes(b"database changed")
    (root / "data" / "run.lock").write_text("1", encoding="utf-8")
    (root / "exchange" / "results.json").unlink()
    found = " | ".join(guard.differences(before, guard.snapshot(root)))
    assert "jobs.db" in found and "changed" in found
    assert "run.lock" in found and "added" in found
    assert "results.json" in found and "removed" in found


def test_an_added_backup_folder_entry_is_reported(tmp_path):
    root = _layout(tmp_path)
    before = guard.snapshot(root)
    (root / "backups" / "jobs-2026.db").write_bytes(b"b")
    assert any("jobs-2026.db" in d for d in guard.differences(before, guard.snapshot(root)))


def test_missing_folders_are_fine(tmp_path):
    assert guard.snapshot(tmp_path) == {}


def test_the_skip_variable_switches_the_guard_off(monkeypatch):
    monkeypatch.setenv("RECRUITMENT_SKIP_REAL_PATH_GUARD", "1")
    assert guard.skipped()
    monkeypatch.delenv("RECRUITMENT_SKIP_REAL_PATH_GUARD")
    assert not guard.skipped()
