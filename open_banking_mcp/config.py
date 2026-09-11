"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SANDBOX = "sandbox"
PRODUCTION = "production"

_AUTH_BASE = {
    SANDBOX: "https://auth.truelayer-sandbox.com",
    PRODUCTION: "https://auth.truelayer.com",
}
_API_BASE = {
    SANDBOX: "https://api.truelayer-sandbox.com/data/v1",
    PRODUCTION: "https://api.truelayer.com/data/v1",
}

# offline_access is what gets us a refresh token; without it the connection
# dies after an hour and there is nothing to renew.
DEFAULT_SCOPES = (
    "info accounts balance transactions cards direct_debits standing_orders offline_access"
)

# uk-cs-mock is the sandbox mock bank; the -all wildcards cover every real
# provider TrueLayer supports under each auth type.
DEFAULT_PROVIDERS = {
    SANDBOX: "uk-cs-mock",
    PRODUCTION: "uk-ob-all uk-oauth-all",
}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    env: str
    scopes: str
    providers: str
    token_file: Path | None
    use_keyring: bool

    @property
    def auth_base_url(self) -> str:
        return _AUTH_BASE[self.env]

    @property
    def api_base_url(self) -> str:
        return _API_BASE[self.env]

    @property
    def token_endpoint(self) -> str:
        return f"{self.auth_base_url}/connect/token"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. See the README for how to get one from "
            f"https://console.truelayer.com"
        )
    return value


def load_settings() -> Settings:
    env = os.environ.get("TRUELAYER_ENV", SANDBOX).strip().lower()
    if env not in _AUTH_BASE:
        raise ConfigError(
            f"TRUELAYER_ENV must be '{SANDBOX}' or '{PRODUCTION}', got {env!r}"
        )

    token_file = os.environ.get("TRUELAYER_TOKEN_FILE", "").strip()

    return Settings(
        client_id=_require("TRUELAYER_CLIENT_ID"),
        client_secret=_require("TRUELAYER_CLIENT_SECRET"),
        redirect_uri=os.environ.get(
            "TRUELAYER_REDIRECT_URI", "http://localhost:8080/callback"
        ).strip(),
        env=env,
        scopes=os.environ.get("TRUELAYER_SCOPES", DEFAULT_SCOPES).strip(),
        providers=os.environ.get("TRUELAYER_PROVIDERS", DEFAULT_PROVIDERS[env]).strip(),
        token_file=Path(token_file).expanduser() if token_file else None,
        # An explicit token file means the user asked for file storage.
        use_keyring=not token_file
        and os.environ.get("TRUELAYER_USE_KEYRING", "1").strip() not in ("0", "false"),
    )
