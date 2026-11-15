"""`reselect` and `release-closed` (v0.90.0): the stored top list after a job started waiting for a description, and closed jobs that
still wait for a score. Offline, on temporary databases."""
import logging
from datetime import timedelta

import pytest

from app import adapters, cli, db, postpone
from app.adapters import six
from app.results import compute_selection, import_results, select_top
from tests.test_batch_results import NOW, add_jobs, batch, results_file, score, status
from tests.test_batch_results import cfg   # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def clean_logger(monkeypatch):
    monkeypatch.setattr(cli, "load_env", lambda *a, **k: {})
    log = logging.getLogger("recruitment")
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)
    yield
    for h in list(log.handlers):
        h.close()
        log.removeHandler(h)


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg["paths"]["database"])
    yield c
    c.close()


@pytest.fixture
def config_file(tmp_path, cfg, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(f"paths:\n  database: {(tmp_path / 'jobs.db').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
                    f"  logs: {(tmp_path / 'logs').as_posix()}\n  lock: {(tmp_path / 'run.lock').as_posix()}\n"
                    "scoring:\n  top_n: 3\n", encoding="utf-8")
    return path


def run(config_file, *args):
    return cli.main(["--config", str(config_file), *args])


def selection(conn):
    return [(r["job_id"], r["rank"]) for r in conn.execute("SELECT job_id, rank FROM batch_jobs WHERE batch_id = 'run1' AND selected = 1 ORDER BY rank")]


def open_postponements(conn):
    return conn.execute("SELECT COUNT(*) FROM postponements WHERE resolved_at IS NULL").fetchone()[0]


@pytest.fixture
def imported(conn, cfg):
    """Five described jobs scored 90..50, top 3 stored; then the job ranked second loses its description."""
    cfg["scoring"]["top_n"] = 3
    ids = add_jobs(conn, cfg, 5)
    batch(conn, cfg)
    import_results(conn, cfg, results_file(cfg, "run1", [score(j, 90 - 10 * i) for i, j in enumerate(ids)]), NOW)
    assert selection(conn) == [(ids[0], 1), (ids[1], 2), (ids[2], 3)]
    conn.execute("UPDATE jobs SET description_missing = 1 WHERE id = ?", (ids[1],))
    conn.commit()
    return ids


def test_compute_selection_is_what_select_top_stores(conn, cfg, imported):
    ids = imported
    chosen, queued = compute_selection(conn, "run1", 3, True)
    assert chosen == [ids[0], ids[2], ids[3]] and queued == [(2, ids[1])]
    assert selection(conn) == [(ids[0], 1), (ids[1], 2), (ids[2], 3)]            # computing wrote nothing
    assert select_top(conn, "run1", 3, True) == chosen


def test_reselect_preview_writes_nothing(config_file, conn, imported, capsys):
    ids = imported
    before = [tuple(r) for r in conn.execute("SELECT * FROM batch_jobs ORDER BY job_id")]
    assert run(config_file, "reselect") == 0
    out = capsys.readouterr().out
    assert "batch run1" in out and f"2:{ids[1]}" in out and "preview" in out
    assert [tuple(r) for r in conn.execute("SELECT * FROM batch_jobs ORDER BY job_id")] == before
    assert status(conn, ids[1]) == "scored"


def test_reselect_apply_queues_the_held_job_and_renumbers_the_ranks(config_file, conn, imported, capsys):
    ids = imported
    assert run(config_file, "reselect", "--apply") == 0
    assert "1 batch(es) re-picked" in capsys.readouterr().out
    assert selection(conn) == [(ids[0], 1), (ids[2], 2), (ids[3], 3)]
    assert conn.execute("SELECT queue_rank FROM batch_jobs WHERE job_id = ?", (ids[1],)).fetchone()[0] == 2
    assert status(conn, ids[1]) == "not_selected" and status(conn, ids[3]) == "scored"
    assert run(config_file, "reselect") == 0
    assert "0 batch(es)" in capsys.readouterr().out                                # nothing stale any more


def test_reselect_leaves_a_batch_without_waiting_jobs_alone(config_file, conn, cfg, capsys):
    cfg["scoring"]["top_n"] = 3
    ids = add_jobs(conn, cfg, 3)
    batch(conn, cfg)
    import_results(conn, cfg, results_file(cfg, "run1", [score(j, 90 - 10 * i) for i, j in enumerate(ids)]), NOW)
    assert run(config_file, "reselect", "--apply") == 0
    assert "0 batch(es)" in capsys.readouterr().out


# --- closed jobs that wait for a score ----------------------------------------------------------------------

def _close(conn, job_id):
    conn.execute("UPDATE jobs SET closed_at = '2026-11-10T06:00:00Z' WHERE id = ?", (job_id,))
    conn.commit()


@pytest.fixture
def stuck(conn, cfg):
    """A job of an imported batch that stayed awaiting_llm with an open postponement."""
    ids = add_jobs(conn, cfg, 2)
    batch(conn, cfg)
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 80)]), NOW)
    postpone.record(conn, ["run1"], set(), NOW)
    assert status(conn, ids[1]) == "awaiting_llm" and open_postponements(conn) == 1
    return ids


