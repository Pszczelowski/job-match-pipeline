"""Automatic discovery of the same job (goal.md 5.1a (a), v0.73.0): board-name guesses, the cache table, confident matching,
the local twin, the limits, the queue, the pipeline step and the command. Fixtures in tests/fixtures/discovery (a trimmed
real answer of the Scandit Greenhouse board and its robots.txt, 4 Nov 2026) and tests/fixtures/greenhouse (the posting).
No network."""
import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cli, db, descriptions, resolver
from app.config import DISCOVERY_PLATFORMS, ConfigError, load_config
from app.dedup import ingest_job
from app.descriptions_fetch import FetchError
from app.models import NormalizedJob
from app.pipeline import prepare
from app.resolver import discovery, greenhouse
from app.resolver.common import PlatformError
from app.results import import_results
from tests.test_batch_results import batch, cfg, results_file, score   # noqa: F401  (fixture cfg)
from tests.test_location import Replay, TODAY
from tests.test_mailbox import FakeIMAP
from tests.test_missing_queue import mixed
from app.travel import OsrmProvider, Providers, TransportProvider

FIX = Path(__file__).parent / "fixtures"
BOARD = (FIX / "discovery" / "greenhouse_board.json").read_text(encoding="utf-8")
ROBOTS = (FIX / "discovery" / "greenhouse_robots.txt").read_text(encoding="utf-8")
MISS = (FIX / "discovery" / "greenhouse_miss.json").read_text(encoding="utf-8")
DETAIL = (FIX / "greenhouse" / "scandit_job.json").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
ZH_TITLE = "Senior Machine Learning Engineer (Biometrics & Face Recognition)"
HOST = "https://boards-api.greenhouse.io"
BOARD_URL = f"{HOST}/v1/boards/scandit/jobs"
DETAIL_URL = f"{BOARD_URL}/7552679"
BODY = ("<h2>Responsibilities</h2><p>" + "Plan and run the daily work with the team. " * 25 + "</p>"
        "<h2>Requirements</h2><p>" + "Several years of experience in the field. " * 25 + "</p>")


PLAIN = ("Responsibilities\n" + "Plan and run the daily work with the team. " * 25 + "\nRequirements\n" + "Several years of experience in the field. " * 25)


class Site:
    """A fake fetcher: url (or url prefix ending in *) -> text or an exception; every call is recorded."""

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

    def asked(self):
        return [u for u in self.calls if not u.endswith("robots.txt")]


def scandit_site(**extra):
    return Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: BOARD, DETAIL_URL: DETAIL, **extra})


def gh_board(*postings, company="Scandit"):
    """postings: (id, title, city); an empty city leaves the place out."""
    return json.dumps({"jobs": [{"id": i, "title": t, "company_name": company, "absolute_url": f"https://example.invalid/{i}",
                                 "location": {"name": c}} for i, t, c in postings], "meta": {"total": len(postings)}})


def detail(body=BODY):
    return json.dumps({"id": 1, "content": html.escape(body)})


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.start_run(c, "run1")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def discovery_on(cfg):
    cfg["resolver"].update(enabled=True)
    cfg["resolver"]["discovery"]["enabled"] = True
    cfg["resolver"]["discovery"]["platforms"] = ["greenhouse"]       # the exact request lists below are those of Greenhouse alone (Ashby: test_discovery_ashby.py)
    for board in ("jobcloud", "jobup"):
        cfg["resolver"]["sources"].pop(board, None)


def add_job(conn, title=ZH_TITLE, company="Scandit AG", places=("Zurich, Switzerland",), ext="1", age_days=1, filtered=True, description=""):
    job_id = ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id=ext, title=title, company=company,
                                            url=f"https://www.linkedin.com/jobs/view/{ext}/", locations=list(places),
                                            description=description), "run1").job_id
    seen = (NOW - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", (seen, job_id))
        if filtered:
            conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, created_at) VALUES (?, 'seniority', 'pass', ?)", (job_id, seen))
    return job_id


