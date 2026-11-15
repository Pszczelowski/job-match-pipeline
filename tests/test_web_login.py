"""The web login (v0.74.0, goal.md 5.7, 5.9a; T-WEB-72 to T-WEB-87 in docs/TEST_CASES.md).

All offline: the passwords and hashes are made up, the scrypt cost is lowered to 2^15 in most tests (the minimum the parser
accepts), the clock is a fake one, and every database is a temporary one.
"""
import asyncio
import getpass
import logging
import os
import re

import httpx2
import pytest
from fastapi.testclient import TestClient

from app import cli, weblogin, web
from app.config import ConfigError, load_config
from tests.test_cli import clean_logger, config_file          # noqa: F401  (fixtures)
from tests.test_report import job, pipeline
from tests.test_web import HOST, conn, cfg                     # noqa: F401  (fixtures)

PASSWORD = "correct horse battery"
USER = "alex"
NAS = "100.64.1.2"


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(scope="module")
def pwhash():
    return weblogin.hash_password(PASSWORD, n_log2=15)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def login(pwhash, clock):
    return weblogin.WebLogin(USER, pwhash, stay_days=30, clock=clock)


@pytest.fixture
def site(cfg, conn, tmp_path, login):
    ids, _ = pipeline(conn, cfg, [job(1), job(2)], [55, 90])
    c = TestClient(web.create_app(cfg, tmp_path / "w.db", login=login), base_url=HOST, follow_redirects=False)
    return c, ids


def post_login(c, user=USER, password=PASSWORD, stay=False, nxt="/", token=True, **headers):
    data = {"username": user, "password": password, "next": nxt}
    if stay:
        data["stay"] = "on"
    if token:
        data["csrf"] = c.app.state.csrf("login")
    return c.post("/login", data=data, headers=headers)


def set_cookie(r):
    return r.headers.get("set-cookie", "")


# --- T-WEB-72 the hash ---------------------------------------------------------------------------------

def test_hash_round_trip_wrong_password_and_format(pwhash):
    assert weblogin.verify_password(PASSWORD, pwhash) is True
    assert weblogin.verify_password(PASSWORD + "x", pwhash) is False
    assert weblogin.verify_password("", pwhash) is False
    assert "$" not in pwhash and re.fullmatch(r"scrypt:15:8:1:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+", pwhash)
    assert weblogin.hash_password(PASSWORD, n_log2=15) != pwhash                        # a new salt every time
    assert weblogin.hash_password("pw", n_log2=17).startswith("scrypt:17:8:1:")          # the default cost


def test_malformed_and_weak_hash_strings_are_refused(pwhash):
    salt, key = pwhash.split(":")[4:]
    bad = ["", "x", "scrypt", "scrypt:15:8:1:" + salt, "bcrypt:15:8:1:" + salt + ":" + key,
           f"scrypt:14:8:1:{salt}:{key}",                      # below 2^15
           f"scrypt:15:8:1:{salt}:", f"scrypt:15:8:1::{key}",
           f"scrypt:abc:8:1:{salt}:{key}", f"scrypt:15:8:1:{salt}:{key}:extra",
           f"scrypt:15:8:1:!!!:{key}", f"scrypt:25:8:1:{salt}:{key}"]               # not base64, or absurdly costly
    for text in bad:
        with pytest.raises(ValueError):
            weblogin.parse_hash(text)
        assert weblogin.verify_password(PASSWORD, text) is False
    with pytest.raises(ValueError):
        weblogin.hash_password(PASSWORD, n_log2=14)


# --- T-WEB-73 login off ----------------------------------------------------------------------------------

def test_login_off_every_page_is_open_and_login_routes_do_not_exist(cfg, conn, tmp_path):
    ids, _ = pipeline(conn, cfg, [job(1)], [70])
    c = TestClient(web.create_app(cfg, tmp_path / "w.db"), base_url=HOST, follow_redirects=False)
    assert c.app.state.login is None
    for url in ("/", "/jobs", f"/jobs/{ids[0]}", "/analytics", "/health", "/missing"):
        assert c.get(url).status_code == 200, url
    assert c.get("/login").status_code == 404 and c.post("/login").status_code in (404, 405) and c.post("/logout").status_code in (404, 405)
    assert "Log out" not in c.get("/").text


