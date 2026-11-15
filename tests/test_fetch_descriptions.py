import html as H
import io
import json
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, descriptions_fetch
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.descriptions_fetch import FetchDisabled, FetchError, PageFetcher, eligible_jobs, fetch_descriptions, require_enabled
from app.filters.runner import filter_new_jobs
from app.models import NormalizedJob
from app.parsers.ubs import parse_alert, to_job
from app.pipeline import prepare
from app.ubs_page import PageError, html_to_text, parse_job_page
from tests.test_location import Replay, TODAY
from tests.test_mailbox import FakeIMAP, four_alerts
from app.travel import OsrmProvider, Providers, TransportProvider

UBS = Path(__file__).parent / "fixtures" / "ubs"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
SECTIONS = [
    ("Key responsibilities", "You analyse reporting requirements for the trading desks.<br><br>You test the reports &amp; improve daily data checks."),
    ("The team", "<p>The team works with the business and IT in Zürich on data and reporting.</p>"),
    ("Your skills and experience", "<ul><li>SQL and reporting tools</li><li>Good communication in English</li></ul> Several years in a bank."),
    ("About us", "UBS is a leading wealth manager with a long text about the bank that must not be used for scoring."),
    ("Join us", "At Northwind we know that our people drive our ongoing success, flexible working and more text."),
]


def jid(url):
    return parse_qs(urlsplit(url).query)["jobid"][0]


def make_page(job_id="351249", title="Business Analyst", sections=SECTIONS):
    fields = [{"QuestionName": "", "AnswerValue": title, "VerityZone": "jobtitle"},
              {"QuestionName": "", "AnswerValue": str(job_id), "VerityZone": "reqid"}]
    fields += [{"QuestionName": name, "AnswerValue": body, "VerityZone": f"z{i}"} for i, (name, body) in enumerate(sections)]
    blob = json.dumps({"JobDetails": {"Fields": fields}, "other": [{"x": 1}]})
    return f'<html><body><input id="other" value="x"><input id="preLoadJSON" type="hidden" value="{H.escape(blob, quote=True)}"></body></html>'


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x-test-pass")
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    c["descriptions"].update(fetch_enabled=True, permission_note="test permission note")
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.start_run(c, "run1")
    yield c
    c.close()


class FakePages:
    def __init__(self, pages=None, fail=None):
        self.pages, self.fail, self.calls = pages or {}, fail or {}, []

    def __call__(self, url):
        self.calls.append(url)
        if url in self.fail:
            raise self.fail[url]
        return self.pages[url] if url in self.pages else make_page(jid(url))


def load_alerts(conn, names=None, filtered=False, cfg=None):
    for name in names or sorted(p.name for p in UBS.glob("*.html")):
        for j in parse_alert((UBS / name).read_text(encoding="utf-8")).jobs:
            ingest_job(conn, to_job(j), "run1")
    if filtered:
        filter_new_jobs(conn, cfg, "run1")


# --- the page parser -------------------------------------------------------------------------------

def test_parse_job_page_keeps_job_sections_and_drops_boilerplate():
    page = parse_job_page(make_page())
    assert (page.job_id, page.title) == ("351249", "Business Analyst")
    assert [h for h, _ in page.sections] == ["Key responsibilities", "The team", "Your skills and experience"]
    assert "About us" not in page.text and "wealth manager" not in page.text and "Join us" not in page.text


def test_html_becomes_plain_text():
    text = parse_job_page(make_page()).text
    assert "You analyse reporting requirements for the trading desks.\n\nYou test the reports & improve daily data checks." in text
    assert "• SQL and reporting tools" in text and "• Good communication in English" in text
    assert "<" not in text and "&amp;" not in text and "Zürich" in text


def test_html_to_text_details():
    assert html_to_text("a<br/>b<BR>c") == "a\nb\nc"
    assert html_to_text("x&nbsp;y &lt;tag&gt;") == "x y <tag>"
    assert html_to_text("<p>one</p><p>two</p>") == "one\n\ntwo"


