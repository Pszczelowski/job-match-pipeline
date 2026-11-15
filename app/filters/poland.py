"""Jobs located in Poland: decided by work mode, not by travel time (goal.md 5.3.1, decided 2 Nov 2026).

The user's words (2 Nov 2026): "For polish roles please focus on remote/ hybrid roles only. Give them normal scoring and
report them same as Swiss roles. I have an option to have a JDG in Poland, so 'located in Poland' might be only required
by law or employer." The reading recorded in goal.md: a job in Poland that is remote or hybrid qualifies (the user can
work through a JDG, a Polish sole proprietorship); an on-site job in Poland fails; a job in Poland whose work mode is
unknown is a conditional pass.

  remote   pass         poland_remote
  unknown  conditional  poland_work_mode_unknown
  hybrid or on-site, by the office presence the posting states (decided by the user on 14 Nov 2026):
    low presence (once a month, one week a month, one day a week ...)   pass  poland_hybrid
    high presence (2 to 5 days a week in the office)                    fail  poland_too_much_office
    nothing stated: hybrid  conditional  poland_hybrid_presence_unknown;  on-site  fail  poland_on_site

A job is "in Poland" when every location it names is Polish: the text names the country (Poland, Polska, ...) or one of
the configured Polish cities (diacritics and case are ignored, so Kraków, Krakow and KRAKÓW are the same). A location that
also names another country ("Warsaw, Indiana, United States") is not Polish unless it names Poland as well. A job that
names a Polish and a non-Polish place is NOT handled here: the travel-time rule of location.py decides, as before.

The work mode comes from, in this order: LinkedIn's own label (`raw.work_mode`: Remote, Hybrid, On-site), then the title
and the location text, then the description. Inside one text, hybrid is looked for first ("hybrid, 2 days remote" is
hybrid), then remote ("remote with occasional office visits" is remote), then on-site. A pattern that is negated just
before or after ("no remote", "remote work is not possible") is ignored. All patterns are the config
`location.poland.*_patterns` (regular expressions, case-insensitive).

No network is used, so this rule can run whenever a title, a place or a description changes (descriptions.py).
"""
from __future__ import annotations

import re
import unicodedata

from .base import FilterResult, JobContext

# LinkedIn's work-mode labels (app/parsers/linkedin.py keeps them in raw["work_mode"]) -> our modes
LINKEDIN_MODES = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite", "on site": "onsite",
                  "vor ort": "onsite"}
MODE_REASON = {"remote": ("pass", "poland_remote"), "hybrid": ("pass", "poland_hybrid"),
               "onsite": ("fail", "poland_on_site"), None: ("conditional", "poland_work_mode_unknown")}
PRESENCE_REASON = {"low": ("pass", "poland_hybrid"), "high": ("fail", "poland_too_much_office")}
MODE_NAME = {"remote": "remote", "hybrid": "hybrid", "onsite": "on-site", None: "not stated"}
ORDER = ("hybrid", "remote", "onsite")          # within one text: the first family that matches wins

_NEG_BEFORE = re.compile(r"(?:\bno|\bnot|\bnon|\bwithout|\bbez|\bbrak|\bnie|\bkein\w*|\bnicht)[\s-]+(?:\w+[\s-]+)?$", re.IGNORECASE)
_NEG_AFTER = re.compile(r"^[^.\n]{0,30}?\b(?:not (?:possible|available|offered|allowed|an option)|unavailable|niemo\w+|nicht m\w+)",
                        re.IGNORECASE)


def fold(text: str) -> str:
    """Lower case without diacritics (Ł does not decompose, so it is mapped by hand): 'Łódź' -> 'lodz'."""
    text = (text or "").replace("ł", "l").replace("Ł", "L")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


def _word_regex(names: list[str]) -> re.Pattern | None:
    folded = sorted({fold(n).strip() for n in names if n.strip()}, key=len, reverse=True)
    if not folded:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in folded) + r")(?!\w)")