def run(conn, cfg, fetch, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("sleep", lambda s: None)
    return discovery.discover_descriptions(conn, cfg, fetch, **kw)


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM description_resolutions ORDER BY id")]


def guesses(conn):
    return {(r["platform"], r["slug"]): dict(r) for r in conn.execute("SELECT * FROM board_guesses")}


def missing(conn, job_id):
    return conn.execute("SELECT description_missing FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]


# --- guessing and company check --------------------------------------------------------------------------------

@pytest.mark.parametrize("company, expected", [
    ("GetYourGuide AG", ["getyourguide"]),
    ("21Shares AG", ["21shares"]),
    ("Zürcher Kantonalbank", ["zurcherkantonalbank", "zurcher-kantonalbank"]),
    ("Swiss Re Group", ["swissregroup", "swiss-re-group", "swissre"]),
    ("Scandit Switzerland GmbH", ["scanditswitzerland", "scandit-switzerland", "scandit"]),
    ("XY", []),
    ("", []),
])
def test_slugs_for(company, expected):
    assert discovery.slugs_for(company) == expected


def test_slugs_for_gives_at_most_three_distinct_names():
    out = discovery.slugs_for("One Two Three Group Holding")
    assert len(out) <= 3 and len(set(out)) == len(out)


@pytest.mark.parametrize("job, board, ok", [
    ("scandit", "Scandit", True), ("axa", "AXA Versicherungen AG", True), ("axa versicherungen", "AXA", True),
    ("scandit", "Scanditronix", False), ("scandit", None, False), ("", "Scandit", False), ("ubs", "UBS Group AG", True),
])
def test_company_matches(job, board, ok):
    assert discovery.company_matches(job, board) is ok


def test_the_greenhouse_parser_reads_the_real_board_and_a_miss_is_none():
    postings = greenhouse.parse_board(BOARD)
    assert len(postings) == 11 and {p.company for p in postings} == {"Scandit"}
    assert sorted(p.cities[0] for p in postings if p.title == ZH_TITLE) == ["Tampere", "Zurich"]
    assert greenhouse.probe(Site({BOARD_URL: FetchError("HTTP 404", 404)}), "scandit") is None
    with pytest.raises(FetchError):
        greenhouse.probe(Site({BOARD_URL: FetchError("HTTP 500", 500)}), "scandit")
    with pytest.raises(PlatformError):                                      # a 200 with the miss body is not a board
        greenhouse.probe(Site({BOARD_URL: MISS}), "scandit")


# --- the config and the table ----------------------------------------------------------------------------------------

def test_discovery_defaults_are_off_with_greenhouse_ashby_and_lever():
    d = load_config()["resolver"]["discovery"]
    assert d == {"enabled": False, "window_days": 120, "platforms": ["greenhouse", "ashby", "lever"], "local_twins": True,
                 "new_companies_per_day": 10, "miss_retry_days": 30}
    assert tuple(discovery.PLATFORMS) == DISCOVERY_PLATFORMS


def cfg_file(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.parametrize("body, message", [
    ("resolver:\n  discovery:\n    platforms: [greenhouse, workday]\n", "unknown platform 'workday'; supported: greenhouse, ashby, lever"),
    ("resolver:\n  discovery:\n    platforms: [workday]\n", "unknown platform 'workday'"),
    ("resolver:\n  discovery:\n    platforms: greenhouse\n", "platforms must be a list"),
    ("resolver:\n  discovery:\n    platforms: [greenhouse, greenhouse]\n", "a platform is listed twice"),
    ("resolver:\n  discovery:\n    enabled: 1\n", "discovery.enabled"),
    ("resolver:\n  discovery:\n    local_twins: no thanks\n", "discovery.local_twins"),
    ("resolver:\n  discovery:\n    window_days: 0\n", "window_days must be 1 to 365"),
    ("resolver:\n  discovery:\n    window_days: 366\n", "window_days"),
    ("resolver:\n  discovery:\n    new_companies_per_day: 51\n", "new_companies_per_day must be 1 to 50"),
    ("resolver:\n  discovery:\n    miss_retry_days: 0\n", "miss_retry_days"),
    ("resolver:\n  discovery:\n    colour: red\n", "unknown config key: resolver.discovery.colour"),
])
def test_discovery_config_validation(tmp_path, body, message):
    with pytest.raises(ConfigError, match=message):
        load_config(cfg_file(tmp_path, body))


def test_migration_19_creates_board_guesses_on_a_v18_database_and_is_idempotent(tmp_path, monkeypatch):
    assert len(db.MIGRATIONS) >= 19
    full = list(db.MIGRATIONS)
    monkeypatch.setattr(db, "MIGRATIONS", full[:18])
    old = db.connect(tmp_path / "old.db")
    assert db.schema_version(old) == 18
    assert old.execute("SELECT name FROM sqlite_master WHERE name = 'board_guesses'").fetchone() is None
    monkeypatch.setattr(db, "MIGRATIONS", full)
    assert db.migrate(old) == len(full)
    cols = [r[1] for r in old.execute("PRAGMA table_info(board_guesses)")]
    assert cols == ["id", "platform", "slug", "status", "board_company", "jobs_listed", "http_status", "first_checked_at",
                    "checked_at", "day", "company_norm", "checks"]
    old.execute("INSERT INTO board_guesses (platform, slug, status, first_checked_at, checked_at, day, company_norm) "
                "VALUES ('greenhouse', 'x', 'miss', 'a', 'a', 'd', 'x')")
    with pytest.raises(db.sqlite3.IntegrityError):                          # one row per platform and slug
        old.execute("INSERT INTO board_guesses (platform, slug, status, first_checked_at, checked_at, day, company_norm) "
                    "VALUES ('greenhouse', 'x', 'hit', 'a', 'a', 'd', 'x')")
    with pytest.raises(db.sqlite3.IntegrityError):
        old.execute("INSERT INTO board_guesses (platform, slug, status, first_checked_at, checked_at, day, company_norm) "
                    "VALUES ('greenhouse', 'y', 'bogus', 'a', 'a', 'd', 'x')")
    old.rollback()
    old.executescript(full[18])                                              # running the script again changes nothing
    assert db.migrate(old) == len(full)
    old.close()


# --- selection ---------------------------------------------------------------------------------------------------------

def test_selection_window_closed_described_and_unfiltered_jobs(conn, cfg):
    dc = cfg["resolver"]["discovery"]
    inside = add_job(conn, title="Role A", ext="1", age_days=119)
    outside = add_job(conn, title="Role B", ext="2", age_days=121)
    closed = add_job(conn, title="Role C", ext="3")
    described = add_job(conn, title="Role D", ext="4", description=PLAIN)
    unfiltered = add_job(conn, title="Role E", ext="5", filtered=False)
    with conn:
        conn.execute("UPDATE jobs SET closed_at = ? WHERE id = ?", ("2026-11-01T00:00:00Z", closed))
    got = [r["id"] for r in discovery._selection(conn, dc, NOW, None)]
    assert got == [inside] and outside not in got and closed not in got and described not in got and unfiltered not in got
    dc["window_days"] = 200
    assert outside in [r["id"] for r in discovery._selection(conn, dc, NOW, None)]


def test_a_job_the_resolver_gave_up_on_is_still_looked_up_and_queued_jobs_come_first(conn, cfg, monkeypatch):
    old, newer, queued = (add_job(conn, title="Role A", ext="1", age_days=60), add_job(conn, title="Role B", ext="2", age_days=2),
                          add_job(conn, title="Role C", ext="3", age_days=100))
    with conn:
        for d in range(1, 8):                                               # seven days with an attempt on a named source
            conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) "
                         "VALUES (?, 'a', ?, 'T1', 'axa', 'jibe', 'no_match')", (old, f"2026-10-{d:02d}"))
    assert len(resolver._attempt_days(conn, old)) == 7
    monkeypatch.setattr(discovery, "queued_jobs", lambda c: [{"id": queued}])
    assert [r["id"] for r in discovery._selection(conn, cfg["resolver"]["discovery"], NOW, None)] == [queued, newer, old]


def test_discovery_rows_do_not_count_toward_the_resolvers_give_up(conn):
    job_id = add_job(conn)
    with conn:
        for i, (key, platform) in enumerate([("greenhouse:scandit", "greenhouse"), ("discovery:local", "local")]):
            conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) "
                         "VALUES (?, 'a', ?, 'T1', ?, ?, 'no_match')", (job_id, f"2026-10-0{i + 1}", key, platform))
    assert resolver._attempt_days(conn, job_id) == set()
    with conn:
        conn.execute("INSERT INTO description_resolutions (job_id, attempted_at, day, tier, source_key, platform, status) "
                     "VALUES (?, 'a', '2026-10-09', 'T1', 'axa', 'jibe', 'no_match')", (job_id,))
    assert resolver._attempt_days(conn, job_id) == {"2026-10-09"}


# --- Greenhouse: guess, hit, store ------------------------------------------------------------------------------------

def test_a_hit_stores_the_text_and_records_the_row_and_the_guess(conn, cfg):
    job_id = add_job(conn)
    fetch = scandit_site()
    s = run(conn, cfg, fetch, run_id="run1")
    assert s.resolved == 1 and s.attempted == 1 and s.stopped is None
    assert missing(conn, job_id) == 0
    assert conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    (r,) = rows(conn)
    assert (r["status"], r["tier"], r["platform"], r["source_key"], r["posting_id"]) == ("success", "T1", "greenhouse", "greenhouse:scandit", "7552679")
    assert r["confidence"] >= 0.9 and r["duplicate_of_job_id"] is None
    assert conn.execute("SELECT source FROM description_log WHERE job_id = ?", (job_id,)).fetchone()[0] == "discovery:greenhouse:scandit"
    g = guesses(conn)[("greenhouse", "scandit")]
    assert (g["status"], g["board_company"], g["jobs_listed"], g["http_status"], g["checks"], g["company_norm"], g["day"]) == \
        ("hit", "Scandit", 11, 200, 1, "scandit", "2026-11-04")
    assert fetch.calls == [f"{HOST}/robots.txt", BOARD_URL, DETAIL_URL]       # robots.txt first; only the API host; the posting URL is never asked


