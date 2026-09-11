import stat
from datetime import datetime, timedelta, timezone

from open_banking_mcp.storage import JsonFileTokenStore, Token


def _token(**kw):
    defaults = dict(
        provider_id="uk-cs-mock",
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        scope="accounts balance",
    )
    return Token(**{**defaults, **kw})


def test_roundtrip_and_permissions(tmp_path):
    store = JsonFileTokenStore(tmp_path / "tokens.json")
    store.put(_token())

    loaded = store.get("uk-cs-mock")
    assert loaded.access_token == "at"
    assert loaded.refresh_token == "rt"
    assert store.providers() == ["uk-cs-mock"]

    mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, expected 0600"


def test_multiple_providers_and_delete(tmp_path):
    store = JsonFileTokenStore(tmp_path / "tokens.json")
    store.put(_token(provider_id="uk-ob-monzo"))
    store.put(_token(provider_id="uk-oauth-starling"))
    assert store.providers() == ["uk-oauth-starling", "uk-ob-monzo"]

    store.delete("uk-ob-monzo")
    assert store.providers() == ["uk-oauth-starling"]
    assert store.get("uk-ob-monzo") is None


def test_expiry_uses_leeway():
    assert _token(expires_at=datetime.now(timezone.utc) + timedelta(seconds=30)).is_expired()
    assert not _token(expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)).is_expired()


def test_consent_window_counts_from_connection():
    connected = datetime.now(timezone.utc) - timedelta(days=80)
    token = _token(connected_at=connected)
    assert token.consent_days_left == 10


def test_from_response_sets_expiry():
    token = Token.from_response(
        "uk-cs-mock",
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "accounts"},
    )
    remaining = (token.expires_at - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < remaining <= 3600
