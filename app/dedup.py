"""Deduplication (goal.md 5.2, 5.3.3, 5.5).

Matching order for an incoming job, first hit wins:
  1. the same source sighting (source, external_id) already known,
  2. the same canonical URL,
  3. the same dedup key (normalised company + title + locations),
  4. the same company + title with the same core city set (spelling variants such as Warszawa / Warsaw, Poland), only against
     an open job of another source, only when exactly one such job exists; never against `job_archive`.
Steps 1 to 3 are looked up in the working table `jobs` first and then in `job_archive`, so a rejected job that
arrives again is not treated as new.

Outcomes (IngestResult.outcome):
  new         inserted as a new job
  unchanged   this exact sighting (source, external_id, source_date) was already stored; nothing written
  seen_again  known job, same source, new listing date or a later mail: sighting added, `repost` set
  linked      known job, different source: sighting added, no repost flag
  archived    matches a rejected job in job_archive; nothing inserted, the caller decides what to do
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db
from .location import normalize_place_query
from .models import NormalizedJob

JOBSCH_SOURCE = "jobsch_alert"          # the source key of app/parsers/jobsch.py

log = logging.getLogger("recruitment")

TRACKING_PARAMS = {
    "trk", "trkinfo", "trackingid", "refid", "ref", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
    "lipi", "midtoken", "midsig", "eid", "otptoken", "currentjobid", "src", "source", "sid", "campaign",
}
_COMPANY_SUFFIXES = re.compile(r"\b(ag|sa|gmbh|ltd|limited|inc|llc|plc|corp|corporation|co|kg|nv|bv|ab|as)\b")
# Trailing generic words and country names are dropped from the company key (at least one token stays).
_COMPANY_TRAILING = {
    "bank", "group", "holding", "switzerland", "schweiz", "suisse", "poland", "polska", "deutschland", "germany",
}


def canonicalize_url(url: str) -> str:
    """Lower-case scheme and host, drop the fragment, tracking parameters and a trailing slash.

    Parameters that identify the job (UBS partnerid/siteid/jobid) are kept, sorted.
    """
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def normalize_text(value: str) -> str:
    """Case-folded, accents removed (ü -> u), punctuation dropped, whitespace collapsed."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().replace("ł", "l").replace("ß", "ss")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_company(value: str) -> str:
    text = normalize_text(value)
    text = _COMPANY_SUFFIXES.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_company_key(value: str) -> str:
    """Company part of the dedup key: legal forms, then trailing generic words and country names, are removed."""
    tokens = normalize_company(value).split()
    while len(tokens) > 1 and tokens[-1] in _COMPANY_TRAILING:
        tokens.pop()
    return " ".join(tokens)


_COUNTRIES = ["Switzerland", "Schweiz", "Suisse", "Poland", "Polska", "Germany", "Deutschland", "Austria", "Osterreich",
              "France", "Italy", "United States", "USA", "United Kingdom", "UK", "Spain", "Netherlands", "Ireland"]
_WORK_WORDS = re.compile(r"\b(?:remote|hybrid|on site|onsite|home office)\b")
# canonical name -> (spellings, countries of the city as normalised text)
_CITY_ALIASES = {
    "warszawa": (("warsaw", "warszawa"), {"poland", "polska"}),
    "krakow": (("cracow", "krakow"), {"poland", "polska"}),
    "geneve": (("geneva", "geneve", "genf"), {"switzerland", "schweiz", "suisse"}),
    "zurich": (("zuerich", "zurich"), {"switzerland", "schweiz", "suisse"}),
    "basel": (("basle", "bale", "basel"), {"switzerland", "schweiz", "suisse"}),
    "bern": (("berne", "bern"), {"switzerland", "schweiz", "suisse"}),
    "luzern": (("lucerne", "luzern"), {"switzerland", "schweiz", "suisse"}),
    "munchen": (("munich", "munchen"), {"germany", "deutschland"}),
    "wien": (("vienna", "wien"), {"austria", "osterreich"}),
}
_ALIAS = {spelling: (canon, countries) for canon, (spellings, countries) in _CITY_ALIASES.items() for spelling in spellings}
_COUNTRY_NAMES = {normalize_text(c) for c in _COUNTRIES}


