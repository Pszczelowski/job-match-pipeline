"""Run the location rule again on the archived location rejections (v0.92.0, decided by the user on 14 Nov 2026).

The location rules changed (hybrid limit, place aliases, Switzerland and Liechtenstein, Poland office presence), so jobs
archived at the filter stage as `location_too_far`, `poland_on_site` or `location_unresolved` may qualify now. Each such
archive row that is not restored yet is run through `location_checks` from its stored record, offline (the place and
travel-time caches only, expired entries included; no service is called, nothing is written by the check). A row whose
place or time is not cached is listed as skipped. A row that now passes or is conditional is restored with
`db.restore_job` (status `new`: the filters run again in the next prepare); a row whose dedup key is already open is
skipped and listed. A row that still fails stays archived. With `dry_run` nothing is written. Running it twice changes
nothing: a restored row has `restored_at` set and is no longer selected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import json

from . import db
from .filters.base import context_from_record
from .location import location_checks
from .travel import Providers

REASONS = ("location_too_far", "poland_on_site", "location_unresolved")


@dataclass
class ArchiveItem:
    archive_id: int
    title: str
    company: str
    locations: list[str]
    old_reason: str
    new_outcome: str = ""
    new_reason: str | None = None
    action: str = ""              # "restore" | "" (stays archived)
    skipped: str = ""
    restored_job_id: int | None = None


@dataclass
class ArchiveRecheck:
    items: list[ArchiveItem] = field(default_factory=list)
    checked: int = 0
    restored: int = 0             # would be restored in a dry run
    still_failing: int = 0
    skipped_uncached: int = 0
    skipped_open: int = 0


def recheck_archive_location(conn, cfg: dict, dry_run: bool = False, providers: Providers | None = None) -> ArchiveRecheck:
    """`providers` given (`--online`): places and times missing from the cache are looked up live and cached as usual; a row
    whose service did not answer is skipped, never restored on a failure. None: offline, cache only."""
    online = providers is not None
    providers = providers or Providers.offline()
    result = ArchiveRecheck()
    rows = conn.execute(
        "SELECT id, dedup_key, title, company, locations, record, reason_code FROM job_archive "
        f"WHERE rejection_stage = 'filter' AND reason_code IN ({','.join('?' * len(REASONS))}) AND restored_at IS NULL "
        "ORDER BY id", REASONS).fetchall()
    for row in rows:
        record = json.loads(row["record"])
        job = context_from_record(record)
        if not online:
            providers.transport.calls.clear()
            providers.osrm.calls.clear()
        new, _ = location_checks(conn, providers, cfg, job, datetime.now(timezone.utc), persist=False)
        item = ArchiveItem(row["id"], row["title"], row["company"], job.locations or ([job.location_text] if job.location_text else []),
                           row["reason_code"], new[0].outcome, new[0].reason_code)
        result.items.append(item)
        if not online and (providers.transport.calls or providers.osrm.calls):
            item.skipped, item.new_outcome, item.new_reason = "a place or travel time is not in the cache", "", None
            result.skipped_uncached += 1
            continue
        if online and new[0].reason_code in ("travel_time_unavailable", "travel_time_approximate"):
            item.skipped, item.new_outcome, item.new_reason = "a service did not answer", "", None
            result.skipped_uncached += 1
            continue
        result.checked += 1
        if new[0].outcome == "fail":
            result.still_failing += 1
            continue
        if conn.execute("SELECT 1 FROM jobs WHERE dedup_key = ?", (row["dedup_key"],)).fetchone():
            item.skipped = "a job with the same key is already open"
            result.skipped_open += 1
            continue
        item.action = "restore"
        result.restored += 1
        if not dry_run:
            item.restored_job_id = db.restore_job(conn, row["id"])
    return result
