"""Configuration loading (goal.md 5.3, 5.5, 5.7, 9).

`config.example.yaml` holds the defaults. `config.yaml` (untracked) overrides them key by key.
Unknown keys and wrong types are errors, so a typo cannot silently fall back to a default.
"""
from __future__ import annotations

import copy
import ipaddress
import os
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = ROOT / "config.example.yaml"
DEFAULT_USER_CONFIG = ROOT / "config.yaml"       # the user's private config; the tests point this elsewhere


DISCOVERY_PLATFORMS = ("greenhouse", "ashby", "lever")             # the public job-board interfaces of resolver.discovery (app/resolver/discovery.py)
WEB_HOSTS = ("127.0.0.1", "localhost", "::1")      # the web app binds to the local computer only (goal.md 5.7)


class ConfigError(ValueError):
    """Raised when the configuration is missing, malformed or invalid."""


def resolve_path(value: str | os.PathLike) -> Path:
    """A path from the config: absolute as given, relative to the repository root otherwise."""
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def _deep_merge(base: dict, override: dict, path: str = "") -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        where = f"{path}.{key}" if path else key
        if key not in base:
            if path == "resolver.sources":                              # the user may add sources of their own
                merged[key] = copy.deepcopy(value)
                continue
            raise ConfigError(f"unknown config key: {where}")
        if key == "facets" and path.startswith("adapters."):           # a Workday facet filter replaces the default as a whole
            merged[key] = copy.deepcopy(value)
        elif isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ConfigError(f"config key {where} must be a mapping")
            merged[key] = _deep_merge(base[key], value, where)
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")
    return data


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ConfigError(message)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate(cfg: dict) -> None:
    loc, flt, sc, em = cfg["location"], cfg["filters"], cfg["scoring"], cfg["email"]
    _require(isinstance(cfg["timezone"], str) and cfg["timezone"], "timezone must be a non-empty string")
    for key in ("database", "exchange"):
        _require(isinstance(cfg["paths"][key], str) and cfg["paths"][key], f"paths.{key} must be a non-empty string")

    _require(_is_number(loc["limit_minutes"]) and loc["limit_minutes"] > 0, "location.limit_minutes must be > 0")
    _require(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(loc["arrival_time"])) is not None,
             "location.arrival_time must be HH:MM")
    _require(_is_number(loc["car_traffic_factor"]) and loc["car_traffic_factor"] >= 1,
             "location.car_traffic_factor must be a number >= 1")
    _require(_is_number(loc["train_car_factor"]) and loc["train_car_factor"] >= 1,
             "location.train_car_factor must be a number >= 1")
    _require(_is_number(loc["approximate_near_limit_minutes"]) and loc["approximate_near_limit_minutes"] >= 0,
             "location.approximate_near_limit_minutes must be >= 0")
    approx = loc["approximation"]
    _require(_is_number(approx["detour_factor"]) and approx["detour_factor"] >= 1,
             "location.approximation.detour_factor must be >= 1")
    _require(_is_number(approx["average_speed_kmh"]) and approx["average_speed_kmh"] > 0,
             "location.approximation.average_speed_kmh must be > 0")
    _require(isinstance(loc["cache_expiry_days"], int) and loc["cache_expiry_days"] > 0,
             "location.cache_expiry_days must be a positive integer")
    for key in ("train_origin", "car_origin_address", "car_origin_town"):
        _require(isinstance(loc[key], str), f"location.{key} must be a string")
    for key in ("logs", "lock", "documents", "approved"):
        _require(isinstance(cfg["paths"][key], str) and cfg["paths"][key], f"paths.{key} must be a non-empty string")
    bk = cfg["backup"]
    _require(isinstance(bk["dir"], str) and bk["dir"].strip(), "backup.dir must be a non-empty string")
    _require(isinstance(bk["keep"], int) and not isinstance(bk["keep"], bool) and 1 <= bk["keep"] <= 365,
             "backup.keep must be an integer from 1 to 365")
    ds = cfg["descriptions"]
    _require(isinstance(ds["fetch_enabled"], bool), "descriptions.fetch_enabled must be true or false")
    _require(isinstance(ds["permission_note"], str), "descriptions.permission_note must be a string")
    _require(not ds["fetch_enabled"] or ds["permission_note"].strip(),
             "descriptions.fetch_enabled needs descriptions.permission_note (the basis for fetching)")
    _require(_is_number(ds["delay_seconds"]) and ds["delay_seconds"] >= 1, "descriptions.delay_seconds must be at least 1")
    _require(isinstance(ds["daily_cap"], int) and 1 <= ds["daily_cap"] <= 200, "descriptions.daily_cap must be 1 to 200")
    _require(_is_number(ds["timeout_seconds"]) and ds["timeout_seconds"] > 0, "descriptions.timeout_seconds must be > 0")
    _require(isinstance(ds["user_agent"], str) and ds["user_agent"].strip(), "descriptions.user_agent must be a non-empty string")
    ad = cfg["adapters"]
    _require(_is_number(ad["delay_seconds"]) and ad["delay_seconds"] >= 1, "adapters.delay_seconds must be at least 1")
    _require(isinstance(ad["daily_cap"], int) and 1 <= ad["daily_cap"] <= 200, "adapters.daily_cap must be 1 to 200")
    _require(isinstance(ad["max_list_pages"], int) and 1 <= ad["max_list_pages"] <= 50, "adapters.max_list_pages must be 1 to 50")
    _require(isinstance(ad["closed_after_missing"], int) and 1 <= ad["closed_after_missing"] <= 20,
             "adapters.closed_after_missing must be 1 to 20")
    _require(_is_number(ad["timeout_seconds"]) and ad["timeout_seconds"] > 0, "adapters.timeout_seconds must be > 0")
    _require(isinstance(ad["user_agent"], str) and ad["user_agent"].strip(), "adapters.user_agent must be a non-empty string")
    for name, site in ((k, v) for k, v in ad.items() if isinstance(v, dict)):
        _require(isinstance(site["enabled"], bool), f"adapters.{name}.enabled must be true or false")
        _require(isinstance(site["permission_note"], str), f"adapters.{name}.permission_note must be a string")
        _require(isinstance(site.get("decision_note", ""), str), f"adapters.{name}.decision_note must be a string")
        facets = site.get("facets", {})
        _require(isinstance(facets, dict) and all(isinstance(k, str) and _is_str_list(v) for k, v in facets.items()),
                 f"adapters.{name}.facets must be a mapping of facet names to lists of ids (strings)")
        for key in ("country_codes", "countries", "places"):
            _require(_is_str_list(site.get(key, [])), f"adapters.{name}.{key} must be a list of strings")
        _require(isinstance(site["list_url"], str) and site["list_url"].startswith("https://"),
                 f"adapters.{name}.list_url must be an https address")
    _require(_is_str_list(ad["six"]["country_codes"]), "adapters.six.country_codes must be a list of strings")
    _require(_is_str_list(ad["lgt"]["places"]), "adapters.lgt.places must be a list of strings")
    _require(_is_str_list(ad["sygnum"]["countries"]), "adapters.sygnum.countries must be a list of strings")
    _require(isinstance(ad["julius"]["country_facet"], str), "adapters.julius.country_facet must be a string (\"\" reads every country)")
    _require(isinstance(ad["julius"]["decision_note"], str), "adapters.julius.decision_note must be a string")
    _require(not ad["sygnum"]["enabled"] or ad["sygnum"]["permission_note"].strip(),
             "adapters.sygnum.enabled needs adapters.sygnum.permission_note (the basis: who confirmed what, when)")
    rs = cfg["resolver"]          # goal.md 5.1a, decided 3 Nov 2026
    _require(isinstance(rs["enabled"], bool), "resolver.enabled must be true or false")
    _require(isinstance(rs["accept_t2_risk"], bool), "resolver.accept_t2_risk must be true or false")
    _require(_is_number(rs["delay_seconds"]) and rs["delay_seconds"] >= 1, "resolver.delay_seconds must be at least 1")
    _require(isinstance(rs["daily_cap_per_host"], int) and not isinstance(rs["daily_cap_per_host"], bool)
             and 1 <= rs["daily_cap_per_host"] <= 1000, "resolver.daily_cap_per_host must be 1 to 1000")
    _require(isinstance(rs["max_attempt_days"], int) and not isinstance(rs["max_attempt_days"], bool)
             and 1 <= rs["max_attempt_days"] <= 30, "resolver.max_attempt_days must be 1 to 30")
    _require(_is_number(rs["timeout_seconds"]) and rs["timeout_seconds"] > 0, "resolver.timeout_seconds must be > 0")
    _require(isinstance(rs["user_agent"], str), "resolver.user_agent must be a string (empty: descriptions.user_agent)")
    dc = rs["discovery"]          # goal.md 5.1a (a), v0.73.0
    _require(isinstance(dc["enabled"], bool), "resolver.discovery.enabled must be true or false")
    _require(isinstance(dc["local_twins"], bool), "resolver.discovery.local_twins must be true or false")
    for key, top in (("window_days", 365), ("new_companies_per_day", 50), ("miss_retry_days", 365)):
        _require(isinstance(dc[key], int) and not isinstance(dc[key], bool) and 1 <= dc[key] <= top,
                 f"resolver.discovery.{key} must be 1 to {top}")
    _require(_is_str_list(dc["platforms"]), "resolver.discovery.platforms must be a list of platform names")
    _require(len(set(dc["platforms"])) == len(dc["platforms"]), "resolver.discovery.platforms: a platform is listed twice")
    for name in dc["platforms"]:
        _require(name in DISCOVERY_PLATFORMS, f"resolver.discovery.platforms: unknown platform {name!r}; supported: {', '.join(DISCOVERY_PLATFORMS)}")
    _require(isinstance(rs["sources"], dict), "resolver.sources must be a mapping")
    for name, src in rs["sources"].items():
        where = f"resolver.sources.{name}"
        _require(isinstance(src, dict), f"{where} must be a mapping")
        extra = set(src) - {"platform", "base_url", "list_url", "aliases", "permission_note", "any_company"}
        _require(not extra, f"unknown config key: {where}.{sorted(extra)[0] if extra else ''}")
        _require(src.get("platform") in ("jibe", "refline", "liebherr", "jobcloud"),
                 f"{where}.platform must be jibe, refline, liebherr or jobcloud")
        _require(isinstance(src.get("base_url"), str) and src["base_url"].startswith("https://") and len(src["base_url"]) > len("https://"),
                 f"{where}.base_url must be an https address")
        _require(isinstance(src.get("list_url", ""), str) and (not src.get("list_url") or src["list_url"].startswith("https://")),
                 f"{where}.list_url must be an https address or empty")
        _require(isinstance(src.get("any_company", False), bool), f"{where}.any_company must be true or false")
        _require(_is_str_list(src.get("aliases")) and all(a.strip() for a in src["aliases"])
                 and (src["aliases"] or src.get("any_company")),
                 f"{where}.aliases must be a non-empty list of company names (empty only with any_company: true)")
        _require(isinstance(src.get("permission_note", ""), str), f"{where}.permission_note must be a string")
    gm, mon = cfg["gmail"], cfg["monitoring"]
    for key in ("imap_host", "label_ubs", "label_linkedin", "label_julius", "label_jobsch", "ubs_sender", "linkedin_sender", "julius_sender", "jobsch_sender"):
        _require(isinstance(gm[key], str) and gm[key].strip(), f"gmail.{key} must be a non-empty string")
    _require(isinstance(gm["lookback_days"], int) and gm["lookback_days"] >= 1, "gmail.lookback_days must be an integer >= 1")
    _require(isinstance(gm["mark_seen"], bool), "gmail.mark_seen must be true or false")
    _require(isinstance(gm["max_message_bytes"], int) and gm["max_message_bytes"] > 0, "gmail.max_message_bytes must be > 0")
    _require(isinstance(mon["zero_jobs_alert_after"], int) and mon["zero_jobs_alert_after"] >= 1,
             "monitoring.zero_jobs_alert_after must be an integer >= 1")
    _require(_is_number(mon["lock_stale_hours"]) and mon["lock_stale_hours"] > 0, "monitoring.lock_stale_hours must be > 0")
    _require(isinstance(mon["alerts"], bool), "monitoring.alerts must be true or false")
    prov = loc["providers"]
    for key in ("transport_url", "osrm_url", "user_agent"):
        _require(isinstance(prov[key], str) and prov[key], f"location.providers.{key} must be a non-empty string")
    _require(prov["transport_url"].startswith("https://") and prov["osrm_url"].startswith("https://"),
             "location.providers URLs must start with https://")
    _require(_is_number(prov["request_delay_seconds"]) and prov["request_delay_seconds"] >= 0,
             "location.providers.request_delay_seconds must be >= 0")
    _require(_is_number(prov["timeout_seconds"]) and prov["timeout_seconds"] > 0,
             "location.providers.timeout_seconds must be > 0")
    for key in ("remote_patterns", "countries"):
        _require(_is_str_list(loc[key]), f"location.{key} must be a list of strings")
    for pattern in loc["remote_patterns"]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"location.remote_patterns: invalid regular expression {pattern!r}: {exc}") from exc
    _require(_is_number(loc["hybrid_limit_minutes"]) and loc["hybrid_limit_minutes"] >= loc["limit_minutes"],
             "location.hybrid_limit_minutes must be a number >= location.limit_minutes")
    _require(_is_str_list(loc["geneva_canton_places"]) and all(x.strip() for x in loc["geneva_canton_places"]),
             "location.geneva_canton_places must be a list of non-empty strings")
    aliases = loc["place_aliases"]
    _require(isinstance(aliases, dict) and all(isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
                                               for k, v in aliases.items()),
             "location.place_aliases must map names to non-empty names")
    pl = loc["poland"]            # goal.md 5.3.1, decided 2 Nov 2026
    _require(isinstance(pl["enabled"], bool), "location.poland.enabled must be true or false")
    for key in ("country_names", "cities", "remote_patterns", "hybrid_patterns", "onsite_patterns"):
        _require(_is_str_list(pl[key]), f"location.poland.{key} must be a list of strings")
        _require(all(x.strip() for x in pl[key]), f"location.poland.{key} must not hold an empty string")
    _require(not pl["enabled"] or (pl["country_names"] or pl["cities"]),
             "location.poland needs country_names or cities while it is enabled")
    for key in ("low_presence_patterns", "high_presence_patterns"):
        _require(_is_str_list(pl[key]) and all(x.strip() for x in pl[key]),
                 f"location.poland.{key} must be a list of non-empty strings")
    for key in ("remote_patterns", "hybrid_patterns", "onsite_patterns", "low_presence_patterns", "high_presence_patterns"):
        for pattern in pl[key]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(f"location.poland.{key}: invalid regular expression {pattern!r}: {exc}") from exc

    for key in ("title_include", "title_exclude", "excluded_companies", "seniority_reject_ubs_titles",
                "seniority_reject_title_patterns", "seniority_allow_title_patterns"):
        _require(_is_str_list(flt[key]), f"filters.{key} must be a list of strings")
    for key in ("title_include", "title_exclude", "seniority_reject_title_patterns",
                "seniority_allow_title_patterns"):
        for pattern in flt[key]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(f"filters.{key}: invalid regular expression {pattern!r}: {exc}") from exc

    lang = cfg["languages"]
    _require(isinstance(lang["names"], list) and lang["names"], "languages.names must be a non-empty list")
    for entry in lang["names"]:
        _require(isinstance(entry, dict) and set(entry) == {"language", "patterns"}
                 and isinstance(entry["language"], str) and entry["language"] and _is_str_list(entry["patterns"])
                 and entry["patterns"], "languages.names entries need 'language' (string) and 'patterns' (list of strings)")
    for key in ("ignored_languages", "descriptive_nouns", "requirement_cues", "c1_phrases", "optional_phrases"):
        _require(_is_str_list(lang[key]), f"languages.{key} must be a list of strings")
    patterns = [p for e in lang["names"] for p in e["patterns"]] + lang["ignored_languages"] + lang["descriptive_nouns"] \
        + lang["requirement_cues"] + lang["c1_phrases"] + lang["optional_phrases"]
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"languages: invalid regular expression {pattern!r}: {exc}") from exc
    gp = lang["german_posting"]
    _require(isinstance(gp["min_german_words"], int) and gp["min_german_words"] >= 0,
             "languages.german_posting.min_german_words must be an integer >= 0")
    _require(_is_number(gp["min_ratio"]) and gp["min_ratio"] > 0, "languages.german_posting.min_ratio must be > 0")

    _require(_is_number(sc["band_low_below"]) and _is_number(sc["band_high_from"]), "scoring bands must be numbers")
    _require(0 <= sc["band_low_below"] < sc["band_high_from"] <= 100,
             "scoring: need 0 <= band_low_below < band_high_from <= 100")
    _require(sc["batch_cap"] is None or (isinstance(sc["batch_cap"], int) and sc["batch_cap"] > 0),
             "scoring.batch_cap must be null or a positive integer")
    _require(isinstance(sc["top_n"], int) and sc["top_n"] > 0, "scoring.top_n must be a positive integer")
    _require(isinstance(sc["prompt_file"], str) and sc["prompt_file"], "scoring.prompt_file must be a non-empty string")
    weights = sc["weights"]
    _require(set(weights) == {"skills", "experience", "domain", "seniority", "location"},
             "scoring.weights needs exactly skills, experience, domain, seniority, location")
    _require(all(_is_number(w) and w >= 0 for w in weights.values()) and sum(weights.values()) == 100,
             "scoring.weights must be non-negative numbers that add up to 100")
    _require(_is_number(sc["overall_tolerance"]) and sc["overall_tolerance"] >= 0, "scoring.overall_tolerance must be >= 0")
    _require(isinstance(sc["no_description_cap"], int) and 0 <= sc["no_description_cap"] <= 100,
             "scoring.no_description_cap must be an integer from 0 to 100")
    _require(isinstance(sc["non_english_cap"], int) and not isinstance(sc["non_english_cap"], bool)
             and 0 <= sc["non_english_cap"] < sc["band_high_from"],
             "scoring.non_english_cap must be an integer from 0 up to below band_high_from (a non-English posting is never HIGH)")
    _require(_is_number(sc["calibration_min_agreement"]) and 0 <= sc["calibration_min_agreement"] <= 100,
             "scoring.calibration_min_agreement must be a number from 0 to 100")

    tl = cfg["tailoring"]
    _require(isinstance(tl["documents_top_n"], int) and not isinstance(tl["documents_top_n"], bool)
             and 0 <= tl["documents_top_n"] <= 1000, "tailoring.documents_top_n must be an integer from 0 up (0 switches documents off)")
    _require(isinstance(tl["min_score"], int) and not isinstance(tl["min_score"], bool) and 0 <= tl["min_score"] <= 100,
             "tailoring.min_score must be an integer from 0 to 100")
    _require(isinstance(tl["require_description"], bool), "tailoring.require_description must be true or false")
    _require(isinstance(tl["photo"], str), "tailoring.photo must be a string (a path, or empty for no photo)")
    for key in ("letter_max_pages", "cv_max_pages"):
        _require(isinstance(tl[key], int) and not isinstance(tl[key], bool) and 1 <= tl[key] <= 10,
                 f"tailoring.{key} must be an integer from 1 to 10")

    _require(isinstance(em["smtp_port"], int) and 0 < em["smtp_port"] < 65536, "email.smtp_port must be a valid port")
    for key in ("sender", "recipient"):
        _require(isinstance(em[key], str) and "@" in em[key], f"email.{key} must be an email address")
    _require(isinstance(em["send_attempts"], int) and em["send_attempts"] >= 1, "email.send_attempts must be an integer >= 1")
    _require(_is_number(em["retry_wait_seconds"]) and em["retry_wait_seconds"] >= 0, "email.retry_wait_seconds must be >= 0")
    rep = cfg["report"]
    _require(isinstance(rep["subject_prefix"], str) and rep["subject_prefix"].strip()
             and not re.search(r"[\r\n]", rep["subject_prefix"]), "report.subject_prefix must be one non-empty line")
    _require(isinstance(rep["web_base_url"], str) and (not rep["web_base_url"] or rep["web_base_url"].startswith(("http://", "https://"))),
             "report.web_base_url must be empty or start with http:// or https://")
    for key in ("max_matches", "max_gaps"):
        _require(isinstance(rep[key], int) and rep[key] >= 0, f"report.{key} must be an integer >= 0")
    sch = cfg["schedule"]
    _require(isinstance(sch["enabled"], bool), "schedule.enabled must be true or false")
    _require(isinstance(sch["run_backup"], bool), "schedule.run_backup must be true or false")
    _require(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(sch["prepare_time"])) is not None,
             "schedule.prepare_time must be HH:MM")
    _validate_headless(cfg["headless"])
    try:
        ZoneInfo(cfg["timezone"])
    except Exception as exc:                                  # ZoneInfoNotFoundError, ValueError, OSError
        raise ConfigError(f"timezone {cfg['timezone']!r} is not a known time zone name") from exc
    web = cfg["web"]
    _require(web["host"] in WEB_HOSTS, f"web.host must be one of {', '.join(WEB_HOSTS)} (this version listens on the local computer only; "
             "LAN and ZeroTier binding come with the NAS, Phase 5)")
    _require(isinstance(web["port"], int) and not isinstance(web["port"], bool) and 1 <= web["port"] <= 65535,
             "web.port must be an integer from 1 to 65535")
    _require(isinstance(web["container_bind"], bool), "web.container_bind must be true or false")
    _require(isinstance(web["allowed_hosts"], list) and all(_is_host_name(h) for h in web["allowed_hosts"]),
             "web.allowed_hosts must be a list of lowercase host names or IP addresses (no scheme, no port, no *, not 0.0.0.0 or ::)")
    login = web["login"]
    _require(isinstance(login["username"], str) and len(login["username"]) <= 64 and ":" not in login["username"]
             and not any(ord(ch) < 32 or ord(ch) == 127 for ch in login["username"]),
             "web.login.username must be one line of at most 64 characters without ':' (empty: no login)")
    _require(isinstance(login["stay_days"], int) and not isinstance(login["stay_days"], bool) and 1 <= login["stay_days"] <= 90,
             "web.login.stay_days must be an integer from 1 to 90")


