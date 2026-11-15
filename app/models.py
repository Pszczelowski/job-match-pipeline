"""The common job record every source is normalised to before dedup (goal.md 5.1, 5.2)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NormalizedJob:
    source: str                 # e.g. "ubs_alert", "linkedin_alert", or an employer adapter name
    external_id: str            # the source's own stable id (UBS jobid, LinkedIn job id, ...)
    title: str
    company: str
    url: str = ""
    locations: list[str] = field(default_factory=list)   # places named in the offer, as written
    reference: str = ""         # secondary id from the source, if any (UBS Job Reference #)
    source_date: str = ""       # date the source states for this listing (ISO), "" if none
    description: str = ""
    message_id: str = ""        # Gmail message the job came from, if any
    raw: dict = field(default_factory=dict)
