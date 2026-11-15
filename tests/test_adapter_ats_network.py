"""REAL network checks of the public-ATS readers (v0.70.0). Skipped unless RECRUITMENT_NETWORK_TESTS=1.

Run by hand, not in CI:  RECRUITMENT_NETWORK_TESTS=1 python -m pytest tests/test_adapter_ats_network.py -q
conftest.py blocks the network for every test, so these tests put the real socket connection back (the fetcher opens through build_opener since v0.80.4). Per reader: robots.txt, the first
page of the list and the first job page, slowly (adapters.delay_seconds) with the honest User-Agent of the config. The list
is read with `store=False` and the database is a temporary one: nothing is stored.
"""
import os
import socket
import urllib.request

import pytest

from app import adapters, db
from app.config import load_config
from app.descriptions_fetch import PageFetcher

pytestmark = pytest.mark.skipif(os.environ.get("RECRUITMENT_NETWORK_TESTS") != "1",
                                reason="network test: set RECRUITMENT_NETWORK_TESTS=1 to run it by hand")

_REAL_CONNECT = socket.create_connection
_REAL_URLOPEN = urllib.request.urlopen          # kept from before conftest.py blocks it; the fetcher opens through build_opener (v0.80.4), so it is the restored socket that makes these tests reach the network


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setattr(urllib.request, "urlopen", _REAL_URLOPEN)
    monkeypatch.setattr(socket, "create_connection", _REAL_CONNECT)
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    cfg = load_config()
    cfg["adapters"]["max_list_pages"] = 1
    conn = db.connect(tmp_path / "net.db")
    yield cfg, conn, PageFetcher(cfg["adapters"]["user_agent"], cfg["adapters"]["timeout_seconds"])
    conn.close()


@pytest.mark.parametrize("key", ["swisscom", "vontobel", "temenos", "swissre", "ey", "zurich", "scandit", "getyourguide", "shares21"])
def test_the_list_and_the_first_job_page_are_read(live, key):
    cfg, conn, fetch = live
    spec = next(s for s in adapters.REGISTRY if s.key == key)
    cfg["adapters"][key].update(enabled=True, decision_note="network check, run by hand")
    summary, jobs = adapters.fetch_list(conn, cfg, spec, fetch, store=False)
    assert summary.error is None, summary.error
    if not jobs:
        pytest.skip(f"{spec.label}: no job on the first page that passes the country filter (valid for a small employer)")
    page = spec.parse_detail(adapters.read_page(cfg, spec, fetch, jobs[0].url))      # the same pacing and robots.txt rules
    assert len(page.text) > 200
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
