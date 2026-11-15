"""Title, seniority and company filters (goal.md 5.3). Rules only, no LLM.

Each filter takes (JobContext, cfg) and returns a list of FilterResult. All patterns and lists come
from the `filters` section of the config.
"""
from __future__ import annotations

import re

from ..dedup import normalize_company
from .base import FilterResult, JobContext


def _compile(patterns: list[str]) -> list[re.Pattern]:
    # Whole-word match, case-insensitive: "Director" must not match "Directory".
    return [re.compile(rf"\b(?:{p})\b", re.IGNORECASE) for p in patterns]


def _squash(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def seniority(job: JobContext, cfg: dict) -> list[FilterResult]:
    """Reject above Associate Director level.

    UBS jobs: decided only by the 'Corporate title' column (exact value from the reject list).
    Other jobs, or UBS rows without that column: title patterns, after removing the allowed phrases
    ('Assistant Vice President', 'Associate Director') so that they do not match 'Vice President' or
    'Director'. Everything not rejected passes; the scorer's seniority sub-score does the rest.
    """
    flt = cfg["filters"]
    if job.corporate_title:
        rejected = {_squash(t) for t in flt["seniority_reject_ubs_titles"]}
        if _squash(job.corporate_title) in rejected:
            return [FilterResult("seniority", "fail", "seniority_corporate_title",
                                 f"UBS corporate title '{job.corporate_title}' is above Associate Director")]
        return [FilterResult("seniority", "pass")]

    text = job.title
    for pattern in _compile(flt["seniority_allow_title_patterns"]):
        text = pattern.sub(" ", text)
    for pattern in _compile(flt["seniority_reject_title_patterns"]):
        match = pattern.search(text)
        if match:
            return [FilterResult("seniority", "fail", "seniority_title_pattern",
                                 f"title contains '{match.group(0)}', above Associate Director")]
    return [FilterResult("seniority", "pass")]


def title(job: JobContext, cfg: dict) -> list[FilterResult]:
    """Optional include/exclude patterns on the title. Both lists are empty by default (no role pre-filter)."""
    flt = cfg["filters"]
    for pattern in _compile(flt["title_exclude"]):
        match = pattern.search(job.title)
        if match:
            return [FilterResult("title", "fail", "title_excluded", f"title matches exclude pattern '{match.group(0)}'")]
    include = _compile(flt["title_include"])
    if include and not any(p.search(job.title) for p in include):
        return [FilterResult("title", "fail", "title_not_included", "title matches none of the include patterns")]
    return [FilterResult("title", "pass")]


def company(job: JobContext, cfg: dict) -> list[FilterResult]:
    """Reject companies on the exclude list (compared after normalisation: case, accents, AG/SA/...)."""
    excluded = {normalize_company(c) for c in cfg["filters"]["excluded_companies"]}
    excluded.discard("")
    if normalize_company(job.company) in excluded:
        return [FilterResult("company", "fail", "company_excluded", f"company '{job.company}' is on the exclude list")]
    return [FilterResult("company", "pass")]
