"""Score calibration (goal.md 5.4): you label jobs blind, then the labels are compared with the model's scores.

Two steps, both read-only on the database:
  * `build_sheet`   picks jobs spread over the whole score range and writes a CSV without any score in it;
  * `build_report`  reads the filled CSV and reports rank correlation, band agreement and the biggest disagreements.

Bands come from the existing config (`scoring.band_low_below`, `scoring.band_high_from`): below the first is low, from
the second is high, between is medium. A label of low/medium/high is turned into a number for the rank correlation
and for the size of a disagreement by taking the middle of its band (no new cut-offs).
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date
from pathlib import Path

from .batch import exchange_dir
from .report import band

SHEET_COLUMNS = ["job_id", "company", "title", "location", "url", "has_description", "your_label", "your_note"]
DEFAULT_COUNT = 30
WORD_LABELS = ("low", "medium", "high")
SMALL_SAMPLE = 20                    # fewer labels than this get a warning (goal.md 5.4 asks for about 30)
DISAGREEMENTS_SHOWN = 5


class CalibrationError(Exception):
    """A problem the user can fix (existing file, no scored jobs, unreadable sheet)."""


def default_sheet_path(cfg: dict, today: date | None = None) -> Path:
    return exchange_dir(cfg) / "calibration" / f"labels_{(today or date.today()).isoformat()}.csv"


def latest_scores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per scored job: the job fields and its newest score, ordered by job id."""
    return conn.execute(
        """SELECT j.id, j.title, j.company, j.location_text, j.description_missing, j.canonical_url,
                  s.overall, s.subscores,
                  (SELECT url FROM job_sources u WHERE u.job_id = j.id AND u.url IS NOT NULL ORDER BY u.id LIMIT 1) AS url
           FROM jobs j JOIN scores s ON s.id = (SELECT MAX(id) FROM scores WHERE job_id = j.id)
           ORDER BY j.id""").fetchall()


