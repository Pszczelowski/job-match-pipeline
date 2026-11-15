"""Part-time rule of prompt 1.12.0 (goal.md 5.4, v0.60.0): 5 points off `overall` after any cap.

No network, no real config, no real database."""
import json

import pytest

from app import db
from app.batch import build_batch, exchange_dir
from app.config import load_config
from app.dedup import ingest_job
from app.filters.runner import filter_new_jobs
from app.models import NormalizedJob
from app.results import import_results
from tests.test_language_cap import GERMAN, NOW, SUBS     # formula: 79

ENGLISH = "We are looking for a Business Analyst. You will work with the team and the business."


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"]["exchange"] = str(tmp_path / "exchange")
    return c


def _import(cfg, tmp_path, overall, description, red_flags):
    conn = db.connect(tmp_path / "t.db")
    jid = ingest_job(conn, NormalizedJob(source="s", external_id="1", title="Senior IT-Business Analyst", company="A",
                                         locations=["Zürich"], description=description)).job_id
    filter_new_jobs(conn, cfg)
    build_batch(conn, cfg, "p", "v1", run_id="r1", now=NOW)
    entry = {"job_id": jid, "overall": overall, "subscores": SUBS, "matches": [], "gaps": [], "red_flags": red_flags,
             "confidence": "high", "summary": "s", "cv_variant": "resume"}
    path = exchange_dir(cfg) / "results" / "r.json"
    path.write_text(json.dumps({"schema_version": "1.0", "run_id": "r1", "model": "m", "prompt_version": "v1",
                                "results": [entry], "top": [jid]}), encoding="utf-8")
    result = import_results(conn, cfg, path, NOW)
    return conn, jid, result


def _off(result):
    return [w for w in result.warnings if "overall differs" in w]


def test_import_part_time_overall_five_lower_gives_no_warning(cfg, tmp_path):
    conn, jid, r = _import(cfg, tmp_path, 74, ENGLISH, ["Part-time, 60-80% workload"])
    assert r.scored == [jid] and _off(r) == []
    conn.close()


def test_import_part_time_without_the_deduction_warns(cfg, tmp_path):
    conn, jid, r = _import(cfg, tmp_path, 79, ENGLISH, ["part-time role"])
    assert r.scored == [jid] and len(_off(r)) == 1 and f"{jid}: 79 vs 74" in _off(r)[0]
    conn.close()


def test_import_part_time_and_german_expected_is_cap_minus_five(cfg, tmp_path):
    conn, jid, r = _import(cfg, tmp_path, 44, GERMAN, ["Part-time (Teilzeit 80%)"])
    assert conn.execute("SELECT overall FROM scores WHERE job_id = ?", (jid,)).fetchone()["overall"] == 44
    assert _off(r) == []
    conn.close()