def test_short_and_duplicate_fields_are_dropped():
    sections = [("Note", "too short"), ("Key responsibilities", "x" * 80), ("Key responsibilities", "x" * 80)]
    assert [h for h, _ in parse_job_page(make_page(sections=sections)).sections] == ["Key responsibilities"]


@pytest.mark.parametrize("page, message", [
    ("<html><body>no data here</body></html>", "no job data block"),
    ('<input id="preLoadJSON" value="not json">', "not valid JSON"),
    (make_page(sections=[]), "no posting text"),
    (make_page(sections=[("About us", "x" * 200)]), "no posting text"),
])
def test_unreadable_pages(page, message):
    with pytest.raises(PageError, match=message):
        parse_job_page(page)


# --- the switch --------------------------------------------------------------------------------------

def test_fetching_is_off_by_default(conn):
    cfg = load_config()
    with pytest.raises(FetchDisabled, match="switched off"):
        require_enabled(cfg)
    with pytest.raises(FetchDisabled):
        fetch_descriptions(conn, cfg, FakePages())


def test_enabling_needs_the_basis_to_be_written_down(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("descriptions:\n  fetch_enabled: true\n  permission_note: ''\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="permission_note"):
        load_config(p)
    p.write_text("descriptions:\n  fetch_enabled: true\n  permission_note: 'UBS written permission, 31 Oct 2026'\n", encoding="utf-8")
    assert load_config(p)["descriptions"]["fetch_enabled"] is True


@pytest.mark.parametrize("text, message", [("descriptions:\n  delay_seconds: 0\n", "delay_seconds"),
                                            ("descriptions:\n  daily_cap: 0\n", "daily_cap"),
                                            ("descriptions:\n  daily_cap: 500\n", "daily_cap"),
                                            ("descriptions:\n  user_agent: ''\n", "user_agent")])
def test_bad_description_settings(tmp_path, text, message):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(p)


# --- fetching ------------------------------------------------------------------------------------------

def test_fetch_stores_the_text_and_records_each_page(conn, cfg):
    load_alerts(conn, filtered=True, cfg=cfg)
    fetch, waits = FakePages(), []
    s = fetch_descriptions(conn, cfg, fetch, sleep=waits.append, now=NOW)
    assert (s.fetched, s.failed, s.attempted) == (7, 0, 7) and s.stopped is None
    assert all(u.startswith("https://jobs.ubs.com/") and "jobid=" in u for u in fetch.calls)
    assert waits == [5] * 6                                          # a pause between requests, none before the first
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE description_missing = 1").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM description_fetches WHERE status = 'ok'").fetchone()[0] == 7
    log = conn.execute("SELECT DISTINCT source FROM description_log").fetchall()
    assert [r[0] for r in log] == ["ubs_page"]


def test_each_page_is_requested_only_once(conn, cfg):
    load_alerts(conn)
    fetch_descriptions(conn, cfg, FakePages(), sleep=lambda s: None, now=NOW)
    again = FakePages()
    s = fetch_descriptions(conn, cfg, again, sleep=lambda s: None, now=NOW)
    assert again.calls == [] and s.fetched == 0 and eligible_jobs(conn) == []


def test_unfiltered_jobs_only_get_the_text_and_the_filters_see_it_later(conn, cfg):
    load_alerts(conn, names=["alert_2026-10-30.html"])                          # not filtered yet
    pages = {u: make_page(jid(u), sections=SECTIONS[:2] + [("Your skills and experience", "Fluent German (C1) is a must, written and spoken, for this role in Zürich.")])
             for u in [r["url"] for r in eligible_jobs(conn)]}
    s = fetch_descriptions(conn, cfg, FakePages(pages), sleep=lambda s: None, now=NOW)
    assert s.fetched == 4
    assert conn.execute("SELECT COUNT(*) FROM job_filter_results").fetchone()[0] == 0     # nothing was filtered by the fetch itself
    counts = filter_new_jobs(conn, cfg, "run1")
    assert counts["fail"] == 4                                                # the language rule now sees C1 and rejects
    assert {r[0] for r in conn.execute("SELECT reason_code FROM job_archive")} == {"language_c1_required"}


def test_filtered_scored_jobs_are_checked_and_marked_for_scoring_again(conn, cfg):
    load_alerts(conn, names=["alert_2026-10-28.html"], filtered=True, cfg=cfg)
    jid = conn.execute("SELECT id FROM jobs").fetchone()[0]
    with conn:
        conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (jid,))
    fetch_descriptions(conn, cfg, FakePages(), sleep=lambda s: None, now=NOW)
    assert conn.execute("SELECT needs_rescore FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_filter_results WHERE filter = 'language'").fetchone()[0] == 2