def test_the_city_must_agree_the_zurich_posting_is_chosen_over_the_tampere_twin(conn, cfg):
    add_job(conn, places=("Tampere, Finland",))
    s = run(conn, cfg, scandit_site(**{f"{BOARD_URL}/8264704": detail()}))
    assert s.resolved == 1 and rows(conn)[0]["posting_id"] == "8264704"


def test_names_that_do_not_exist_are_missed_then_the_next_name_hits(conn, cfg):
    job_id = add_job(conn, company="Scandit Group")
    fetch = scandit_site(**{BOARD_URL: BOARD.replace('"company_name":"Scandit"', '"company_name":"Scandit Group"')})   # v0.75.0: the name without "group" needs the whole company on the board
    s = run(conn, cfg, fetch)
    assert s.resolved == 1 and missing(conn, job_id) == 0
    g = guesses(conn)
    assert {k[1]: v["status"] for k, v in g.items()} == {"scanditgroup": "miss", "scandit-group": "miss", "scandit": "hit"}
    assert g[("greenhouse", "scanditgroup")]["http_status"] == 404
    assert [u for u in fetch.asked() if u.endswith("/jobs")] == [f"{HOST}/v1/boards/{n}/jobs" for n in ("scanditgroup", "scandit-group", "scandit")]


def test_a_board_of_another_company_is_remembered_and_makes_no_attempt(conn, cfg):
    job_id = add_job(conn, company="Other Corp")
    fetch = Site({f"{HOST}/robots.txt": ROBOTS, f"{HOST}/v1/boards/other/jobs": BOARD})
    s = run(conn, cfg, fetch)
    assert s.resolved == 0 and rows(conn) == [] and missing(conn, job_id) == 1
    g = guesses(conn)[("greenhouse", "other")]
    assert (g["status"], g["board_company"], g["jobs_listed"]) == ("other_company", "Scandit", 11)


def test_an_empty_board_is_a_miss_and_an_unexpected_answer_is_an_error(conn, cfg):
    add_job(conn, company="Empty AG", ext="1")
    add_job(conn, company="Odd AG", ext="2")
    fetch = Site({f"{HOST}/robots.txt": ROBOTS, f"{HOST}/v1/boards/empty/jobs": gh_board(), f"{HOST}/v1/boards/odd/jobs": FetchError("HTTP 400", 400)})
    run(conn, cfg, fetch)
    g = guesses(conn)
    assert (g[("greenhouse", "empty")]["status"], g[("greenhouse", "empty")]["jobs_listed"]) == ("miss", 0)
    assert (g[("greenhouse", "odd")]["status"], g[("greenhouse", "odd")]["http_status"]) == ("error", 400)


def test_a_miss_is_asked_again_only_after_the_retry_days_and_at_most_four_times(conn, cfg):
    add_job(conn, company="Nobody AG", age_days=0)
    fetch = Site({f"{HOST}/robots.txt": ROBOTS})
    run(conn, cfg, fetch)
    probes = lambda: [u for u in fetch.asked() if u.endswith("/nobody/jobs")]                    # noqa: E731
    assert len(probes()) == 1
    run(conn, cfg, fetch, now=NOW + timedelta(days=29))
    assert len(probes()) == 1                                               # cached
    run(conn, cfg, fetch, now=NOW + timedelta(days=31))
    assert len(probes()) == 2 and guesses(conn)[("greenhouse", "nobody")]["checks"] == 2
    with conn:
        conn.execute("UPDATE board_guesses SET checks = 4 WHERE slug = 'nobody'")
    run(conn, cfg, fetch, now=NOW + timedelta(days=100))
    assert len(probes()) == 2                                               # four checks are enough


def test_a_hit_is_not_asked_again_for_another_job_of_the_company_in_the_same_run(conn, cfg):
    add_job(conn, ext="1")
    add_job(conn, title="Customer Success Engineer", places=("Boston, USA",), ext="2", age_days=2)
    fetch = scandit_site(**{f"{BOARD_URL}/8300293": detail()})
    s = run(conn, cfg, fetch)
    assert s.resolved == 2 and fetch.calls.count(BOARD_URL) == 1 and fetch.calls.count(f"{HOST}/robots.txt") == 1


def test_the_company_budget_per_day(conn, cfg):
    cfg["resolver"]["discovery"]["new_companies_per_day"] = 1
    add_job(conn, company="Alpha AG", ext="1", age_days=1)
    add_job(conn, company="Beta AG", ext="2", age_days=2)
    fetch = Site({f"{HOST}/robots.txt": ROBOTS})
    s = run(conn, cfg, fetch)
    assert [u for u in fetch.asked()] == [f"{HOST}/v1/boards/alpha/jobs"]
    assert any("waits for tomorrow" in m and "beta" in m for _, m in s.items) and set(k[1] for k in guesses(conn)) == {"alpha"}
    run(conn, cfg, fetch, now=NOW + timedelta(days=1))                      # the next day Beta is guessed; Alpha's miss is cached
    assert [u for u in fetch.asked()] == [f"{HOST}/v1/boards/alpha/jobs", f"{HOST}/v1/boards/beta/jobs"]


def test_queued_jobs_get_the_companies_budget_first(conn, cfg, monkeypatch):
    cfg["resolver"]["discovery"]["new_companies_per_day"] = 1
    add_job(conn, company="Alpha AG", ext="1", age_days=1)
    queued = add_job(conn, company="Beta AG", ext="2", age_days=50)
    monkeypatch.setattr(discovery, "queued_jobs", lambda c: [{"id": queued}])
    fetch = Site({f"{HOST}/robots.txt": ROBOTS})
    run(conn, cfg, fetch)
    assert fetch.asked() == [f"{HOST}/v1/boards/beta/jobs"]


def test_a_429_on_a_probe_stops_everything_and_records_no_guess(conn, cfg):
    add_job(conn, company="Alpha AG", ext="1", age_days=1)
    add_job(conn, company="Beta AG", ext="2", age_days=2)
    fetch = Site({f"{HOST}/robots.txt": ROBOTS, f"{HOST}/v1/boards/alpha/jobs": FetchError("HTTP 429", 429)})
    s = run(conn, cfg, fetch)
    assert s.stopped and "nothing more is requested" in s.stopped and "429" in s.stopped
    assert fetch.asked() == [f"{HOST}/v1/boards/alpha/jobs"] and guesses(conn) == {} and rows(conn) == []


def test_a_5xx_or_network_error_stops_too(conn, cfg):
    add_job(conn)
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: FetchError("network error: timed out")}))
    assert s.stopped and "network error" in s.stopped


