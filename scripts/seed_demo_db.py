"""Creates a demo database of fictional jobs for screenshots and for trying the web app (goal.md 7.4, v0.95.0).

    python scripts/seed_demo_db.py --db demo/jobs.db [--jobs 40] [--force]

The database is built with the app's own code: `db.connect` (the schema and its migrations), `ingest_job` (dedup), `build_batch`
and `import_results` (batches, scores, the top 10), `set_status` and `archive_job` (the later life of a job). Two runs on two
days, about two thirds of the jobs scored in the first. All companies, titles and texts are made up; the scores are
deterministic. The fictional persona of profile/ (examples/persona/ in the private repository) is the candidate.

To look at it (nothing here touches data/ or the real config):

    RECRUITMENT_CONFIG=demo/demo-config.yaml python -m app web      (PowerShell: $env:RECRUITMENT_CONFIG = "demo/demo-config.yaml")

The script refuses to write inside the repository's data/ folder and refuses to replace an existing file without --force.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db                                                    # noqa: E402
from app.batch import build_batch, exchange_dir                       # noqa: E402
from app.config import load_config                                    # noqa: E402
from app.dedup import ingest_job                                      # noqa: E402
from app.models import NormalizedJob                                  # noqa: E402
from app.results import import_results                                # noqa: E402

COMPANIES = ["Contoso Bank", "Northwind Insurance", "Fabrikam Capital", "Litware Pharma", "Tailspin Logistics", "Adventure Works Retail",
             "Wide World Importers", "Proseware Systems", "Woodgrove Bank", "Alpine Ski House", "Coho Vineyard", "Lucerne Publishing",
             "Humongous Insurance", "Trey Research", "Relecloud Energy", "Fourth Coffee"]
TITLES = ["Business Analyst", "IT Business Analyst", "Senior Business Analyst", "Reporting Analyst", "Data Analyst", "Requirements Engineer",
          "Test Manager", "Test Automation Engineer", "Process Analyst", "Pricing Analyst", "Product Owner", "Data Quality Analyst",
          "Financial Systems Analyst", "Automation Developer", "Project Coordinator IT"]
PLACES = ["Zürich", "Zürich", "Zürich", "Zug", "Winterthur", "Basel", "Bern", "Luzern", "Lausanne", "Schlieren", "Opfikon"]
SOURCES = ["linkedin_alert", "jobsch_alert", "julius_alert", "ubs_alert", "greenhouse"]
SKILLS = ["SQL", "requirements", "user stories", "Jira", "test cases", "UAT", "Python", "reporting", "stakeholder workshops", "Tableau",
          "data validation", "process automation", "Confluence", "regression testing", "SWIFT messages"]
STATUS_PLAN = ["shortlisted", "shortlisted", "applied", "applied", "interview", "offer", "withdrawn"]       # given to the best scored jobs
REJECTED = 3


def fake_job(i: int, rng: random.Random) -> NormalizedJob:
    company, title = COMPANIES[i % len(COMPANIES)], TITLES[(i * 7 + 3) % len(TITLES)]
    skills = rng.sample(SKILLS, 4)
    description = "" if i % 9 == 4 else (
        f"{company} is looking for a {title} to join the team in {PLACES[i % len(PLACES)]}. You work with {skills[0]} and {skills[1]}, "
        f"write clear {skills[2]} for the developers and check the results with {skills[3]}. We offer a fixed contract, flexible hours "
        "and two days of remote work. Fluent English is required; German is a plus.")
    return NormalizedJob(source=SOURCES[i % len(SOURCES)], external_id=f"demo-{i:03d}", title=title, company=company,
                         url=f"https://jobs.example.com/{company.split()[0].lower()}/{1000 + i}",
                         locations=[PLACES[(i * 5) % len(PLACES)]], description=description, reference=f"REQ-{2000 + i}")


def score_entry(job_id: int, rng: random.Random) -> dict:
    overall = rng.choice([rng.randint(30, 59), rng.randint(60, 79), rng.randint(60, 79), rng.randint(80, 96)])
    sub = {k: max(0, min(100, overall + rng.randint(-15, 15))) for k in ("skills", "experience", "domain", "seniority")}
    sub["location"] = 100
    return {"job_id": job_id, "overall": overall, "subscores": sub,
            "matches": rng.sample(["SQL reporting", "Requirements work", "Test planning", "Process automation", "Stakeholder workshops"], 3),
            "gaps": rng.sample(["Kotlin", "Spark", "Scala", "SAP", "Salesforce"], rng.randint(1, 3)),
            "red_flags": [], "confidence": rng.choice(["high", "medium", "medium", "low"]),
            "summary": f"Job {job_id}: {'strong' if overall >= 80 else 'partial' if overall >= 60 else 'weak'} overlap with the profile.",
            "cv_variant": rng.choice(["resume", "resume_cs"])}


def run_day(conn, cfg, run_id: str, jobs: list[NormalizedJob], rng: random.Random, now: datetime) -> list[int]:
    """One day: ingest, mark the jobs as filtered (no network), batch, score, import."""
    db.start_run(conn, run_id)
    ids = [ingest_job(conn, j, run_id).job_id for j in jobs]
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        for job_id in ids:
            conn.execute("UPDATE jobs SET first_seen_at = ?, last_seen_at = ? WHERE id = ?", (stamp, stamp, job_id))
            for flt in ("seniority", "location", "language"):
                conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, run_id, created_at) VALUES (?, ?, 'pass', ?, ?)",
                             (job_id, flt, run_id, stamp))
    build_batch(conn, cfg, "demo prompt (fictional jobs)", "demo-1", run_id=run_id, now=now)
    doc = {"schema_version": "1.0", "run_id": run_id, "model": "demo", "prompt_version": "demo-1",
           "results": [score_entry(i, rng) for i in ids], "top": []}
    path = exchange_dir(cfg) / "results" / f"{run_id}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    result = import_results(conn, cfg, path, now)
    if result.status != "imported":
        raise RuntimeError(f"the demo results were not imported: {result.status} {result.reason}")
    return ids


def seed(db_path: Path, jobs: int = 40, force: bool = False, root: Path = ROOT) -> dict:
    """Builds the demo database at `db_path` and a demo-config.yaml next to it; returns counts."""
    db_path = Path(db_path).resolve()
    data = (root / "data").resolve()
    if db_path == data or data in db_path.parents:
        raise SystemExit(f"refused: {db_path} is inside the repository's data/ folder (the real database lives there)")
    if db_path.exists() or db_path.with_name(db_path.name + "-wal").exists():
        if not force:
            raise SystemExit(f"refused: {db_path} exists; use --force to replace it")
        for suffix in ("", "-wal", "-shm"):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    exchange = db_path.parent / "demo-exchange"
    config_path = db_path.parent / "demo-config.yaml"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"# Demo configuration written by scripts/seed_demo_db.py\npaths:\n  database: {db_path.as_posix()}\n"
                           f"  exchange: {exchange.as_posix()}\n", encoding="utf-8")
    cfg = load_config(config_path)
    rng = random.Random(7)
    all_jobs = [fake_job(i, rng) for i in range(jobs)]
    first = max(1, round(jobs * 2 / 3))
    conn = db.connect(db_path)
    try:
        day1 = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
        ids = run_day(conn, cfg, "demo-day1", all_jobs[:first], rng, day1)
        if all_jobs[first:]:
            ids += run_day(conn, cfg, "demo-day2", all_jobs[first:], rng, day1 + timedelta(days=1))
        ranked = [r["job_id"] for r in conn.execute(
            "SELECT job_id FROM scores ORDER BY overall DESC, job_id LIMIT ?", (len(STATUS_PLAN) + REJECTED,))]
        for job_id, status in zip(ranked, STATUS_PLAN):
            db.set_status(conn, job_id, status, "demo")
        for job_id in ranked[len(STATUS_PLAN):]:
            with conn:
                db.archive_job(conn, job_id, "user", "not_interested", "demo")
        with conn:                                                    # one job that left the employer's list, one repost
            conn.execute("UPDATE jobs SET closed_at = '2026-11-11T08:00:00Z' WHERE id = ?", (ids[-1],))
            conn.execute("UPDATE jobs SET repost = 1 WHERE id = ?", (ids[-2],))
        counts = {"jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                  "archived": conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0],
                  "scores": conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]}
    finally:
        conn.close()
    counts["total"] = counts["jobs"] + counts["archived"]
    counts["config"] = str(config_path)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create a demo database of fictional jobs.")
    ap.add_argument("--db", required=True, type=Path, help="database file to create (not under data/)")
    ap.add_argument("--jobs", type=int, default=40, help="number of fictional jobs (default 40)")
    ap.add_argument("--force", action="store_true", help="replace an existing demo database")
    args = ap.parse_args(argv)
    if args.jobs < 12:
        ap.error("--jobs must be at least 12")
    counts = seed(args.db, args.jobs, args.force)
    print(f"demo database: {counts['total']} jobs ({counts['jobs']} open, {counts['archived']} archived), {counts['scores']} scores; "
          f"config {counts['config']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