# --- T-WEB-74 login on: the gate ---------------------------------------------------------------------------

def test_login_on_pages_redirect_to_the_form_and_writes_are_refused(site, conn):
    c, ids = site
    r = c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/"
    assert c.get("/jobs?page=2").headers["location"] == "/login?next=/jobs%3Fpage%3D2"
    assert c.head("/jobs").status_code == 303
    for url in (f"/jobs/{ids[0]}", f"/jobs/{ids[0]}/documents/cv/pdf", f"/jobs/{ids[0]}/documents/cv/approved-pdf", "/analytics", "/health", "/missing"):
        assert c.get(url).status_code == 303, url
    assert c.get("/static/style.css").status_code == 200
    assert c.get("/login").status_code == 200 and "csrf" in c.get("/login").text
    status = conn.execute("SELECT status FROM jobs WHERE id = ?", (ids[0],)).fetchone()[0]
    r = c.post(f"/jobs/{ids[0]}/shortlist", data={"csrf": c.app.state.csrf(f"shortlist:{ids[0]}"), "next": "job"})
    assert r.status_code == 403
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (ids[0],)).fetchone()[0] == status


def test_the_host_check_comes_before_the_login_gate(site):
    c, _ = site
    r = c.get("/", headers={"host": "evil.example"})
    assert r.status_code == 400


# --- T-WEB-75 a right login --------------------------------------------------------------------------------

def test_right_login_sets_a_signed_session_cookie(site):
    c, _ = site
    r = post_login(c)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = set_cookie(r).lower()
    assert cookie.startswith("recruitment_session=v1.") and "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "secure" not in cookie and "max-age" not in cookie and "expires" not in cookie and "domain" not in cookie
    assert c.get("/").status_code == 200 and "Log out" in c.get("/").text


def test_stay_logged_in_gives_thirty_days(site):
    c, _ = site
    r = post_login(c, stay=True)
    assert r.status_code == 303 and "max-age=2592000" in set_cookie(r).lower()


# --- T-WEB-76 wrong user or password -----------------------------------------------------------------------

def test_wrong_user_and_wrong_password_look_the_same_and_leave_no_trace(site, caplog, monkeypatch):
    c, _ = site
    monkeypatch.setattr(logging.getLogger("recruitment"), "propagate", True)
    with caplog.at_level(logging.INFO, logger="recruitment"):
        a = post_login(c, user="nobody-typed-this", password="wrong-secret-one")
        b = post_login(c, password="wrong-secret-two")
    assert a.status_code == b.status_code == 401 and a.text == b.text and "Wrong user name or password" in a.text
    assert not set_cookie(a) and not set_cookie(b) and not c.cookies
    assert "web: login failed from" in caplog.text
    for secret in ("nobody-typed-this", "wrong-secret-one", "wrong-secret-two", PASSWORD):
        assert secret not in caplog.text and secret not in a.text
    assert c.get("/").status_code == 303


def test_a_success_is_logged_without_secrets(site, caplog, monkeypatch):
    c, _ = site
    monkeypatch.setattr(logging.getLogger("recruitment"), "propagate", True)
    with caplog.at_level(logging.INFO, logger="recruitment"):
        post_login(c)
    assert "web: login from" in caplog.text and PASSWORD not in caplog.text


# --- T-WEB-77 throttle -------------------------------------------------------------------------------------

def test_five_failures_lock_the_address_even_for_the_right_password(site, clock):
    c, _ = site
    for _ in range(5):
        assert post_login(c, password="nope").status_code == 401
    r = post_login(c)                                                        # the right password, but locked
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0 and not set_cookie(r)
    clock.now += 14 * 60
    assert post_login(c).status_code == 429
    clock.now += 2 * 60
    assert post_login(c).status_code == 303


