"""One table of the stage pages (v0.97.0, K2): statusflow.STAGE_PAGES drives the routes, the menu, the titles and the empty texts, and the
page tuples of app/web.py. Every test builds its own temporary database; none uses the real config, the real database or the network."""
import re

import pytest

from app import statusflow, web
from tests.test_web import client, conn, cfg, site                                                    # noqa: F401  (fixtures and helpers)

PAGES = statusflow.APPLICATION_PAGES


def test_the_table_is_complete():
    assert list(PAGES) == list(statusflow.STAGE_PAGES.values())
    assert set(statusflow.STAGE_PAGE_TEXT) == set(PAGES) and set(PAGES) <= set(statusflow.PAGE_ACTIONS)
    for title, empty in statusflow.STAGE_PAGE_TEXT.values():
        assert title and empty


@pytest.mark.parametrize("page", PAGES)
def test_every_page_has_a_route_a_title_an_empty_text_and_its_own_menu_mark(site, page):
    c, _ = site
    r = c.get(f"/{page}")
    title, empty = statusflow.STAGE_PAGE_TEXT[page]
    assert r.status_code == 200 and f"<title>{title} - Recruitment</title>" in r.text and empty in r.text and f"<h1>{title} (0)</h1>" in r.text
    current = re.findall(r'<a href="([^"]+)" aria-current="page"', r.text)
    assert current == [f"/{page}"]
    for other in PAGES:
        assert f'<a href="/{other}"' in r.text and f">{statusflow.STAGE_PAGE_TEXT[other][0]}</a>" in r.text


@pytest.mark.parametrize("page", PAGES)
def test_every_page_is_in_the_tuples_of_the_web_module(page):
    assert page in web.NEXT_PAGES and web.PAGE_PATHS[page] == f"/{page}" and web.BULK_PATHS[page] == f"/{page}" and page in web.BULK_PAGED


def test_the_other_pages_keep_their_own_menu_mark(site):
    c, ids = site
    for path in ("/", "/jobs", "/missing", "/analytics", "/health", f"/jobs/{ids[0]}"):
        marks = re.findall(r'<a href="([^"]+)" aria-current="page"', c.get(path).text)
        assert len(marks) == 1 and marks[0] not in {f"/{p}" for p in PAGES}, path
