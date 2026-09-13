"""Sandbox and production must never see each other's data.

A mock bank balance reported as real money is the failure this prevents.
"""

import json

import pytest

from open_banking_mcp.config import load_settings
from open_banking_mcp.storage import JsonFileTokenStore, Token, _ProviderIndex


def _token(provider_id="uk-ob-monzo"):
    from datetime import datetime, timedelta, timezone

    return Token(
        provider_id=provider_id,
        access_token="at",
        refresh_token="rt",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def test_token_stores_are_isolated_by_environment(tmp_path):
    path = tmp_path / "tokens.json"
    sandbox = JsonFileTokenStore(path, "sandbox")
    production = JsonFileTokenStore(path, "production")

    sandbox.put(_token("mock"))
    production.put(_token("uk-ob-monzo"))

    assert sandbox.providers() == ["mock"]
    assert production.providers() == ["uk-ob-monzo"]
    assert sandbox.get("uk-ob-monzo") is None
    assert production.get("mock") is None


def test_deleting_in_one_environment_leaves_the_other(tmp_path):
    path = tmp_path / "tokens.json"
    sandbox = JsonFileTokenStore(path, "sandbox")
    production = JsonFileTokenStore(path, "production")
    sandbox.put(_token("shared-id"))
    production.put(_token("shared-id"))

    sandbox.delete("shared-id")

    assert sandbox.providers() == []
    assert production.providers() == ["shared-id"], "wrong environment was cleared"


def test_provider_index_is_grouped_by_environment(tmp_path):
    path = tmp_path / "providers.json"
    _ProviderIndex(path, "sandbox").add("mock")
    _ProviderIndex(path, "production").add("uk-ob-monzo")

    assert _ProviderIndex(path, "sandbox").read() == ["mock"]
    assert _ProviderIndex(path, "production").read() == ["uk-ob-monzo"]
    assert set(json.loads(path.read_text())) == {"sandbox", "production"}


def test_legacy_flat_index_is_treated_as_sandbox(tmp_path):
    """Indexes written before namespacing held sandbox connections only."""
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(["mock"]))

    assert _ProviderIndex(path, "sandbox").read() == ["mock"]
    assert _ProviderIndex(path, "production").read() == []


def test_cache_file_differs_per_environment(monkeypatch):
    monkeypatch.setenv("TRUELAYER_CLIENT_ID", "x")
    monkeypatch.setenv("TRUELAYER_CLIENT_SECRET", "y")
    monkeypatch.delenv("TRUELAYER_CACHE_FILE", raising=False)

    monkeypatch.setenv("TRUELAYER_ENV", "sandbox")
    sandbox = load_settings().cache_file
    monkeypatch.setenv("TRUELAYER_ENV", "production")
    production = load_settings().cache_file

    assert sandbox != production
    assert "sandbox" in sandbox.name and "production" in production.name


def test_explicit_cache_file_is_respected(monkeypatch, tmp_path):
    monkeypatch.setenv("TRUELAYER_CLIENT_ID", "x")
    monkeypatch.setenv("TRUELAYER_CLIENT_SECRET", "y")
    monkeypatch.setenv("TRUELAYER_CACHE_FILE", str(tmp_path / "mine.db"))
    assert load_settings().cache_file == tmp_path / "mine.db"


class _FakeKeyring:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def get_password(self, service, account):
        return self.store.get((service, account))

    def set_password(self, service, account, value):
        self.store[(service, account)] = value

    def delete_password(self, service, account):
        del self.store[(service, account)]


def test_legacy_keychain_entry_is_migrated_into_sandbox(monkeypatch, tmp_path):
    from open_banking_mcp.storage import KEYRING_SERVICE, KeyringTokenStore

    legacy = {(KEYRING_SERVICE, "mock"): _token("mock").model_dump_json()}
    fake = _FakeKeyring(legacy)
    monkeypatch.setattr(KeyringTokenStore, "_keyring", staticmethod(lambda: fake))

    store = KeyringTokenStore("sandbox", tmp_path / "providers.json")
    found = store.get("mock")

    assert found is not None and found.access_token == "at"
    assert (KEYRING_SERVICE, "sandbox:mock") in fake.store, "not re-keyed"
    assert (KEYRING_SERVICE, "mock") not in fake.store, "legacy entry left behind"


def test_legacy_keychain_entry_is_never_claimed_by_production(monkeypatch, tmp_path):
    """A pre-namespacing token can only have been sandbox."""
    from open_banking_mcp.storage import KEYRING_SERVICE, KeyringTokenStore

    fake = _FakeKeyring({(KEYRING_SERVICE, "mock"): _token("mock").model_dump_json()})
    monkeypatch.setattr(KeyringTokenStore, "_keyring", staticmethod(lambda: fake))

    store = KeyringTokenStore("production", tmp_path / "providers.json")
    assert store.get("mock") is None
    assert (KEYRING_SERVICE, "mock") in fake.store, "production consumed a sandbox token"
