"""Zurich Insurance career site, goal.md 5.1 (v0.71.0): SAP SuccessFactors "career site builder", read like Swiss Re and EY.

Same layout and the same code as `csb.py`; only the host and the standard paragraphs of the postings differ. The host is
www.careers.zurich.com (careers.zurich.com does not answer, 4 Nov 2026); the job links are relative `/job/<slug>/<id>/`
without a path prefix. Tier T2: the user accepts the risk in `decision_note`; robots.txt is read first and respected, and
`/services/` is never requested.
"""
from __future__ import annotations

from . import csb

SITE = csb.Site(key="zurich", label="Zurich Insurance career site", company="Zurich Insurance", host="www.careers.zurich.com",
                boilerplate_starts=("• recruiter name:", "you belong here", "zurich is a leading multi-line insurer",
                                    "we look forward to receiving your online application", "information for recruitment agencies",
                                    "zurich does not accept any applications", "at zurich, we like to think outside the box",
                                    "we are an equal opportunity employer"))
SPEC = csb.make_spec(SITE)
