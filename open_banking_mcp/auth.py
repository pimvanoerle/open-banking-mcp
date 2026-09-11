"""OAuth 2.0 authorisation code flow against TrueLayer.

The interactive half (`authorize`) runs once per bank, from the CLI: it opens a
browser, catches the redirect on a throwaway localhost server, and swaps the
code for tokens. The unattended half (`TokenManager`) is what the MCP server
uses at request time, and only ever refreshes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import secrets
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from typing import Any

import httpx

from .config import Settings
from .storage import Token, TokenStore

# TrueLayer's token endpoint takes JSON, not the form encoding most OAuth
# providers use. Sending form data here gets a confusing 400.
_JSON_HEADERS = {"Content-Type": "application/json"}

_SUCCESS_PAGE = b"""<!doctype html>
<html><head><title>Connected</title><style>
body{font-family:system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
div{text-align:center}h1{font-weight:600}p{color:#666}
</style></head>
<body><div><h1>Bank connected</h1><p>You can close this tab and return to the terminal.</p></div></body></html>
"""

_FAILURE_PAGE = b"""<!doctype html>
<html><head><title>Failed</title><style>
body{font-family:system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
div{text-align:center}h1{font-weight:600;color:#b00}p{color:#666}
</style></head>
<body><div><h1>Authorisation failed</h1><p>Check the terminal for details.</p></div></body></html>
"""


class AuthError(RuntimeError):
    """Raised when the OAuth flow or a token refresh fails."""


class ConsentExpiredError(AuthError):
    """Raised when the refresh token is dead and the user must re-authorise."""


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def build_auth_url(
    settings: Settings, *, state: str, code_challenge: str | None = None
) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "scope": settings.scopes,
        "providers": settings.providers,
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    # TrueLayer requires %20 for the spaces in scope/providers; the default
    # "+" form encoding is rejected by their auth endpoint.
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{settings.auth_base_url}/?{query}"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        # Ignore favicon and other stray requests the browser makes.
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return

        type(self).result = {k: v[0] for k, v in query.items()}
        body = _SUCCESS_PAGE if "code" in query else _FAILURE_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log."""


def _wait_for_callback(redirect_uri: str, timeout: float) -> dict[str, str]:
    parsed = urllib.parse.urlparse(redirect_uri)
    host, port = parsed.hostname or "localhost", parsed.port or 80

    _CallbackHandler.result = {}
    try:
        server = http.server.HTTPServer((host, port), _CallbackHandler)
    except OSError as exc:
        raise AuthError(
            f"Could not listen on {host}:{port} for the OAuth redirect ({exc}). "
            f"Something else may be using that port -- set TRUELAYER_REDIRECT_URI "
            f"to a free one (and register it in the TrueLayer console)."
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = threading.Event()
        waited = 0.0
        while not _CallbackHandler.result and waited < timeout:
            deadline.wait(0.25)
            waited += 0.25
    finally:
        server.shutdown()
        server.server_close()

    if not _CallbackHandler.result:
        raise AuthError(f"Timed out after {timeout:.0f}s waiting for the bank redirect.")
    return _CallbackHandler.result


async def _post_token(settings: Settings, payload: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            settings.token_endpoint, json=payload, headers=_JSON_HEADERS
        )
    if response.status_code >= 400:
        raise AuthError(
            f"Token request failed ({response.status_code}): {response.text.strip()}"
        )
    return response.json()


async def exchange_code(
    settings: Settings, code: str, code_verifier: str | None = None
) -> dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
        "code": code,
        "redirect_uri": settings.redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    return await _post_token(settings, payload)


async def refresh_access_token(settings: Settings, refresh_token: str) -> dict[str, Any]:
    try:
        return await _post_token(
            settings,
            {
                "grant_type": "refresh_token",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "refresh_token": refresh_token,
            },
        )
    except AuthError as exc:
        # A refused refresh almost always means the 90-day consent lapsed or the
        # user revoked access in their banking app.
        if "400" in str(exc) or "401" in str(exc):
            raise ConsentExpiredError(str(exc)) from exc
        raise


async def identify_provider(settings: Settings, access_token: str) -> str:
    """Ask TrueLayer which bank this token belongs to.

    The token response doesn't say, and we key storage by provider so that
    several banks can be connected at once.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{settings.api_base_url}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code >= 400:
        return "unknown"
    results = response.json().get("results") or []
    if not results:
        return "unknown"
    provider = results[0].get("provider") or {}
    return provider.get("provider_id") or results[0].get("provider_id") or "unknown"


async def authorize(
    settings: Settings,
    store: TokenStore,
    *,
    use_pkce: bool = True,
    timeout: float = 300.0,
    open_browser: bool = True,
) -> Token:
    """Run the full interactive consent flow and persist the resulting token."""
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair() if use_pkce else (None, None)
    url = build_auth_url(settings, state=state, code_challenge=challenge)

    # flush=True: stdout is block-buffered when this is piped or run
    # non-interactively, which would hide the URL until the flow ends.
    print(f"Opening your browser to connect a bank...\n\n  {url}\n", flush=True)
    if open_browser:
        webbrowser.open(url)

    callback = await asyncio.to_thread(_wait_for_callback, settings.redirect_uri, timeout)

    if "error" in callback:
        raise AuthError(
            f"The bank returned an error: {callback['error']} "
            f"{callback.get('error_description', '')}".strip()
        )
    if callback.get("state") != state:
        raise AuthError(
            "State mismatch on the OAuth redirect -- possible CSRF. Aborting."
        )

    payload = await exchange_code(settings, callback["code"], verifier)
    if not payload.get("refresh_token"):
        print(
            "Warning: no refresh token was returned. Without 'offline_access' in "
            "TRUELAYER_SCOPES this connection will stop working in an hour.",
            flush=True,
        )

    provider_id = await identify_provider(settings, payload["access_token"])
    token = Token.from_response(provider_id, payload)
    store.put(token)
    return token


class TokenManager:
    """Hands out valid access tokens, refreshing transparently."""

    def __init__(self, settings: Settings, store: TokenStore) -> None:
        self._settings = settings
        self._store = store
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, provider_id: str) -> asyncio.Lock:
        # One lock per provider so parallel tool calls don't each burn a refresh
        # (TrueLayer rotates the refresh token, so a race can invalidate it).
        if provider_id not in self._locks:
            self._locks[provider_id] = asyncio.Lock()
        return self._locks[provider_id]

    def providers(self) -> list[str]:
        return self._store.providers()

    async def access_token(self, provider_id: str) -> str:
        async with self._lock_for(provider_id):
            token = self._store.get(provider_id)
            if token is None:
                raise AuthError(
                    f"No saved connection for {provider_id!r}. "
                    f"Run 'open-banking-mcp auth' to connect a bank."
                )
            if not token.is_expired():
                return token.access_token
            if not token.refresh_token:
                raise ConsentExpiredError(
                    f"The access token for {provider_id} expired and there is no "
                    f"refresh token. Run 'open-banking-mcp auth' to reconnect."
                )

            payload = await refresh_access_token(self._settings, token.refresh_token)
            refreshed = Token.from_response(
                provider_id,
                payload,
                # Refreshing does not restart the 90-day consent window.
                connected_at=token.connected_at,
            )
            if not refreshed.refresh_token:
                # Verified against the TrueLayer sandbox: a refresh response
                # often omits refresh_token entirely rather than rotating it.
                # Dropping it here would break every subsequent refresh.
                refreshed = refreshed.model_copy(
                    update={"refresh_token": token.refresh_token}
                )
            self._store.put(refreshed)
            return refreshed.access_token

    def consent_status(self) -> list[tuple[str, int, datetime]]:
        """(provider_id, days_left, expires_at) for every saved connection."""
        rows = []
        for provider_id in self._store.providers():
            token = self._store.get(provider_id)
            if token:
                rows.append(
                    (provider_id, token.consent_days_left, token.consent_expires_at)
                )
        return rows
