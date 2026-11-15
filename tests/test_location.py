import io
import http.client
import json
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.filters.base import load_context
from app.filters.runner import build_filters, filter_new_jobs, run_filters
from app.location import (check_location, classify, make_location_filter, normalize_place_query, resolve_place,
                          round_minutes)
from app.models import NormalizedJob
from app.parsers.ubs import parse_alert, to_job
from app.travel import (HttpFetcher, OsrmProvider, Place, ProviderError, Providers, TransportProvider,
                        approximate_minutes, next_weekday)

TRAVEL = Path(__file__).parent / "fixtures" / "travel"
UBS = Path(__file__).parent / "fixtures" / "ubs"
TODAY = date(2026, 10, 31)          # a Saturday: the train lookup date is Monday 2 Nov 2026
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


# --- helpers ----------------------------------------------------------------------------------

class Replay:
    """Replays the recorded responses; an unrecorded URL behaves like a service failure."""

    def __init__(self):
        self.index = json.loads((TRAVEL / "index.json").read_text(encoding="utf-8"))
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.index:
            raise ProviderError("not recorded")
        return json.loads((TRAVEL / self.index[url]).read_text(encoding="utf-8"))


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    return load_config()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def replay():
    return Replay()


@pytest.fixture
def providers(cfg, replay):
    p = cfg["location"]["providers"]
    return Providers(TransportProvider(replay, p["transport_url"]), OsrmProvider(replay, p["osrm_url"]), today=TODAY)


def place(name, lat, lon, sid=None, ch=True):
    return Place(name, sid or name, lat, lon, ch)


ZUERICH_HB = place("Zürich HB", 47.378176, 8.540212, "8503000")
BUELACH = place("Exampletown", 47.50000, 8.50000, "8599999")


class FakeTransport:
    name = "fake-transport"

    def __init__(self, places, train=None, search_error=False):
        self.places = {"Zürich HB": ZUERICH_HB, "Exampletown": BUELACH, **places}
        self.train = train or {}
        self.search_error = search_error
        self.calls = []

    def find_station(self, query):
        self.calls.append(("find", query))
        if self.search_error:
            raise ProviderError("down")
        return self.places.get(query)

    def train_minutes(self, origin, dest, day, arrival):
        self.calls.append(("train", dest.name))
        value = self.train.get(dest.name)
        if value is None or isinstance(value, Exception):
            raise value or ProviderError("no connection")
        return value


class FakeOsrm:
    name = "fake-osrm"

    def __init__(self, car=None):
        self.car = car or {}
        self.calls = []

    def car_minutes(self, origin, dest):
        self.calls.append(dest.name)
        value = self.car.get(dest.name)
        if value is None or isinstance(value, Exception):
            raise value or ProviderError("no route")
        return value


def fake(places, train=None, car=None, **kw):
    t, o = FakeTransport(places, train, **kw), FakeOsrm(car)
    return Providers(t, o, today=TODAY), t, o


def check(conn, providers, cfg, text, now=NOW, mode="onsite"):
    return check_location(conn, providers, cfg, text, now, mode)


# --- names, dates, rounding -------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Zürich", "Zürich"), ("Switzerland - Zürich", "Zürich"), ("Zürich, Switzerland", "Zürich"),
    ("Basel (BS)", "Basel"), ("Switzerland - Western Switzerland", "Western Switzerland"),
    ("Frankfurt am Main", "Frankfurt am Main"), ("Switzerland", ""), ("Schweiz", ""), ("", ""),
    ("  Winterthur  ", "Winterthur"), ("Germany - Frankfurt am Main", "Frankfurt am Main"),
])
def test_normalize_place_query(cfg, raw, expected):
    assert normalize_place_query(raw, cfg["location"]["countries"]) == expected


@pytest.mark.parametrize("today, expected", [
    (date(2026, 10, 31), date(2026, 11, 2)),    # Saturday -> Monday
    (date(2026, 11, 1), date(2026, 11, 2)),     # Sunday -> Monday
    (date(2026, 11, 2), date(2026, 11, 3)),     # Monday -> Tuesday
    (date(2026, 11, 6), date(2026, 11, 9)),     # Friday -> Monday
])
def test_next_weekday(today, expected):
    assert next_weekday(today) == expected


@pytest.mark.parametrize("value, expected", [(59.4, 59), (59.5, 60), (60.0, 60), (60.49, 60), (60.5, 61), (0, 0)])
def test_rounding_is_half_up(value, expected):
    assert round_minutes(value) == expected


@pytest.mark.parametrize("minutes, approx, expected", [
    (59, False, "in"), (60, False, "in"), (61, False, "out"), (None, False, "unknown"),
    (45, True, "in"), (49, True, "in"), (50, True, "near"), (60, True, "near"), (70, True, "near"), (71, True, "out"),
])
def test_classify_boundaries(minutes, approx, expected):
    assert classify(minutes, approx, 60, 10) == expected


# --- recorded answers of the real services ----------------------------------------------------

@pytest.mark.parametrize("name, outcome", [
    ("Zürich", "pass"), ("Winterthur", "pass"), ("Opfikon", "pass"), ("Thalwil", "pass"),
    ("Basel", "pass"),      # train 52 min: passes although the car takes 91
    ("Bern", "pass"),       # train 56 min
    ("Genève", "fail"), ("Lugano", "fail"), ("Frankfurt am Main", "fail"),
])
def test_recorded_answers(conn, providers, cfg, name, outcome):
    c = check(conn, providers, cfg, name)
    assert c.outcome == outcome, c.comment


def test_recorded_numbers(conn, providers, cfg):
    c = check(conn, providers, cfg, "Winterthur")
    assert (c.train.minutes, c.train.approximate, c.train.provider) == (25, False, "transport.opendata.ch")
    assert (c.car.minutes, c.car.approximate, c.car.provider) == (30, False, "router.project-osrm.org")
    assert c.car.factor == 1.25
    assert c.place.name == "Winterthur" and c.place.in_switzerland


