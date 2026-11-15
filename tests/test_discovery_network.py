"""REAL network checks of the discovery step (goal.md 5.1a (a)). Skipped unless RECRUITMENT_NETWORK_TESTS=1.

Run by hand, not in CI:  RECRUITMENT_NETWORK_TESTS=1 python -m pytest tests/test_discovery_network.py -q
Three slow, polite requests to boards-api.greenhouse.io with the honest User-Agent of the config: robots.txt, the Scandit board
and a board name that does not exist (HTTP 404); and (v0.75.0) api.ashbyhq.com: robots.txt (401) and a missing board; and (v0.76.0) api.lever.co: robots.txt (Crawl-delay 1) and a missing board (404). Nothing is stored.
"""
import os
import socket
import time
import urllib.request

import pytest

from app.config import load_config
from app.descriptions_fetch import PageFetcher
from app import resolver
from app.resolver import greenhouse
from app.resolver.robots import Robots

pytestmark = pytest.mark.skipif(os.environ.get("RECRUITMENT_NETWORK_TESTS") != "1",
                                reason="network test: set RECRUITMENT_NETWORK_TESTS=1 to run it by hand")

_REAL_CONNECT = socket.create_connection
_REAL_URLOPEN = urllib.request.urlopen          # kept from before conftest.py blocks it; the fetcher opens through build_opener (v0.80.4), so it is the restored socket that makes these tests reach the network


@pytest.fixture
def fetch(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _REAL_URLOPEN)
    monkeypatch.setattr(socket, "create_connection", _REAL_CONNECT)
    cfg = load_config()
    ua = resolver.user_agent(cfg)                   # the same honest User-Agent as the resolver
    yield PageFetcher(ua, cfg["resolver"]["timeout_seconds"]), ua
    time.sleep(6)                                   # a pause before the next test's request


def test_robots_allow_the_interface_and_the_scandit_board_names_its_company(fetch):
    get, ua = fetch
    assert Robots(get, ua).allowed(greenhouse.board_url("scandit"))
    time.sleep(6)
    postings = greenhouse.probe(get, "scandit")
    assert postings and {p.company for p in postings} == {"Scandit"} and all(p.cities for p in postings)


def test_a_board_that_does_not_exist_answers_404(fetch):
    get, _ = fetch
    assert greenhouse.probe(get, "zz-no-such-board-0730") is None


def test_ashby_robots_txt_counts_as_no_rules_and_a_board_that_does_not_exist_is_a_miss(fetch):
    """Ashby answered 401 for robots.txt on 3 Nov 2026 (RFC 9309 2.3.1.3: no rules). The miss answer of the posting API is
    ASSUMED to be HTTP 404 until this test has run; set RECRUITMENT_ASHBY_BOARD=<a board with open jobs> to check the job fields too."""
    from app.resolver import ashby
    get, ua = fetch
    assert Robots(get, ua).allowed(ashby.board_url("zz-no-such-board-0750"))
    time.sleep(6)
    assert ashby.probe(get, "zz-no-such-board-0750") is None
    board = os.environ.get("RECRUITMENT_ASHBY_BOARD")
    if board:
        time.sleep(6)
        postings = ashby.probe(get, board)
        assert postings and all(p.title and p.cities for p in postings[:3]) and any(p.text for p in postings)


def test_lever_robots_txt_allows_the_interface_and_a_board_that_does_not_exist_is_a_miss(fetch):
    """api.lever.co/robots.txt on 4 Nov 2026: "User-agent: *, Allow: /, Crawl-delay: 1"; an unknown board answered HTTP 404.
    Set RECRUITMENT_LEVER_BOARD=<a board with open jobs> (the public demo board is "leverdemo") to check the job fields too."""
    from app.resolver import lever
    get, ua = fetch
    robots = Robots(get, ua)
    assert robots.allowed(lever.board_url("zz-no-such-board-0760")) and robots.crawl_delay(lever.HOST) >= 1
    time.sleep(6)
    assert lever.probe(get, "zz-no-such-board-0760") is None
    board = os.environ.get("RECRUITMENT_LEVER_BOARD")
    if board:
        time.sleep(6)
        postings = lever.probe(get, board)
        assert postings and all(p.title for p in postings[:3]) and any(p.text for p in postings)
