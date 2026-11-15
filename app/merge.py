"""merge-duplicates (goal.md 5.2, v0.77.1): open jobs whose keys differ only in the company part (AXA Switzerland = AXA since
v0.76.0 dropped country names from the key) become one job, and the stored keys are rewritten to the current rule.

`plan` only reads (it also runs on a read-only connection); `apply` writes, one transaction per group. The caller holds the run
lock and has made the backup. Nothing is deleted on disk; the loser's full record goes into `job_merges`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import db
from .config import resolve_path
from .dedup import core_cities, normalize_company_key
from .descriptions import USER_STATUSES, DescriptionError, apply_description

STATUS_RANK = {"new": 0, "location_failed": 0, "awaiting_llm": 1, "scored": 2, "not_selected": 2, "shortlisted": 3,
               "approved": 4, "applied": 5, "interview": 6, "offer": 7, "accepted": 8, "rejected": 7, "withdrawn": 7}


def rekey(key: str) -> str:
    """The key under the current rule: the company part is reduced again, the rest is kept. Idempotent."""
    i = key.find("|")
    return key if i < 0 else normalize_company_key(key[:i]) + key[i:]


@dataclass
class Group:
    key: str
    members: list                       # sqlite rows of the open jobs, best first
    survivor: int | None = None
    losers: list[int] = field(default_factory=list)
    skip: str | None = None             # why nothing is done
    core_city: bool = False             # matched by company + title + core city instead of the exact key
    detail: str = ""                    # one text per loser for the listing


@dataclass
class Plan:
    groups: list[Group] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rekey_open: list[tuple[int, str]] = field(default_factory=list)          # (job id, new key)
    rekey_archive: list[tuple[int, str]] = field(default_factory=list)       # (archive id, new key)

    @property
    def to_merge(self) -> list[Group]:
        return [g for g in self.groups if g.skip is None]

    @property
    def empty(self) -> bool:
        return not self.to_merge and not self.rekey_open and not self.rekey_archive


@dataclass
class Result:
    merged: int = 0
    rekeyed_open: int = 0
    rekeyed_archive: int = 0
    notes: list[str] = field(default_factory=list)


def _count(conn, table: str, job_id: int, where: str = "") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE job_id = ?{where}", (job_id,)).fetchone()[0]


def _has_description(row) -> bool:
    return bool((row["description"] or "").strip())


def plan(conn, cfg: dict | None = None) -> Plan:
    """Read-only: groups, skips, reports and the keys to rewrite."""
    result = Plan()
    jobs = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    by_key: dict[str, list] = {}
    for row in jobs:
        by_key.setdefault(rekey(row["dedup_key"]), []).append(row)
    skipped_ids: set[int] = set()

    def add_group(key: str, rows: list, core_city: bool = False) -> None:
        has_score = {r["id"]: _count(conn, "scores", r["id"]) > 0 for r in rows}
        has_docs = {r["id"]: _count(conn, "documents", r["id"]) > 0 for r in rows}
        ordered = sorted(rows, key=lambda r: (-STATUS_RANK.get(r["status"], 0), not _has_description(r), not has_score[r["id"]],
                                              not has_docs[r["id"]], r["first_seen_at"], r["id"]))
        group = Group(key, ordered, core_city=core_city)
        working = [r for r in ordered if r["status"] in USER_STATUSES]
        waiting = [r for r in ordered if r["status"] == "awaiting_llm"]
        if len(working) >= 2:
            group.skip = (f"{' and '.join(str(r['id']) + ' ' + r['status'] for r in working)} are both in work; "
                          "decide by hand (reject or withdraw one) and run again")
        elif waiting:
            group.skip = f"{waiting[0]['id']} waits for scores (awaiting_llm); run after finish"
        else:
            survivor, losers = ordered[0], ordered[1:]
            approved = [r["id"] for r in losers if _count(conn, "documents", r["id"], " AND status = 'approved'")]
            if approved:
                group.skip = f"{approved[0]} has an approved document; it is not merged away"
            else:
                group.survivor, group.losers = survivor["id"], [r["id"] for r in losers]
                have = {r["kind"] for r in conn.execute("SELECT kind FROM documents WHERE job_id = ?", (survivor["id"],))}
                parts = []
                for loser in losers:
                    moved = [r["kind"] for r in conn.execute("SELECT kind FROM documents WHERE job_id = ?", (loser["id"],))
                             if r["kind"] not in have]
                    have.update(moved)
                    copy = "description copied" if not _has_description(survivor) and _has_description(loser) else "description kept"
                    parts.append(f"{loser['id']} ({loser['status']}, {loser['company']}) | {loser['title']} | "
                                 f"{loser['location_text'] or '-'} | {_count(conn, 'job_sources', loser['id'])} source(s) moved, "
                                 f"{len(moved)} document(s), {copy}")
                group.detail = "; ".join(parts)
        if group.skip:
            skipped_ids.update(r["id"] for r in rows)
        result.groups.append(group)

    singles = []
    for key, rows in by_key.items():
        if len(rows) < 2:
            singles.append((key, rows[0]))
            continue
        add_group(key, rows)
    # second pass (batch-rest item 19): the rest that agree in company + title and core cities, from different sources
    by_core: dict[tuple[str, frozenset], list] = {}
    for key, row in singles:
        cities = core_cities([r["location_text"] for r in conn.execute(
            "SELECT location_text FROM job_locations WHERE job_id = ?", (row["id"],))])
        if cities:
            by_core.setdefault((key.rsplit("|", 1)[0], cities), []).append(row)
    for (prefix, cities), rows in by_core.items():
        if len(rows) < 2:
            continue
        sources = [{r["source"] for r in conn.execute("SELECT source FROM job_sources WHERE job_id = ?", (row["id"],))}
                   for row in rows]
        if sum(len(x) for x in sources) != len(set().union(*sources)):        # a shared source: not the same posting
            continue
        add_group(f"{prefix}|{'+'.join(sorted(cities))}", rows, core_city=True)
    removed = {i for g in result.to_merge for i in g.losers}
    survivors = {g.survivor for g in result.to_merge}         # the merge itself writes their new key
    for row in jobs:
        if row["id"] not in removed | survivors | skipped_ids and rekey(row["dedup_key"]) != row["dedup_key"]:
            result.rekey_open.append((row["id"], rekey(row["dedup_key"])))
    archive_keys: dict[str, list[int]] = {}
    for a in conn.execute("SELECT id, dedup_key, restored_at FROM job_archive ORDER BY id"):
        new = rekey(a["dedup_key"])
        if new != a["dedup_key"]:
            result.rekey_archive.append((a["id"], new))
        if not a["restored_at"]:
            archive_keys.setdefault(new, []).append(a["id"])
    for row in jobs:
        if row["id"] not in removed:
            for aid in archive_keys.get(rekey(row["dedup_key"]), []):
                result.reports.append(f"open job {row['id']} = archive entry {aid}; nothing done")
    if cfg is not None:
        folder = resolve_path(cfg["paths"]["exchange"]) / "descriptions"       # not exchange_dir(): that creates folders
        for loser in sorted(removed):
            if (folder / f"{loser}.txt").exists():
                result.warnings.append(f"exchange/descriptions/{loser}.txt waits for a job that is merged away")
    return result


def format_plan(p: Plan) -> list[str]:
    lines = []
    for g in p.groups:
        if g.skip:
            lines.append(f"SKIPPED {g.key}{' (core city)' if g.core_city else ''}: {g.skip}")
        else:
            s = g.members[0]
            lines.append(f"keep {s['id']} ({s['status']}, {s['company']}){' [core city]' if g.core_city else ''} <- {g.detail}")
    lines += [f"REPORT {r}" for r in p.reports]
    lines += [f"warning: {w}" for w in p.warnings]
    return lines


def summary(p: Plan) -> str:
    removed = sum(len(g.losers) for g in p.to_merge)
    return (f"{len(p.groups)} group(s): {len(p.to_merge)} to merge, {len(p.groups) - len(p.to_merge)} skipped; "
            f"{removed} job(s) removed; re-key {len(p.rekey_open)} open job(s), {len(p.rekey_archive)} archive entr(ies)")


def _loser_record(conn, job_id: int) -> dict:
    record = {"job": dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())}
    for name, table in (("sources", "job_sources"), ("filter_results", "job_filter_results"), ("documents", "documents"),
                        ("scores", "scores"), ("status_history", "status_history"), ("description_log", "description_log"),
                        ("interviews", "interviews"), ("notes", "job_notes")):
        record[name] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE job_id = ? ORDER BY id", (job_id,))]
    locations = []
    for loc in conn.execute("SELECT * FROM job_locations WHERE job_id = ? ORDER BY id", (job_id,)):
        entry = dict(loc)
        entry["travel_checks"] = [dict(t) for t in conn.execute(
            "SELECT * FROM job_travel_checks WHERE location_id = ? ORDER BY id", (loc["id"],))]
        locations.append(entry)
    record["locations"] = locations
    return record


def _merge_group(conn, g: Group, backup_file: str | None) -> dict | None:
    """One transaction. Returns what the description step needs, or None when the group changed since the plan."""
    ids = [g.survivor, *g.losers]
    rows = {r["id"]: r for r in conn.execute(f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(ids))})", ids)}
    if len(rows) != len(ids):
        return None
    survivor = rows[g.survivor]
    new_key = rekey(survivor["dedup_key"])
    loser_text = next((rows[i]["description"] for i in g.losers if _has_description(rows[i])), None)
    everyone = [rows[i] for i in ids]
    with conn:
        for loser_id in g.losers:
            record = _loser_record(conn, loser_id)             # the whole loser as it was, before anything moves
            moved = {"sources": conn.execute("UPDATE job_sources SET job_id = ? WHERE job_id = ?", (g.survivor, loser_id)).rowcount,
                     "interviews": conn.execute("UPDATE interviews SET job_id = ? WHERE job_id = ?", (g.survivor, loser_id)).rowcount,
                     "notes": conn.execute("UPDATE job_notes SET job_id = ? WHERE job_id = ?", (g.survivor, loser_id)).rowcount,
                     "documents": []}
            if g.core_city:
                moved["match"] = "core_city"
            have = {r["kind"] for r in conn.execute("SELECT kind FROM documents WHERE job_id = ?", (g.survivor,))}
            for d in conn.execute("SELECT id, kind, status FROM documents WHERE job_id = ? ORDER BY id", (loser_id,)).fetchall():
                if d["kind"] not in have and d["status"] != "approved":
                    conn.execute("UPDATE documents SET job_id = ? WHERE id = ?", (g.survivor, d["id"]))
                    have.add(d["kind"])
                    moved["documents"].append(d["kind"])
            conn.execute(
                "INSERT INTO job_merges (survivor_job_id, merged_job_id, merged_key, survivor_old_key, new_key, reason, record, moved, "
                "backup_file, merged_at) VALUES (?, ?, ?, ?, ?, 'company_key', ?, ?, ?, ?)",
                (g.survivor, loser_id, rows[loser_id]["dedup_key"], survivor["dedup_key"], new_key,
                 json.dumps(record, ensure_ascii=False), json.dumps(moved), backup_file, db.utcnow()))
            conn.execute("DELETE FROM jobs WHERE id = ?", (loser_id,))
        closed = None if any(r["closed_at"] is None for r in everyone) else max(r["closed_at"] for r in everyone)
        conn.execute(
            "UPDATE jobs SET dedup_key = ?, first_seen_at = ?, last_seen_at = ?, repost = ?, closed_at = ?, "
            "canonical_url = COALESCE(canonical_url, ?), posted_on = COALESCE(posted_on, ?) WHERE id = ?",
            (new_key, min(r["first_seen_at"] for r in everyone), max(r["last_seen_at"] for r in everyone),
             max(r["repost"] for r in everyone), closed,
             next((r["canonical_url"] for r in everyone if r["canonical_url"]), None),
             next((r["posted_on"] for r in everyone if r["posted_on"]), None), g.survivor))
    return {"survivor": g.survivor, "status": survivor["status"], "has_description": _has_description(survivor), "text": loser_text}


def apply(conn, cfg: dict, p: Plan, backup_file: str | None = None) -> Result:
    """Write the plan: merge each group, copy a description the survivor lacks, re-key the rest, refresh the selection once."""
    from .results import refresh_selection
    result = Result()
    pending = []
    for g in p.to_merge:
        info = _merge_group(conn, g, backup_file)
        if info is None:
            result.notes.append(f"group {g.key} changed since the plan; run again")
            continue
        result.merged += len(g.losers)
        pending.append(info)
    for info in pending:
        if info["has_description"] or not info["text"]:
            continue
        try:
            apply_description(conn, cfg, info["survivor"], info["text"], source="merge",
                              check_language=info["status"] not in USER_STATUSES)
        except DescriptionError as exc:
            result.notes.append(f"job {info['survivor']}: description not copied: {exc}")
    with conn:
        for job_id, key in p.rekey_open:
            if conn.execute("UPDATE jobs SET dedup_key = ? WHERE id = ? AND dedup_key <> ?", (key, job_id, key)).rowcount:
                result.rekeyed_open += 1
        for archive_id, key in p.rekey_archive:
            row = conn.execute("SELECT record FROM job_archive WHERE id = ?", (archive_id,)).fetchone()
            record = json.loads(row["record"])
            if isinstance(record.get("job"), dict):
                record["job"]["dedup_key"] = key               # restore_job builds the new job from this copy
            conn.execute("UPDATE job_archive SET dedup_key = ?, record = ? WHERE id = ?",
                         (key, json.dumps(record, ensure_ascii=False), archive_id))
            result.rekeyed_archive += 1
    if result.merged:
        refresh_selection(conn, cfg)
    return result
