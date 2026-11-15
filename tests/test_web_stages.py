"""The stage pages, v0.96.0 (goal.md 5.7): /interviews, /offers, /rejected and /withdrawn next to /applied, the menu sections, the
/jobs filter and the selection markup that works without JavaScript (checkboxes and a bar that open /bulk-status).
Every test builds its own temporary database; none uses the real config, the real database or the network."""
import html
import re

import pytest

from app import db, interviews, statusflow, web
from tests.test_web import client, conn, cfg, site                                                    # noqa: F401  (fixtures and helpers)
from tests.test_web_bulk import dbfile, bulk, flash_of, set_up, walk                                   # noqa: F401
from tests.test_web_writes import history, seen, snapshot, status_of

PAGES = {"interview": "/interviews", "offer": "/offers", "rejected": "/rejected", "withdrawn": "/withdrawn", "applied": "/applied"}


def put(conn, ids, status):
    """Walk the jobs to a status of the stages (rejected and withdrawn come from the interview stage)."""
    for i in ids:
        if status in ("rejected", "withdrawn"):
            walk(conn, i, "shortlisted", "applied", "interview", status)
        else:
            set_up(conn, [i], status)


def rows_of(page_text):
    return re.findall(r'<td class="title"><a href="/jobs/(\d+)">', page_text)


@pytest.mark.parametrize("status,path", list(PAGES.items()))
def test_each_page_lists_only_its_status(site, conn, status, path):
    c, ids = site
    put(conn, [ids[0]], status)
    others = {"applied": "interview", "interview": "offer", "offer": "rejected", "rejected": "withdrawn", "withdrawn": "applied"}[status]
    put(conn, [ids[1]], others)
    r = c.get(path)
    assert r.status_code == 200 and rows_of(r.text) == [str(ids[0])]
    assert "script-src 'self'" in r.headers["content-security-policy"] and "<script" not in r.text.replace('<script src="/static/select.js" defer></script>', "").lower()


@pytest.mark.parametrize("status,path,empty", [("interview", "/interviews", "No job is in the interview stage."), ("offer", "/offers", "No job has an offer."),
                                                ("rejected", "/rejected", "No employer has rejected"), ("withdrawn", "/withdrawn", "You have not withdrawn")])
def test_an_empty_page_says_so_and_the_title_has_the_count(site, conn, status, path, empty):
    c, ids = site
    r = c.get(path)
    assert r.status_code == 200 and empty in r.text and "(0)</h1>" in r.text and 'class="selbar"' not in r.text
    put(conn, ids[:2], status)
    page = c.get(path).text
    assert "(2)</h1>" in page and empty not in page


def test_the_stage_date_comes_from_the_history_and_the_newest_is_first(site, conn, dbfile):
    c, ids = site
    put(conn, ids, "interview")
    with conn:
        for jid, day in zip(ids, ("2026-11-03", "2026-11-09", "2026-11-05")):
            conn.execute("UPDATE status_history SET changed_at = ? WHERE job_id = ? AND to_status = 'interview'", (f"{day}T08:00:00Z", jid))
            conn.execute("UPDATE status_history SET changed_at = '2026-11-01T08:00:00Z' WHERE job_id = ? AND to_status = 'applied'", (jid,))
    page = c.get("/interviews").text
    assert rows_of(page) == [str(ids[1]), str(ids[2]), str(ids[0])]
    assert "2026-11-09" in page and "2026-11-01" in page and "In stage since" in page and "Applied on" in page and ">Days<" in page


def test_the_pager_works_on_a_stage_page(site, conn):
    c, ids = site
    with conn:
        for i in range(web.PAGE_SIZE + 5):
            conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) VALUES (?, ?, 'Zeta', 'offer', 'a', 'a')", (f"o{i}", f"Offer {i}"))
    first = c.get("/offers").text
    assert len(rows_of(first)) == web.PAGE_SIZE and "Page 1 of 2" in first and 'href="/offers?page=2"' in first
    second = c.get("/offers?page=2").text
    assert len(rows_of(second)) == 5 and "Page 2 of 2" in second and "(205)</h1>" in second
    assert "Page 2 of 2" in c.get("/offers?page=99").text and "Page 1 of 2" in c.get("/offers?page=%C2%B2").text


