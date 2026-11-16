"""Jobs located in Poland: remote or hybrid qualifies, on-site fails, unknown is conditional (goal.md 5.3.1, 2 Nov 2026).

No network, no real config, no real database. The Swiss path is covered by tests/test_location.py; the tests here also
show that a job located in Poland never calls a travel-time service and that other jobs are not touched."""
import json
from datetime import datetime, timezone

import pytest

from app import db, descriptions, report
from app.batch import build_batch, exchange_dir
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.filters.base import current_results, load_context
from app.filters.poland import fold, is_polish_job, is_polish_location, poland_location, work_mode
from app.filters.runner import filter_new_jobs, run_filters
from app.location import make_location_filter
from app.models import NormalizedJob
from app.results import import_results
from app.travel import ProviderError, Providers
from tests.test_location import FakeOsrm, FakeTransport, place

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)
FILLER = ("You will work with the business and the IT teams to define requirements, document processes and test the platform. "
          "The team reports to the head of the data office and supports several departments with analysis. ")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"]["exchange"] = str(tmp_path / "exchange")
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


class NoTravel:
    """A travel-time service that must never be called: a job in Poland needs none."""
    name = "no-travel"

    def find_station(self, query):
        raise AssertionError(f"travel-time service called for {query!r}")

    def train_minutes(self, *a):
        raise AssertionError("train service called")

    def car_minutes(self, *a):
        raise AssertionError("car service called")


@pytest.fixture
def no_travel():
    return Providers(NoTravel(), NoTravel())


def swiss_providers():
    transport = FakeTransport({"Winterthur": place("Winterthur", 47.5, 8.72), "Genève": place("Genève", 46.2, 6.14),
                               "Brig": place("Brig", 46.3, 8.0)},
                              train={"Winterthur": 25, "Genève": 170, "Brig": 170})
    return Providers(transport, FakeOsrm({"Winterthur": 20, "Genève": 140, "Brig": 140}), today=datetime(2026, 10, 31).date()), transport


def add(conn, locations, title="Business Analyst", description="", external_id="1", raw=None):
    return ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id=external_id, title=title, company="Acme", url="",
                                          locations=locations, description=description, raw=raw or {})).job_id


def decide(conn, cfg, job_id):
    return [(r.outcome, r.reason_code) for r in poland_location(load_context(conn, job_id), cfg)]


# --- which locations are Polish -------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Poland", "POLAND", "Polska", "Polen", "Warsaw", "Warszawa", "Kraków", "Krakow", "KRAKÓW", "Cracow", "Wrocław", "Wroclaw",
    "Gdańsk", "Gdansk", "Poznań", "Poznan", "Łódź", "Lodz", "łódź", "Szczecin", "Katowice", "Lublin",
    "Warsaw, Masovian Voivodeship, Poland", "Kraków, Lesser Poland, Poland", "Wrocław, Lower Silesian Voivodeship, Poland",
    "Poland - Kraków", "Poland (Remote)", "Greater Warsaw", "Gdynia, Pomeranian, Poland",
])
def test_polish_locations_are_recognised_with_and_without_diacritics(cfg, text):
    assert is_polish_location(text, cfg)


@pytest.mark.parametrize("text", [
    "", "Zürich", "Switzerland", "Basel, Switzerland", "Winterthur", "Remote", "Berlin, Germany", "Warsaw, Germany",
    "Lublin, Austria", "Poznań, United Kingdom", "Polish Street Zürich"])
def test_other_places_are_not_polish(cfg, text):
    assert not is_polish_location(text, cfg)


def test_diacritics_are_folded_including_the_stroked_l():
    assert fold("Łódź") == "lodz" and fold("Wrocław") == "wroclaw" and fold("Kraków") == "krakow" and fold("Białystok") == "bialystok"


def test_a_job_is_polish_only_when_every_location_is(conn, cfg):
    ctx = lambda locs: load_context(conn, add(conn, locs, external_id=str(len(locs)) + "".join(locs)))
    assert is_polish_job(ctx(["Warsaw, Poland"]), cfg)
    assert is_polish_job(ctx(["Warsaw", "Kraków"]), cfg)
    assert not is_polish_job(ctx(["Zürich", "Warsaw"]), cfg)           # mixed: the travel-time rule decides, as before
    assert not is_polish_job(ctx([]), cfg)