def test_daily_cap_and_the_next_day(conn, cfg):
    cfg["descriptions"]["daily_cap"] = 3
    load_alerts(conn)
    s = fetch_descriptions(conn, cfg, FakePages(), sleep=lambda x: None, now=NOW)
    assert (s.fetched, s.remaining_today) == (3, 0) and "daily cap of 3" in s.stopped and "waits" not in s.stopped
    same_day = FakePages()
    assert fetch_descriptions(conn, cfg, same_day, sleep=lambda x: None, now=NOW + timedelta(hours=5)).fetched == 0 and same_day.calls == []
    next_day = fetch_descriptions(conn, cfg, FakePages(), sleep=lambda x: None, now=NOW + timedelta(days=1))
    assert next_day.fetched == 3


def test_limit(conn, cfg):
    load_alerts(conn)
    assert fetch_descriptions(conn, cfg, FakePages(), sleep=lambda s: None, limit=2, now=NOW).fetched == 2


@pytest.mark.parametrize("failure", [FetchError("HTTP 403", 403), FetchError("HTTP 429", 429), FetchError("HTTP 503", 503),
                                      FetchError("network error: TimeoutError")])
def test_a_refusal_stops_the_run_at_once(conn, cfg, failure):
    load_alerts(conn)
    urls = [r["url"] for r in eligible_jobs(conn)]
    fetch = FakePages(fail={urls[1]: failure})
    s = fetch_descriptions(conn, cfg, fetch, sleep=lambda x: None, now=NOW)
    assert len(fetch.calls) == 2 and s.fetched == 1 and s.failed == 1 and "nothing more is requested" in s.stopped
    rows = conn.execute("SELECT status, http_status FROM description_fetches ORDER BY job_id").fetchall()
    assert [r["status"] for r in rows] == ["ok", "error"]


def test_a_page_that_cannot_be_read_is_recorded_and_the_run_goes_on(conn, cfg):
    load_alerts(conn, names=["alert_2026-10-30.html"])
    urls = [r["url"] for r in eligible_jobs(conn)]
    fetch = FakePages(pages={urls[0]: "<html>layout changed</html>", urls[1]: make_page("999999")})       # unreadable, and a different job
    s = fetch_descriptions(conn, cfg, fetch, sleep=lambda x: None, now=NOW)
    assert s.fetched == 2 and s.failed == 2 and s.stopped is None and len(fetch.calls) == 4
    errors = [r[0] for r in conn.execute("SELECT error FROM description_fetches WHERE status = 'error' ORDER BY job_id")]
    assert "no job data block" in errors[0] and "is for job 999999" in errors[1]


def test_retry_fetches_failed_pages_only(conn, cfg):
    load_alerts(conn, names=["alert_2026-10-28.html"])
    url = eligible_jobs(conn)[0]["url"]
    fetch_descriptions(conn, cfg, FakePages(fail={url: FetchError("HTTP 503", 503)}), sleep=lambda x: None, now=NOW)
    assert eligible_jobs(conn) == [] and len(eligible_jobs(conn, retry=True)) == 1
    s = fetch_descriptions(conn, cfg, FakePages(), sleep=lambda x: None, retry=True, now=NOW + timedelta(days=1))
    assert s.fetched == 1 and eligible_jobs(conn, retry=True) == []