def test_zurich_is_the_origin_itself_zero_minutes_not_approximate(conn, providers, cfg):
    c = check(conn, providers, cfg, "Zürich")
    assert (c.train.minutes, c.train.approximate) == (0, False)


def test_basel_passes_by_train_even_though_car_is_too_slow(conn, providers, cfg):
    c = check(conn, providers, cfg, "Basel")
    assert c.train.minutes <= 60 < c.car.minutes and c.outcome == "pass"


def test_unknown_name_is_unresolved_not_failed(conn, providers, cfg):
    c = check(conn, providers, cfg, "Xyzzyville Nowhere")
    assert (c.outcome, c.reason) == ("conditional", "location_unresolved")


def test_bare_country_is_unspecified(conn, providers, cfg):
    c = check(conn, providers, cfg, "Switzerland")
    assert (c.outcome, c.reason) == ("conditional", "location_unspecified")


def test_foreign_station_is_resolved_and_far(conn, providers, cfg):
    c = check(conn, providers, cfg, "Frankfurt am Main")
    assert c.place.name == "Frankfurt (Main) Hbf" and not c.place.in_switzerland
    assert (c.outcome, c.reason) == ("fail", "location_too_far")


# --- boundaries, either mode, both modes ------------------------------------------------------

PLACE = place("Town", 47.5, 8.7)


@pytest.mark.parametrize("train, outcome", [(59, "pass"), (60, "pass"), (60.4, "pass"), (60.5, "fail"), (61, "fail")])
def test_train_boundary(conn, cfg, train, outcome):
    providers, *_ = fake({"Town": PLACE}, train={"Town": train}, car={"Town": 200})
    assert check(conn, providers, cfg, "Town").outcome == outcome


@pytest.mark.parametrize("free_flow, minutes, outcome", [(47.6, 60, "pass"), (48.0, 60, "pass"), (48.4, 61, "fail"), (50, 63, "fail")])
def test_car_boundary_uses_the_traffic_factor(conn, cfg, free_flow, minutes, outcome):
    providers, *_ = fake({"Town": PLACE}, train={"Town": 200}, car={"Town": free_flow})
    c = check(conn, providers, cfg, "Town")
    assert (c.car.minutes, c.outcome) == (minutes, outcome)


def test_train_fails_but_car_passes(conn, cfg):
    providers, *_ = fake({"Town": PLACE}, train={"Town": 90}, car={"Town": 30})
    assert check(conn, providers, cfg, "Town").outcome == "pass"


def test_car_fails_but_train_passes(conn, cfg):
    providers, *_ = fake({"Town": PLACE}, train={"Town": 30}, car={"Town": 90})
    assert check(conn, providers, cfg, "Town").outcome == "pass"


def test_both_too_long_fails(conn, cfg):
    providers, *_ = fake({"Town": PLACE}, train={"Town": 61}, car={"Town": 60})     # car 60 x 1.25 = 75
    c = check(conn, providers, cfg, "Town")
    assert (c.outcome, c.reason) == ("fail", "location_too_far")
    assert "train 61 min" in c.comment and "car 75 min" in c.comment


def test_changed_limit_takes_effect(conn, cfg):
    providers, *_ = fake({"Town": PLACE}, train={"Town": 45}, car={"Town": 200})
    assert check(conn, providers, cfg, "Town").outcome == "pass"
    cfg["location"]["limit_minutes"] = 40
    assert check(conn, providers, cfg, "Town").outcome == "fail"


# --- failures: never a silent pass or fail ----------------------------------------------------

def test_station_search_down_is_conditional_unavailable(conn, cfg):
    providers, *_ = fake({}, search_error=True)
    c = check(conn, providers, cfg, "Town")
    assert (c.outcome, c.reason) == ("conditional", "travel_time_unavailable")


def test_search_error_is_not_cached(conn, cfg):
    providers, t, _ = fake({}, search_error=True)
    check(conn, providers, cfg, "Town")
    assert conn.execute("SELECT COUNT(*) FROM place_cache").fetchone()[0] == 0


def test_both_services_down_uses_a_marked_approximation(conn, cfg):
    far = place("Far", 47.5, 8.7 + 1.3)          # about 98 km east of Exampletown: approximation ~ 127 min
    providers, *_ = fake({"Far": far}, train={"Far": ProviderError("down")}, car={"Far": ProviderError("down")})
    c = check(conn, providers, cfg, "Far")
    assert (c.outcome, c.reason) == ("fail", "location_too_far")
    assert c.train.approximate and c.car.approximate and "approximate" in c.comment


def test_approximation_close_to_the_limit_is_conditional(conn, cfg):
    near = place("Near", 47.5 + 0.405, 8.7)      # about 45 km: 45 x 1.3 / 60 x 60 = 58.5 min
    providers, *_ = fake({"Near": near}, train={"Near": ProviderError("down")}, car={"Near": ProviderError("down")})
    c = check(conn, providers, cfg, "Near")
    assert (c.outcome, c.reason) == ("conditional", "travel_time_approximate")


def test_approximation_clearly_inside_is_a_marked_pass(conn, cfg):
    close = place("Close", 47.50000, 8.54)          # a few km from Exampletown: far below limit minus margin
    providers, *_ = fake({"Close": close}, train={"Close": ProviderError("down")}, car={"Close": ProviderError("down")})
    c = check(conn, providers, cfg, "Close")
    assert c.outcome == "pass" and c.train.approximate and "approximate" in c.comment


def test_approximation_is_not_cached(conn, cfg):
    far = place("Far", 47.5, 10.0)
    providers, *_ = fake({"Far": far}, train={"Far": ProviderError("down")}, car={"Far": ProviderError("down")})
    check(conn, providers, cfg, "Far")
    assert conn.execute("SELECT COUNT(*) FROM travel_cache").fetchone()[0] == 0


