import pytest

from open_banking_mcp.config import ConfigError, load_settings


@pytest.fixture(autouse=True)
def _base_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("TRUELAYER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TRUELAYER_CLIENT_ID", "sandbox-x")
    monkeypatch.setenv("TRUELAYER_CLIENT_SECRET", "y")


def test_defaults_are_sandbox_and_daily_cache():
    s = load_settings()
    assert s.env == "sandbox"
    assert s.auth_base_url == "https://auth.truelayer-sandbox.com"
    assert s.api_base_url == "https://api.truelayer-sandbox.com/data/v1"
    # A daily sync plus slack, so yesterday's sync isn't flagged stale.
    assert s.max_age_hours == 25.0
    assert s.history_days == 365
    assert s.psu_ip is None


def test_production_switches_both_hosts(monkeypatch):
    monkeypatch.setenv("TRUELAYER_ENV", "production")
    s = load_settings()
    assert s.auth_base_url == "https://auth.truelayer.com"
    assert s.api_base_url == "https://api.truelayer.com/data/v1"
    assert s.providers == "uk-ob-all uk-oauth-all"


def test_missing_credentials_name_the_variable(monkeypatch):
    monkeypatch.delenv("TRUELAYER_CLIENT_SECRET")
    with pytest.raises(ConfigError, match="TRUELAYER_CLIENT_SECRET"):
        load_settings()


def test_unknown_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("TRUELAYER_ENV", "staging")
    with pytest.raises(ConfigError, match="sandbox"):
        load_settings()


def test_token_file_implies_file_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUELAYER_TOKEN_FILE", str(tmp_path / "t.json"))
    assert load_settings().use_keyring is False


def test_keyring_is_the_default():
    assert load_settings().use_keyring is True


def test_non_numeric_max_age_is_rejected(monkeypatch):
    monkeypatch.setenv("TRUELAYER_MAX_AGE_HOURS", "soon")
    with pytest.raises(ConfigError, match="must be a number"):
        load_settings()


def test_cache_and_psu_ip_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUELAYER_CACHE_FILE", str(tmp_path / "c.db"))
    monkeypatch.setenv("TRUELAYER_MAX_AGE_HOURS", "2")
    monkeypatch.setenv("TRUELAYER_PSU_IP", "1.2.3.4")
    s = load_settings()
    assert s.cache_file == tmp_path / "c.db"
    assert s.max_age_hours == 2.0
    assert s.psu_ip == "1.2.3.4"
