import csv
import json
import math

import pytest

from app import calibration, db
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from tests.test_cli import clean_logger, config_file, run      # noqa: F401  (fixtures and the runner)
from tests.test_report import job


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    return load_config()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def add_scored(conn, n, overall_of=lambda i: i * 3, **kw):
    """n jobs with ids 1..n, one score each; returns the ids."""
    db.start_run(conn, "r1")
    ids = []
    for i in range(1, n + 1):
        jid = ingest_job(conn, job(i, **kw), "r1").job_id
        ids.append(jid)
        overall = overall_of(i)
        conn.execute("INSERT INTO scores (job_id, run_id, prompt_version, model, overall, subscores, created_at) "
                     "VALUES (?, 'r1', 'v1', 'm', ?, ?, '2026-11-01T00:00:00Z')",
                     (jid, overall, json.dumps({"skills": overall, "experience": 50, "domain": 40, "seniority": 30, "location": 100})))
    conn.commit()
    return ids


def read_sheet(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write_labels(path, labels, delimiter=","):
    """Fill your_label of a sheet by job id (labels: {job_id: text}); returns the path."""
    rows = read_sheet(path)
    for r in rows:
        r["your_label"] = labels.get(int(r["job_id"]), "")
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=calibration.SHEET_COLUMNS, delimiter=delimiter)
        w.writeheader()
        w.writerows(rows)
    return path


# ---------------------------------------------------------------- sheet

def test_sheet_picks_lowest_middle_and_highest(conn, tmp_path):
    ids = add_scored(conn, 60, overall_of=lambda i: i + 20)      # overall 21..80, job id = i
    out = tmp_path / "sheet.csv"
    info = calibration.build_sheet(conn, out, 10)
    rows = read_sheet(out)
    got = [int(r["job_id"]) for r in rows]
    assert info["written"] == 10 and info["scored"] == 60
    assert got == sorted(got)                       # by id, so the order carries no hint
    assert ids[0] in got and ids[-1] in got         # the extremes are included
    overall = [i + 20 for i in got]
    assert max(overall) - min(overall) >= 58        # spread across the range
    assert any(45 <= o <= 55 for o in overall)      # some middle ones as well


def test_sheet_includes_selected_and_not_selected_jobs(conn, tmp_path):
    ids = add_scored(conn, 40, overall_of=lambda i: i + 40)
    conn.execute("UPDATE jobs SET status = 'not_selected' WHERE id <= ?", (ids[19],))
    conn.execute("UPDATE jobs SET status = 'scored' WHERE id > ?", (ids[19],))
    conn.commit()
    out = tmp_path / "sheet.csv"
    calibration.build_sheet(conn, out, 10)
    status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM jobs")}
    kinds = {status[int(r["job_id"])] for r in read_sheet(out)}
    assert kinds == {"scored", "not_selected"}


def test_sheet_is_deterministic(conn, tmp_path):
    add_scored(conn, 50, overall_of=lambda i: (i * 7) % 90 + 5)
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    calibration.build_sheet(conn, a, 12)
    calibration.build_sheet(conn, b, 12)
    assert a.read_bytes() == b.read_bytes()


def test_sheet_has_no_scores_or_bands(conn, tmp_path):
    add_scored(conn, 5, overall_of=lambda i: 71 + i)           # 72..76, easy to recognise
    out = tmp_path / "sheet.csv"
    calibration.build_sheet(conn, out, 5)
    raw = out.read_text(encoding="utf-8-sig")
    header = raw.splitlines()[0].split(",")
    assert header == calibration.SHEET_COLUMNS
    assert not {"overall", "score", "band", "skills"} & set(header)
    for needle in ("72", "73", "74", "75", "76", "subscores", "high", "medium", "low"):
        assert needle not in raw.lower()
    for r in read_sheet(out):
        assert r["your_label"] == "" and r["your_note"] == ""


