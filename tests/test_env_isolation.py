import os

from app import cli, config


def test_cli_main_does_not_load_the_real_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ISOLATION_PROBE_KEY=made-up\n", encoding="utf-8")
    monkeypatch.delenv("ISOLATION_PROBE_KEY", raising=False)
    monkeypatch.setattr(config, "ROOT", tmp_path)               # what an unstubbed load_env() would read
    cli.load_env()                                              # the name cli.main() calls
    assert "ISOLATION_PROBE_KEY" not in os.environ


def test_config_load_env_itself_still_works(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ISOLATION_PROBE_KEY=made-up\n", encoding="utf-8")
    monkeypatch.delenv("ISOLATION_PROBE_KEY", raising=False)
    assert config.load_env(env_file) == {"ISOLATION_PROBE_KEY": ""}
    os.environ.pop("ISOLATION_PROBE_KEY", None)


def test_load_env_skips_the_claude_token_and_every_anthropic_setting(tmp_path, monkeypatch):      # G7 (v0.78.0)
    env_file = tmp_path / ".env"
    env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=made-up-token\nANTHROPIC_API_KEY=sk-ant-made-up\nANTHROPIC_AUTH_TOKEN=x\n"
                        "ANTHROPIC_BASE_URL=https://example.invalid\nISOLATION_PROBE_KEY=made-up\n", encoding="utf-8")
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ISOLATION_PROBE_KEY"):
        monkeypatch.delenv(name, raising=False)
    loaded = config.load_env(env_file)
    assert loaded == {"ISOLATION_PROBE_KEY": ""}                       # the token and the API settings never reach the containers that hold the database
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        assert name not in os.environ
    os.environ.pop("ISOLATION_PROBE_KEY", None)