def test_the_rule_can_be_switched_off(conn, cfg):
    jid = add(conn, ["Warsaw, Poland"], raw={"work_mode": "Remote"})
    cfg["location"]["poland"]["enabled"] = False
    assert decide(conn, cfg, jid) == []


# --- the work mode ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label, expected", [
    ("Remote", ("pass", "poland_remote")), ("Hybrid", ("conditional", "poland_hybrid_presence_unknown")),
    ("On-site", ("fail", "poland_on_site"))])
def test_linkedin_work_mode_decides(conn, cfg, label, expected):
    jid = add(conn, ["Warsaw, Masovian Voivodeship, Poland"], raw={"work_mode": label})
    assert decide(conn, cfg, jid) == [expected]


def test_the_linkedin_label_comes_before_the_text(conn, cfg):
    jid = add(conn, ["Poland"], title="Analyst (fully remote)", description="Fully remote.", raw={"work_mode": "On-site"})
    assert decide(conn, cfg, jid) == [("fail", "poland_on_site")]


def test_no_work_mode_is_a_conditional_pass(conn, cfg):
    jid = add(conn, ["Poland"], description=FILLER)
    (r,) = poland_location(load_context(conn, jid), cfg)
    assert (r.outcome, r.reason_code) == ("conditional", "poland_work_mode_unknown") and "not stated" in r.comment


@pytest.mark.parametrize("text, expected", [
    ("This is a fully remote position.", "poland_remote"),
    ("Remote with occasional office visits in Warsaw.", "poland_remote"),            # the user's reading: still remote
    ("Remote-first company, an office visit once a quarter.", "poland_remote"),
    ("We offer a hybrid working model.", "poland_hybrid_presence_unknown"),
    ("Hybrid: 2 days remote, 3 days in the office.", "poland_hybrid_presence_unknown"),               # hybrid is tried before remote
    ("You work 2 days in the office and the rest from home.", "poland_hybrid_presence_unknown"),
    ("Position based on-site in our Warsaw office.", "poland_on_site"),
    ("This role is office-based; the team sits together.", "poland_on_site"),
    ("Praca zdalna z okazjonalnymi wizytami w biurze.", "poland_remote"),
    ("Oferujemy pracę hybrydową, 3 dni w biurze.", "poland_hybrid_presence_unknown"),
    ("Praca stacjonarna w Warszawie.", "poland_on_site"),
    ("Tryb pracy: zdalny", "poland_remote"),
])
def test_patterns_in_the_description(conn, cfg, text, expected):
    jid = add(conn, ["Warsaw, Poland"], description=FILLER + text)
    assert decide(conn, cfg, jid)[0][1] == expected


def test_a_title_or_place_text_counts_before_the_description(conn, cfg):
    jid = add(conn, ["Warsaw (Remote)"], description=FILLER + "The team works on-site on Mondays.")
    assert decide(conn, cfg, jid) == [("pass", "poland_remote")]
    jid2 = add(conn, ["Poland"], title="Hybrid Business Analyst", description=FILLER + "On-site presence is not needed.", external_id="2")
    assert decide(conn, cfg, jid2) == [("conditional", "poland_hybrid_presence_unknown")]


@pytest.mark.parametrize("text, expected", [
    ("This is an on-site role. Remote work is not possible.", "poland_on_site"),
    ("On-site only, no remote work.", "poland_on_site"),
    ("Without remote option, in-office position.", "poland_on_site"),
    ("Brak pracy zdalnej, praca stacjonarna.", "poland_on_site"),
])
def test_a_negated_remote_is_not_remote(conn, cfg, text, expected):
    jid = add(conn, ["Warsaw, Poland"], description=FILLER + text)
    assert decide(conn, cfg, jid)[0][1] == expected


def test_hybrid_cloud_is_not_a_work_mode(conn, cfg):
    jid = add(conn, ["Kraków, Poland"], description=FILLER + "You will document our hybrid cloud platform.")
    assert decide(conn, cfg, jid) == [("conditional", "poland_work_mode_unknown")]
    assert work_mode(load_context(conn, jid), cfg)[0] is None


def test_the_comment_names_where_the_mode_was_read(conn, cfg):
    jid = add(conn, ["Warsaw, Poland"], raw={"work_mode": "Hybrid"})
    (r,) = poland_location(load_context(conn, jid), cfg)
    assert "located in Poland" in r.comment and "hybrid" in r.comment and "LinkedIn work mode 'Hybrid'" in r.comment