def test_throttle_unit_per_address_global_success_and_size():
    clock = Clock()
    t = weblogin.LoginThrottle(clock)
    for _ in range(4):
        t.record_failure("1.1.1.1")
    assert t.check("1.1.1.1") == 0
    t.record_failure("1.1.1.1")
    assert 0 < t.check("1.1.1.1") <= 900 and t.check("2.2.2.2") == 0
    clock.now += 901
    assert t.check("1.1.1.1") == 0
    for _ in range(4):
        t.record_failure("3.3.3.3")
    t.record_success("3.3.3.3")                                              # a success clears the address
    for _ in range(4):
        t.record_failure("3.3.3.3")
    assert t.check("3.3.3.3") == 0
    g = weblogin.LoginThrottle(Clock())
    for i in range(19):
        g.record_failure(f"10.0.0.{i}")
    assert g.check("10.9.9.9") == 0
    g.record_failure("10.0.1.1")                                             # the 20th failure within the hour: everybody waits
    assert g.check("10.9.9.9") > 0 and g.check("10.0.0.1") > 0
    s = weblogin.LoginThrottle(Clock())
    for i in range(2500):
        s.record_failure(f"ip{i}")
    assert len(s.failures) <= 1000


def _global_lock(t):
    for i in range(20):
        t.record_failure(f"10.0.0.{i}")


def test_known_good_address_passes_the_global_lock_but_not_its_own():
    clock = Clock()
    t = weblogin.LoginThrottle(clock)
    t.record_success("7.7.7.7")
    clock.now += 29 * 86400
    _global_lock(t)
    assert t.check("9.9.9.9") > 0 and t.check("10.0.0.1") > 0              # a fresh address is still stopped
    assert t.check("7.7.7.7") == 0                                           # the known one passes
    for _ in range(5):
        t.record_failure("7.7.7.7")
    assert t.check("7.7.7.7") > 0                                            # its own per-address lock still holds


def test_known_good_expires_after_30_days_and_is_capped():
    clock = Clock()
    t = weblogin.LoginThrottle(clock)
    t.record_success("7.7.7.7")
    clock.now += 31 * 86400
    _global_lock(t)
    assert t.check("7.7.7.7") > 0
    s = weblogin.LoginThrottle(Clock())
    for i in range(2500):
        s.record_success(f"ip{i}")
    assert len(s.known_good) <= 1000 and "ip2499" in s.known_good and "ip0" not in s.known_good


def test_known_good_address_logs_in_during_the_global_lock(site, clock):
    c, _ = site
    assert post_login(c).status_code == 303                                  # the testclient address becomes known
    for i in range(20):
        c.app.state.login.throttle.record_failure(f"10.0.0.{i}")
    assert c.app.state.login.throttle.check("1.2.3.4") > 0
    assert post_login(c).status_code == 303


def test_default_stay_cookie_max_age_is_90_days(monkeypatch, pwhash):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    login = weblogin.WebLogin(USER, pwhash, load_config()["web"]["login"]["stay_days"])
    assert login.make_cookie(True)[1] == 7776000
    assert login.make_cookie(False)[1] is None or login.make_cookie(False)[1] <= 12 * 3600


# --- T-WEB-78 cookies ----------------------------------------------------------------------------------------

def test_cookie_checks(pwhash, clock):
    login = weblogin.WebLogin(USER, pwhash, stay_days=30, clock=clock)
    value, max_age = login.make_cookie(stay=False)
    assert max_age is None and login.read_cookie(value) is True
    head, payload, sig = value.split(".")
    assert head == "v1"
    flipped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    for bad in ("", "x", f"v1.{payload}.{flipped}", f"v2.{payload}.{sig}", f"v1.{payload}", f"v1.{payload}x.{sig}", value + ".more"):
        assert login.read_cookie(bad) is False, bad
    other_user = weblogin.WebLogin("someone", pwhash, clock=clock)
    assert login.read_cookie(other_user.make_cookie(False)[0]) is False
    new_password = weblogin.WebLogin(USER, weblogin.hash_password(PASSWORD, n_log2=15), clock=clock)         # a new salt: every session ends
    assert login.read_cookie(new_password.make_cookie(False)[0]) is False
    clock.now += 12 * 3600 - 5
    assert login.read_cookie(value) is True
    clock.now += 10
    assert login.read_cookie(value) is False                                  # a session cookie lives 12 hours
    stay, age = login.make_cookie(stay=True)
    assert age == 30 * 86400
    clock.now += 30 * 86400 - 5
    assert login.read_cookie(stay) is True
    clock.now += 10
    assert login.read_cookie(stay) is False