def test_jobs_and_the_top_10_hide_every_job_from_the_application_on(site, conn):
    c, ids = site
    assert all(f"/jobs/{i}\"" in c.get("/jobs").text for i in ids)
    put(conn, [ids[0]], "applied")
    put(conn, [ids[1]], "offer")
    put(conn, [ids[2]], "rejected")
    jobs = c.get("/jobs").text
    assert not any(f'<a href="/jobs/{i}">' in jobs for i in ids)
    top = c.get("/").text
    assert not any(f'<a href="/jobs/{i}">' in top for i in ids)
    assert "Analyst" not in c.get("/missing").text


def test_the_menu_has_two_sections_and_one_current_link(site, conn):
    c, ids = site
    body = c.get("/").text
    assert body.index('<p class="navhead">Jobs</p>') < body.index('href="/jobs"') < body.index('href="/missing"') < body.index('<p class="navhead">Applications</p>') \
        < body.index('href="/applied"') < body.index('href="/interviews"') < body.index('href="/offers"') < body.index('href="/rejected"') < body.index('href="/withdrawn"') \
        < body.index('<p class="navhead">Insights</p>')
    for path, text in (("/", "Top 10"), ("/jobs", "All open jobs"), ("/missing", "Missing descriptions"), ("/applied", "Applied"), ("/interviews", "Interviews"),
                       ("/offers", "Offers"), ("/rejected", "Rejected by employer"), ("/withdrawn", "Withdrawn"), ("/analytics", "Analytics")):
        page = c.get(path).text
        assert re.findall(r'<a href="([^"]+)" aria-current="page">', page) == [path], path
        assert f'aria-current="page">{text}</a>' in page, path


def test_the_reject_page_does_not_light_up_rejected_by_employer(site):
    c, ids = site
    for path in (f"/jobs/{ids[0]}/reject", f"/jobs/{ids[0]}"):
        page = c.get(path).text
        assert 'href="/rejected" aria-current' not in page and 'href="/withdrawn" aria-current' not in page
        assert re.findall(r'<a href="([^"]+)" aria-current="page">', page) == ["/jobs"]


@pytest.mark.parametrize("status,actions", [("applied", ["interview", "employer_rejected", "withdrawn"]), ("interview", ["offer", "employer_rejected", "withdrawn"]),
                                            ("offer", ["accept", "employer_rejected", "withdrawn"]), ("accepted", []), ("rejected", []), ("withdrawn", [])])
def test_the_job_page_links_the_actions_of_its_status(site, conn, status, actions):
    c, ids = site
    put(conn, [ids[0]], status)
    page = c.get(f"/jobs/{ids[0]}").text
    found = re.findall(r'href="/bulk-status\?action=(\w+)&amp;id=(\d+)&amp;next=job"', page)
    assert [a for a, _ in found] == actions and {i for _, i in found} == ({str(ids[0])} if actions else set())
    for a in actions:                                                                               # each link leads to a working confirm page
        r = c.get(f"/bulk-status?action={a}&id={ids[0]}&next=job")
        assert r.status_code == 200 and "will become" in r.text


def test_the_labels_of_the_offers_page_and_the_default_labels(site, conn):
    c, ids = site
    put(conn, [ids[0]], "offer")
    offers = c.get("/offers").text
    assert ">Declined&hellip;</a>" in offers and ">Offer withdrawn by employer&hellip;</a>" in offers and ">Withdrawn&hellip;</a>" not in offers
    assert '>Declined</button>' in offers and 'value="employer_rejected">Offer withdrawn by employer</button>' in offers
    put(conn, [ids[1]], "applied")
    applied = c.get("/applied").text
    assert ">Interview&hellip;</a>" in applied and ">Rejected by employer&hellip;</a>" in applied and ">Withdrawn&hellip;</a>" in applied


