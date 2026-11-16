"""The public portfolio copy (goal.md 7.4, v0.95.0): scripts/build_public_copy.py and scripts/seed_demo_db.py.

Planted values are put together at run time, so this file itself holds no email address, phone number, address or token.
Temporary folders only; the real build test needs the untracked scripts/public_copy.local.yaml and skips without it.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_public_copy as bpc                      # noqa: E402
import seed_demo_db                                   # noqa: E402

RULES = {
    "include": ["app/", "tests/", "README.md", "*.example*", ".gitignore"],
    "exclude": [
        {"glob": "config.yaml", "reason": "real config"},
        {"glob": ".env*", "except": ["*.example"], "reason": "secrets"},
        {"glob": "*.db*", "reason": "databases"},
        {"glob": "data/", "reason": "runtime data"},
        {"glob": "profile/", "reason": "real CV"},
        {"glob": "tests/test_private.py", "reason": "reads a private file"},
    ],
    "binary_allow": ["app/fonts/*.ttf", "profile/photo.jpg"],
    "replace_skip_paths": ["README.md", "LICENSE"],
    "denylist_exempt_paths": ["README.md", "LICENSE"],
    "persona": {"source": "examples/persona/", "target": "profile/"},
    "private_block": {"start": "<!-- private:start -->", "end": "<!-- private:end -->"},
    "replacements": [],
    "license": "MIT License\n",
    "email_allow": ["jobmail@jobs.ch"],
    "email_domain_allow": ["example.com", "example.org", "example.net", "example"],
    "ip_allow": ["10.0.0.5"],
    "scan_skip_patterns": ["app/static/vendor/*"],
    "placeholder_words": ["made-up"],
}


def scan(text, deny=None, path="app/x.txt", rules=RULES):
    return bpc.scan_tree({path: text.encode("utf-8")}, rules, {"denylist": deny or []})


def rules_of(findings):
    return sorted({f.rule for f in findings})


# --- the scan ---------------------------------------------------------------------------------------------------

def test_a_planted_email_address_fails_the_scan():
    found = scan("write to " + "some.one" + "@" + "mail-provider.com" + " today")
    assert rules_of(found) == ["email"] and found[0].line == 1 and found[0].path == "app/x.txt"


@pytest.mark.parametrize("address", ["me@example.com", "x@sub.example.org", "a@b.example", "noreply@anthropic.com", "jobmail@jobs.ch"])
def test_example_domains_and_allowed_senders_pass(address):
    assert scan(f"mail {address} now") == []


@pytest.mark.parametrize("phone", ["+41 76 " + "123 45 78", "+48 600 " + "700 800", "+4176" + "1234578"])
def test_a_planted_phone_number_fails_the_scan(phone):
    assert rules_of(scan(f"call {phone}.")) == ["phone"]


def test_a_swiss_national_phone_number_fails_and_a_zero_number_passes():
    assert rules_of(scan("tel " + "044 " + "292 28 89")) == ["phone"]
    assert scan("tel +41 76 000 00 00 and 044 000 00 00") == []


@pytest.mark.parametrize("ip", ["192.168." + "7.20", "10." + "1.2.3", "8.8." + "8.8"])
def test_a_planted_ip_address_fails_the_scan(ip):
    assert rules_of(scan(f"host {ip}")) == ["ipv4"]


@pytest.mark.parametrize("ip", ["127.0.0.1", "0.0.0.0", "198.51.100.7", "203.0.113.9", "192.0.2.3", "10.0.0.5", "999.1.1.1"])
def test_loopback_documentation_allowed_and_non_address_values_pass(ip):
    assert scan(f"host {ip}") == []


def test_planted_secret_shapes_fail_the_scan():
    values = {"token-github": "gh" + "p_" + "A" * 36, "token-github-pat": "github" + "_pat_" + "B" * 30,
              "token-anthropic": "sk-" + "ant-" + "api03-" + "Zz9" * 6, "token-aws": "AK" + "IA" + "ABCDEFGH12345678",
              "private-key": "-----BEGIN " + "RSA PRIVATE KEY-----", "app-password": "GMAIL password: " + "abcd efgh ijkl mnop".replace("a", "q")}
    for rule, value in values.items():
        assert rule in rules_of(scan(f"x = {value}")), rule


def test_a_made_up_token_of_a_test_is_a_placeholder_not_a_finding():
    assert scan("key = " + "sk-" + "ant-" + "made-up-value") == []


def test_four_short_words_in_prose_are_not_an_app_password():
    assert scan("this will also help some more") == []


def test_a_denylist_value_with_diacritics_is_found_in_any_spelling():
    deny = ["Żółć Łódź"]
    for text in ("the ŻÓŁĆ ŁÓDŹ office", "zolc lodz", "ZOLC LÓDZ", "Zolc Lodz"):
        assert rules_of(scan(text, deny)) == ["denylist"], text
    assert scan("nothing here", deny) == []


def test_the_denylist_also_checks_file_names_but_not_the_exempt_paths():
    deny = ["Rare Name"]
    assert rules_of(scan("x", deny, path="app/Rare Name.txt")) == ["denylist-path"]
    assert scan("Rare Name is the author", deny, path="README.md") == []


def test_a_denylist_entry_can_be_exempt_in_named_paths_only():
    deny = [{"value": "Rare Town", "exempt_paths": ["tests/fixtures/travel/*"]}]
    assert scan("in rare town", deny, path="tests/fixtures/travel/a.json") == []
    assert rules_of(scan("in rare town", deny, path="tests/test_a.py")) == ["denylist"]


def test_a_binary_must_be_allowlisted_and_an_image_must_have_no_exif():
    blob = b"\x00\x01\x02binary"
    assert [f.rule for f in bpc.scan_tree({"app/x.bin": blob}, RULES, {})] == ["binary"]
    assert bpc.scan_tree({"app/fonts/a.ttf": blob}, RULES, {}) == []
    clean = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x00\x01"
    assert bpc.scan_tree({"profile/photo.jpg": clean}, RULES, {}) == []
    with_exif = b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00\x00\x00"
    assert [f.rule for f in bpc.scan_tree({"profile/photo.jpg": with_exif}, RULES, {})] == ["exif"]


def test_a_finding_prints_path_line_rule_and_hides_the_value():
    value = "some.one" + "@" + "mail-provider.com"
    text = str(scan("ok\n" + value)[0])
    assert text.startswith("app/x.txt:2:email") and "some.one" not in text and "mail-provider" not in text and "hash=" in text


def test_scan_allow_silences_one_line_by_path_and_hash():
    line = "contact " + "some.one" + "@" + "mail-provider.com"
    finding = scan(line)[0]
    allowed = dict(RULES, scan_allow=[{"path": "app/x.txt", "line_hash": finding.digest, "rule": "email"}])
    assert scan(line, rules=allowed) == []
    assert scan(line.replace("contact", "other"), rules=allowed) != []


def test_third_party_files_are_not_pattern_scanned_but_still_denylist_scanned():
    text = "var a='" + "10." + "1.2.3';"
    assert scan(text, path="app/static/vendor/lib.js") == []
    assert rules_of(scan(text + " secretword", ["secretword"], path="app/static/vendor/lib.js")) == ["denylist"]


# --- the plan: what is copied --------------------------------------------------------------------------------------

def test_excluded_paths_are_never_copied_and_the_persona_takes_the_place_of_profile():
    files = {
        "app/main.py": b"print(1)\n", "tests/test_a.py": b"x = 1\n", "tests/test_private.py": b"y = 2\n",
        "config.yaml": b"real: 1\n", ".env": b"X=1\n", ".env.example": b"X=\n", "config.example.yaml": b"a: 1\n",
        "data/jobs.db": b"db", "jobs.db": b"db", "app/jobs.db-wal": b"db", "profile/master_cv.yaml": b"real: cv\n",
        "goal.md": b"private\n", "examples/persona/master_cv.yaml": b"fictional: cv\n", "README.md": b"hi\n",
    }
    out = bpc.plan(files, RULES, {})
    assert sorted(out) == [".env.example", "LICENSE", "README.md", "app/main.py", "config.example.yaml", "profile/master_cv.yaml",
                           "tests/test_a.py"]
    assert out["profile/master_cv.yaml"] == b"fictional: cv\n"


def test_every_exclude_of_the_real_rules_has_a_reason():
    rules = bpc.load_yaml(bpc.RULES)
    assert rules["exclude"] and all(r.get("reason") for r in rules["exclude"])
    assert bpc.excluded_by(rules, "config.yaml") and bpc.excluded_by(rules, ".env") and bpc.excluded_by(rules, "data/jobs.db")
    assert bpc.excluded_by(rules, ".env.example") is None and bpc.excluded_by(rules, "goal.md")
    for private in ("CHANGELOG.md", "CLAUDE.md", "session.md", "DEPLOYMENT_MANUAL.md", "reports/phase-1-report.md", "profile/master_cv.yaml",
                    "exchange/results/x.json", "output/a.pdf", "backups/x", "logs/x.log", ".github/workflows/release.yml"):
        assert bpc.excluded_by(rules, private), private


def test_private_blocks_are_stripped_and_replacements_apply_except_in_skipped_paths():
    rules = dict(RULES, replacements=[{"find": "Secret Corp", "replace": "Contoso"}])
    text = b"keep\n<!-- private:start -->\nthe private part\n<!-- private:end -->\nSecret Corp stays out\n"
    out = bpc.plan({"app/a.md": text, "README.md": text}, rules, {})
    assert out["app/a.md"] == b"keep\nContoso stays out\n"
    assert b"private part" not in out["README.md"]


def test_a_replacement_can_skip_paths_and_files_that_quote_the_markers_keep_their_blocks():
    local = {"replacements": [{"find": "Town", "replace": "Exampletown", "skip_paths": ["tests/fixtures/*"]}]}
    out = bpc.plan({"app/a.py": b"Town\n", "tests/fixtures/t.json": b"Town\n"}, RULES, local)
    assert out["app/a.py"] == b"Exampletown\n" and out["tests/fixtures/t.json"] == b"Town\n"
    block = b"a <!-- private:start --> b <!-- private:end --> c\n"
    rules = dict(RULES, private_block=dict(RULES["private_block"], skip_paths=["app/quote.py"]))
    out = bpc.plan({"app/quote.py": block, "app/other.py": block}, rules, {})
    assert out["app/quote.py"] == block and out["app/other.py"] == b"ac\n"


def test_local_replacements_keep_their_format_and_can_be_limited_to_paths():
    local = {"replacements": [{"find": "real.person@mail.test", "replace": "alex.morgan@example.com"},
                              {"find": r"\bBank\b", "replace": "Contoso", "regex": True, "paths": ["tests/test_a.py"]}]}
    files = {"app/a.py": b"real.person@mail.test Bank\n", "tests/test_a.py": b"Bank\n"}
    out = bpc.plan(files, RULES, local)
    assert out["app/a.py"] == b"alex.morgan@example.com Bank\n" and out["tests/test_a.py"] == b"Contoso\n"


def test_the_license_is_added_and_the_build_result_has_no_crlf_from_the_tree():
    out = bpc.plan({"app/a.py": b"x\n"}, RULES, {})
    assert out["LICENSE"].startswith(b"MIT License")


# --- the output folder and the git source -----------------------------------------------------------------------------

def test_an_output_folder_inside_the_repository_is_refused(tmp_path):
    for bad in (tmp_path, tmp_path / "copy", tmp_path / "a" / "b"):
        with pytest.raises(bpc.Refused):
            bpc.check_out(bad, force=True, root=tmp_path)
    with pytest.raises(bpc.Refused):
        bpc.check_out(tmp_path.parent, force=True, root=tmp_path)             # a folder that contains the repository


def test_a_non_empty_output_folder_needs_force_and_must_be_an_earlier_copy(tmp_path):
    root, out = tmp_path / "repo", tmp_path / "out"
    root.mkdir()
    out.mkdir()
    assert bpc.check_out(out, False, root) == out.resolve()                 # empty: fine
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(bpc.Refused, match="not empty"):
        bpc.check_out(out, False, root)
    with pytest.raises(bpc.Refused, match="not made by this script"):
        bpc.check_out(out, True, root)
    (out / bpc.MARKER).write_text("x", encoding="utf-8")
    assert bpc.check_out(out, True, root) == out.resolve()


def test_write_tree_replaces_an_old_copy_and_keeps_its_git_folder(tmp_path):
    out = tmp_path / "out"
    (out / ".git").mkdir(parents=True)
    (out / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (out / "old.txt").write_text("old", encoding="utf-8")
    bpc.write_tree({"app/new.txt": b"new"}, out)
    assert not (out / "old.txt").exists() and (out / "app" / "new.txt").read_bytes() == b"new"
    assert (out / ".git" / "HEAD").exists() and (out / bpc.MARKER).exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_only_tracked_content_of_head_is_read_and_a_dirty_tree_is_detected(tmp_path):
    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text("committed\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("tracked but excluded\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    (tmp_path / "app" / "untracked.py").write_text("never leaks\n", encoding="utf-8")
    assert not bpc.is_dirty(tmp_path)
    files = bpc.read_sources(RULES, False, tmp_path)
    assert files == {"app/a.py": b"committed\n"}
    (tmp_path / "app" / "a.py").write_text("changed\n", encoding="utf-8")
    assert bpc.is_dirty(tmp_path)
    assert bpc.read_sources(RULES, False, tmp_path)["app/a.py"] == b"committed\n"       # HEAD, not the working tree
    with pytest.raises(bpc.Refused, match="changes to tracked files"):
        bpc.build(tmp_path.parent / "never", root=tmp_path, rules_path=bpc.RULES, local_path=tmp_path / "none.yaml", say=lambda *_: None)


def test_a_scan_finding_stops_the_build_and_writes_nothing(tmp_path, monkeypatch):
    bad = {"app/a.py": ("mail " + "some.one" + "@" + "mail-provider.com\n").encode()}
    monkeypatch.setattr(bpc, "read_sources", lambda *a, **k: bad)
    out = tmp_path / "out"
    lines = []
    code = bpc.build(out, allow_dirty=True, root=tmp_path / "repo", rules_path=bpc.RULES, local_path=tmp_path / "none.yaml", say=lines.append)
    assert code == 1 and not out.exists()
    assert any(line.startswith("app/a.py:1:email") for line in lines) and "some.one" not in "\n".join(lines)


# --- the demo database ---------------------------------------------------------------------------------------------

def test_the_seed_script_creates_the_requested_number_of_fictional_jobs(tmp_path):
    counts = seed_demo_db.seed(tmp_path / "demo" / "jobs.db", jobs=20)
    assert counts["total"] == 20 and counts["archived"] >= 1 and counts["scores"] >= 15
    from app import db
    conn = db.connect(tmp_path / "demo" / "jobs.db")
    try:
        statuses = {r["status"] for r in conn.execute("SELECT status FROM jobs")}
        assert {"scored", "applied", "interview"} <= statuses
        assert all(r["company"] and r["url"].startswith("https://jobs.example.com/")
                   for r in conn.execute("SELECT company, canonical_url AS url FROM jobs"))
        assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 2
    finally:
        conn.close()


def test_the_seed_script_is_deterministic_and_refuses_to_overwrite_or_to_write_into_data(tmp_path):
    a = seed_demo_db.seed(tmp_path / "a" / "jobs.db", jobs=14)
    b = seed_demo_db.seed(tmp_path / "b" / "jobs.db", jobs=14)
    assert {k: a[k] for k in ("jobs", "archived", "scores")} == {k: b[k] for k in ("jobs", "archived", "scores")}
    with pytest.raises(SystemExit, match="exists"):
        seed_demo_db.seed(tmp_path / "a" / "jobs.db", jobs=14)
    assert seed_demo_db.seed(tmp_path / "a" / "jobs.db", jobs=14, force=True)["total"] == 14
    with pytest.raises(SystemExit, match="data/"):
        seed_demo_db.seed(tmp_path / "repo" / "data" / "jobs.db", jobs=14, root=tmp_path / "repo")


# --- the real build (needs the owner's local file) --------------------------------------------------------------------

@pytest.mark.skipif(not bpc.LOCAL.is_file() or not (ROOT / ".git").exists(),
                    reason="scripts/public_copy.local.yaml (the real values) or the git repository is absent")
def test_the_real_build_finds_nothing_and_leaves_the_private_files_out(tmp_path):
    out = tmp_path / "copy"
    lines = []
    code = bpc.build(out, allow_dirty=True, say=lines.append)
    assert code == 0, "\n".join(lines[:20])
    names = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    for private in ("config.yaml", "goal.md", "CHANGELOG.md", "CLAUDE.md", "compose.env", ".env", "session.md"):
        assert private not in names
    assert not any(n.startswith(("data/", "exchange/", "output/", "backups/", "logs/", "reports/", "profile/samples/")) for n in names)
    assert {"LICENSE", "profile/master_cv.yaml", "profile/photo.jpg", "scripts/build_public_copy.py", "app/web.py"} <= names


def _seeded(tmp_path, name="demo", jobs=24):
    from app import db
    seed_demo_db.seed(tmp_path / name / "jobs.db", jobs=jobs)
    return db.connect(tmp_path / name / "jobs.db")


def test_the_seed_shows_every_stage_after_the_application(tmp_path):
    conn = _seeded(tmp_path)
    try:
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        for status in ("shortlisted", "applied", "interview", "offer", "rejected", "withdrawn"):
            assert counts.get(status, 0) >= 1, status
        assert counts["shortlisted"] == 2 and counts["applied"] == 1                      # two shortlisted, one plain applied
    finally:
        conn.close()


def test_every_history_row_of_the_seed_is_a_step_statusflow_allows(tmp_path):
    from app import statusflow
    conn = _seeded(tmp_path)
    try:
        rows = conn.execute("SELECT job_id, from_status, to_status, note FROM status_history WHERE note = 'demo' ORDER BY id").fetchall()
        assert rows
        by_job = {}
        for r in rows:
            by_job.setdefault(r["job_id"], []).append(r)
            assert statusflow.can(statusflow.ACTION_FOR_STATUS[r["to_status"]], r["from_status"], True), tuple(r)
        for steps in by_job.values():                                                    # each job's rows form one chain starting at a scored job
            assert steps[0]["from_status"] in ("scored", "not_selected")
            assert all(a["to_status"] == b["from_status"] for a, b in zip(steps, steps[1:]))
        reached = {r["to_status"] for r in rows}
        assert {"shortlisted", "applied", "interview", "offer", "rejected", "withdrawn"} <= reached
    finally:
        conn.close()


def test_the_seed_dates_are_fixed_spread_and_not_today(tmp_path):
    from datetime import datetime, timezone
    conn = _seeded(tmp_path)
    try:
        stamps = [r[0] for r in conn.execute("SELECT changed_at FROM status_history WHERE note = 'demo' ORDER BY id")]
        days = {s[:10] for s in stamps}
        assert len(days) >= 4 and len(set(stamps)) > len(stamps) // 2                       # spread over several days and hours
        assert datetime.now(timezone.utc).strftime("%Y-%m-%d") not in days
        assert min(days) >= "2026-11-10" and max(days) <= "2026-11-14"
        for job_id, in conn.execute("SELECT DISTINCT job_id FROM status_history WHERE note = 'demo'").fetchall():
            seq = [r[0] for r in conn.execute("SELECT changed_at FROM status_history WHERE job_id = ? AND note = 'demo' ORDER BY id", (job_id,))]
            assert seq == sorted(seq)                                                         # the steps of a job never go back in time
    finally:
        conn.close()


def test_the_seed_history_is_the_same_on_every_run_and_feeds_the_applications_block(tmp_path):
    from datetime import datetime, timezone
    from app import analytics
    dumps = []
    for name in ("a", "b"):
        conn = _seeded(tmp_path, name)
        try:
            dumps.append([tuple(r) for r in conn.execute("SELECT job_id, from_status, to_status, note, changed_at FROM status_history WHERE note = 'demo' ORDER BY id")])
            block = analytics.applications(conn, datetime(2026, 11, 16, 12, tzinfo=timezone.utc))
        finally:
            conn.close()
    assert dumps[0] == dumps[1]
    assert block["total"] == 8 and block["outcomes"] == {"declined": 1, "offer_withdrawn": 0, "accepted": 1} and block["week"]["total"] > 0
    assert block["timings"]["first_answer"]["n"] >= 4 and block["rates"]["interview"]


def test_the_seed_has_one_accepted_job_with_two_rounds_and_a_note(tmp_path):
    conn = _seeded(tmp_path)
    try:
        accepted = [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE status = 'accepted'")]
        assert len(accepted) == 1
        assert conn.execute("SELECT COUNT(*) FROM interviews WHERE job_id = ?", (accepted[0],)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM job_notes WHERE job_id = ?", (accepted[0],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 2
    finally:
        conn.close()


def test_the_copy_holds_the_freeze_script_and_the_landing_page_with_the_name_unchanged():
    rules = bpc.load_yaml(bpc.RULES)
    files = {path: (ROOT / path).read_bytes() for path in ("scripts/freeze_demo.py", "tests/test_freeze_demo.py", "site/_headers")}
    local = {"replacements": [{"find": "Morgan", "replace": "Doe"}], "denylist": ["Morgan"]}
    out = bpc.plan(files, rules, local)
    assert set(files) <= set(out)
    assert out["site/_headers"] == files["site/_headers"]                          # site/ is copied byte for byte like the README
    assert out["tests/test_freeze_demo.py"] == files["tests/test_freeze_demo.py"]  # it checks those landing links
    assert not [f for f in bpc.scan_tree({p: out[p] for p in files if p.startswith("site/")}, rules, local)]
    assert "Morgan" not in out["scripts/freeze_demo.py"].decode("utf-8")