# --- the location filter and the runner -----------------------------------------------------------------------------

def test_a_job_in_poland_needs_no_travel_time_service(conn, cfg, no_travel):
    jid = add(conn, ["Warsaw, Poland"], raw={"work_mode": "Remote"})
    (r,) = make_location_filter(conn, no_travel, NOW)(load_context(conn, jid), cfg)
    assert (r.filter, r.outcome, r.reason_code) == ("location", "pass", "poland_remote")


def test_an_on_site_job_in_poland_is_archived_like_other_location_failures(conn, cfg, no_travel):
    jid = add(conn, ["Wrocław, Lower Silesian Voivodeship, Poland"], raw={"work_mode": "On-site"})
    counts = filter_new_jobs(conn, cfg, providers=no_travel)
    assert counts == {"pass": 0, "conditional": 0, "fail": 1}
    row = conn.execute("SELECT * FROM job_archive").fetchone()
    assert (row["rejection_stage"], row["reason_code"], row["original_job_id"]) == ("filter", "poland_on_site", jid)
    assert "on-site" in row["comment"] and "only remote or hybrid" in row["comment"]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_remote_hybrid_and_unknown_jobs_in_poland_stay_open(conn, cfg, no_travel):
    ids = {k: add(conn, ["Poland"], title=f"Analyst {k}", external_id=k, raw=raw)
           for k, raw in (("r", {"work_mode": "Remote"}), ("h", {"work_mode": "Hybrid"}), ("u", {}))}
    counts = filter_new_jobs(conn, cfg, providers=no_travel)
    assert counts == {"pass": 1, "conditional": 2, "fail": 0}
    got = {k: conn.execute("SELECT outcome, reason_code FROM job_filter_results WHERE job_id = ? AND filter = 'location'", (j,)).fetchone()
           for k, j in ids.items()}
    assert [tuple(got[k]) for k in "rhu"] == [("pass", "poland_remote"), ("conditional", "poland_hybrid_presence_unknown"), ("conditional", "poland_work_mode_unknown")]
    assert conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 0


# --- other countries and Switzerland stay as they were ---------------------------------------------------------------

def test_swiss_jobs_are_decided_by_travel_time_whatever_the_text_says(conn, cfg):
    providers, transport = swiss_providers()
    near = add(conn, ["Winterthur"], description=FILLER + "Our sister team in Warsaw, Poland is on-site only.", raw={"work_mode": "On-site"})
    far = add(conn, ["Brig"], description=FILLER + "Work remotely from Poland if you like.", external_id="2", raw={"work_mode": "Remote"})
    f = make_location_filter(conn, providers, NOW)
    (r_near,), (r_far,) = f(load_context(conn, near), cfg), f(load_context(conn, far), cfg)
    assert (r_near.outcome, r_near.reason_code) == ("pass", None) and "Winterthur" in r_near.comment
    assert (r_far.outcome, r_far.reason_code) == ("fail", "location_too_far")
    assert ("train", "Winterthur") in transport.calls                      # the travel-time service was used
    assert decide(conn, cfg, near) == [] and decide(conn, cfg, far) == []


def test_a_mixed_swiss_and_polish_job_keeps_the_travel_time_rule(conn, cfg):
    providers, transport = swiss_providers()
    jid = add(conn, ["Winterthur", "Warsaw"], raw={"work_mode": "On-site"})
    (r,) = make_location_filter(conn, providers, NOW)(load_context(conn, jid), cfg)
    assert (r.outcome, r.reason_code) == ("pass", None) and ("find", "Winterthur") in transport.calls


def test_swiss_remote_pattern_still_gives_reason_remote(conn, cfg):
    providers, transport = swiss_providers()
    jid = add(conn, ["Zürich"], title="Analyst (fully remote)")
    (r,) = make_location_filter(conn, providers, NOW)(load_context(conn, jid), cfg)
    assert (r.outcome, r.reason_code) == ("pass", "remote") and transport.calls == []


def test_no_location_is_still_unspecified(conn, cfg, no_travel):
    jid = add(conn, [], raw={"work_mode": "Remote"})
    (r,) = make_location_filter(conn, no_travel, NOW)(load_context(conn, jid), cfg)
    assert (r.outcome, r.reason_code) == ("conditional", "location_unspecified")


