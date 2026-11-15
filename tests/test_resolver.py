"""The description resolver (goal.md 5.1a): robots.txt, completeness, matching, the platforms (on trimmed real samples in
tests/fixtures/resolver), tiers, limits, the migration, the config, the pipeline step and the command. No network."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cli, db, resolver
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.descriptions_fetch import FetchError
from app.models import NormalizedJob
from app.pipeline import prepare
from app.resolver import jibe, jobcloud, liebherr, refline
from app.resolver.common import Expired, PlatformError
from app.resolver.complete import is_complete
from app.resolver.jsonld import extract_job_posting
from app.resolver.match import Candidate, choose, normalise_title, title_score
from app.resolver.robots import Robots
from tests.test_location import Replay, TODAY
from tests.test_mailbox import FakeIMAP
from app.travel import OsrmProvider, Providers, TransportProvider

FIX = Path(__file__).parent / "fixtures" / "resolver"
AXA_JSON = (FIX / "axa_api.json").read_text(encoding="utf-8")
RL_LIST = (FIX / "refline_list.html").read_text(encoding="utf-8")
RL_JOB = (FIX / "refline_job.html").read_text(encoding="utf-8")
LH_SEARCH = (FIX / "liebherr_search.html").read_text(encoding="utf-8")
LH_JOB = (FIX / "liebherr_job.html").read_text(encoding="utf-8")
LH_ROBOTS = "User-agent: *\nDisallow: /wip/\nDisallow: /_wip/\n"
LH_TITLE = "Business Process Owner (BPO) Customer Service (m/f/d)"
NOW = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
AXA_TITLE = "Business Analyst Claims Smart Automation (all genders)"
AXA_ROBOTS = "User-agent: *\nAllow: /\nCrawl-delay: 5\n"
ZKB_ROBOTS = "User-agent: *\nAllow: /\n"
UA = "Recruitment-pipeline/test (personal job-search tool)"


class Site:
    """A fake fetcher: url (or url prefix) -> text or an exception; every call is recorded."""

    def __init__(self, pages=None):
        self.pages, self.calls = dict(pages or {}), []

    def __call__(self, url, data=None):
        self.calls.append(url)
        for key, value in self.pages.items():
            if url == key or (key.endswith("*") and url.startswith(key[:-1])):
                if isinstance(value, Exception):
                    raise value
                return value
        raise FetchError("HTTP 404", 404)

    def requests(self, host):
        return [u for u in self.calls if host in u and not u.endswith("robots.txt")]


def axa_site(**extra):
    return Site({"https://careers.axa.com/robots.txt": AXA_ROBOTS, "https://careers.axa.com/api/jobs?*": AXA_JSON, **extra})


def zkb_site(**extra):
    return Site({"https://apply.refline.ch/robots.txt": ZKB_ROBOTS, "https://apply.refline.ch/792841/search.html": RL_LIST,
                 "https://apply.refline.ch/792841/11149/pub/1": RL_JOB, **extra})


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x-test-pass")
    c = load_config()
    c["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    c["resolver"].update(enabled=True, accept_t2_risk=True)
    for board in ("jobcloud", "jobup"):                                 # the job boards are tried for every company: tests that are not about them leave them out
        c["resolver"]["sources"].pop(board, None)
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.start_run(c, "run1")
    yield c
    c.close()


def add_job(conn, title=AXA_TITLE, company="AXA", places=("Winterthur, Zurich, Switzerland",), ext="189"):
    return ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id=ext, title=title, company=company,
                                          url=f"https://www.linkedin.com/jobs/view/{ext}/", locations=list(places)), "run1").job_id


def legacy_key(conn, job_id):
    """Give the job a key that differs from the current one, so a pair that is a real duplicate stays two rows."""
    conn.execute("UPDATE jobs SET dedup_key = 'legacy|' || dedup_key WHERE id = ?", (job_id,))
    conn.commit()
    return job_id


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM description_resolutions ORDER BY id")]


def resolve(conn, cfg, fetch, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("sleep", lambda s: None)
    return resolver.resolve_descriptions(conn, cfg, fetch, **kw)


# --- robots.txt ------------------------------------------------------------------------------------------

def test_robots_allow_deny_and_crawl_delay():
    fetch = Site({"https://h.example/robots.txt": "User-agent: *\nDisallow: /private/\nCrawl-delay: 7\n"})
    robots = Robots(fetch, UA)
    assert robots.allowed("https://h.example/jobs/1") and not robots.allowed("https://h.example/private/x")
    assert robots.crawl_delay("h.example") == 7.0
    assert fetch.calls == ["https://h.example/robots.txt"]                      # read once per host


def test_no_robots_txt_means_allowed():
    robots = Robots(Site(), UA)                                                  # 404
    assert robots.allowed("https://h.example/anything") and robots.crawl_delay("h.example") == 0.0
    assert robots.refusal("h.example") is None


@pytest.mark.parametrize("failure", [FetchError("HTTP 403", 403), FetchError("HTTP 503", 503), FetchError("network error: URLError")])
def test_an_unreadable_robots_txt_means_not_allowed(failure):
    robots = Robots(Site({"https://h.example/robots.txt": failure}), UA)
    assert not robots.allowed("https://h.example/jobs/1")
    assert robots.refusal("h.example")[0] == failure.http_status


@pytest.mark.parametrize("status", [400, 401, 405, 451])
def test_a_robots_txt_answering_4xx_means_no_rules(status):
    """RFC 9309 2.3.1.3 (4 Nov 2026): a robots.txt that answers 4xx (401 included) means there are no rules."""
    robots = Robots(Site({"https://h.example/robots.txt": FetchError(f"HTTP {status}", status)}), UA)
    assert robots.allowed("https://h.example/jobs/1") and robots.crawl_delay("h.example") == 0.0
    assert robots.refusal("h.example") is None


@pytest.mark.parametrize("status", [403, 429])
def test_a_robots_txt_answering_403_or_429_still_refuses(status):
    robots = Robots(Site({"https://h.example/robots.txt": FetchError(f"HTTP {status}", status)}), UA)
    assert not robots.allowed("https://h.example/jobs/1") and robots.refusal("h.example")[0] == status


def test_a_named_source_with_a_401_robots_txt_is_asked(conn, cfg):
    add_job(conn, title=AXA_TITLE)
    fetch = axa_site(**{"https://careers.axa.com/robots.txt": FetchError("HTTP 401", 401)})
    s = resolve(conn, cfg, fetch)
    assert s.stopped is None and s.resolved == 1 and fetch.requests("careers.axa.com")


# --- completeness -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("tasks, reqs", [
    ("YOUR CHALLENGE", "YOUR PROFILE"),
    ("How You'll Make An Impact", "Your background"),
    ("Was ist dein Gestaltungsraum?", "Was bringst du mit?"),
    ("Was ist der Inhalt der Rolle?", "Was du mitbringst"),
    ("Position Description", "Candidate Profile"),
])
def test_more_real_headings_are_recognised(tasks, reqs):
    body = "Neutral filler sentence about the firm. " * 40
    assert is_complete(chr(10).join([tasks, body, reqs, body]))


def test_a_long_text_with_tasks_and_requirements_is_complete():
    text = "Your tasks\n" + "We build things. " * 100 + "\nYour profile\nSQL and good English."
    assert len(text) > 1500 and is_complete(text)
    assert is_complete(("Aufgaben: Berichte erstellen. " * 30) + "Anforderungen: Erfahrung mit SQL. " * 20)
    assert is_complete(("Vos missions: analyser. " * 40) + "Votre profil: expérience en banque. " * 20)


def test_a_short_text_or_one_without_both_parts_is_partial():
    assert not is_complete("Your tasks and your profile. " * 10)                        # under 1,500 characters
    assert not is_complete("Our company is great and old. " * 80 + " Your tasks: many.")   # no requirements part
    assert not is_complete("Our company is great and old. " * 80 + " Your profile: you.")  # no tasks part


def test_the_real_samples_are_complete():
    axa = json.loads(AXA_JSON)["jobs"][0]["data"]["description"]
    assert len(axa) == 6332 and is_complete(axa)
    zkb = extract_job_posting(RL_JOB)
    assert zkb.title.startswith("Enterprise Architect") and zkb.location == "Zürich" and is_complete(zkb.description)
    assert "<" not in zkb.description and "Deine Aufgaben" in zkb.description


# --- JSON-LD ----------------------------------------------------------------------------------------------

def test_json_ld_is_found_in_a_graph_and_in_a_list():
    desc = "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;one&lt;/li&gt;&lt;/ul&gt;"
    page = ('<script type="application/ld+json">' + json.dumps({"@graph": [{"@type": "WebSite"}, {
        "@type": ["JobPosting"], "title": "Analyst", "description": desc.replace("&amp;amp;", "and"),
        "jobLocation": [{"address": {"addressLocality": "Bern"}}, {"address": {"addressLocality": "Basel"}}]}]}) + "</script>")
    data = extract_job_posting(page)
    assert (data.title, data.location) == ("Analyst", "Bern, Basel") and data.description == "Hello and welcome\n\n• one"
    assert extract_job_posting('<script type="application/ld+json">[{"@type":"Organization"}]</script>') is None
    assert extract_job_posting('<script type="application/ld+json">{broken</script>') is None


# --- matching ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, clean", [
    ("Business Analyst (all genders)", "business analyst"),
    ("Projektleiter (m/w/d) 80-100%", "projektleiter"),
    ("Senior Engineer (m/f/d) - 100%", "senior engineer"),
    ("Data Analyst 80 % - 100 %", "data analyst"),
    ("Kundenberater:in Privatkunden", "kundenberater privatkunden"),
    ("Leiter Büro Zürich", "leiter buro zurich"),
    ("Business Analyst (f/m/n)", "business analyst"),
    ("Business Analyst (H/F/X)", "business analyst"),
    ("Business Analyst 100% (a)", "business analyst"),
    ("Spezialist/-in Risiko", "spezialist risiko"),
    ("Spezialist(in) Risiko", "spezialist risiko"),
    ("Business Analyst (m,w,d)", "business analyst"),
    ("Business Analyst (w/m/div)", "business analyst"),
    ("Business Analyst (m/w/divers)", "business analyst"),
    ("Business Analyst (gn*)", "business analyst"),
    ("Business Analyst f/m/n", "business analyst"),
    ("Business Analyst m/f", "business analyst"),
    ("Business Analyst (alle)", "business analyst"),
    ("Business Analyst 60-80 Prozent", "business analyst"),
    ("Conseiller(ère) clientèle", "conseiller clientele"),
    ("Data (AI) Lead", "data ai lead"),
    ("BI/DWH Engineer", "bi dwh engineer"),
    ("M&A Analyst", "m a analyst"),
    ("LinkedIn Manager", "linkedin manager"),
    ("Head (Interim)", "head interim"),
])
def test_titles_are_normalised(raw, clean):
    assert normalise_title(raw) == clean


def cand(pid, title, city="Zürich"):
    return Candidate(pid, title, city, f"https://x.example/{pid}")


def test_a_unique_close_title_is_accepted_with_evidence():
    r = choose("Business Analyst Claims Smart Automation", ["Winterthur"],
               [cand("1", AXA_TITLE, "WINTERTHUR"), cand("2", "Claims Services Associate", "WINTERTHUR")])
    assert r.status == "match" and r.candidate.posting_id == "1" and r.score >= 0.85 and r.score - r.runner_up >= 0.10


def test_two_equally_close_postings_are_ambiguous():
    r = choose("Kundenberater Privatkunden", ["Zürich"], [cand("1", "Kundenberater:in Privatkunden"), cand("2", "Kundenberater:in Privatkunden")])
    assert r.status == "ambiguous" and r.runner_up == r.score
    assert choose("Kundenberater Privatkunden", [], [cand("1", "Kundenberater:in Privatkunden"),
                                                      cand("1", "Kundenberater:in Privatkunden")]).status == "match"     # one posting listed twice


def test_a_different_city_is_not_a_match_but_an_unknown_city_is_fine():
    c = [cand("1", "Data Analyst", "BERN")]
    assert choose("Data Analyst", ["Zurich"], c).status == "none"
    assert choose("Data Analyst", [], c).status == "match"
    assert choose("Data Analyst", ["Bern, Switzerland"], c).status == "match"
    assert choose("Data Analyst", ["Zurich"], [cand("1", "Data Analyst", None)]).status == "match"
    assert choose("Zürich Analyst", ["Zurich"], [cand("1", "Zurich Analyst", "Zürich")]).status == "match"        # umlaut folding


def test_a_weak_title_is_no_match():
    r = choose("Business Analyst", [], [cand("1", "Senior Software Engineer Backend")])
    assert r.status == "none" and r.score < 0.85
    assert title_score("Kundenberater Privatkunden", "Kundenberater Firmenkunden") < 0.85
    assert choose("Business Analyst", [], []).status == "none"


# --- the platforms, on the real samples --------------------------------------------------------------------

def test_jibe_lists_candidates_with_the_text_from_the_answer():
    fetch = axa_site()
    found = jibe.candidates(fetch, {"base_url": "https://careers.axa.com"}, AXA_TITLE)
    assert [c.posting_id for c in found] == ["27724", "26914", "29350"] and found[0].city == "WINTERTHUR"
    assert fetch.calls == ["https://careers.axa.com/api/jobs?keywords=business+analyst+claims+smart+automation&page=1"]
    assert found[0].url == "https://careers.axa.com/careers-home/jobs/27724?lang=en-us" and len(found[0].text) == 6332


def test_jibe_tries_fewer_words_when_nothing_is_found():
    empty = json.dumps({"jobs": []})
    calls = []

    def get(url):
        calls.append(url)
        return empty if len(calls) == 1 else AXA_JSON
    found = jibe.candidates(get, {"base_url": "https://careers.axa.com"}, AXA_TITLE)
    assert len(calls) == 2 and len(found) == 3 and "keywords=business+analyst+claims+smart+automation" in calls[0]


def test_jibe_rejects_an_answer_that_is_not_its_json():
    with pytest.raises(PlatformError):
        jibe.candidates(lambda u: "<html>blocked</html>", {"base_url": "https://careers.axa.com"}, AXA_TITLE)


def test_jibe_detail_reads_the_json_ld_of_the_job_page():
    page = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "T", "description": "<p>Your tasks</p>"}</script>')
    assert jibe.detail_text(lambda u: page, cand("1", "T")) == "Your tasks"
    assert jibe.detail_text(lambda u: "<html></html>", cand("1", "T")) is None


def test_refline_reads_the_list_page_rows():
    found = refline.candidates(lambda u: RL_LIST, {"base_url": "https://apply.refline.ch/792841"}, "x")
    assert len(found) == 5 and found[0].posting_id == "11149" and found[0].title == "Enterprise Architect AI & Data (m/w/d)"
    assert found[0].city == "Zürich" and found[0].url == "https://apply.refline.ch/792841/11149/pub/1" and found[0].text is None
    with pytest.raises(PlatformError):
        refline.candidates(lambda u: "<html>moved</html>", {"base_url": "https://apply.refline.ch/792841"}, "x")


# --- resolving ----------------------------------------------------------------------------------------------

def test_axa_jobs_189_and_192_resolve_to_the_same_posting_and_the_second_is_flagged(conn, cfg):
    a = legacy_key(conn, add_job(conn, company="AXA Switzerland", ext="189"))
    b = add_job(conn, company="AXA", ext="192")
    assert a != b
    fetch = axa_site()
    s = resolve(conn, cfg, fetch)
    assert s.resolved == 2 and s.stopped is None
    r = rows(conn)
    assert [(x["job_id"], x["status"], x["posting_id"], x["duplicate_of_job_id"]) for x in r] == [(b, "success", "27724", None), (a, "success", "27724", b)]
    assert r[0]["tier"] == "T2" and r[0]["platform"] == "jibe" and r[0]["source_key"] == "axa" and r[0]["confidence"] >= 0.85
    assert r[0]["url"] == "https://careers.axa.com/careers-home/jobs/27724?lang=en-us"
    assert len(fetch.requests("careers.axa.com")) == 1                              # one list answer served both jobs
    for job_id in (a, b):
        job = conn.execute("SELECT description, description_missing FROM jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["description_missing"] == 0 and "Claims-Management-System" in job["description"]
    assert [x["source"] for x in conn.execute("SELECT source FROM description_log ORDER BY id")] == ["resolver:axa"] * 2


def test_a_stored_description_marks_a_scored_job_for_rescoring(conn, cfg):
    job_id = add_job(conn)
    with conn:
        conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (job_id,))
    resolve(conn, cfg, axa_site())
    assert tuple(conn.execute("SELECT needs_rescore, description_missing FROM jobs WHERE id = ?", (job_id,)).fetchone()) == (1, 0)


def test_zkb_resolves_through_the_list_and_the_json_ld_page(conn, cfg):
    job_id = add_job(conn, title="Enterprise Architect AI & Data (m/w/d)", company="Zürcher Kantonalbank", places=("Zurich",), ext="300")
    fetch = zkb_site()
    s = resolve(conn, cfg, fetch)
    assert s.resolved == 1
    assert fetch.requests("refline.ch") == ["https://apply.refline.ch/792841/search.html", "https://apply.refline.ch/792841/11149/pub/1"]
    r = rows(conn)[0]
    assert (r["status"], r["posting_id"], r["source_key"], r["platform"]) == ("success", "11149", "zkb", "refline")
    assert "Deine Aufgaben" in conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def test_an_ambiguous_match_stores_nothing_but_the_attempt(conn, cfg):
    job_id = add_job(conn, title="Kundenberater Privatkunden", company="ZKB", places=(), ext="301")     # three branches list it
    fetch = zkb_site()
    s = resolve(conn, cfg, fetch)
    assert s.resolved == 0 and [x["status"] for x in rows(conn)] == ["ambiguous"]
    assert not any("/pub/" in u for u in fetch.calls)                              # no posting page was read
    assert conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == 1


def test_the_city_makes_the_same_title_unique(conn, cfg):
    add_job(conn, title="Kundenberater Privatkunden", company="ZKB", places=("Rüti ZH",), ext="302")
    fetch = zkb_site(**{"https://apply.refline.ch/792841/11232/pub/1": RL_JOB})
    resolve(conn, cfg, fetch)
    assert (rows(conn)[0]["status"], rows(conn)[0]["posting_id"]) == ("success", "11232")


def test_no_match_and_wrong_city_are_recorded(conn, cfg):
    add_job(conn, title="Quantum Chef", ext="1")
    add_job(conn, title=AXA_TITLE, places=("Bern",), ext="2", company="AXA Versicherungen")
    resolve(conn, cfg, axa_site())
    assert [x["status"] for x in rows(conn)] == ["no_match", "no_match"]


def test_a_short_posting_is_stored_as_partial_and_the_job_stays_open(conn, cfg):
    data = json.loads(AXA_JSON)
    data["jobs"][0]["data"]["description"] = "Short text about the role."
    job_id = add_job(conn)
    page = '<script type="application/ld+json">{"@type": "JobPosting", "title": "T", "description": "Short text about the role."}</script>'
    fetch = axa_site(**{"https://careers.axa.com/api/jobs?*": json.dumps(data),
                        "https://careers.axa.com/careers-home/jobs/27724?lang=en-us": page})
    s = resolve(conn, cfg, fetch)
    r = rows(conn)[0]
    assert s.resolved == 0 and r["status"] == "partial" and r["posting_id"] == "27724"
    assert conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == 1
    assert fetch.requests("careers.axa.com")[-1].startswith("https://careers.axa.com/careers-home/jobs/27724")   # the page was tried


def test_the_job_page_is_the_fallback_when_the_list_text_is_short(conn, cfg):
    data = json.loads(AXA_JSON)
    data["jobs"][0]["data"]["description"] = "Short."
    full = json.loads(AXA_JSON)["jobs"][0]["data"]["description"]
    page = '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": "T", "description": full}) + "</script>"
    add_job(conn)
    fetch = axa_site(**{"https://careers.axa.com/api/jobs?*": json.dumps(data), "https://careers.axa.com/careers-home/jobs/27724?lang=en-us": page})
    assert resolve(conn, cfg, fetch).resolved == 1


# --- tiers ----------------------------------------------------------------------------------------------------

def test_tier_t2_is_skipped_unless_the_risk_is_accepted(conn, cfg):
    add_job(conn)
    cfg["resolver"]["accept_t2_risk"] = False
    fetch = axa_site()
    s = resolve(conn, cfg, fetch)
    assert fetch.calls == [] and rows(conn) == [] and "accept_t2_risk" in s.items[0][1]
    cfg["resolver"]["accept_t2_risk"] = True
    assert resolve(conn, cfg, axa_site()).resolved == 1


def test_a_source_with_a_permission_note_is_tier_t1_and_needs_no_risk_switch(conn, cfg):
    add_job(conn)
    cfg["resolver"]["accept_t2_risk"] = False
    cfg["resolver"]["sources"]["axa"]["permission_note"] = "AXA HR, e-mail of 10 Nov 2026"
    assert resolve(conn, cfg, axa_site()).resolved == 1
    assert rows(conn)[0]["tier"] == "T1"


def test_a_company_that_is_not_a_source_is_left_alone(conn, cfg):
    add_job(conn, company="Some Other Bank")
    fetch = axa_site()
    resolve(conn, cfg, fetch)
    assert fetch.calls == [] and rows(conn) == []


def test_closed_and_described_jobs_are_not_eligible(conn, cfg):
    a, b = add_job(conn, ext="1"), add_job(conn, company="AXA Switzerland", ext="2")
    with conn:
        conn.execute("UPDATE jobs SET closed_at = '2026-11-01T00:00:00Z' WHERE id = ?", (a,))
        conn.execute("UPDATE jobs SET description = 'x', description_missing = 0 WHERE id = ?", (b,))
    fetch = axa_site()
    resolve(conn, cfg, fetch)
    assert fetch.calls == []


# --- the rules of every request ---------------------------------------------------------------------------------

def test_robots_txt_is_read_first_and_a_disallowed_page_is_not_requested(conn, cfg):
    add_job(conn)
    fetch = axa_site(**{"https://careers.axa.com/robots.txt": "User-agent: *\nDisallow: /api/\n"})
    s = resolve(conn, cfg, fetch)
    assert fetch.calls == ["https://careers.axa.com/robots.txt"] and s.stopped is None
    assert rows(conn)[0]["status"] == "refused" and "robots.txt" in rows(conn)[0]["error"]


def test_the_delay_is_at_least_the_crawl_delay_and_the_configured_delay(conn, cfg):
    add_job(conn, title=AXA_TITLE, ext="1")
    add_job(conn, title="Claims Services Associate", ext="2", places=("Hong Kong",))
    waits = []
    cfg["resolver"]["delay_seconds"] = 2
    resolve(conn, cfg, axa_site(), sleep=waits.append)
    assert waits and min(waits) == 5.0                                              # robots.txt says 5, the config 2
    cfg["resolver"]["delay_seconds"] = 9
    waits.clear()
    add_job(conn, title="Data Science Analyst", ext="3", places=("Hong Kong",))
    resolve(conn, cfg, axa_site(), sleep=waits.append, now=NOW + timedelta(days=1))
    assert waits and min(waits) == 9.0


@pytest.mark.parametrize("failure, status", [(FetchError("HTTP 429", 429), "refused"), (FetchError("HTTP 403", 403), "refused"),
                                             (FetchError("HTTP 503", 503), "refused"), (FetchError("network error: URLError"), "error")])
def test_the_run_stops_at_the_first_refusal(conn, cfg, failure, status):
    add_job(conn, ext="1")
    add_job(conn, company="AXA Switzerland", title="Process Engineer", places=("Bruxelles",), ext="2")
    fetch = axa_site(**{"https://careers.axa.com/api/jobs?*": failure})
    s = resolve(conn, cfg, fetch)
    assert s.stopped and "nothing more is requested" in s.stopped and s.resolved == 0
    assert len(fetch.requests("careers.axa.com")) == 1                              # the second job was never tried
    assert [x["status"] for x in rows(conn)] == [status]


@pytest.mark.parametrize("failure", [FetchError("HTTP 403", 403), FetchError("HTTP 500", 500)])
def test_a_forbidden_robots_txt_stops_the_run(conn, cfg, failure):
    add_job(conn)
    fetch = axa_site(**{"https://careers.axa.com/robots.txt": failure})
    s = resolve(conn, cfg, fetch)
    assert s.stopped and fetch.calls == ["https://careers.axa.com/robots.txt"]


def test_a_missing_page_is_an_error_of_that_job_only(conn, cfg):
    add_job(conn, ext="1")
    fetch = axa_site(**{"https://careers.axa.com/api/jobs?*": FetchError("HTTP 404", 404)})
    s = resolve(conn, cfg, fetch)
    assert s.stopped is None and rows(conn)[0]["status"] == "error"


def test_only_the_hosts_of_the_sources_are_requested(conn, cfg):
    cfg["resolver"]["sources"]["axa"]["list_url"] = "https://evil.example/api/jobs"
    add_job(conn)
    fetch = axa_site()
    resolve(conn, cfg, fetch)
    assert all("evil.example" not in u for u in fetch.calls) and rows(conn)[0]["status"] == "refused"


def test_the_daily_cap_per_host(conn, cfg):
    cfg["resolver"]["daily_cap_per_host"] = 2
    for i, place in enumerate(["Hong Kong", "Chicago", "Madrid"]):
        add_job(conn, title=["Claims Services Associate", "Assistant Operations Manager", "AI-Enhanced Penetration Tester"][i], places=(place,), ext=str(i))
    s = resolve(conn, cfg, axa_site())
    assert s.attempted == 2 and len(rows(conn)) == 2 and any("daily cap" in m for _, m in s.items)
    s = resolve(conn, cfg, axa_site())                                              # the same day: nothing more for AXA
    assert s.attempted == 0 and len(rows(conn)) == 2
    assert resolve(conn, cfg, axa_site(), now=NOW + timedelta(days=1)).attempted == 2        # a new day, the cap again


def test_a_job_is_given_up_after_the_attempt_days(conn, cfg):
    add_job(conn, title="Quantum Chef")
    for d in range(7):
        assert resolve(conn, cfg, axa_site(), now=NOW + timedelta(days=d)).attempted == 1
    fetch = axa_site()
    s = resolve(conn, cfg, fetch, now=NOW + timedelta(days=7))
    assert fetch.calls == [] and s.attempted == 0 and "given up" in s.items[0][1] and len(rows(conn)) == 7


def test_a_job_is_tried_once_a_day(conn, cfg):
    add_job(conn, title="Quantum Chef")
    resolve(conn, cfg, axa_site())
    fetch = axa_site()
    resolve(conn, cfg, fetch)
    assert fetch.calls == [] and len(rows(conn)) == 1
    assert resolve(conn, cfg, axa_site(), only_job=1).attempted == 1                 # asking for one job by hand tries it again


def test_the_limit_counts_jobs(conn, cfg):
    for i, (t, p) in enumerate([("Claims Services Associate", "Hong Kong"), ("Process Engineer", "Bruxelles")]):
        add_job(conn, title=t, places=(p,), ext=str(i))
    assert resolve(conn, cfg, axa_site(), limit=1).attempted == 1


def test_dry_run_writes_nothing(conn, cfg):
    a = legacy_key(conn, add_job(conn, company="AXA Switzerland", ext="189"))
    b = add_job(conn, ext="192")
    before = [tuple(r) for r in conn.execute("SELECT id, description, description_missing, needs_rescore FROM jobs")]
    s = resolve(conn, cfg, axa_site(), dry_run=True)
    assert s.resolved == 2 and s.outcomes[0].message.startswith("would store 6332") and s.outcomes[1].duplicate_of_job_id == b
    assert rows(conn) == [] and conn.execute("SELECT COUNT(*) FROM description_log").fetchone()[0] == 0
    assert [tuple(r) for r in conn.execute("SELECT id, description, description_missing, needs_rescore FROM jobs")] == before


# --- the migration ------------------------------------------------------------------------------------------------

def test_migration_16_creates_the_resolutions_table_and_keeps_the_rows(tmp_path):
    path = tmp_path / "old.db"
    raw = db.sqlite3.connect(str(path))
    for number, script in enumerate(db.MIGRATIONS[:15], start=1):
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    raw.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES ('k', 'Analyst', 'AXA', 'new', 'a', 'b')")
    raw.commit()
    raw.close()
    c = db.connect(path)
    assert db.schema_version(c) == len(db.MIGRATIONS) >= 16
    assert [r[1] for r in c.execute("PRAGMA table_info(description_resolutions)")] == [
        "id", "job_id", "attempted_at", "day", "tier", "source_key", "platform", "url", "posting_id", "confidence", "status",
        "duplicate_of_job_id", "error", "text"]       # "text" since migration 24 (v0.87.1)
    assert c.execute("SELECT title FROM jobs").fetchone()[0] == "Analyst"
    with pytest.raises(db.sqlite3.IntegrityError):
        c.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) "
                  "VALUES (1, 'a', 'b', 'T9', 'axa', 'jibe', 'success')")
    c.close()


# --- the config ---------------------------------------------------------------------------------------------------

def cfg_file(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_the_resolver_is_off_by_default_with_five_example_sources():
    r = load_config()["resolver"]
    assert r["enabled"] is False and r["accept_t2_risk"] is False and r["delay_seconds"] == 5 and r["daily_cap_per_host"] == 60
    assert r["max_attempt_days"] == 7 and set(r["sources"]) == {"axa", "zkb", "liebherr", "jobcloud", "jobup"} and r["sources"]["axa"]["permission_note"] == ""
    assert resolver.user_agent(load_config()) == load_config()["descriptions"]["user_agent"]


def test_a_user_can_add_a_source_of_their_own(tmp_path):
    body = "resolver:\n  sources:\n    liebherr:\n      platform: jibe\n      base_url: https://jobs.example.com\n      aliases: [Liebherr]\n"
    r = load_config(cfg_file(tmp_path, body))["resolver"]["sources"]
    assert set(r) == {"axa", "zkb", "liebherr", "jobcloud", "jobup"} and r["liebherr"]["aliases"] == ["Liebherr"]


def test_daily_cap_per_host_accepts_500(tmp_path):
    body = "resolver:\n  daily_cap_per_host: 500\n"
    assert load_config(cfg_file(tmp_path, body))["resolver"]["daily_cap_per_host"] == 500


@pytest.mark.parametrize("body, message", [
    ("resolver:\n  enabled: yes please\n", "resolver.enabled"),
    ("resolver:\n  accept_t2_risk: 1\n", "accept_t2_risk"),
    ("resolver:\n  delay_seconds: 0\n", "delay_seconds"),
    ("resolver:\n  daily_cap_per_host: 0\n", "daily_cap_per_host"),
    ("resolver:\n  daily_cap_per_host: 1001\n", "must be 1 to 1000"),
    ("resolver:\n  max_attempt_days: 0\n", "max_attempt_days"),
    ("resolver:\n  wrong: 1\n", "unknown config key: resolver.wrong"),
    ("resolver:\n  sources:\n    axa:\n      platform: workday\n", "platform must be jibe, refline, liebherr or jobcloud"),
    ("resolver:\n  sources:\n    axa:\n      base_url: ''\n", "base_url"),
    ("resolver:\n  sources:\n    axa:\n      base_url: http://careers.axa.com\n", "https"),
    ("resolver:\n  sources:\n    axa:\n      aliases: []\n", "aliases"),
    ("resolver:\n  sources:\n    axa:\n      list_url: http://x.example/\n", "list_url"),
    ("resolver:\n  sources:\n    axa:\n      colour: red\n", "unknown config key: resolver.sources.axa.colour"),
    ("resolver:\n  sources:\n    new:\n      platform: jibe\n      base_url: https://x.example\n", "aliases"),
])
def test_config_validation_errors(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message):
        load_config(cfg_file(tmp_path, body))


# --- inside prepare and the command ---------------------------------------------------------------------------------

@pytest.fixture
def providers(cfg):
    replay = Replay()
    p = cfg["location"]["providers"]
    return Providers(TransportProvider(replay, p["transport_url"]), OsrmProvider(replay, p["osrm_url"]), today=TODAY)


def run_prepare(conn, cfg, providers, fetch):
    return prepare(conn, cfg, providers=providers, prompt=("p", "v1"), mail_client_factory=lambda c: FakeIMAP({}),
                   page_fetcher=fetch, page_sleep=lambda s: None, run_id="run2", now=NOW)


def test_prepare_resolves_before_the_filters_and_records_a_row(conn, cfg, providers):
    job_id = add_job(conn)
    r = run_prepare(conn, cfg, providers, axa_site())
    assert rows(conn)[0]["status"] == "success" and r.warnings == []
    assert tuple(conn.execute("SELECT adapter, found, error FROM adapter_runs WHERE adapter = 'Description resolver'").fetchone()) == ("Description resolver", 1, None)
    assert conn.execute("SELECT 1 FROM job_filter_results WHERE job_id = ? AND filter = 'language'", (job_id,)).fetchone()   # filters saw the text


def test_prepare_does_not_resolve_when_it_is_off(conn, cfg, providers):
    cfg["resolver"]["enabled"] = False
    add_job(conn)
    fetch = axa_site()
    run_prepare(conn, cfg, providers, fetch)
    assert fetch.calls == [] and rows(conn) == []


def test_prepare_survives_a_refusal_and_says_so(conn, cfg, providers):
    add_job(conn)
    r = run_prepare(conn, cfg, providers, axa_site(**{"https://careers.axa.com/api/jobs?*": FetchError("HTTP 429", 429)}))
    assert any("nothing more is requested" in w for w in r.warnings) and r.batch is not None
    assert "HTTP 429" in conn.execute("SELECT error FROM adapter_runs WHERE adapter = 'Description resolver'").fetchone()[0]


def test_prepare_survives_a_crash_in_the_resolver(conn, cfg, providers, monkeypatch):
    add_job(conn)

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(resolver, "resolve_descriptions", boom)
    r = run_prepare(conn, cfg, providers, axa_site())
    assert "resolving descriptions failed: RuntimeError" in r.warnings and r.batch is not None


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x-test-pass")
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        f"paths:\n  database: {(tmp_path / 'jobs.db').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
        f"  logs: {(tmp_path / 'logs').as_posix()}\n  lock: {(tmp_path / 'run.lock').as_posix()}\n", encoding="utf-8")
    return path


def command(config_file, *args, fetch=None):
    return cli.main(["--config", str(config_file), "resolve-descriptions", *args], page_fetcher=fetch)


def seed(config_file, n=1):
    c = db.connect(load_config(config_file)["paths"]["database"])
    db.start_run(c, "run1")
    ids = [add_job(c, company="AXA Switzerland", ext=str(i)) for i in range(n)]
    c.close()
    return ids


def test_the_command_is_refused_while_the_resolver_is_off(config_file, capsys):
    seed(config_file)
    assert command(config_file, fetch=axa_site()) == 1
    assert "resolver.enabled" in capsys.readouterr().err


def test_the_command_dry_run_then_the_real_run(config_file, capsys):
    (job_id,) = seed(config_file)
    config_file.write_text(config_file.read_text(encoding="utf-8") + "resolver:\n  enabled: true\n  accept_t2_risk: true\n  delay_seconds: 1\n", encoding="utf-8")
    fetch = axa_site()
    assert command(config_file, "--dry-run", fetch=fetch) == 0
    out = capsys.readouterr().out
    assert f"job {job_id}: would store 6332 characters" in out and "dry run, nothing written" in out and "1 resolved" in out
    c = db.connect(load_config(config_file)["paths"]["database"])
    assert c.execute("SELECT COUNT(*) FROM description_resolutions").fetchone()[0] == 0
    c.close()
    assert command(config_file, "--job", str(job_id), "--limit", "1", fetch=axa_site()) == 0
    assert "1 job(s) tried, 1 resolved" in capsys.readouterr().out
    c = db.connect(load_config(config_file)["paths"]["database"])
    assert c.execute("SELECT description_missing FROM jobs").fetchone()[0] == 0
    c.close()


def test_the_command_exits_1_when_the_site_refuses(config_file, capsys, monkeypatch):
    seed(config_file)
    monkeypatch.setattr("time.sleep", lambda s: None)
    config_file.write_text(config_file.read_text(encoding="utf-8") + "resolver:\n  enabled: true\n  accept_t2_risk: true\n  delay_seconds: 1\n", encoding="utf-8")
    assert command(config_file, fetch=axa_site(**{"https://careers.axa.com/api/jobs?*": FetchError("HTTP 429", 429)})) == 1
    assert "STOPPED" in capsys.readouterr().err


# --- Liebherr (www.liebherr.com, server-rendered Next.js search page) ---------------------------------------------------

LH_SEARCH_URL = "https://www.liebherr.com/de-ch/karriere/offene-stellen-3360980?term="
LH_SRC = {"base_url": "https://www.liebherr.com"}


def lh_page(jobs, total=None, index=1):
    data = {"props": {"pageProps": {"hydrationData": {"jlpData": {"jobs": jobs, "total": len(jobs) if total is None else total,
                                                                  "pageSize": 10, "pageIndex": index}}}}}
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script></html>"


def lh_job(job_id, title="Some Job (m/w/d)", lang="de", city="Nussbaumen"):
    return {"jobId": str(job_id), "title": title, "company": {"name": "Liebherr-International AG"}, "city": {"label": city},
            "jobDetailPageUrl": f"/de-ch/j/karriere/offene-stellen/{job_id}-{lang}-3714359"}


def lh_site(**extra):
    return Site({"https://www.liebherr.com/robots.txt": LH_ROBOTS, LH_SEARCH_URL + "*": LH_SEARCH,
                 "https://www.liebherr.com/de-ch/j/karriere/offene-stellen/85351-de-3714359": LH_JOB, **extra})


def test_liebherr_parses_the_search_page():
    found = liebherr.candidates(lh_site(), LH_SRC, LH_TITLE)
    assert [(c.posting_id, c.city) for c in found] == [("85351", "Nussbaumen"), ("84974", "Bulle")]
    assert found[0].title == "Business Process Owner (BPO) Customer Service (m/w/d)"
    assert found[0].url == "https://www.liebherr.com/de-ch/j/karriere/offene-stellen/85351-de-3714359"


def test_liebherr_search_term_has_no_gender_suffix():
    fetch = lh_site()
    liebherr.candidates(fetch, LH_SRC, LH_TITLE)
    assert fetch.calls[0] == LH_SEARCH_URL + "business%20process%20owner%20bpo%20customer%20service"


def test_liebherr_dedupes_languages_and_prefers_the_matching_title():
    page = lh_page([lh_job(5, "Prozessverantwortlicher (m/w/d)", "de"), lh_job(5, "Process Owner (m/w/d)", "en"), lh_job(6, "Other (m/w/d)", "fr")])
    found = liebherr.candidates(Site({"https://www.liebherr.com/*": page}), LH_SRC, "Process Owner (m/f/d)")
    assert [c.posting_id for c in found] == ["5", "6"]
    assert "-en-" in found[0].url


def test_liebherr_reads_further_pages_only_while_total_says_more_and_up_to_three():
    pages = {f"{LH_SEARCH_URL}x&p=2": lh_page([lh_job(i) for i in range(11, 21)], 45, 2),
             f"{LH_SEARCH_URL}x&p=3": lh_page([lh_job(i) for i in range(21, 31)], 45, 3),
             f"{LH_SEARCH_URL}x&p=4": lh_page([lh_job(99)], 45, 4), f"{LH_SEARCH_URL}x": lh_page([lh_job(i) for i in range(1, 11)], 45)}
    fetch = Site(pages)
    found = liebherr.candidates(fetch, LH_SRC, "x")
    assert len(found) == 30 and len(fetch.calls) == 3
    one = Site({f"{LH_SEARCH_URL}x": lh_page([lh_job(1)], 1)})
    liebherr.candidates(one, LH_SRC, "x")
    assert len(one.calls) == 1


def test_liebherr_a_page_without_the_data_is_a_platform_error():
    with pytest.raises(PlatformError):
        liebherr.candidates(Site({"https://www.liebherr.com/*": "<html>no data</html>"}), LH_SRC, "x")


def test_liebherr_no_result_is_an_empty_list():
    assert liebherr.candidates(Site({"https://www.liebherr.com/*": lh_page([], 0)}), LH_SRC, "Quantum Chef") == []


def test_the_gender_suffixes_of_the_two_sites_are_equal():
    assert normalise_title("Business Process Owner (BPO) Customer Service (m/f/d)") == normalise_title(
        "Business Process Owner (BPO) Customer Service (m/w/d)")
    assert title_score(LH_TITLE, "Business Process Owner (BPO) Customer Service (m/w/d)") >= 0.85
    assert normalise_title("Young Talent Lean Management (m/f/x)") == "young talent lean management"


def test_liebherr_detail_text_is_clean_and_complete():
    cand = liebherr.candidates(lh_site(), LH_SRC, LH_TITLE)[0]
    text = liebherr.detail_text(lh_site(), cand)
    assert is_complete(text) and "<" not in text and "&nbsp;" not in text
    assert "Prozessverantwortung\n\n• Umfassende Verantwortung" in text


def test_liebherr_resolves_job_190_through_the_search_page_and_the_json_ld_page(conn, cfg):
    job_id = add_job(conn, title=LH_TITLE, company="Liebherr", places=("Nussbaumen, Aargau, Switzerland",), ext="190")
    fetch = lh_site()
    s = resolve(conn, cfg, fetch)
    assert s.resolved == 1
    assert fetch.requests("liebherr.com")[-1] == "https://www.liebherr.com/de-ch/j/karriere/offene-stellen/85351-de-3714359"
    r = rows(conn)[0]
    assert (r["status"], r["posting_id"], r["source_key"], r["platform"]) == ("success", "85351", "liebherr", "liebherr")
    assert r["confidence"] >= 0.85
    assert "Prozessverantwortung" in conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def test_liebherr_no_result_is_recorded_as_no_match(conn, cfg):
    add_job(conn, title="Quantum Chef (m/f/d)", company="Liebherr Group", places=(), ext="191")
    fetch = Site({"https://www.liebherr.com/robots.txt": LH_ROBOTS, LH_SEARCH_URL + "*": lh_page([], 0)})
    s = resolve(conn, cfg, fetch)
    assert s.resolved == 0 and [x["status"] for x in rows(conn)] == ["no_match"]
    assert not any("/j/karriere/" in u for u in fetch.calls)


# --- JobCloud (www.jobs.ch, www.jobup.ch: search page state + JSON-LD job page, any-company source) ----------------------

JC_SEARCH = (FIX / "jobcloud_search.html").read_text(encoding="utf-8")
JC_JOB = (FIX / "jobcloud_job.html").read_text(encoding="utf-8")
JC_JOBUP = (FIX / "jobcloud_jobup_search.html").read_text(encoding="utf-8")
JC_ROBOTS = "User-agent: *\nDisallow: /*?*feat=\nDisallow: /api/\nDisallow: /en/vacancies/detail/*/*/*\nDisallow: /de/stellenangebote/detail/*/*/*\n"
JC_UUID = "bc3b0b8c-e34c-430a-af46-84ad36ac85cd"
JC_SRC = {"base_url": "https://www.jobs.ch"}
JC_SEARCH_URL = "https://www.jobs.ch/en/vacancies/?term="
JC_DETAIL = f"https://www.jobs.ch/en/vacancies/detail/{JC_UUID}/"
JC_TITLE = "Datenanalyst:in 80-100%"


def jc_page(rows):
    state = {"vacancy": {"results": {"main": {"results": rows, "meta": {"totalHits": len(rows), "numPages": 1}}}}}
    return "<html><script>\n  __INIT__ = " + json.dumps(state) + ";\n  __OTHER__ = {};\n</script></html>"


def jc_row(uid, title="Some Job", company="Example AG", place="Zürich", active=True):
    return {"id": uid, "title": title, "place": place, "isActive": active, "company": {"name": company}}


@pytest.fixture
def jcfg(cfg):
    """The test config with the job board jobs.ch (the `cfg` fixture drops the two any-company sources, so other tests ignore them)."""
    cfg["resolver"]["sources"]["jobcloud"] = {"platform": "jobcloud", "base_url": "https://www.jobs.ch", "list_url": "", "any_company": True,
                                              "aliases": [], "permission_note": ""}
    return cfg


def jc_site(**extra):
    return Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_SEARCH_URL + "*": JC_SEARCH, JC_DETAIL: JC_JOB, **extra})


def test_jobcloud_parses_the_search_rows_and_drops_the_inactive_ones():
    found = jobcloud.candidates(jc_site(), JC_SRC, "Data Scientist")
    assert len(found) == 5                                                 # 6 rows, 1 inactive
    first = found[0]
    assert (first.posting_id, first.title, first.company, first.url) == (JC_UUID, "Datenanalyst:in 80-100%", "santéservices ag", JC_DETAIL)
    assert first.city is None                                              # "Solothurn oder Zürich": several places, so no single city
    ruag = next(c for c in found if c.company == "RUAG MRO Holding AG")
    assert ruag.city == "Wangen" and ruag.title == "Data Scientist Sensors"
    assert "00000000-0000-4000-8000-000000000001" not in " ".join(c.posting_id for c in found)


def test_jobcloud_searches_with_the_title_words_only():
    fetch = jc_site()
    jobcloud.candidates(fetch, JC_SRC, "Business Analyst (m/f/d)")
    assert fetch.calls == [JC_SEARCH_URL + "business%20analyst"]
    assert not any("/api/" in u or "feat=" in u for u in fetch.calls)


def test_jobcloud_jobup_uses_the_french_path_and_a_list_url_overrides_it():
    fetch = Site({"https://www.jobup.ch/fr/emplois/?term=*": JC_JOBUP})
    found = jobcloud.candidates(fetch, {"base_url": "https://www.jobup.ch"}, "Analyste")
    assert fetch.calls == ["https://www.jobup.ch/fr/emplois/?term=analyste"]
    assert (found[0].company, found[0].city) == ("Move UP SA", "Genève")
    assert found[0].url == f"https://www.jobup.ch/fr/emplois/detail/{found[0].posting_id}/"
    de = Site({"https://www.jobs.ch/de/stellenangebote/?term=*": JC_SEARCH})
    found = jobcloud.candidates(de, {"base_url": "https://www.jobs.ch", "list_url": "https://www.jobs.ch/de/stellenangebote/"}, "x")
    assert found[0].url == f"https://www.jobs.ch/de/stellenangebote/detail/{JC_UUID}/"


def test_jobcloud_detail_urls_are_allowed_by_robots_and_the_long_forms_and_api_are_not():
    robots = Robots(lambda url: JC_ROBOTS, UA)
    for url in (JC_DETAIL, f"https://www.jobs.ch/de/stellenangebote/detail/{JC_UUID}/", JC_SEARCH_URL + "data%20analyst"):
        assert robots.allowed(url), url
    for url in ("https://www.jobs.ch/en/vacancies/detail/x/a/b", f"{JC_DETAIL}some-slug/company/",
                "https://www.jobs.ch/api/v1/vacancies", JC_SEARCH_URL + "data&feat=x"):
        assert not robots.allowed(url), url
    assert all(robots.allowed(c.url) for c in jobcloud.candidates(jc_site(), JC_SRC, "x"))


@pytest.mark.parametrize("page", ["<html>no state</html>", "<script>__INIT__ = {broken;</script>", '<script>__INIT__ = {"vacancy": {}};</script>',
                                  jc_page([{"title": "no id"}])])
def test_jobcloud_a_page_without_the_state_is_a_platform_error(page):
    with pytest.raises(PlatformError):
        jobcloud.candidates(Site({"https://www.jobs.ch/*": page}), JC_SRC, "x")


def test_jobcloud_no_result_is_an_empty_list():
    assert jobcloud.candidates(Site({"https://www.jobs.ch/*": jc_page([])}), JC_SRC, "Quantum Chef") == []


def test_jobcloud_detail_text_comes_from_the_json_ld_and_is_clean():
    cand = jobcloud.candidates(jc_site(), JC_SRC, JC_TITLE)[0]
    text = jobcloud.detail_text(jc_site(), cand)
    assert is_complete(text) and "<" not in text and "&nbsp;" not in text and "u003c" not in text
    assert "Deine Mission" in text and "Example Contact" in text


def test_jobcloud_the_expired_notice_counts_only_when_it_is_shown_not_in_the_translation_bundle():
    assert "Dieses Inserat ist abgelaufen" in JC_JOB and not jobcloud.is_expired(JC_JOB)       # the bundle in a <script> is on every page
    shown = JC_JOB.replace('<div id="root"></div>', '<div id="root"><h1>Dieses Inserat ist abgelaufen</h1></div>')
    assert jobcloud.is_expired(shown)
    cand = jobcloud.candidates(jc_site(), JC_SRC, JC_TITLE)[0]
    with pytest.raises(Expired):
        jobcloud.detail_text(Site({JC_DETAIL: shown}), cand)
    assert jobcloud.detail_text(Site({JC_DETAIL: "<html><body>Seite nicht gefunden</body></html>"}), cand) is None


def test_jobcloud_resolves_a_job_of_a_company_without_a_source_of_its_own(conn, jcfg):
    job_id = add_job(conn, title=JC_TITLE, company="Santéservices AG", places=("Zürich, Zurich, Switzerland",), ext="300")
    fetch = jc_site()
    s = resolve(conn, jcfg, fetch)
    assert s.resolved == 1 and fetch.requests("jobs.ch")[-1] == JC_DETAIL
    r = rows(conn)[0]
    assert (r["status"], r["posting_id"], r["source_key"], r["platform"], r["tier"]) == ("success", JC_UUID, "jobcloud", "jobcloud", "T2")
    assert "Deine Mission" in conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


@pytest.mark.parametrize("name", ["santéservices ag", "Santéservices AG", "Santéservices", "SANTÉSERVICES SA", "santéservices GmbH"])
def test_jobcloud_company_suffix_variants_are_accepted(conn, jcfg, name):
    add_job(conn, title=JC_TITLE, company=name, places=(), ext="301")
    assert resolve(conn, jcfg, jc_site()).resolved == 1


@pytest.mark.parametrize("name", ["Deloitte Consulting", "Other Company AG", "santé"])
def test_jobcloud_a_posting_of_another_company_is_rejected(conn, jcfg, name):
    add_job(conn, title=JC_TITLE, company=name, places=(), ext="302")
    fetch = jc_site()
    s = resolve(conn, jcfg, fetch)
    assert s.resolved == 0 and [x["status"] for x in rows(conn)] == ["no_match"]
    assert not fetch.requests("detail")


def test_jobcloud_the_company_source_goes_first_and_the_job_board_only_after_it_failed(conn, jcfg):
    add_job(conn, title="Data Scientist Sensors (m/f/d)", company="AXA", places=(), ext="303")
    uid = "aaaaaaaa-0000-4000-8000-000000000001"
    page = jc_page([jc_row(uid, "Data Scientist Sensors", "AXA SA")])
    fetch = axa_site(**{"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_SEARCH_URL + "*": page,
                        f"https://www.jobs.ch/en/vacancies/detail/{uid}/": JC_JOB})
    s = resolve(conn, jcfg, fetch)
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("axa", "no_match"), ("jobcloud", "success")]
    assert s.attempted == 1 and s.resolved == 1
    order = [u for u in fetch.calls if "robots" not in u]
    assert order[0].startswith("https://careers.axa.com/") and order[-1].startswith("https://www.jobs.ch/en/vacancies/detail/")


def test_jobcloud_is_not_asked_when_the_company_source_succeeded(conn, jcfg):
    add_job(conn, title=AXA_TITLE, company="AXA", ext="304")
    fetch = axa_site(**{"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_SEARCH_URL + "*": JC_SEARCH})
    resolve(conn, jcfg, fetch)
    assert [r["source_key"] for r in rows(conn)] == ["axa"] and not fetch.requests("jobs.ch")


def test_jobcloud_a_refusal_of_the_job_board_stops_the_run_like_any_other(conn, jcfg):
    add_job(conn, title=JC_TITLE, company="Santéservices AG", places=(), ext="305")
    add_job(conn, title="Another Title", company="Other AG", places=(), ext="306")
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_SEARCH_URL + "*": FetchError("HTTP 429", 429)})
    s = resolve(conn, jcfg, fetch)
    assert s.stopped and [r["status"] for r in rows(conn)] == ["refused"] and len(fetch.requests("jobs.ch")) == 1


def test_jobcloud_the_job_board_is_tier_t2_without_a_note_and_t1_with_one(conn, jcfg):
    add_job(conn, title=JC_TITLE, company="Santéservices AG", places=(), ext="307")
    jcfg["resolver"]["accept_t2_risk"] = False
    fetch = jc_site()
    s = resolve(conn, jcfg, fetch)
    assert s.items == [] and not fetch.calls and rows(conn) == []              # silent: every job would be reported otherwise
    jcfg["resolver"]["sources"]["jobcloud"]["permission_note"] = "jobs.ch, permission of 3 Nov 2026: job pages for personal use"
    assert resolve(conn, jcfg, jc_site()).resolved == 1 and rows(conn)[0]["tier"] == "T1"


def test_jobcloud_only_its_hosts_are_requested(conn, jcfg):
    add_job(conn, title=JC_TITLE, company="Santéservices AG", places=(), ext="308")
    jcfg["resolver"]["sources"]["jobcloud"]["list_url"] = "https://evil.example/en/vacancies/"
    fetch = jc_site()
    resolve(conn, jcfg, fetch)
    assert [x["status"] for x in rows(conn)] == ["refused"] and not any("evil.example" in u for u in fetch.calls)
    assert {s["host"] for s in resolver.sources(load_config()).values()} >= {"www.jobs.ch", "www.jobup.ch"}


def test_jobcloud_the_daily_cap_of_the_job_board_is_reported_once(conn, jcfg):
    jcfg["resolver"]["daily_cap_per_host"] = 1
    for n in range(3):
        add_job(conn, title=f"Unknown Title {n}", company=f"Other {n} AG", places=(), ext=f"31{n}")
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_SEARCH_URL + "*": jc_page([])})
    s = resolve(conn, jcfg, fetch)
    assert s.attempted == 1 and sum("daily cap" in m for _, m in s.items) == 1


def test_config_jobcloud_and_jobup_are_any_company_sources_and_any_company_must_be_a_bool(tmp_path):
    src = load_config()["resolver"]["sources"]
    assert (src["jobcloud"]["platform"], src["jobcloud"]["any_company"], src["jobcloud"]["aliases"]) == ("jobcloud", True, [])
    assert src["jobup"]["base_url"] == "https://www.jobup.ch" and src["jobup"]["any_company"] is True
    assert "any_company" not in src["axa"]
    with pytest.raises(ConfigError, match="any_company must be true or false"):
        load_config(cfg_file(tmp_path, "resolver:\n  sources:\n    jobcloud:\n      any_company: 1\n"))


def test_jobcloud_jobup_is_tried_after_jobs_ch_found_nothing(conn, jcfg):
    jcfg["resolver"]["sources"]["jobup"] = {"platform": "jobcloud", "base_url": "https://www.jobup.ch", "list_url": "", "any_company": True,
                                            "aliases": [], "permission_note": ""}
    add_job(conn, title="Business Analyst", company="Université de Genève", places=(), ext="320")
    uid = json.loads(JC_JOBUP.split("__INIT__ = ")[1].split(";\n")[0])["vacancy"]["results"]["main"]["results"][2]["id"]
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_SEARCH_URL + "*": jc_page([]),
                  "https://www.jobup.ch/robots.txt": JC_ROBOTS, "https://www.jobup.ch/fr/emplois/?term=*": JC_JOBUP,
                  f"https://www.jobup.ch/fr/emplois/detail/{uid}/": JC_JOB})
    assert resolve(conn, jcfg, fetch).resolved == 1
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("jobcloud", "no_match"), ("jobup", "success")]


# --- when the job boards are tried: no named source, or every named source found no match ---------------------------------

def axa_on_board(uid="aaaaaaaa-0000-4000-8000-000000000002", **extra):
    page = jc_page([jc_row(uid, "Business Analyst Claims Smart Automation", "AXA SA", place="Winterthur")])
    return axa_site(**{"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_SEARCH_URL + "*": page,
                       f"https://www.jobs.ch/en/vacancies/detail/{uid}/": JC_JOB, **extra})


def test_the_job_board_is_tried_when_the_company_has_no_source_of_its_own(conn, jcfg):
    add_job(conn, title=JC_TITLE, company="Santéservices AG", places=(), ext="330")
    fetch = jc_site()
    assert resolve(conn, jcfg, fetch).resolved == 1 and [r["source_key"] for r in rows(conn)] == ["jobcloud"]


def test_the_job_board_is_tried_when_the_named_source_found_no_match(conn, jcfg):
    add_job(conn, ext="331")
    fetch = axa_on_board(**{"https://careers.axa.com/api/jobs?*": json.dumps({"jobs": [], "totalCount": 0})})
    assert resolve(conn, jcfg, fetch).resolved == 1
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("axa", "no_match"), ("jobcloud", "success")]


def test_the_job_board_is_not_tried_after_an_error_of_the_named_source(conn, jcfg):
    add_job(conn, ext="332")
    fetch = axa_on_board(**{"https://careers.axa.com/api/jobs?*": FetchError("HTTP 404", 404)})
    resolve(conn, jcfg, fetch)
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("axa", "error")] and not fetch.requests("jobs.ch")


def test_after_an_error_of_the_named_source_the_alert_link_is_still_read_but_not_the_search(conn, jcfg):
    uid = "aaaaaaaa-0000-4000-8000-000000000002"
    job_id = add_alert_job(conn, uid=uid, title="Business Analyst Claims Smart Automation", company="AXA")
    fetch = axa_on_board(uid, **{"https://careers.axa.com/api/jobs?*": FetchError("HTTP 404", 404)})
    s = resolve(conn, jcfg, fetch)
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("axa", "error"), ("jobcloud", "success")]
    assert fetch.requests("jobs.ch") == [f"https://www.jobs.ch/en/vacancies/detail/{uid}/"] and s.resolved == 1
    assert conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == 0


def test_after_an_error_of_the_named_source_a_failing_alert_link_does_not_start_the_search(conn, jcfg):
    uid = "aaaaaaaa-0000-4000-8000-000000000009"
    add_alert_job(conn, uid=uid, title="Business Analyst Claims Smart Automation", company="AXA")
    fetch = axa_on_board(**{"https://careers.axa.com/api/jobs?*": FetchError("HTTP 404", 404)})   # the linked page is not on the site
    resolve(conn, jcfg, fetch)
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("axa", "error"), ("jobcloud", "error")]
    assert not [u for u in fetch.requests("jobs.ch") if "/detail/" not in u and "robots" not in u]


def test_the_alert_link_is_not_read_after_a_partial_outcome_of_the_named_source(conn, jcfg):
    data = json.loads(AXA_JSON)
    data["jobs"][0]["data"]["description"] = "Short text about the role."
    uid = "aaaaaaaa-0000-4000-8000-000000000002"
    ingest_job(conn, NormalizedJob(source="jobsch_alert", external_id=uid, title=AXA_TITLE, company="AXA",
                                   url=f"https://www.jobs.ch/en/vacancies/detail/{uid}/",
                                   locations=["Winterthur, Zurich, Switzerland"]), "run1")
    page = '<script type="application/ld+json">{"@type": "JobPosting", "title": "T", "description": "Short text about the role."}</script>'
    fetch = axa_on_board(uid, **{"https://careers.axa.com/api/jobs?*": json.dumps(data),
                                 "https://careers.axa.com/careers-home/jobs/27724?lang=en-us": page})
    resolve(conn, jcfg, fetch)
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("axa", "partial")] and not fetch.requests("jobs.ch")


def test_the_job_board_is_not_tried_after_a_partial_outcome_of_the_named_source(conn, jcfg):
    data = json.loads(AXA_JSON)
    data["jobs"][0]["data"]["description"] = "Short text about the role."
    add_job(conn, ext="333")
    page = '<script type="application/ld+json">{"@type": "JobPosting", "title": "T", "description": "Short text about the role."}</script>'
    fetch = axa_on_board(**{"https://careers.axa.com/api/jobs?*": json.dumps(data),
                            "https://careers.axa.com/careers-home/jobs/27724?lang=en-us": page})
    resolve(conn, jcfg, fetch)
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("axa", "partial")] and not fetch.requests("jobs.ch")


def test_the_job_board_is_not_tried_after_the_named_source_succeeded(conn, jcfg):
    add_job(conn, ext="334")
    fetch = axa_on_board()
    assert resolve(conn, jcfg, fetch).resolved == 1
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("axa", "success")] and not fetch.requests("jobs.ch")


def test_the_job_board_is_tried_when_the_named_source_was_skipped_as_tier_t2(conn, jcfg):
    jcfg["resolver"]["accept_t2_risk"] = False
    jcfg["resolver"]["sources"]["jobcloud"]["permission_note"] = "permitted"
    add_job(conn, ext="335")
    fetch = axa_on_board()
    assert resolve(conn, jcfg, fetch).resolved == 1
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("jobcloud", "success")] and not fetch.requests("careers.axa.com")


def test_the_job_board_is_tried_when_the_named_source_reached_its_daily_cap(conn, jcfg, monkeypatch):
    real = resolver._used_today
    monkeypatch.setattr(resolver, "_used_today", lambda c, d, k: 10**6 if k == "axa" else real(c, d, k))
    add_job(conn, ext="336")
    fetch = axa_on_board()
    assert resolve(conn, jcfg, fetch).resolved == 1
    assert [(r["source_key"], r["status"]) for r in rows(conn)] == [("jobcloud", "success")] and not fetch.requests("careers.axa.com")


@pytest.mark.parametrize("board, expected", [("AXA Versicherungen AG", True), ("AXA SA", True), ("AXA", True), ("AXAB", False),
                                             ("Axa", True), ("AXAB Versicherungen", False), ("Other AXA", False), ("", False)])
def test_same_company_accepts_a_board_name_that_starts_with_the_company_as_a_whole_word(board, expected):
    assert resolver._same_company("axa", Candidate("1", "T", "Zürich", "u", company=board)) is expected


def test_resolver_logs_start_end_and_outcome_counts(conn, cfg, providers, caplog):
    """v0.78.2: the resolver step says when it starts and ends and what came out, per outcome and per tier (fetching is unchanged)."""
    import logging
    caplog.set_level(logging.INFO, logger="recruitment")
    add_job(conn)
    run_prepare(conn, cfg, providers, axa_site())
    lines = [r.getMessage() for r in caplog.records]
    (start,) = [m for m in lines if m.startswith("event=resolver_start")]
    (end,) = [m for m in lines if m.startswith("event=resolver_end")]
    assert "sources=" in start and lines.index(start) < lines.index(end)
    assert "resolved=1" in end and "attempted=1" in end and "outcomes=success:1" in end and "tiers=T" in end and "duration_s=" in end


# --- jobs.ch alert jobs: the posting the mail links is read directly (path B, v0.84.0) ----------------------------------------

def add_alert_job(conn, uid=JC_UUID, title="Some Title Unlike The Posting", company="Santéservices AG", ext=None):
    return ingest_job(conn, NormalizedJob(source="jobsch_alert", external_id=ext or uid, title=title, company=company,
                                          url=f"https://www.jobs.ch/en/vacancies/detail/{uid}/", locations=["Zürich"]), "run1").job_id


def test_jobcloud_a_job_from_a_jobsch_alert_is_read_directly_without_a_search(conn, jcfg):
    job_id = add_alert_job(conn)
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_DETAIL: JC_JOB})
    s = resolve(conn, jcfg, fetch)
    assert s.resolved == 1 and fetch.requests("jobs.ch") == [JC_DETAIL]
    r = rows(conn)
    assert [(x["status"], x["posting_id"], x["source_key"], x["url"]) for x in r] == [("success", JC_UUID, "jobcloud", JC_DETAIL)]
    assert "Deine Mission" in conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


def test_jobcloud_direct_reading_keeps_the_robots_rule_and_the_stop_on_refusal(conn, jcfg):
    add_alert_job(conn)
    add_alert_job(conn, uid="aaaaaaaa-0000-4000-8000-000000000002", title="Other", company="Other AG")
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, "https://www.jobs.ch/en/vacancies/detail/*": FetchError("HTTP 429", 429)})
    s = resolve(conn, jcfg, fetch)
    assert s.stopped and [x["status"] for x in rows(conn)] == ["refused"] and len(fetch.requests("jobs.ch")) == 1
    blocked = Site({"https://www.jobs.ch/robots.txt": "User-agent: *\nDisallow: /en/vacancies/detail/\n", JC_SEARCH_URL + "*": JC_SEARCH})
    resolve(conn, jcfg, blocked, only_job=1)
    assert not [u for u in blocked.requests("jobs.ch") if "/detail/" in u]


def test_jobcloud_direct_reading_counts_against_the_daily_cap(conn, jcfg):
    jcfg["resolver"]["daily_cap_per_host"] = 1
    add_alert_job(conn)
    add_alert_job(conn, uid="aaaaaaaa-0000-4000-8000-000000000003", title="Other", company="Other AG")
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, "https://www.jobs.ch/en/vacancies/detail/*": JC_JOB})
    s = resolve(conn, jcfg, fetch)
    assert s.attempted == 1 and len(rows(conn)) == 1 and any("daily cap" in m for _, m in s.items)


def test_jobcloud_when_the_direct_page_fails_the_search_still_runs(conn, jcfg):
    add_alert_job(conn, uid="aaaaaaaa-0000-4000-8000-000000000004", title=JC_TITLE)
    fetch = jc_site()                                                          # the linked page is a 404; the search finds the posting JC_UUID
    s = resolve(conn, jcfg, fetch)
    assert s.resolved == 1 and [(x["status"], x["posting_id"]) for x in rows(conn)] == [("error", "aaaaaaaa-0000-4000-8000-000000000004"), ("success", JC_UUID)]
    assert s.attempted == 1


def test_jobcloud_a_link_on_another_host_is_not_read_directly(conn, jcfg):
    ingest_job(conn, NormalizedJob(source="jobsch_alert", external_id="x", title="Some Title", company="Santéservices AG",
                                   url=f"https://evil.example/en/vacancies/detail/{JC_UUID}/"), "run1")
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_SEARCH_URL + "*": jc_page([])})
    resolve(conn, jcfg, fetch)
    assert not any("evil.example" in u or "/detail/" in u for u in fetch.calls)


# --- the description audit (v0.86.0): reasons stored, the cap order, jobs abroad, better company and city matching ------------

def expired_page():
    return JC_JOB.replace('<div id="root"></div>', '<div id="root"><h1>Dieses Inserat ist abgelaufen</h1></div>')


def test_a_no_match_stores_its_reason(conn, jcfg):
    add_job(conn, title=JC_TITLE, company="Other Company AG", places=(), ext="400")
    resolve(conn, jcfg, jc_site())
    r = rows(conn)[0]
    assert r["status"] == "no_match" and "lists no posting of" in r["error"]


def test_an_ambiguous_match_stores_its_reason(conn, cfg):
    add_job(conn, title="Kundenberater Privatkunden", company="ZKB", places=(), ext="401")     # three branches list it
    resolve(conn, cfg, zkb_site())
    r = rows(conn)[0]
    assert r["status"] == "ambiguous" and "equally close" in r["error"]


def test_a_partial_stores_its_reason(conn, cfg):
    data = json.loads(AXA_JSON)
    data["jobs"][0]["data"]["description"] = "Short text about the role."
    add_job(conn, ext="402")
    page = '<script type="application/ld+json">{"@type": "JobPosting", "title": "T", "description": "Short text about the role."}</script>'
    resolve(conn, cfg, axa_site(**{"https://careers.axa.com/api/jobs?*": json.dumps(data),
                                   "https://careers.axa.com/careers-home/jobs/27724?lang=en-us": page}))
    r = rows(conn)[0]
    assert r["status"] == "partial" and r["error"].startswith("partial: posting")


def test_jobcloud_an_expired_posting_is_not_partial(conn, jcfg):
    add_alert_job(conn)
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_DETAIL: expired_page(), JC_SEARCH_URL + "*": jc_page([])})
    s = resolve(conn, jcfg, fetch)
    first = rows(conn)[0]
    assert first["status"] == "expired" and first["error"] == f"expired: posting {JC_UUID} shows the expired notice"     # v0.87.1: its own status (was no_match)
    assert s.resolved == 0 and [x["status"] for x in rows(conn)] == ["expired", "no_match"]      # the search still ran after it
    assert any(u.startswith(JC_SEARCH_URL) for u in fetch.calls)


def test_expired_attempts_count_as_attempt_days(conn, cfg):
    job_id = add_job(conn)
    for d in range(7):
        conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status, error) "
                     "VALUES (?, 'x', ?, 'T1', 'axa', 'jibe', 'expired', 'expired: gone')", (job_id, f"2026-10-{10 + d}"))
    conn.commit()
    fetch = axa_site()
    s = resolve(conn, cfg, fetch)
    assert fetch.calls == [] and s.attempted == 0 and s.given_up == [job_id] and "given up" in s.items[0][1]


def test_a_partial_stores_its_text(conn, cfg):
    data = json.loads(AXA_JSON)
    data["jobs"][0]["data"]["description"] = "Short text about the role."
    job_id = add_job(conn, ext="403")
    page = '<script type="application/ld+json">{"@type": "JobPosting", "title": "T", "description": "Short text about the role."}</script>'
    resolve(conn, cfg, axa_site(**{"https://careers.axa.com/api/jobs?*": json.dumps(data),
                                   "https://careers.axa.com/careers-home/jobs/27724?lang=en-us": page}))
    r = rows(conn)[0]
    assert r["status"] == "partial" and r["text"] == "Short text about the role."
    assert conn.execute("SELECT description_missing, description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == 1
    assert not conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]       # never stored as the description


def _resolution_days(conn, job_id, n):
    for d in range(n):
        conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) "
                     "VALUES (?, 'x', ?, 'T1', 'axa', 'jibe', 'no_match')", (job_id, f"2026-10-{10 + d}"))
    conn.commit()


def test_the_cap_goes_to_the_newest_job_first(conn, cfg):
    cfg["resolver"]["daily_cap_per_host"] = 1
    old = add_job(conn, title="Quantum Chef", places=("Hong Kong",), ext="410")
    new = add_job(conn, title="Process Engineer", places=("Hong Kong",), ext="411")
    _resolution_days(conn, old, 3)
    s = resolve(conn, cfg, axa_site())
    today = [r for r in rows(conn) if r["day"] == "2026-11-03"]
    assert [r["job_id"] for r in today] == [new] and s.attempted == 1 and s.deferred == [old]


def test_a_path_b_job_comes_before_a_linkedin_job_under_the_cap(conn, jcfg):
    jcfg["resolver"]["daily_cap_per_host"] = 1
    add_job(conn, title="Unknown Title", company="Other AG", places=(), ext="420")
    alert = add_alert_job(conn)
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_DETAIL: JC_JOB, JC_SEARCH_URL + "*": jc_page([])})
    resolve(conn, jcfg, fetch)
    assert [(r["job_id"], r["status"]) for r in rows(conn)] == [(alert, "success")]


def test_a_job_in_warsaw_asks_no_job_board(conn, jcfg):
    add_job(conn, title=JC_TITLE, company="Santéservices AG", places=("Warsaw, Poland",), ext="430")
    fetch = jc_site()
    s = resolve(conn, jcfg, fetch)
    assert fetch.calls == [] and rows(conn) == [] and s.attempted == 0 and s.skipped_abroad == 1


def test_a_job_in_warsaw_and_zurich_still_asks_the_board(conn, jcfg):
    add_job(conn, title=JC_TITLE, company="Santéservices AG", places=("Warsaw, Poland", "Zürich, Switzerland"), ext="431")
    s = resolve(conn, jcfg, jc_site())
    assert s.skipped_abroad == 0 and s.attempted == 1 and len(rows(conn)) == 1


def test_a_job_abroad_with_an_alert_link_still_reads_that_link_only(conn, jcfg):
    ingest_job(conn, NormalizedJob(source="jobsch_alert", external_id="432", title="Some Title", company="Santéservices AG",
                                   url=JC_DETAIL, locations=["Warsaw, Poland"]), "run1")
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_DETAIL: JC_JOB, JC_SEARCH_URL + "*": jc_page([])})
    s = resolve(conn, jcfg, fetch)
    assert s.resolved == 1 and s.skipped_abroad == 0 and fetch.requests("jobs.ch") == [JC_DETAIL]


def test_jobcloud_the_first_search_names_the_company():
    fetch = jc_site()
    found = jobcloud.candidates(fetch, JC_SRC, "Data Scientist (m/f/d)", company="Santéservices AG", good=lambda c: True)
    assert fetch.calls == [JC_SEARCH_URL + "data%20scientist%20santeservices"] and found
    plain = jc_site()
    jobcloud.candidates(plain, JC_SRC, "Data Scientist")                    # without a company: as before
    assert plain.calls == [JC_SEARCH_URL + "data%20scientist"]


def _pages_of(n_pages, uid):
    state = {"vacancy": {"results": {"main": {"results": [jc_row(uid, "Other Job", "Other AG")], "meta": {"numPages": n_pages}}}}}
    return "<html><script>\n  __INIT__ = " + json.dumps(state) + ";\n</script></html>"


def test_jobcloud_reads_page_2_only_without_a_good_row():
    base = JC_SEARCH_URL + "data%20scientist%20santeservices"
    never = lambda c: False                                                 # noqa: E731
    one = Site({base: _pages_of(1, "aaaaaaaa-0000-4000-8000-000000000011"), JC_SEARCH_URL + "data%20scientist": jc_page([])})
    jobcloud.candidates(one, JC_SRC, "Data Scientist", company="Santéservices AG", good=never)
    assert one.calls == [base, JC_SEARCH_URL + "data%20scientist"]          # one page only: no page 2, then the plain search
    two = Site({base: _pages_of(2, "aaaaaaaa-0000-4000-8000-000000000011"), base + "&page=2": _pages_of(2, "aaaaaaaa-0000-4000-8000-000000000012"),
                JC_SEARCH_URL + "data%20scientist": _pages_of(9, "aaaaaaaa-0000-4000-8000-000000000013")})
    found = jobcloud.candidates(two, JC_SRC, "Data Scientist", company="Santéservices AG", good=never)
    assert two.calls == [base, base + "&page=2", JC_SEARCH_URL + "data%20scientist"] and len(found) == 3    # at most 3 pages, all kept
    good = Site({base: _pages_of(2, "aaaaaaaa-0000-4000-8000-000000000011")})
    jobcloud.candidates(good, JC_SRC, "Data Scientist", company="Santéservices AG", good=lambda c: True)
    assert good.calls == [base]                                             # a good row on page 1: nothing more


@pytest.mark.parametrize("job, board, expected", [
    ("Experis Switzerland", "Experis AG", True), ("Merck Group", "Merck", True),
    ("Generali Switzerland", "Generali Versicherungen AG", True), ("UBS", "Fabrikam AG", False)])
def test_company_match_both_ways(job, board, expected):
    from app.dedup import normalize_company
    assert resolver._same_company(normalize_company(job), Candidate("1", "T", "Zürich", "u", company=board)) is expected


@pytest.mark.parametrize("places, city, expected", [
    (["Pfäffikon"], "Pfäffikon SZ", True), (["Zurich"], "8001 Zürich", True), (["Zurich"], "Zürich-Oerlikon", True),
    (["Switzerland"], "Zug", True), (["Zug"], "Zugerberg", False), (["Winterthur, Zurich, Switzerland"], "Basel", False),
    (["Winterthur, Zurich, Switzerland"], "Zürich", True)])
def test_city_both_ways(places, city, expected):
    from app.resolver.match import city_agrees
    assert city_agrees(places, city) is expected


def test_prepare_warns_about_the_cap_and_given_up_jobs(conn, cfg, providers):
    cfg["resolver"]["daily_cap_per_host"] = 1
    old = add_job(conn, title="Quantum Chef", places=("Hong Kong",), ext="440")
    add_job(conn, title="Process Engineer", places=("Hong Kong",), ext="441")
    add_job(conn, title="Another Chef", places=("Hong Kong",), ext="442")
    _resolution_days(conn, old, 7)
    r = run_prepare(conn, cfg, providers, axa_site())
    warning = conn.execute("SELECT warning FROM adapter_runs WHERE adapter = 'Description resolver'").fetchone()[0]
    assert "daily cap" in warning and "given up" in warning and "careers.axa.com" in warning
    assert any("daily cap" in w for w in r.warnings)
    assert "daily cap" in conn.execute("SELECT summary FROM runs WHERE id = 'run2'").fetchone()[0]


# --- v0.90.0: career-site jobs, city in the early stop, notes, timestamps, given-up count, queries -------------------------

def add_site_job(conn, ext="900"):
    return ingest_job(conn, NormalizedJob(source="lgt_site", external_id=ext, title=AXA_TITLE, company="AXA",
                                          url=f"https://www.lgt.com/job/{ext}", locations=["Winterthur"]), "run1").job_id


def test_a_job_of_an_enabled_career_site_is_left_to_the_site_fetch(conn, cfg):
    cfg["adapters"]["lgt"]["enabled"] = True
    job_id = add_site_job(conn)
    fetch = axa_site()
    s = resolve(conn, cfg, fetch)
    assert rows(conn) == [] and s.attempted == 0 and fetch.calls == []
    assert s.items == [(job_id, "skipped: its own career site is read after the filters")]


def test_a_career_site_job_whose_page_fetch_failed_is_attempted(conn, cfg):
    cfg["adapters"]["lgt"]["enabled"] = True
    job_id = add_site_job(conn)
    conn.execute("INSERT INTO description_fetches (job_id, attempted_at, status, http_status, error) VALUES (?, 'x', 'error', 500, 'boom')", (job_id,))
    conn.commit()
    s = resolve(conn, cfg, axa_site())
    assert s.attempted == 1 and [r["job_id"] for r in rows(conn)] == [job_id]


def test_a_career_site_job_of_a_disabled_adapter_is_attempted(conn, cfg):
    cfg["adapters"]["lgt"]["enabled"] = False
    job_id = add_site_job(conn)
    assert resolve(conn, cfg, axa_site()).attempted == 1 and [r["job_id"] for r in rows(conn)] == [job_id]


def test_jobcloud_early_stop_needs_the_job_city(conn, jcfg):
    job_id = add_job(conn, title="Data Scientist", company="Example AG", places=("Zürich",), ext="777")
    base = JC_SEARCH_URL + "data%20scientist%20example"
    bern = jc_row("aaaaaaaa-0000-4000-8000-000000000021", "Data Scientist", "Example AG", "Bern")
    zurich_id = "aaaaaaaa-0000-4000-8000-000000000022"
    zurich = jc_row(zurich_id, "Data Scientist", "Example AG", "Zürich")

    def page(n, row):
        state = {"vacancy": {"results": {"main": {"results": [row], "meta": {"numPages": n}}}}}
        return "<html><script>\n  __INIT__ = " + json.dumps(state) + ";\n</script></html>"
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, base: page(2, bern), base + "&page=2": page(2, zurich),
                  f"https://www.jobs.ch/en/vacancies/detail/{zurich_id}/": JC_JOB})
    s = resolve(conn, jcfg, fetch)
    assert base + "&page=2" in fetch.calls
    assert [(r["job_id"], r["posting_id"], r["status"]) for r in rows(conn)] == [(job_id, zurich_id, "success")] and s.resolved == 1


def test_a_city_miss_names_the_cities_seen():
    r = choose("Data Analyst", ["Zurich"], [cand("1", "Data Analyst", "Bern"), cand("2", "Data Analyst", "Basel"), cand("3", "Other", "Bern")])
    assert r.status == "none" and r.note == "no posting in the same city (postings in: Bern, Basel)"


def test_the_board_note_says_how_many_rows_and_which_companies(conn, jcfg):
    add_job(conn, title="Data Scientist", company="Example AG", places=("Zürich",), ext="778")
    rows_ = [jc_row(f"aaaaaaaa-0000-4000-8000-0000000000{i:02d}", "Data Scientist", f"Firm {n} AG") for i, n in enumerate("ABCD", start=30)]
    fetch = Site({"https://www.jobs.ch/robots.txt": JC_ROBOTS, JC_SEARCH_URL + "*": jc_page(rows_)})
    resolve(conn, jcfg, fetch)
    first = rows(conn)[0]
    assert first["error"].endswith("lists no posting of Example AG for these words (4 rows read; companies seen: Firm A AG, Firm B AG, Firm C AG)")
    assert first["status"] == "no_match"


def test_each_attempt_has_its_own_timestamp_and_the_run_day(conn, cfg):
    add_job(conn, title="Quantum Chef", ext="1")
    add_job(conn, title="Quantum Cook", ext="2")
    before = datetime.now(timezone.utc).replace(microsecond=0)
    resolve(conn, cfg, axa_site())
    got = rows(conn)
    assert len(got) == 2 and all(r["day"] == "2026-11-03" for r in got)
    for r in got:
        stamp = datetime.strptime(r["attempted_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert before <= stamp <= datetime.now(timezone.utc)


def _past_attempts(conn, job_id, days):
    for d in days:
        conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status, error) "
                     "VALUES (?, 'x', ?, 'T1', 'axa', 'jibe', 'no_match', 'no match')", (job_id, d))
    conn.commit()


def test_the_given_up_warning_counts_only_jobs_given_up_now(conn, cfg):
    old, new = add_job(conn, title="Old Job", ext="1"), add_job(conn, title="New Job", ext="2")
    _past_attempts(conn, old, [f"2026-10-{10 + d}" for d in range(7)])           # given up long ago
    _past_attempts(conn, new, [f"2026-10-{25 + d}" for d in range(7)])           # its 7th day is the last day with any attempt
    s = resolve(conn, cfg, axa_site())
    assert s.given_up == [new] and len([m for _, m in s.items if "given up" in m]) == 2
    assert resolver.run_warning(s, 7) == "1 given up after 7 days"


def test_jibe_queries_repeat_no_word():
    assert jibe._queries("Analyst Analyst Manager (m/w/d)") == ["analyst manager"]
    assert jibe._queries("Data Data Science Science Lead Lead") == ["data science lead"]
    assert jibe._queries("Senior Data Data Analyst Reporting Reporting") == ["senior data analyst reporting", "senior analyst reporting"]
