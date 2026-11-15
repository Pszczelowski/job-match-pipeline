"""`python -m app set-status JOB_ID --to STATUS [--yes]` (v0.96.0): the same rules and refusal texts as the web buttons
(app/statusflow.py), a dry run without --yes, the history note `cli`. Every test uses its own temporary database."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import cli, db, statusflow, web
from app.config import load_config
from tests.test_cli import _dump, _seed_job, config_file, run                       # noqa: F401  (fixtures and helpers)
from tests.test_web import HOST

TARGETS = ("shortlisted", "applied", "interview", "offer", "accepted", "rejected", "withdrawn")


def conn_of(config_file):
    return db.connect(load_config(config_file)["paths"]["database"])


def history(config_file, jid):
    c = conn_of(config_file)
    try:
        return [tuple(r) for r in c.execute("SELECT from_status, to_status, note FROM status_history WHERE job_id = ? ORDER BY id", (jid,))]
    finally:
        c.close()


def status(config_file, jid):
    c = conn_of(config_file)
    try:
        return c.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
    finally:
        c.close()


def test_the_command_is_in_the_help_and_takes_only_known_statuses(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["set-status", "1", "--to", "archived"])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])
    assert "set-status" in capsys.readouterr().out
    assert cli.build_parser().parse_args(["set-status", "1", "--to", "offer"]).to == "offer"
    assert set(statusflow.ACTION_FOR_STATUS) == set(TARGETS)


def test_a_dry_run_shows_the_change_and_writes_nothing(config_file, capsys):
    jid = _seed_job(config_file, "applied", key="ss1")
    before = _dump(config_file)
    capsys.readouterr()
    assert run(config_file, "set-status", str(jid), "--to", "interview") == 0
    out = capsys.readouterr().out
    assert f"job {jid} | Data Engineer (ACME) | applied -> interview" in out and "Nothing changed; run again with --yes" in out
    assert _dump(config_file) == before


@pytest.mark.parametrize("start,to", [("scored", "shortlisted"), ("not_selected", "shortlisted"), ("shortlisted", "applied"), ("approved", "applied"),
                                      ("applied", "interview"), ("interview", "offer"), ("applied", "rejected"), ("interview", "rejected"),
                                      ("offer", "rejected"), ("applied", "withdrawn"), ("interview", "withdrawn"), ("offer", "withdrawn")])
def test_every_allowed_change_is_written_with_its_history_row(config_file, capsys, start, to):
    jid = _seed_job(config_file, start, key="ss2")
    capsys.readouterr()
    assert run(config_file, "set-status", str(jid), "--to", to, "--yes") == 0
    assert f"Job {jid} is now {to} (was {start})." in capsys.readouterr().out
    assert status(config_file, jid) == to and history(config_file, jid) == [(start, to, "cli")]


def test_every_refused_change_exits_1_with_the_web_text_and_writes_nothing(config_file, capsys):
    ids = {s: _seed_job(config_file, s, key=f"k-{s}") for s in db.STATUSES}
    before = _dump(config_file)
    refused = 0
    for status_, jid in ids.items():
        for to in TARGETS:
            why = statusflow.refusal(statusflow.ACTION_FOR_STATUS[to], jid, status_, False)
            if not why:
                continue                                                                      # the allowed pairs are tested above
            for flags in ((), ("--yes",)):
                capsys.readouterr()
                assert run(config_file, "set-status", str(jid), "--to", to, *flags) == 1, (status_, to)
                assert why in capsys.readouterr().err
                refused += 1
    assert refused > 100 and _dump(config_file) == before


def test_a_job_that_does_not_exist_exits_1(config_file, capsys):
    before = _dump(config_file)
    capsys.readouterr()
    for flags in ((), ("--yes",)):
        assert run(config_file, "set-status", "9999", "--to", "applied", *flags) == 1
        assert "no open job 9999" in capsys.readouterr().err
    assert _dump(config_file) == before


def test_a_busy_database_exits_1_with_the_busy_wording_and_changes_nothing(config_file, capsys, monkeypatch):
    jid = _seed_job(config_file, "applied", key="ss3")

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "set_status", locked)
    before = _dump(config_file)
    capsys.readouterr()
    assert run(config_file, "set-status", str(jid), "--to", "interview", "--yes") == 1
    assert "may be busy with a pipeline run" in capsys.readouterr().err
    assert _dump(config_file) == before


def test_the_end_state_is_the_same_as_the_bulk_route(config_file, capsys):
    cfg = load_config(config_file)
    path = cfg["paths"]["database"]
    a = _seed_job(config_file, "applied", key="ss4")
    b = _seed_job(config_file, "applied", key="ss5")
    assert run(config_file, "set-status", str(a), "--to", "interview", "--yes") == 0
    c = TestClient(web.create_app(cfg, path), base_url=HOST)
    r = c.post("/bulk-status", data={"action": "interview", "ids": str(b), "next": "applied", "csrf": c.app.state.csrf("bulk:interview"), "confirm": "yes"},
               follow_redirects=False)
    assert r.status_code == 303
    assert status(config_file, a) == status(config_file, b) == "interview"
    assert history(config_file, a) == [("applied", "interview", "cli")] and history(config_file, b) == [("applied", "interview", "web bulk")]
    # and a refusal of the CLI is the refusal the web gives
    assert run(config_file, "set-status", str(a), "--to", "applied", "--yes") == 1
    cli_text = capsys.readouterr().err
    assert statusflow.refusal("applied", a, "interview", False) in cli_text