def test_one_service_down_other_far_stays_conditional_if_unknown(conn, cfg):
    # train answers "too far"; the car service is down and the origin cannot be resolved for an approximation
    providers, t, _ = fake({"Town": PLACE}, train={"Town": 90}, car={"Town": ProviderError("down")})
    del t.places["Exampletown"]                       # car origin unknown: no approximation possible
    c = check(conn, providers, cfg, "Town")
    assert (c.outcome, c.reason) == ("conditional", "travel_time_unavailable")


def test_approximate_minutes_formula():
    a, b = place("A", 47.0, 8.0), place("B", 47.0, 8.0 + 0.2)       # about 15.2 km
    assert approximate_minutes(a, b, 1.3, 60) == pytest.approx(15.2 * 1.3, rel=0.02)


# --- caching ----------------------------------------------------------------------------------

def test_second_check_uses_the_cache(conn, cfg):
    providers, t, o = fake({"Town": PLACE}, train={"Town": 30}, car={"Town": 20})
    check(conn, providers, cfg, "Town")
    calls_before = (len(t.calls), len(o.calls))
    c = check(conn, providers, cfg, "Town")
    assert (len(t.calls), len(o.calls)) == calls_before and c.train.cached and c.car.cached


def test_cache_expires(conn, cfg):
    providers, t, o = fake({"Town": PLACE}, train={"Town": 30}, car={"Town": 20})
    check(conn, providers, cfg, "Town")
    n = len(o.calls)
    check(conn, providers, cfg, "Town", now=NOW + timedelta(days=cfg["location"]["cache_expiry_days"] + 1))
    assert len(o.calls) == n + 1


def test_changed_traffic_factor_applies_to_cached_free_flow_time(conn, cfg):
    providers, _, o = fake({"Town": PLACE}, train={"Town": 200}, car={"Town": 40})
    assert check(conn, providers, cfg, "Town").car.minutes == 50
    cfg["location"]["car_traffic_factor"] = 1.5
    c = check(conn, providers, cfg, "Town")
    assert c.car.minutes == 60 and c.car.cached and len(o.calls) == 1


def test_unresolved_names_are_cached_too(conn, providers, cfg, replay):
    check(conn, providers, cfg, "Xyzzyville Nowhere")
    n = len(replay.calls)
    check(conn, providers, cfg, "Xyzzyville Nowhere")
    assert len(replay.calls) == n


def test_station_search_prefers_a_swiss_station_over_an_earlier_foreign_one():
    answer = {"stations": [
        {"id": "9", "name": "Zürich (D)", "coordinate": {"x": 50.0, "y": 8.0}},
        {"id": "8503000", "name": "Zürich HB", "coordinate": {"x": 47.378, "y": 8.540}}]}
    found = TransportProvider(lambda url: answer, "https://x").find_station("Zürich")
    assert found.station_id == "8503000" and found.in_switzerland


LOERRACH = {"stations": [
    {"id": "8000999", "name": "Lörrach Hbf", "coordinate": {"x": 47.61, "y": 7.67}},          # inside the box of Switzerland
    {"id": "8500090", "name": "Basel Bad Bf", "coordinate": {"x": 47.567, "y": 7.608}}]}


def test_a_german_station_inside_the_box_of_switzerland_is_not_swiss():
    german = {"stations": LOERRACH["stations"][:1]}
    found = TransportProvider(lambda url: german, "https://x").find_station("Lörrach")
    assert found.station_id == "8000999" and not found.in_switzerland
    swiss = {"stations": [{"id": "8503000", "name": "Zürich HB", "coordinate": {"x": 47.378, "y": 8.540}}]}
    assert TransportProvider(lambda url: swiss, "https://x").find_station("Zürich").in_switzerland


def test_a_german_town_next_to_the_border_resolves_as_german(conn, cfg):
    class Real(FakeTransport):
        def find_station(self, query):
            if query in ("Zürich HB", "Exampletown"):
                return super().find_station(query)
            return TransportProvider(lambda url: {"stations": LOERRACH["stations"][:1]}, "https://x").find_station(query)

    t, o = Real({}, train={"Lörrach Hbf": 150}), FakeOsrm({"Lörrach Hbf": 120})
    c = check(conn, Providers(t, o, today=TODAY), cfg, "Lörrach Baden-Württemberg Germany")
    assert c.reason != "location_unresolved" and c.place.name == "Lörrach Hbf" and not c.place.in_switzerland


def test_a_cached_place_that_is_not_swiss_by_its_id_is_read_as_not_swiss(conn, cfg):
    with conn:
        conn.execute("INSERT INTO place_cache (query, resolved, station_id, station_name, latitude, longitude, in_switzerland, "
                     "provider, resolved_at, expires_at) VALUES ('london', 1, '1402500', 'Saint-Genis, Allondon Sud', 46.2, 6.0, 1, "
                     "'x', '2026-10-31T00:00:00Z', '2027-10-31T00:00:00Z')")
    providers, *_ = fake({})
    found, status = resolve_place(conn, providers, cfg, "london", NOW)
    assert status == "ok" and found.station_id == "1402500" and not found.in_switzerland


def test_station_search_falls_back_to_the_first_foreign_station():
    answer = {"stations": [{"id": "9", "name": "Frankfurt Hbf", "coordinate": {"x": 50.1, "y": 8.66}},
                           {"id": "10", "name": "Frankfurt Süd", "coordinate": {"x": 50.09, "y": 8.69}}]}
    found = TransportProvider(lambda url: answer, "https://x").find_station("Frankfurt")
    assert found.station_id == "9" and not found.in_switzerland


def test_station_search_skips_entries_without_coordinates():
    answer = {"stations": [{"id": "1", "name": "No coords"}, {"id": "2", "name": "Bern", "coordinate": {"x": 46.95, "y": 7.44}}]}
    assert TransportProvider(lambda url: answer, "https://x").find_station("Bern").station_id == "2"


