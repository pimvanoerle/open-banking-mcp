"""End-to-end exercise of the interactive consent flow.

The real localhost callback server runs; a fake 'browser' plays the part of the
bank and hits the redirect. Only TrueLayer's HTTP endpoints are mocked.
"""

import threading
import urllib.parse
import urllib.request

import httpx
import pytest
import respx

from open_banking_mcp.auth import AuthError, authorize
from open_banking_mcp.storage import JsonFileTokenStore


def _fake_bank(redirect_uri, *, code="auth-code-123", state=None, error=None, delay=0.05):
    """Return a webbrowser.open stand-in that redirects like a bank would."""

    def opener(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        params = {"state": state or query["state"][0]}
        if error:
            params["error"] = error
        else:
            params["code"] = code

        def hit():
            # urllib, not httpx -- respx must not intercept this one.
            urllib.request.urlopen(f"{redirect_uri}?{urllib.parse.urlencode(params)}").read()

        threading.Timer(delay, hit).start()
        return True

    return opener


def _mock_truelayer(settings, *, provider_id="uk-cs-mock", refresh_token="rt-1"):
    payload = {"access_token": "at-1", "expires_in": 3600, "scope": settings.scopes}
    if refresh_token:
        payload["refresh_token"] = refresh_token
    respx.post(settings.token_endpoint).mock(return_value=httpx.Response(200, json=payload))
    respx.get(f"{settings.api_base_url}/me").mock(
        return_value=httpx.Response(
            200, json={"results": [{"provider": {"provider_id": provider_id}}]}
        )
    )


@respx.mock
async def test_full_flow_stores_token_keyed_by_provider(settings, tmp_path, monkeypatch):
    _mock_truelayer(settings, provider_id="uk-ob-monzo")
    monkeypatch.setattr(
        "open_banking_mcp.auth.webbrowser.open", _fake_bank(settings.redirect_uri)
    )
    store = JsonFileTokenStore(tmp_path / "t.json")

    token = await authorize(settings, store, timeout=10)

    assert token.provider_id == "uk-ob-monzo"
    assert token.access_token == "at-1"
    assert token.refresh_token == "rt-1"
    assert store.providers() == ["uk-ob-monzo"]
    assert store.get("uk-ob-monzo").access_token == "at-1"


@respx.mock
async def test_pkce_verifier_matches_the_challenge_sent(settings, tmp_path, monkeypatch):
    import base64
    import hashlib
    import json

    _mock_truelayer(settings)
    sent = {}

    def capture(url):
        sent.update(
            {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        )
        return _fake_bank(settings.redirect_uri)(url)

    monkeypatch.setattr("open_banking_mcp.auth.webbrowser.open", capture)
    await authorize(settings, JsonFileTokenStore(tmp_path / "t.json"), timeout=10)

    token_post = next(
        c.request for c in respx.calls if c.request.url == settings.token_endpoint
    )
    verifier = json.loads(token_post.content)["code_verifier"]
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert sent["code_challenge"] == expected
    assert sent["code_challenge_method"] == "S256"


@respx.mock
async def test_state_mismatch_is_rejected(settings, tmp_path, monkeypatch):
    _mock_truelayer(settings)
    monkeypatch.setattr(
        "open_banking_mcp.auth.webbrowser.open",
        _fake_bank(settings.redirect_uri, state="attacker-supplied"),
    )
    store = JsonFileTokenStore(tmp_path / "t.json")

    with pytest.raises(AuthError, match="State mismatch"):
        await authorize(settings, store, timeout=10)
    assert store.providers() == []


@respx.mock
async def test_bank_error_is_surfaced(settings, tmp_path, monkeypatch):
    _mock_truelayer(settings)
    monkeypatch.setattr(
        "open_banking_mcp.auth.webbrowser.open",
        _fake_bank(settings.redirect_uri, error="access_denied"),
    )

    with pytest.raises(AuthError, match="access_denied"):
        await authorize(settings, JsonFileTokenStore(tmp_path / "t.json"), timeout=10)


@respx.mock
async def test_timeout_when_user_never_consents(settings, tmp_path, monkeypatch):
    _mock_truelayer(settings)
    monkeypatch.setattr("open_banking_mcp.auth.webbrowser.open", lambda url: True)

    with pytest.raises(AuthError, match="Timed out"):
        await authorize(settings, JsonFileTokenStore(tmp_path / "t.json"), timeout=1)


@respx.mock
async def test_port_in_use_gives_actionable_error(settings, tmp_path, monkeypatch):
    import http.server

    parsed = urllib.parse.urlparse(settings.redirect_uri)
    blocker = http.server.HTTPServer((parsed.hostname, parsed.port), http.server.BaseHTTPRequestHandler)
    monkeypatch.setattr("open_banking_mcp.auth.webbrowser.open", lambda url: True)
    try:
        with pytest.raises(AuthError, match="TRUELAYER_REDIRECT_URI"):
            await authorize(settings, JsonFileTokenStore(tmp_path / "t.json"), timeout=5)
    finally:
        blocker.server_close()
