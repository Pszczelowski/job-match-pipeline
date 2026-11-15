"""Zurich Insurance career-site reader (SuccessFactors, tier T2, v0.71.0) on pages read on 4 Nov 2026 (tests/fixtures/zurich).

Real: the first page (25 rows, "of 46"), the second page (21 rows) and one job page (the recruiter's name is replaced by a made-up
one). No test touches the network.
"""
from datetime import datetime, timezone

import pytest

from app import adapters, cli, db
from app.adapters import AdapterDisabled, zurich
from app.config import load_config
from app.filters.runner import filter_new_jobs
from app.pipeline import prepare
from app.travel import OsrmProvider, Providers, TransportProvider
from tests.ats_support import Site, fx, make_cfg, make_conn
from tests.test_location import Replay, TODAY
from tests.test_mailbox import FakeIMAP

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
ZUR = next(s for s in adapters.REGISTRY if s.key == "zurich")
LIST1, LIST2, DETAIL = fx("zurich", "list1.html"), fx("zurich", "list2.html"), fx("zurich", "detail.html")
URL = "https://www.careers.zurich.com/search/?locationsearch=Switzerland&locale=en_US"
CH = {"country_codes": ["CH"]}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return make_cfg(tmp_path, monkeypatch, "zurich")


@pytest.fixture
def conn(tmp_path):
    c = make_conn(tmp_path)
    yield c
    c.close()


def site(**kw):
    return Site(pages={URL: LIST1, f"{URL}&startrow=25": LIST2}, default=DETAIL, **kw)


def test_the_first_page_gives_swiss_rows_with_ids_and_urls_on_the_host():
    jobs = ZUR.parse_list(LIST1, CH)
    assert len(jobs) == 25 and len({j.external_id for j in jobs}) == 25
    assert all(j.external_id.isdigit() and j.url.startswith("https://www.careers.zurich.com/job/") and j.url.endswith(f"/{j.external_id}/") for j in jobs)
    assert jobs[0].external_id == "1373356957" and jobs[0].title == "Ceded Reinsurance Manager 100%" and jobs[0].locations == ["Zürich, CH"]
    assert len(ZUR.parse_list(LIST2, CH)) == 21
    assert ZUR.parse_list(LIST1.replace(", CH", ", DE"), CH) == []                  # another country is dropped


def test_46_jobs_make_one_extra_page():
    assert ZUR.more_pages(URL, LIST1) == [f"{URL}&startrow=25"]
    assert ZUR.more_pages(URL, LIST1.replace("of <b>46</b>", "of <b>25</b>")) == []


def test_the_job_page_gives_text_and_the_posting_date():
    d = ZUR.parse_detail(DETAIL)
    assert d.posted == "2026-10-27" and "<" not in d.text and "Ceded Reinsurance Manager" in d.text and "What you bring" in d.text
    assert "Recruiter name" not in d.text and "Jane Example" not in d.text and "equal opportunity employer" not in d.text


def test_the_reader_is_registered_t2_and_off_by_default(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    plain = load_config()
    assert zurich.SPEC is ZUR and ZUR.tier == "T2" and ZUR.label == "Zurich Insurance career site" and ZUR.company == "Zurich Insurance"
    assert plain["adapters"]["zurich"]["enabled"] is False and plain["adapters"]["zurich"]["decision_note"] == ""
    assert plain["adapters"]["zurich"]["list_url"] == URL and ZUR not in adapters.enabled_adapters(plain)


def test_it_is_refused_without_a_decision_note(conn, cfg):
    cfg["adapters"]["zurich"]["decision_note"] = ""
    with pytest.raises(AdapterDisabled, match="tier T2"):
        adapters.fetch_list(conn, cfg, ZUR, Site())
    assert ZUR not in adapters.enabled_adapters(cfg)


def test_robots_is_read_first_and_the_services_path_is_never_requested(conn, cfg):
    s = site()
    summary, jobs = adapters.fetch_list(conn, cfg, ZUR, s, sleep=s.sleep, run_id="run1")
    assert (summary.found, summary.new, summary.error, summary.complete) == (46, 46, None, True)
    assert [u for _, u in s.requests] == ["https://www.careers.zurich.com/robots.txt", URL, f"{URL}&startrow=25"]
    assert not any("/services/" in u for _, u in s.requests)
    assert conn.execute("SELECT DISTINCT company FROM jobs").fetchone()[0] == "Zurich Insurance"
    assert conn.execute("SELECT DISTINCT source FROM job_sources").fetchone()[0] == "zurich_site"


def test_job_pages_flow_after_the_filters(conn, cfg):
    adapters.fetch_list(conn, cfg, ZUR, site(), sleep=lambda s: None, run_id="run1")
    filter_new_jobs(conn, cfg, "run1")
    s = Site(default=DETAIL)
    st = adapters.fetch_details(conn, cfg, ZUR, s, sleep=s.sleep, now=NOW, run_id="run1", limit=1)
    assert (st.fetched, st.failed) == (1, 0) and s.requests[0][1] == "https://www.careers.zurich.com/robots.txt"


@pytest.fixture
def providers(cfg):
    replay = Replay()
    p = cfg["location"]["providers"]
    return Providers(TransportProvider(replay, p["transport_url"]), OsrmProvider(replay, p["osrm_url"]), today=TODAY)


def test_prepare_gives_one_report_line_for_the_reader(conn, cfg, providers):
    s = site()
    prepare(conn, cfg, providers=providers, prompt=("p", "v1"), mail_client_factory=lambda c: FakeIMAP({}),
            page_fetcher=s, page_sleep=lambda x: None, run_id="run1", now=NOW)
    rows = conn.execute("SELECT found, new_jobs, error FROM adapter_runs WHERE adapter = 'Zurich Insurance career site'").fetchall()
    assert [tuple(r) for r in rows] == [(46, 46, None)]


def test_check_adapter_zurich_reads_list_and_one_job_page(tmp_path, monkeypatch, capsys):
    import time
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(f"paths:\n  database: {(tmp_path / 'jobs.db').as_posix()}\n  exchange: {(tmp_path / 'exchange').as_posix()}\n"
                    f"  logs: {(tmp_path / 'logs').as_posix()}\n  lock: {(tmp_path / 'run.lock').as_posix()}\n"
                    "adapters:\n  max_list_pages: 1\n  zurich:\n    enabled: true\n    decision_note: \"test\"\n", encoding="utf-8")
    s = site()
    monkeypatch.setattr(time, "sleep", s.sleep)
    assert cli.main(["--config", str(path), "check-adapter", "zurich", "--detail"], page_fetcher=s) == 0
    out = capsys.readouterr().out
    assert "Zurich Insurance career site" in out and "first job page:" in out
    assert db.connect(tmp_path / "jobs.db").execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_check_adapter_help_lists_every_registered_adapter():
    sub = next(a for a in cli.build_parser()._actions if getattr(a, "choices", None) and "check-adapter" in a.choices)
    helptext = " ".join(sub.choices["check-adapter"].format_help().split())
    assert all(s.key in helptext for s in adapters.REGISTRY) and "zurich" in helptext