def is_polish_location(text: str, cfg: dict) -> bool:
    """True when the location text names Poland or a configured Polish city (and no other country without Poland)."""
    pl = cfg["location"]["poland"]
    value = fold(text)
    if not value.strip():
        return False
    country = _word_regex(pl["country_names"])
    if country and country.search(value):
        return True
    city = _word_regex(pl["cities"])
    if not (city and city.search(value)):
        return False
    polish_names = {fold(n) for n in pl["country_names"]}
    others = _word_regex([c for c in cfg["location"]["countries"] if fold(c) not in polish_names])
    return not (others and others.search(value))


def job_locations(job: JobContext) -> list[str]:
    return [p for p in (job.locations or ([job.location_text] if job.location_text else [])) if p and p.strip()]


def is_polish_job(job: JobContext, cfg: dict) -> bool:
    """Every named location is Polish (a job with no location is not)."""
    pl = cfg["location"].get("poland") or {}
    places = job_locations(job)
    return bool(pl.get("enabled")) and bool(places) and all(is_polish_location(p, cfg) for p in places)


def _find(text: str, patterns: list[str]) -> str | None:
    """The first match of any pattern that is not negated right before or after it, or None."""
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if _NEG_BEFORE.search(text[max(0, m.start() - 18):m.start()]) or _NEG_AFTER.match(text[m.end():m.end() + 45]):
                continue
            return m.group(0).strip()
    return None


def work_mode(job: JobContext, cfg: dict) -> tuple[str | None, str]:
    """('remote' | 'hybrid' | 'onsite' | None, where it was read). None: the posting does not say."""
    pl = cfg["location"]["poland"]
    label = LINKEDIN_MODES.get(str(job.raw.get("work_mode") or "").strip().casefold())
    if label:
        return label, f"LinkedIn work mode '{job.raw.get('work_mode')}'"
    families = {"remote": pl["remote_patterns"], "hybrid": pl["hybrid_patterns"], "onsite": pl["onsite_patterns"]}
    for where, text in (("title or place", "\n".join([job.title, *job_locations(job)])), ("description", job.description)):
        for mode in ORDER:
            hit = _find(text, families[mode]) if text.strip() else None
            if hit:
                return mode, f"{where}: '{hit[:40]}'"
    return None, "no work mode found"


def office_presence(job: JobContext, cfg: dict) -> tuple[str | None, str]:
    """('low' | 'high' | None, the matching text). A low-presence pattern wins when both families match (do not reject on doubt)."""
    pl = cfg["location"]["poland"]
    text = "\n".join([job.title, *job_locations(job), job.description])
    for name in ("low", "high"):
        hit = _find(text, pl[f"{name}_presence_patterns"])
        if hit:
            return name, hit[:40]
    return None, ""


def poland_location(job: JobContext, cfg: dict) -> list[FilterResult]:
    """The Poland rule as a filter. Returns [] for a job that is not (only) in Poland or when the rule is switched off, so
    it can sit in any filter list without touching other jobs; otherwise exactly one `location` result."""
    if not is_polish_job(job, cfg):
        return []
    mode, evidence = work_mode(job, cfg)
    outcome, reason = MODE_REASON[mode]
    where = "; ".join(job_locations(job))
    if mode in ("hybrid", "onsite"):
        presence, quote = office_presence(job, cfg)
        if presence:
            outcome, reason = PRESENCE_REASON[presence]
            what = "low office presence qualifies" if presence == "low" else "too much office presence"
            return [FilterResult("location", outcome, reason, f"located in Poland ({where}); {MODE_NAME[mode]} ({evidence}); {what} ('{quote}')")]
        if mode == "hybrid":
            return [FilterResult("location", "conditional", "poland_hybrid_presence_unknown",
                                 f"located in Poland ({where}); hybrid ({evidence}), office presence not stated, so the location stays unclear")]
    if mode is None:
        comment = f"located in Poland ({where}); work mode not stated, so the location stays unclear"
    elif outcome == "fail":
        comment = f"located in Poland ({where}); on-site ({evidence}): only remote or hybrid roles in Poland qualify"
    else:
        comment = f"located in Poland ({where}); {MODE_NAME[mode]} ({evidence}): qualifies, no travel-time check"
    return [FilterResult("location", outcome, reason, comment)]