HEADLESS_ROLES = ("scorer", "letters", "review")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MODEL_ID_RE = re.compile(r"^claude-(sonnet|opus|haiku)-[0-9][0-9a-z-]*$")         # a full model id, never an alias


def _positive_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _validate_headless(hl: dict) -> None:
    """The `headless` block (goal.md 5.10, v0.78.0)."""
    _require(isinstance(hl["enabled"], bool), "headless.enabled must be true or false")
    _require(isinstance(hl["retry_after_limit"], bool), "headless.retry_after_limit must be true or false")
    _require(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(hl["retry_latest"])) is not None, "headless.retry_latest must be HH:MM")
    for key in ("max_wait_minutes", "poll_seconds", "retry_fallback_minutes"):
        _require(_positive_int(hl[key]), f"headless.{key} must be a positive integer")
    _require(hl["poll_seconds"] <= 120, "headless.poll_seconds must be at most 120 (the scheduler heartbeat is only touched once per poll)")
    _require(isinstance(hl["resume_attempts"], int) and not isinstance(hl["resume_attempts"], bool) and 0 <= hl["resume_attempts"] <= 10,
             "headless.resume_attempts must be an integer from 0 to 10")
    roles = hl["roles"]
    _require(set(roles) == set(HEADLESS_ROLES), "headless.roles needs exactly scorer, letters, review")
    total = 0
    for name in HEADLESS_ROLES:
        role = roles[name]
        _require(isinstance(role["model"], str) and MODEL_ID_RE.fullmatch(role["model"]) is not None,
                 f"headless.roles.{name}.model must be a full model id such as claude-sonnet-5-5 (no alias)")
        _require(role["effort"] in EFFORT_LEVELS, f"headless.roles.{name}.effort must be one of {', '.join(EFFORT_LEVELS)}")
        for key in ("timeout_minutes", "max_turns"):
            _require(_positive_int(role[key]), f"headless.roles.{name}.{key} must be a positive integer")
        total += role["timeout_minutes"]
    _require(total + 15 <= hl["max_wait_minutes"] < 300,
             "headless: the role timeouts plus 15 minutes must not exceed max_wait_minutes, and max_wait_minutes must be below 300")
    rev = hl["revision"]
    _require(isinstance(rev, dict) and set(rev) == {"wait_minutes", "quiet_minutes", "timeout_minutes", "max_turns"},
             "headless.revision needs exactly wait_minutes, quiet_minutes, timeout_minutes, max_turns")
    for key in ("wait_minutes", "quiet_minutes", "timeout_minutes", "max_turns"):
        _require(_positive_int(rev[key]), f"headless.revision.{key} must be a positive integer")
    _require(2 * rev["timeout_minutes"] + 15 <= rev["wait_minutes"] < 300,
             "headless.revision: two role timeouts plus 15 minutes must not exceed wait_minutes, and wait_minutes must be below 300")


