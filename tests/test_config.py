import pytest

from app.config import ConfigError, load_config


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_load_without_user_file(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    cfg = load_config()
    assert cfg["location"]["limit_minutes"] == 60
    assert cfg["location"]["car_traffic_factor"] == 1.25
    assert cfg["scoring"]["top_n"] == 10
    assert cfg["filters"]["title_include"] == []


def test_override_merges_key_by_key(tmp_path):
    cfg = load_config(write(tmp_path, "location:\n  limit_minutes: 45\n"))
    assert cfg["location"]["limit_minutes"] == 45
    assert cfg["location"]["arrival_time"] == "08:30"  # untouched default


def test_unknown_key_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="unknown config key: location.limit_minute"):
        load_config(write(tmp_path, "location:\n  limit_minute: 45\n"))


def test_explicit_missing_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_env_variable_selects_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RECRUITMENT_CONFIG", str(write(tmp_path, "scoring:\n  top_n: 5\n")))
    assert load_config()["scoring"]["top_n"] == 5


@pytest.mark.parametrize("text, message", [
    ("location:\n  limit_minutes: 0\n", "limit_minutes"),
    ("location:\n  limit_minutes: abc\n", "limit_minutes"),
    ("location:\n  arrival_time: '25:00'\n", "arrival_time"),
    ("location:\n  car_traffic_factor: 0.5\n", "car_traffic_factor"),
    ("filters:\n  title_exclude: ['(']\n", "invalid regular expression"),
    ("filters:\n  excluded_companies: acme\n", "list of strings"),
    ("scoring:\n  band_low_below: 80\n  band_high_from: 70\n", "band_low_below"),
    ("scoring:\n  batch_cap: 0\n", "batch_cap"),
    ("scoring:\n  top_n: 0\n", "top_n"),
    ("email:\n  smtp_port: 99999\n", "smtp_port"),
    ("email:\n  recipient: nobody\n", "recipient"),
    ("location: 5\n", "must be a mapping"),
    ("- a\n- b\n", "mapping at the top level"),
    ("a: [\n", "not valid YAML"),
])
def test_invalid_values_are_rejected(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_empty_user_file_means_defaults(tmp_path):
    assert load_config(write(tmp_path, ""))["location"]["limit_minutes"] == 60


def test_batch_cap_null_means_no_cap(tmp_path):
    assert load_config(write(tmp_path, "scoring:\n  batch_cap: 20\n"))["scoring"]["batch_cap"] == 20
    assert load_config(write(tmp_path, "scoring:\n  batch_cap: null\n"))["scoring"]["batch_cap"] is None


def test_example_file_has_no_home_address(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    assert load_config()["location"]["car_origin_address"] == ""


def _user_agents(node, found=None):
    found = [] if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "user_agent":
                found.append(v)
            else:
                _user_agents(v, found)
    elif isinstance(node, list):
        for v in node:
            _user_agents(v, found)
    return found


def test_no_default_user_agent_contains_an_at_sign(monkeypatch):
    """4 Nov 2026: the User-Agent is a generic client name with no contact (no e-mail, no URL)."""
    import yaml
    from app import resolver
    from app.config import EXAMPLE_PATH
    agents = [a for a in _user_agents(yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))) if a]
    assert len(agents) >= 3
    for a in agents:
        assert "@" not in a
        assert "http" not in a and a.startswith("Recruitment-pipeline/")
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    cfg = load_config()
    assert "@" not in resolver.user_agent(cfg) and "@" not in cfg["adapters"]["user_agent"]


def test_headless_poll_seconds_above_120_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="poll_seconds"):
        load_config(write(tmp_path, "headless:\n  poll_seconds: 600\n"))