def test_only_ubs_alert_jobs_on_the_ubs_job_site_are_fetched(conn, cfg):
    ingest_job(conn, NormalizedJob(source="other", external_id="1", title="A", company="B", url="https://example.com/job/1", locations=["Zürich"]))
    load_alerts(conn, names=["alert_2026-10-28.html"])
    assert len(eligible_jobs(conn)) == 1                                       # the job from another source is not eligible
    with conn:
        conn.execute("UPDATE job_sources SET url = 'https://evil.example.com/x?jobid=351249' WHERE source = 'ubs_alert'")
    fetch = FakePages()
    s = fetch_descriptions(conn, cfg, fetch, sleep=lambda x: None, now=NOW)
    assert fetch.calls == [] and s.fetched == 0 and "not a page on jobs.ubs.com" in s.items[0][1]


# --- the real fetcher ------------------------------------------------------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_page_fetcher_sends_an_honest_user_agent(monkeypatch):
    seen = {}

    def fake_open(self, request, data=None, timeout=None):
        seen.update(ua=request.get_header("User-agent"), timeout=timeout, url=request.full_url)
        return _Resp("<html>Zürich</html>".encode("utf-8"))
    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)
    assert PageFetcher("Test-UA/1", timeout=9)("https://jobs.ubs.com/x") == "<html>Zürich</html>"
    assert seen == {"ua": "Test-UA/1", "timeout": 9, "url": "https://jobs.ubs.com/x"}


@pytest.mark.parametrize("failure, status", [(urllib.error.HTTPError("u", 403, "Forbidden", {}, None), 403),
                                              (urllib.error.HTTPError("u", 429, "Too Many", {}, None), 429),
                                              (urllib.error.URLError("down"), None), (TimeoutError("slow"), None)])
def test_page_fetcher_errors_become_fetch_errors_without_retry(monkeypatch, failure, status):
    calls = []

    def boom(self, request, data=None, timeout=None):
        calls.append(1)
        raise failure
    monkeypatch.setattr("urllib.request.OpenerDirector.open", boom)
    with pytest.raises(FetchError) as exc:
        PageFetcher("UA")("https://jobs.ubs.com/x")
    assert exc.value.http_status == status and len(calls) == 1


def test_page_fetcher_refuses_a_huge_page(monkeypatch):
    monkeypatch.setattr(descriptions_fetch, "MAX_PAGE_BYTES", 100)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", lambda self, r, data=None, timeout=None: _Resp(b"x" * 500))
    with pytest.raises(FetchError, match="larger than the limit"):
        PageFetcher("UA")("https://jobs.ubs.com/x")


# --- inside prepare ------------------------------------------------------------------------------------

@pytest.fixture
def providers(cfg):
    replay = Replay()
    p = cfg["location"]["providers"]
    return Providers(TransportProvider(replay, p["transport_url"]), OsrmProvider(replay, p["osrm_url"]), today=TODAY)


def run_prepare(conn, cfg, providers, fetch, run_id="run1"):
    return prepare(conn, cfg, providers=providers, prompt=("p", "v1"), mail_client_factory=lambda c: FakeIMAP(four_alerts()),
                   page_fetcher=fetch, page_sleep=lambda s: None, run_id=run_id, now=NOW)


def test_prepare_fetches_before_the_filters_so_the_batch_has_descriptions(conn, cfg, providers):
    fetch = FakePages()
    r = run_prepare(conn, cfg, providers, fetch)
    assert len(fetch.calls) == 9 and r.filters == {"pass": 7, "conditional": 0, "fail": 2}      # all 9 distinct jobs fetched
    doc = json.loads(r.batch.path.read_text(encoding="utf-8"))
    assert all(not j["description_missing"] and "analyse reporting requirements" in j["description"] for j in doc["jobs"])
    row = conn.execute("SELECT adapter, found, error FROM adapter_runs WHERE adapter = 'UBS job pages'").fetchone()
    assert tuple(row) == ("UBS job pages", 9, None)


def test_prepare_does_not_fetch_when_it_is_off(conn, cfg, providers):
    cfg["descriptions"]["fetch_enabled"] = False
    fetch = FakePages()
    r = run_prepare(conn, cfg, providers, fetch)
    assert fetch.calls == [] and r.batch.job_count == 7
    assert conn.execute("SELECT COUNT(*) FROM adapter_runs WHERE adapter = 'UBS job pages'").fetchone()[0] == 0