def _is_host_name(value) -> bool:
    """A lowercase host name or an IP literal that may be named in web.allowed_hosts (never a wildcard or the unspecified address)."""
    if not isinstance(value, str) or not value or value != value.lower():
        return False
    try:
        return not ipaddress.ip_address(value).is_unspecified
    except ValueError:
        pass
    return len(value) <= 253 and re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*", value) is not None


def load_env(path: str | os.PathLike | None = None) -> dict[str, str]:
    """Read KEY=VALUE lines from the untracked .env into os.environ (never over a value already set).

    Blank lines and lines starting with # are ignored; surrounding quotes are removed. Returns the names
    that were set (not the values, so the result is safe to log).
    """
    env_path = Path(path) if path else ROOT / ".env"
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "RECRUITMENT_CONTAINER":       # only the image sets it; a .env on the PC must not turn the container bind on
            continue
        if key == "CLAUDE_CODE_OAUTH_TOKEN" or key.startswith("ANTHROPIC_"):
            continue                              # the Claude token lives in llm.env, read by the llm service only (goal.md 5.10, v0.78.0)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded[key] = ""
    return loaded


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Return the validated config: defaults from config.example.yaml, overridden by the user's file.

    Path order: the `path` argument, then $RECRUITMENT_CONFIG, then ./config.yaml in the repo root.
    A missing user file is allowed (defaults only); a path that is given explicitly must exist.
    """
    cfg = _load_yaml(EXAMPLE_PATH)
    explicit = path or os.environ.get("RECRUITMENT_CONFIG") or None
    user_path = Path(explicit) if explicit else DEFAULT_USER_CONFIG
    if user_path.exists():
        cfg = _deep_merge(cfg, _load_yaml(user_path))
    elif explicit:
        raise ConfigError(f"config file not found: {user_path}")
    validate(cfg)
    return cfg