# --- provider parsing and the real fetcher ----------------------------------------------------

def test_transport_parsing_errors():
    t = TransportProvider(lambda url: {"stations": []}, "https://x")
    assert t.find_station("x") is None
    with pytest.raises(ProviderError):
        TransportProvider(lambda url: {"oops": 1}, "https://x").find_station("x")
    for bad in ({"connections": []}, {"connections": [{"duration": "weird"}]}, {}):
        with pytest.raises(ProviderError):
            TransportProvider(lambda url, b=bad: b, "https://x").train_minutes("A", ZUERICH_HB, TODAY, "08:30")


def test_transport_duration_with_days():
    t = TransportProvider(lambda url: {"connections": [{"duration": "01d02:30:30"}]}, "https://x")
    assert t.train_minutes("A", ZUERICH_HB, TODAY, "08:30") == pytest.approx(26 * 60 + 30.5)


def test_transport_url_asks_for_arrival_time_on_the_given_day():
    seen = []
    t = TransportProvider(lambda url: seen.append(url) or {"connections": [{"duration": "00d00:25:00"}]}, "https://x/v1")
    t.train_minutes("Zürich HB", ZUERICH_HB, date(2026, 11, 2), "08:30")
    assert "date=2026-11-02" in seen[0] and "time=08%3A30" in seen[0] and "isArrivalTime=1" in seen[0]


@pytest.mark.parametrize("answer", [{"code": "NoRoute", "routes": []}, {}, {"code": "Ok", "routes": []},
                                    {"code": "Ok", "routes": [{"duration": "x"}]}])
def test_osrm_errors(answer):
    with pytest.raises(ProviderError):
        OsrmProvider(lambda url: answer, "https://x").car_minutes(ZUERICH_HB, BUELACH)


def test_osrm_url_uses_longitude_first():
    seen = []
    OsrmProvider(lambda url: seen.append(url) or {"code": "Ok", "routes": [{"duration": 600}]}, "https://x").car_minutes(
        ZUERICH_HB, BUELACH)
    assert "/8.54021,47.37818;8.50000,47.50000?" in seen[0]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_fetcher_sends_user_agent_and_parses_json(monkeypatch):
    seen = {}

    def fake_open(request, timeout):
        seen["ua"], seen["timeout"] = request.get_header("User-agent"), timeout
        return _Resp(b'{"a": 1}')
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    assert HttpFetcher("Test-UA/1", delay=0, timeout=7)("https://x/y") == {"a": 1}
    assert seen == {"ua": "Test-UA/1", "timeout": 7}


@pytest.mark.parametrize("failure", [
    urllib.error.HTTPError("https://x", 429, "Too Many", {}, None), urllib.error.HTTPError("https://x", 503, "x", {}, None),
    urllib.error.URLError("no route"), TimeoutError("slow")])
def test_http_fetcher_turns_failures_into_provider_error(monkeypatch, failure):
    def boom(request, timeout):
        raise failure
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(ProviderError):
        HttpFetcher("UA", delay=0)("https://x/y")


def test_http_fetcher_rejects_bad_json(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda r, timeout: _Resp(b"<html>blocked</html>"))
    with pytest.raises(ProviderError):
        HttpFetcher("UA", delay=0)("https://x/y")


def test_http_fetcher_waits_between_requests(monkeypatch):
    sleeps = []
    monkeypatch.setattr("urllib.request.urlopen", lambda r, timeout: _Resp(b"{}"))
    monkeypatch.setattr("time.sleep", sleeps.append)
    f = HttpFetcher("UA", delay=5)
    f("https://x/1")
    f("https://x/2")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 5


def test_default_delay_between_requests_is_one_and_a_half_seconds(cfg):
    assert HttpFetcher("UA").delay == 1.5                       # OSRM asks for at most 1 request per second; keep a margin
    assert Providers.live(cfg).osrm.fetch.delay == 1.5          # config.example.yaml value reaches the fetcher


def test_live_providers_use_the_config(cfg):
    p = Providers.live(cfg)
    assert p.transport.base == "https://transport.opendata.ch/v1" and p.osrm.base == "https://router.project-osrm.org"
    assert "Recruitment-pipeline" in p.transport.fetch.user_agent


