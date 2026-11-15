"""The static demo (goal.md 7.4, v0.100.0): scripts/freeze_demo.py, site/ and the demo mode of the web app.

Every build runs on a fictional database in a temporary folder; nothing here touches data/, the real config or the network.
"""
import re
import smtplib
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_public_copy as bpc                      # noqa: E402
import freeze_demo as fd                              # noqa: E402
import seed_demo_db                                   # noqa: E402
from app import web                                   # noqa: E402
from app.config import load_config                    # noqa: E402

LINK = re.compile(r'\b(?:href|src|action)="([^"]*)"')


def build_into(path):
    out = fd.prepare_out(path, False)
    return out, fd.build(out)


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    out, counts = build_into(tmp_path_factory.mktemp("site") / "_site")
    return out, counts


def pages(out):
    return sorted((out / "demo").rglob("*.html"))


def text(p):
    return p.read_text(encoding="utf-8")


def test_the_freeze_writes_the_landing_page_the_demo_and_the_headers(frozen):
    out, counts = frozen
    for name in ("index.html", "404.html", "style.css", "_headers", "demo/index.html", "demo/404.html", "demo/jobs/index.html",
                 "demo/static/style.css", "demo/static/vendor/LICENSE.md"):
        assert (out / name).is_file(), name
    assert counts["pages"] > 10 and counts["jobs"] > 10
    assert any((out / "demo" / "jobs").glob("*/index.html"))


def test_no_page_has_a_form_a_token_or_a_javascript_link(frozen):
    out, _ = frozen
    for p in pages(out):
        body = text(p).lower()
        for banned in ("<form", "csrf", 'method="post"', "/bulk-status", "javascript:"):
            assert banned not in body, f"{p.relative_to(out)} has {banned}"


def test_every_demo_page_has_the_banner_and_a_link_back_to_the_landing_page(frozen):
    out, _ = frozen
    for p in pages(out):
        body = text(p)
        assert "Fictional data, read-only snapshot; nothing is saved" in body, p
        assert 'class="demo-banner"' in body and 'href="/"' in body


def test_every_root_link_stays_in_the_demo_and_resolves_to_a_file(frozen):
    out, _ = frozen
    seen = 0
    for p in pages(out):
        for value in LINK.findall(text(p)):
            value = value.replace("&amp;", "&")
            if not value.startswith("/"):
                assert value.startswith(("https://", "#")), f"{p}: {value}"
                continue
            path = urlsplit(value).path
            assert path == "/" or path.startswith("/demo/"), f"{p}: {value}"
            assert "?" not in value, f"{p}: {value}"
            target = out / path.lstrip("/")
            assert (target / "index.html" if path.endswith("/") else target).is_file(), f"{p}: {value}"
            seen += 1
    assert seen > 100


def test_the_page_links_of_a_list_become_page_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "PAGE_SIZE", 5)
    out, _ = build_into(tmp_path / "_site")
    first = text(out / "demo" / "jobs" / "index.html")
    assert 'href="/demo/jobs/page/2/"' in first and "?page=" not in first
    assert 'href="/demo/jobs/"' in text(out / "demo" / "jobs" / "page" / "2" / "index.html")      # Previous of page 2
    assert (out / "demo" / "jobs" / "page" / "3" / "index.html").is_file()


def test_every_get_route_is_frozen_or_on_the_skip_list_with_a_reason(frozen, tmp_path):
    out, _ = frozen
    seed_demo_db.seed(tmp_path / "jobs.db", 12)
    cfg = load_config(tmp_path / "demo-config.yaml")
    app = web.create_app(cfg, tmp_path / "jobs.db", allowed_hosts=("testserver",), demo=True)
    for route in app.routes:
        if "GET" in getattr(route, "methods", ()) and "{" not in route.path:
            if route.path in fd.SKIP_ROUTES:
                assert fd.SKIP_ROUTES[route.path].strip()
            else:
                _, public, file = fd.public_target(route.path)
                assert (out / file).is_file(), f"{route.path} was not frozen"
    assert all(fd.SKIP_ROUTES.values())