def test_other_countries_are_not_poland(conn, cfg):
    providers, transport = swiss_providers()
    jid = add(conn, ["Berlin, Germany"], raw={"work_mode": "Remote"})
    (r,) = make_location_filter(conn, providers, NOW)(load_context(conn, jid), cfg)
    assert r.reason_code in ("location_unresolved", "travel_time_unavailable") and not r.reason_code.startswith("poland")


# --- a description that arrives later ---------------------------------------------------------------------------------

def _filtered_unknown_job(conn, cfg, no_travel, **kw):
    jid = add(conn, ["Poland"], title="Business Analyst", **kw)
    filter_new_jobs(conn, cfg, providers=no_travel)
    row = conn.execute("SELECT reason_code FROM job_filter_results WHERE job_id = ? AND filter = 'location'", (jid,)).fetchone()
    assert row["reason_code"] == "poland_work_mode_unknown"
    return jid


def test_a_remote_description_turns_the_conditional_pass_into_a_pass(conn, cfg, no_travel):
    jid = _filtered_unknown_job(conn, cfg, no_travel)
    conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (jid,))
    conn.commit()
    result = descriptions.apply_description(conn, cfg, jid, FILLER + "This position is fully remote, with occasional office visits.")
    assert result.outcome == "stored" and result.rescore and "Poland rule: poland_remote" in result.message
    rows = conn.execute("SELECT outcome, reason_code FROM job_filter_results WHERE job_id = ? AND filter = 'location' ORDER BY id", (jid,)).fetchall()
    assert [tuple(r) for r in rows] == [("conditional", "poland_work_mode_unknown"), ("pass", "poland_remote")]   # audit trail kept
    assert [(r["filter"], r["reason_code"]) for r in current_results(conn, jid) if r["filter"] == "location"] == [("location", "poland_remote")]
    assert conn.execute("SELECT needs_rescore FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == 1


def test_an_on_site_description_archives_the_job(conn, cfg, no_travel):
    jid = _filtered_unknown_job(conn, cfg, no_travel)
    result = descriptions.apply_description(conn, cfg, jid, FILLER + "This is an on-site role in our Warsaw office.")
    assert result.outcome == "archived" and "on-site job in Poland" in result.message
    assert conn.execute("SELECT reason_code FROM job_archive").fetchone()[0] == "poland_on_site"


def test_a_description_without_a_work_mode_changes_nothing_in_the_result(conn, cfg, no_travel):
    jid = _filtered_unknown_job(conn, cfg, no_travel)
    result = descriptions.apply_description(conn, cfg, jid, FILLER + "We value teamwork and clear communication.")
    assert result.outcome == "stored" and "poland_work_mode_unknown" in result.message
    assert [r["reason_code"] for r in current_results(conn, jid) if r["filter"] == "location"] == ["poland_work_mode_unknown"]


def test_a_description_for_a_swiss_job_does_not_run_the_location_rule(conn, cfg):
    providers, transport = swiss_providers()
    jid = add(conn, ["Winterthur"])
    filter_new_jobs(conn, cfg, providers=providers)
    before = conn.execute("SELECT COUNT(*) FROM job_filter_results WHERE filter = 'location'").fetchone()[0]
    descriptions.apply_description(conn, cfg, jid, FILLER + "This is an on-site role in Poland.")
    assert conn.execute("SELECT COUNT(*) FROM job_filter_results WHERE filter = 'location'").fetchone()[0] == before == 1


# --- recheck-poland (for jobs filtered before the rule existed) -------------------------------------------------------

def _filtered_before_the_rule(conn, cfg, no_travel, rows):
    """Jobs filtered while the Poland rule was off: a place that is only a country is `location_unspecified`."""
    off = json.loads(json.dumps(cfg))
    off["location"]["poland"]["enabled"] = False
    ids = [add(conn, ["Poland"], title=f"Analyst {k}", external_id=k, raw=raw) for k, raw in rows]
    filter_new_jobs(conn, off, providers=no_travel)
    for jid in ids:
        assert conn.execute("SELECT reason_code FROM job_filter_results WHERE job_id = ? AND filter = 'location'", (jid,)).fetchone()[0] \
            == "location_unspecified"
    return ids


def test_recheck_poland_applies_the_rule_to_jobs_filtered_before(conn, cfg, no_travel):
    remote, hybrid, onsite, unknown = _filtered_before_the_rule(
        conn, cfg, no_travel, [("1", {"work_mode": "Remote"}), ("2", {"work_mode": "Hybrid"}), ("3", {"work_mode": "On-site"}), ("4", {})])
    conn.execute("UPDATE jobs SET status = 'scored' WHERE id IN (?, ?, ?)", (remote, hybrid, unknown))
    conn.commit()
    counts = descriptions.recheck_poland(conn, cfg)
    assert counts == {"checked": 4, "changed": 4, "archived": 1, "rescore": 3, "skipped": 0}
    current = {j: [r["reason_code"] for r in current_results(conn, j) if r["filter"] == "location"] for j in (remote, hybrid, unknown)}
    assert current == {remote: ["poland_remote"], hybrid: ["poland_hybrid_presence_unknown"], unknown: ["poland_work_mode_unknown"]}
    assert conn.execute("SELECT reason_code FROM job_archive").fetchone()[0] == "poland_on_site"
    assert {r["id"] for r in conn.execute("SELECT id FROM jobs WHERE needs_rescore = 1")} == {remote, hybrid, unknown}


def test_recheck_poland_changes_nothing_the_second_time(conn, cfg, no_travel):
    _filtered_before_the_rule(conn, cfg, no_travel, [("1", {"work_mode": "Remote"})])
    descriptions.recheck_poland(conn, cfg)
    rows = conn.execute("SELECT COUNT(*) FROM job_filter_results").fetchone()[0]
    assert descriptions.recheck_poland(conn, cfg) == {"checked": 1, "changed": 0, "archived": 0, "rescore": 0, "skipped": 0}
    assert conn.execute("SELECT COUNT(*) FROM job_filter_results").fetchone()[0] == rows


def test_recheck_poland_dry_run_writes_nothing(conn, cfg, no_travel):
    ids = _filtered_before_the_rule(conn, cfg, no_travel, [("1", {"work_mode": "Remote"}), ("2", {"work_mode": "On-site"})])
    conn.execute("UPDATE jobs SET status = 'scored' WHERE id = ?", (ids[0],))
    conn.commit()
    snapshot = [tuple(r) for r in conn.execute("SELECT id, status, needs_rescore FROM jobs")], conn.execute("SELECT COUNT(*) FROM job_filter_results").fetchone()[0]
    counts = descriptions.recheck_poland(conn, cfg, dry_run=True)
    assert counts == {"checked": 2, "changed": 2, "archived": 1, "rescore": 1, "skipped": 0}
    assert ([tuple(r) for r in conn.execute("SELECT id, status, needs_rescore FROM jobs")],
            conn.execute("SELECT COUNT(*) FROM job_filter_results").fetchone()[0]) == snapshot
    assert conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 0


def test_recheck_poland_leaves_a_job_you_work_on_and_swiss_jobs_alone(conn, cfg, no_travel):
    (onsite,) = _filtered_before_the_rule(conn, cfg, no_travel, [("1", {"work_mode": "On-site"})])
    conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (onsite,))
    providers, _ = swiss_providers()
    swiss = add(conn, ["Winterthur"], external_id="9", raw={"work_mode": "On-site"})
    filter_new_jobs(conn, cfg, providers=providers)
    counts = descriptions.recheck_poland(conn, cfg)
    assert counts == {"checked": 1, "changed": 0, "archived": 0, "rescore": 0, "skipped": 1}
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (onsite,)).fetchone()[0] == "applied"
    assert conn.execute("SELECT COUNT(*) FROM job_filter_results WHERE job_id = ? AND filter = 'location'", (swiss,)).fetchone()[0] == 1