def test_sheet_columns_and_description_flag(conn, tmp_path):
    db.start_run(conn, "r1")
    with_text = ingest_job(conn, job(1, description="x" * 300, locations=["Zürich"], company="Müller AG"), "r1").job_id
    without = ingest_job(conn, job(2, description=""), "r1").job_id
    for jid in (with_text, without):
        conn.execute("INSERT INTO scores (job_id, run_id, prompt_version, model, overall, subscores, created_at) "
                     "VALUES (?, 'r1', 'v1', 'm', 50, '{}', 'x')", (jid,))
    conn.commit()
    out = tmp_path / "sheet.csv"
    calibration.build_sheet(conn, out, 30)
    rows = {int(r["job_id"]): r for r in read_sheet(out)}
    assert rows[with_text]["has_description"] == "yes" and rows[without]["has_description"] == "no"
    assert rows[with_text]["company"] == "Müller AG" and rows[with_text]["url"].startswith("https://")


def test_sheet_uses_the_latest_score_only(conn, tmp_path):
    (jid,) = add_scored(conn, 1, overall_of=lambda i: 10)
    conn.execute("INSERT INTO scores (job_id, run_id, prompt_version, model, overall, subscores, created_at) "
                 "VALUES (?, 'r1', 'v2', 'm', 90, '{}', 'x')", (jid,))
    conn.commit()
    assert [r["overall"] for r in calibration.latest_scores(conn)] == [90]


def test_sheet_starts_with_a_bom(conn, tmp_path):
    add_scored(conn, 3, overall_of=lambda i: i * 10)
    out = tmp_path / "sheet.csv"
    calibration.build_sheet(conn, out, 3)
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")


def test_sheet_refuses_to_overwrite(conn, tmp_path):
    add_scored(conn, 3, overall_of=lambda i: i * 10)
    out = tmp_path / "sheet.csv"
    out.write_text("my labels", encoding="utf-8")
    with pytest.raises(calibration.CalibrationError, match="already exists"):
        calibration.build_sheet(conn, out, 3)
    assert out.read_text(encoding="utf-8") == "my labels"


def test_sheet_without_scores_is_an_error(conn, tmp_path):
    with pytest.raises(calibration.CalibrationError, match="no scored jobs"):
        calibration.build_sheet(conn, tmp_path / "s.csv", 5)


def test_sheet_with_fewer_jobs_than_count_takes_all(conn, tmp_path):
    add_scored(conn, 4, overall_of=lambda i: i * 10)
    assert calibration.build_sheet(conn, tmp_path / "s.csv", 30)["written"] == 4


def test_default_path_is_in_the_exchange_folder(cfg, tmp_path):
    cfg["paths"]["exchange"] = str(tmp_path / "exchange")
    from datetime import date
    p = calibration.default_sheet_path(cfg, date(2026, 11, 2))
    assert p == tmp_path / "exchange" / "calibration" / "labels_2026-11-02.csv"


# ---------------------------------------------------------------- labels and statistics

def test_parse_label_numbers_and_words(cfg):
    assert calibration.parse_label("75", cfg) == 75
    assert calibration.parse_label(" 62,5 ", cfg) == 62.5
    assert calibration.parse_label("LOW", cfg) == 25
    assert calibration.parse_label("Medium", cfg) == 60
    assert calibration.parse_label("high", cfg) == 85
    for bad in ("101", "-1", "great", "nan"):
        with pytest.raises(ValueError):
            calibration.parse_label(bad, cfg)


def test_average_ranks_and_spearman_with_ties():
    assert calibration.average_ranks([10, 20, 20, 40]) == [1, 2.5, 2.5, 4]
    rho = calibration.spearman([1, 2, 2, 4], [1, 3, 2, 4])          # worked by hand: 4.5 / sqrt(22.5)
    assert rho == pytest.approx(4.5 / math.sqrt(22.5))
    assert calibration.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert calibration.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert calibration.spearman([1, 1, 1], [1, 2, 3]) is None        # no variation
    assert calibration.spearman([1, 2], [1, 2]) is None              # too few


# ---------------------------------------------------------------- report

def make_sheet(conn, tmp_path, overall_of, n=10):
    ids = add_scored(conn, n, overall_of=overall_of)
    out = tmp_path / "sheet.csv"
    calibration.build_sheet(conn, out, n)
    return ids, out