def test_the_row_checkboxes_and_the_bar_per_page(site, conn):
    c, ids = site
    put(conn, [ids[0]], "applied")
    for page, bar in (("/jobs", ["shortlist", "applied", "undo", "reject"]), ("/", ["shortlist", "applied", "undo", "reject"]), ("/missing", ["reject"]),
                      ("/applied", ["interview", "employer_rejected", "withdrawn", "undo", "reject"])):
        body = c.get(page).text
        if page == "/missing":
            continue                                                                              # the queue is empty here: see its own test
        assert body.count('id="bulk"') == 1 and 'method="get" action="/bulk-status"' in body
        assert re.findall(r'<button type="submit" form="bulk" name="action" value="(\w+)"', body) == bar, page
    body = c.get("/applied").text
    assert body.count('class="pick"') == 1 and f'name="id" value="{ids[0]}" form="bulk" aria-label="Select job {ids[0]}: Analyst 1"' in body
    assert 'class="selectall" hidden' in body and 'name="next" value="applied"' in body and 'role="toolbar"' in body
    m = re.search(r'data-actions="([^"]*)"', body)
    assert m and m.group(1) == "interview employer_rejected withdrawn undo reject" and 'data-title="Analyst 1"' in body and 'data-company="Acme"' in body


def test_data_actions_follow_the_status_of_each_row(site, conn):
    c, ids = site
    walk(conn, ids[1], "shortlisted")
    body = c.get("/jobs").text
    pairs = re.findall(r'data-actions="([^"]*)" data-title="[^"]*" data-company="[^"]*"><input[^>]*value="(\d+)"', body)
    by_id = {int(i): a for a, i in pairs}
    assert by_id[ids[0]] == "shortlist reject" and by_id[ids[1]] == "applied undo reject"


def test_the_top_10_card_has_a_checkbox_and_its_actions(site, conn):
    c, ids = site
    walk(conn, ids[1], "shortlisted")
    body = c.get("/").text
    assert re.search(r'<article class="card band-\w+" data-actions="applied undo reject" data-title="Analyst 2" data-company="Acme">', body)
    assert body.count('class="pick"') == 3 and 'name="next" value="top"' in body and "Reject selected…" in body


def test_the_missing_page_offers_reject_only(cfg, conn, tmp_path):
    from tests.test_report import job, pipeline
    cfg["paths"]["lock"] = str(tmp_path / "run.lock")
    cfg["scoring"]["top_n"] = 2
    ids, _ = pipeline(conn, cfg, [job(1), job(2, description=""), job(3, description="")], [80, 60, 50])
    c = client(cfg, tmp_path)
    body = c.get("/missing").text
    assert re.findall(r'<button type="submit" form="bulk" name="action" value="(\w+)"', body) == ["reject"]
    assert 'data-actions="reject"' in body and body.count('class="pick"') == 1 and 'name="next" value="missing"' in body
    confirm = c.get(f"/bulk-status?action=reject&id={ids[1]}&next=missing")
    assert confirm.status_code == 200 and "will be rejected and archived" in confirm.text
    assert c.get(f"/bulk-status?action=shortlist&id={ids[1]}&next=missing").status_code == 400


def test_titles_with_markup_are_escaped_in_the_attributes(site, conn):
    c, ids = site
    evil = '"><script>alert(1)</script>'
    with conn:
        conn.execute("UPDATE jobs SET title = ?, company = ? WHERE id = ?", (evil, evil, ids[0]))
    put(conn, [ids[1]], "applied")
    conn.execute("UPDATE jobs SET title = ?, company = ? WHERE id = ?", (evil, evil, ids[1]))
    conn.commit()
    for page in ("/jobs", "/", "/applied"):
        body = c.get(page).text
        assert "<script>" not in body and "&lt;script&gt;" in body, page
        for attr in re.findall(r'data-(?:title|company)="([^"]*)"', body):
            assert "<" not in attr and '"' not in attr