def test_prepare_survives_a_refusal_and_says_so(conn, cfg, providers):
    first_url = None

    class Refusing(FakePages):
        def __call__(self, url):
            self.calls.append(url)
            raise FetchError("HTTP 429", 429)
    fetch = Refusing()
    r = run_prepare(conn, cfg, providers, fetch)
    assert len(fetch.calls) == 1 and any("nothing more is requested" in w for w in r.warnings)
    assert r.batch is not None and r.batch.job_count == 7                       # the batch is written without the descriptions
    assert "HTTP 429" in conn.execute("SELECT error FROM adapter_runs WHERE adapter = 'UBS job pages'").fetchone()[0]


def test_prepare_with_a_c1_posting_rejects_the_job_in_the_filters(conn, cfg, providers):
    c1 = make_page(sections=[("Key responsibilities", "You analyse requirements for the trading desks and write clear specifications for the developers every week."),
                             ("Your skills and experience", "Business fluent German (C1) and English, written and spoken, is a must.")])

    class Mixed(FakePages):
        def __call__(self, url):
            self.calls.append(url)
            return c1.replace("351249", jid(url)) if jid(url) == "351249" else make_page(jid(url))
    r = run_prepare(conn, cfg, providers, Mixed())
    assert r.filters["fail"] == 3                                               # 2 seniority rejections + the C1 posting
    assert "language_c1_required" in {x[0] for x in conn.execute("SELECT reason_code FROM job_archive")}


def _fake_http(monkeypatch, routes):
    """Replaces the socket layer of urllib's HTTP handler: `routes` maps a URL to (status, Location or body)."""
    import email.message
    import urllib.response
    seen = []

    def http_open(self, req):
        seen.append(req.full_url)
        code, payload = routes[req.full_url]
        headers = email.message.Message()
        if code == 302:
            headers["Location"] = payload
        resp = urllib.response.addinfourl(io.BytesIO(b"" if code == 302 else payload), headers, req.full_url, code)
        resp.msg = "Found" if code == 302 else "OK"
        return resp
    monkeypatch.setattr("urllib.request.HTTPHandler.http_open", http_open)
    monkeypatch.setattr("urllib.request.HTTPSHandler.https_open", http_open)
    # code that still calls urlopen runs through the same fake handlers instead of stopping at the conftest.py block
    monkeypatch.setattr("urllib.request.urlopen", lambda r, timeout=None: urllib.request.build_opener().open(r, timeout=timeout))
    return seen


def test_redirect_to_other_host_is_refused(monkeypatch):
    seen = _fake_http(monkeypatch, {"http://jobs.ubs.test/job": (302, "http://login.other.test/sso"),
                                    "http://login.other.test/sso": (200, b"other host")})
    with pytest.raises(FetchError):
        PageFetcher("UA/1", timeout=5, allowed_host="jobs.ubs.test")("http://jobs.ubs.test/job")
    assert seen == ["http://jobs.ubs.test/job"]


def test_redirect_on_the_same_host_is_followed(monkeypatch):
    seen = _fake_http(monkeypatch, {"http://jobs.ubs.test/job": (302, "http://jobs.ubs.test/moved"),
                                    "http://jobs.ubs.test/moved": (200, b"ok")})
    assert PageFetcher("UA/1", timeout=5, allowed_host="jobs.ubs.test")("http://jobs.ubs.test/job") == "ok"
    assert seen == ["http://jobs.ubs.test/job", "http://jobs.ubs.test/moved"]

def _fake_http_cookies(monkeypatch, routes):
    """Like _fake_http, but records (URL, Cookie header) and lets a route raise or answer with a custom body reader."""
    import email.message
    import urllib.response
    seen = []

    def http_open(self, req):
        seen.append((req.full_url, req.get_header("Cookie")))
        code, payload = routes[req.full_url]
        headers = email.message.Message()
        if code == 302:
            headers["Location"] = payload
        resp = urllib.response.addinfourl(io.BytesIO(b"" if code == 302 else payload), headers, req.full_url, code)
        resp.msg = "Found" if code == 302 else "OK"
        return resp
    monkeypatch.setattr("urllib.request.HTTPHandler.http_open", http_open)
    monkeypatch.setattr("urllib.request.HTTPSHandler.https_open", http_open)
    # code that still calls urlopen runs through the same fake handlers instead of stopping at the conftest.py block
    monkeypatch.setattr("urllib.request.urlopen", lambda r, timeout=None: urllib.request.build_opener().open(r, timeout=timeout))
    return seen