def core_cities(locations: list[str]) -> frozenset:
    """The set of canonical city names of a job's location strings; empty when any place has no clear city (remote, a bare
    country, a city that names another country) so that nothing is matched loosely."""
    cities = set()
    for text in locations:
        if not (text or "").strip():
            continue
        city = normalize_text(_WORK_WORDS.sub(" ", normalize_text(normalize_place_query(text, _COUNTRIES))))
        if not city:
            return frozenset()
        if city in _ALIAS:
            canon, countries = _ALIAS[city]
            named = {c for c in _COUNTRY_NAMES if re.search(rf"\b{re.escape(c)}\b", normalize_text(text))}
            if named and not named & countries:
                return frozenset()
            city = canon
        cities.add(city)
    return frozenset(cities)


def dedup_key(company: str, title: str, locations: list[str]) -> str:
    places = sorted({normalize_text(loc) for loc in locations if normalize_text(loc)})
    return f"{normalize_company_key(company)}|{normalize_text(title)}|{'+'.join(places)}"


def legacy_dedup_key(company: str, title: str, locations: list[str]) -> str:
    """Key as built before v0.76.0 (company without legal form only); rows stored under it must still match."""
    places = sorted({normalize_text(loc) for loc in locations if normalize_text(loc)})
    return f"{normalize_company(company)}|{normalize_text(title)}|{'+'.join(places)}"


@dataclass
class IngestResult:
    outcome: str
    job_id: int | None = None
    archive_id: int | None = None
    repost: bool = False
    matched_by: str = ""        # "source", "url", "key", "city" or "resolution"


def _find_by_core_city(conn, job: NormalizedJob, key: str, legacy_key: str | None):
    """Id of the one open job of another source with the same company + title and the same core cities, or None."""
    wanted = core_cities(job.locations)
    if not wanted:
        return None
    prefixes = {k.rsplit("|", 1)[0] + "|" for k in (key, legacy_key or key)}
    ids: set[int] = set()
    for prefix in prefixes:
        for row in conn.execute("SELECT id FROM jobs WHERE substr(dedup_key, 1, ?) = ?", (len(prefix), prefix)):
            ids.add(row["id"])
    matching = [i for i in sorted(ids) if core_cities(
        [r["location_text"] for r in conn.execute("SELECT location_text FROM job_locations WHERE job_id = ?", (i,))]) == wanted]
    if len(matching) != 1:
        return None
    if conn.execute("SELECT 1 FROM job_sources WHERE job_id = ? AND source = ? LIMIT 1", (matching[0], job.source)).fetchone():
        return None
    return matching[0]


def _find(conn, job: NormalizedJob, url: str, key: str, legacy_key: str | None = None):
    """Return (table, row_id, matched_by) for the first match, or None."""
    for table, id_col in (("jobs", "id"), ("job_archive", "id")):
        if table == "jobs":
            row = conn.execute(
                "SELECT job_id AS id FROM job_sources WHERE source = ? AND external_id = ? LIMIT 1",
                (job.source, job.external_id)).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM job_archive WHERE source = ? AND external_id = ? LIMIT 1",
                (job.source, job.external_id)).fetchone()
        if row:
            return table, row["id"], "source"
        if url:
            row = conn.execute(f"SELECT {id_col} AS id FROM {table} WHERE canonical_url = ? LIMIT 1", (url,)).fetchone()
            if row:
                return table, row["id"], "url"
        row = conn.execute(
            f"SELECT {id_col} AS id FROM {table} WHERE dedup_key IN (?, ?) LIMIT 1", (key, legacy_key or key)).fetchone()
        if row:
            return table, row["id"], "key"
    job_id = _find_by_core_city(conn, job, key, legacy_key)
    if job_id is not None:
        log.info("core city match: %s %s linked to job %s", job.source, job.external_id, job_id)
        return "jobs", job_id, "city"
    return None