def test_report_counts_numbers_words_skips_and_invalid(conn, cfg, tmp_path):
    ids, out = make_sheet(conn, tmp_path, lambda i: 20 + i * 6, n=8)       # 26, 32, ..., 68
    write_labels(out, {ids[0]: "10", ids[1]: "low", ids[2]: "Medium", ids[3]: "banana", ids[4]: "140", ids[5]: "60"})
    r = calibration.build_report(conn, cfg, out)
    assert r["labelled"] == 4 and r["skipped"] == 2 and len(r["invalid"]) == 2
    reasons = " ".join(b["reason"] for b in r["invalid"])
    assert "not a number" in reasons and "between 0 and 100" in reasons
    assert all(b["line"] >= 2 and b["title"] for b in r["invalid"])


def test_report_unknown_and_duplicate_job_ids_are_invalid(conn, cfg, tmp_path):
    ids, out = make_sheet(conn, tmp_path, lambda i: 30 + i, n=3)
    with open(out, "a", encoding="utf-8", newline="") as fh:
        fh.write(f"{ids[0]},A,dup,,,yes,50,\n999,A,ghost,,,yes,50,\nx,A,text id,,,yes,50,\n")
    rows = read_sheet(out)
    assert len(rows) == 6                            # label the first original row so the duplicate comes second
    rows[0]["your_label"] = "50"
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=calibration.SHEET_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    r = calibration.build_report(conn, cfg, out)
    reasons = sorted(b["reason"] for b in r["invalid"])
    assert r["labelled"] == 1 and len(reasons) == 3
    assert any("twice" in x for x in reasons) and any("no score" in x for x in reasons) and any("whole number" in x for x in reasons)


def test_an_invalid_first_row_does_not_make_the_corrected_second_row_a_duplicate(conn, cfg, tmp_path):
    ids, out = make_sheet(conn, tmp_path, lambda i: 30 + i, n=3)
    rows = read_sheet(out)
    first = dict(rows[0], your_label="hgh")
    second = dict(rows[0], your_label="high")
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=calibration.SHEET_COLUMNS)
        w.writeheader()
        w.writerows([first, second])
    r = calibration.build_report(conn, cfg, out)
    assert r["labelled"] == 1 and len(r["invalid"]) == 1
    assert not any("twice" in b["reason"] for b in r["invalid"])


def test_report_band_agreement_and_spearman(conn, cfg, tmp_path):
    ids, out = make_sheet(conn, tmp_path, lambda i: [20, 40, 55, 60, 75, 90][i - 1], n=6)
    # model bands: low low medium medium high high
    write_labels(out, {ids[0]: "low", ids[1]: "medium", ids[2]: "medium", ids[3]: "high", ids[4]: "high", ids[5]: "95"})
    r = calibration.build_report(conn, cfg, out)
    assert r["band_agreement_count"] == 4 and r["band_agreement_percent"] == pytest.approx(66.7, abs=0.1)
    assert r["spearman"] == pytest.approx(calibration.spearman([25, 60, 60, 85, 85, 95], [20, 40, 55, 60, 75, 90]), abs=1e-3)
    assert r["spearman"] > 0.9


def test_report_largest_disagreements_are_sorted_and_limited_to_five(conn, cfg, tmp_path):
    ids, out = make_sheet(conn, tmp_path, lambda i: 50, n=8)
    labels = {ids[i]: str(50 + (i + 1) * 5) for i in range(8)}        # differences 5, 10, ..., 40
    write_labels(out, labels)
    r = calibration.build_report(conn, cfg, out)
    top = r["largest_disagreements"]
    assert [d["difference"] for d in top] == [40, 35, 30, 25, 20]
    assert top[0]["job_id"] == ids[7] and top[0]["subscores"]["skills"] == 50 and top[0]["overall"] == 50


def test_report_verdict_uses_the_config_threshold(conn, cfg, tmp_path):
    ids, out = make_sheet(conn, tmp_path, lambda i: [20, 40, 55, 60][i - 1], n=4)
    write_labels(out, {ids[0]: "low", ids[1]: "low", ids[2]: "medium", ids[3]: "high"})      # 3 of 4 agree = 75%
    assert calibration.build_report(conn, cfg, out)["trusted"] is True
    cfg["scoring"]["calibration_min_agreement"] = 80
    r = calibration.build_report(conn, cfg, out)
    assert r["trusted"] is False and "Review the rubric" in r["verdict"] and "80%" in r["verdict"]
    cfg["scoring"]["calibration_min_agreement"] = 75
    assert calibration.build_report(conn, cfg, out)["trusted"] is True           # exactly at the limit passes