def test_a_bad_cookie_in_the_browser_is_a_login_page(site):
    c, _ = site
    post_login(c)
    assert c.get("/").status_code == 200
    c.cookies.clear()
    c.cookies.set("recruitment_session", "v1.garbage.garbage")
    assert c.get("/").status_code == 303


def test_the_session_expires_after_twelve_hours(site, clock):
    c, _ = site
    post_login(c)
    clock.now += 13 * 3600
    assert c.get("/").status_code == 303


# --- T-WEB-79 the login form is protected too ------------------------------------------------------------------

def test_login_needs_its_token_and_a_local_origin(site):
    c, _ = site
    assert post_login(c, token=False).status_code == 403
    r = c.post("/login", data={"username": USER, "password": PASSWORD, "next": "/", "csrf": "0" * 64})
    assert r.status_code == 403
    r = post_login(c, origin="http://evil.example")
    assert r.status_code == 403 and not set_cookie(r)
    assert post_login(c, origin=HOST).status_code == 303


# --- T-WEB-80 logout ----------------------------------------------------------------------------------------

def test_logout_clears_the_cookie_and_needs_its_token(site):
    c, _ = site
    post_login(c)
    assert c.get("/logout").status_code == 405
    assert c.post("/logout", data={"x": "1"}).status_code == 403
    assert c.get("/").status_code == 200
    r = c.post("/logout", data={"csrf": c.app.state.csrf("logout")})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "max-age=0" in set_cookie(r).lower()
    assert c.get("/").status_code == 303


@pytest.mark.parametrize("state", ["expired", "garbage", "none"])
def test_logout_with_an_expired_or_bad_cookie_clears_it_and_goes_to_the_login_page(site, clock, state):
    c, _ = site
    if state == "expired":
        post_login(c)
        clock.now += 13 * 3600
    elif state == "garbage":
        c.cookies.set("recruitment_session", "v1.garbage.garbage")
    r = c.post("/logout", data={"csrf": c.app.state.csrf("logout")})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "max-age=0" in set_cookie(r).lower()
    assert c.post("/logout", data={"csrf": c.app.state.csrf("logout")}, headers={"origin": "http://evil.example"}).status_code == 403


def test_pages_show_a_logout_form_only_when_the_login_is_on(site):
    c, _ = site
    post_login(c)
    body = c.get("/").text
    assert 'action="/logout"' in body and c.app.state.csrf("logout") in body


# --- T-WEB-81 next ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["//evil.example", "http://x", "https://evil.example/a", "/\\evil.example", "\\\\evil", "javascript:alert(1)",
                                 "evil", "", "/a b", "/<script>", "/a\r\nSet-Cookie: x=1", "/" + "a" * 300])
def test_next_that_is_not_a_local_path_goes_to_the_top_page(site, bad):
    c, _ = site
    assert weblogin.safe_next(bad) == "/"
    assert post_login(c, nxt=bad).headers["location"] == "/"


def test_next_keeps_a_local_path(site):
    c, _ = site
    assert weblogin.safe_next("/jobs?page=2") == "/jobs?page=2" and weblogin.safe_next("/jobs/12") == "/jobs/12"
    assert weblogin.safe_next(None) == "/"
    assert post_login(c, nxt="/jobs/12").headers["location"] == "/jobs/12"


# --- T-WEB-82 serve and the container binding ----------------------------------------------------------------

def container_cfg(cfg, **web_keys):
    cfg["web"] = {**cfg["web"], "container_bind": True, "allowed_hosts": [NAS, "nas.example"],
                  "login": {"username": USER, "stay_days": 30}, **web_keys}
    return cfg


@pytest.fixture
def container_env(monkeypatch, pwhash):
    monkeypatch.setenv("RECRUITMENT_CONTAINER", "1")
    monkeypatch.setenv("RECRUITMENT_WEB_PASSWORD_HASH", pwhash)


def test_serve_in_the_container_binds_all_interfaces_with_a_login(cfg, container_env):
    calls = []
    assert web.serve(container_cfg(cfg), run=lambda app, **kw: calls.append((app, kw))) == 0
    app, kw = calls[0]
    assert kw["host"] == "0.0.0.0" and kw["port"] == 8000 and app.state.login is not None
    assert app.state.login.username == USER