def test_config_rejects_bad_provider_settings(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("location:\n  providers:\n    transport_url: http://insecure\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="https"):
        load_config(p)


# --- the filter: jobs ---------------------------------------------------------------------------

def add(conn, locations, title="Business Analyst", description="", external_id="1", **kw):
    job = NormalizedJob(source="s", external_id=external_id, title=title, company="Acme", url="",
                        locations=locations, description=description, **kw)
    return ingest_job(conn, job).job_id


ONSITE = {"work_mode": "On-site"}       # the Swiss limit is 60 minutes for an on-site job, 61 to 120 is conditional when the mode is not stated


def run_loc(conn, cfg, providers, job_id):
    from app.filters.base import load_context
    return make_location_filter(conn, providers, NOW)(load_context(conn, job_id), cfg)


def test_job_without_location_is_conditional_unspecified(conn, cfg, providers):
    (r,) = run_loc(conn, cfg, providers, add(conn, []))
    assert (r.outcome, r.reason_code) == ("conditional", "location_unspecified")


def test_job_with_only_a_country_is_unspecified(conn, cfg, providers):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Switzerland"]))
    assert (r.outcome, r.reason_code) == ("conditional", "location_unspecified")


def test_one_qualifying_location_is_enough(conn, cfg, providers):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Genève", "Zürich"]))      # UBS job 352530
    assert r.outcome == "pass" and "Zürich HB" in r.comment


def test_no_qualifying_location_fails(conn, cfg, providers):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Genève", "Lugano"], raw=ONSITE))
    assert (r.outcome, r.reason_code) == ("fail", "location_too_far")
    assert "Genève" in r.comment and "Lugano" in r.comment


def test_far_location_plus_unresolved_one_stays_conditional(conn, cfg, providers):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Genève", "Xyzzyville Nowhere"]))
    assert (r.outcome, r.reason_code) == ("conditional", "location_unresolved")


def test_location_variants_are_checked_once(conn, cfg, providers, replay):
    run_loc(conn, cfg, providers, add(conn, ["Switzerland - Winterthur", "Winterthur, Switzerland"]))
    assert sum("query=Winterthur" in u for u in replay.calls) == 1


@pytest.mark.parametrize("title", ["Reporting Analyst (fully remote)", "Analyst - 100% remote", "Analyst, work from anywhere"])
def test_fully_remote_passes_without_a_check(conn, cfg, providers, replay, title):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Lugano"], title=title))
    assert (r.outcome, r.reason_code) == ("pass", "remote") and replay.calls == []


@pytest.mark.parametrize("description", [
    "This role is not fully remote.", "No 100% remote work possible.", "Non-remote-first culture.",
    "Without work from anywhere option.", "Die Stelle ist nicht vollständig remote.",
    "Kein vollständig remote Modell.", "Ohne work from anywhere."])
def test_negated_remote_mention_is_not_remote(conn, cfg, providers, description):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Lugano"], description=description, raw=ONSITE))
    assert (r.outcome, r.reason_code) == ("fail", "location_too_far")


@pytest.mark.parametrize("description", [
    "This role is not offered as fully remote.", "There is no option for 100% remote work.",
    "Kein Angebot für vollständig remote.", "Fully remote work is not possible.", "100% remote is not an option.",
    "Vollständig remote ist leider nicht möglich.", "Work from anywhere: not available."])
def test_remote_negations_in_longer_phrasings_are_not_remote(conn, cfg, providers, description):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Lugano"], description=description, raw=ONSITE))
    assert (r.outcome, r.reason_code) == ("fail", "location_too_far")


@pytest.mark.parametrize("description", [
    "Fully remote, no travel needed.", "No travel needed, fully remote.", "We are fully remote and not planning an office."])
def test_remote_with_an_unrelated_negation_nearby_still_passes(conn, cfg, providers, description):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Lugano"], description=description))
    assert (r.outcome, r.reason_code) == ("pass", "remote")


def test_plain_remote_in_the_description_still_passes(conn, cfg, providers, replay):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Lugano"], description="This is a fully remote position."))
    assert (r.outcome, r.reason_code) == ("pass", "remote") and replay.calls == []


def test_hybrid_checks_the_office_location(conn, cfg, providers):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Lugano"], description="This is a hybrid role, 2 days remote."))
    assert (r.outcome, r.reason_code) == ("pass", "location_hybrid")        # Lugano is 115 minutes away: within the hybrid limit
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Lugano"], description="A business analyst role.", external_id="3", title="Third Analyst"))
    assert (r.outcome, r.reason_code) == ("conditional", "travel_time_work_mode_unknown")        # unknown mode: 61 to 120 is conditional
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Zürich"], description="Hybrid working model.", external_id="2",
                                              title="Other Analyst"))
    assert r.outcome == "pass"


def test_hybrid_without_office_location_is_conditional(conn, cfg, providers):
    (r,) = run_loc(conn, cfg, providers, add(conn, [], description="Hybrid working model."))
    assert (r.outcome, r.reason_code) == ("conditional", "location_unspecified")


def test_calculation_is_stored_per_location_and_mode(conn, cfg, providers):
    jid = add(conn, ["Winterthur"])
    run_loc(conn, cfg, providers, jid)
    loc = conn.execute("SELECT * FROM job_locations WHERE job_id=?", (jid,)).fetchone()
    assert loc["resolved"] == 1 and loc["latitude"] == pytest.approx(47.5, abs=0.1)
    rows = {r["mode"]: r for r in conn.execute("SELECT * FROM job_travel_checks WHERE location_id=?", (loc["id"],))}
    assert set(rows) == {"train", "car"}
    assert (rows["train"]["origin"], rows["train"]["destination"], rows["train"]["minutes"]) == ("Zürich HB", "Winterthur", 25)
    assert (rows["car"]["origin"], rows["car"]["minutes"], rows["car"]["provider"]) == ("Exampletown", 30, "router.project-osrm.org")
    assumptions = json.loads(rows["car"]["assumptions"])
    assert assumptions["traffic_factor"] == 1.25 and assumptions["arrival"] == "08:30" and assumptions["limit_minutes"] == 60


# --- runner -------------------------------------------------------------------------------------

def test_without_providers_the_location_rule_is_not_applied(conn, cfg):
    jid = add(conn, ["Lugano"])
    assert run_filters(conn, jid, cfg) == "pass"
    assert len(build_filters(conn)) == 4


def test_runner_archives_a_far_job_with_its_travel_checks(conn, cfg, providers):
    jid = add(conn, ["Lugano"], raw=ONSITE)
    assert run_filters(conn, jid, cfg, providers=providers) == "fail"
    row = conn.execute("SELECT * FROM job_archive").fetchone()
    assert (row["reason_code"], row["rejection_stage"]) == ("location_too_far", "filter")
    record = json.loads(row["record"])
    checks = record["locations"][0]["travel_checks"]
    assert {c["mode"] for c in checks} == {"train", "car"} and all(c["minutes"] > 60 for c in checks)


def test_runner_keeps_a_conditional_job(conn, cfg, providers):
    jid = add(conn, ["Xyzzyville Nowhere"])
    assert run_filters(conn, jid, cfg, providers=providers) == "conditional"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    row = conn.execute("SELECT outcome, reason_code FROM job_filter_results WHERE filter='location'").fetchone()
    assert tuple(row) == ("conditional", "location_unresolved")


def test_no_web_calls_for_a_job_that_already_failed(conn, cfg, providers, replay):
    jid = add(conn, ["Zürich"], title="Head of Data")
    assert run_filters(conn, jid, cfg, providers=providers) == "fail"
    assert replay.calls == []
    assert conn.execute("SELECT reason_code FROM job_archive").fetchone()[0] == "seniority_title_pattern"


