import asyncio
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from open_banking_mcp.auth import (
    AuthError,
    ConsentExpiredError,
    TokenManager,
    authorize,
    build_auth_url,
    exchange_code,
    refresh_access_token,
)
from open_banking_mcp.storage import JsonFileTokenStore, Token


def _query(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def test_auth_url_carries_every_required_param(settings):
    params = _query(build_auth_url(settings, state="xyz", code_challenge="chal"))
    assert params["response_type"] == "code"
    assert params["client_id"] == "sandbox-test"
    assert params["redirect_uri"] == settings.redirect_uri
    assert params["scope"] == settings.scopes
    assert params["providers"] == "uk-cs-mock"
    assert params["state"] == "xyz"
    assert params["code_challenge"] == "chal"
    assert params["code_challenge_method"] == "S256"


def test_auth_url_omits_pkce_when_unused(settings):
    params = _query(build_auth_url(settings, state="xyz"))
    assert "code_challenge" not in params


def test_sandbox_urls(settings):
    assert settings.token_endpoint == "https://auth.truelayer-sandbox.com/connect/token"
    assert settings.api_base_url == "https://api.truelayer-sandbox.com/data/v1"


@respx.mock
async def test_exchange_code_posts_json_not_form(settings):
    route = respx.post(settings.token_endpoint).mock(
        return_value=httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
    )
    await exchange_code(settings, "the-code", "verifier")

    request = route.calls.last.request
    assert request.headers["content-type"] == "application/json"
    import json as _json

    body = _json.loads(request.content)
    assert body["grant_type"] == "authorization_code"
    assert body["client_secret"] == "secret-abc"
    assert body["code"] == "the-code"
    assert body["code_verifier"] == "verifier"
    assert body["redirect_uri"] == settings.redirect_uri


@respx.mock
async def test_refused_refresh_becomes_consent_expired(settings):
    respx.post(settings.token_endpoint).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(ConsentExpiredError):
        await refresh_access_token(settings, "dead-token")


@respx.mock
async def test_manager_refreshes_and_preserves_consent_window(settings, tmp_path):
    store = JsonFileTokenStore(tmp_path / "t.json")
    connected = datetime.now(timezone.utc) - timedelta(days=40)
    store.put(
        Token(
            provider_id="uk-cs-mock",
            access_token="old",
            refresh_token="rt-1",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            connected_at=connected,
        )
    )
    respx.post(settings.token_endpoint).mock(
        return_value=httpx.Response(
            200, json={"access_token": "new", "refresh_token": "rt-2", "expires_in": 3600}
        )
    )

    token = await TokenManager(settings, store).access_token("uk-cs-mock")
    assert token == "new"

    saved = store.get("uk-cs-mock")
    assert saved.refresh_token == "rt-2", "rotated refresh token must be persisted"
    # Refreshing must not restart the 90-day clock.
    assert saved.connected_at.date() == connected.date()
    assert saved.consent_days_left == 50


@respx.mock
async def test_manager_keeps_old_refresh_token_when_not_rotated(settings, tmp_path):
    store = JsonFileTokenStore(tmp_path / "t.json")
    store.put(
        Token(
            provider_id="uk-cs-mock",
            access_token="old",
            refresh_token="rt-1",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )
    respx.post(settings.token_endpoint).mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    )

    await TokenManager(settings, store).access_token("uk-cs-mock")
    assert store.get("uk-cs-mock").refresh_token == "rt-1"


@respx.mock
async def test_manager_skips_refresh_when_token_still_valid(settings, tmp_path):
    store = JsonFileTokenStore(tmp_path / "t.json")
    store.put(
        Token(
            provider_id="uk-cs-mock",
            access_token="still-good",
            refresh_token="rt",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
    )
    route = respx.post(settings.token_endpoint).mock(return_value=httpx.Response(500))

    assert await TokenManager(settings, store).access_token("uk-cs-mock") == "still-good"
    assert not route.called


async def test_manager_errors_helpfully_for_unknown_provider(settings, tmp_path):
    manager = TokenManager(settings, JsonFileTokenStore(tmp_path / "t.json"))
    with pytest.raises(AuthError, match="open-banking-mcp auth"):
        await manager.access_token("uk-ob-monzo")


def test_scope_spaces_use_percent20_not_plus(settings):
    """TrueLayer's auth endpoint rejects '+' as a space in the scope list."""
    url = build_auth_url(settings, state="s")
    assert "info%20accounts%20balance%20offline_access" in url
    assert "+" not in url