def _find_by_resolution(conn, job: NormalizedJob):
    """A jobs.ch alert job (v0.84.1): the job the JobCloud resolver already read for the same posting (path A search or
    path B), or None. The vacancy uuid is compared in lower case with the stored posting id and URL. Returns
    (table, row_id): "jobs" for an open or rejected-in-place job, "job_archive" for a job already archived (so that a job
    the user rejected is not brought back)."""
    uid = job.external_id.strip().lower()
    if job.source != JOBSCH_SOURCE or not uid:
        return None
    for row in conn.execute(
            "SELECT job_id FROM description_resolutions WHERE status = 'success' "
            "AND (lower(posting_id) = ? OR instr(lower(coalesce(url, '')), ?) > 0) ORDER BY id", (uid, uid)):
        if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (row["job_id"],)).fetchone():
            return "jobs", row["job_id"]
        arch = conn.execute("SELECT id FROM job_archive WHERE original_job_id = ? ORDER BY id DESC LIMIT 1", (row["job_id"],)).fetchone()
        if arch:
            return "job_archive", arch["id"]
    return None


def ingest_job(conn, job: NormalizedJob, run_id: str | None = None) -> IngestResult:
    """Store one normalised job, or link it to the job it duplicates. Idempotent."""
    if not job.title.strip() or not job.company.strip() or not job.source or not job.external_id:
        raise ValueError("job needs source, external_id, title and company")
    url = canonicalize_url(job.url)
    key = dedup_key(job.company, job.title, job.locations)
    legacy_key = legacy_dedup_key(job.company, job.title, job.locations)
    now = db.utcnow()

    with conn:
        match = _find(conn, job, url, key, legacy_key)
        if match is None:
            by_resolution = _find_by_resolution(conn, job)
            if by_resolution:
                match = (*by_resolution, "resolution")
        if match is None:
            cur = conn.execute(
                """INSERT INTO jobs (dedup_key, canonical_url, title, company, location_text, description,
                                     description_missing, status, first_seen_at, last_seen_at, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)""",
                (key, url or None, job.title.strip(), job.company.strip(), ", ".join(job.locations) or None,
                 job.description or None, 0 if job.description.strip() else 1, now, now, run_id))
            job_id = cur.lastrowid
            _add_source(conn, job_id, job, url, now)
            for place in dict.fromkeys(p.strip() for p in job.locations if p.strip()):
                conn.execute("INSERT OR IGNORE INTO job_locations (job_id, location_text) VALUES (?, ?)", (job_id, place))
            return IngestResult("new", job_id=job_id)

        table, row_id, matched_by = match
        if table == "job_archive":
            return IngestResult("archived", archive_id=row_id, matched_by=matched_by)

        job_id = row_id
        exists = conn.execute(
            "SELECT 1 FROM job_sources WHERE source = ? AND external_id = ? AND source_date = ?",
            (job.source, job.external_id, job.source_date)).fetchone()
        if exists:
            return IngestResult("unchanged", job_id=job_id, matched_by=matched_by)

        same_source = conn.execute(
            "SELECT 1 FROM job_sources WHERE job_id = ? AND source = ? LIMIT 1", (job_id, job.source)).fetchone()
        _add_source(conn, job_id, job, url, now)
        conn.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ?", (now, job_id))
        if job.description.strip():
            conn.execute(
                "UPDATE jobs SET description = ?, description_missing = 0 WHERE id = ? AND description_missing = 1",
                (job.description, job_id))
        if same_source:
            conn.execute("UPDATE jobs SET repost = 1 WHERE id = ?", (job_id,))
            return IngestResult("seen_again", job_id=job_id, repost=True, matched_by=matched_by)
        return IngestResult("linked", job_id=job_id, matched_by=matched_by)


def _add_source(conn, job_id: int, job: NormalizedJob, url: str, now: str) -> None:
    conn.execute(
        """INSERT INTO job_sources (job_id, source, external_id, reference, url, source_date, message_id, seen_at, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_id, job.source, job.external_id, job.reference or None, url or None, job.source_date,
         job.message_id or None, now, json.dumps(job.raw, ensure_ascii=False) if job.raw else None))