def test_real_alerts_with_recorded_travel_times(conn, cfg, providers):
    for name in sorted(p.name for p in UBS.glob("*.html")):
        for j in parse_alert((UBS / name).read_text(encoding="utf-8")).jobs:
            ingest_job(conn, to_job(j, message_id=name))
    counts = filter_new_jobs(conn, cfg, providers=providers)
    assert counts == {"pass": 7, "conditional": 0, "fail": 2}       # the 2 are the Director codes, as before
    for row in conn.execute("SELECT job_id, outcome FROM job_filter_results WHERE filter='location'"):
        assert row["outcome"] == "pass"


# --- timetable gives nothing: train estimated from the exact car time (goal.md 5.3.1, v0.68.0) ------------------

@pytest.mark.parametrize("train_error", [
    ProviderError("HTTP 503 from transport.opendata.ch"), ProviderError("no train connection returned"),
    ProviderError("unreadable duration 'x'")], ids=["service down", "no connection", "unreadable duration"])
def test_timetable_failure_uses_car_estimate(conn, cfg, train_error):
    providers, *_ = fake({"Town": PLACE}, train={"Town": train_error}, car={"Town": 40})        # car 40 x 1.25 = 50
    c = check(conn, providers, cfg, "Town")
    assert (c.train.minutes, c.train.approximate, c.train.provider) == (50, True, "car estimate")
    assert c.car.minutes == 50 and not c.car.approximate and "train 50 min (approximate)" in c.comment
    assert c.outcome == "pass"                                  # the exact car time passes


def test_car_estimate_uses_train_car_factor(conn, cfg):
    cfg["location"]["car_traffic_factor"] = 2           # car 80, but the train estimate is free-flow 40 x train_car_factor
    providers, *_ = fake({"Town": PLACE}, train={"Town": ProviderError("down")}, car={"Town": 40})
    c = check(conn, providers, cfg, "Town")
    assert (c.car.minutes, c.train.minutes) == (80, 50)
    assert (c.outcome, c.reason) == ("conditional", "travel_time_approximate")      # 50 is within 10 min of the limit
    cfg["location"]["train_car_factor"] = 1.0
    assert check(conn, providers, cfg, "Town").train.minutes == 40


def test_car_estimate_not_cached_stored_with_factor(conn, cfg):
    providers, *_ = fake({"Town": PLACE}, train={"Town": ProviderError("down")}, car={"Town": 40})
    check(conn, providers, cfg, "Town")
    assert [r["mode"] for r in conn.execute("SELECT mode FROM travel_cache")] == ["car"]
    jid = ingest_job(conn, NormalizedJob(source="linkedin_alert", external_id="1", title="Analyst", company="A",
                                         locations=["Town"])).job_id
    make_location_filter(conn, providers, NOW)(load_context(conn, jid), cfg)
    row = conn.execute("SELECT t.minutes, t.approximate, t.provider, t.assumptions FROM job_travel_checks t "
                       "WHERE t.mode = 'train'").fetchone()
    assert (row["minutes"], row["approximate"], row["provider"]) == (50, 1, "car estimate")
    assert json.loads(row["assumptions"])["traffic_factor"] == 1.25


def test_timetable_time_is_kept_when_it_answers(conn, cfg):
    providers, *_ = fake({"Town": PLACE}, train={"Town": 30}, car={"Town": 40})
    c = check(conn, providers, cfg, "Town")
    assert (c.train.minutes, c.train.approximate, c.train.provider) == (30, False, "fake-transport")


def test_both_down_keeps_the_straight_line_approximation(conn, cfg):
    far = place("Far", 47.5, 8.7 + 1.3)
    providers, *_ = fake({"Far": far}, train={"Far": ProviderError("down")}, car={"Far": ProviderError("down")})
    c = check(conn, providers, cfg, "Far")
    assert c.train.provider == "approximation" and c.car.provider == "approximation"


def test_car_estimate_far_fails(conn, cfg):
    providers, *_ = fake({"Town": PLACE}, train={"Town": ProviderError("down")}, car={"Town": 90})      # 90 x 1.25 = 113
    c = check(conn, providers, cfg, "Town")
    assert (c.train.minutes, c.outcome, c.reason) == (113, "fail", "location_too_far")


@pytest.mark.parametrize("text, message", [
    ("location:\n  train_car_factor: 0.5\n", "train_car_factor"), ("location:\n  train_car_factor: abc\n", "train_car_factor")])
def test_train_car_factor_validation(tmp_path, text, message):
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)
    assert load_config()["location"]["train_car_factor"] == 1.25


# --- text that names no place, and comma-less LinkedIn locations (v0.79.1) ------------------------------------

@pytest.mark.parametrize("raw", ["Remote", "Remote, Switzerland", "Remote - Switzerland", "Central Switzerland", "Hybrid"])
def test_normalize_place_query_gives_nothing_for_a_text_that_names_no_place(cfg, raw):
    assert normalize_place_query(raw, cfg["location"]["countries"]) == ""


@pytest.mark.parametrize("raw, expected", [
    ("Mannheim Baden Wurttemberg Germany", "Mannheim"), ("Zurich Zurich Switzerland", "Zurich"),
    ("Lodz Lodz Poland", "Lodz"), ("Karlstein am Main Bavaria Germany", "Karlstein am Main"), ("Frankfurt am Main", "Frankfurt am Main"),
    ("Frankfurt am Main Germany", "Frankfurt am Main"), ("Zürich", "Zürich"),
    ("Basel (BS), Switzerland", "Basel"), ("Switzerland - Zürich", "Zürich"), ("Switzerland - Western Switzerland", "Western Switzerland"),
])
def test_normalize_place_query_drops_a_trailing_country_of_a_comma_less_text(cfg, raw, expected):
    assert normalize_place_query(raw, cfg["location"]["countries"]) == expected