def test_robots_that_disallow_the_interface_mean_nothing_is_asked(conn, cfg):
    job_id = add_job(conn)
    fetch = Site({f"{HOST}/robots.txt": "User-agent: *\nDisallow: /\n", BOARD_URL: BOARD})
    s = run(conn, cfg, fetch)
    assert fetch.calls == [f"{HOST}/robots.txt"] and s.resolved == 0 and s.stopped is None
    assert guesses(conn) == {} and rows(conn) == [] and missing(conn, job_id) == 1


def test_the_real_robots_txt_allows_the_v1_interface():
    from app.resolver.robots import Robots
    robots = Robots(Site({f"{HOST}/robots.txt": ROBOTS}), "Some-Agent/1.0")
    assert robots.allowed(BOARD_URL) and not robots.allowed(f"{HOST}/embed/x")


def test_a_robots_txt_that_cannot_be_read_stops_the_run(conn, cfg):
    add_job(conn)
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": FetchError("HTTP 403", 403)}))
    assert s.stopped and "robots.txt" in s.stopped


def test_a_partial_text_is_not_stored(conn, cfg):
    job_id = add_job(conn)
    s = run(conn, cfg, scandit_site(**{DETAIL_URL: detail("Responsibilities: a few words. Requirements: few too.")}))
    assert s.resolved == 0 and missing(conn, job_id) == 1
    assert [r["status"] for r in rows(conn)] == ["partial"]
    assert rows(conn)[0]["text"] and "a few words" in rows(conn)[0]["text"]               # kept for the paste page (v0.87.1)


def test_an_ambiguous_title_is_not_stored(conn, cfg):
    job_id = add_job(conn, title="Data Analyst")
    fetch = Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: gh_board((1, "Data Analyst", "Zurich"), (2, "Data Analyst", "Zurich"))})
    run(conn, cfg, fetch)
    assert [r["status"] for r in rows(conn)] == ["ambiguous"] and missing(conn, job_id) == 1


def test_a_title_below_090_is_no_match(conn, cfg):
    add_job(conn, title="Senior Machine Learning Engineer Vision")
    s = run(conn, cfg, scandit_site())
    assert [r["status"] for r in rows(conn)] == ["no_match"] and "below 0.9" in s.items[0][1]


def test_a_posting_without_a_place_never_matches(conn, cfg):
    job_id = add_job(conn, title="Data Analyst")
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: gh_board((1, "Data Analyst", ""))}))
    assert [r["status"] for r in rows(conn)] == ["no_match"] and missing(conn, job_id) == 1
    assert "same place" in s.items[0][1]


def test_a_job_without_a_place_never_matches(conn, cfg):
    job_id = add_job(conn, title="Data Analyst", places=())
    run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: gh_board((1, "Data Analyst", "Zurich")), f"{BOARD_URL}/1": detail()}))
    assert [r["status"] for r in rows(conn)] == ["no_match"] and missing(conn, job_id) == 1


def test_a_posting_in_another_place_never_matches(conn, cfg):
    job_id = add_job(conn, title="Data Analyst", places=("Bern, Switzerland",))
    run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: gh_board((1, "Data Analyst", "Zurich")), f"{BOARD_URL}/1": detail()}))
    assert [r["status"] for r in rows(conn)] == ["no_match"] and missing(conn, job_id) == 1


def test_a_posting_of_another_company_on_the_board_is_ignored(conn, cfg):
    add_job(conn, title="Data Analyst")
    board = json.loads(gh_board((1, "Data Analyst", "Zurich")))
    board["jobs"][0]["company_name"] = "Somebody Else"
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: json.dumps(board)}))
    assert s.resolved == 0 and guesses(conn)[("greenhouse", "scandit")]["status"] == "other_company"


def test_one_attempt_per_job_and_day_and_the_daily_cap(conn, cfg):
    add_job(conn, title="Data Analyst", ext="1")
    board = gh_board((1, "Data Analyst", "Zurich"))
    fetch = Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: board, f"{BOARD_URL}/1": detail("short")})
    run(conn, cfg, fetch)
    n = len(fetch.calls)
    run(conn, cfg, fetch)
    assert len(fetch.calls) == n and len(rows(conn)) == 1                   # tried today already
    run(conn, cfg, fetch, now=NOW + timedelta(days=1))
    assert len(rows(conn)) == 2                                             # tomorrow it is tried again

    cfg["resolver"]["daily_cap_per_host"] = 1
    add_job(conn, title="Data Analyst Lead", ext="2", age_days=0)
    s = run(conn, cfg, fetch, now=NOW + timedelta(days=1))
    assert len(rows(conn)) == 2 and any("daily cap" in m for _, m in s.items)


def test_a_duplicate_posting_is_marked(conn, cfg):
    first = add_job(conn, ext="1")
    second = add_job(conn, ext="2", age_days=2, places=("Zürich",), company="Scandit")
    s = run(conn, cfg, scandit_site())
    assert s.resolved == 2
    assert rows(conn)[1]["duplicate_of_job_id"] == first and second != first


def test_a_dry_run_writes_nothing_but_the_guesses(conn, cfg):
    job_id = add_job(conn)
    s = run(conn, cfg, scandit_site(), dry_run=True)
    assert s.resolved == 1 and "would store" in s.items[0][1]
    assert rows(conn) == [] and missing(conn, job_id) == 1
    assert [g["status"] for g in guesses(conn).values()] == ["hit"]           # v0.75.0: kept, so the real run shares the company budget
    assert conn.execute("SELECT COUNT(*) FROM description_log").fetchone()[0] == 0


def test_the_command_limit_and_job_options(conn, cfg):
    first = add_job(conn, ext="1", age_days=1)
    add_job(conn, ext="2", age_days=2, title="Data Analyst")
    fetch = scandit_site(**{DETAIL_URL: detail("short")})                    # the text is partial, so the job stays without one
    s = run(conn, cfg, fetch, limit=1)
    assert s.attempted == 1 and [r["job_id"] for r in rows(conn)] == [first]
    assert run(conn, cfg, fetch, only_job=first).attempted == 1             # --job ignores "tried today"


# --- the local twin -------------------------------------------------------------------------------------------------------

def test_a_local_twin_with_a_gender_tag_title_copies_the_text_without_a_request(conn, cfg):
    twin = add_job(conn, ext="t", company="Scandit", description=PLAIN, filtered=False)
    job_id = add_job(conn, title=ZH_TITLE + " (m/w/d)", ext="1")
    fetch = Site()
    s = run(conn, cfg, fetch)
    assert s.resolved == 1 and fetch.calls == [] and missing(conn, job_id) == 0
    (r,) = rows(conn)
    assert (r["platform"], r["tier"], r["source_key"], r["duplicate_of_job_id"], r["posting_id"]) == ("local", "T1", "discovery:local", twin, str(twin))
    assert conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0].startswith("Responsibilities")
    assert conn.execute("SELECT source FROM description_log WHERE job_id = ?", (job_id,)).fetchone()[0] == "discovery:local"


