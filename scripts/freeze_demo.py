"""Freezes the web app on the fictional demo database into a static, read-only site (goal.md 7.4, v0.100.0).

    python scripts/freeze_demo.py --out _site [--force] [--no-denylist]

The demo database is built by scripts/seed_demo_db.py in a temporary folder (never data/), the app runs in demo mode (no forms, no
buttons, no scripts that write; every POST gets 405) behind a test client, and every page is crawled from the routes without a path
parameter plus every open job. Links are rewritten to the static layout under /demo/ (`/jobs?page=2` becomes `/demo/jobs/page/2/`,
a PDF becomes `.../cv.pdf`). Any page that does not answer 200, any link that has no static place and any query string other than
`page` is a build error. site/ (the landing page, its 404 page and the Cloudflare `_headers`) is copied into the root of --out.
The finished tree is scanned with the rules of the public copy (scripts/public_copy.yaml and the untracked public_copy.local.yaml
with the real denylist): exit 1 on any finding, exit 2 when the denylist is missing and --no-denylist was not given.
"""
from __future__ import annotations

import argparse
import contextlib
import html
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_public_copy as bpc                                       # noqa: E402
import seed_demo_db                                                    # noqa: E402
from app import web                                                    # noqa: E402
from app.config import load_config                                     # noqa: E402

MARKER = ".freeze-demo-build"          # written into --out; a non-empty folder without it is refused unless --force
DEMO = "/demo"                         # where the app lives in the static site
SITE = ROOT / "site"
STATIC = ROOT / "app" / "static"
RULES = ROOT / "scripts" / "public_copy.yaml"
LOCAL = ROOT / "scripts" / "public_copy.local.yaml"
# GET routes without a path parameter that are not frozen, each with its reason (a test checks that every other one is).
SKIP_ROUTES = {
    "/login": "exists only when a web login is on; the demo has none",
    "/bulk-status": "the confirmation page of a selection; it needs a query and belongs to the buttons, which the demo leaves out",
}
PDF_SUFFIX = {"pdf": ".pdf", "approved-pdf": "-approved.pdf"}
ATTR = re.compile(r'\b(href|src|action)="([^"]*)"')
PDF_PATH = re.compile(r"/jobs/([0-9]+)/documents/([a-z_]+)/(pdf|approved-pdf)")
LIST_PAGES = ("/jobs",) + tuple("/" + p for p in web.statusflow.APPLICATION_PAGES)


class FreezeError(Exception):
    pass


def public_target(url: str) -> tuple[str, str, str]:
    """(url to fetch, public link, file under --out) of one link of the app. External links are not passed here."""
    parts = urlsplit(url)
    path, query = parts.path, parse_qsl(parts.query, keep_blank_values=True)
    if path == "/index.html" and not query:
        return "", "/", "index.html"
    if path.startswith("/static/") and not query:
        if not (STATIC / path[len("/static/"):]).is_file():
            raise FreezeError(f"no such static file: {url}")
        return "", DEMO + path, DEMO[1:] + path
    m = PDF_PATH.fullmatch(path)
    if m and not query:
        job, kind, which = m.groups()
        base = f"{DEMO}/jobs/{job}/documents/{kind}{PDF_SUFFIX[which]}"
        return path, base, base[1:]
    fetch = path
    if query:
        if path not in LIST_PAGES or len(query) != 1 or query[0][0] != "page" or not query[0][1].isdigit():
            raise FreezeError(f"a query string other than page=N on a list page: {url}")
        n = int(query[0][1])
        if n > 1:
            public = f"{DEMO}{path}/page/{n}/"
            return f"{path}?page={n}", public, public[1:] + "index.html"
        query = []
    if not path.startswith("/") or path.endswith(".html") or ".." in path:
        raise FreezeError(f"a link with no place in the static site: {url}")
    public = DEMO + "/" if path == "/" else f"{DEMO}{path.rstrip('/')}/"
    return fetch, public, public[1:] + "index.html"


def rewrite(page: str, found: list[tuple[str, str, str]]) -> str:
    """Rewrites href/src/action of the page; every internal link is also appended to `found` as (fetch, public, file)."""
    def one(m: re.Match) -> str:
        attr, value = m.group(1), html.unescape(m.group(2))
        if value.startswith("#") or re.match(r"[a-z][a-z0-9+.-]*:", value, re.I):
            if value.lower().startswith(("javascript:", "data:")):
                raise FreezeError(f"a {value.split(':')[0]} link in the output: {value[:60]}")
            return m.group(0)                                   # an anchor or an external link (https:, mailto:)
        if not value.startswith("/"):
            raise FreezeError(f"a relative link: {value}")
        base, _, fragment = value.partition("#")
        target = public_target(base)
        found.append(target)
        return f'{attr}="{html.escape(target[1] + ("#" + fragment if fragment else ""), quote=True)}"'
    return ATTR.sub(one, page)


