"""Shared types for the hard filters (goal.md 5.3)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# Worst outcome wins across filters: a single `fail` rejects the job.
OUTCOME_RANK = {"pass": 0, "conditional": 1, "fail": 2}


@dataclass(frozen=True)
class FilterResult:
    filter: str                 # e.g. "seniority", "language", "location"
    outcome: str                # "pass" | "fail" | "conditional"
    reason_code: str | None = None
    comment: str | None = None


@dataclass
class JobContext:
    """What a filter may look at: one working job with its source data."""
    id: int
    title: str
    company: str
    description: str
    location_text: str
    sources: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)          # raw cells of the source rows, merged
    locations: list[str] = field(default_factory=list)   # every place the offer names, as written (job_locations)

    @property
    def corporate_title(self) -> str:
        """The UBS 'Corporate title' cell, or '' for jobs without one."""
        return (self.raw.get("Corporate title") or "").strip()


def load_context(conn, job_id: int) -> JobContext:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise KeyError(f"no job with id {job_id}")
    raw: dict = {}
    sources: list[str] = []
    for row in conn.execute("SELECT source, raw FROM job_sources WHERE job_id = ? ORDER BY id", (job_id,)):
        if row["source"] not in sources:
            sources.append(row["source"])
        if row["raw"]:
            try:
                raw.update(json.loads(row["raw"]))      # later sightings overwrite earlier ones
            except json.JSONDecodeError:
                pass
    locations = [r["location_text"] for r in conn.execute(
        "SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (job_id,))]
    return JobContext(id=job["id"], title=job["title"], company=job["company"],
                      description=job["description"] or "", location_text=job["location_text"] or "",
                      sources=sources, raw=raw, locations=locations)


def context_from_record(record: dict, job_id: int = 0) -> JobContext:
    """The same context from an archive `record` (db.archive_job): the archived job, its sources and its locations.
    `job_id` is only a label: nothing is read from or written to the jobs table with it."""
    job = record["job"]
    raw: dict = {}
    sources: list[str] = []
    for s in record.get("sources", []):
        if s["source"] not in sources:
            sources.append(s["source"])
        if s.get("raw"):
            try:
                raw.update(json.loads(s["raw"]))
            except json.JSONDecodeError:
                pass
    return JobContext(id=job_id, title=job["title"], company=job["company"], description=job.get("description") or "",
                      location_text=job.get("location_text") or "", sources=sources, raw=raw,
                      locations=[l["location_text"] for l in record.get("locations", [])])


def current_results(conn, job_id: int) -> list:
    """The filter results that count now, oldest first: every stored row, except that only the newest run of the
    `location` filter is kept. The location rule can run again later (a description was added, the Poland rule,
    `recheck-poland`); the older location rows stay in the table as the audit trail but are superseded. The location
    filter writes exactly one row per run, so the newest row is the current one. Other filters are returned unchanged."""
    rows = list(conn.execute("SELECT * FROM job_filter_results WHERE job_id = ? ORDER BY id", (job_id,)))
    location = [r for r in rows if r["filter"] == "location"]
    if len(location) < 2:
        return rows
    newest = location[-1]["id"]
    return [r for r in rows if r["filter"] != "location" or r["id"] == newest]