def select_jobs(rows: list[sqlite3.Row], count: int) -> list[sqlite3.Row]:
    """Pick `count` jobs spread evenly over the score ranking (lowest and highest included), returned by job id.

    The ranking is only used to choose; the order of the result is the job id, so the sheet does not hint at scores.
    Ties in the score are broken by job id, so the same data always gives the same sheet.
    """
    if count < 1:
        raise CalibrationError("--count must be at least 1")
    if len(rows) <= count:
        return sorted(rows, key=lambda r: r["id"])
    ranked = sorted(rows, key=lambda r: (r["overall"], r["id"]))
    if count == 1:
        picks = [len(ranked) // 2]
    else:
        picks = sorted({round(i * (len(ranked) - 1) / (count - 1)) for i in range(count)})
    return sorted((ranked[i] for i in picks), key=lambda r: r["id"])


def build_sheet(conn: sqlite3.Connection, path: Path, count: int = DEFAULT_COUNT) -> dict:
    """Write the blind labelling sheet. Refuses to overwrite. Returns {"path", "written", "scored"}."""
    path = Path(path)
    exists = f"{path} already exists; I do not overwrite your labels. Choose another name with --out."
    if path.exists():
        raise CalibrationError(exists)
    rows = latest_scores(conn)
    if not rows:
        raise CalibrationError("there are no scored jobs yet; score a batch first")
    chosen = select_jobs(rows, count)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # BOM so that Excel on Windows reads umlauts correctly; "x" so that a file made meanwhile is never replaced
        with open(path, "x", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(SHEET_COLUMNS)
            for r in chosen:
                writer.writerow([r["id"], r["company"], r["title"], r["location_text"] or "", r["url"] or r["canonical_url"] or "",
                                 "no" if r["description_missing"] else "yes", "", ""])
    except FileExistsError:
        raise CalibrationError(exists) from None
    return {"path": str(path), "written": len(chosen), "scored": len(rows)}


# ---------------------------------------------------------------- report

def word_values(cfg: dict) -> dict[str, float]:
    """Number used for each word label: the middle of its band."""
    low, high = cfg["scoring"]["band_low_below"], cfg["scoring"]["band_high_from"]
    return {"low": low / 2, "medium": (low + high) / 2, "high": (high + 100) / 2}


def parse_label(raw: str, cfg: dict) -> float:
    """A label as a number 0-100. Accepts a number or low/medium/high in any case; raises ValueError with the reason."""
    text = raw.strip().lower()
    if text in WORD_LABELS:
        return word_values(cfg)[text]
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        raise ValueError("not a number from 0 to 100 and not low, medium or high") from None
    if value != value or not 0 <= value <= 100:
        raise ValueError("a number must be between 0 and 100")
    return value


def average_ranks(values: list[float]) -> list[float]:
    """Ranks starting at 1; equal values share the average of the ranks they cover."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation (Pearson on average ranks). None when it is undefined (under 3 pairs or no variation)."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = average_ranks(xs), average_ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sxx * syy) ** 0.5


def _read_sheet(path: Path) -> list[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CalibrationError(f"cannot read {path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError:
        raise CalibrationError(f"{path} is not UTF-8; save it from Excel as 'CSV UTF-8'") from None
    head = text.split("\n", 1)[0]
    delimiter = max(",;\t", key=head.count)               # Excel with German settings saves with semicolons
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    names = [(n or "").strip() for n in (reader.fieldnames or [])]
    missing = [c for c in ("job_id", "your_label") if c not in names]
    if missing:
        raise CalibrationError(f"{path} has no column {' or '.join(missing)}; was it made by calibration-sheet?")
    reader.fieldnames = names
    return [row for row in reader if any(v.strip() for v in row.values() if isinstance(v, str))]


def build_report(conn: sqlite3.Connection, cfg: dict, path: Path) -> dict:
    """Compare the filled sheet with the stored scores. Returns a plain dict (see `render_text`)."""
    sheet = _read_sheet(path)
    scores = {r["id"]: r for r in latest_scores(conn)}
    min_agreement = cfg["scoring"]["calibration_min_agreement"]
    skipped, invalid, pairs, seen = 0, [], [], set()
    for line, row in enumerate(sheet, start=2):                 # line 1 is the header
        raw_label = (row.get("your_label") or "").strip()
        raw_id = (row.get("job_id") or "").strip()
        title = (row.get("title") or "").strip()
        if not raw_label:
            skipped += 1
            continue

        def bad(reason: str) -> None:
            invalid.append({"line": line, "job_id": raw_id, "title": title, "label": raw_label, "reason": reason})

        try:
            job_id = int(raw_id)
        except ValueError:
            bad("the job_id is not a whole number")
            continue
        if job_id in seen:
            bad("this job_id appears twice; only the first row counts")
            continue
        try:
            value = parse_label(raw_label, cfg)
        except ValueError as exc:
            bad(str(exc))
            continue
        score = scores.get(job_id)
        if score is None:
            bad("no score for this job in the database (wrong database, or the job was archived)")
            continue
        seen.add(job_id)                                        # only a row that counts blocks a later one for the same job
        pairs.append({"job_id": job_id, "title": score["title"], "company": score["company"], "label": raw_label,
                      "label_value": value, "label_band": band(value, cfg), "overall": score["overall"],
                      "model_band": band(score["overall"], cfg), "subscores": json.loads(score["subscores"])})

    labelled = len(pairs)
    agree = sum(1 for p in pairs if p["label_band"] == p["model_band"])
    agreement = round(100 * agree / labelled, 1) if labelled else None
    rho = spearman([p["label_value"] for p in pairs], [p["overall"] for p in pairs])
    worst = sorted(pairs, key=lambda p: (-abs(p["label_value"] - p["overall"]), p["job_id"]))[:DISAGREEMENTS_SHOWN]
    keep = ("job_id", "title", "company", "label", "label_band", "overall", "model_band", "subscores")
    result = {"labelled": labelled, "skipped": skipped, "invalid": invalid,
              "spearman": None if rho is None else round(rho, 3),
              "band_agreement_percent": agreement, "band_agreement_count": agree, "min_agreement_percent": min_agreement,
              "largest_disagreements": [{k: p[k] for k in keep} | {"difference": round(abs(p["label_value"] - p["overall"]), 1)}
                                        for p in worst]}
    if labelled == 0:
        result["verdict"] = "No usable labels yet, so there is nothing to compare. Fill in the your_label column and run this again."
        result["trusted"] = None
    elif agreement >= min_agreement:
        result["verdict"] = (f"Your labels and the model's bands agree on {agreement:g}% of the jobs (at least {min_agreement:g}% is needed). "
                             "The ranking can be trusted for now.")
        result["trusted"] = True
    else:
        result["verdict"] = (f"Your labels and the model's bands agree on only {agreement:g}% of the jobs (at least {min_agreement:g}% is needed). "
                             "Review the rubric before you trust the ranking.")
        result["trusted"] = False
    if 0 < labelled < SMALL_SAMPLE:
        result["verdict"] += f" Note: you labelled only {labelled} job(s); about 30 give a more reliable answer."
    return result


def render_text(r: dict, cfg: dict) -> str:
    """The report as plain text for you (second person, no internal ids)."""
    low, high = cfg["scoring"]["band_low_below"], cfg["scoring"]["band_high_from"]
    out = [f"Calibration report: {r['labelled']} labelled, {r['skipped']} skipped (no label), {len(r['invalid'])} invalid",
           f"Bands: low is below {low:g}, medium is {low:g} to {high:g}, high is {high:g} and above. "
           "A label of low, medium or high counts as the middle of its band."]
    if r["invalid"]:
        out += ["", "Rows I could not use:"]
        for b in r["invalid"]:
            out.append(f"  line {b['line']}: job {b['job_id'] or '?'} {b['title'][:50]!r}, label {b['label']!r}: {b['reason']}")
    out.append("")
    if r["spearman"] is None:
        out.append("Rank correlation (Spearman): not defined (it needs at least 3 labelled jobs and some variation in your labels and in the scores)")
    else:
        out.append(f"Rank correlation (Spearman): {r['spearman']:+.2f}  (+1 means your order and the model's order are the same, 0 means unrelated)")
    if r["band_agreement_percent"] is None:
        out.append("Band agreement: not defined (no labelled jobs)")
    else:
        out.append(f"Band agreement: {r['band_agreement_percent']:g}%  ({r['band_agreement_count']} of {r['labelled']} jobs in the same band)")
    if r["largest_disagreements"]:
        out += ["", f"The {len(r['largest_disagreements'])} largest disagreements:"]
        for d in r["largest_disagreements"]:
            sub = ", ".join(f"{k} {v}" for k, v in d["subscores"].items())
            out.append(f"  {d['title'][:70]} ({d['company']})")
            out.append(f"    your label: {d['label']} ({d['label_band']}); model overall: {d['overall']} ({d['model_band']}); difference {d['difference']:g}")
            out.append(f"    model sub-scores: {sub}")
    out += ["", f"Verdict: {r['verdict']}"]
    return "\n".join(out)