def test_the_freeze_needs_no_network_mail_or_subprocess(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the freeze must not use the network, mail or a subprocess")
    real_connect = socket.socket.connect

    def loopback_only(self, address, *a, **k):
        # asyncio's Windows Proactor loop connects a self-pipe to 127.0.0.1 at start; that is not the network
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return real_connect(self, address, *a, **k)
        refuse()
    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(smtplib.SMTP, "__init__", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    out, counts = build_into(tmp_path / "_site")
    assert counts["pages"] > 10


def test_the_scan_of_the_frozen_tree_finds_nothing(frozen):
    out, _ = frozen
    findings, problem = fd.scan(out, denylist=False)
    assert problem == "" and findings == [], [str(f) for f in findings]


def test_the_scan_asks_for_the_denylist_unless_told_not_to(frozen, tmp_path):
    out, _ = frozen
    assert fd.scan(out, local_path=tmp_path / "missing.yaml")[1]
    local = tmp_path / "local.yaml"
    local.write_text("denylist: [Contoso]\n", encoding="utf-8")
    findings, problem = fd.scan(out, local_path=local)
    assert problem == "" and findings and all(f.rule.startswith("denylist") for f in findings)


def test_the_contact_values_of_the_master_cv_appear_nowhere_in_the_demo(frozen):
    out, _ = frozen
    cv = ROOT / "profile" / "master_cv.yaml"
    if not cv.is_file():
        pytest.skip("no profile/master_cv.yaml")
    person = yaml.safe_load(cv.read_text(encoding="utf-8")).get("person", {})
    values = [str(person[k]) for k in ("email", "phone", "address", "street", "birthdate") if person.get(k)]
    values += [re.sub(r"\D", "", str(person["phone"]))] if person.get("phone") else []
    assert values
    for p in (out / "demo").rglob("*"):
        if p.is_file() and p.suffix in (".html", ".css", ".js", ".md"):
            body = text(p)
            for v in values:
                assert v not in body, f"{p.relative_to(out)} holds a contact value"


def test_a_post_to_the_demo_app_gets_405_and_the_default_app_still_has_forms(tmp_path):
    seed_demo_db.seed(tmp_path / "jobs.db", 12)
    cfg = load_config(tmp_path / "demo-config.yaml")
    demo = TestClient(web.create_app(cfg, tmp_path / "jobs.db", allowed_hosts=("testserver",), demo=True), follow_redirects=False)
    assert demo.post("/jobs/1/shortlist", data={"csrf": "x", "next": "job"}).status_code == 405
    assert demo.post("/bulk-status", data={}).status_code == 405
    assert demo.get("/jobs").status_code == 200 and "<form" not in demo.get("/jobs").text
    normal = TestClient(web.create_app(cfg, tmp_path / "jobs.db", allowed_hosts=("testserver",)), follow_redirects=False)
    assert 'method="post"' in normal.get("/").text and "select.js" in normal.get("/jobs").text
    assert "demo-banner" not in normal.get("/").text


def test_an_unknown_link_or_a_query_string_is_a_build_error():
    with pytest.raises(fd.FreezeError):
        fd.public_target("/jobs?status=applied")
    with pytest.raises(fd.FreezeError):
        fd.public_target("/static/nope.css")
    with pytest.raises(fd.FreezeError):
        fd.rewrite('<a href="javascript:alert(1)">x</a>', [])
    with pytest.raises(fd.FreezeError):
        fd.rewrite('<a href="page.html">x</a>', [])


def test_the_link_mapping_of_the_design():
    assert fd.public_target("/")[1:] == ("/demo/", "demo/index.html")
    assert fd.public_target("/jobs")[1:] == ("/demo/jobs/", "demo/jobs/index.html")
    assert fd.public_target("/jobs?page=1")[1:] == ("/demo/jobs/", "demo/jobs/index.html")
    assert fd.public_target("/jobs?page=3") == ("/jobs?page=3", "/demo/jobs/page/3/", "demo/jobs/page/3/index.html")
    assert fd.public_target("/applied?page=2")[1] == "/demo/applied/page/2/"
    assert fd.public_target("/jobs/12")[1] == "/demo/jobs/12/"
    assert fd.public_target("/jobs/12/documents/cv/pdf")[1:] == ("/demo/jobs/12/documents/cv.pdf", "demo/jobs/12/documents/cv.pdf")
    assert fd.public_target("/jobs/12/documents/cv/approved-pdf")[1] == "/demo/jobs/12/documents/cv-approved.pdf"
    assert fd.public_target("/static/style.css")[1] == "/demo/static/style.css"
    assert fd.public_target("/index.html")[1] == "/"
    out = []
    assert fd.rewrite('<a href="/jobs?page=2">n</a>', out) == '<a href="/demo/jobs/page/2/">n</a>' and out


def test_a_non_empty_output_folder_needs_the_marker_or_force(tmp_path):
    out = tmp_path / "site"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(fd.FreezeError):
        fd.prepare_out(out, False)
    assert (out / "keep.txt").exists()
    fd.prepare_out(out, True)
    assert not (out / "keep.txt").exists() and (out / fd.MARKER).is_file()
    (out / "again.txt").write_text("x", encoding="utf-8")
    fd.prepare_out(out, False)                                    # made by the script: rebuilt without --force
    with pytest.raises(fd.FreezeError):
        fd.prepare_out(ROOT, True)


def test_the_headers_file_has_the_policy_and_no_unsafe_directive():
    headers = (ROOT / "site" / "_headers").read_text(encoding="utf-8")
    assert "unsafe-" not in headers
    for needed in ("default-src 'self'", "script-src 'self'", "form-action 'none'", "frame-ancestors 'none'", "X-Content-Type-Options: nosniff",
                   "X-Robots-Tag: noindex"):
        assert needed in headers
    assert "/demo/*" in headers


def test_the_landing_page_has_the_links_and_nothing_external_loads():
    index = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    for link in ("https://github.com/Pszczelowski", "https://www.linkedin.com/in/pszczelowski/", 'href="/demo/"',
                 "https://github.com/Pszczelowski/job-match-pipeline"):
        assert link in index
    assert "<script" not in index and "<form" not in index
    for name in ("index.html", "404.html"):
        page = (ROOT / "site" / name).read_text(encoding="utf-8")
        assert 'rel="stylesheet" href="/style.css"' in page and not re.search(r'<(?:link|img|script|iframe)[^>]*(?:src|href)="(?:https?:)?//', page)
    assert "prefers-color-scheme: dark" in (ROOT / "site" / "style.css").read_text(encoding="utf-8")


def test_the_landing_page_may_carry_the_name_but_the_demo_may_not(frozen, tmp_path):
    out, _ = frozen
    local = tmp_path / "local.yaml"
    local.write_text("denylist: [Pszczelowski]\n", encoding="utf-8")
    page = out / "demo" / "index.html"  # same basename as the landing page: must still be scanned
    original = page.read_bytes()
    page.write_bytes(original + b"<p>Pszczelowski</p>\n")
    try:
        findings, problem = fd.scan(out, local_path=local)
    finally:
        page.write_bytes(original)
    assert problem == ""
    assert {f.path for f in findings} == {"demo/index.html"}, [str(f) for f in findings]
