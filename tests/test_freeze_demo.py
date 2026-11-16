"""The static demo (goal.md 7.4, v0.100.0): scripts/freeze_demo.py, site/ and the demo mode of the web app.

Every build runs on a fictional database in a temporary folder; nothing here touches data/, the real config or the network.
"""
import re
import shutil
import smtplib
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_public_copy as bpc                      # noqa: E402
import freeze_demo as fd                              # noqa: E402
import seed_demo_db                                   # noqa: E402
from app import web                                   # noqa: E402
from app.config import load_config                    # noqa: E402

LINK = re.compile(r'\b(?:href|src|action)="([^"]*)"')


def make_landing(root, extra=None):
    """A fake exported landing tree (what `next build` writes to web/out): pages, a font, the generated images and a PDF."""
    root.mkdir(parents=True, exist_ok=True)
    page = ('<!DOCTYPE html><html lang="en"><head><title>Landing</title><script src="/theme-init.js"></script></head><body>'
            '<a href="/demo/">demo</a><script src="/_next/static/chunks/app.js" defer=""></script>'
            '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script></body></html>\n')
    (root / "index.html").write_text(page, encoding="utf-8")
    (root / "404.html").write_text(page.replace("Landing", "Not found"), encoding="utf-8")
    (root / "theme-init.js").write_text("document.documentElement.classList.add('x');\n", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>\n", encoding="utf-8")
    (root / "_next" / "static" / "chunks").mkdir(parents=True)
    (root / "_next" / "static" / "chunks" / "app.js").write_text("console.log(1);\n", encoding="utf-8")
    (root / "_next" / "static" / "media").mkdir(parents=True)
    (root / "_next" / "static" / "media" / "font.woff2").write_bytes(b"wOF2\x00\x01\x00\x00fake-font")
    (root / "generated").mkdir()
    Image.new("RGBA", (8, 10), (27, 27, 27, 255)).save(root / "generated" / "portrait.webp", "WEBP", lossless=True)
    Image.new("RGB", (12, 6), (245, 245, 245)).save(root / "generated" / "og.png", "PNG")
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with (root / "generated" / "cv.pdf").open("wb") as fh:
        writer.write(fh)
    for name, data in (extra or {}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def build_into(path, landing=None):
    out = fd.prepare_out(path, False)
    return out, fd.build(out, landing=landing)


@pytest.fixture(scope="module")
def landing(tmp_path_factory):
    return make_landing(tmp_path_factory.mktemp("landing") / "out")


@pytest.fixture(scope="module")
def frozen(tmp_path_factory, landing):
    out, counts = build_into(tmp_path_factory.mktemp("site") / "_site", landing)
    return out, counts


def pages(out):
    return sorted((out / "demo").rglob("*.html"))


def text(p):
    return p.read_text(encoding="utf-8")


def test_the_freeze_writes_the_landing_page_the_demo_and_the_headers(frozen):
    out, counts = frozen
    for name in ("index.html", "404.html", "generated/cv.pdf", "_next/static/media/font.woff2", "_headers", "demo/index.html", "demo/404.html", "demo/jobs/index.html",
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
    out, _ = build_into(tmp_path / "_site")                      # no landing tree needed here
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


def test_the_scan_of_the_frozen_tree_finds_nothing(frozen, landing):
    out, _ = frozen
    findings, problem = fd.scan(out, denylist=False, landing=landing, contacts=CONTACTS)
    assert problem == "" and findings == [], [str(f) for f in findings]


def test_the_scan_asks_for_the_denylist_unless_told_not_to(frozen, landing, tmp_path):
    out, _ = frozen
    assert fd.scan(out, local_path=tmp_path / "missing.yaml", landing=landing)[1]
    local = tmp_path / "local.yaml"
    local.write_text("denylist: [Contoso]\n", encoding="utf-8")
    findings, problem = fd.scan(out, local_path=local, landing=landing)
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


def header_blocks():
    """`site/_headers` as {path pattern: [header lines]}."""
    blocks, current = {}, None
    for line in (ROOT / "site" / "_headers").read_text(encoding="utf-8").splitlines():
        if line.strip():
            if not line.startswith(" "):
                current = blocks.setdefault(line.strip(), [])
            else:
                current.append(line.strip())
    return blocks


def test_the_headers_file_has_the_policy_and_no_unsafe_directive():
    blocks = header_blocks()
    everywhere = " | ".join(blocks["/*"])
    assert "unsafe-" not in everywhere.replace("style-src-attr 'unsafe-inline'", "")      # only inline style attributes (framer-motion)
    assert "style-src 'self'; style-src-attr 'unsafe-inline'" in everywhere and "script-src 'self';" in everywhere
    for needed in ("default-src 'self'", "script-src 'self'", "form-action 'none'", "frame-ancestors 'none'", "object-src 'none'",
                   "img-src 'self' data:", "font-src 'self'", "base-uri 'none'", "X-Content-Type-Options: nosniff"):
        assert needed in everywhere
    demo = blocks["/demo/*"]
    assert "X-Robots-Tag: noindex" in demo
    assert "! Content-Security-Policy" in demo and demo.index("! Content-Security-Policy") < demo.index(next(h for h in demo if h.startswith("Content-Security-Policy")))
    strict = "Content-Security-Policy: default-src 'self'; script-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"
    assert strict in demo and "unsafe-" not in " ".join(demo) and "style-src" not in " ".join(demo)


def test_the_landing_tree_is_copied_to_the_root_and_site_headers_come_last(tmp_path, landing):
    clashing = make_landing(tmp_path / "out", {"_headers": b"/*\n  X-Test: landing\n"})
    out, _ = build_into(tmp_path / "_site", clashing)
    assert (out / "index.html").read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert (out / "generated" / "cv.pdf").is_file() and (out / "_next" / "static" / "media" / "font.woff2").is_file()
    assert (out / "_headers").read_bytes() == (ROOT / "site" / "_headers").read_bytes()
    assert not (ROOT / "site" / "index.html").exists() and not (ROOT / "site" / "style.css").exists() and not (ROOT / "site" / "404.html").exists()


CONTACTS = {"email": "planted.person@example.com", "phone": "+41 79 111 22 33", "birthdate": "1990-01-02",
            "address": "Testweg 99, 8000 Teststadt"}


def scan_with(out, landing, planted, contacts=CONTACTS, local=None):
    """Plants {path: bytes} in `out`, scans, restores; returns the findings."""
    originals = {path: (out / path).read_bytes() if (out / path).exists() else None for path in planted}
    try:
        for path, data in planted.items():
            (out / path).parent.mkdir(parents=True, exist_ok=True)
            (out / path).write_bytes(data)
        findings, problem = fd.scan(out, local_path=local or Path("missing.yaml"), denylist=bool(local), landing=landing, contacts=contacts)
        assert problem == ""
        return findings
    finally:
        for path, data in originals.items():
            if data is None:
                (out / path).unlink()
            else:
                (out / path).write_bytes(data)


def test_landing_binaries_allowed_only_at_landing_paths(frozen, landing, tmp_path):
    out, _ = frozen
    assert scan_with(out, landing, {}) == []                                              # the fake landing tree is clean
    image = (landing / "generated" / "portrait.webp").read_bytes()
    elsewhere = scan_with(out, landing, {"demo/generated/portrait.webp": image, "extra/font.woff2": b"wOF2\x00fake",
                                         "generated/other.bin": b"\x00\x01binary"})
    assert {(f.path, f.rule) for f in elsewhere} == {("demo/generated/portrait.webp", "binary"), ("extra/font.woff2", "binary"),
                                                      ("generated/other.bin", "binary")}   # not in the landing tree: not allowed
    odd = make_landing(tmp_path / "odd", {"notes.bin": b"\x00\x01binary", "generated/photo.jpg": b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00"})
    out2, _ = build_into(tmp_path / "_site2", odd)
    findings, _ = fd.scan(out2, denylist=False, landing=odd)
    assert {(f.path, f.rule) for f in findings} == {("notes.bin", "binary"), ("generated/photo.jpg", "binary")}      # a .jpg is not on the landing allowlist
    # a landing image that carries metadata is a finding: the EXIF check runs on the allowed files
    with_exif = scan_with(out, landing, {"generated/portrait.webp": image + b"Exif\x00\x00"})
    assert [(f.path, f.rule) for f in with_exif] == [("generated/portrait.webp", "exif")]


def test_landing_html_has_no_inline_script_or_style(frozen, landing):
    out, _ = frozen
    page = (landing / "index.html").read_text(encoding="utf-8")
    assert scan_with(out, landing, {}) == []                                              # src scripts and JSON data are fine
    for rule, planted in (("inline-script", page.replace("</body>", "<script>alert(1)</script></body>")),
                          ("inline-style", page.replace("</head>", "<style>body{color:red}</style></head>")),
                          ("inline-handler", page.replace("<a href", '<a onclick="x()" href'))):
        found = scan_with(out, landing, {"index.html": planted.encode("utf-8")})
        assert [(f.path, f.rule) for f in found] == [("index.html", rule)], rule
    found = scan_with(out, landing, {"demo/index.html": b"<script>1</script>"})            # a demo page is not landing HTML
    assert not [f for f in found if f.rule.startswith("inline")]


def test_contacts_checked_despite_landing_exemption(frozen, landing, tmp_path):
    out, _ = frozen
    local = tmp_path / "local.yaml"
    local.write_text("denylist: [Zxqvmarker]\n", encoding="utf-8")
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Subject": "Testweg 99 8000 Teststadt"})
    pdf = tmp_path / "meta.pdf"
    with pdf.open("wb") as fh:
        writer.write(fh)
    page = (landing / "index.html").read_text(encoding="utf-8")
    planted = {"index.html": (page + "<p>Zxqvmarker, planted.person@example.com</p>").encode("utf-8"),        # exempt from the denylist, not from this
               "_next/static/chunks/app.js": b"var p='+41 79 111 22 33';",
               "generated/cv.pdf": pdf.read_bytes(),
               "demo/jobs/index.html": b"born 02.01.1990"}
    found = scan_with(out, landing, planted, local=local)
    contact = {(f.path, f.masked) for f in found if f.rule == "contact"}
    assert contact == {("index.html", "the real email in text"), ("_next/static/chunks/app.js", "the real phone in text"),
                       ("generated/cv.pdf", "the real address in pdf metadata"), ("demo/jobs/index.html", "the real birthdate in text")}
    assert not [f for f in found if f.path in ("index.html", "_next/static/chunks/app.js", "generated/cv.pdf") and f.rule.startswith("denylist")]
    assert [f.path for f in found if f.rule.startswith("denylist")] == []                    # (Zxqvmarker only in the exempt landing page)
    assert "planted" not in " ".join(str(f) for f in found)                                  # findings never print the value


def test_the_landing_page_may_carry_the_name_but_the_demo_may_not(frozen, landing, tmp_path):
    out, _ = frozen
    local = tmp_path / "local.yaml"
    local.write_text("denylist: [Pszczelowski]\n", encoding="utf-8")
    page = (landing / "index.html").read_bytes()
    findings = scan_with(out, landing, {"index.html": page + b"<p>Pszczelowski</p>\n", "demo/index.html": b"<p>Pszczelowski</p>\n"}, local=local)
    assert {f.path for f in findings} == {"demo/index.html"}, [str(f) for f in findings]      # same basename as the landing page