def test_report_with_no_labels(conn, cfg, tmp_path):
    ids, out = make_sheet(conn, tmp_path, lambda i: 30 + i, n=3)
    r = calibration.build_report(conn, cfg, out)
    assert r["labelled"] == 0 and r["skipped"] == 3 and r["trusted"] is None and r["spearman"] is None
    assert "No usable labels" in calibration.render_text(r, cfg)


def test_report_reads_semicolon_files_from_german_excel(conn, cfg, tmp_path):
    ids, out = make_sheet(conn, tmp_path, lambda i: 30 + i, n=3)
    write_labels(out, {ids[0]: "high"}, delimiter=";")
    assert calibration.build_report(conn, cfg, out)["labelled"] == 1


def test_report_rejects_a_file_without_the_label_column(conn, cfg, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(calibration.CalibrationError, match="your_label"):
        calibration.build_report(conn, cfg, bad)
    with pytest.raises(calibration.CalibrationError, match="cannot read"):
        calibration.build_report(conn, cfg, tmp_path / "missing.csv")


def test_report_text_is_plain_english_without_fact_ids(conn, cfg, tmp_path):
    ids, out = make_sheet(conn, tmp_path, lambda i: 40 + i * 5, n=5)
    write_labels(out, {ids[0]: "90", ids[1]: "low", ids[2]: "55"})
    text = calibration.render_text(calibration.build_report(conn, cfg, out), cfg)
    for part in ("3 labelled", "2 skipped", "your label", "Spearman", "Band agreement", "model sub-scores", "Verdict"):
        assert part in text
    assert "[exp-" not in text


def test_threshold_is_validated(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("scoring:\n  calibration_min_agreement: 150\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="calibration_min_agreement"):
        load_config(p)
    assert load_config()["scoring"]["calibration_min_agreement"] == 70


# ---------------------------------------------------------------- CLI

def fill_db(tmp_path, n=6):
    c = db.connect(tmp_path / "jobs.db")
    add_scored(c, n, overall_of=lambda i: 20 + i * 10)
    c.close()


def test_cli_sheet_and_report(config_file, tmp_path, capsys):
    fill_db(tmp_path)
    out = tmp_path / "labels.csv"
    assert run(config_file, "calibration-sheet", "--count", "4", "--out", str(out)) == 0
    assert "4 of 6" in capsys.readouterr().out
    assert len(read_sheet(out)) == 4
    assert run(config_file, "calibration-sheet", "--out", str(out)) == 1             # no overwrite
    assert "already exists" in capsys.readouterr().err
    first = int(read_sheet(out)[0]["job_id"])
    write_labels(out, {first: "high"})
    assert run(config_file, "calibration-report", str(out)) == 0
    assert "Verdict" in capsys.readouterr().out
    assert run(config_file, "calibration-report", str(out), "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["labelled"] == 1 and "verdict" in data


def test_cli_default_sheet_path_is_under_exchange(config_file, tmp_path, capsys):
    fill_db(tmp_path)
    assert run(config_file, "calibration-sheet") == 0
    made = list((tmp_path / "exchange" / "calibration").glob("labels_*.csv"))
    assert len(made) == 1


def test_cli_commands_do_not_write_to_the_database(config_file, tmp_path):
    fill_db(tmp_path)
    c = db.connect(tmp_path / "jobs.db")
    before = c.execute("SELECT (SELECT COUNT(*) FROM jobs), (SELECT COUNT(*) FROM scores), (SELECT COUNT(*) FROM status_history)").fetchone()[:]
    c.close()
    out = tmp_path / "l.csv"
    run(config_file, "calibration-sheet", "--out", str(out))
    run(config_file, "calibration-report", str(out))
    c = db.connect(tmp_path / "jobs.db")
    after = c.execute("SELECT (SELECT COUNT(*) FROM jobs), (SELECT COUNT(*) FROM scores), (SELECT COUNT(*) FROM status_history)").fetchone()[:]
    c.close()
    assert tuple(before) == tuple(after)


def test_cli_sheet_with_no_scores_is_exit_1(config_file, tmp_path, capsys):
    assert run(config_file, "calibration-sheet", "--out", str(tmp_path / "x.csv")) == 1
    assert "no scored jobs" in capsys.readouterr().err