def test_a_twin_in_another_place_or_with_an_incomplete_text_is_not_used(conn, cfg):
    add_job(conn, ext="t1", company="Scandit", description=PLAIN, filtered=False, places=("Boston, USA",))
    add_job(conn, ext="t2", company="Scandit", description="short text", filtered=False, title=ZH_TITLE + " (f/m/d)")
    job_id = add_job(conn, title=ZH_TITLE + " (m/w/d)", ext="1")
    fetch = Site({f"{HOST}/robots.txt": ROBOTS})
    run(conn, cfg, fetch)
    assert missing(conn, job_id) == 1 and fetch.calls                         # fell through to the boards


def test_local_twins_can_be_switched_off(conn, cfg):
    cfg["resolver"]["discovery"]["local_twins"] = False
    add_job(conn, ext="t", company="Scandit", description=PLAIN, filtered=False)
    job_id = add_job(conn, title=ZH_TITLE + " (m/w/d)", ext="1")
    fetch = Site({f"{HOST}/robots.txt": ROBOTS})
    run(conn, cfg, fetch)
    assert missing(conn, job_id) == 1 and fetch.asked()


# --- the queue (v0.72.0) ----------------------------------------------------------------------------------------------------

def test_a_queued_job_that_is_found_is_rescored_in_the_next_batch(conn, cfg):
    described, title_only = mixed(conn, cfg, [1], [1], top_n=2)
    import_results(conn, cfg, results_file(cfg, "run1", [score(described[0], 70), score(title_only[0], 60)]), NOW)
    assert [q["id"] for q in descriptions.queued_jobs(conn)] == title_only
    fetch = Site({f"{HOST}/robots.txt": ROBOTS, f"{HOST}/v1/boards/acme/jobs": gh_board((5, "Analyst 2", "Zurich"), company="Acme"),
                  f"{HOST}/v1/boards/acme/jobs/5": detail()})
    s = run(conn, cfg, fetch)
    assert s.resolved == 1 and descriptions.queued_jobs(conn) == []
    assert conn.execute("SELECT needs_rescore FROM jobs WHERE id = ?", (title_only[0],)).fetchone()[0] == 1
    batch(conn, cfg, "run2", NOW)
    assert conn.execute("SELECT 1 FROM batch_jobs WHERE batch_id = 'run2' AND job_id = ?", (title_only[0],)).fetchone()


# --- inside prepare and the command ----------------------------------------------------------------------------------------

@pytest.fixture
def providers(cfg):
    replay = Replay()
    p = cfg["location"]["providers"]
    return Providers(TransportProvider(replay, p["transport_url"]), OsrmProvider(replay, p["osrm_url"]), today=TODAY)


@pytest.fixture
def prepared_cfg(cfg, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x-test-pass")
    return cfg


def run_prepare(conn, cfg, providers, fetch):
    return prepare(conn, cfg, providers=providers, prompt=("p", "v1"), mail_client_factory=lambda c: FakeIMAP({}),
                   page_fetcher=fetch, page_sleep=lambda s: None, run_id="run2", now=NOW)


def seed_prepare_job(conn):
    job_id = ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id="9", title=ZH_TITLE, company="Scandit AG",
                                            url="https://www.linkedin.com/jobs/view/9/", locations=["Winterthur, Zurich, Switzerland"]), "run1").job_id
    return job_id


def test_prepare_runs_discovery_after_the_filters_and_records_a_row(conn, prepared_cfg, providers):
    job_id = seed_prepare_job(conn)
    r = run_prepare(conn, prepared_cfg, providers, scandit_site())
    assert missing(conn, job_id) == 0 and r.warnings == []
    assert tuple(conn.execute("SELECT adapter, found, error FROM adapter_runs WHERE adapter = 'Description discovery'").fetchone()) == ("Description discovery", 1, None)
    assert conn.execute("SELECT 1 FROM job_filter_results WHERE job_id = ? AND filter = 'seniority'", (job_id,)).fetchone()


def test_prepare_does_not_run_discovery_when_it_is_off(conn, prepared_cfg, providers):
    prepared_cfg["resolver"]["discovery"]["enabled"] = False
    job_id = seed_prepare_job(conn)
    fetch = scandit_site()
    run_prepare(conn, prepared_cfg, providers, fetch)
    assert fetch.calls == [] and missing(conn, job_id) == 1
    assert conn.execute("SELECT 1 FROM adapter_runs WHERE adapter = 'Description discovery'").fetchone() is None


def test_prepare_does_not_run_discovery_when_the_resolver_is_off(conn, prepared_cfg, providers):
    prepared_cfg["resolver"]["enabled"] = False
    seed_prepare_job(conn)
    fetch = scandit_site()
    run_prepare(conn, prepared_cfg, providers, fetch)
    assert fetch.calls == []


def test_prepare_survives_a_crash_in_discovery(conn, prepared_cfg, providers, monkeypatch):
    seed_prepare_job(conn)

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(discovery, "discover_descriptions", boom)
    r = run_prepare(conn, prepared_cfg, providers, scandit_site())
    assert "description discovery failed: RuntimeError" in r.warnings and r.batch is not None


def test_prepare_survives_a_refusal_and_says_so(conn, prepared_cfg, providers):
    seed_prepare_job(conn)
    r = run_prepare(conn, prepared_cfg, providers, scandit_site(**{BOARD_URL: FetchError("HTTP 429", 429)}))
    assert any("nothing more is requested" in w for w in r.warnings) and r.batch is not None
    assert "429" in conn.execute("SELECT error FROM adapter_runs WHERE adapter = 'Description discovery'").fetchone()[0]


def test_prepare_records_a_robots_refusal_in_the_discovery_row(conn, prepared_cfg, providers):
    seed_prepare_job(conn)
    r = run_prepare(conn, prepared_cfg, providers, scandit_site(**{f"{HOST}/robots.txt": "User-agent: *\nDisallow: /\n"}))
    assert r.batch is not None
    error = conn.execute("SELECT error FROM adapter_runs WHERE adapter = 'Description discovery'").fetchone()[0]
    assert error and "robots.txt" in error and "boards-api.greenhouse.io" in error


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x-test-pass")
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        f"paths:\n  database: {(tmp_path / 'jobs.db').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
        f"  logs: {(tmp_path / 'logs').as_posix()}\n  lock: {(tmp_path / 'run.lock').as_posix()}\n"
        "resolver:\n  enabled: true\n  delay_seconds: 1\n  discovery:\n    window_days: 365\n", encoding="utf-8")
    return path


def command(config_file, *args, fetch=None):
    return cli.main(["--config", str(config_file), "resolve-descriptions", "--discovery", *args], page_fetcher=fetch)