def test_redirect_to_other_host_is_refused_without_allowed_host(monkeypatch):
    seen = _fake_http(monkeypatch, {"http://a.test/job": (302, "http://b.test/x"), "http://b.test/x": (200, b"other host")})
    with pytest.raises(FetchError) as info:
        PageFetcher("UA/1", timeout=5)("http://a.test/job")
    assert info.value.http_status == 302
    assert seen == ["http://a.test/job"]


def test_redirect_from_http_to_https_is_refused_without_allowed_host(monkeypatch):
    seen = _fake_http(monkeypatch, {"http://a.test/job": (302, "https://a.test/job")})
    with pytest.raises(FetchError):
        PageFetcher("UA/1", timeout=5)("http://a.test/job")
    assert seen == ["http://a.test/job"]


def test_a_cookie_never_reaches_another_host_on_a_redirect(monkeypatch):
    seen = _fake_http_cookies(monkeypatch, {"http://a.test/job": (302, "http://b.test/x"), "http://b.test/x": (200, b"other host")})
    fetcher = PageFetcher("UA/1", timeout=5)
    fetcher.cookies["a.test"] = {"sid": "secret"}
    with pytest.raises(FetchError):
        fetcher("http://a.test/job")
    assert seen == [("http://a.test/job", "sid=secret")]


def test_same_host_redirect_is_followed_without_allowed_host_guard(monkeypatch):
    seen = _fake_http(monkeypatch, {"http://a.test/job": (302, "http://a.test/moved"), "http://a.test/moved": (200, b"ok")})
    assert PageFetcher("UA/1", timeout=5)("http://a.test/job") == "ok"
    assert seen == ["http://a.test/job", "http://a.test/moved"]


def test_a_same_host_redirect_to_a_path_robots_disallows_is_refused(monkeypatch):
    seen = _fake_http(monkeypatch, {"http://a.test/job": (302, "http://a.test/private/x"), "http://a.test/private/x": (200, b"no")})
    fetcher = PageFetcher("UA/1", timeout=5)
    fetcher.path_allowed = lambda url: "/private/" not in url
    with pytest.raises(FetchError, match="robots.txt"):
        fetcher("http://a.test/job")
    assert seen == ["http://a.test/job"]


def test_a_shared_fetcher_serves_the_adapter_wrapper_after_a_resolver_session_was_built(monkeypatch, cfg):
    from app.adapters import _Paced
    from app.resolver import _Session
    seen = _fake_http(monkeypatch, {"https://ad.test/robots.txt": (200, b"User-agent: *\nDisallow: /private/"),
                                    "https://ad.test/list": (302, "https://ad.test/moved"), "https://ad.test/moved": (200, b"ok"),
                                    "https://ad.test/list2": (302, "https://ad.test/private/x"), "https://ad.test/private/x": (200, b"no")})
    fetcher = PageFetcher("UA/1", timeout=5)
    paced = _Paced(cfg, type("Spec", (), {"host": "ad.test"})(), fetcher, lambda s: None)
    _Session(cfg, fetcher, lambda s: None, {})                                  # built last: must not take over the adapter's rules
    assert paced("https://ad.test/list") == "ok"
    with pytest.raises(FetchError, match="robots.txt"):                          # the adapter's own rules still apply
        paced("https://ad.test/list2")
    assert fetcher.path_allowed is None and "https://ad.test/private/x" not in seen