def seeds(app, ids: list[int]) -> list[str]:
    """Every GET route without a path parameter that is not on SKIP_ROUTES, then /jobs/<id> for every open job."""
    paths = []
    for route in app.routes:
        if "GET" in getattr(route, "methods", ()) and "{" not in route.path and route.path not in SKIP_ROUTES and route.path not in paths:
            paths.append(route.path)
    return paths + [f"/jobs/{i}" for i in ids]


def build(out: Path, jobs: int = 40) -> dict:
    """Crawls the app and writes the static site into `out` (which exists and is empty apart from the marker). Returns counts."""
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory(prefix="freeze-demo-") as tmp:
        seed_demo_db.seed(Path(tmp) / "jobs.db", jobs)
        cfg = load_config(Path(tmp) / "demo-config.yaml")
        db_path = Path(cfg["paths"]["database"])
        app = web.create_app(cfg, db_path, allowed_hosts=("testserver",), demo=True)
        with contextlib.closing(app.state.open_db()) as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM jobs ORDER BY id")]
        client = TestClient(app, follow_redirects=False)
        queue = [(p, *public_target(p)[1:]) for p in seeds(app, ids)]
        # a page found by the crawl is fetched once, from its first link
        done: dict[str, str] = {}
        pages = pdfs = 0
        while queue:
            fetch, public, file = queue.pop(0)
            if file in done or not fetch:
                continue
            done[file] = public
            response = client.get(fetch)
            if response.status_code != 200:
                raise FreezeError(f"{fetch} answered {response.status_code}")
            target = out / file
            target.parent.mkdir(parents=True, exist_ok=True)
            if file.endswith(".pdf"):
                target.write_bytes(response.content)
                pdfs += 1
                continue
            found: list[tuple[str, str, str]] = []
            target.write_text(rewrite(response.text, found), encoding="utf-8", newline="\n")
            pages += 1
            queue += [t for t in found if t[2] not in done]
        missing = client.get("/jobs/999999999")
        if missing.status_code != 404:
            raise FreezeError(f"the not-found page answered {missing.status_code}")
        (out / DEMO[1:] / "404.html").write_text(rewrite(missing.text, []), encoding="utf-8", newline="\n")
        client.close()
    shutil.copytree(STATIC, out / DEMO[1:] / "static", dirs_exist_ok=True)
    shutil.copytree(SITE, out, dirs_exist_ok=True)
    return {"pages": pages, "pdfs": pdfs, "jobs": len(ids)}


def prepare_out(out: Path, force: bool) -> Path:
    out = out.resolve()
    if out == ROOT or out in ROOT.parents:
        raise FreezeError(f"--out must not be the repository or a folder that holds it: {out}")
    if out.exists() and not out.is_dir():
        raise FreezeError(f"--out {out} is a file")
    if out.exists() and any(out.iterdir()) and not (out / MARKER).exists() and not force:
        raise FreezeError(f"--out {out} is not empty and was not made by this script (no {MARKER} file); use --force to replace it")
    if out.exists():
        for child in out.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("made by scripts/freeze_demo.py\n", encoding="utf-8")
    return out


def scan(out: Path, rules_path: Path = RULES, local_path: Path = LOCAL, denylist: bool = True) -> tuple[list, str]:
    """(findings, problem). `problem` is not empty when the real denylist is required and missing."""
    rules = bpc.load_yaml(rules_path)
    local = bpc.load_yaml(local_path) if local_path.is_file() else {}
    if denylist and not local.get("denylist"):
        return [], f"{local_path.name} is missing or has no denylist; create it (see public_copy.local.example.yaml) or pass --no-denylist"
    if not denylist:
        local = {}
    # site/* is exempt from the denylist in the repo rules (the landing page may carry the real name, goal.md 7.4);
    # in --out those files sit at the root, so drop denylist findings of exactly those paths (an exact comparison:
    # a slash-less glob would match every demo index.html by basename), never of the demo pages.
    landing = {p.relative_to(SITE).as_posix() for p in SITE.rglob("*") if p.is_file()}
    tree = {p.relative_to(out).as_posix(): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file() and p.name != MARKER}
    findings = [f for f in bpc.scan_tree(tree, rules, local) if not (f.path in landing and f.rule.startswith("denylist"))]
    return findings, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Freeze the demo of the web app into a static site.")
    ap.add_argument("--out", required=True, type=Path, help="output folder (for example _site)")
    ap.add_argument("--force", action="store_true", help="replace a non-empty folder that this script did not make")
    ap.add_argument("--no-denylist", action="store_true", help="scan without the real denylist (the pattern rules still run)")
    args = ap.parse_args(argv)
    try:
        out = prepare_out(args.out, args.force)
        counts = build(out)
    except FreezeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    findings, problem = scan(out, denylist=not args.no_denylist)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    for f in findings:
        print(f"finding: {f}", file=sys.stderr)
    if findings:
        print(f"{len(findings)} finding(s) in {out}; nothing may be published", file=sys.stderr)
        return 1
    print(f"frozen: {counts['pages']} pages, {counts['pdfs']} PDFs, {counts['jobs']} jobs, written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