def test_the_command_discovery_dry_run_then_the_real_run_while_discovery_is_still_off(config_file, capsys):
    c = db.connect(load_config(config_file)["paths"]["database"])
    db.start_run(c, "run1")
    job_id = add_job(c, age_days=0)
    c.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), job_id))
    c.commit()
    c.close()
    assert load_config(config_file)["resolver"]["discovery"]["enabled"] is False
    assert command(config_file, "--dry-run", fetch=scandit_site()) == 0
    out = capsys.readouterr().out
    assert f"job {job_id}: would store" in out and "dry run, nothing written" in out and "1 resolved" in out
    c = db.connect(load_config(config_file)["paths"]["database"])
    assert c.execute("SELECT COUNT(*) FROM description_resolutions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM board_guesses").fetchone()[0] == 1       # v0.75.0: the guess is kept
    c.close()
    assert command(config_file, "--job", str(job_id), fetch=scandit_site()) == 0
    assert "1 job(s) tried, 1 resolved" in capsys.readouterr().out
    c = db.connect(load_config(config_file)["paths"]["database"])
    assert c.execute("SELECT description_missing FROM jobs").fetchone()[0] == 0
    c.close()


def test_the_command_exits_1_when_the_site_refuses(config_file, capsys):
    c = db.connect(load_config(config_file)["paths"]["database"])
    db.start_run(c, "run1")
    job_id = add_job(c, age_days=0)
    c.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), job_id))
    c.commit()
    c.close()
    assert command(config_file, fetch=scandit_site(**{BOARD_URL: FetchError("HTTP 429", 429)})) == 1
    assert "STOPPED" in capsys.readouterr().err


# --- review round: places, 404 on a hit, refusals, and the mutations that survived ---------------------------------------------

@pytest.mark.parametrize("places, city, ok", [
    (["Zurich, Switzerland"], "Zurich", True), (["Zurich"], "Zurich, Switzerland", True), (["Zurich, Switzerland"], "Zurich, Switzerland", True),
    (["Zurich, Switzerland"], "Geneva", False), (["Geneva, Switzerland"], "Switzerland", False), (["Switzerland"], "Geneva, Switzerland", False),
    (["Geneva, Switzerland"], "Schweiz", False), ([], "Zurich", False), (["Zurich"], "", False),
])
def test_place_agrees_ignores_country_only_places(places, city, ok):
    assert discovery._place_agrees(places, city) is ok


def test_a_posting_that_names_only_the_country_never_matches_a_city_job(conn, cfg):
    job_id = add_job(conn, title="Data Analyst", places=("Geneva, Switzerland",))
    run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: gh_board((1, "Data Analyst", "Switzerland")), f"{BOARD_URL}/1": detail()}))
    assert [r["status"] for r in rows(conn)] == ["no_match"] and missing(conn, job_id) == 1


def test_a_board_that_answers_404_after_a_hit_is_saved_as_a_miss(conn, cfg):
    add_job(conn, ext="1")
    run(conn, cfg, scandit_site())
    assert guesses(conn)[("greenhouse", "scandit")]["status"] == "hit"
    add_job(conn, title="Customer Success Engineer", places=("Boston, USA",), ext="2", age_days=1)
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS}), now=NOW + timedelta(days=1))
    g = guesses(conn)[("greenhouse", "scandit")]
    assert (g["status"], g["http_status"], g["checks"]) == ("miss", 404, 2) and s.resolved == 0


def test_a_robots_refusal_is_one_line_per_host_in_the_summary(conn, cfg):
    add_job(conn, company="Alpha AG", ext="1", age_days=1)
    add_job(conn, company="Beta AG", ext="2", age_days=2)
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": "User-agent: *\nDisallow: /\n"}))
    lines = [m for _, m in s.items if "boards-api.greenhouse.io" in m]
    assert len(lines) == 1 and lines[0].startswith("not asked: ") and "robots.txt" in lines[0]
    assert s.stopped is None and guesses(conn) == {}


def test_a_company_check_per_posting_ignores_the_postings_of_other_companies(conn, cfg):
    add_job(conn, title="Data Analyst")
    board = json.loads(gh_board((1, "Data Analyst", "Zurich"), (2, "Data Analyst Lead Region", "Zurich")))
    board["jobs"][0]["company_name"] = "Somebody Else"                      # the better title belongs to another company
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: json.dumps(board), f"{BOARD_URL}/1": detail()}))
    assert s.resolved == 0 and [r["status"] for r in rows(conn)] == ["no_match"]


def test_the_company_budget_counts_the_guesses_already_made_today(conn, cfg):
    cfg["resolver"]["discovery"]["new_companies_per_day"] = 1
    add_job(conn, company="Alpha AG", ext="1", age_days=1)
    fetch = Site({f"{HOST}/robots.txt": ROBOTS})
    run(conn, cfg, fetch)
    add_job(conn, company="Beta AG", ext="2", age_days=2)
    s = run(conn, cfg, fetch)                                               # the same day, a second run
    assert fetch.asked() == [f"{HOST}/v1/boards/alpha/jobs"] and any("waits for tomorrow" in m for _, m in s.items)


def test_a_posting_that_requires_c1_german_archives_the_job(conn, cfg):
    job_id = add_job(conn)
    text = BODY + "<p>Fluent German (C1) is a must for this role, written and spoken.</p>"
    run(conn, cfg, scandit_site(**{DETAIL_URL: detail(text)}))
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT reason_code FROM job_archive").fetchone()[0] == "language_c1_required"


def test_a_local_twin_text_that_requires_c1_german_archives_the_job(conn, cfg):
    add_job(conn, ext="t", company="Scandit", description=PLAIN + "\nFluent German (C1) is a must for this role, written and spoken.", filtered=False)
    job_id = add_job(conn, title=ZH_TITLE + " (m/w/d)", ext="1")
    run(conn, cfg, Site())
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE id = ?", (job_id,)).fetchone()[0] == 0


def test_the_margin_between_two_postings_is_0_10(conn, cfg):
    job_id = add_job(conn, title="Senior Machine Learning Engineer Biometrics & Face Recognition Zurich")
    board = gh_board((1, ZH_TITLE, "Zurich"), (2, "Machine Learning Engineer Biometrics & Face Recognition", "Zurich"))   # 0.9099 and 0.8167
    run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS, BOARD_URL: board, f"{BOARD_URL}/1": detail()}))
    assert [r["status"] for r in rows(conn)] == ["ambiguous"] and missing(conn, job_id) == 1


def test_the_title_threshold_is_0_90_not_0_85(conn, cfg):
    job_id = add_job(conn, title="Machine Learning Engineer (Biometrics & Face Recognition)")                  # scores 0.8976
    run(conn, cfg, scandit_site())
    assert [r["status"] for r in rows(conn)] == ["no_match"] and missing(conn, job_id) == 1


