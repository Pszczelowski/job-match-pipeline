"""The read-only web app, chunk 1 (goal.md 5.7, Phase 4; T-WEB-* in docs/TEST_CASES.md).

Every test builds its own temporary database; none uses the real config, the real database or the network.
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import cli, db, report, web
from app.config import ConfigError, load_config
from tests.test_cli import clean_logger, config_file          # noqa: F401  (fixtures)
from tests.test_language_cap import GERMAN
from tests.test_report import job, pipeline

XSS = '<script>alert("x")</script>'
HOST = "http://127.0.0.1"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    c = load_config()
    c["paths"]["exchange"] = str(tmp_path / "exchange")
    return c


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "w.db")
    yield c
    c.close()


def client(cfg, tmp_path, **kw):
    return TestClient(web.create_app(cfg, tmp_path / "w.db", **kw), base_url=HOST)


@pytest.fixture
def site(cfg, conn, tmp_path):
    """A database with three scored jobs (90, 55, 20) and a client on it; the writer connection stays open, like a running pipeline."""
    ids, _ = pipeline(conn, cfg, [job(1), job(2), job(3)], [55, 90, 20])
    return client(cfg, tmp_path), ids


def test_a_unicode_digit_is_not_a_job_id_or_a_page_number(cfg, conn, tmp_path):
    pipeline(conn, cfg, [job(1)], [55])
    c = TestClient(web.create_app(cfg, tmp_path / "w.db"), base_url=HOST, raise_server_exceptions=False)
    two = "%C2%B2"                                                                  # superscript two: isdigit() is true, int() fails
    for path in (f"/jobs/{two}", f"/jobs/{two}/reject", f"/jobs/{two}/documents/cv/pdf"):
        assert c.get(path).status_code == 404, path
    assert c.post(f"/jobs/{two}/shortlist", data={"csrf": "x" * 64}).status_code in (403, 404)
    r = c.get(f"/jobs?page={two}")
    assert r.status_code == 200 and "Analyst 1" in r.text


# --- pages ------------------------------------------------------------------------------------------

def test_top10_page_lists_the_jobs_in_rank_order_with_band_and_batch(site):
    c, ids = site
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    body = r.text
    assert "Today's Top 10" in body and "batch run1" in body and "2 Nov 2026" in body
    assert body.index("Analyst 2") < body.index("Analyst 1") < body.index("Analyst 3")
    assert "#1 &middot; <span class=\"score\">90</span> &middot; <span class=\"band\">HIGH</span>" in body
    assert 'class="card band-medium"' in body and 'class="card band-low"' in body
    assert f'href="/jobs/{ids[1]}"' in body and "Fits job" in body and "Acme" in body and "Zürich" in body
    assert "Run summary" in body


def test_top10_shows_the_markers_and_notes_of_the_report(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(1, description=""), job(2, description=GERMAN)], [70, 70])
    with conn:
        conn.execute("UPDATE jobs SET repost = 1 WHERE id = ?", (ids[1],))        # job 2: job 1 is queued since v0.72.0 and shows no markers
        conn.execute("UPDATE jobs SET closed_at = '2026-11-01T00:00:00Z' WHERE id = ?", (ids[1],))
    body = client(cfg, tmp_path).get("/").text
    assert "repost" in body and "no longer listed on the employer&#39;s site" in body
    assert "Posting in German: score capped at 49 (low)" in body
    assert "documents missing" not in body                       # the German job is capped at 49, below tailoring.min_score
    # v0.72.0: job 1 (title-only, 70) is queued: it leaves the top 10 and shows in the waiting block, without its score or markers
    assert "Waiting for a description (1)" in body and "Analyst 1" in body
    assert "No description: scored from the title only" not in body and "no documents (no description)" not in body


def test_top10_hides_acted_jobs_and_moves_the_next_ranked_up(cfg, conn, tmp_path):
    cfg["scoring"]["top_n"] = 2
    ids, _ = pipeline(conn, cfg, [job(1), job(2), job(3)], [90, 80, 70])      # selected: jobs 1 and 2; job 3 is the next-ranked
    with conn:
        conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (ids[0],))
    body = client(cfg, tmp_path).get("/").text
    assert "Analyst 1" not in body and body.index("Analyst 2") < body.index("Analyst 3")
    assert "#1 &middot; <span class=\"score\">80</span>" in body and "#2 &middot; <span class=\"score\">70</span>" in body


def test_top10_archived_selected_jobs_are_replaced_and_ranks_have_no_gaps(cfg, conn, tmp_path):
    cfg["scoring"]["top_n"] = 4
    ids, _ = pipeline(conn, cfg, [job(i) for i in range(1, 9)], [90, 85, 80, 75, 70, 65, 60, 55])
    with conn:
        db.archive_job(conn, ids[0], "user", "not_interested")           # selected ranks 1 and 2: archived, the job row is deleted
        db.archive_job(conn, ids[1], "user", "not_interested")
        conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (ids[2],))
    body = client(cfg, tmp_path).get("/").text
    for n in (1, 2, 3):
        assert f"Analyst {n}<" not in body and f"Analyst {n} " not in body
    shown = [n for n in range(4, 9) if f"Analyst {n}" in body]
    assert shown == [4, 5, 6, 7]
    for place, score in ((1, 75), (2, 70), (3, 65), (4, 60)):
        assert f"#{place} &middot; <span class=\"score\">{score}</span>" in body
    assert "#5 &middot;" not in body


def test_report_collect_hide_acted_numbers_ranks_without_gaps(cfg, conn):
    cfg["scoring"]["top_n"] = 3
    ids, _ = pipeline(conn, cfg, [job(i) for i in range(1, 7)], [90, 85, 80, 75, 70, 65])
    with conn:
        db.archive_job(conn, ids[0], "user", "not_interested")
        db.archive_job(conn, ids[1], "user", "not_interested")
    rep = report.collect(conn, cfg, "run1", hide_acted=True)
    assert [(j.rank, j.title) for j in rep.jobs] == [(1, "Analyst 3"), (2, "Analyst 4"), (3, "Analyst 5")]


def test_report_collect_default_keeps_acted_jobs(cfg, conn):
    cfg["scoring"]["top_n"] = 2
    ids, _ = pipeline(conn, cfg, [job(1), job(2), job(3)], [90, 80, 70])
    with conn:
        conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (ids[0],))
    rep = report.collect(conn, cfg, "run1")
    assert [(j.rank, j.title) for j in rep.jobs] == [(1, "Analyst 1"), (2, "Analyst 2")]
    web_rep = report.collect(conn, cfg, "run1", hide_acted=True)
    assert [(j.rank, j.title) for j in web_rep.jobs] == [(1, "Analyst 2"), (2, "Analyst 3")]


def test_top10_empty_database_is_a_friendly_page(cfg, conn, tmp_path):
    r = client(cfg, tmp_path).get("/")
    assert r.status_code == 200 and "No results have been imported yet" in r.text
    assert client(cfg, tmp_path).get("/jobs").status_code == 200


def test_top10_batch_without_matches(cfg, conn, tmp_path):
    with conn:                                  # a batch whose results were imported but that selected nothing
        conn.execute("INSERT INTO batches (id, created_at, file_name, sha256, job_count, fresh_count) VALUES ('b0', '2026-11-02T06:00:00Z', 'b0.json', 'x', 0, 0)")
        conn.execute("INSERT INTO imports (batch_id, file_name, sha256, status, imported_at) VALUES ('b0', 'b0.json', 'x', 'imported', '2026-11-02T06:00:00Z')")
    r = client(cfg, tmp_path).get("/")
    assert r.status_code == 200 and "No job matches in this batch" in r.text


def test_missing_database_file_is_an_error_page_not_a_crash(cfg, tmp_path):
    c = TestClient(web.create_app(cfg, tmp_path / "nothing.db"), base_url=HOST)
    r = c.get("/")
    assert r.status_code == 503 and "does not exist yet" in r.text
    assert not (tmp_path / "nothing.db").exists()             # the web app never creates a database


def test_schema_version_mismatch_is_a_clear_error_and_nothing_is_migrated(cfg, tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE jobs (id INTEGER)")
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()
    r = TestClient(web.create_app(cfg, path), base_url=HOST).get("/")
    assert r.status_code == 503 and "schema version 3" in r.text
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
    raw.close()
    newer = tmp_path / "new.db"
    raw = sqlite3.connect(newer)
    raw.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    raw.close()
    assert TestClient(web.create_app(cfg, newer), base_url=HOST).get("/jobs").status_code == 503


def test_a_newer_database_says_the_web_app_was_started_before_an_update(cfg, tmp_path):
    newer = tmp_path / "new.db"
    raw = sqlite3.connect(newer)
    raw.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    raw.close()
    text = TestClient(web.create_app(cfg, newer), base_url=HOST).get("/jobs").text
    assert "started before an update" in text and "Ctrl+C" in text and "python -m app web" in text
    assert "to migrate it" not in text


def test_a_newer_database_in_the_container_says_to_restart_the_container(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("RECRUITMENT_CONTAINER", "1")
    newer = tmp_path / "new.db"
    raw = sqlite3.connect(newer)
    raw.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    raw.close()
    text = TestClient(web.create_app(cfg, newer), base_url=HOST).get("/jobs").text
    assert "started before an update" in text and "Restart the container" in text and "docker compose" in text
    assert "Ctrl+C" not in text


def test_job_detail_shows_score_sources_filters_and_description(site, conn):
    c, ids = site
    with conn:
        conn.execute("UPDATE jobs SET posted_on = '2026-10-13' WHERE id = ?", (ids[1],))
    r = c.get(f"/jobs/{ids[1]}")
    assert r.status_code == 200
    b = r.text
    for text in ("Analyst 2", "Acme", "Zürich", "status scored", "posted 13 Oct 2026", "Description 2", "confidence medium", "CV variant resume",
                 "SQL reporting", "Requirements work", "Kotlin", "Fits job", "Filter results", "Sources", "Documents", "No documents for this job"):
        assert text in b, text
    assert '<span class="score">90</span>' in b and ">HIGH<" in b
    assert '<th>skills</th>' in b and '<td class="n">90</td>' in b
    assert 'href="https://example.com/job/2" rel="noopener noreferrer"' in b
    assert any(f'<td class="n">{f}</td>' in b for f in ("pass", "conditional"))


def test_job_detail_of_a_not_selected_job_and_the_jobs_list(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(i) for i in range(1, 14)], list(range(40, 53)))
    low = conn.execute("SELECT id FROM jobs WHERE status = 'not_selected' ORDER BY id LIMIT 1").fetchone()["id"]
    c = client(cfg, tmp_path)
    r = c.get(f"/jobs/{low}")
    assert r.status_code == 200 and "status not_selected" in r.text and "LOW" in r.text
    lst = c.get("/jobs").text
    assert f'href="/jobs/{low}"' in lst and lst.count("<tr>") == 14          # header row + 13 jobs (decision of 11 Nov: not_selected jobs are listed again)
    scores = [int(s) for s in __import__("re").findall(r"<td class=\"n\" data-label=\"Score\">(\d+)</td>\s*<td class=\"n band\"><span class=\"chip", lst)]
    assert scores == sorted(scores, reverse=True) and scores[0] == 52
    assert '<table class="stack">' in lst and '<td class="n band">' in lst
    assert 'data-label="Company"' in lst and 'data-label="Status"' in lst


def test_jobs_list_pagination_and_odd_page_values(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "PAGE_SIZE", 5)
    pipeline(conn, cfg, [job(i) for i in range(1, 13)], list(range(40, 52)))
    c = client(cfg, tmp_path)
    assert "Page 1 of 3" in c.get("/jobs").text and "Page 3 of 3" in c.get("/jobs?page=3").text     # decision of 11 Nov: all 12 jobs are counted
    for odd in ("0", "-1", "abc", "99999", "1e9"):
        assert c.get(f"/jobs?page={odd}").status_code == 200


def test_unknown_and_archived_jobs_are_404(site, conn):
    c, ids = site
    assert c.get("/jobs/99999").status_code == 404
    assert c.get("/jobs/abc").status_code == 404
    assert c.get("/jobs/-1").status_code == 404
    assert c.get("/jobs/" + "9" * 40).status_code == 404
    assert c.get("/nothing-here").status_code == 404
    assert c.get(f"/jobs/{ids[0]}").status_code == 200
    db.archive_job(conn, ids[0], "user", "rejected_by_user")
    r = c.get(f"/jobs/{ids[0]}")
    assert r.status_code == 404 and "Not found" in r.text
    assert "Analyst 1" not in c.get("/").text                   # an archived job leaves the top 10 too


def letter(opening="Opening line", lead_in="Lead in"):
    return {"contact_name": "", "blocks": {"opening": opening, "lead_in": lead_in, "achievements": [{"label": "Win", "text": "Did a thing", "facts": ["f1"]}],
                                          "skills": "Skills line", "closing": "Closing line"}}


def cv(summary="CV summary"):
    return {"variant": "resume", "summary": summary, "experience": [{"experience_id": "e1", "bullets": [{"text": "Built it", "fact_id": "f2"}]}], "skills": ["sql"]}


def add_doc(conn, job_id, kind, content, status="draft", pdf=None, approved=None, findings="[]"):
    with conn:
        conn.execute("INSERT INTO documents (job_id, kind, content, status, findings, generated_on, created_at, pdf_path, pages, approved_pdf_path, approved_at) "
                     "VALUES (?, ?, ?, ?, ?, '2026-11-02', '2026-11-02T06:00:00Z', ?, ?, ?, ?)",
                     (job_id, kind, json.dumps(content), status, findings, pdf, 1 if pdf else None, approved,
                      "2026-11-02T07:00:00Z" if status == "approved" else None))


def test_documents_list_shows_status_findings_pdf_path_and_the_text_on_the_job_page_only(site, conn):
    c, ids = site
    findings = json.dumps([{"kind": "x", "reason": "r1"}, {"kind": "y", "reason": "r2"}])
    add_doc(conn, ids[1], "cover_letter", letter("SECRET-LETTER-TEXT-Dear-Sir"), "blocked", "job2/letter.pdf", findings=findings)
    add_doc(conn, ids[1], "cv", cv("SECRET-CV-SUMMARY"), approved="job2/cv.pdf")
    b = c.get(f"/jobs/{ids[1]}").text
    assert "cover letter" in b and "blocked" in b and "job2/letter.pdf (1 page)" in b and '<td class="n">2</td>' in b
    assert "SECRET-LETTER-TEXT-Dear-Sir" in b and "SECRET-CV-SUMMARY" in b and "Did a thing" in b and "Built it" in b
    assert f'href="/jobs/{ids[1]}/documents/cover_letter/pdf"' in b and f'href="/jobs/{ids[1]}/documents/cv/approved-pdf"' in b
    assert f'href="/jobs/{ids[1]}/documents/cv/pdf"' not in b and f'href="/jobs/{ids[1]}/documents/cover_letter/approved-pdf"' not in b
    top = c.get("/").text
    assert "SECRET-LETTER-TEXT-Dear-Sir" not in top and "SECRET-CV-SUMMARY" not in top
    assert "documents blocked (2 findings)" in top


def test_unreadable_document_content_does_not_break_the_job_page(site, conn):
    c, ids = site
    with conn:
        conn.execute("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at) VALUES (?, 'cv', 'not json', 'draft', '2026-11-02', '2026-11-02T06:00:00Z')", (ids[1],))
        conn.execute("INSERT INTO documents (job_id, kind, content, status, generated_on, created_at) VALUES (?, 'cover_letter', '{}', 'draft', '2026-11-02', '2026-11-02T06:00:00Z')", (ids[1],))
    r = c.get(f"/jobs/{ids[1]}")
    assert r.status_code == 200 and r.text.count("could not be read") == 2


def test_document_text_is_escaped_on_the_job_page(site, conn):
    c, ids = site
    add_doc(conn, ids[1], "cover_letter", letter(f"O {XSS}"))
    add_doc(conn, ids[1], "cv", cv(f"S {XSS}"))
    b = c.get(f"/jobs/{ids[1]}").text.replace('<script src="/static/confirm.js" defer></script>', "")                                  # the job page loads confirm.js (v0.99.0)
    assert "<script" not in b.lower()
    assert "O &lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in b and "S &lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in b


@pytest.fixture
def pdfs(cfg, tmp_path):
    cfg["paths"]["documents"] = str(tmp_path / "docs")
    cfg["paths"]["approved"] = str(tmp_path / "approved")
    for d in ("docs", "approved"):
        (tmp_path / d).mkdir()
    return tmp_path / "docs", tmp_path / "approved"


def test_pdf_routes_serve_the_stored_files_inline(cfg, conn, tmp_path, pdfs):
    docs, approved = pdfs
    ids, _ = pipeline(conn, cfg, [job(1)], [80])
    (docs / "j1").mkdir()
    (docs / "j1" / "Letter Zürich.pdf").write_bytes(b"%PDF-draft")
    (approved / "cv.pdf").write_bytes(b"%PDF-approved")
    add_doc(conn, ids[0], "cover_letter", letter(), pdf="j1/Letter Zürich.pdf")
    add_doc(conn, ids[0], "cv", cv(), "approved", pdf="j1/none.pdf", approved="cv.pdf")
    c = client(cfg, tmp_path)
    r = c.get(f"/jobs/{ids[0]}/documents/cover_letter/pdf")
    assert r.status_code == 200 and r.content == b"%PDF-draft" and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline;") and "Letter" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"].startswith("default-src 'self'")
    r = c.get(f"/jobs/{ids[0]}/documents/cv/approved-pdf")
    assert r.status_code == 200 and r.content == b"%PDF-approved" and r.headers["content-disposition"] == 'inline; filename="cv.pdf"'
    assert c.get(f"/jobs/{ids[0]}/documents/cv/pdf").status_code == 404                  # the row names a file that is not on disk
    assert c.get(f"/jobs/{ids[0]}/documents/cover_letter/approved-pdf").status_code == 404    # not approved: no approved copy


def test_pdf_routes_refuse_everything_else(cfg, conn, tmp_path, pdfs):
    docs, approved = pdfs
    ids, _ = pipeline(conn, cfg, [job(1), job(2)], [80, 70])
    (docs / "ok.pdf").write_bytes(b"%PDF")
    (tmp_path / "evil.pdf").write_bytes(b"%PDF-secret")
    (tmp_path / "docs-evil").mkdir()
    (tmp_path / "docs-evil" / "x.pdf").write_bytes(b"%PDF-secret")
    add_doc(conn, ids[0], "cover_letter", letter(), pdf="ok.pdf")
    add_doc(conn, ids[0], "cv", cv(), pdf="../evil.pdf", approved="../evil.pdf")
    add_doc(conn, ids[1], "cover_letter", letter(), pdf="../docs-evil/x.pdf")
    add_doc(conn, ids[1], "cv", cv(), pdf=str(tmp_path / "evil.pdf"))                  # an absolute path
    c = client(cfg, tmp_path)
    assert c.get(f"/jobs/{ids[0]}/documents/cover_letter/pdf").status_code == 200
    assert c.get(f"/jobs/{ids[0]}/documents/letter/pdf").status_code == 404            # unknown kind
    assert c.get(f"/jobs/{ids[0]}/documents/cv/pdf").status_code == 404                # ../evil.pdf
    assert c.get(f"/jobs/{ids[0]}/documents/cv/approved-pdf").status_code == 404
    assert c.get(f"/jobs/{ids[1]}/documents/cover_letter/pdf").status_code == 404      # a sibling folder with the same prefix
    assert c.get(f"/jobs/{ids[1]}/documents/cv/pdf").status_code == 404
    assert c.get("/jobs/99999/documents/cv/pdf").status_code == 404
    assert c.get("/jobs/abc/documents/cv/pdf").status_code == 404
    r = c.get(f"/jobs/{ids[0]}/documents/cv/pdf")
    assert "Not found" in r.text and b"secret" not in r.content
    db.archive_job(conn, ids[0], "user", "rejected_by_user")
    assert c.get(f"/jobs/{ids[0]}/documents/cover_letter/pdf").status_code == 404      # archived job


def test_pdf_routes_have_the_host_check(cfg, conn, tmp_path, pdfs):
    docs, _ = pdfs
    ids, _ = pipeline(conn, cfg, [job(1)], [80])
    (docs / "ok.pdf").write_bytes(b"%PDF")
    add_doc(conn, ids[0], "cv", cv(), pdf="ok.pdf", approved="ok.pdf")
    bad = TestClient(web.create_app(cfg, tmp_path / "w.db"), base_url="http://evil.example")
    for suffix in ("pdf", "approved-pdf"):
        r = bad.get(f"/jobs/{ids[0]}/documents/cv/{suffix}")
        assert r.status_code == 400 and r.text == "Bad Host header"


def test_long_description_and_line_breaks_are_rendered(site, conn):
    c, ids = site
    text = "Line one\nLine two\n\n" + ("Responsibilities include reporting. " * 6000)
    with conn:
        conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (text, ids[0]))
    r = c.get(f"/jobs/{ids[0]}")
    assert r.status_code == 200 and "Line one\nLine two\n\nResponsibilities" in r.text and len(r.text) > 200_000
    assert '<div class="description">' in r.text


# --- security ---------------------------------------------------------------------------------------

def test_stored_xss_is_escaped_everywhere(cfg, conn, tmp_path):
    ids, _ = pipeline(
        conn, cfg, [job(1, title=f"T {XSS}", company=f"C {XSS}", description=f"D {XSS}", locations=[f"L {XSS}"])], [80],
        extra_entries=())
    with conn:
        conn.execute("UPDATE scores SET details = ? WHERE job_id = ?", (json.dumps({"summary": f"S {XSS}", "matches": [f"M {XSS}"], "gaps": [f"G {XSS}"],
                                                                                    "red_flags": [f"R {XSS}"]}), ids[0]))
        conn.execute("INSERT INTO job_filter_results (job_id, filter, outcome, reason_code, comment, created_at) "
                     "VALUES (?, 'f', 'pass', 'rc', ?, '2026-11-02T00:00:00Z')", (ids[0], f"K {XSS}"))
    c = client(cfg, tmp_path)
    for url in ("/", f"/jobs/{ids[0]}", "/jobs"):
        body = c.get(url).text
        if url in web.SCRIPT_PATHS:                                                                           # the list pages load select.js (v0.97.0)
            body = body.replace('<script src="/static/select.js" defer></script>', "")
        body = body.replace('<script src="/static/confirm.js" defer></script>', "")                                                           # the job page loads confirm.js (v0.99.0)
        assert "<script" not in body.lower(), url
        assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in body, url
    detail = c.get(f"/jobs/{ids[0]}").text
    for marker in ("S ", "M ", "G ", "R ", "K ", "D ", "L "):
        assert f"{marker}&lt;script&gt;" in detail, marker


def test_only_http_links_are_rendered_as_links(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(1)], [80])
    with conn:
        conn.execute("UPDATE job_sources SET url = 'javascript:alert(1)' WHERE job_id = ?", (ids[0],))
        conn.execute("INSERT INTO job_sources (job_id, source, external_id, url, source_date, seen_at) VALUES (?, 'b', 'x', "
                     "'https://example.com/a\"onmouseover=\"x', '', '2026-11-02T00:00:00Z')", (ids[0],))
        conn.execute("INSERT INTO job_sources (job_id, source, external_id, url, source_date, seen_at) VALUES (?, 'c', 'y', "
                     "'http://example.org/ok', '', '2026-11-02T00:00:00Z')", (ids[0],))
    b = client(cfg, tmp_path).get(f"/jobs/{ids[0]}").text
    assert "href=\"javascript" not in b and "javascript:alert(1)" in b and "not a web link" in b
    assert "onmouseover" in b and 'href="https://example.com/a' not in b
    assert 'href="http://example.org/ok" rel="noopener noreferrer"' in b


def test_security_headers_on_every_kind_of_response(site):
    c, ids = site
    for url in ("/", "/jobs", f"/jobs/{ids[0]}", "/jobs/999", "/static/style.css", "/missing"):
        h = c.get(url).headers
        assert h["content-security-policy"].startswith("default-src 'self'"), url
        wanted = "script-src 'self'" if url in ("/", "/jobs", "/missing", f"/jobs/{ids[0]}") else "script-src 'none'"             # the list pages load select.js (v0.97.0), the job page confirm.js (v0.99.0)
        assert wanted in h["content-security-policy"] and "unsafe-inline" not in h["content-security-policy"] and "unsafe-eval" not in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "same-origin", url
        assert "form-action 'self'" in h["content-security-policy"] and "access-control-allow-origin" not in h, url
        assert h["x-frame-options"] == "DENY" and h["cache-control"] == "no-store", url
    assert "<script" not in c.get("/").text.replace('<script src="/static/select.js" defer></script>', "").lower() and "style=" not in c.get("/").text.lower()


def test_no_external_resources_and_no_openapi_pages(site):
    c, _ = site
    body = c.get("/").text
    assert "http://" not in body.replace("http://127.0.0.1", "") and "https://" not in body and "//cdn" not in body
    for url in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(url).status_code == 404


def test_the_pages_of_chunk_1_take_no_writes_and_only_the_ten_post_routes_exist(site):
    c, ids = site
    app = c.app
    posts = sorted(r.path for r in app.routes if "POST" in (getattr(r, "methods", None) or ()))
    assert posts == ["/bulk-status", "/jobs/{job_id}/applied", "/jobs/{job_id}/approve", "/jobs/{job_id}/description", "/jobs/{job_id}/findings/accept",
                     "/jobs/{job_id}/interviews", "/jobs/{job_id}/interviews/{round_id}", "/jobs/{job_id}/interviews/{round_id}/delete", "/jobs/{job_id}/notes", "/jobs/{job_id}/notes/{note_id}/delete",
                     "/jobs/{job_id}/recheck", "/jobs/{job_id}/reject", "/jobs/{job_id}/revise", "/jobs/{job_id}/shortlist", "/jobs/{job_id}/undo-approval", "/jobs/{job_id}/undo-status"]
    methods = {m for r in app.routes for m in (getattr(r, "methods", None) or ())}
    assert methods <= {"GET", "HEAD", "POST"}                    # no PUT, PATCH or DELETE anywhere
    for url in ("/", "/jobs", f"/jobs/{ids[0]}"):
        for verb in ("post", "put", "patch", "delete"):
            assert getattr(c, verb)(url).status_code == 405, (verb, url)
    for url in (f"/jobs/{ids[0]}/{a}" for a in ("shortlist", "applied", "approve", "reject", "undo-approval", "undo-status")):
        for verb in ("put", "patch", "delete"):
            assert getattr(c, verb)(url).status_code == 405, (verb, url)


def test_database_connection_is_read_only(site, tmp_path):
    c, ids = site
    conn = c.app.state.open_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
        for sql in ("DELETE FROM jobs", "UPDATE jobs SET title = 'x'", "INSERT INTO runs (id, started_at) VALUES ('z', 'now')",
                    "CREATE TABLE evil (a)", "PRAGMA user_version = 99"):
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(sql)
    finally:
        conn.close()
    w = sqlite3.connect(tmp_path / "w.db")
    assert w.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3 and w.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    w.close()


def test_a_request_leaves_the_database_file_untouched(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(1), job(2)], [60, 70])
    conn.close()
    path = tmp_path / "w.db"
    before = path.read_bytes()             # a read-only open of a WAL database may create -shm/-wal side files; the database file itself must not change
    c = client(cfg, tmp_path)
    for url in ("/", "/jobs", f"/jobs/{ids[0]}", "/jobs/9999"):
        c.get(url)
    assert path.read_bytes() == before


def test_reads_work_while_the_pipeline_holds_a_write_transaction(site, conn):
    c, ids = site
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE jobs SET title = 'changing' WHERE id = ?", (ids[0],))
    try:
        assert c.get("/").status_code == 200 and c.get(f"/jobs/{ids[0]}").status_code == 200
    finally:
        conn.rollback()


def test_foreign_host_header_is_refused(site):
    c, _ = site
    assert c.get("/", headers={"host": "evil.example"}).status_code == 400
    assert c.get("/", headers={"host": "127.0.0.1:8000"}).status_code == 200
    assert c.get("/", headers={"host": "localhost:8000"}).status_code == 200
    assert c.get("/", headers={"host": "[::1]:8000"}).status_code == 200
    assert c.get("/", headers={"host": "127.0.0.1.evil.example"}).status_code == 400


# --- content rules ----------------------------------------------------------------------------------

def test_fact_ids_are_stripped(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(1)], [80])
    with conn:
        conn.execute("UPDATE scores SET details = ? WHERE job_id = ?", (json.dumps({
            "summary": "ok", "matches": ["SQL reporting [exp-northwind-zurich-analysis]"], "gaps": ["Kotlin [skill-kotlin]"],
            "red_flags": ["Travel [exp-x-y]"]}), ids[0]))
    c = client(cfg, tmp_path)
    detail = c.get(f"/jobs/{ids[0]}").text
    assert "SQL reporting" in detail and "Kotlin" in detail and "Travel" in detail and "[exp-" not in detail and "[skill-" not in detail
    assert "[exp-" not in c.get("/").text


def test_bands_follow_the_config_thresholds(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(1), job(2)], [45, 75])
    cfg["scoring"]["band_low_below"], cfg["scoring"]["band_high_from"] = 40, 80
    c = client(cfg, tmp_path)
    top = c.get("/").text
    assert 'class="card band-medium"' in top and top.count('class="card band-medium"') == 2 and "band-high" not in top.replace("band-high {", "")
    assert ">MEDIUM<" in c.get(f"/jobs/{ids[0]}").text and ">MEDIUM<" in c.get(f"/jobs/{ids[1]}").text
    cfg["scoring"]["band_low_below"], cfg["scoring"]["band_high_from"] = 50, 70
    assert ">LOW<" in c.get(f"/jobs/{ids[0]}").text and ">HIGH<" in c.get(f"/jobs/{ids[1]}").text


def test_home_address_and_config_values_never_appear(cfg, conn, tmp_path):
    cfg["location"]["car_origin_address"] = "Secretstrasse 12, 8000 Hometown"
    cfg["email"]["recipient"] = "private.person@example.com"
    ids, _ = pipeline(conn, cfg, [job(1)], [80])
    c = client(cfg, tmp_path)
    for url in ("/", "/jobs", f"/jobs/{ids[0]}"):
        b = c.get(url).text
        assert "Secretstrasse" not in b and "private.person" not in b and "GMAIL" not in b


def test_static_css_is_served_and_the_page_is_responsive(site):
    c, _ = site
    css = c.get("/static/style.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"] and "@media (max-width" in css.text
    assert 'name="viewport"' in c.get("/").text
    assert c.get("/static/../web.py").status_code in (404, 400)


# --- config, command line ---------------------------------------------------------------------------

def test_web_config_defaults_and_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    cfg = load_config()
    assert cfg["web"] == {"host": "127.0.0.1", "port": 8000, "container_bind": False, "allowed_hosts": [],
                          "login": {"username": "", "stay_days": 90}}
    for host in ("0.0.0.0", "192.168.1.5", "::", "", "example.com"):
        p = tmp_path / "c.yaml"
        p.write_text(f"web:\n  host: '{host}'\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="web.host"):
            load_config(p)
    for port in ("0", "70000", "'80'", "true", "-1"):
        p.write_text(f"web:\n  port: {port}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="web.port"):
            load_config(p)
    p.write_text("web:\n  host: localhost\n  port: 8123\n", encoding="utf-8")
    assert load_config(p)["web"] == {"host": "localhost", "port": 8123, "container_bind": False, "allowed_hosts": [],
                                     "login": {"username": "", "stay_days": 90}}
    p.write_text("web:\n  typo: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(p)


def test_serve_refuses_a_network_host_before_anything_starts(cfg):
    started = []
    for host in ("0.0.0.0", "::", "192.168.0.10", "evil.example"):
        with pytest.raises(ConfigError, match="127.0.0.1"):
            web.serve(cfg, host=host, run=lambda *a, **k: started.append(1))
    with pytest.raises(ConfigError, match="web.port"):
        web.serve(cfg, port=0, run=lambda *a, **k: started.append(1))
    assert started == []


def test_serve_passes_the_app_and_the_local_address_to_the_server(cfg, capsys):
    calls = []
    assert web.serve(cfg, run=lambda app, **kw: calls.append((app, kw))) == 0
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8000, "log_level": "warning"} and calls[0][0].title == "Recruitment"
    assert "http://127.0.0.1:8000/" in capsys.readouterr().out
    web.serve(cfg, host="::1", port=8123, run=lambda app, **kw: calls.append((app, kw)))
    assert calls[1][1]["host"] == "::1" and calls[1][1]["port"] == 8123


def test_web_command_is_wired_and_does_not_migrate(config_file, tmp_path, capsys):
    calls = []
    assert cli.main(["--config", str(config_file), "web", "--port", "8123"], web_runner=lambda app, **kw: calls.append(kw)) == 0
    assert calls == [{"host": "127.0.0.1", "port": 8123, "log_level": "warning"}]
    assert not (tmp_path / "jobs.db").exists()                      # db.connect was never called
    assert "http://127.0.0.1:8123/" in capsys.readouterr().out


def test_web_command_refuses_other_hosts_with_exit_3(config_file, capsys):
    calls = []
    assert cli.main(["--config", str(config_file), "web", "--host", "0.0.0.0"], web_runner=lambda *a, **k: calls.append(1)) == 3
    assert "configuration error" in capsys.readouterr().err and calls == []
    assert cli.build_parser().parse_args(["web"]).command == "web"


def test_web_command_uses_the_config_file_values(config_file, tmp_path):
    config_file.write_text(config_file.read_text(encoding="utf-8") + "web:\n  host: localhost\n  port: 8555\n", encoding="utf-8")
    calls = []
    assert cli.main(["--config", str(config_file), "web"], web_runner=lambda app, **kw: calls.append(kw)) == 0
    assert calls == [{"host": "localhost", "port": 8555, "log_level": "warning"}]


def _outside_setup(conn, cfg, scores):
    cfg["scoring"]["top_n"] = 2
    return pipeline(conn, cfg, [job(i) for i in range(1, len(scores) + 1)], scores)


def test_outside_count_ignores_title_only_and_promoted_jobs(cfg, conn, tmp_path):
    ids, _ = _outside_setup(conn, cfg, [90, 80, 55, 49])
    with conn:
        conn.execute("UPDATE batch_jobs SET title_only = 1 WHERE job_id = ?", (ids[2],))     # never ranked without a description
        conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (ids[0],))
    assert report.collect(conn, cfg, "run1", hide_acted=True).outside_count == 0
    assert report.collect(conn, cfg, "run1").outside_count == 0
    assert "No job outside the top 2" in client(cfg, tmp_path).get("/").text


def test_outside_count_web_hides_acted_but_mail_keeps_not_selected_ones(cfg, conn):
    ids, _ = _outside_setup(conn, cfg, [90, 80, 60])
    with conn:
        conn.execute("UPDATE jobs SET status = 'applied' WHERE id = ?", (ids[2],))
    assert report.collect(conn, cfg, "run1", hide_acted=True).outside_count == 0
    assert report.collect(conn, cfg, "run1").outside_count == 1


def test_outside_count_skips_carried_over_jobs(cfg, conn):
    ids, _ = _outside_setup(conn, cfg, [90, 80, 60])
    with conn:
        conn.execute("UPDATE batch_jobs SET fresh = 0 WHERE job_id = ?", (ids[2],))
    assert report.collect(conn, cfg, "run1", hide_acted=True).outside_count == 0
    assert report.collect(conn, cfg, "run1").outside_count == 0


def test_every_page_with_the_sidebar_marks_exactly_one_link_as_the_current_page(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(1), job(2)], [55, 90])
    cfg["backup"]["dir"] = str(tmp_path / "bk")
    c = client(cfg, tmp_path)
    current = {"/": "/", "/jobs": "/jobs", "/applied": "/applied", "/missing": "/missing", "/analytics": "/analytics", "/health": "/health",
               f"/jobs/{ids[0]}": "/jobs", f"/jobs/{ids[0]}/reject": "/jobs", "/jobs/999": "/jobs", f"/jobs/{ids[0]}/description": "/missing"}
    for url, link in current.items():
        body = c.get(url).text
        assert body.count('aria-current="page"') == 1, url
        assert f'<a href="{link}" aria-current="page">' in body, url