def test_remote_in_switzerland_is_unspecified_and_no_station_is_searched(conn, cfg):
    providers, transport, _ = fake({"Remote": place("Remiremont", 48.0, 6.6, ch=False)})
    c = check(conn, providers, cfg, "Remote, Switzerland")
    assert (c.outcome, c.reason) == ("conditional", "location_unspecified")
    assert [call for call in transport.calls if call[0] == "find"] == []


def test_a_foreign_station_for_a_swiss_text_is_unresolved(conn, cfg):
    foreign = place("Baden-Baden Rebland", 48.7, 8.2, ch=False)
    providers, *_ = fake({"Baden Aargau Switzerland": foreign, "Baden Aargau": foreign}, train={"Baden-Baden Rebland": 200})
    c = check(conn, providers, cfg, "Baden Aargau Switzerland")
    assert (c.outcome, c.reason) == ("conditional", "location_unresolved")


def test_a_comma_less_text_falls_back_to_its_leading_word(conn, cfg):
    mannheim = place("Mannheim Hbf", 49.48, 8.47, ch=False)
    providers, *_ = fake({"Mannheim": mannheim}, train={"Mannheim Hbf": 150}, car={"Mannheim Hbf": 120})
    c = check(conn, providers, cfg, "Mannheim Baden Wurttemberg Germany")
    assert c.place.name == "Mannheim Hbf" and (c.outcome, c.reason) == ("fail", "location_too_far")


@pytest.mark.parametrize("failure", [http.client.IncompleteRead(b"", 10), http.client.BadStatusLine("x")])
def test_http_fetcher_turns_http_client_errors_into_provider_error(monkeypatch, failure):
    class Truncated(_Resp):
        def read(self):
            raise failure

    def fake_open(request, timeout):
        if isinstance(failure, http.client.BadStatusLine):
            raise failure
        return Truncated(b"")
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    with pytest.raises(ProviderError):
        HttpFetcher("UA", delay=0)("https://x/y")


@pytest.mark.parametrize("text", ["- No night shifts\n- Fully remote work", "Benefits\nNo relocation needed\nFully remote",
                                   "No travel. Fully remote."])
def test_a_negation_in_an_earlier_line_or_sentence_does_not_reach_the_remote_mention(text):
    from app.location import _not_negated
    assert _not_negated(r"fully remote", text) is True
    assert _not_negated(r"fully remote", "Hybrid, non-remote culture, not fully remote") is False


# --- v0.92.0 location rules (decided by the user on 14 Nov 2026) -------------------------------

@pytest.mark.parametrize("text, station", [("Berne, CH", "Bern"), ("Lucerne, Switzerland", "Luzern"), ("Basle", "Basel"),
                                           ("Neuchatel", "Neuchâtel"), ("Bâle", "Basel")])
def test_place_aliases_are_looked_up_under_the_timetable_name(conn, cfg, text, station):
    providers, transport, _ = fake({station: place(station, 47.0, 7.4)}, train={station: 40}, car={station: 30})
    c = check(conn, providers, cfg, text)
    assert (c.outcome, c.query) == ("pass", station)
    assert ("find", station) in transport.calls


@pytest.mark.parametrize("text", ["Vaduz, Liechtenstein", "Bendern, Liechtenstein", "Bendern Liechtenstein", "Vaduz, LI", "Vaduz CH"])
def test_liechtenstein_and_ch_count_as_swiss(conn, cfg, text):
    name = text.replace(",", "").split()[0]
    providers, *_ = fake({name: place(name, 47.1, 9.5)}, train={name: 50}, car={name: 40})
    c = check(conn, providers, cfg, text)
    assert (c.outcome, c.place.name) == ("pass", name)


@pytest.mark.parametrize("locations", [["Remote"], ["Fully remote"], ["Remote, Switzerland"], ["Remote, CH"],
                                       ["Remote", "Fully remote, Switzerland"]])
def test_remote_only_locations_pass_as_remote(conn, cfg, providers, replay, locations):
    (r,) = run_loc(conn, cfg, providers, add(conn, locations))
    assert (r.outcome, r.reason_code) == ("pass", "remote") and replay.calls == []


def test_remote_next_to_a_real_place_is_not_remote(conn, cfg, providers):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Remote", "Lugano"]))
    assert r.reason_code != "remote"


@pytest.mark.parametrize("text", ["Carouge, Switzerland", "Geneva", "Genf", "Genève", "Meyrin", "Thônex, Switzerland",
                                  "Le Grand-Saconnex"])
def test_geneva_canton_fails_without_a_travel_call(conn, cfg, text):
    providers, transport, osrm = fake({"Genève": place("Genève", 46.2, 6.14)}, train={"Genève": 30}, car={"Genève": 20})
    c = check(conn, providers, cfg, text)
    assert (c.outcome, c.reason) == ("fail", "location_geneva_canton")
    assert [call for call in transport.calls if call[0] == "train"] == [] and osrm.calls == []


def test_geneva_canton_station_name_fails_even_with_a_30_minute_station(conn, cfg):
    # the query is not a listed name, the station it finds is: the part of its name before the comma matches
    providers, transport, osrm = fake({"Rondeau": place("Carouge, Rondeau", 46.18, 6.14)}, train={"Carouge, Rondeau": 30},
                                      car={"Carouge, Rondeau": 20})
    c = check(conn, providers, cfg, "Rondeau")
    assert (c.outcome, c.reason) == ("fail", "location_geneva_canton")
    assert [call for call in transport.calls if call[0] == "train"] == [] and osrm.calls == []


def test_a_fully_remote_job_in_geneva_still_passes(conn, cfg, providers):
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Geneva"], description="This is a fully remote role."))
    assert (r.outcome, r.reason_code) == ("pass", "remote")


@pytest.mark.parametrize("minutes, car_only, outcome, reason", [
    (120, False, "pass", "location_hybrid"), (121, False, "fail", "location_too_far"),
    (60, False, "pass", None), (90, True, "pass", "location_hybrid"), (121, True, "fail", "location_too_far")])