@pytest.mark.parametrize("what", ["no_container_env", "no_allowed_hosts", "no_username", "no_hash", "bad_hash"])
def test_serve_refuses_a_container_bind_without_all_four_conditions(cfg, container_env, monkeypatch, what):
    c = container_cfg(cfg)
    if what == "no_container_env":
        monkeypatch.delenv("RECRUITMENT_CONTAINER")
    elif what == "no_allowed_hosts":
        c["web"]["allowed_hosts"] = []
    elif what == "no_username":
        c["web"]["login"]["username"] = ""
    elif what == "no_hash":
        monkeypatch.delenv("RECRUITMENT_WEB_PASSWORD_HASH")
    else:
        monkeypatch.setenv("RECRUITMENT_WEB_PASSWORD_HASH", "scrypt:14:8:1:aaaa:bbbb")
    started = []
    with pytest.raises(ConfigError):
        web.serve(c, run=lambda *a, **k: started.append(1))
    assert started == []


def test_the_container_env_value_must_be_exactly_one(cfg, container_env, monkeypatch):
    monkeypatch.setenv("RECRUITMENT_CONTAINER", "true")
    with pytest.raises(ConfigError, match="container"):
        web.serve(container_cfg(cfg), run=lambda *a, **k: None)


def test_host_flag_stays_local_only_in_the_container(cfg, container_env):
    with pytest.raises(ConfigError, match="127.0.0.1"):
        web.serve(container_cfg(cfg), host="0.0.0.0", run=lambda *a, **k: None)


# --- T-WEB-87 login on the PC is an opt-in ---------------------------------------------------------------------------