def test_a_local_twin_needs_a_title_score_of_0_85(conn, cfg):
    add_job(conn, ext="t", company="Scandit", description=PLAIN, filtered=False, title="Senior Machine Learning Engineer (Biometrics)")   # 0.7746
    job_id = add_job(conn, ext="1")
    run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS}))
    assert missing(conn, job_id) == 1 and all(r["platform"] != "local" for r in rows(conn))


def test_the_window_includes_exactly_120_days_and_excludes_one_second_more(conn, cfg):
    dc = cfg["resolver"]["discovery"]
    on_edge, past = add_job(conn, title="Role A", ext="1"), add_job(conn, title="Role B", ext="2")
    with conn:
        conn.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", ((NOW - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ"), on_edge))
        conn.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", ((NOW - timedelta(days=120, seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), past))
    assert [r["id"] for r in discovery._selection(conn, dc, NOW, None)] == [on_edge]


# --- v0.75.0: the v0.73.0 review nits ----------------------------------------------------------------------------------------

def test_a_reached_daily_cap_asks_no_board_not_even_a_known_hit(conn, cfg):
    cfg["resolver"]["daily_cap_per_host"] = 1
    add_job(conn, ext="1")
    run(conn, cfg, scandit_site())                                         # one attempt today, the board is a stored hit
    assert len(rows(conn)) == 1 and guesses(conn)[("greenhouse", "scandit")]["status"] == "hit"
    add_job(conn, title="Customer Success Engineer", places=("Boston, USA",), ext="2", age_days=1)
    fetch = Site({})
    s = run(conn, cfg, fetch)
    assert fetch.calls == [] and len(rows(conn)) == 1
    assert len([m for _, m in s.items if "daily cap" in m]) == 1


@pytest.mark.parametrize("company, slug, name, ok", [
    ("scandit", "scandit", "Scandit", True),
    ("scandit", "scandit", "Scandit AG", True),
    ("scandit", "scandit", "Scanditronix", False),
    ("partners group", "partnersgroup", "Partners Group", True),
    ("partners group", "partners-group", "Partners Group Holding", True),
    ("partners group", "partners", "Partners", False),                     # the slug dropped "group": the board must name the whole company
    ("partners group", "partners", "Partners Group", True),
    ("partners group", "partners", "Partners Group Holding", True),
    ("partners group", "partners", "Partners Capital", False),
    ("partners group", "partners", None, False),
    ("partners", "partners", "Partners Group", True),                      # a full-name slug: the old rule
    ("partners", "partners", "Partners", True),
    ("partners group", "partnersgroup", None, False),
])
def test_company_ok(company, slug, name, ok):
    assert discovery.company_ok(company, slug, name) is ok


def test_company_ok_without_a_company_name_on_the_board_needs_a_full_name_slug():
    assert discovery.company_ok("partners group", "partnersgroup", None, names_company=False) is True
    assert discovery.company_ok("partners group", "partners-group", None, names_company=False) is True
    assert discovery.company_ok("partners group", "partners", None, names_company=False) is False


def test_a_name_with_dropped_words_needs_the_full_company_on_the_board(conn, cfg):
    job_id = add_job(conn, title="Data Analyst", company="Partners Group", ext="1")
    board = gh_board((1, "Data Analyst", "Zurich"), company="Partners")
    fetch = Site({f"{HOST}/robots.txt": ROBOTS, f"{HOST}/v1/boards/partners/jobs": board, f"{HOST}/v1/boards/partners/jobs/1": detail()})
    s = run(conn, cfg, fetch)
    assert s.resolved == 0 and rows(conn) == [] and missing(conn, job_id) == 1
    assert guesses(conn)[("greenhouse", "partners")]["status"] == "other_company"


def test_an_other_company_row_is_used_for_the_company_it_names(conn, cfg):
    first = add_job(conn, title="Data Analyst", company="Partners Group", ext="1")
    conn.execute("UPDATE jobs SET dedup_key = 'legacy|' || dedup_key WHERE id = ?", (first,))  # since v0.76.0 'Partners Group' and 'Partners' share a key
    conn.commit()
    board = gh_board((1, "Data Analyst", "Zurich"), company="Partners")
    fetch = Site({f"{HOST}/robots.txt": ROBOTS, f"{HOST}/v1/boards/partners/jobs": board, f"{HOST}/v1/boards/partners/jobs/1": detail()})
    run(conn, cfg, fetch)
    assert guesses(conn)[("greenhouse", "partners")]["status"] == "other_company"
    real = add_job(conn, title="Data Analyst", company="Partners", ext="2", age_days=2)
    s = run(conn, cfg, fetch)
    assert s.resolved == 1 and missing(conn, real) == 0
    assert guesses(conn)[("greenhouse", "partners")]["status"] == "hit"


def test_a_dry_run_and_a_real_run_on_the_same_day_share_the_company_budget(conn, cfg):
    cfg["resolver"]["discovery"]["new_companies_per_day"] = 1
    add_job(conn, company="Alpha AG", ext="1", age_days=1)
    add_job(conn, company="Beta AG", ext="2", age_days=2)
    fetch = Site({f"{HOST}/robots.txt": ROBOTS})
    run(conn, cfg, fetch, dry_run=True)
    assert fetch.asked() == [f"{HOST}/v1/boards/alpha/jobs"]
    real = Site({f"{HOST}/robots.txt": ROBOTS})
    s = run(conn, cfg, real)
    assert real.asked() == [] and any("waits for tomorrow" in m and "beta" in m for _, m in s.items)


@pytest.mark.parametrize("setup, text", [
    ("missing", "no job with this id"),
    ("closed", "closed"),
    ("described", "has a description"),
    ("unfiltered", "has not passed the filters yet"),
    ("old", "first seen 2026-04-18, outside the 120-day window (resolver.discovery.window_days)"),
    ("nocompany", "no company name"),
])
def test_the_job_option_says_why_a_job_is_not_looked_up(conn, cfg, setup, text):
    jid = 999
    if setup == "closed":
        jid = add_job(conn)
        with conn:
            conn.execute("UPDATE jobs SET closed_at = ? WHERE id = ?", ("2026-11-01T00:00:00Z", jid))
    elif setup == "described":
        jid = add_job(conn, description=PLAIN, filtered=False)
    elif setup == "unfiltered":
        jid = add_job(conn, filtered=False)
    elif setup == "old":
        jid = add_job(conn, age_days=200)
    elif setup == "nocompany":
        jid = add_job(conn)
        with conn:
            conn.execute("UPDATE jobs SET company = '' WHERE id = ?", (jid,))
    fetch = Site({})
    s = run(conn, cfg, fetch, only_job=jid)
    assert fetch.calls == [] and s.attempted == 0
    assert len(s.items) == 1 and s.items[0][0] == jid and text in s.items[0][1]


def test_the_command_job_option_prints_the_reason(config_file, capsys):
    db.connect(load_config(config_file)["paths"]["database"]).close()
    assert command(config_file, "--job", "999", fetch=Site({})) == 0
    out = capsys.readouterr().out
    assert "job 999: no job with this id" in out and "0 job(s) tried" in out