# --- batch and report -------------------------------------------------------------------------------------------------

def test_the_batch_exports_only_the_current_location_result(conn, cfg, no_travel):
    jid = _filtered_unknown_job(conn, cfg, no_travel)
    descriptions.apply_description(conn, cfg, jid, FILLER + "Fully remote.")
    info = build_batch(conn, cfg, "p", "v1", run_id="r3", now=NOW)
    (job,) = json.loads(info.path.read_text(encoding="utf-8"))["jobs"]
    assert [(f["filter"], f["reason_code"]) for f in job["filter_results"] if f["filter"] == "location"] == [("location", "poland_remote")]
    assert "poland_work_mode_unknown" not in job["conditional_reasons"]


def test_the_report_shows_remote_poland_like_a_normal_location(conn, cfg, no_travel):
    remote = add(conn, ["Poland"], title="A", external_id="1", raw={"work_mode": "Remote"})
    hybrid = add(conn, ["Poland"], title="B", external_id="2", raw={"work_mode": "Hybrid"}, description=FILLER + "Office once a month.")
    unknown = add(conn, ["Poland"], title="C", external_id="3")
    filter_new_jobs(conn, cfg, providers=no_travel)
    assert report.notes(conn, remote) == ["remote (Poland)"]
    assert report.notes(conn, hybrid) == ["hybrid (Poland)"]
    assert report.notes(conn, unknown) == ["location unclear (Poland, work mode not stated)"]
    descriptions.apply_description(conn, cfg, unknown, FILLER + "Hybrid role, office once a month.")
    assert report.notes(conn, unknown) == ["hybrid (Poland)"]            # the old "unclear" row is superseded


