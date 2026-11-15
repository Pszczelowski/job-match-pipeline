"""Runs the hard filters over working jobs, stores every decision, archives rejects (goal.md 5.3, 5.3.3)."""
from __future__ import annotations

from .. import db
from . import rules
from .base import OUTCOME_RANK, FilterResult, JobContext, load_context
from .language import language
from ..location import make_location_filter

# Filters that need no network. The location filter needs the travel-time providers and is added
# by `build_filters` when providers are given.
DEFAULT_FILTERS = [rules.seniority, rules.title, rules.company, language]


def build_filters(conn, providers=None) -> list:
    """The filters for a run: the rule filters, plus the location filter when providers are given.

    Production code must pass `Providers.live(cfg)`; without providers the location rule is NOT applied.
    """
    if providers is None:
        return list(DEFAULT_FILTERS)
    return [*DEFAULT_FILTERS, make_location_filter(conn, providers)]


def run_filters(conn, job_id: int, cfg: dict, run_id: str | None = None, filters=None, providers=None) -> str:
    """Apply all filters to one job. Returns the overall outcome: 'pass', 'conditional' or 'fail'.

    Every result is written to job_filter_results (the audit trail). If any filter fails, the job is
    moved to job_archive with stage 'filter', the first failing reason code and a comment naming all
    failing reasons. A conditional job stays where it is.
    """
    job: JobContext = load_context(conn, job_id)
    results: list[FilterResult] = []
    for flt in (filters if filters is not None else build_filters(conn, providers)):
        # a filter marked `expensive` (it calls a web service) is skipped once the job has already failed
        if getattr(flt, "expensive", False) and any(r.outcome == "fail" for r in results):
            continue
        results.extend(flt(job, cfg))

    now = db.utcnow()
    with conn:
        for r in results:
            conn.execute(
                "INSERT INTO job_filter_results (job_id, filter, outcome, reason_code, comment, run_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, r.filter, r.outcome, r.reason_code, r.comment, run_id, now))

    overall = max((r.outcome for r in results), key=OUTCOME_RANK.__getitem__, default="pass")
    if overall == "fail":
        failed = [r for r in results if r.outcome == "fail"]
        db.archive_job(conn, job_id, "filter", failed[0].reason_code or failed[0].filter,
                       "; ".join(f"{r.filter}: {r.comment}" for r in failed), run_id,
                       approved_folder=cfg["paths"]["approved"])
    return overall


def filter_new_jobs(conn, cfg: dict, run_id: str | None = None, filters=None, providers=None) -> dict[str, int]:
    """Filter every working job that has not been filtered yet. Returns counts per outcome."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM jobs WHERE status = 'new' "
        "AND NOT EXISTS (SELECT 1 FROM job_filter_results f WHERE f.job_id = jobs.id AND f.filter = 'seniority') ORDER BY id")]
    filters = filters if filters is not None else build_filters(conn, providers)
    counts = {"pass": 0, "conditional": 0, "fail": 0}
    for job_id in ids:
        counts[run_filters(conn, job_id, cfg, run_id, filters)] += 1
    return counts
