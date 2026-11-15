"""Keeps every test away from the developer's own files and from the network.

* The user's `config.yaml` in the repository root is never read: tests start from the defaults of
  `config.example.yaml` (a test can still pass an explicit config file).
* The real `.env` is never loaded by `cli.main()` (it would put the Gmail app password into os.environ for the rest of the run).
* No test may open a real network connection. HTTP, IMAP and SMTP are blocked unless a test replaces them with its
  own fake (monkeypatch in the test overrides this).

Reason: on 31 Oct 2026 the user's real config switched fetching of UBS pages on, a test run picked it up and sent real
requests. That must not be possible again.
"""
import imaplib
import smtplib
import socket
import urllib.request

import pytest

from app import cli, config, documents
from tests import real_path_guard


@pytest.fixture(autouse=True)
def isolated_from_the_real_environment(monkeypatch, tmp_path_factory):
    nowhere = tmp_path_factory.mktemp("no_user_config") / "config.yaml"          # does not exist
    monkeypatch.setattr(config, "DEFAULT_USER_CONFIG", nowhere)
    real_load_yaml = config._load_yaml

    def load_yaml_with_readers_off(path):
        """config.example.yaml switches five Workday readers on (4 Nov 2026); a test that runs `prepare` must not call them
        (the fake fetch answers 404). A test that is about the shipped setting reads the file itself."""
        data = real_load_yaml(path)
        if path == config.EXAMPLE_PATH:
            for key in ("swisscom", "vontobel", "hitachienergy", "novartis", "deutschebank"):
                data["adapters"][key]["enabled"] = False
        return data
    monkeypatch.setattr(config, "_load_yaml", load_yaml_with_readers_off)
    monkeypatch.delenv("RECRUITMENT_WEB_PASSWORD_HASH", raising=False)       # the web login and the container bind read these two (v0.74.0)
    monkeypatch.delenv("RECRUITMENT_CONTAINER", raising=False)
    monkeypatch.setattr(cli, "load_env", lambda *a, **k: {})        # cli.main() must never read the developer's real .env into os.environ

    # generated PDFs: the default folder (output/documents in the repository) is never written by a test
    real_documents_dir = documents.documents_dir
    pdf_root = tmp_path_factory.mktemp("pdf_output")

    def documents_dir(cfg):
        return pdf_root if cfg["paths"]["documents"] == "output/documents" else real_documents_dir(cfg)

    monkeypatch.setattr(documents, "documents_dir", documents_dir)

    # approved copies of the PDFs: the default folder (output/approved in the repository) is never written by a test either
    real_approved_dir = documents.approved_dir
    approved_root = tmp_path_factory.mktemp("approved_output")

    def approved_dir(cfg):
        return approved_root if cfg["paths"]["approved"] == "output/approved" else real_approved_dir(cfg)

    monkeypatch.setattr(documents, "approved_dir", approved_dir)

    def blocked(*args, **kwargs):
        raise AssertionError("a test tried to use the network; fake it with monkeypatch")

    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", blocked)
    monkeypatch.setattr(smtplib, "SMTP", blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


# Guard against writes into the real repository (data/jobs.db, data/run.lock, logs/, backups/, exchange/, output/).
# Only the pytest-xdist controller (or a plain run) compares: the snapshot is taken at the start and compared at the end,
# after every worker has finished. Set RECRUITMENT_SKIP_REAL_PATH_GUARD=1 when the app runs at the same time.
_REAL_SNAPSHOT = {}


def pytest_sessionstart(session):
    if real_path_guard.skipped() or hasattr(session.config, "workerinput"):
        return
    _REAL_SNAPSHOT["before"] = real_path_guard.snapshot(config.ROOT)


def pytest_sessionfinish(session, exitstatus):
    if "before" not in _REAL_SNAPSHOT or real_path_guard.skipped() or hasattr(session.config, "workerinput"):
        return
    found = real_path_guard.differences(_REAL_SNAPSHOT["before"], real_path_guard.snapshot(config.ROOT))
    if found:
        print("\nREAL PATH GUARD: the test run changed the real repository (a test wrote outside its temporary folders):")
        for line in found[:30]:
            print("  " + line)
        session.exitstatus = 1