def test_a_cookie_is_stored_for_the_host_that_set_it_after_a_robots_redirect(monkeypatch):
    import email.message
    import urllib.response

    def http_open(self, req):
        headers = email.message.Message()
        if req.full_url == "http://a.test/robots.txt":
            headers["Location"] = "http://www.a.test/robots.txt"
            code = 302
        else:
            headers["Set-Cookie"] = "sid=abc; path=/"
            code = 200
        resp = urllib.response.addinfourl(io.BytesIO(b"" if code == 302 else b"ok"), headers, req.full_url, code)
        resp.msg = "Found" if code == 302 else "OK"
        return resp
    monkeypatch.setattr("urllib.request.HTTPHandler.http_open", http_open)
    monkeypatch.setattr("urllib.request.urlopen", lambda r, timeout=None: urllib.request.build_opener().open(r, timeout=timeout))
    fetcher = PageFetcher("UA/1", timeout=5)
    assert fetcher("http://a.test/robots.txt") == "ok"
    assert fetcher.cookies == {"www.a.test": {"sid": "abc"}}


def test_a_robots_url_with_a_query_is_a_page_and_does_not_cross_hosts(monkeypatch):
    seen = _fake_http(monkeypatch, {"http://a.test/robots.txt?x=1": (302, "http://b.test/robots.txt")})
    with pytest.raises(FetchError):
        PageFetcher("UA/1", timeout=5)("http://a.test/robots.txt?x=1")
    assert seen == ["http://a.test/robots.txt?x=1"]


def test_robots_txt_follows_a_cross_host_redirect_without_the_cookie(monkeypatch):
    seen = _fake_http_cookies(monkeypatch, {"http://a.test/robots.txt": (302, "http://www.a.test/robots.txt"),
                                            "http://www.a.test/robots.txt": (200, b"User-agent: *\nAllow: /")})
    fetcher = PageFetcher("UA/1", timeout=5)
    fetcher.cookies["a.test"] = {"sid": "secret"}
    assert "Allow" in fetcher("http://a.test/robots.txt")
    assert seen == [("http://a.test/robots.txt", "sid=secret"), ("http://www.a.test/robots.txt", None)]


def test_robots_txt_redirects_are_limited_to_five_hops(monkeypatch):
    routes = {f"http://a.test/robots{i}": (302, f"http://a.test/robots{i + 1}") for i in range(1, 12)}
    routes["http://a.test/robots.txt"] = (302, "http://a.test/robots1")
    seen = _fake_http(monkeypatch, routes)
    with pytest.raises(FetchError):
        PageFetcher("UA/1", timeout=5)("http://a.test/robots.txt")
    assert len(seen) == 6                                                       # the request plus five followed redirects


def test_a_truncated_body_is_a_network_error(monkeypatch):
    import http.client

    class Truncated(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b"par", 10)
    import email.message
    import urllib.response

    def http_open(self, req):
        resp = urllib.response.addinfourl(Truncated(b""), email.message.Message(), req.full_url, 200)
        resp.msg = "OK"
        return resp
    monkeypatch.setattr("urllib.request.HTTPHandler.http_open", http_open)
    monkeypatch.setattr("urllib.request.urlopen", lambda r, timeout=None: urllib.request.build_opener().open(r, timeout=timeout))
    with pytest.raises(FetchError, match="^network error") as info:
        PageFetcher("UA/1", timeout=5)("http://a.test/job")
    assert info.value.http_status is None


def test_a_truncated_page_is_recorded_and_stops_the_run(conn, cfg, monkeypatch):
    import email.message
    import http.client
    import urllib.response

    class Truncated(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b"par", 10)
    calls = []

    def https_open(self, req):
        calls.append(req.full_url)
        resp = urllib.response.addinfourl(Truncated(b""), email.message.Message(), req.full_url, 200)
        resp.msg = "OK"
        return resp
    monkeypatch.setattr("urllib.request.HTTPSHandler.https_open", https_open)
    load_alerts(conn)
    s = fetch_descriptions(conn, cfg, PageFetcher("UA/1", timeout=5, allowed_host="jobs.ubs.com"), sleep=lambda x: None, now=NOW)
    assert len(calls) == 1 and s.failed == 1 and "network error" in s.stopped
    rows = conn.execute("SELECT status, http_status, error FROM description_fetches").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "error" and rows[0]["error"].startswith("network error")