def test_without_a_username_and_without_the_container_the_login_is_off(cfg, container_env, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONTAINER")      # the PC: the container variable is not set (the container case is tested below)
    calls = []
    web.serve(cfg, run=lambda app, **kw: calls.append((app, kw)))
    assert calls[0][0].state.login is None and calls[0][1]["host"] == "127.0.0.1"


def test_a_username_on_the_pc_turns_the_login_on_at_127_0_0_1(cfg, pwhash, monkeypatch):
    monkeypatch.setenv("RECRUITMENT_WEB_PASSWORD_HASH", pwhash)
    cfg["web"]["login"]["username"] = USER
    calls = []
    web.serve(cfg, run=lambda app, **kw: calls.append((app, kw)))
    assert calls[0][1]["host"] == "127.0.0.1" and calls[0][0].state.login.username == USER
    assert TestClient(calls[0][0], base_url=HOST, follow_redirects=False).get("/").status_code == 303


def test_a_username_without_a_hash_is_refused_at_start(cfg, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_WEB_PASSWORD_HASH", raising=False)
    cfg["web"]["login"]["username"] = USER
    started = []
    with pytest.raises(ConfigError, match="RECRUITMENT_WEB_PASSWORD_HASH"):
        web.serve(cfg, run=lambda *a, **k: started.append(1))
    assert started == []


def test_the_web_command_exits_3_on_a_refused_container_bind(config_file, container_env, monkeypatch, capsys):
    monkeypatch.delenv("RECRUITMENT_WEB_PASSWORD_HASH")
    config_file.write_text(config_file.read_text(encoding="utf-8") + f"web:\n  container_bind: true\n  allowed_hosts: ['{NAS}']\n"
                           f"  login:\n    username: {USER}\n", encoding="utf-8")
    calls = []
    assert cli.main(["--config", str(config_file), "web"], web_runner=lambda *a, **k: calls.append(1)) == 3
    assert "configuration error" in capsys.readouterr().err and calls == []


# --- T-WEB-83 the Host allow-list --------------------------------------------------------------------------------------

def test_host_check_in_container_mode(cfg, container_env, tmp_path, conn):
    pipeline(conn, cfg, [job(1)], [70])
    cfg["paths"]["database"] = str(tmp_path / "w.db")
    apps = []
    web.serve(container_cfg(cfg), run=lambda app, **kw: apps.append(app))
    c = TestClient(apps[0], base_url=f"http://{NAS}:8000", follow_redirects=False)
    assert c.get("/login").status_code == 200                                   # the allowed address is served
    assert c.get("/login", headers={"host": "nas.example"}).status_code == 200
    assert c.get("/login", headers={"host": "127.0.0.1:8000"}).status_code == 200      # the local names stay served
    for bad in ("evil.example", "100.64.1.3:8000", "0.0.0.0", "", "nas.example.evil"):
        assert c.get("/login", headers={"host": bad}).status_code == 400, bad
    r = c.post("/login", data={"username": USER, "password": PASSWORD, "next": "/", "csrf": apps[0].state.csrf("login")},
               headers={"origin": f"http://{NAS}:8000"})
    assert r.status_code == 303                                                  # the Origin must be http:// plus the Host header


# --- T-WEB-84 config ------------------------------------------------------------------------------------------------

def test_web_config_new_keys_defaults(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    w = load_config()["web"]
    assert w["container_bind"] is False and w["allowed_hosts"] == [] and w["login"] == {"username": "", "stay_days": 90}


@pytest.mark.parametrize("text,key", [
    ("container_bind: 'yes'", "web.container_bind"), ("container_bind: 1", "web.container_bind"),
    ("allowed_hosts: nas", "web.allowed_hosts"), ("allowed_hosts: [5]", "web.allowed_hosts"),
    ("allowed_hosts: ['http://nas']", "web.allowed_hosts"), ("allowed_hosts: ['nas:8000']", "web.allowed_hosts"),
    ("allowed_hosts: ['*']", "web.allowed_hosts"), ("allowed_hosts: ['0.0.0.0']", "web.allowed_hosts"),
    ("allowed_hosts: ['::']", "web.allowed_hosts"), ("allowed_hosts: ['NAS']", "web.allowed_hosts"),
    ("allowed_hosts: ['']", "web.allowed_hosts"), ("allowed_hosts: ['a b']", "web.allowed_hosts"),
    ("login: {username: 'a:b'}", "web.login.username"), ("login: {username: 5}", "web.login.username"),
    ("login: {username: \"a\\nb\"}", "web.login.username"),
    ("login: {stay_days: 0}", "web.login.stay_days"), ("login: {stay_days: 91}", "web.login.stay_days"),
    ("login: {stay_days: true}", "web.login.stay_days"), ("login: {stay_days: '30'}", "web.login.stay_days"),
])
def test_web_config_new_keys_are_checked(tmp_path, monkeypatch, text, key):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    p = tmp_path / "c.yaml"
    p.write_text(f"web:\n  {text}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=re.escape(key)):
        load_config(p)


def test_web_config_new_keys_accept_good_values(tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    p = tmp_path / "c.yaml"
    p.write_text("web:\n  container_bind: true\n  allowed_hosts: ['100.64.1.2', 'nas.local', 'fd00::1', 'localhost']\n"
                 "  login:\n    username: alex\n    stay_days: 90\n", encoding="utf-8")
    w = load_config(p)["web"]
    assert w["container_bind"] is True and w["allowed_hosts"][2] == "fd00::1" and w["login"] == {"username": "alex", "stay_days": 90}
    p.write_text("web:\n  login:\n    typo: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(p)


def test_container_bind_alone_does_not_fail_other_commands(config_file):
    config_file.write_text(config_file.read_text(encoding="utf-8") + "web:\n  container_bind: true\n", encoding="utf-8")
    assert cli.main(["--config", str(config_file), "status"]) == 0


# --- T-WEB-85 set-web-password -------------------------------------------------------------------------------------

def answers(monkeypatch, *texts):
    queue = list(texts)
    prompts = []

    def fake(prompt="Password: ", stream=None):
        prompts.append(prompt)
        return queue.pop(0)

    monkeypatch.setattr(getpass, "getpass", fake)
    return prompts


def test_set_web_password_prints_one_verifiable_line(monkeypatch, capsys):
    prompts = answers(monkeypatch, "a long enough password", "a long enough password")
    assert cli.main(["set-web-password"]) == 0
    out, err = capsys.readouterr()
    assert len(prompts) == 2
    (line,) = [ln for ln in out.splitlines() if ln.startswith("RECRUITMENT_WEB_PASSWORD_HASH=")]
    assert weblogin.verify_password("a long enough password", line.split("=", 1)[1]) is True
    assert "scrypt:17:8:1:" in line and "a long enough password" not in out + err and ".env" in out


def test_set_web_password_refuses_a_mismatch_and_a_short_password(monkeypatch, capsys):
    answers(monkeypatch, "a long enough password", "another long password")
    assert cli.main(["set-web-password"]) == 1
    out, err = capsys.readouterr()
    assert "RECRUITMENT_WEB_PASSWORD_HASH=" not in out and "do not match" in err
    answers(monkeypatch, "short", "short")
    assert cli.main(["set-web-password"]) == 1
    out, err = capsys.readouterr()
    assert "RECRUITMENT_WEB_PASSWORD_HASH=" not in out and "12 characters" in err and "short" not in out


def test_set_web_password_writes_no_file_and_needs_no_config(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    answers(monkeypatch, "a long enough password", "a long enough password")
    assert cli.main(["--config", str(tmp_path / "does-not-exist.yaml"), "set-web-password"]) == 0
    assert list(tmp_path.iterdir()) == []


# --- review fixes (4 Nov 2026) -----------------------------------------------------------------------------------------

def test_concurrent_wrong_passwords_cannot_outrun_the_throttle(cfg, conn, tmp_path, login):
    """The check, the scrypt and the failure record are one step: ten wrong passwords at once plus the right one
    give at most five 401, the rest 429, and no session."""
    pipeline(conn, cfg, [job(1)], [55])
    app = web.create_app(cfg, tmp_path / "w.db", login=login)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=HOST) as ac:
            def post(password):
                return ac.post("/login", data={"username": USER, "password": password, "next": "/", "csrf": app.state.csrf("login")})
            return await asyncio.gather(*[post("nope") for _ in range(10)], post(PASSWORD))

    codes = [r.status_code for r in asyncio.run(run())]
    assert codes.count(401) <= 5 and codes.count(303) == 0 and codes.count(429) >= 6, codes


def test_scrypt_cost_above_the_memory_limit_is_refused(pwhash):
    salt, key = pwhash.split(":")[4:]
    assert weblogin.SCRYPT_MAX_LOG2 == 17
    with pytest.raises(ValueError):
        weblogin.parse_hash(f"scrypt:18:8:1:{salt}:{key}")
    with pytest.raises(ValueError):
        weblogin.hash_password(PASSWORD, n_log2=18)
    weblogin.parse_hash(f"scrypt:17:8:1:{salt}:{key}")


def test_a_container_flag_in_dot_env_is_not_loaded(tmp_path, monkeypatch):
    from app.config import load_env
    monkeypatch.delenv("RECRUITMENT_CONTAINER", raising=False)
    monkeypatch.delenv("SOME_OTHER_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("RECRUITMENT_CONTAINER=1\nSOME_OTHER_KEY=x\n", encoding="utf-8")
    assert "RECRUITMENT_CONTAINER" not in load_env(env) and "RECRUITMENT_CONTAINER" not in os.environ
    assert os.environ["SOME_OTHER_KEY"] == "x"
    monkeypatch.delenv("SOME_OTHER_KEY")


def test_the_web_command_does_not_take_the_container_flag_from_dot_env(config_file, pwhash, tmp_path, monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONTAINER", raising=False)
    (tmp_path / ".env").write_text("RECRUITMENT_CONTAINER=1\n", encoding="utf-8")
    monkeypatch.setenv("RECRUITMENT_WEB_PASSWORD_HASH", pwhash)
    config_file.write_text(config_file.read_text(encoding="utf-8") + f"web:\n  container_bind: true\n  allowed_hosts: ['{NAS}']\n"
                           f"  login:\n    username: {USER}\n", encoding="utf-8")
    from app import config as config_module
    monkeypatch.setattr(config_module, "ROOT", tmp_path)
    calls = []
    assert cli.main(["--config", str(config_file), "web"], web_runner=lambda *a, **k: calls.append(1)) == 3
    assert calls == []


def test_the_container_without_container_bind_does_not_start(cfg, pwhash, monkeypatch):
    monkeypatch.setenv("RECRUITMENT_CONTAINER", "1")
    monkeypatch.setenv("RECRUITMENT_WEB_PASSWORD_HASH", pwhash)
    cfg["web"] = {**cfg["web"], "container_bind": False}
    calls = []
    with pytest.raises(ConfigError, match="container_bind"):
        web.serve(cfg, run=lambda app, **kw: calls.append(1))
    assert calls == []
