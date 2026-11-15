"""REAL network checks of the description resolver (goal.md 5.1a). Skipped unless RECRUITMENT_NETWORK_TESTS=1.

Run by hand, not in CI:  RECRUITMENT_NETWORK_TESTS=1 python -m pytest tests/test_resolver_network.py -q
conftest.py blocks the network for every test (urlopen and sockets), so these tests put the real sockets back (PageFetcher opens its own opener, not urlopen). They make a handful of slow,
polite requests with the honest User-Agent of the config: one AXA list call, one Refline list call and one Refline
job page, one Liebherr search and job page, one jobs.ch search and job page. Nothing is stored.
"""
import os
import socket
import time
import urllib.request

import pytest

from app.config import load_config
from app.descriptions_fetch import PageFetcher
from app.resolver import jibe, jobcloud, liebherr, refline
from app.resolver.complete import is_complete
from app.resolver.robots import Robots

pytestmark = pytest.mark.skipif(os.environ.get("RECRUITMENT_NETWORK_TESTS") != "1",
                                reason="network test: set RECRUITMENT_NETWORK_TESTS=1 to run it by hand")

_REAL_CONNECT = socket.create_connection
_REAL_URLOPEN = urllib.request.urlopen          # taken at import time, before conftest.py blocks it for each test
AXA = {"base_url": "https://careers.axa.com"}
ZKB = {"base_url": "https://apply.refline.ch/792841"}
LIEBHERR = {"base_url": "https://www.liebherr.com"}
JOBS = {"base_url": "https://www.jobs.ch"}


@pytest.fixture
def fetch(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _REAL_URLOPEN)
    monkeypatch.setattr(socket, "create_connection", _REAL_CONNECT)
    cfg = load_config()
    return PageFetcher(cfg["descriptions"]["user_agent"], cfg["resolver"]["timeout_seconds"]), cfg["descriptions"]["user_agent"]


def test_axa_list_answers_with_the_full_text(fetch):
    get, ua = fetch
    robots = Robots(get, ua)
    url = "https://careers.axa.com/api/jobs?keywords=claims&page=1"
    assert robots.allowed(url)
    time.sleep(max(5, robots.crawl_delay("careers.axa.com")))
    found = jibe.candidates(get, AXA, "Claims Smart Automation")
    assert found and all(c.posting_id and c.url.startswith("https://careers.axa.com/") for c in found)
    assert any(is_complete(c.text or "") for c in found)


def test_refline_list_and_one_job_page(fetch):
    get, ua = fetch
    robots = Robots(get, ua)
    assert robots.allowed("https://apply.refline.ch/792841/search.html")
    time.sleep(max(5, robots.crawl_delay("apply.refline.ch")))
    found = refline.candidates(get, ZKB, "")
    assert found
    time.sleep(max(5, robots.crawl_delay("apply.refline.ch")))
    text = refline.detail_text(get, found[0])
    assert text and len(text) > 500


def test_liebherr_search_and_one_job_page(fetch):
    get, ua = fetch
    robots = Robots(get, ua)
    assert robots.allowed("https://www.liebherr.com/de-ch/karriere/offene-stellen-3360980?term=process")
    time.sleep(max(5, robots.crawl_delay("www.liebherr.com")))
    found = liebherr.candidates(get, LIEBHERR, "Business Process Owner (m/f/d)")
    assert found and all(c.url.startswith("https://www.liebherr.com/") for c in found)
    time.sleep(max(5, robots.crawl_delay("www.liebherr.com")))
    text = liebherr.detail_text(get, found[0])
    assert text and len(text) > 500


def test_jobcloud_search_and_one_job_page(fetch):
    get, ua = fetch
    robots = Robots(get, ua)
    assert robots.allowed("https://www.jobs.ch/en/vacancies/?term=data%20analyst")
    time.sleep(max(5, robots.crawl_delay("www.jobs.ch")))
    found = jobcloud.candidates(get, JOBS, "Data Analyst")
    assert found and all(c.url.startswith("https://www.jobs.ch/en/vacancies/detail/") and c.url.endswith("/") for c in found)
    assert robots.allowed(found[0].url)
    time.sleep(max(5, robots.crawl_delay("www.jobs.ch")))
    text = jobcloud.detail_text(get, found[0])
    assert text and len(text) > 500