def test_hybrid_limit_is_120_for_train_or_car(conn, cfg, minutes, car_only, outcome, reason):
    if car_only:        # the car time is the free-flow time x 1.25
        providers, *_ = fake({"Aarau": place("Aarau", 47.4, 8.0)}, train={"Aarau": 500}, car={"Aarau": minutes / 1.25})
    else:
        providers, *_ = fake({"Aarau": place("Aarau", 47.4, 8.0)}, train={"Aarau": minutes}, car={"Aarau": 400})
    c = check_location(conn, providers, cfg, "Aarau", NOW, "hybrid")
    assert (c.outcome, c.reason, c.limit) == (outcome, reason, 120)


def test_on_site_limit_stays_60(conn, cfg):
    providers, *_ = fake({"Aarau": place("Aarau", 47.4, 8.0)}, train={"Aarau": 61}, car={"Aarau": 400})
    c = check_location(conn, providers, cfg, "Aarau", NOW, "onsite")
    assert (c.outcome, c.reason, c.limit) == ("fail", "location_too_far", 60)


@pytest.mark.parametrize("train, expected", [(60, ("pass", None)), (61, ("conditional", "travel_time_work_mode_unknown")),
                                              (120, ("conditional", "travel_time_work_mode_unknown")),
                                              (121, ("fail", "location_too_far"))])
def test_unknown_work_mode(conn, cfg, train, expected):
    providers, *_ = fake({"Aarau": place("Aarau", 47.4, 8.0)}, train={"Aarau": train}, car={"Aarau": 400})
    c = check_location(conn, providers, cfg, "Aarau", NOW)
    assert (c.outcome, c.reason) == expected


def test_unknown_work_mode_by_car_is_conditional_too(conn, cfg):
    providers, *_ = fake({"Aarau": place("Aarau", 47.4, 8.0)}, train={"Aarau": 400}, car={"Aarau": 80})       # car 100 min
    c = check_location(conn, providers, cfg, "Aarau", NOW)
    assert (c.outcome, c.reason) == ("conditional", "travel_time_work_mode_unknown")


@pytest.mark.parametrize("kw, description, expected", [
    ({"raw": {"work_mode": "Hybrid"}}, "", ("pass", "location_hybrid")),
    ({"raw": {"work_mode": "Remote"}}, "", ("pass", "location_hybrid")),          # a Swiss LinkedIn "Remote" is hybrid, not remote
    ({"raw": {"work_mode": "On-site"}}, "", ("fail", "location_too_far")),
    ({}, "", ("conditional", "travel_time_work_mode_unknown")),
    ({}, "Hybrid working model, 4 days in the office.", ("pass", "location_hybrid")),      # office days never reject a Swiss job
])
def test_the_filter_reads_the_work_mode_once_per_job(conn, cfg, kw, description, expected):
    providers, *_ = fake({"Aarau": place("Aarau", 47.4, 8.0)}, train={"Aarau": 100}, car={"Aarau": 400})
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Aarau"], description=description, **kw))
    assert (r.outcome, r.reason_code) == expected


def test_the_limit_and_work_mode_used_are_stored_in_the_assumptions(conn, cfg):
    providers, *_ = fake({"Aarau": place("Aarau", 47.4, 8.0)}, train={"Aarau": 100}, car={"Aarau": 400})
    run_loc(conn, cfg, providers, add(conn, ["Aarau"], raw={"work_mode": "Hybrid"}))
    rows = conn.execute("SELECT assumptions FROM job_travel_checks").fetchall()
    assert rows and all(json.loads(r[0])["limit_minutes"] == 120 and json.loads(r[0])["work_mode"] == "hybrid" for r in rows)


def test_a_job_in_warsaw_makes_no_provider_call(conn, cfg):
    providers, transport, osrm = fake({})
    (r,) = run_loc(conn, cfg, providers, add(conn, ["Warsaw, PL"], raw={"work_mode": "Hybrid"}, description="Office once a month."))
    assert r.outcome == "pass" and transport.calls == [] and osrm.calls == []


def test_the_new_config_keys_are_validated(cfg):
    from app.config import validate
    for path, value, key in ((("hybrid_limit_minutes",), 30, "hybrid_limit_minutes"), (("place_aliases",), {"berne": ""}, "place_aliases"),
                             (("poland", "high_presence_patterns"), ["("], "high_presence_patterns"),
                             (("geneva_canton_places",), [""], "geneva_canton_places")):
        bad = json.loads(json.dumps(cfg))
        target = bad["location"]
        for k in path[:-1]:
            target = target[k]
        target[path[-1]] = value
        with pytest.raises(ConfigError, match=key):
            validate(bad)
    assert len(cfg["location"]["geneva_canton_places"]) == 45


def _cache_place(conn, query, p):
    with conn:
        conn.execute("INSERT INTO place_cache (query, resolved, station_id, station_name, latitude, longitude, in_switzerland, provider, "
                     "resolved_at, expires_at) VALUES (?, 1, ?, ?, ?, ?, 1, 'x', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')",
                     (query.casefold(), p.station_id, p.name, p.latitude, p.longitude))


def test_an_error_on_the_whole_query_still_tries_the_leading_word(conn, cfg):
    baden = place("Baden", 47.47, 8.31, "8503505")
    _cache_place(conn, "baden", baden)
    providers = Providers.offline()
    providers.today = TODAY
    c = check(conn, providers, cfg, "Baden Aargau Switzerland")
    assert c.place is not None and c.place.name == "Baden" and c.query == "Baden Aargau"


def test_an_error_on_both_candidates_stays_unavailable(conn, cfg):
    providers = Providers.offline()
    c = check(conn, providers, cfg, "Baden Aargau Switzerland")
    assert (c.outcome, c.reason) == ("conditional", "travel_time_unavailable")