# --- scoring: nothing clamps a Polish posting ---------------------------------------------------------------------------

POLISH = ("Szukamy doświadczonego analityka biznesowego do zespołu. Będziesz odpowiedzialny za zbieranie i dokumentowanie "
          "wymagań oraz współpracę z działami biznesowymi i zespołem IT. Oferujemy pracę zdalną, elastyczne godziny i "
          "stabilne zatrudnienie w dużej instytucji finansowej. Wymagamy znajomości języka angielskiego i doświadczenia "
          "w pracy z danymi. Dołącz do naszego zespołu i rozwijaj się razem z nami w międzynarodowym środowisku.")


def test_a_polish_posting_is_not_clamped_to_69(conn, cfg, no_travel):
    jid = add(conn, ["Warsaw, Poland"], title="Analityk biznesowy", description=POLISH)
    filter_new_jobs(conn, cfg, providers=no_travel)
    info = build_batch(conn, cfg, "p", "v1", run_id="r4", now=NOW)
    doc = json.loads(info.path.read_text(encoding="utf-8"))
    (entry,) = doc["jobs"]
    assert "posting_language_english" not in entry                          # only German is detected, never Polish
    assert [f["reason_code"] for f in entry["filter_results"] if f["filter"] == "location"] == ["poland_remote"]
    subs = {"skills": 80, "experience": 75, "domain": 70, "seniority": 80, "location": 100}       # formula 79
    results = {"schema_version": "1.1", "run_id": "r4", "model": "m", "prompt_version": "v1", "top": [jid],
               "results": [{"job_id": jid, "overall": 79, "subscores": subs, "matches": [], "gaps": [], "red_flags": [],
                            "confidence": "high", "summary": "s", "cv_variant": "resume"}]}
    path = exchange_dir(cfg) / "results" / "r.json"
    path.write_text(json.dumps(results), encoding="utf-8")
    result = import_results(conn, cfg, path, NOW)
    assert result.status == "imported" and result.scored == [jid]
    assert conn.execute("SELECT overall FROM scores WHERE job_id = ?", (jid,)).fetchone()["overall"] == 79
    assert not any("not in English" in w for w in result.warnings)


# --- the prompt says it -------------------------------------------------------------------------------------------------

def test_the_prompt_states_the_poland_rules(cfg):
    from app.prompt import load_prompt
    text, version = load_prompt(cfg)
    assert version == "1.18.0"
    assert "`poland_remote`: 100" in text and "`poland_hybrid`: 85" in text and "`poland_work_mode_unknown`" in text
    assert "is not a red flag and not a gap" in text and "A posting written in Polish is not capped" in text
    assert "neither English nor Polish" in text and "JDG" in text


# --- configuration ---------------------------------------------------------------------------------------------------------

def test_defaults_exist_without_a_user_section(cfg):
    pl = cfg["location"]["poland"]
    assert pl["enabled"] is True
    for city in ("Warsaw", "Warszawa", "Kraków", "Wrocław", "Gdańsk", "Poznań", "Łódź", "Szczecin", "Katowice", "Lublin"):
        assert city in pl["cities"]
    assert pl["remote_patterns"] and pl["hybrid_patterns"] and pl["onsite_patterns"] and "Poland" in pl["country_names"]


