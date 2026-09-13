"""Token persistence.

Two backends behind one interface: the OS keychain (default) and a plain JSON
file (opt-in, for Linux boxes and CI where no keyring daemon is running).
Either way the *index* of connected providers lives in a small plaintext file,
because keyrings can't be enumerated.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

KEYRING_SERVICE = "open-banking-mcp"
DEFAULT_DIR = Path.home() / ".open-banking-mcp"

# FCA rules cap a consent at 90 days, after which the refresh token is dead and
# the user has to re-authorise in a browser.
CONSENT_DAYS = 90


class StorageError(RuntimeError):
    """Raised when tokens cannot be read or written."""


class Token(BaseModel):
    provider_id: str
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime
    scope: str = ""
    connected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_response(
        cls,
        provider_id: str,
        payload: dict,
        *,
        connected_at: datetime | None = None,
    ) -> "Token":
        expires_in = int(payload.get("expires_in", 3600))
        return cls(
            provider_id=provider_id,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            scope=payload.get("scope", ""),
            connected_at=connected_at or datetime.now(timezone.utc),
        )

    def is_expired(self, leeway_seconds: int = 60) -> bool:
        """True if the access token is gone or about to be."""
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=leeway_seconds)
        return self.expires_at <= cutoff

    @property
    def consent_expires_at(self) -> datetime:
        return self.connected_at + timedelta(days=CONSENT_DAYS)

    @property
    def consent_days_left(self) -> int:
        delta = self.consent_expires_at - datetime.now(timezone.utc)
        # Round up: a countdown showing "0 days" should mean expired, not
        # "expires in another 23 hours".
        return max(0, math.ceil(delta.total_seconds() / 86400))


class TokenStore(Protocol):
    def get(self, provider_id: str) -> Token | None: ...
    def put(self, token: Token) -> None: ...
    def delete(self, provider_id: str) -> None: ...
    def providers(self) -> list[str]: ...


class _ProviderIndex:
    """Plaintext list of connected provider ids, grouped by environment.

    Contains no secrets. Grouping by environment matters: a sandbox
    connection showing up in production would let a mock bank's balance be
    reported as real money.
    """

    def __init__(self, path: Path, env: str) -> None:
        self.path = path
        self.env = env

    def _read_all(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {}
        if isinstance(data, list):
            # Pre-namespacing format: a bare list written before environments
            # were separated. Those connections were sandbox-only.
            return {"sandbox": data}
        return data if isinstance(data, dict) else {}

    def read(self) -> list[str]:
        return sorted(set(self._read_all().get(self.env, [])))

    def add(self, provider_id: str) -> None:
        data = self._read_all()
        current = set(data.get(self.env, []))
        current.add(provider_id)
        data[self.env] = sorted(current)
        self._write(data)

    def remove(self, provider_id: str) -> None:
        data = self._read_all()
        data[self.env] = [p for p in data.get(self.env, []) if p != provider_id]
        self._write(data)

    def _write(self, data: dict[str, list[str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))


class KeyringTokenStore:
    """Stores each provider's tokens as one keychain entry, per environment."""

    def __init__(self, env: str = "sandbox", index_path: Path | None = None) -> None:
        self.env = env
        self._index = _ProviderIndex(
            index_path or DEFAULT_DIR / "providers.json", env
        )

    def _account(self, provider_id: str) -> str:
        """Keychain account name. Namespaced so live and sandbox never mix."""
        return f"{self.env}:{provider_id}"

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise StorageError(
                "The 'keyring' package is required for keychain storage. "
                "Install it, or set TRUELAYER_TOKEN_FILE to use file storage."
            ) from exc
        return keyring

    def get(self, provider_id: str) -> Token | None:
        keyring = self._keyring()
        raw = keyring.get_password(KEYRING_SERVICE, self._account(provider_id))
        if not raw:
            raw = self._migrate_legacy(provider_id)
        if not raw:
            return None
        return Token.model_validate_json(raw)

    def _migrate_legacy(self, provider_id: str) -> str | None:
        """Adopt an entry written before keys were namespaced by environment.

        Those predate the sandbox/production split, so they can only have been
        sandbox; claiming one as production could report a mock balance as real
        money.
        """
        if self.env != "sandbox":
            return None
        keyring = self._keyring()
        raw = keyring.get_password(KEYRING_SERVICE, provider_id)
        if not raw:
            return None
        keyring.set_password(KEYRING_SERVICE, self._account(provider_id), raw)
        try:
            keyring.delete_password(KEYRING_SERVICE, provider_id)
        except Exception:
            pass
        return raw

    def put(self, token: Token) -> None:
        self._keyring().set_password(
            KEYRING_SERVICE, self._account(token.provider_id), token.model_dump_json()
        )
        self._index.add(token.provider_id)

    def delete(self, provider_id: str) -> None:
        keyring = self._keyring()
        try:
            keyring.delete_password(KEYRING_SERVICE, self._account(provider_id))
        except Exception:
            # Already gone, or the backend has nothing to delete. Either way the
            # index entry below is what matters.
            pass
        self._index.remove(provider_id)

    def providers(self) -> list[str]:
        return self._index.read()


class JsonFileTokenStore:
    """Stores every provider's tokens in one 0600 JSON file, keyed by env."""

    def __init__(self, path: Path | None = None, env: str = "sandbox") -> None:
        self.path = path or DEFAULT_DIR / "tokens.json"
        self.env = env

    def _key(self, provider_id: str) -> str:
        return f"{self.env}:{provider_id}"

    def _read_all(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise StorageError(f"{self.path} is not valid JSON: {exc}") from exc

    def _write_all(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive permissions before any secret is written.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, default=str)

    def get(self, provider_id: str) -> Token | None:
        raw = self._read_all().get(self._key(provider_id))
        return Token.model_validate(raw) if raw else None

    def put(self, token: Token) -> None:
        data = self._read_all()
        data[self._key(token.provider_id)] = json.loads(token.model_dump_json())
        self._write_all(data)

    def delete(self, provider_id: str) -> None:
        data = self._read_all()
        if data.pop(self._key(provider_id), None) is not None:
            self._write_all(data)

    def providers(self) -> list[str]:
        prefix = f"{self.env}:"
        return sorted(
            key[len(prefix):] for key in self._read_all() if key.startswith(prefix)
        )


def build_store(settings) -> TokenStore:
    """Pick a backend from settings, falling back to a file if no keyring works."""
    if not settings.use_keyring:
        return JsonFileTokenStore(settings.token_file, settings.env)

    store = KeyringTokenStore(settings.env)
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        if isinstance(keyring.get_keyring(), FailKeyring):
            raise StorageError("no usable keyring backend")
    except (ImportError, StorageError):
        return JsonFileTokenStore(settings.token_file, settings.env)
    return store