def test_closing_a_waiting_job_ends_the_waiting(conn, cfg, stuck):
    ids = stuck
    conn.execute("UPDATE job_sources SET source = ? WHERE job_id = ?", (six.SPEC.source, ids[1]))      # the job is now a sighting of the SIX list
    conn.commit()
    assert adapters.track_listing(conn, six.SPEC, set(), 1) == (1, 0)
    assert status(conn, ids[1]) == "new" and open_postponements(conn) == 0
    assert conn.execute("SELECT to_status FROM status_history WHERE job_id = ? ORDER BY id DESC LIMIT 1", (ids[1],)).fetchone()[0] == "new"


def test_a_job_in_a_batch_not_imported_yet_keeps_waiting_when_closed(conn, cfg):
    ids = add_jobs(conn, cfg, 1)
    batch(conn, cfg)
    _close(conn, ids[0])
    assert postpone.release_closed(conn, ids[0]) is None and status(conn, ids[0]) == "awaiting_llm"


def test_a_closed_job_in_an_old_batch_that_was_never_imported_is_released(conn, cfg):
    first = add_jobs(conn, cfg, 1)[0]
    batch(conn, cfg, "run1")
    _close(conn, first)
    add_jobs(conn, cfg, 1, start=2)
    batch(conn, cfg, "run2", now=NOW + timedelta(days=1))               # the closed job is not carried into it
    assert postpone.release_closed(conn, first) == "new" and status(conn, first) == "new"


def test_release_closed_command_previews_then_applies(config_file, conn, stuck, capsys):
    ids = stuck
    _close(conn, ids[1])
    assert run(config_file, "release-closed") == 0
    out = capsys.readouterr().out
    assert f"job {ids[1]} |" in out and "awaiting_llm -> new" in out and "1 open postponement(s) would be resolved" in out
    assert status(conn, ids[1]) == "awaiting_llm" and open_postponements(conn) == 1
    assert run(config_file, "release-closed", "--apply") == 0
    assert "1 released" in capsys.readouterr().out
    assert status(conn, ids[1]) == "new" and open_postponements(conn) == 0
    assert run(config_file, "release-closed", "--apply") == 0
    assert "0 closed job(s)" in capsys.readouterr().out


def test_a_closed_job_with_a_score_goes_back_to_scored_for_a_new_score(conn, cfg):
    ids = add_jobs(conn, cfg, 1)
    batch(conn, cfg)
    import_results(conn, cfg, results_file(cfg, "run1", [score(ids[0], 70)]), NOW)
    conn.execute("UPDATE jobs SET status = 'awaiting_llm', needs_rescore = 0 WHERE id = ?", (ids[0],))
    _close(conn, ids[0])
    assert postpone.release_closed(conn, ids[0]) == "scored"
    assert tuple(conn.execute("SELECT status, needs_rescore FROM jobs WHERE id = ?", (ids[0],)).fetchone()) == ("scored", 1)
