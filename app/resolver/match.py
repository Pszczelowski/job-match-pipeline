"""Matching a LinkedIn job to a posting on the employer's site (goal.md 5.1a).

Titles are normalised (gender tags, working-time percentages, punctuation, umlauts) and scored with a mix of a
character-level and a word-level similarity. A match is accepted only if it is the one best candidate: its score is at
least `ACCEPT` and the runner-up is at least `MARGIN` lower. When both sides state a city, the city must agree.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

ACCEPT = 0.85
MARGIN = 0.10

_T = r"(?:divers|div|[mwfdxinh])"                                              # one gender letter or word
_GENDER = re.compile(
    rf"\(\s*{_T}(?:\s*[/|*:,]\s*{_T})+\s*[/*]?\s*\)"                          # (m/w/d) (m,w,d) (w/m/div) (H/F/X) (m/w/d/)
    r"|\(\s*(?:a|gn\*?|all|alle|all\s+genders?|alle\s+geschlechter)\s*\)"      # (a) (gn*) (all) (alle) (all genders)
    rf"|(?<![\w/|])(?:{_T}\s*[/|]\s*){{1,3}}{_T}/?(?![\w/|])"                  # bare: m/f/d, f/m/n, m/f
    r"|\ball genders\b", re.I)
_PERCENT = re.compile(r"\b\d{1,3}\s*(?:%|prozent)?\s*(?:[-–—]|bis|to)\s*\d{1,3}\s*(?:%|prozent)|\b\d{1,3}\s*%", re.I)
_FEMININE = re.compile(r"(?<=\w)(?:(?:/-|[:*_/])in(?:nen)?\b|\((?:in|innen|e|ère|ere|trice)\))", re.I)   # Kundenberater:in, Berater/in, Spezialist/-in, Spezialist(in), Conseiller(ère)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.casefold().replace("ß", "ss")


def normalise_title(title: str) -> str:
    text = _GENDER.sub(" ", title or "")
    text = _PERCENT.sub(" ", text)
    text = _FEMININE.sub("", text)
    text = re.sub(r"[^\w\s]", " ", _fold(text))
    return re.sub(r"\s+", " ", text).strip()


def normalise_place(place: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", _fold(place))).strip()


def title_score(a: str, b: str) -> float:
    na, nb = normalise_title(a), normalise_title(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb) / len(ta | tb)
    return round(0.5 * ratio + 0.5 * overlap, 4)


_COUNTRY_NAMES = {"switzerland", "schweiz", "suisse", "svizzera", "liechtenstein", "germany", "deutschland", "austria", "osterreich",
                  "france", "italy", "united kingdom", "uk", "netherlands", "denmark", "sweden", "luxembourg", "poland", "polska",
                  "spain", "ireland", "belgium"}


def city_agrees(job_places: list[str], city: str | None) -> bool:
    """True when the job states no place or the posting states none; otherwise the posting's city and the job's places agree.

    A job's place may be a whole text ("Winterthur, Zürich, Switzerland"): the posting's city counts if it is a word group in it.
    The other way round counts too: a part of the job's place ("Zürich") that is a whole word of the posting's city ("8001 Zürich",
    "Pfäffikon SZ"). A place that is only a country name states no city and is left out.
    """
    c = normalise_place(city or "")
    places = [p for p in job_places if normalise_place(p) and normalise_place(p) not in _COUNTRY_NAMES]
    if not places or not c:
        return True
    for p in places:
        if re.search(rf"\b{re.escape(c)}\b", normalise_place(p)):
            return True
        for part in p.split(","):
            n = normalise_place(part)
            if n and n not in _COUNTRY_NAMES and re.search(rf"\b{re.escape(n)}\b", c):
                return True
    return False


@dataclass
class Candidate:
    """A posting on the employer's site. `text` is set when the list already carries the description."""
    posting_id: str
    title: str
    city: str | None
    url: str
    text: str | None = None
    company: str | None = None        # the employer the board names (job boards that list many employers)


@dataclass
class MatchResult:
    status: str                       # "match" | "ambiguous" | "none"
    candidate: Candidate | None = None
    score: float = 0.0
    runner_up: float = 0.0
    note: str = ""


def choose(job_title: str, job_places: list[str], candidates: list[Candidate], accept: float = ACCEPT) -> MatchResult:
    unique: dict[str, Candidate] = {}
    for c in candidates:
        unique.setdefault(c.posting_id, c)
    usable = [c for c in unique.values() if city_agrees(job_places, c.city)]
    if not usable:
        cities = list(dict.fromkeys(c.city.strip() for c in unique.values() if c.city and c.city.strip()))[:5]
        return MatchResult("none", note=f"no posting in the same city (postings in: {', '.join(cities)})" if unique else "the site lists no posting for these words")
    ranked = sorted(((title_score(job_title, c.title), c) for c in usable), key=lambda x: -x[0])
    best, cand = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    if best < accept:
        return MatchResult("none", cand, best, second, f"best title similarity {best:.2f} is below {accept}")
    if best - second < MARGIN:
        return MatchResult("ambiguous", cand, best, second, f"two postings are about equally close ({best:.2f} and {second:.2f})")
    return MatchResult("match", cand, best, second)