def test_a_user_config_without_the_section_still_works(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("location:\n  limit_minutes: 45\n", encoding="utf-8")
    assert load_config(f)["location"]["poland"]["enabled"] is True


def test_a_user_config_can_change_one_key(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("location:\n  poland:\n    cities: ['Zakopane']\n", encoding="utf-8")
    pl = load_config(f)["location"]["poland"]
    assert pl["cities"] == ["Zakopane"] and pl["enabled"] is True and pl["remote_patterns"]


@pytest.mark.parametrize("body, message", [
    ("remote_patterns: ['(unclosed']", "location.poland.remote_patterns: invalid regular expression"),
    ("hybrid_patterns: ['[bad']", "location.poland.hybrid_patterns: invalid regular expression"),
    ("onsite_patterns: ['*oops']", "location.poland.onsite_patterns: invalid regular expression"),
    ("enabled: 'yes'", "location.poland.enabled must be true or false"),
    ("cities: 'Warsaw'", "location.poland.cities must be a list of strings"),
    ("remote_patterns: [1]", "location.poland.remote_patterns must be a list of strings"),
    ("onsite_patterns: ['']", "must not hold an empty string"),
    ("cities: []\n    country_names: []", "needs country_names or cities"),
    ("unknown_key: 1", "unknown config key: location.poland.unknown_key"),
])
def test_config_validation(tmp_path, body, message):
    f = tmp_path / "c.yaml"
    f.write_text(f"location:\n  poland:\n    {body}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=message.replace("(", r"\(")):
        load_config(f)


def test_poland_can_be_disabled_with_no_cities(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("location:\n  poland:\n    enabled: false\n    cities: []\n    country_names: []\n", encoding="utf-8")
    assert load_config(f)["location"]["poland"]["enabled"] is False


# --- office presence (v0.92.0, decided by the user on 14 Nov 2026) ------------------------------------------------------

@pytest.mark.parametrize("label", ["Hybrid", "On-site"])
@pytest.mark.parametrize("text", [
    "The office is attended once a month.", "One week per month in the office.", "We expect 1 day a week at the office.",
    "One day per week on site.", "Biuro raz w miesiącu.", "Jeden tydzień w miesiącu w biurze.", "Only occasional office visits are needed."])
def test_a_low_office_presence_passes(conn, cfg, label, text):
    jid = add(conn, ["Warsaw, Poland"], description=FILLER + text, raw={"work_mode": label})
    assert decide(conn, cfg, jid) == [("pass", "poland_hybrid")]


@pytest.mark.parametrize("label", ["Hybrid", "On-site"])
@pytest.mark.parametrize("text", [
    "You work 3 days per week in the office.", "2-5 days per week in office.", "Expect four days a week at the office.",
    "Praca 3 dni w tygodniu w biurze.", "2 – 3 days a week in the office"])
def test_a_high_office_presence_fails(conn, cfg, label, text):
    jid = add(conn, ["Warsaw, Poland"], description=FILLER + text, raw={"work_mode": label})
    assert decide(conn, cfg, jid) == [("fail", "poland_too_much_office")]


def test_remote_stays_a_pass_whatever_the_office_text_says(conn, cfg):
    jid = add(conn, ["Warsaw, Poland"], description=FILLER + "Occasionally 3 days per week in the office.", raw={"work_mode": "Remote"})
    assert decide(conn, cfg, jid) == [("pass", "poland_remote")]


def test_nothing_stated_hybrid_is_conditional_and_on_site_fails(conn, cfg):
    assert decide(conn, cfg, add(conn, ["Warsaw, Poland"], description=FILLER, raw={"work_mode": "Hybrid"})) == [
        ("conditional", "poland_hybrid_presence_unknown")]
    assert decide(conn, cfg, add(conn, ["Warsaw, Poland"], description=FILLER, raw={"work_mode": "On-site"}, external_id="2")) == [
        ("fail", "poland_on_site")]


def test_one_day_a_week_remote_is_not_a_low_presence(conn, cfg):
    jid = add(conn, ["Warsaw, Poland"], description=FILLER + "You work one day a week from home.", raw={"work_mode": "Hybrid"})
    assert decide(conn, cfg, jid) == [("conditional", "poland_hybrid_presence_unknown")]


def test_a_hybrid_description_pattern_reads_the_presence_too(conn, cfg):
    jid = add(conn, ["Warsaw, Poland"], description=FILLER + "Hybrid model, one week per month in the office.")
    assert decide(conn, cfg, jid) == [("pass", "poland_hybrid")]