def test_pages_never_write(site, conn, dbfile):
    c, ids = site
    put(conn, [ids[0]], "interview")
    before = snapshot(dbfile)
    for path in ("/interviews", "/offers", "/rejected", "/withdrawn", "/applied", "/jobs", "/", "/missing", f"/bulk-status?action=offer&id={ids[0]}&next=interviews"):
        assert c.get(path).status_code == 200, path
    assert snapshot(dbfile) == before


def test_end_to_end_through_the_confirm_page(site, conn, dbfile):
    """The no-JavaScript path: the selection form opens the confirm page, its form changes the jobs, the next stage page shows them."""
    c, ids = site
    put(conn, ids[:2], "applied")
    page = c.get("/applied").text
    assert str(ids[0]) in page
    confirm = c.get(f"/bulk-status?next=applied&page=1&id={ids[0]}&id={ids[1]}&action=interview")      # what the GET form of the page sends
    assert confirm.status_code == 200 and "Interview: 2 of 2 jobs?" in seen(confirm)
    token = re.search(r'name="csrf" value="([0-9a-f]{64})"', confirm.text).group(1)
    ids_field = re.search(r'name="ids" value="([\d,]+)"', confirm.text).group(1)
    r = c.post("/bulk-status", data={"csrf": token, "action": "interview", "ids": ids_field, "next": "applied", "page": "1", "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/applied?flash=")
    assert sorted(rows_of(c.get("/interviews").text)) == sorted(map(str, ids[:2])) and rows_of(c.get("/applied").text) == []
    assert "2 jobs are now interview" in flash_of(c, r)
    # forward again from the Interviews page, then back with Undo
    r = bulk(c, "offer", [ids[0]], nxt="interviews", confirm="yes")
    assert r.headers["location"].startswith("/interviews?flash=") and rows_of(c.get("/offers").text) == [str(ids[0])]
    r = bulk(c, "undo", [ids[0]], nxt="offers", confirm="yes")
    assert status_of(dbfile, ids[0]) == "interview" and history(conn, ids[0])[-1] == ("offer", "interview", "web undo")


def test_the_single_undo_button_comes_back_to_the_stage_page(site, conn, dbfile):
    c, ids = site
    put(conn, [ids[0]], "interview")
    page = c.get("/interviews").text
    assert f'href="/jobs/{ids[0]}/undo-status?next=interviews"' in page                              # a stage asks first
    confirm = c.get(f"/jobs/{ids[0]}/undo-status?next=interviews")
    assert confirm.status_code == 200 and "back to <code>applied</code>" in confirm.text
    r = c.post(f"/jobs/{ids[0]}/undo-status", data={"csrf": c.app.state.csrf(f"undo-status:{ids[0]}"), "next": "interviews", "confirm": "yes"}, follow_redirects=False)
    assert r.headers["location"].startswith("/interviews?flash=") and status_of(dbfile, ids[0]) == "applied"
    walk(conn, ids[0], "interview")
    before = snapshot(dbfile)                                                                        # an undo of a stage needs confirm=yes since v0.96.0
    r = c.post(f"/jobs/{ids[0]}/undo-status", data={"csrf": c.app.state.csrf(f"undo-status:{ids[0]}"), "next": "interviews"}, follow_redirects=False)
    assert "Not confirmed" in flash_of(c, r) and snapshot(dbfile) == before


def test_no_stage_status_is_listed_twice_and_every_stage_has_a_page():
    assert set(web.STAGE_STATUSES) == set(statusflow.STAGE_PAGES.values()) and set(web.STAGE_STATUSES.values()) == set(statusflow.STAGE_PAGES)
    assert set(statusflow.STAGE_PAGES.values()) <= set(web.PAGE_PATHS)
    assert set(web.report.ACTED) == set(statusflow.STAGE_PAGES)


def test_the_job_page_of_an_offer_job_uses_the_offer_labels_and_others_do_not(site, conn):
    c, ids = site
    put(conn, [ids[0]], "offer")
    put(conn, [ids[1]], "applied")
    offer = c.get(f"/jobs/{ids[0]}").text
    assert ">Declined&hellip;</a>" in offer and ">Offer withdrawn by employer&hellip;</a>" in offer
    applied = c.get(f"/jobs/{ids[1]}").text
    assert "Declined" not in applied and "Offer withdrawn by employer" not in applied and ">Withdrawn&hellip;</a>" in applied


def test_the_applied_heading_has_the_count(site, conn):
    c, ids = site
    assert "<h1>Applied (0)</h1>" in c.get("/applied").text or "<h1>Applied" in c.get("/applied").text
    put(conn, ids[:2], "applied")
    assert "<h1>Applied (2)</h1>" in c.get("/applied").text


# --- Accepted (v0.98.0) -------------------------------------------------------------------------------------------

def test_the_accepted_page_lists_only_accepted_jobs_and_sits_after_offers(site, conn):
    c, ids = site
    r = c.get("/accepted")
    assert r.status_code == 200 and "You have not accepted any offer yet." in r.text and "(0)</h1>" in r.text
    put(conn, [ids[0]], "accepted")
    put(conn, [ids[1]], "offer")
    r = c.get("/accepted")
    assert rows_of(r.text) == [str(ids[0])] and "(1)</h1>" in r.text
    assert rows_of(c.get("/offers").text) == [str(ids[1])]
    body = c.get("/").text
    assert body.index('href="/offers"') < body.index('href="/accepted"') < body.index('href="/rejected"')
    assert re.findall(r'<a href="([^"]+)" aria-current="page">', r.text) == ["/accepted"]
    assert str(ids[0]) not in rows_of(c.get("/jobs").text) and str(ids[0]) not in rows_of(c.get("/").text)


def test_the_offers_page_shows_accept(site, conn):
    c, ids = site
    put(conn, [ids[0]], "offer")
    assert 'data-label-accept="Accepted"' in re.search(r'<form id="bulk"[^>]*>', c.get("/offers").text).group(0)


# --- the Next interview column (v0.99.0): /interviews only -------------------------------------------------------------

def _next_cells(page_text):
    return re.findall(r'data-label="Next interview">([^<]*)</td>', page_text)


def test_the_next_interview_is_the_earliest_future_round_in_local_time(site, conn):
    c, ids = site
    put(conn, ids[:2], "interview")
    for day in ("2001-01-01", "2099-01-01", "2098-06-01"):                                 # a past round, a far one and the next one (summer: UTC+2)
        interviews.add_round(conn, ids[0], day, "09:30", "video", "", "", "Europe/Zurich")
    interviews.add_round(conn, ids[2], "2097-01-01", "09:30", "video", "", "", "Europe/Zurich")      # another job, not on the page
    page = c.get("/interviews").text
    shown = dict(zip(rows_of(page), _next_cells(page)))
    assert "Next interview" in page and shown == {str(ids[0]): "2098-06-01 09:30", str(ids[1]): "-"}
    assert "2097-01-01" not in page and "2099-01-01" not in page and "2001-01-01" not in page


def test_no_future_round_shows_a_dash(site, conn):
    c, ids = site
    put(conn, ids[:1], "interview")
    interviews.add_round(conn, ids[0], "2001-01-01", "09:30", "video", "", "", "Europe/Zurich")
    assert _next_cells(c.get("/interviews").text) == ["-"]


@pytest.mark.parametrize("page", statusflow.APPLICATION_PAGES)
def test_the_column_is_only_on_the_next_interview_pages(site, conn, page):
    c, ids = site
    status = {p: s for s, p in statusflow.STAGE_PAGES.items()}[page]
    put(conn, ids[:1], status)
    assert ("Next interview" in c.get("/" + page).text) == (page in statusflow.NEXT_INTERVIEW_PAGES) and statusflow.NEXT_INTERVIEW_PAGES == ("interviews",)
