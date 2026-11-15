"""The test suite must not touch the user's own files or the network (see conftest.py)."""
import imaplib
import smtplib
import urllib.request

import pytest

from app import config
from app.config import load_config


def test_the_users_config_is_not_read():
    assert not config.DEFAULT_USER_CONFIG.exists()
    assert load_config()["descriptions"]["fetch_enabled"] is False          # the default, whatever config.yaml says


def test_an_explicit_config_file_still_works(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("scoring:\n  top_n: 3\n", encoding="utf-8")
    assert load_config(p)["scoring"]["top_n"] == 3


@pytest.mark.parametrize("call", [lambda: urllib.request.urlopen("https://example.com"),
                                  lambda: imaplib.IMAP4_SSL("imap.example.com"),
                                  lambda: smtplib.SMTP("smtp.example.com", 587)])
def test_the_network_is_blocked(call):
    with pytest.raises(AssertionError, match="network"):
        call()


def test_a_test_can_still_fake_the_network(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: "faked")
    assert urllib.request.urlopen("https://example.com") == "faked"
