"""Ashby in the discovery step (goal.md 5.1a (a), v0.75.0): the parser, robots.txt answering 401, the full-name board names, the
order of the platforms. Fixture tests/fixtures/discovery/ashby_board.json is MADE UP in the documented shape of the Ashby posting
API (4 Nov 2026: the live answer was not fetched; the field names are assumed from the documentation, see the network test). No network."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.config import ConfigError, load_config
from app.descriptions_fetch import FetchError
from app.resolver import ashby, discovery
from app.resolver.common import PlatformError
from tests.test_discovery import (BOARD, BOARD_URL, HOST, NOW, ROBOTS, Site, add_job, cfg, conn, discovery_on,   # noqa: F401  (fixtures)
                                  guesses, missing, rows, run, scandit_site)

BOARD_JSON = (Path(__file__).parent / "fixtures" / "discovery" / "ashby_board.json").read_text(encoding="utf-8")
AHOST = "https://api.ashbyhq.com"
ROBOTS_URL = f"{AHOST}/robots.txt"
A_URL = f"{AHOST}/posting-api/job-board/examplerobotics"
TITLE = "Senior Data Engineer"
COMPANY = "Example Robotics AG"
UNAUTHORIZED = FetchError("HTTP 401", 401)


@pytest.fixture
def ashby_only(cfg):
    cfg["resolver"]["discovery"]["platforms"] = ["ashby"]
    return cfg


def site(**extra):
    return Site({ROBOTS_URL: UNAUTHORIZED, A_URL: BOARD_JSON, **extra})


def job(conn, **kw):
    kw.setdefault("title", TITLE)
    kw.setdefault("company", COMPANY)
    return add_job(conn, **kw)


# --- the parser ----------------------------------------------------------------------------------------------------

def test_the_parser_reads_listed_postings_with_places_and_text():
    postings = ashby.parse_board(BOARD_JSON)
    assert [p.title for p in postings] == [TITLE, "Platform Engineer", "Office Manager"]       # the unlisted one is skipped
    first = postings[0]
    assert first.cities == ("Zurich", "Berlin") and first.city == "Zurich" and first.company is None
    assert first.url == "https://jobs.ashbyhq.com/examplerobotics/0a1b2c3d-0000-4000-8000-000000000001"
    assert "Responsibilities" in first.text and "<" not in first.text and len(first.text) > 1000
    assert postings[1].cities == ("Lisbon",)
    assert postings[2].text is not None and "Responsibilities" in postings[2].text        # empty HTML: the plain text is used


@pytest.mark.parametrize("text", ["not json", "[]", '{"jobs": 3}', '{"jobs": [{"title": "x"}]}', '{"jobs": [{"id": 5, "title": "x"}]}',
                                  '{"jobs": [{"id": "a"}]}', '{"error": "Job board not found"}'])
def test_a_board_answer_that_is_not_the_expected_json_is_a_platform_error(text):
    with pytest.raises(PlatformError):
        ashby.parse_board(text)


def test_probe_a_missing_board_is_none_and_other_errors_propagate():
    assert ashby.probe(Site({A_URL: FetchError("HTTP 404", 404)}), "examplerobotics") is None
    with pytest.raises(FetchError):
        ashby.probe(Site({A_URL: FetchError("HTTP 500", 500)}), "examplerobotics")
    assert ashby.probe(Site({A_URL: '{"jobs": []}'}), "examplerobotics") == []


def test_the_description_comes_with_the_list_and_costs_no_request():
    fetch = Site({})
    posting = ashby.parse_board(BOARD_JSON)[0]
    assert ashby.detail_text(fetch, "examplerobotics", posting) == posting.text and fetch.calls == []


def test_ashby_is_a_discovery_platform_without_company_names():
    assert "ashby" in discovery.PLATFORMS and ashby.COMPANY_KEY is False and discovery.PLATFORMS["greenhouse"].COMPANY_KEY is True


# --- the run -------------------------------------------------------------------------------------------------------

def test_a_robots_txt_answering_401_means_the_board_is_asked(conn, ashby_only):
    job_id = job(conn)
    fetch = site()
    s = run(conn, ashby_only, fetch)
    assert s.stopped is None and s.resolved == 1 and missing(conn, job_id) == 0
    assert fetch.calls[0] == ROBOTS_URL and A_URL in fetch.calls


def test_a_hit_makes_only_the_robots_and_the_board_request(conn, ashby_only):
    job_id = job(conn)
    fetch = site()
    s = run(conn, ashby_only, fetch, run_id="run1")
    assert fetch.calls == [ROBOTS_URL, A_URL]                              # the text is in the list answer; the posting URL is never asked
    (r,) = rows(conn)
    assert (r["status"], r["tier"], r["platform"], r["source_key"], r["job_id"]) == ("success", "T1", "ashby", "ashby:examplerobotics", job_id)
    assert r["posting_id"] == "0a1b2c3d-0000-4000-8000-000000000001" and r["confidence"] >= 0.9
    g = guesses(conn)[("ashby", "examplerobotics")]
    assert (g["status"], g["board_company"], g["jobs_listed"], g["http_status"]) == ("hit", None, 3, 200)
    assert conn.execute("SELECT source FROM description_log WHERE job_id = ?", (job_id,)).fetchone()[0] == "discovery:ashby:examplerobotics"
    assert s.refusals == []


def test_only_full_name_slugs_are_asked(conn, ashby_only):
    job(conn, company="Example Robotics Group")
    fetch = site()
    run(conn, ashby_only, fetch)
    assert fetch.asked() == [f"{AHOST}/posting-api/job-board/exampleroboticsgroup", f"{AHOST}/posting-api/job-board/example-robotics-group"]
    assert set(k[1] for k in guesses(conn)) == {"exampleroboticsgroup", "example-robotics-group"}


def test_full_slugs_and_the_slugs_of_each_platform():
    assert discovery.full_slugs("example robotics group") == ["exampleroboticsgroup", "example-robotics-group"]
    assert discovery._slugs(ashby, "example robotics group") == discovery.full_slugs("example robotics group")
    assert discovery._slugs(discovery.greenhouse, "scandit switzerland") == ["scanditswitzerland", "scandit-switzerland", "scandit"]


def test_unlisted_jobs_are_never_chosen(conn, ashby_only):
    job_id = job(conn, title="Data Analyst")
    s = run(conn, ashby_only, site())
    assert s.resolved == 0 and [r["status"] for r in rows(conn)] == ["no_match"] and missing(conn, job_id) == 1


def test_an_empty_board_is_a_miss(conn, ashby_only):
    job_id = job(conn)
    run(conn, ashby_only, site(**{A_URL: '{"jobs": []}'}))
    g = guesses(conn)[("ashby", "examplerobotics")]
    assert (g["status"], g["jobs_listed"]) == ("miss", 0) and rows(conn) == [] and missing(conn, job_id) == 1


def test_a_board_that_does_not_exist_is_a_miss_with_404(conn, ashby_only):
    job(conn)
    run(conn, ashby_only, Site({ROBOTS_URL: UNAUTHORIZED}))
    assert {k[1]: (v["status"], v["http_status"]) for k, v in guesses(conn).items()} == {"examplerobotics": ("miss", 404), "example-robotics": ("miss", 404)}


def test_another_place_gives_no_match(conn, ashby_only):
    job_id = job(conn, places=("Geneva, Switzerland",))
    s = run(conn, ashby_only, site())
    assert s.resolved == 0 and [r["status"] for r in rows(conn)] == ["no_match"] and missing(conn, job_id) == 1


def test_a_stored_hit_is_not_used_for_a_company_with_a_longer_name(conn, ashby_only):
    job(conn, ext="1")
    run(conn, ashby_only, site())
    assert guesses(conn)[("ashby", "examplerobotics")]["status"] == "hit"
    other = job(conn, company="Example Robotics Holdings", ext="2", age_days=2)
    fetch = Site({ROBOTS_URL: UNAUTHORIZED})
    run(conn, ashby_only, fetch, now=NOW + timedelta(days=1))
    assert missing(conn, other) == 1 and not any(u.endswith("/examplerobotics") for u in fetch.asked())


def test_greenhouse_is_tried_before_ashby(conn, cfg):
    cfg["resolver"]["discovery"]["platforms"] = ["greenhouse", "ashby"]
    job(conn, company="Scandit AG", title="Data Analyst", ext="1")
    fetch = Site({f"{HOST}/robots.txt": ROBOTS, ROBOTS_URL: UNAUTHORIZED})
    run(conn, cfg, fetch)
    assert fetch.asked() == [BOARD_URL, f"{AHOST}/posting-api/job-board/scandit"]


def test_ashby_is_not_asked_once_greenhouse_has_the_job(conn, cfg):
    cfg["resolver"]["discovery"]["platforms"] = ["greenhouse", "ashby"]
    add_job(conn)
    fetch = scandit_site()
    s = run(conn, cfg, fetch)
    assert s.resolved == 1 and not any(u.startswith(AHOST) for u in fetch.calls)


def test_a_429_from_ashby_stops_the_run(conn, ashby_only):
    job(conn)
    job(conn, company="Another Corp", ext="2", age_days=2)
    fetch = site(**{A_URL: FetchError("HTTP 429", 429)})
    s = run(conn, ashby_only, fetch)
    assert s.stopped and "429" in s.stopped and "nothing more is requested" in s.stopped
    assert fetch.asked() == [A_URL] and guesses(conn) == {}


def test_a_403_robots_txt_of_ashby_stops_the_run(conn, ashby_only):
    job(conn)
    s = run(conn, ashby_only, Site({ROBOTS_URL: FetchError("HTTP 403", 403), A_URL: BOARD_JSON}))
    assert s.stopped and "robots.txt" in s.stopped


def test_a_5xx_robots_txt_of_ashby_stops_the_run(conn, ashby_only):
    job(conn)
    s = run(conn, ashby_only, Site({ROBOTS_URL: FetchError("HTTP 503", 503), A_URL: BOARD_JSON}))
    assert s.stopped and "robots.txt" in s.stopped


def test_the_config_accepts_ashby(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("resolver:\n  discovery:\n    platforms: [ashby]\n", encoding="utf-8")
    assert load_config(p)["resolver"]["discovery"]["platforms"] == ["ashby"]
    p.write_text("resolver:\n  discovery:\n    platforms: [workday]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="supported: greenhouse, ashby, lever"):
        load_config(p)