@pytest.mark.parametrize("company, slug, name, ok", [
    ("roche schweiz", "roche", "Roche", True),
    ("swiss re group", "swissre", "Swiss Re", True),
    ("partners group", "partners", "Partners Capital", False),          # group dropped, one word left: the whole company is needed
    ("roche schweiz", "roche", "Rochester", False),
])
def test_company_ok_drops_place_and_group_words(company, slug, name, ok):
    assert discovery.company_ok(company, slug, name) is ok


def test_company_ok_ashby_slug_from_the_company_without_place_words():
    assert discovery.company_ok("roche schweiz", "roche", None, names_company=False) is True
    assert discovery.company_ok("partners group", "partners", None, names_company=False) is False


# --- v0.75.1: a job that finds no board says why ------------------------------------------------------------------------------------

def no_board_lines(s, job_id):
    return [m for j, m in s.items if j == job_id and m.startswith("no job board found")]


def test_all_boards_answering_404_give_exactly_one_line_naming_the_guesses(conn, cfg):
    job_id = add_job(conn, company="Scandit Group")
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS}))
    assert len(s.items) == 1 and s.attempted == 0 and s.looked_up == 1
    line = no_board_lines(s, job_id)[0]
    for slug in ("scanditgroup", "scandit-group", "scandit"):
        assert f"greenhouse:{slug} no board (404)" in line


def test_the_job_option_on_a_cached_miss_asks_nothing_and_says_when_it_is_asked_again(conn, cfg):
    job_id = add_job(conn, company="Nobody AG")
    run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS}))
    fetch = Site({f"{HOST}/robots.txt": ROBOTS})
    s = run(conn, cfg, fetch, only_job=job_id, now=NOW + timedelta(days=1))
    assert fetch.asked() == []
    assert len(no_board_lines(s, job_id)) == 1 and "asked again from 2026-12-04" in s.items[0][1]


def test_a_cached_miss_that_was_checked_four_times_says_it_is_given_up(conn, cfg):
    job_id = add_job(conn, company="Nobody AG")
    run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS}))
    with conn:
        conn.execute("UPDATE board_guesses SET checks = 4")
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS}), only_job=job_id, now=NOW + timedelta(days=90))
    assert "given up after 4 checks" in no_board_lines(s, job_id)[0]


def test_a_platform_error_is_named_in_the_line(conn, cfg, monkeypatch):
    job_id = add_job(conn, company="Nobody AG")

    def broken(get, slug):
        raise PlatformError("unexpected answer")
    monkeypatch.setattr(greenhouse, "probe", broken)
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS}))
    assert "error" in no_board_lines(s, job_id)[0]


def test_a_board_of_another_company_and_an_empty_board_are_named(conn, cfg):
    other, empty = add_job(conn, company="Other Corp", ext="1"), add_job(conn, company="Empty AG", ext="2")
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS, f"{HOST}/v1/boards/other/jobs": BOARD, f"{HOST}/v1/boards/empty/jobs": gh_board()}))
    assert "board of another company" in no_board_lines(s, other)[0]
    assert "empty board" in no_board_lines(s, empty)[0]


def test_a_robots_refusal_is_named_for_every_job(conn, cfg):
    first, second = add_job(conn, company="Alpha AG", ext="1"), add_job(conn, company="Beta AG", ext="2")
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": "User-agent: *\nDisallow: /\n"}))
    hosted = [j for j, m in s.items if m.startswith("not asked: ")]               # the host line belongs to the job that met the refusal first
    other = ({first, second} - set(hosted)).pop()
    assert len(hosted) == 1 and "not asked (robots.txt)" in no_board_lines(s, other)[0] and no_board_lines(s, hosted[0]) == []


def test_the_budget_case_says_only_waits_for_tomorrow(conn, cfg):
    cfg["resolver"]["discovery"]["new_companies_per_day"] = 1
    add_job(conn, company="Alpha AG", ext="1", age_days=1)
    beta = add_job(conn, company="Beta AG", ext="2", age_days=2)
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS}))
    assert [m for j, m in s.items if j == beta] == [m for j, m in s.items if j == beta and "waits for tomorrow" in m] and len([1 for j, _ in s.items if j == beta]) == 1


def test_no_enabled_platform_is_said(conn, cfg):
    cfg["resolver"]["discovery"]["platforms"] = []
    job_id = add_job(conn)
    s = run(conn, cfg, Site({}))
    assert "no discovery platform is enabled" in no_board_lines(s, job_id)[0]


def test_the_command_dry_run_prints_the_no_board_line_and_the_looked_up_count(config_file, capsys):
    c = db.connect(load_config(config_file)["paths"]["database"])
    db.start_run(c, "run1")
    job_id = add_job(c, company="Nobody AG", age_days=0)
    c.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), job_id))
    c.commit()
    c.close()
    assert command(config_file, "--dry-run", fetch=Site({f"{HOST}/robots.txt": ROBOTS})) == 0
    out = capsys.readouterr().out
    assert f"job {job_id}: no job board found" in out and "1 job(s) looked up, 0 job(s) tried, 0 resolved" in out


def test_a_company_without_a_buildable_board_name_does_not_blame_the_platforms(conn, cfg):
    job_id = add_job(conn, company="EY")
    s = run(conn, cfg, Site({}))
    line = no_board_lines(s, job_id)[0]
    assert "no board name can be built from 'EY'" in line and "no discovery platform" not in line


def test_a_second_job_on_a_board_that_failed_earlier_in_the_run_reports_the_reason(conn, cfg, monkeypatch):
    add_job(conn, ext="1")
    run(conn, cfg, scandit_site())                                         # the board is a stored hit
    second = add_job(conn, title="Data Analyst", ext="2", age_days=2)
    third = add_job(conn, title="Data Engineer", ext="3", age_days=3)

    def broken(get, slug):
        raise PlatformError("unexpected answer")
    monkeypatch.setattr(greenhouse, "probe", broken)
    s = run(conn, cfg, Site({f"{HOST}/robots.txt": ROBOTS}), now=NOW + timedelta(days=1))
    for job_id in (second, third):
        line = no_board_lines(s, job_id)[0]
        assert "unexpected answer" in line and "empty board" not in line


def test_jobs_after_the_daily_cap_say_not_looked_up(conn, cfg):
    cfg["resolver"]["daily_cap_per_host"] = 1
    add_job(conn, ext="1")
    run(conn, cfg, scandit_site())
    later = [add_job(conn, title="Data Analyst", ext="2", age_days=2), add_job(conn, title="Data Engineer", ext="3", age_days=3)]
    s = run(conn, cfg, Site({}))
    assert not [m for _, m in s.items if m.startswith("no job board found")]
    assert len([1 for j, m in s.items if m == "not looked up: daily cap reached"]) == 1 and len([j for j, m in s.items if j in later]) == 2
