"""Lever in the discovery step (goal.md 5.1a (a), v0.76.0): the parser, robots.txt with Crawl-delay 1, the full-name board names, the
order of the platforms. Fixture tests/fixtures/discovery/lever_board.json is MADE UP in the shape of the Lever postings answer
seen on 4 Nov 2026 (a JSON list; keys text, id, hostedUrl, categories, description, lists, additional). No network."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.config import ConfigError, load_config
from app.descriptions_fetch import FetchError
from app.resolver import discovery, lever
from app.resolver.common import PlatformError
from tests.test_discovery import (NOW, Site, add_job, cfg, conn, discovery_on,   # noqa: F401  (fixtures)
                                  guesses, missing, rows, run)

BOARD_JSON = (Path(__file__).parent / "fixtures" / "discovery" / "lever_board.json").read_text(encoding="utf-8")
LHOST = "https://api.lever.co"
ROBOTS_URL = f"{LHOST}/robots.txt"
L_URL = f"{LHOST}/v0/postings/examplerobotics?mode=json"
ROBOTS = "User-agent: *\nAllow: /\nCrawl-delay: 1\n"
NOT_FOUND = '{"ok":false,"error":"Document not found"}'
TITLE = "Senior Data Engineer"
COMPANY = "Example Robotics AG"


@pytest.fixture
def lever_only(cfg):
    cfg["resolver"]["discovery"]["platforms"] = ["lever"]
    return cfg


def site(**extra):
    return Site({ROBOTS_URL: ROBOTS, L_URL: BOARD_JSON, **extra})


def job(conn, **kw):
    kw.setdefault("title", TITLE)
    kw.setdefault("company", COMPANY)
    return add_job(conn, **kw)


# --- the parser ----------------------------------------------------------------------------------------------------

def test_the_parser_reads_postings_with_places_and_text():
    postings = lever.parse_board(BOARD_JSON)
    assert [p.title for p in postings] == [TITLE, "Platform Engineer", "Office Manager"]
    first = postings[0]
    assert first.cities == ("Zurich", "Berlin") and first.city == "Zurich" and first.company is None
    assert first.url == "https://jobs.lever.co/examplerobotics/0a1b2c3d-0000-4000-8000-000000000001"
    assert first.text.startswith("We build robots") and "<" not in first.text and len(first.text) > 1500
    assert "Responsibilities\n" in first.text and "Requirements\n" in first.text and "• Plan and run" in first.text
    assert first.text.rstrip().endswith("flexible hours.")
    assert postings[1].cities == ("Lisbon",) and postings[1].text == "Short plain text only."      # plain text when the HTML is missing
    assert postings[2].cities == () and postings[2].city is None and postings[2].text is None


@pytest.mark.parametrize("text", ["not json", "{}", '{"ok": false}', "3", '[{"text": "x"}]', '[{"id": 5, "text": "x"}]',
                                  '[{"id": "a"}]', '["a"]'])
def test_a_board_answer_that_is_not_the_expected_json_is_a_platform_error(text):
    with pytest.raises(PlatformError):
        lever.parse_board(text)


def test_probe_a_missing_board_is_none_and_other_errors_propagate():
    assert lever.probe(Site({L_URL: FetchError("HTTP 404", 404)}), "examplerobotics") is None
    with pytest.raises(FetchError):
        lever.probe(Site({L_URL: FetchError("HTTP 500", 500)}), "examplerobotics")
    assert lever.probe(Site({L_URL: "[]"}), "examplerobotics") == []


def test_the_description_comes_with_the_list_and_costs_no_request():
    fetch = Site({})
    posting = lever.parse_board(BOARD_JSON)[0]
    assert lever.detail_text(fetch, "examplerobotics", posting) == posting.text and fetch.calls == []


def test_lever_is_a_discovery_platform_without_company_names():
    assert "lever" in discovery.PLATFORMS and lever.COMPANY_KEY is False and lever.HOST == "api.lever.co"
    assert discovery._slugs(lever, "example robotics group") == discovery.full_slugs("example robotics group")


# --- the run -------------------------------------------------------------------------------------------------------

def test_a_hit_makes_only_the_robots_and_the_board_request(conn, lever_only):
    job_id = job(conn)
    fetch = site()
    s = run(conn, lever_only, fetch, run_id="run1")
    assert fetch.calls == [ROBOTS_URL, L_URL]                              # the text is in the list answer; the posting URL is never asked
    assert s.stopped is None and s.resolved == 1 and missing(conn, job_id) == 0
    (r,) = rows(conn)
    assert (r["status"], r["tier"], r["platform"], r["source_key"], r["job_id"]) == ("success", "T1", "lever", "lever:examplerobotics", job_id)
    assert r["posting_id"] == "0a1b2c3d-0000-4000-8000-000000000001" and r["confidence"] >= 0.9
    g = guesses(conn)[("lever", "examplerobotics")]
    assert (g["status"], g["board_company"], g["jobs_listed"], g["http_status"]) == ("hit", None, 3, 200)
    assert conn.execute("SELECT source FROM description_log WHERE job_id = ?", (job_id,)).fetchone()[0] == "discovery:lever:examplerobotics"


def test_the_crawl_delay_of_one_second_is_kept_between_requests(conn, lever_only):
    lever_only["resolver"]["delay_seconds"] = 0
    job(conn, company="Example Robotics Group")
    sleeps = []
    run(conn, lever_only, site(), sleep=sleeps.append)
    assert sleeps and all(x >= 1 for x in sleeps)


def test_only_full_name_slugs_are_asked(conn, lever_only):
    job(conn, company="Example Robotics Group")
    fetch = site()
    run(conn, lever_only, fetch)
    assert fetch.asked() == [f"{LHOST}/v0/postings/exampleroboticsgroup?mode=json", f"{LHOST}/v0/postings/example-robotics-group?mode=json"]


def test_an_empty_board_is_a_miss(conn, lever_only):
    job_id = job(conn)
    run(conn, lever_only, site(**{L_URL: "[]"}))
    g = guesses(conn)[("lever", "examplerobotics")]
    assert (g["status"], g["jobs_listed"]) == ("miss", 0) and rows(conn) == [] and missing(conn, job_id) == 1


def test_a_board_that_does_not_exist_is_a_miss_with_404(conn, lever_only):
    job(conn)
    run(conn, lever_only, Site({ROBOTS_URL: ROBOTS, f"{LHOST}/v0/postings/*": FetchError("HTTP 404", 404)}))
    assert {k[1]: (v["status"], v["http_status"]) for k, v in guesses(conn).items()} == {"examplerobotics": ("miss", 404), "example-robotics": ("miss", 404)}


def test_a_404_with_the_lever_error_body_is_the_same_miss():
    assert lever.probe(Site({L_URL: FetchError(NOT_FOUND, 404)}), "examplerobotics") is None


def test_another_place_gives_no_match(conn, lever_only):
    job_id = job(conn, places=("Geneva, Switzerland",))
    s = run(conn, lever_only, site())
    assert s.resolved == 0 and [r["status"] for r in rows(conn)] == ["no_match"] and missing(conn, job_id) == 1


def test_a_posting_without_a_place_is_never_chosen(conn, lever_only):
    job_id = job(conn, title="Office Manager")
    s = run(conn, lever_only, site())
    assert s.resolved == 0 and missing(conn, job_id) == 1


def test_a_stored_hit_is_not_used_for_a_company_with_a_longer_name(conn, lever_only):
    job(conn, ext="1")
    run(conn, lever_only, site())
    assert guesses(conn)[("lever", "examplerobotics")]["status"] == "hit"
    other = job(conn, company="Example Robotics Holdings", ext="2", age_days=2)
    fetch = Site({ROBOTS_URL: ROBOTS})
    run(conn, lever_only, fetch, now=NOW + timedelta(days=1))
    assert missing(conn, other) == 1 and not any("/examplerobotics?" in u for u in fetch.asked())


def test_lever_is_asked_after_greenhouse_and_ashby(conn, cfg):
    cfg["resolver"]["discovery"]["platforms"] = ["greenhouse", "ashby", "lever"]
    job(conn, company="Zed Corp", title="Data Analyst")
    fetch = Site({})
    run(conn, cfg, fetch)
    asked = fetch.asked()
    assert asked[-1].startswith(LHOST) and asked[0].startswith("https://boards-api.greenhouse.io")
    assert [u.split("/")[2] for u in asked] == sorted([u.split("/")[2] for u in asked], key={"boards-api.greenhouse.io": 0, "api.ashbyhq.com": 1, "api.lever.co": 2}.get)


def test_a_429_from_lever_stops_the_run(conn, lever_only):
    job(conn)
    job(conn, company="Another Corp", ext="2", age_days=2)
    fetch = site(**{L_URL: FetchError("HTTP 429", 429)})
    s = run(conn, lever_only, fetch)
    assert s.stopped and "429" in s.stopped and "nothing more is requested" in s.stopped
    assert fetch.asked() == [L_URL] and guesses(conn) == {}


def test_a_robots_txt_disallowing_everything_means_nothing_is_asked(conn, lever_only):
    job(conn)
    fetch = Site({ROBOTS_URL: "User-agent: *\nDisallow: /\n", L_URL: BOARD_JSON})
    s = run(conn, lever_only, fetch)
    assert s.resolved == 0 and fetch.asked() == []


def test_a_403_robots_txt_of_lever_stops_the_run(conn, lever_only):
    job(conn)
    s = run(conn, lever_only, Site({ROBOTS_URL: FetchError("HTTP 403", 403), L_URL: BOARD_JSON}))
    assert s.stopped and "robots.txt" in s.stopped


def test_the_config_accepts_lever(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("resolver:\n  discovery:\n    platforms: [lever]\n", encoding="utf-8")
    assert load_config(p)["resolver"]["discovery"]["platforms"] == ["lever"]
    p.write_text("resolver:\n  discovery:\n    platforms: [workday]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="supported: greenhouse, ashby, lever"):
        load_config(p)
