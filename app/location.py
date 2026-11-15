"""Location filter and travel-time calculation (goal.md 5.3.1).

A job passes when, for at least one location it names, the train time from Zürich HB (station to
station) or the car time from Exampletown is at most `limit_minutes`; either mode is enough.

Outcomes, all stored with a reason code:
  pass         a qualifying location (or a fully remote offer, reason `remote`)
  fail         every named location is too far in both modes             location_too_far
  conditional  nothing named / a bare country                            location_unspecified
               a name no station matches                                 location_unresolved
               a service did not answer and no approximation is possible travel_time_unavailable
               only an approximation, within the near-limit margin       travel_time_approximate
Work mode (decided by the user on 14 Nov 2026; read once per job with filters/poland.work_mode, a Swiss "remote" counts
as hybrid): on-site 60 minutes (train or car), hybrid `hybrid_limit_minutes` (120; a pass above 60 has reason `location_hybrid`),
not stated: up to 60 passes, 61 to 120 is conditional `travel_time_work_mode_unknown`, above 120 fails. Office days never reject.
A place in the canton of Geneva (`geneva_canton_places`) fails `location_geneva_canton` before any travel call; a fully remote
offer (or only "Remote" locations) passes first. `place_aliases` map 'Berne' to 'Bern' before the station search.
No location means unspecified.
A job located only in Poland is decided before all of this by its work mode (app/filters/poland.py, goal.md 5.3.1,
decided 2 Nov 2026): remote `poland_remote` and hybrid `poland_hybrid` pass, on-site `poland_on_site` fails, an unknown
work mode is conditional `poland_work_mode_unknown`. No web call is made for such a job.

Rounding: a time is rounded to the nearest whole minute (half up) before it is compared; 60 passes, 61 fails.
Car time = OSRM free-flow time x `car_traffic_factor`. The cache holds the free-flow time, so a change of
the factor takes effect at once. The car origin is the station of `car_origin_town`: the street address in
the private config is NOT sent to any service (it would need a geocoder, and the address is personal data).
Train fallback (decided by the user 3 Nov 2026): the real timetable time is always used when the timetable answers. Only
when the timetable call fails (service down, no connection, unreadable duration) and an exact routed car time exists
(OSRM, live or cached), the train time is estimated as free-flow car time x `train_car_factor` (its own key, separate
from `car_traffic_factor`), marked approximate, provider "car estimate", never cached. Without an exact car time the
straight-line approximation from Zürich HB applies, as before.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .filters.base import FilterResult, JobContext
from .filters.poland import fold, poland_location, work_mode
from .travel import Place, Providers, ProviderError, approximate_minutes, is_swiss, next_weekday


@dataclass
class ModeResult:
    mode: str                       # "train" | "car"
    minutes: int | None             # rounded, factor applied; None if unavailable
    approximate: bool = False
    provider: str = ""
    error: str | None = None
    cached: bool = False
    factor: float | None = None
    raw: float | None = None        # free-flow minutes before any factor (exact routed car time only)


@dataclass
class LocationCheck:
    text: str
    query: str
    place: Place | None
    outcome: str                    # "pass" | "fail" | "conditional"
    reason: str | None
    comment: str
    train: ModeResult | None = None
    car: ModeResult | None = None
    limit: float | None = None      # the limit the decision used (60 on-site / unknown, hybrid_limit_minutes for hybrid)
    work_mode: str | None = None    # "onsite" | "hybrid" | None (not stated); a Swiss "remote" counts as hybrid


def round_minutes(value: float) -> int:
    return int(math.floor(value + 0.5))


def classify(minutes: int | None, approximate: bool, limit: float, margin: float) -> str:
    """'in', 'near', 'out' or 'unknown'. An exact time is in or out; an approximation within `margin` is near."""
    if minutes is None:
        return "unknown"
    if not approximate:
        return "in" if minutes <= limit else "out"
    if abs(minutes - limit) <= margin:
        return "near"
    return "in" if minutes < limit else "out"


# --- names ------------------------------------------------------------------------------------

# A text that names no place (a work mode or a region) is "unspecified"; searching it finds an unrelated station.
_REGION_RE = re.compile(r"(?:central|northern|eastern|western|southern)\s+(?:switzerland|schweiz)|(?:greater|metropolitan)\b.*\barea",
                        re.IGNORECASE)
_WORK_MODE_RE = re.compile(r"(?:(?:fully|partly)\s+)?(?:remote|hybrid|flexible|home\s*office)", re.IGNORECASE)
_SWISS_NAMES = {"switzerland", "schweiz", "suisse", "ch", "liechtenstein", "li"}
# a region word after the city of a comma-less text that ended in a country ('Mannheim Baden Wurttemberg Germany')
_STATE_RE = re.compile(r"\s+(?:baden[\s-]*w(?:ü|ue|u)rttemberg|bavaria|bayern|hesse|hessen|saxony|sachsen|thuringia|thüringen|"
                       r"lower\s+saxony|niedersachsen|north\s+rhine[\s-]*westphalia|nordrhein[\s-]*westfalen|"
                       r"rhineland[\s-]*palatinate|rheinland[\s-]*pfalz|saarland|brandenburg|mecklenburg[\s-]*vorpommern)$",
                       re.IGNORECASE)


def normalize_place_query(text: str, countries: list[str]) -> str:
    """'Switzerland - Zürich' -> 'Zürich'; 'Basel (BS), Switzerland' -> 'Basel'; a bare country -> '';
    'Mannheim Baden Wurttemberg Germany' (a LinkedIn text without commas) -> 'Mannheim', 'Zurich Zurich Switzerland' -> 'Zurich';
    'Remote, Switzerland' and 'Central Switzerland' name no place -> ''."""
    known = {c.casefold() for c in countries}
    value = re.sub(r"\([^)]*\)", " ", text or "")
    parts = [p.strip() for p in re.split(r"\s+-\s+", value) if p.strip()]
    reduced = len(parts) > 1 and parts[0].casefold() in known
    if reduced:
        value = parts[-1]
    value = value.split(",")[0].strip()
    value = re.sub(r"\s+", " ", value)
    if not reduced:
        if _REGION_RE.fullmatch(value):
            return ""
        names = "|".join(re.escape(c) for c in sorted(countries, key=len, reverse=True))
        stripped = re.sub(rf"(?:\s+-\s+|\s+)(?:{names})$", "", value, flags=re.IGNORECASE) if names else value
        if stripped != value:      # a country ended the text: also drop a region word and a repeated city ('Zurich Zurich')
            reduced_state = _STATE_RE.sub("", stripped)
            stripped = reduced_state if reduced_state else stripped
            words = stripped.split()
            half = len(words) // 2
            if len(words) % 2 == 0 and [w.casefold() for w in words[:half]] == [w.casefold() for w in words[half:]]:
                stripped = " ".join(words[:half])
        value = stripped
        if _WORK_MODE_RE.fullmatch(value):
            return ""
    return "" if value.casefold() in known else value


def _expected_in_switzerland(text: str, countries: list[str]) -> bool | None:
    """True / False when the text names Switzerland / another configured country, else None (nothing to check)."""
    low = re.sub(r"\([^)]*\)", " ", text or "").casefold()
    if _SWISS_NAMES & set(re.split(r"[\s,-]+", low)):
        return True
    if any(re.search(rf"\b{re.escape(c.casefold())}\b", low) for c in countries if c.casefold() not in _SWISS_NAMES):
        return False
    return None


# --- caches -----------------------------------------------------------------------------------

def _expiry(cfg: dict, now: datetime) -> str:
    return (now + timedelta(days=cfg["location"]["cache_expiry_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_place(conn, providers: Providers, cfg: dict, query: str, now: datetime | None = None):
    """Return (Place | None, status) with status 'ok', 'not_found' or 'error'. Answers are cached; errors are not."""
    now = now or _now()
    row = conn.execute("SELECT * FROM place_cache WHERE query = ? AND expires_at > ?",
                       (query.casefold(), "" if providers.cache_any_age else now.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()
    if row:
        if not row["resolved"]:
            return None, "not_found"
        return Place(row["station_name"], row["station_id"], row["latitude"], row["longitude"],
                     bool(row["in_switzerland"]) and is_swiss(row["station_id"], row["latitude"], row["longitude"])), "ok"
    try:
        place = providers.transport.find_station(query)
    except ProviderError:
        return None, "error"
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO place_cache (query, resolved, station_id, station_name, latitude, longitude, "
            "in_switzerland, provider, resolved_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (query.casefold(), 1 if place else 0, place and place.station_id, place and place.name,
             place and place.latitude, place and place.longitude, place and int(place.in_switzerland),
             providers.transport.name, now.strftime("%Y-%m-%dT%H:%M:%SZ"), _expiry(cfg, now)))
    return (place, "ok") if place else (None, "not_found")


def _cached_minutes(conn, origin: str, destination: str, mode: str, now: datetime, any_age: bool = False):
    row = conn.execute("SELECT minutes, provider FROM travel_cache WHERE origin = ? AND destination = ? AND mode = ? "
                       "AND expires_at > ?", (origin, destination, mode, "" if any_age else now.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()
    return (row["minutes"], row["provider"]) if row and row["minutes"] is not None else None


def _store_minutes(conn, cfg, origin, destination, mode, minutes, provider, assumptions, now):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO travel_cache (origin, destination, mode, minutes, provider, approximate, assumptions, "
            "computed_at, expires_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (origin, destination, mode, minutes, provider, json.dumps(assumptions), now.strftime("%Y-%m-%dT%H:%M:%SZ"),
             _expiry(cfg, now)))


# --- one location -----------------------------------------------------------------------------

def _mode_time(conn, providers, cfg, mode, origin: Place | None, dest: Place, day: date, now,
               car: ModeResult | None = None) -> ModeResult:
    loc = cfg["location"]
    approx = loc["approximation"]
    factor = loc["car_traffic_factor"] if mode == "car" else None
    origin_key = loc["train_origin"] if mode == "train" else loc["car_origin_town"]
    if origin is not None and origin.station_id == dest.station_id:     # the destination is the origin itself
        return ModeResult(mode, 0, False, "same place", factor=factor)
    hit = _cached_minutes(conn, origin_key, dest.station_id, mode, now, providers.cache_any_age)
    if hit:
        raw, provider = hit
        return ModeResult(mode, round_minutes(raw * (factor or 1)), False, provider, cached=True, factor=factor,
                          raw=raw if mode == "car" else None)
    error = None
    try:
        if mode == "train":
            raw = providers.transport.train_minutes(loc["train_origin"], dest, day, loc["arrival_time"])
            provider = providers.transport.name
        else:
            if origin is None:
                raise ProviderError("car origin could not be resolved")
            raw = providers.osrm.car_minutes(origin, dest)
            provider = providers.osrm.name
        _store_minutes(conn, cfg, origin_key, dest.station_id, mode, raw, provider,
                       {"arrival": loc["arrival_time"], "date": day.isoformat(), "free_flow": mode == "car"}, now)
        return ModeResult(mode, round_minutes(raw * (factor or 1)), False, provider, factor=factor,
                          raw=raw if mode == "car" else None)
    except ProviderError as exc:
        error = str(exc)
    if mode == "train" and car is not None and car.raw is not None and not car.approximate:
        # the timetable gave nothing, the car route is exact: free-flow car time x train_car_factor, approximate, not cached
        estimate = loc["train_car_factor"]
        return ModeResult(mode, round_minutes(car.raw * estimate), True, "car estimate", error, factor=estimate)
    if origin is not None:      # last fallback: straight line x detour / speed, marked approximate, not cached
        raw = approximate_minutes(origin, dest, approx["detour_factor"], approx["average_speed_kmh"])
        return ModeResult(mode, round_minutes(raw), True, "approximation", error, factor=None)
    return ModeResult(mode, None, False, "none", error)


_REMOTE_ONLY_RE = re.compile(r"(?:fully\s+)?remote", re.IGNORECASE)


def is_remote_text(text: str) -> bool:
    """'Remote', 'Fully remote', 'Remote, Switzerland' or 'Remote, CH': the location names no place, only the work mode."""
    parts = [p.strip() for p in (text or "").split(",")]
    return bool(_REMOTE_ONLY_RE.fullmatch(parts[0])) and all(p.casefold() in _SWISS_NAMES for p in parts[1:])


def apply_alias(query: str, cfg: dict) -> str:
    """'Berne' -> 'Bern' by location.place_aliases (compared in lower case), else the query unchanged."""
    aliases = {k.casefold(): v for k, v in cfg["location"]["place_aliases"].items()}
    return aliases.get(query.casefold(), query)


def in_geneva_canton(name: str, cfg: dict) -> bool:
    """The name (a query, or a station name up to the comma) is one of location.geneva_canton_places."""
    places = {fold(p) for p in cfg["location"]["geneva_canton_places"]}
    return fold(name.split(",")[0]).strip() in places


def check_location(conn, providers: Providers, cfg: dict, text: str, now: datetime | None = None,
                   mode: str | None = None) -> LocationCheck:
    """`mode` is the work mode of the offer: "hybrid" (limit `hybrid_limit_minutes`), "onsite", or None when not stated.
    The caller passes a Swiss "remote" as "hybrid"."""
    now = now or _now()
    loc = cfg["location"]
    query = apply_alias(normalize_place_query(text, loc["countries"]), cfg)
    if not query:
        return LocationCheck(text, query, None, "conditional", "location_unspecified", f"'{text}' names no specific place")
    if in_geneva_canton(query, cfg):
        return LocationCheck(text, query, None, "fail", "location_geneva_canton",
                             f"{query}: in the canton of Geneva, which is excluded (no travel time checked)", work_mode=mode)
    # the whole text first; when the text names a country, then its leading word (the city of 'Mannheim Baden
    # Wurttemberg'). A station in another country than the text names is no match ('Baden Aargau Switzerland' must
    # not become Baden-Baden).
    words = query.split()
    expected = _expected_in_switzerland(text, loc["countries"])
    dest, status, first = None, "not_found", None
    for candidate in [query] + ([words[0]] if expected is not None and len(words) > 1 and len(words[0]) >= 4 else []):
        place, status = resolve_place(conn, providers, cfg, candidate, now)
        if status == "error":
            first = first or "error"        # the whole query could not be searched: the leading word is still tried
            continue
        if place is not None and (expected is None or place.in_switzerland == expected):
            dest, status = place, "ok"
            break
        status = "not_found"
    if dest is None and first == "error":
        status = "error"                    # the fallback is a second try; its failure keeps the first answer
    if status == "not_found":
        return LocationCheck(text, query, None, "conditional", "location_unresolved", f"{query}: no station matches this name")
    if status == "error":
        return LocationCheck(text, query, None, "conditional", "travel_time_unavailable", f"{query}: station search did not answer")
    if in_geneva_canton(dest.name, cfg):
        return LocationCheck(text, query, dest, "fail", "location_geneva_canton",
                             f"{dest.name}: in the canton of Geneva, which is excluded (no travel time checked)", work_mode=mode)

    tz = ZoneInfo(cfg["timezone"])
    day = next_weekday(providers.today or now.astimezone(tz).date())
    train_origin, _ = resolve_place(conn, providers, cfg, loc["train_origin"], now)
    car_origin, _ = resolve_place(conn, providers, cfg, loc["car_origin_town"], now)
    car = _mode_time(conn, providers, cfg, "car", car_origin, dest, day, now)
    train = _mode_time(conn, providers, cfg, "train", train_origin, dest, day, now, car)

    base, margin = loc["limit_minutes"], loc["approximate_near_limit_minutes"]
    wide = loc["hybrid_limit_minutes"]
    limit = wide if mode == "hybrid" else base

    def classes_at(value: float) -> dict[str, str]:
        return {m.mode: classify(m.minutes, m.approximate, value, margin) for m in (train, car)}

    classes, wide_classes = classes_at(base), classes_at(wide)

    def describe(m: ModeResult) -> str:
        if m.minutes is None:
            return f"{m.mode} unavailable"
        return f"{m.mode} {m.minutes} min" + (" (approximate)" if m.approximate else "")

    detail = f"{dest.name}: {describe(train)}, {describe(car)}; limit {limit} min"
    if mode == "hybrid":
        detail += " (hybrid)"
    elif mode is None:
        detail += f" (work mode not stated; up to {wide} min is only conditional)"

    def result(outcome: str, reason: str | None) -> LocationCheck:
        return LocationCheck(text, query, dest, outcome, reason, detail, train, car, limit, mode)

    if "in" in classes.values():
        return result("pass", None)
    if mode == "hybrid":
        if "in" in wide_classes.values():
            return result("pass", "location_hybrid")
        classes = wide_classes
    elif mode is None and "near" not in classes.values() and "in" in wide_classes.values():
        return result("conditional", "travel_time_work_mode_unknown")
    if "near" in classes.values():
        return result("conditional", "travel_time_approximate")
    if "unknown" in classes.values():
        return result("conditional", "travel_time_unavailable")
    return result("fail", "location_too_far")


def _persist(conn, job_id: int, text: str, check: LocationCheck, cfg: dict, now: datetime) -> None:
    row = conn.execute("SELECT id FROM job_locations WHERE job_id = ? AND location_text = ?", (job_id, text)).fetchone()
    if row is None:
        return
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    loc = cfg["location"]
    with conn:
        conn.execute("UPDATE job_locations SET resolved = ?, latitude = ?, longitude = ? WHERE id = ?",
                     (1 if check.place else 0, check.place and check.place.latitude,
                      check.place and check.place.longitude, row["id"]))
        for m in (check.train, check.car):
            if m is None:
                continue
            origin = loc["train_origin"] if m.mode == "train" else loc["car_origin_town"]
            assumptions = {"arrival": loc["arrival_time"], "station_to_station": m.mode == "train",
                           "traffic_factor": m.factor, "cached": m.cached, "error": m.error,
                           "limit_minutes": check.limit if check.limit is not None else loc["limit_minutes"],
                           "work_mode": check.work_mode or "not stated"}
            conn.execute(
                "INSERT INTO job_travel_checks (location_id, mode, origin, destination, minutes, provider, approximate, "
                "assumptions, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["id"], m.mode, origin, check.place.name if check.place else check.query, m.minutes,
                 m.provider or "none", int(m.approximate), json.dumps(assumptions), stamp))


_NEG_BEFORE = re.compile(r"(?:\bno|\bnot|\bnon|\bwithout|\bnicht|\bkein\w*|\bohne)[\s-]+(?:\w+[\s-]+){0,2}$", re.IGNORECASE)
# the negation after the pattern: "Fully remote work is not possible", "100% remote is not an option", "Vollständig remote ist
# leider nicht möglich", "Work from anywhere: not available" (read up to the end of the sentence)
_NEG_AFTER = re.compile(
    r"^\s*(?::\s*|(?:\w+\s+){0,2}?(?:is|are|was|be|ist|sind|wird|war)\s+(?:\w+\s+)?)"
    r"(?:not|nicht|never|no longer)\s+(?:\w+\s+)?"
    r"(?:possible|available|offered|an option|allowed|permitted|supported|möglich|angeboten|vorgesehen|erlaubt)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?;\n]")


def _current_sentence(before: str) -> str:
    """The text before a match back to the last sentence end or line break: a negation in an earlier list item or
    sentence ("- No night shifts" then "- Fully remote work") does not reach the match."""
    cuts = list(_SENTENCE_END.finditer(before))
    return before[cuts[-1].end():] if cuts else before


def _negated_after(text: str, end: int) -> bool:
    rest = text[end:end + 80]
    cut = _SENTENCE_END.search(rest)
    return bool(_NEG_AFTER.search(rest[:cut.start()] if cut else rest))


def _not_negated(pattern: str, text: str) -> bool:
    """True when the pattern matches somewhere in the text without a negation: "not", "no", "nicht", "kein", "ohne" ...
    up to two words before it ("not offered as fully remote", "no option for 100% remote"), or "is not possible" or a
    colon and "not available" after it. "not fully remote" is no remote offer."""
    return any(not _NEG_BEFORE.search(_current_sentence(text[:m.start()])) and not _negated_after(text, m.end())
               for m in re.finditer(pattern, text, re.IGNORECASE))


# --- the filter -------------------------------------------------------------------------------

def location_checks(conn, providers: Providers, cfg: dict, job: JobContext, stamp: datetime,
                    persist: bool = True) -> tuple[list[FilterResult], list[LocationCheck]]:
    """The location decision of one job and the per-location checks behind it. With `persist=False` the travel checks
    are not written (`recheck-travel` compares before it writes)."""
    polish = poland_location(job, cfg)         # a job located only in Poland: by work mode, no travel time
    if polish:
        return polish, []
    loc = cfg["location"]
    text = f"{job.title}\n{job.location_text}\n{job.description}"
    names = [n for n in job.locations if n and n.strip()]
    if any(_not_negated(p, text) for p in loc["remote_patterns"]) or (names and all(is_remote_text(n) for n in names)):
        return [FilterResult("location", "pass", "remote", "fully remote offer; no location check")], []

    if not names:
        return [FilterResult("location", "conditional", "location_unspecified", "the offer names no location")], []

    mode, _ = work_mode(job, cfg)           # once per job; a Swiss "remote" (LinkedIn label) is a hybrid offer, not a remote one
    mode = "hybrid" if mode == "remote" else mode
    checks, seen = [], set()
    for name in names:
        query = apply_alias(normalize_place_query(name, loc["countries"]), cfg).casefold()
        if query in seen:
            continue
        seen.add(query)
        check = check_location(conn, providers, cfg, name, stamp, mode)
        if persist:
            _persist(conn, job.id, name, check, cfg, stamp)
        checks.append(check)

    summary = "; ".join(c.comment for c in checks)
    passed = [c for c in checks if c.outcome == "pass"]
    if passed:
        return [FilterResult("location", "pass", passed[0].reason, f"{passed[0].place.name} qualifies. {summary}")], checks
    conditional = [c for c in checks if c.outcome == "conditional"]
    if conditional:
        return [FilterResult("location", "conditional", conditional[0].reason, summary)], checks
    reasons = [c.reason for c in checks]
    return [FilterResult("location", "fail", "location_too_far" if "location_too_far" in reasons else reasons[0], summary)], checks


def make_location_filter(conn, providers: Providers, now: datetime | None = None, persist: bool = True):
    """Build the location filter for the runner. It needs the database (caches, stored checks) and the providers."""

    def location(job: JobContext, cfg: dict) -> list[FilterResult]:
        return location_checks(conn, providers, cfg, job, now or _now(), persist)[0]

    location.expensive = True       # calls web services: the runner skips it for a job that already failed
    return location
